"""Read Bark full names from Gmail subjects.

Bark emails team@bark.com with a subject such as
"You're one step away from speaking to Daniel Faina".
Only the subject is used.
"""

from __future__ import annotations

import email
import imaplib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from email.utils import parsedate_to_datetime
from pathlib import Path

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
ACCOUNTS_PATH = BASE_DIR / "gmail_accounts.json"
SPEAKING_TO = re.compile(r"speaking to\s+(.+)$", re.I)


def gmail_for(number: int) -> tuple[str, str] | None:
    account = gmail_account(number)
    if account is None:
        return None
    return account["email"], account["password"]


def gmail_account(number: int) -> dict | None:
    if not ACCOUNTS_PATH.exists():
        return None
    rows = json.loads(ACCOUNTS_PATH.read_text(encoding="utf-8"))
    for row in rows:
        nos = {int(item) for item in row.get("nos", [])}
        if int(number) in nos:
            return {
                "email": str(row["email"]).strip(),
                "password": str(row["password"]),
                "recovery": str(row.get("recovery") or "").strip(),
            }
    return None


def accounts_by_email() -> dict[str, dict]:
    if not ACCOUNTS_PATH.exists():
        return {}
    rows = json.loads(ACCOUNTS_PATH.read_text(encoding="utf-8"))
    found: dict[str, dict] = {}
    for row in rows:
        email_address = str(row["email"]).strip()
        found[email_address] = {
            "email": email_address,
            "password": str(row["password"]),
            "recovery": str(row.get("recovery") or "").strip(),
        }
    return found


_JUNK_SUBJECT = re.compile(
    r"\b(leads?|device|security|password|verify|verification|customers?|suspicious|inbox|credits?|unread|notification|sign[- ]in|account|closest)\b",
    re.I,
)


def full_name_from_subject(subject: str) -> str:
    text = re.sub(r"\s+", " ", subject or "").strip()
    match = re.search(r"(?:speaking to|contacting)\s+(.+?)(?:\s+to help|\s+-\s+you could|$)", text, re.I)
    if match:
        name = match.group(1).strip(" .-|")
    else:
        name = re.sub(r"\s+-\s+Bark\.com.*$", "", text, flags=re.I).strip()
    if name.lower() in {"your pro", "a pro", "bark.com"}:
        return ""
    if _JUNK_SUBJECT.search(name):
        return ""
    words = name.split()
    if not words or len(words) > 6:
        return ""
    if any(any(char.isdigit() for char in word) for word in words):
        return ""
    if not re.search(r"[A-Za-z]", name):
        return ""
    return name


def bark_name_messages(email_address: str, password: str, *, since: datetime) -> list[dict]:
    """Subjects from team@bark.com on or after `since`."""
    since_utc = since.astimezone(timezone.utc) - timedelta(days=1)
    with imaplib.IMAP4_SSL("imap.gmail.com") as mailbox:
        mailbox.login(email_address, password)
        mailbox.select("INBOX", readonly=True)
        day = since_utc.strftime("%d-%b-%Y")
        status, data = mailbox.search(None, "FROM", '"team@bark.com"', "SINCE", day)
        if status != "OK" or not data or not data[0]:
            return []
        ids = data[0].split()[-60:]
        found: list[dict] = []
        for msg_id in ids:
            status, fetched = mailbox.fetch(msg_id, "(BODY.PEEK[HEADER.FIELDS (SUBJECT DATE)])")
            if status != "OK" or not fetched:
                continue
            raw = b""
            for part in fetched:
                if isinstance(part, tuple) and len(part) > 1:
                    raw += part[1]
            header = email.message_from_bytes(raw)
            subject = _decode_header(header.get("Subject", ""))
            full_name = full_name_from_subject(subject)
            if not full_name:
                continue
            sent = _sent_at(header.get("Date", ""))
            if sent is None or sent < since.astimezone(timezone.utc) - timedelta(minutes=5):
                continue
            found.append({"name": full_name, "sent_at": sent, "subject": subject})
        return found


def _decode_header(value: str) -> str:
    pieces: list[str] = []
    for text, encoding in decode_header(value or ""):
        if isinstance(text, bytes):
            pieces.append(text.decode(encoding or "utf-8", errors="replace"))
        else:
            pieces.append(text)
    return "".join(pieces).strip()


CHROME_DEBUG_URL = "http://127.0.0.1:9222"
# Every Bark full-name email is forwarded into this inbox.
NAME_MAILBOX = "spearbig1021@gmail.com"


def chrome_is_open() -> bool:
    import urllib.request

    try:
        with urllib.request.urlopen(CHROME_DEBUG_URL + "/json/version", timeout=2) as response:
            return response.status == 200
    except Exception:
        return False


