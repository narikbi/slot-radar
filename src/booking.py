from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
    TimeoutError as PlaywrightTimeout,
)

from . import captcha
from .state import Slot

logger = logging.getLogger(__name__)

BASE_URL = "https://konzinfoidopont.mfa.gov.hu"
CONSULATE_LABEL = "Kazakhstan - Almati"
CASE_TYPE_LABEL = "Visa application (Schengen visa- type 'C')"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
DEFAULT_NAV_TIMEOUT_MS = 45_000
DEBUG_DIR = Path(__file__).resolve().parent.parent / "debug-screenshots"


class BookingError(Exception):
    pass


async def find_earliest_slot() -> Slot:
    async with _stealth_browser() as page:
        try:
            await _open_booking_form(page)
            await _select_consulate(page)
            await _select_case_type(page)
            await _fill_personal_data(page)
            await _tick_consents(page)

            captcha_since = time.time()
            await _click_select_date(page)
            await _wait_for_captcha_field(page)

            max_captcha_attempts = 2  # original + 1 retry; 3rd wrong = lockout
            for attempt in range(1, max_captcha_attempts + 1):
                answer = await asyncio.to_thread(captcha.solve_from_email, captcha_since)
                await _fill_captcha(page, answer)
                await _click_select_date(page)

                if not await _is_incorrect_code_visible(page):
                    break  # captcha accepted

                if attempt == max_captcha_attempts:
                    raise BookingError(
                        f"Captcha rejected on {max_captcha_attempts} attempts in a row. "
                        "Aborting to preserve attempts (3 wrong = lockout)."
                    )

                logger.warning(
                    "Captcha attempt %d was rejected, requesting fresh captcha",
                    attempt,
                )
                await _dismiss_modals(page)
                captcha_since = time.time()
                await _click_request_new_code(page)
                await asyncio.sleep(1)
                await _wait_for_captcha_field(page)

            await _dismiss_modals(page)

            slot = await _read_first_slot(page)
            logger.info("Found earliest slot: %s", slot)
            return slot
        except Exception:
            await _dump_debug(page, "error")
            raise


_console_log: list[str] = []
_failed_requests: list[str] = []


