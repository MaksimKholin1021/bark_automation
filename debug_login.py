"""Debug Bark login page structure."""

from pathlib import Path

from dotenv import load_dotenv
import os
from playwright.sync_api import sync_playwright

load_dotenv(Path(__file__).parent / ".env")
out = Path(__file__).parent / "debug_login"
out.mkdir(exist_ok=True)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1400, "height": 900},
    )
    page = context.new_page()
    page.goto("https://www.bark.com/en/gb/login/", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(4000)
    page.screenshot(path=str(out / "01_login.png"), full_page=True)
    (out / "01_url.txt").write_text(page.url, encoding="utf-8")
    (out / "01_html.html").write_text(page.content(), encoding="utf-8")

    # Click password toggle if present
    for text in ["Use password instead", "password instead"]:
        loc = page.get_by_text(text, exact=False)
        if loc.count():
            try:
                loc.first.click(timeout=2000)
                page.wait_for_timeout(1000)
            except Exception as e:
                (out / "toggle_error.txt").write_text(str(e), encoding="utf-8")

    page.screenshot(path=str(out / "02_after_toggle.png"), full_page=True)
    inputs = page.eval_on_selector_all(
        "input",
        """els => els.map(e => ({
            type: e.type, name: e.name, id: e.id,
            placeholder: e.placeholder, visible: !!(e.offsetParent),
            aria: e.getAttribute('aria-label')
        }))""",
    )
    (out / "02_inputs.txt").write_text(str(inputs), encoding="utf-8")
    buttons = page.eval_on_selector_all(
        "button",
        "els => els.map(e => (e.innerText||'').trim()).filter(Boolean).slice(0,30)",
    )
    (out / "02_buttons.txt").write_text(str(buttons), encoding="utf-8")
    print("URL:", page.url)
    print("Inputs:", inputs)
    print("Buttons:", buttons)
    browser.close()
