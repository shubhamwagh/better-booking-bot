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

from playwright.sync_api import Frame, Page, sync_playwright

BOOKINGS_BASE = "https://bookings.better.org.uk"

log = logging.getLogger(__name__)


@dataclass
class CardDetails:
    cvv: str
    number: str | None = None   # set to use new-card mode
    expiry: str | None = None   # MM/YY or MM/YYYY


def complete_checkout(card: CardDetails, token: str, timeout_s: int = 30) -> str:
    """Navigate to checkout and complete payment.

    Args:
        card: Card details. If card.number is set, enters a new card.
              Otherwise uses the pre-selected saved card (CVV only).
        token: Valid Better PASETO bearer token (from BetterAPI.login).
        timeout_s: Seconds to wait for booking confirmation.

    Returns:
        Booking reference string (e.g. "BET-XXXXXXXX").

    Raises:
        RuntimeError: If checkout does not complete within timeout_s.
    """
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            context = browser.new_context()
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
            page.goto(f"{BOOKINGS_BASE}/basket/checkout", wait_until="domcontentloaded", timeout=20_000)
            _dismiss_cookie_banner(page)
            time.sleep(3)

            if card.number:
                log.info("New card mode — selecting 'Pay with a different card'")
                _select_new_card(page)

            log.info("Filling card details in Opayo iframe…")
            _fill_opayo_iframe(page, card)

            log.info("Clicking Pay now…")
            page.locator('button:has-text("Pay")').click(timeout=10_000)

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
    page.wait_for_selector("iframe", timeout=15_000)

    opayo_frame = _find_opayo_frame(page)
    if opayo_frame is None:
        raise RuntimeError("Opayo payment iframe not found on checkout page")

    if card.number:
        _fill_field(opayo_frame, card.number, [
            'input[name="card-number"]',
            'input[id="card-number"]',
            'input[autocomplete="cc-number"]',
            'input[data-id="card-number"]',
        ], "card number")

    if card.expiry:
        _fill_field(opayo_frame, card.expiry, [
            'input[name="expiry-date"]',
            'input[id="expiry-date"]',
            'input[autocomplete="cc-exp"]',
            'input[data-id="expiry-date"]',
        ], "expiry")

    _fill_field(opayo_frame, card.cvv, [
        'input[name="security-code"]',
        'input[id="security-code"]',
        'input[autocomplete="cc-csc"]',
        'input[placeholder*="CV"]',
        'input[data-id="security-code"]',
    ], "CVV")


def _find_opayo_frame(page: Page) -> Frame | None:
    for frame in page.frames:
        if "opayo" in frame.url or "elavon" in frame.url:
            return frame
    return None


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

def _block_analytics(page: Page) -> None:
    page.route("**/cdn.cookielaw.org/**", lambda r: r.abort())
    page.route("**/googletagmanager.com/**", lambda r: r.abort())
    page.route("**/analytics.google.com/**", lambda r: r.abort())


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