@asynccontextmanager
async def _stealth_browser():
    pw: Playwright
    browser: Browser
    context: BrowserContext

    _console_log.clear()
    _failed_requests.clear()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
        )
        context = await browser.new_context(
            user_agent=USER_AGENT,
            locale="en-US",
            timezone_id="Asia/Almaty",
            viewport={"width": 1366, "height": 900},
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        context.set_default_navigation_timeout(DEFAULT_NAV_TIMEOUT_MS)
        context.set_default_timeout(DEFAULT_NAV_TIMEOUT_MS)
        page = await context.new_page()
        page.on("console", lambda m: _console_log.append(f"[{m.type}] {m.text}"))
        page.on("pageerror", lambda e: _console_log.append(f"[pageerror] {e}"))
        page.on(
            "requestfailed",
            lambda r: _failed_requests.append(f"{r.method} {r.url} -> {r.failure}"),
        )
        try:
            yield page
        finally:
            await context.close()
            await browser.close()


async def _open_booking_form(page: Page) -> None:
    # Blazor WebAssembly app: <body> starts as `<app>Loading...</app>` and the real
    # DOM is rendered client-side after a bunch of .wasm/.dll files download.
    await page.goto(BASE_URL)
    await page.wait_for_load_state("networkidle", timeout=90_000)
    await page.wait_for_function(
        """() => {
            const el = document.querySelector('app');
            if (!el) return false;
            const txt = (el.innerText || '').trim().toLowerCase();
            return el.children.length > 0 && txt && !txt.startsWith('loading');
        }""",
        timeout=90_000,
    )

    await _switch_to_english(page)

    if await page.get_by_text(re.compile(r"please\s*select\s*a\s*consulate", re.I)).count():
        return
    if await page.get_by_text(re.compile(r"i\s*wish\s*to\s*book", re.I)).count():
        if not await page.get_by_text(re.compile(r"please\s*select\s*a\s*consulate", re.I)).count():
            try:
                await page.get_by_role("link", name=re.compile(r"book.*appointment", re.I)).first.click(timeout=3000)
            except PlaywrightTimeout:
                try:
                    await page.get_by_text(re.compile(r"i\s*wish\s*to\s*book", re.I)).first.click(timeout=3000)
                except PlaywrightTimeout:
                    pass
            await page.wait_for_load_state("networkidle", timeout=30_000)
            if await page.get_by_text(re.compile(r"please\s*select\s*a\s*consulate", re.I)).count():
                return

    raise BookingError(
        f"Booking form not found, page text starts with: "
        f"{(await page.inner_text('body'))[:300]!r}"
    )


async def _switch_to_english(page: Page) -> None:
    """Click the language dropdown and pick English if the page renders in Hungarian."""
    if await page.get_by_text(re.compile(r"please\s*select\s*a\s*consulate", re.I)).count():
        return

    try:
        await page.locator("#langSelector").click(timeout=5000)
    except PlaywrightTimeout:
        logger.warning("Language switcher #langSelector not found — page may already be English")
        return

    english_link = page.locator(".dropdown-menu.language a", has_text=re.compile(r"english", re.I))
    if not await english_link.count():
        english_link = page.get_by_role("link", name=re.compile(r"^\s*english\s*$", re.I))
    await english_link.first.click(timeout=5000)

    await page.get_by_text(re.compile(r"please\s*select\s*a\s*consulate", re.I)).first.wait_for(
        state="visible", timeout=15_000
    )


async def _select_consulate(page: Page) -> None:
    # modal2 has no Save button — clicking a radio auto-applies; we then close it.
    await page.locator('button[data-target="#modal2"]').first.click()
    modal = page.locator("#modal2")
    label = modal.locator(
        "label.form-check-label",
        has_text=re.compile(re.escape(CONSULATE_LABEL), re.I),
    )
    await label.first.wait_for(state="visible", timeout=10_000)
    await label.first.click()

    try:
        await page.get_by_text(re.compile(r"selected\s*consulate", re.I)).first.wait_for(
            state="visible", timeout=8_000
        )
    except PlaywrightTimeout:
        await modal.locator("button.close").first.click(timeout=3000)
        await page.get_by_text(re.compile(r"selected\s*consulate", re.I)).first.wait_for(
            state="visible", timeout=10_000
        )


async def _select_case_type(page: Page) -> None:
    # modalCases has a Save button.
    await page.locator('button[data-target="#modalCases"]').first.click()
    modal = page.locator("#modalCases")
    label = modal.locator(
        "label.form-check-label",
        has_text=re.compile(r"Schengen\s*visa.*type.*C", re.I),
    )
    await label.first.wait_for(state="visible", timeout=10_000)
    await label.first.click()

    save_btn = modal.locator("button.btn-success", has_text=re.compile(r"^\s*save\s*$", re.I))
    await save_btn.click()

    await page.get_by_text(re.compile(r"selected\s*case\s*types", re.I)).first.wait_for(
        state="visible", timeout=15_000
    )


async def _fill_personal_data(page: Page) -> None:
    name = os.environ["APPLICANT_NAME"]
    dob = os.environ["APPLICANT_DOB"]               # DD/MM/YYYY
    phone = os.environ["APPLICANT_PHONE"]
    email = os.environ["APPLICANT_EMAIL"]
    passport = os.environ["APPLICANT_PASSPORT"]

    await _fill_by_label(page, r"^\s*name\s*$", name)

    # DOB uses Duet date picker custom element with input id="birthDate".
    # Use type+Tab so the JS framework's input/blur bindings fire.
    dob_input = page.locator("#birthDate")
    await dob_input.click()
    await dob_input.fill("")
    await dob_input.type(dob, delay=30)
    await dob_input.press("Tab")

    try:
        await _fill_by_label(page, r"number\s*of\s*applicants", "1")
    except BookingError:
        pass  # field may be pre-filled or not present
    await _fill_by_label(page, r"phone\s*number", phone)
    await _fill_by_label(page, r"^\s*e-?mail\s*address\s*$", email)
    await _fill_by_label(page, r"re-?enter\s*the\s*email", email)
    await _fill_by_label(page, r"passport\s*number", passport)


async def _tick_consents(page: Page) -> None:
    for pattern in [
        r"i\s*have\s*read\s*and\s*acknowledged",
        r"i\s*give\s*my\s*consent",
    ]:
        cb = page.get_by_role("checkbox", name=re.compile(pattern, re.I))
        try:
            if not await cb.is_checked():
                await cb.check()
        except PlaywrightTimeout:
            label = page.get_by_text(re.compile(pattern, re.I)).first
            await label.click()


async def _click_select_date(page: Page) -> None:
    btn = page.get_by_role("button", name=re.compile(r"select\s*date", re.I))
    await btn.first.click()


async def _wait_for_captcha_field(page: Page, timeout_ms: int = 20_000) -> None:
    await _dismiss_modals(page)
    label = page.locator(
        "label.col-form-label",
        has_text=re.compile(r"security\s*code", re.I),
    )
    await label.first.wait_for(state="visible", timeout=timeout_ms)


async def _fill_captcha(page: Page, answer: str) -> None:
    await _dismiss_modals(page)
    captcha_input = _captcha_input_locator(page)
    if not await captcha_input.count():
        raise BookingError("Could not find captcha input field")
    await captcha_input.first.click()
    await captcha_input.first.fill(answer)


def _captcha_input_locator(page: Page):
    label = page.locator(
        "label.col-form-label",
        has_text=re.compile(r"security\s*code", re.I),
    )
    return label.locator("xpath=following::input[1]")


async def _is_incorrect_code_visible(page: Page) -> bool:
    """Return True if the site shows an 'Incorrect code' modal after captcha submit."""
    try:
        await page.locator(".modal.show").locator(
            "text=/incorrect|invalid|wrong|hib|érvénytelen|hibás/i"
        ).first.wait_for(state="visible", timeout=4000)
        return True
    except PlaywrightTimeout:
        return False


async def _click_request_new_code(page: Page) -> None:
    """Click the 'Request for a new code' button to trigger a fresh captcha email."""
    btn = page.get_by_role(
        "button", name=re.compile(r"request\s*for\s*a?\s*new\s*code", re.I)
    )
    if not await btn.count():
        btn = page.get_by_text(re.compile(r"request\s*for\s*a?\s*new\s*code", re.I))
    await btn.first.click()


async def _dismiss_modals(page: Page) -> None:
    """Close any visible Bootstrap notification modal (e.g. 'check your mailbox' popup)."""
    deadline = asyncio.get_event_loop().time() + 8
    while asyncio.get_event_loop().time() < deadline:
        modal = page.locator(".modal.show")
        if await modal.count() == 0:
            return
        try:
            ok_btn = modal.locator(
                "button.btn-primary, button.btn-success"
            ).first
            await ok_btn.click(timeout=2000)
            await modal.first.wait_for(state="hidden", timeout=4000)
        except PlaywrightTimeout:
            await asyncio.sleep(0.5)


async def _read_first_slot(page: Page) -> Slot:
    await page.wait_for_load_state("networkidle")
    cell_re = re.compile(
        r"(\d{2}-\d{2}-\d{4})\s*[\r\n ]+([A-Za-z]+)\s*[\r\n ]+(\d{2}:\d{2})"
    )

    deadline = asyncio.get_event_loop().time() + 15
    while asyncio.get_event_loop().time() < deadline:
        text = await page.inner_text("body")
        m = cell_re.search(text)
        if m:
            ddmmyyyy, weekday, hhmm = m.group(1), m.group(2), m.group(3)
            d, mo, y = ddmmyyyy.split("-")
            return Slot(date=f"{y}-{mo}-{d}", time=hhmm, weekday=weekday)
        if "no free" in text.lower() or "no free" in text.lower():
            raise BookingError("Site reports no free slots")
        await asyncio.sleep(1)

    raise BookingError("Could not parse any slot from the page")


async def _has_text(page: Page, text: str) -> bool:
    try:
        return await page.locator(f"text={text}").count() > 0
    except Exception:
        return False


async def _fill_by_label(page: Page, label_pattern: str, value: str) -> None:
    rx = re.compile(label_pattern, re.I)
    try:
        await page.get_by_label(rx).first.fill(value)
        return
    except PlaywrightTimeout:
        pass

    label_loc = page.get_by_text(rx).first
    try:
        await label_loc.wait_for(state="attached", timeout=3000)
    except PlaywrightTimeout:
        raise BookingError(f"Label not found: {label_pattern}")

    input_loc = label_loc.locator(
        "xpath=following::*[self::input or self::textarea][1]"
    )
    await input_loc.fill(value)


async def _dump_debug(page: Optional[Page], tag: str) -> None:
    if page is None:
        return
    try:
        DEBUG_DIR.mkdir(exist_ok=True)
        ts = int(time.time())
        await page.screenshot(path=str(DEBUG_DIR / f"{tag}-{ts}.png"), full_page=True)
        html = await page.content()
        (DEBUG_DIR / f"{tag}-{ts}.html").write_text(html, encoding="utf-8")
        try:
            text = await page.inner_text("body")
            (DEBUG_DIR / f"{tag}-{ts}.txt").write_text(text, encoding="utf-8")
        except Exception:
            pass
        try:
            app_html = await page.evaluate(
                "() => { const a = document.querySelector('app'); return a ? a.innerHTML : '<no app element>'; }"
            )
            (DEBUG_DIR / f"{tag}-{ts}.app.html").write_text(app_html, encoding="utf-8")
        except Exception:
            pass
        if _console_log:
            (DEBUG_DIR / f"{tag}-{ts}.console.log").write_text(
                "\n".join(_console_log), encoding="utf-8"
            )
        if _failed_requests:
            (DEBUG_DIR / f"{tag}-{ts}.failed-requests.log").write_text(
                "\n".join(_failed_requests), encoding="utf-8"
            )
        logger.info("Saved debug artifacts to %s (tag=%s)", DEBUG_DIR, tag)
    except Exception as e:
        logger.warning("Failed to save debug artifacts: %s", e)
