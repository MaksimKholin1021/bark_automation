"""
Bark -> Google Sheet lead sync.

Only leads that appear after the current baseline are appended.
Name goes in the Name column. The full lead text is a note on the
Description cell.
"""

from __future__ import annotations

import json
import logging
import re
import msvcrt
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

from bark_accounts import account_for
from bark_scraper import CreditsRequired, Lead, fetch_leads, fingerprints, reveal_contact
from gmail_client import (
    NAME_MAILBOX,
    bark_subjects_by_account,
    chrome_is_open,
    full_name_from_subject,
)
from sheets_client import GoogleSheet, _first_token, _name_key, _plain_name, exist_display_name, sheet_name

BASE_DIR = Path(__file__).resolve().parent
SEEN_PATH = BASE_DIR / "leads_seen.json"
PENDING_PATH = BASE_DIR / "contact_pending.json"
AWAITING_PATH = BASE_DIR / "contact_awaiting.json"
SEEN_MAIL_PATH = BASE_DIR / "bark_names_seen.json"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(BASE_DIR / "sync.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("bark_sync")


def load_seen() -> set[str]:
    if not SEEN_PATH.exists():
        return set()
    try:
        data = json.loads(SEEN_PATH.read_text(encoding="utf-8"))
        return set(data.get("fingerprints", []))
    except Exception as exc:
        logger.warning("Could not read %s: %s", SEEN_PATH, exc)
        return set()


def save_seen(fps: set[str]) -> None:
    SEEN_PATH.write_text(
        json.dumps({"fingerprints": sorted(fps)}, indent=2),
        encoding="utf-8",
    )


def env_bool(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def load_pending() -> list[dict]:
    if not PENDING_PATH.exists():
        return []
    try:
        data = json.loads(PENDING_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Could not read %s: %s", PENDING_PATH, exc)
        return []
    return data if isinstance(data, list) else []


def save_pending(rows: list[dict]) -> None:
    PENDING_PATH.write_text(json.dumps(rows, indent=2), encoding="utf-8")


def load_awaiting() -> list[dict]:
    if not AWAITING_PATH.exists():
        return []
    try:
        data = json.loads(AWAITING_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Could not read %s: %s", AWAITING_PATH, exc)
        return []
    return data if isinstance(data, list) else []


def save_awaiting(rows: list[dict]) -> None:
    AWAITING_PATH.write_text(json.dumps(rows, indent=2), encoding="utf-8")


def remember_awaiting(item: dict) -> None:
    awaiting = load_awaiting()
    row = item.get("row")
    if any(existing.get("row") == row and existing.get("no") == item.get("no") for existing in awaiting):
        return
    awaiting.append(
        {
            "row": row,
            "no": item.get("no"),
            "name": item.get("name") or "",
            "clicked_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    save_awaiting(awaiting)


def queue_contacts(leads: list[Lead], placed: list[tuple[int, int | str]]) -> None:
    pending = load_pending()
    known = {int(item["row"]) for item in pending if str(item.get("row", "")).isdigit()}
    for lead, (row, number) in zip(leads, placed):
        if not isinstance(number, int) or row in known:
            continue
        pending.append(
            {
                "row": row,
                "no": number,
                "name": lead.name,
                "location": lead.location,
                "category": lead.category,
                "attempts": 0,
            }
        )
    save_pending(pending)


def reveal_pending(sheet: GoogleSheet, *, headless: bool) -> None:
    """Click Contact on the Bark account that matches each lead's No."""
    pending = load_pending()
    if not pending:
        return
    still: list[dict] = []
    for item in pending:
        number = item.get("no")
        if not isinstance(number, int):
            continue
        account = account_for(number)
        if account is None:
            logger.warning("No Bark account mapped for No %s", number)
            still.append(item)
            continue
        email, password = account
        lead = Lead(
            name=str(item.get("name") or ""),
            content="",
            location=str(item.get("location") or ""),
            category=str(item.get("category") or ""),
        )
        try:
            reveal_contact(
                email,
                password,
                lead,
                session_path=BASE_DIR / "sessions" / f"{number}.json",
                headless=headless,
            )
        except CreditsRequired:
            logger.error(
                "No %s needs credits to contact %s. The buy button was not clicked.",
                number,
                lead.name,
            )
            continue
        except Exception:
            item["attempts"] = int(item.get("attempts") or 0) + 1
            logger.exception(
                "Contact failed for %s on No %s (attempt %s)",
                lead.name,
                number,
                item["attempts"],
            )
            if item["attempts"] < 4:
                still.append(item)
            continue
        logger.info("Clicked Contact for %s using No %s (%s)", lead.name, number, email)
        remember_awaiting(item)
    save_pending(still)


def _is_open_short_name(name: str) -> bool:
    """A cell still waiting for Bark's email: one name, no star, and not an (exist) row."""
    if not name or "*" in name or "(exist)" in name.lower():
        return False
    return len(_plain_name(name).split()) == 1


def _mail_key(subject: str) -> str:
    return re.sub(r"\s+", " ", subject or "").strip().lower()


def load_seen_mail() -> set[str]:
    if not SEEN_MAIL_PATH.exists():
        return set()
    try:
        data = json.loads(SEEN_MAIL_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Could not read %s: %s", SEEN_MAIL_PATH, exc)
        return set()
    subjects = data.get("subjects", []) if isinstance(data, dict) else []
    return {str(item) for item in subjects}


def save_seen_mail(subjects: set[str]) -> None:
    SEEN_MAIL_PATH.write_text(
        json.dumps({"subjects": sorted(subjects)}, indent=2),
        encoding="utf-8",
    )


def apply_bark_names(sheet: GoogleSheet) -> None:
    """Write a full name only when a new team@bark.com email arrives.

    All of those emails are forwarded to spearbig1021. A name already stored
    on another row is left alone. When the email name matches the cell, the
    cell becomes "Lonnie (*)".
    """
    awaiting = load_awaiting()
    now = datetime.now(timezone.utc)
    if not chrome_is_open():
        logger.info("Gmail is not open for reading, so new full names were not checked")
        return

    try:
        open_mail = bark_subjects_by_account()
    except Exception:
        logger.exception("Could not read spearbig1021")
        return
    if NAME_MAILBOX not in open_mail:
        logger.info("No open spearbig1021 tab. Tabs seen: %s", ", ".join(open_mail) or "(none)")
        return

    seen = load_seen_mail()
    fresh = [subject for subject in open_mail[NAME_MAILBOX] if _mail_key(subject) not in seen]
    rows = sheet.name_rows() if fresh else []
    short_rows = [
        row for row in rows[-60:] if _is_open_short_name(row["name"]) and str(row["no"]).isdigit()
    ]
    filled: set[int] = set()
    for subject in fresh:
        seen.add(_mail_key(subject))
        full_name = full_name_from_subject(subject)
        if not full_name:
            continue
        _apply_arrived_name(sheet, rows, short_rows, full_name, filled)
    if fresh:
        save_seen_mail(seen)

    still: list[dict] = []
    for item in awaiting:
        clicked = _parse_time(str(item.get("clicked_at") or ""))
        row_number = int(item.get("row") or 0)
        current = next((row for row in rows if row["row"] == row_number), None)
        if current is not None and not _is_open_short_name(current["name"]):
            continue
        if row_number in filled:
            continue
        if clicked is not None and now - clicked > timedelta(hours=6):
            logger.warning(
                "No new Bark full-name email for %s on No %s after 6 hours",
                item.get("name"),
                item.get("no"),
            )
            continue
        still.append(item)
    save_awaiting(still)


def _apply_arrived_name(
    sheet: GoogleSheet,
    rows: list[dict],
    short_rows: list[dict],
    full_name: str,
    filled: set[int],
) -> None:
    """Put one newly arrived name on the single lead that is still waiting for it."""
    first = _first_token(full_name)
    waiting = [
        row
        for row in short_rows
        if row["row"] not in filled and _first_token(row["name"]) == first
    ]
    if len(waiting) != 1:
        if len(waiting) > 1:
            logger.info(
                "Left %s waiting %r leads alone. %r could belong to more than one city",
                len(waiting),
                first,
                full_name,
            )
        return
    row = waiting[0]
    same_name = _name_key(full_name) == _name_key(row["name"])
    if not same_name and sheet.full_name_is_other_city(int(row["row"]), full_name, rows):
        logger.info(
            "New mail %r was not written on %r. That name is already a different city",
            full_name,
            row["name"],
        )
        return
    if sheet.write_full_name(int(row["row"]), row["name"], full_name, rows):
        filled.add(int(row["row"]))
        logger.info("New Bark mail for No %s %s -> %s", row["no"], row["name"], full_name)


def _parse_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def sync_once(
    sheet: GoogleSheet,
    *,
    email: str,
    password: str,
    headless: bool,
    deep: bool = False,
    notes_only: bool = False,
) -> int:
    sheet.begin_cycle()
    apply_bark_names(sheet)
    reveal_pending(sheet, headless=headless)
    seen = load_seen()
    sheet_keys = sheet.existing_keys()
    wanted = sheet.names_needing_notes() if deep else None
    leads = fetch_leads(email, password, headless=headless, deep=deep, wanted=wanted)
    current_fps = fingerprints(leads)

    # First run: baseline only — do not flood the sheet with existing leads.
    if not seen:
        baseline = set(current_fps)
        for key in sheet_keys:
            if len(key) == 64 and all(c in "0123456789abcdef" for c in key):
                baseline.add(key)
        logger.info(
            "First run baseline: marking %s current Bark lead(s) as seen (no sheet writes).",
            len(current_fps),
        )
        save_seen(baseline)
        return 0

    # Bark lists the newest lead first. Insert only leads above the first one
    # already stored. Older leads below that are left alone. An edited name
    # such as "Siddharth Ray" or "Gary docter" still counts when it is the same
    # lead. An empty starred placeholder such as "Richard *" does not.
    # A new posting that matches an older row is inserted once as
    # "Full Name (exist)" with that row's note and no No, so Contact is skipped.
    new_leads: list[Lead] = []
    exist_leads: list[Lead] = []
    for lead in leads:
        matches = sheet.matching_rows(lead)
        loose = f"{lead.name.lower()}|{lead.content.lower()}"
        if matches or loose in sheet_keys:
            latest = max(matches, key=lambda row: row["index"]) if matches else None
            already_marked = any("(exist)" in row["name"].lower() for row in matches)
            if latest and not sheet.is_recent_listing(latest) and not already_marked:
                exist_leads.append(
                    Lead(
                        name=exist_display_name(latest["name"]),
                        content=(latest.get("note") or lead.content or "").strip(),
                        location=lead.location,
                        category=lead.category,
                    )
                )
            break
        new_leads.append(lead)

    if leads:
        logger.info("Newest on Bark: %s — %s", leads[0].name, leads[0].content[:100].replace("\n", " | "))
    sheet.update_detail_notes(leads)

    if notes_only:
        logger.info("Detail notes refreshed from %s Bark lead(s).", len(leads))
        save_seen(seen | current_fps)
        return 0

    if not new_leads and not exist_leads:
        logger.info("No new leads this cycle (%s visible on Bark).", len(leads))
        # Keep seen set from growing stale with currently visible leads only:
        # retain old seen + current so leads that scroll off stay remembered.
        save_seen(seen | current_fps)
        return 0

    # The repeat lead is the oldest of this batch. Append it first, then the
    # newer names, so the newest lead is the last row.
    batch = exist_leads + list(reversed(new_leads))
    written, placed = sheet.append_leads(batch)
    queue_contacts(batch, placed)
    reveal_pending(sheet, headless=headless)
    save_seen(seen | current_fps | {lead.fingerprint for lead in batch})
    for lead in batch:
        label = "EXIST" if "(exist)" in lead.name.lower() else "NEW"
        logger.info("%s: %s — %s", label, sheet_name(lead), lead.content[:120])
    return written


def main() -> None:
    load_dotenv(BASE_DIR / ".env")
    lock_handle = open(BASE_DIR / "sync.lock", "a+")
    try:
        msvcrt.locking(lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        raise SystemExit("A Bark sync is already running.")

    email = os.getenv("BARK_EMAIL", "").strip()
    password = os.getenv("BARK_PASSWORD", "").strip()
    sheet_id = os.getenv("GOOGLE_SHEET_ID", "").strip()
    creds_path = os.getenv("GOOGLE_CREDENTIALS_PATH", "credentials.json").strip()
    gid = int(os.getenv("GOOGLE_SHEET_GID", "0") or "0")
    interval = int(os.getenv("POLL_INTERVAL_MINUTES", "5") or "5")
    headless = env_bool("HEADLESS", True)
    once = "--once" in sys.argv
    dry_run = "--dry-run" in sys.argv
    notes_only = "--notes-only" in sys.argv

    if not email or not password:
        raise SystemExit("BARK_EMAIL and BARK_PASSWORD must be set in .env")

    if dry_run:
        logger.info("Dry run: fetching Bark leads only (no Google Sheet writes)")
        leads = fetch_leads(email, password, headless=headless)
        for lead in leads[:20]:
            logger.info("%s | %s", lead.name, lead.content[:160])
        logger.info("Total leads visible: %s", len(leads))
        return

    if not sheet_id:
        raise SystemExit("GOOGLE_SHEET_ID must be set in .env")

    creds_full = Path(creds_path)
    if not creds_full.is_absolute():
        creds_full = BASE_DIR / creds_full

    sheet = GoogleSheet(creds_full, sheet_id, gid=gid)

    logger.info(
        "Starting Bark -> Google Sheet sync (every %s min, headless=%s)",
        interval,
        headless,
    )

    while True:
        try:
            sync_once(
                sheet,
                email=email,
                password=password,
                headless=headless,
                deep=notes_only,
                notes_only=notes_only,
            )
        except Exception:
            logger.exception("Sync cycle failed")

        if once:
            break
        time.sleep(max(interval, 1) * 60)


if __name__ == "__main__":
    main()
