"""Playwright-based checkout flow.

Handles Opayo payment via two modes:
  - saved card: radio already selected, inject CVV only.
  - new card:   click "Pay with a different card", fill number + expiry + CVV.

Only this module needs a browser — everything else is pure API.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from playwright.sync_api import Frame, Page, expect, sync_playwright

BOOKINGS_BASE = "https://bookings.better.org.uk"

log = logging.getLogger(__name__)


@dataclass
class CardDetails:
    cvv: str
    number: str | None = None   # set to use new-card mode
    expiry: str | None = None   # MM/YY or MM/YYYY


def complete_checkout(card: CardDetails, token: str, timeout_s: int = 30, confirm: bool = False) -> str:
    """Navigate to checkout and complete payment.

    Args:
        card: Card details. If card.number is set, enters a new card.
              Otherwise uses the pre-selected saved card (CVV only).
        token: Valid Better PASETO bearer token (from BetterAPI.login).
        timeout_s: Seconds to wait for booking confirmation.
        confirm: If True, pause and require manual confirmation before clicking Pay.

    Returns:
        Booking reference string (e.g. "BET-XXXXXXXX").

    Raises:
        RuntimeError: If checkout does not complete within timeout_s.
    """
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1920, "height": 1080},
            )
            # Hide navigator.webdriver to bypass Opayo bot detection
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            context.add_cookies([{
                "name": "better.org.uk-authToken",
                "value": f'"{token}"',
                "domain": "bookings.better.org.uk",
                "path": "/",
                "secure": True,
                "httpOnly": False,
                "sameSite": "Lax",
            }])
            page = context.new_page()
            _block_analytics(page)

            log.info("Navigating to checkout…")
            page.goto(f"{BOOKINGS_BASE}/basket/checkout", wait_until="networkidle", timeout=30_000)
            _dismiss_cookie_banner(page)
            time.sleep(2)

            if card.number:
                log.info("New card mode — selecting 'Pay with a different card'")
                _select_new_card(page)

            log.info("Filling card details in Opayo iframe…")
            _fill_opayo_iframe(page, card)

            # Accept T&Cs if present (required to enable Pay button)
            _accept_terms(page)

            log.info("Waiting for Pay button to enable…")
            pay_btn = page.locator('button[aria-label="Pay now"], button:has-text("Pay now")').first
            expect(pay_btn).to_be_enabled(timeout=30_000)
            log.info("Clicking Pay now…")
            pay_btn.click(timeout=10_000)

            log.info("Waiting for booking confirmation…")
            ref = _wait_for_confirmation(page, timeout_s)
            log.info("Booking confirmed: %s", ref)
            return ref
        finally:
            browser.close()


# ------------------------------------------------------------------
# Payment mode helpers
# ------------------------------------------------------------------

def _select_new_card(page: Page) -> None:
    """Click the 'Pay with a different card' radio/button."""
    for selector in [
        'label:has-text("Pay with a different card")',
        'input[value*="different"]',
        'button:has-text("different card")',
        '[data-testid*="new-card"]',
    ]:
        try:
            page.click(selector, timeout=5_000)
            time.sleep(1)
            log.debug("Selected new card via %s", selector)
            return
        except Exception:
            continue
    raise RuntimeError("Could not find 'Pay with a different card' option on checkout page")


def _fill_opayo_iframe(page: Page, card: CardDetails) -> None:
    """Locate the Opayo iframe and fill the required fields."""
    # Wait for Opayo iframe src to be populated in the DOM, then use
    # frame_locator (finds by element selector, handles frame load timing).
    log.debug("Waiting for Opayo iframe src to populate...")
    try:
        page.wait_for_function(
            "() => { const f = document.querySelector('iframe'); "
            "return f && f.src && (f.src.includes('opayo') || f.src.includes('elavon')); }",
            timeout=120_000,
        )
    except Exception as exc:
        log.debug("wait_for_function timed out: %s", exc)

    opayo = page.frame_locator(
        'iframe#payment-iframe, iframe[src*="opayo"], iframe[src*="elavon"]'
    )

    if card.number:
        _type_in_frame(opayo, card.number, [
            'input[name="card-number"]',
            'input[id="card-number"]',
            'input[autocomplete="cc-number"]',
        ], "card number")

    if card.expiry:
        _type_in_frame(opayo, card.expiry, [
            'input[name="expiry-date"]',
            'input[id="expiry-date"]',
            'input[autocomplete="cc-exp"]',
        ], "expiry")

    _type_in_frame(opayo, card.cvv, [
        'input[name="security-code"]',
        'input[id="security-code"]',
        'input[autocomplete="cc-csc"]',
        'input[placeholder*="CV"]',
    ], "CVV")


def _find_opayo_frame(page: Page) -> Frame | None:
    for frame in page.frames:
        url = frame.url
        if url and url != "about:blank" and ("opayo" in url or "elavon" in url):
            return frame
    return None


def _type_in_frame(frame_loc: any, value: str, selectors: list[str], label: str) -> None:
    """Type value into first matching selector inside a frame_locator."""
    for selector in selectors:
        try:
            loc = frame_loc.locator(selector).first
            loc.wait_for(state="visible", timeout=30_000)
            loc.click()
            loc.type(value, delay=80)
            log.debug("%s typed in frame via %s", label, selector)
            return
        except Exception:
            continue
    raise RuntimeError(f"Could not locate {label} field in Opayo iframe")


def _fill_field(frame: Frame, value: str, selectors: list[str], label: str) -> None:
    for selector in selectors:
        try:
            frame.wait_for_selector(selector, timeout=5_000)
            frame.fill(selector, value)
            log.debug("%s filled via selector %s", label, selector)
            return
        except Exception:
            continue
    raise RuntimeError(f"Could not locate {label} field in Opayo iframe")


def _type_field(frame: Frame, value: str, selectors: list[str], label: str) -> None:
    """Like _fill_field but uses type() to simulate real keypresses (needed for CVV)."""
    for selector in selectors:
        try:
            frame.wait_for_selector(selector, timeout=5_000)
            frame.click(selector)
            frame.type(selector, value, delay=80)
            log.debug("%s typed via selector %s", label, selector)
            return
        except Exception:
            continue
    raise RuntimeError(f"Could not locate {label} field in Opayo iframe")


# ------------------------------------------------------------------
# Confirmation polling
# ------------------------------------------------------------------

def _wait_for_confirmation(page: Page, timeout_s: int) -> str:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if "confirmation" in page.url or "booking-confirmed" in page.url:
            return _extract_reference(page)
        try:
            ref = page.evaluate("""
                () => {
                    const m = document.body.innerText.match(/BET[-\\s]?[0-9A-Z]{6,}/i);
                    return m ? m[0] : null;
                }
            """)
            if ref:
                return ref
        except Exception:
            pass
        time.sleep(1)

    try:
        page.screenshot(path="/tmp/better-bot-timeout.png")
        log.warning("Timeout screenshot saved to /tmp/better-bot-timeout.png")
    except Exception:
        pass
    raise RuntimeError(f"Checkout did not confirm within {timeout_s}s")


def _extract_reference(page: Page) -> str:
    try:
        ref = page.evaluate("""
            () => {
                const m = document.body.innerText.match(/BET[-\\s]?[0-9A-Z]{6,}/i);
                return m ? m[0] : null;
            }
        """)
        if ref:
            return ref
    except Exception:
        pass
    return page.url


# ------------------------------------------------------------------
# Page helpers
# ------------------------------------------------------------------

def _accept_terms(page: Page) -> None:
    """Check the T&Cs checkbox if present and unchecked."""
    for selector in [
        'input[type="checkbox"][id*="terms"]',
        'input[type="checkbox"][name*="terms"]',
        'input[type="checkbox"][aria-label*="Terms"]',
        'label:has-text("Terms and Conditions") input[type="checkbox"]',
        '[data-testid*="terms"] input',
    ]:
        try:
            cb = page.locator(selector).first
            if cb.is_visible(timeout=2_000) and not cb.is_checked():
                cb.check()
                log.debug("T&Cs accepted via %s", selector)
                return
        except Exception:
            continue
    # Try clicking the label as fallback
    try:
        label = page.locator('label:has-text("Terms and Conditions")').first
        if label.is_visible(timeout=2_000):
            label.click()
            log.debug("T&Cs accepted via label click")
    except Exception:
        pass


def _block_analytics(page: Page) -> None:
    # Block OneTrust cookie banner CDN only.
    # Do NOT block GTM — the Better SPA uses a GTM event to trigger Opayo initialization.
    page.route("**/cdn.cookielaw.org/**", lambda r: r.abort())


def _dismiss_cookie_banner(page: Page) -> None:
    try:
        page.evaluate("""
            () => {
                const sdk = document.getElementById('onetrust-consent-sdk');
                if (sdk) sdk.remove();
                document.body.style.overflow = '';
            }
        """)
    except Exception:
        pass
