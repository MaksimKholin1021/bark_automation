"""Login to Bark seller dashboard and extract visible leads."""

from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from playwright.sync_api import Browser, Page, Playwright, sync_playwright

logger = logging.getLogger(__name__)

LOGIN_URL = "https://www.bark.com/en/gb/login/"
LEADS_URL = "https://www.bark.com/sellers/dashboard/"
BASE_DIR = Path(__file__).resolve().parent
STORAGE_STATE = BASE_DIR / "storage_state.json"
BROWSERS_DIR = BASE_DIR / ".playwright"
BROWSERS_DIR.mkdir(exist_ok=True)
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(BROWSERS_DIR)


@dataclass(frozen=True)
class Lead:
    name: str
    content: str
    location: str = ""
    category: str = ""
    identity: str = ""

    @property
    def fingerprint(self) -> str:
        if self.identity:
            return self.identity
        # Stable identity from the list card. The detail panel replaces content later.
        raw = (
            f"{self.name.strip().lower()}|"
            f"{self.location.strip().lower()}|"
            f"{self.category.strip().lower()}|"
            f"{self.content.strip().lower()[:80]}"
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def with_detail(self, detail: str) -> "Lead":
        return Lead(
            name=self.name,
            content=detail,
            location=self.location,
            category=self.category,
            identity=self.fingerprint,
        )


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _parse_card_text(text: str) -> Lead | None:
    lines = [_clean(line) for line in text.splitlines() if _clean(line)]
    if len(lines) < 3:
        return None

    # Avatar initial is often the first single-character line.
    start = 1 if len(lines[0]) == 1 else 0
    if start >= len(lines):
        return None

    name = lines[start]
    location = ""
    idx = start + 1
    if idx < len(lines) and not re.search(
        r"^(now|\d+\s*(s|m|h|d|min|mins|hour|hours|day|days)\s*ago)$",
        lines[idx],
        re.I,
    ):
        location = lines[idx]
        idx += 1
        if idx < len(lines) and lines[idx].lower() in {"nationwide/online", "nationwide"}:
            location = f"{location} | {lines[idx]}"
            idx += 1

    skip = {
        "urgent",
        "high hiring intent",
        "verified phone",
        "additional details",
        "frequent user",
        "1st to respond",
        "now",
    }
    content_parts: list[str] = []
    category = ""
    for line in lines[idx:]:
        lower = line.lower()
        if lower in skip:
            continue
        if re.match(r"^\d+\s*credits?$", lower):
            continue
        if re.search(
            r"^(now|\d+\s*(s|m|h|d|min|mins|hour|hours|day|days)\s*ago)$",
            lower,
        ):
            continue
        if not category and re.search(r"architectural|structural", line, re.I):
            category = line
        content_parts.append(line)

    content = " | ".join(content_parts).strip()
    if not name or not content:
        return None
    return Lead(name=name, content=content, location=location, category=category)


def _format_detail(raw: str) -> str:
    """Keep the right-hand lead panel and drop Bark's sales buttons."""
    lines = [re.sub(r"\s+", " ", line).strip() for line in raw.splitlines()]
    lines = [line for line in lines if line]
    kept: list[str] = []
    for line in lines:
        lower = line.lower()
        if lower.startswith("contact ") or lower == "not interested":
            continue
        if "get hired guarantee" in lower or "starter pack" in lower:
            continue
        if lower.startswith("if you're not hired") or lower.startswith("if you’re not hired"):
            continue
        if lower.startswith("not seeing the right leads") or lower.startswith("stop seeing leads"):
            break
        if lower.startswith("update lead settings"):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def _remember_barks(response, catalog: list[dict]) -> None:
    if "api.bark.com/seller/barks" not in response.url or response.status != 200:
        return
    try:
        payload = response.json()
    except Exception:
        return
    for item in (payload.get("data") or {}).get("items") or []:
        if isinstance(item, dict):
            catalog.append(item)


def _entry_name(entry) -> str:
    if isinstance(entry, str):
        return entry.strip()
    if not isinstance(entry, dict):
        return ""
    for key in ("original_filename", "filename", "file_name", "name", "title", "caption"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for key in ("url", "file_url", "src", "path", "download_url"):
        value = entry.get(key)
        if isinstance(value, str) and "/" in value:
            return value.split("?")[0].rstrip("/").split("/")[-1]
    return ""


def _with_file_names(detail: str, filenames: list[str]) -> str:
    names: list[str] = []
    for name in filenames:
        clean = (name or "").strip()
        if clean and clean not in names:
            names.append(clean)
    if not names:
        return detail
    return detail.rstrip() + "\n\nFiles\n" + "\n".join(names)


def _file_names_for(lead: Lead, catalog: list[dict] | None) -> list[str]:
    names: list[str] = []
    for item in catalog or []:
        if str(item.get("buyer_share_name") or "").strip().lower() != lead.name.strip().lower():
            continue
        city = str(item.get("city_string") or "").strip().lower()
        if city and city not in lead.location.lower():
            continue
        for key in ("files", "images"):
            for entry in item.get(key) or []:
                label = _entry_name(entry)
                if label and label not in names:
                    names.append(label)
        break
    return names


def _detail_file_names(page: Page) -> list[str]:
    found = page.evaluate(
        """() => {
            const list = document.querySelector('[data-cy="leads-list"]');
            const hits = [...document.querySelectorAll('div')].filter((el) => {
                if (!list || list === el || list.contains(el) || el.contains(list)) return false;
                const text = el.innerText || '';
                return text.includes('Details') && /Contact\\s+\\S/.test(text)
                    && text.length > 180 && text.length < 7000;
            });
            hits.sort((a, b) => a.innerText.length - b.innerText.length);
            const root = hits[0];
            if (!root) return [];
            const names = [];
            root.querySelectorAll('a, img').forEach((node) => {
                const href = node.getAttribute('href') || node.getAttribute('src') || '';
                const text = (node.innerText || node.getAttribute('alt') || node.getAttribute('download') || '').trim();
                const fromUrl = href.split('?')[0].split('/').pop() || '';
                const candidate = text || fromUrl;
                if (/\\.(pdf|png|jpe?g|gif|webp|docx?|xlsx?|csv|zip|heic|dwg|txt)$/i.test(candidate)) {
                    names.push(candidate);
                }
            });
            return names;
        }"""
    )
    return [str(name).strip() for name in (found or []) if str(name).strip()]


def _read_detail_panel(page: Page) -> str:
    raw = page.evaluate(
        """() => {
            const list = document.querySelector('[data-cy="leads-list"]');
            const hits = [...document.querySelectorAll('div')].filter((el) => {
                if (!list || list === el || list.contains(el) || el.contains(list)) return false;
                const text = el.innerText || '';
                return text.includes('Details')
                    && /Contact\\s+\\S/.test(text)
                    && text.length > 180
                    && text.length < 7000;
            });
            hits.sort((a, b) => a.innerText.length - b.innerText.length);
            return hits.length ? hits[0].innerText : '';
        }"""
    )
    return _format_detail(raw or "")


def _collect_visible_cards(
    page: Page,
    leads: list[Lead],
    seen: set[str],
    wanted: set[str] | None = None,
    catalog: list[dict] | None = None,
) -> int:
    cards = page.locator('[data-cy^="lead-card-is-active"]')
    count = cards.count()
    pending: list[tuple[int, Lead]] = []
    fresh = 0
    for i in range(count):
        text = cards.nth(i).inner_text(timeout=5000)
        lead = _parse_card_text(text)
        if not lead or lead.fingerprint in seen:
            continue
        seen.add(lead.fingerprint)
        fresh += 1
        if wanted is not None and not _name_wanted(lead.name, wanted):
            continue
        pending.append((i, lead))

    for index, lead in pending:
        detail = ""
        try:
            card = page.locator('[data-cy^="lead-card-is-active"]').nth(index)
            card.click(timeout=4000)
            page.wait_for_timeout(500)
            detail = _read_detail_panel(page)
        except Exception:
            logger.warning("Could not open detail panel for %s", lead.name)
        if detail:
            filenames = _file_names_for(lead, catalog) + _detail_file_names(page)
            lead = lead.with_detail(_with_file_names(detail, filenames))
        leads.append(lead)
    return fresh


def _name_wanted(name: str, wanted: set[str]) -> bool:
    parts = [part for part in re.findall(r"[a-z0-9']+", name.lower()) if part]
    if not parts:
        return False
    return parts[0] in wanted or name.strip().lower() in wanted


def extract_leads_from_page(
    page: Page,
    *,
    enrich_details: bool = False,
    deep: bool = False,
    wanted: set[str] | None = None,
    catalog: list[dict] | None = None,
) -> list[Lead]:
    page.reload(wait_until="commit", timeout=90000)
    page.wait_for_selector('[data-cy="leads-list"]', timeout=60000)
    page.wait_for_timeout(1200)
    _dismiss_noise(page)

    leads: list[Lead] = []
    seen: set[str] = set()
    stagnant = 0
    # The newest screen spots new leads. A deep pass only opens cards whose
    # names still need a description note.
    rounds = 25 if deep else 1
    for round_index in range(rounds):
        fresh = _collect_visible_cards(page, leads, seen, wanted if deep else None, catalog)
        logger.info(
            "Lead list pass %s: %s new cards, %s panels opened",
            round_index + 1,
            fresh,
            len(leads),
        )
        if not deep:
            break
        opened = {
            part
            for lead in leads
            if lead.name.strip()
            for part in re.findall(r"[a-z0-9']+", lead.name.lower())[:1]
        }
        if wanted and wanted <= opened:
            break
        count_before = page.locator('[data-cy^="lead-card-is-active"]').count()
        page.evaluate(
            """() => {
                const box = document.querySelector('.leads-list-container');
                if (box) box.scrollTop = box.scrollHeight;
            }"""
        )
        grew = False
        for _ in range(8):
            page.wait_for_timeout(250)
            if page.locator('[data-cy^="lead-card-is-active"]').count() > count_before:
                grew = True
                break
        if not grew:
            stagnant += 1
            if stagnant >= 2:
                break
        else:
            stagnant = 0

    detailed = sum(1 for lead in leads if "\n" in lead.content)
    logger.info("Extracted %s unique leads from dashboard (%s with detail panels)", len(leads), detailed)
    return leads


def _open(page: Page, url: str) -> None:
    page.goto(url, wait_until="commit", timeout=90000)


def _dismiss_noise(page: Page) -> None:
    for label in (
        "I don't want web notifications",
        "Close",
        "Accept all",
        "Accept",
    ):
        btn = page.get_by_role("button", name=re.compile(rf"^{re.escape(label)}$", re.I))
        try:
            if btn.count() > 0 and btn.first.is_visible():
                btn.first.click(timeout=1000)
                page.wait_for_timeout(300)
        except Exception:
            pass
    link = page.get_by_role("link", name=re.compile("I don't want web notifications", re.I))
    try:
        if link.count() > 0 and link.first.is_visible():
            link.first.click(timeout=1000)
    except Exception:
        pass


def _show_password_form(page: Page) -> None:
    toggle = page.get_by_role("button", name=re.compile("Use password instead", re.I))
    try:
        if toggle.count() > 0:
            toggle.first.click(force=True, timeout=3000)
            page.wait_for_timeout(800)
    except Exception:
        pass
    page.evaluate(
        """() => {
            const pw = document.querySelector('#pw-loginId');
            if (!pw) return;
            let node = pw;
            while (node && node !== document.body) {
                node.style.display = '';
                node.hidden = false;
                node.removeAttribute('hidden');
                node = node.parentElement;
            }
        }"""
    )


def _logged_in(page: Page) -> bool:
    url = page.url.lower()
    if "login" in url:
        return False
    return "/sellers/" in url


def _open_leads(page: Page) -> None:
    _open(page, LEADS_URL)
    page.wait_for_timeout(1500)
    _dismiss_noise(page)
    try:
        page.wait_for_selector('[data-cy="leads-list"]', timeout=20000)
        return
    except Exception:
        pass
    view = page.get_by_role("link", name=re.compile(r"live leads", re.I))
    if view.count() > 0:
        view.first.click()
        page.wait_for_timeout(1500)
    else:
        _open(page, LEADS_URL)
    page.wait_for_selector('[data-cy="leads-list"]', timeout=60000)


def _login_with_password(page: Page, email: str, password: str) -> None:
    _open(page, LOGIN_URL)
    page.wait_for_timeout(2000)
    _dismiss_noise(page)
    if _logged_in(page):
        return
    _show_password_form(page)

    # Bark keeps both magic-link and password forms in the DOM.
    # Password form fields are specifically #pw-loginId / #pw-password.
    try:
        page.wait_for_selector("#pw-loginId", state="visible", timeout=15000)
    except Exception:
        shot = BASE_DIR / "debug_login" / "login_timeout.png"
        shot.parent.mkdir(exist_ok=True)
        page.screenshot(path=str(shot), full_page=True)
        (BASE_DIR / "debug_login" / "login_timeout.txt").write_text(
            f"url={page.url}\ntitle={page.title()}\n",
            encoding="utf-8",
        )
        raise
    page.locator("#pw-loginId").fill(email)
    page.locator("#pw-password").fill(password)

    login_btn = page.locator("button", has_text=re.compile("^Login$", re.I))
    # Prefer the password-form Login button (visible one).
    clicked = False
    for i in range(login_btn.count()):
        btn = login_btn.nth(i)
        if btn.is_visible():
            btn.click()
            clicked = True
            break
    if not clicked:
        page.locator('form:visible button[type="submit"]').first.click()

    page.wait_for_timeout(3000)
    page.wait_for_function(
        """() => !location.href.toLowerCase().includes('login.bark.com')
               && !location.pathname.toLowerCase().includes('/login')""",
        timeout=90000,
    )


def _ensure_logged_in(page: Page, email: str, password: str) -> None:
    _open(page, LEADS_URL)
    page.wait_for_timeout(1500)
    _dismiss_noise(page)

    if not _logged_in(page):
        _login_with_password(page, email, password)
    _open_leads(page)


def fetch_leads(
    email: str,
    password: str,
    *,
    headless: bool = True,
    deep: bool = False,
    wanted: set[str] | None = None,
) -> list[Lead]:
    with sync_playwright() as p:
        browser = _launch(p, headless=headless)
        context = None
        try:
            context = _new_context(
                browser,
                storage_state=str(STORAGE_STATE) if STORAGE_STATE.exists() else None,
            )
            page = context.new_page()
            catalog: list[dict] = []
            page.on("response", lambda response: _remember_barks(response, catalog))
            _ensure_logged_in(page, email, password)
            context.storage_state(path=str(STORAGE_STATE))
            # List-card parse is enough for detection; enrichment can be slow.
            return extract_leads_from_page(page, deep=deep, wanted=wanted, catalog=catalog)
        finally:
            if context is not None:
                context.close()
            browser.close()


def _launch(p: Playwright, *, headless: bool) -> Browser:
    return p.chromium.launch(
        headless=headless,
        args=["--disable-blink-features=AutomationControlled"],
    )


def _new_context(browser: Browser, storage_state: str | None = None):
    kwargs = {
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
        ),
        "viewport": {"width": 1400, "height": 900},
    }
    if storage_state:
        kwargs["storage_state"] = storage_state
    return browser.new_context(**kwargs)


def fingerprints(leads: Iterable[Lead]) -> set[str]:
    return {lead.fingerprint for lead in leads}


def reveal_contact(
    email: str,
    password: str,
    lead: Lead,
    *,
    session_path: Path,
    headless: bool = True,
) -> None:
    """Log into one seller account and click Contact for this lead.

    Bark emails the full name later. A successful click is enough.
    """
    session_path.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = _launch(p, headless=headless)
        context = None
        try:
            context = _new_context(
                browser,
                storage_state=str(session_path) if session_path.exists() else None,
            )
            page = context.new_page()
            _ensure_logged_in(page, email, password)
            context.storage_state(path=str(session_path))
            _open_matching_card(page, lead)
            _click_contact(page, lead.name)
            context.storage_state(path=str(session_path))
            shot = BASE_DIR / "debug_login" / "contact_last.png"
            shot.parent.mkdir(exist_ok=True)
            page.screenshot(path=str(shot), full_page=True)
        finally:
            if context is not None:
                context.close()
            browser.close()


def _open_matching_card(page: Page, lead: Lead) -> None:
    name = lead.name.strip().lower()
    place = (lead.location or "").split(",")[0].strip().lower()
    for _ in range(8):
        cards = page.locator('[data-cy^="lead-card-is-active"]')
        for index in range(cards.count()):
            text = cards.nth(index).inner_text(timeout=3000).lower()
            if name not in text:
                continue
            if place and place not in text:
                continue
            cards.nth(index).click(timeout=4000)
            page.wait_for_timeout(800)
            return
        page.evaluate(
            """() => {
                const box = document.querySelector('.leads-list-container');
                if (box) box.scrollTop += box.clientHeight;
            }"""
        )
        page.wait_for_timeout(600)
    raise LookupError(f"Could not find {lead.name} on this Bark account")


class CreditsRequired(RuntimeError):
    """Bark asked for a credit purchase. The buy button is left unclicked."""


def _click_contact(page: Page, name: str) -> None:
    pattern = re.compile(rf"^Contact\s+{re.escape(name.strip())}\b", re.I)
    button = page.get_by_role("button", name=pattern)
    if button.count() == 0:
        button = page.locator("button, a").filter(has_text=pattern)
    if button.count() == 0 or not button.first.is_visible():
        shot = BASE_DIR / "debug_login" / "contact_fail.png"
        shot.parent.mkdir(exist_ok=True)
        page.screenshot(path=str(shot), full_page=True)
        raise RuntimeError(f"Contact button not found for {name}")
    button.first.click(timeout=8000)
    page.wait_for_timeout(1500)
    if _credits_wall(page):
        shot = BASE_DIR / "debug_login" / "contact_credits.png"
        shot.parent.mkdir(exist_ok=True)
        page.screenshot(path=str(shot), full_page=True)
        raise CreditsRequired(f"Bark asked to buy credits before contacting {name}")
    dialog = page.locator('[role="dialog"]')
    if dialog.count() > 0 and dialog.first.is_visible():
        confirm = dialog.first.get_by_role(
            "button",
            name=re.compile(r"^contact\b|^confirm\b|^continue\b|^yes\b|^send\b", re.I),
        )
        if confirm.count() > 0 and confirm.first.is_visible():
            confirm.first.click(timeout=5000)
            page.wait_for_timeout(1500)
        if _credits_wall(page):
            raise CreditsRequired(f"Bark asked to buy credits before contacting {name}")
    logger.info("Clicked Contact for %s", name)


def _credits_wall(page: Page) -> bool:
    buy = page.get_by_role("button", name=re.compile(r"buy\s+\d+\s+credits", re.I))
    try:
        if buy.count() > 0 and buy.first.is_visible():
            return True
    except Exception:
        return False
    try:
        text = page.locator("body").inner_text(timeout=3000)
    except Exception:
        return False
    return re.search(r"you need\s+\d+\s+credits\s+to contact", text, re.I) is not None


def _contact_button_visible(page: Page, name: str) -> bool:
    pattern = re.compile(rf"^Contact\s+{re.escape(name.strip())}\b", re.I)
    button = page.get_by_role("button", name=pattern)
    try:
        return button.count() > 0 and button.first.is_visible()
    except Exception:
        return False


def _wait_for_revealed_name(page: Page, short_name: str) -> dict[str, str]:
    revealed = {"name": "", "phone": "", "email": ""}
    for _ in range(16):
        revealed = _contact_fields(_read_detail_panel(page))
        full = revealed["name"]
        if full and full.lower() != short_name.strip().lower() and not full.lower().startswith("contact "):
            return revealed
        page.wait_for_timeout(500)
    if revealed.get("name") and not _contact_button_visible(page, short_name):
        return revealed
    return {"name": "", "phone": "", "email": ""}


def _contact_fields(panel: str) -> dict[str, str]:
    lines = [re.sub(r"\s+", " ", line).strip() for line in (panel or "").splitlines()]
    lines = [line for line in lines if line]
    name = ""
    for line in lines:
        lower = line.lower()
        if lower.startswith("contact ") or lower in {"highlights", "details"}:
            continue
        name = line
        break
    phone = ""
    email = ""
    for line in lines:
        compact = re.sub(r"\s+", "", line)
        if not email and "@" in line and "*" not in line:
            email = line.strip()
        if (
            not phone
            and "*" not in line
            and (
                re.search(r"\(\d{3}\)", line)
                or re.search(r"\d{3}[-.\s]*\d{3}[-.\s]*\d{4}", line)
            )
        ):
            phone = line.strip()
    return {"name": name, "phone": phone, "email": email}