def bark_subjects_by_account() -> dict[str, list[str]]:
    """Read Bark subjects from the one Gmail that receives every full name.

    Napoleon and Raven mail is forwarded into spearbig1021. Chrome must be the
    window started with remote debugging. This does not launch a browser or sign in.
    """
    from playwright.sync_api import sync_playwright

    if not chrome_is_open():
        raise RuntimeError("Gmail Chrome window is not open")
    found: dict[str, list[str]] = {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(CHROME_DEBUG_URL)
        fallback = None
        for context in browser.contexts:
            fallback = context
            for page in context.pages:
                if "mail.google.com/mail" not in page.url:
                    continue
                email_address = _page_account(page)
                if email_address != NAME_MAILBOX:
                    continue
                account_index = "0"
                match = re.search(r"/mail/u/(\d+)/", page.url)
                if match:
                    account_index = match.group(1)
                _open_bark_search(page, account_index)
                found[NAME_MAILBOX] = _visible_subjects(page)
                return found
        if fallback is not None and fallback.pages:
            page = fallback.pages[0]
            page.goto("https://mail.google.com/mail/u/0/", wait_until="commit", timeout=60000)
            page.wait_for_timeout(2500)
            if "accounts.google.com" in page.url:
                logger.info("Sign in to %s in the debugging Chrome window and leave it open", NAME_MAILBOX)
                return found
            if _page_account(page) == NAME_MAILBOX:
                _open_bark_search(page, "0")
                found[NAME_MAILBOX] = _visible_subjects(page)
                return found
    logger.info("No open Gmail tab for %s", NAME_MAILBOX)
    return found


def _open_bark_search(page, account_index: str) -> None:
    page.goto(
        "https://mail.google.com/mail/u/"
        + account_index
        + "/#search/from%3Ateam%40bark.com+newer_than%3A2d+"
        + "(speaking+to+OR+contacting)",
        wait_until="commit",
        timeout=60000,
    )
    page.wait_for_timeout(2500)


def _visible_subjects(page) -> list[str]:
    try:
        page.wait_for_selector("tr.zA", timeout=15000)
    except Exception:
        logger.info("No new Bark name rows visible for %s", NAME_MAILBOX)
        return []
    subjects = page.evaluate(
        """async () => {
            const seen = new Set();
            const found = [];
            const take = () => {
                for (const node of document.querySelectorAll('tr.zA .bog')) {
                    const text = (node.innerText || '').replace(/\\s+/g, ' ').trim();
                    if (text && !seen.has(text)) {
                        seen.add(text);
                        found.push(text);
                    }
                }
            };
            const scroller = document.querySelector('div.AO');
            for (let step = 0; step < 6; step++) {
                take();
                if (!scroller) break;
                scroller.scrollTop += scroller.clientHeight || 600;
                await new Promise(resolve => setTimeout(resolve, 400));
            }
            take();
            return found;
        }"""
    )
    return [str(subject) for subject in subjects]


def _page_account(page) -> str:
    email_address = page.evaluate(
        """() => {
            const label = [...document.querySelectorAll('[aria-label], [data-email]')]
                .map(el => el.getAttribute('data-email') || el.getAttribute('aria-label') || '')
                .find(text => text.includes('@'));
            const match = (label || '').match(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\\.[A-Z]{2,}/i);
            return match ? match[0].toLowerCase() : '';
        }"""
    )
    return str(email_address or "").lower()


def _sign_in(page, account: dict) -> None:
    email_box = page.locator("#identifierId")
    if email_box.count() and email_box.first.is_visible():
        email_box.first.fill(account["email"])
        page.locator("#identifierNext").click()
        page.wait_for_timeout(2500)
    password = page.locator('input[type="password"]')
    if password.count() and password.first.is_visible():
        password.first.fill(account["password"])
        page.locator("#passwordNext").click()
        page.wait_for_timeout(4000)
    for _ in range(6):
        if "mail.google.com" in page.url and "signin" not in page.url:
            return
        body = " ".join(page.locator("body").inner_text(timeout=8000).split())
        confirm = page.get_by_text(re.compile(r"confirm your recovery email", re.I))
        if confirm.count() and confirm.first.is_visible():
            confirm.first.click()
            page.wait_for_timeout(2000)
            _enter_recovery(page, account["recovery"])
            continue
        other = page.get_by_text(re.compile(r"try another way", re.I))
        if other.count() and other.first.is_visible() and "robot" in body.lower():
            other.first.click()
            page.wait_for_timeout(2000)
            continue
        recovery_box = page.locator('input[type="email"]')
        if recovery_box.count() and recovery_box.first.is_visible() and account.get("recovery"):
            _enter_recovery(page, account["recovery"])
            continue
        shot = BASE_DIR / "debug_login" / "gmail_challenge.png"
        shot.parent.mkdir(exist_ok=True)
        page.screenshot(path=str(shot), full_page=True)
        raise RuntimeError(f"Gmail verification stopped for {account['email']}: {body[:300]}")


def _enter_recovery(page, recovery: str) -> None:
    box = page.locator('input[type="email"]')
    if box.count() == 0 or not box.first.is_visible():
        box = page.locator('input[type="text"]')
    box.first.fill(recovery)
    nxt = page.get_by_role("button", name=re.compile(r"^next$", re.I))
    if nxt.count() and nxt.first.is_visible():
        nxt.first.click()
    else:
        page.keyboard.press("Enter")
    page.wait_for_timeout(4000)


def _sent_at(value: str) -> datetime | None:
    if not value:
        return None
    try:
        sent = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if sent.tzinfo is None:
        sent = sent.replace(tzinfo=timezone.utc)
    return sent.astimezone(timezone.utc)
