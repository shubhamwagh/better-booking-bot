"""Playwright-based checkout flow.

Handles Opayo payment via three modes:
  - credit only: full balance covered by account credit - no card entry needed.
  - saved card:  radio already selected, inject CVV only.
  - new card:    click "Pay with a different card", fill number + expiry + CVV.

Credit is applied against the total before card payment: the "pay full amount
using credit" button when credit covers the whole cost, else the manual
"enter amount to redeem" box for whatever partial balance is available - the
remainder is then charged to the card as normal.

Only this module needs a browser - everything else is pure API.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from playwright.sync_api import Frame, Page, expect, sync_playwright
from pydantic import BaseModel

BOOKINGS_BASE = "https://bookings.better.org.uk"

log = logging.getLogger(__name__)


class CardDetails(BaseModel):
    cvv: str
    number: str | None = None  # set to use new-card mode
    expiry: str | None = None  # MM/YY or MM/YYYY
    # Billing address - required for new card mode
    first_name: str | None = None
    last_name: str | None = None
    address1: str | None = None
    address2: str | None = None
    city: str | None = None
    postcode: str | None = None
    save_card: bool = False


def complete_checkout(
    card: CardDetails,
    token: str,
    timeout_s: int = 30,
    confirm: bool = False,
    headless: bool = True,
) -> str:
    """Navigate to checkout and complete payment.

    Auto-detects payment mode from the page:
      1. Applies any available account credit.
      2. If total drops to £0 → confirms without card entry.
      3. Else if saved card radio present → saved card mode (CVV only).
      4. Else → new card mode (billing details + full card).

    Args:
        card: Card credentials. For saved card mode only cvv is needed.
              For new card mode also number, expiry, and billing fields.
        token: Valid Better PASETO bearer token (from BetterAPI.login).
        timeout_s: Seconds to wait for booking confirmation.

    Returns:
        Booking reference URL or ID.

    Raises:
        RuntimeError: If checkout does not complete within timeout_s.
    """
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=headless,
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
            context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            context.add_cookies(
                [
                    {
                        "name": "better.org.uk-authToken",
                        "value": f'"{token}"',
                        "domain": "bookings.better.org.uk",
                        "path": "/",
                        "secure": True,
                        "httpOnly": False,
                        "sameSite": "Lax",
                    }
                ]
            )
            page = context.new_page()
            _block_analytics(page)

            log.info("Navigating to checkout…")
            page.goto(f"{BOOKINGS_BASE}/basket/checkout", wait_until="networkidle", timeout=30_000)
            _dismiss_cookie_banner(page)
            time.sleep(2)

            # Step 1: apply any available credit
            _apply_credit(page)

            # Step 2: detect payment mode from page
            if _is_zero_balance(page):
                log.info("Credit covers full balance - no card entry needed")
            elif _has_saved_card(page):
                log.info("Saved card detected - selecting saved card and filling CVV")
                if not card.cvv:
                    raise RuntimeError("CARD_CVV required for saved card checkout but not set")
                _select_saved_card(page)
                _fill_saved_card_cvv(page, card.cvv)
            else:
                log.info("No saved card - entering new card details")
                if not card.number or not card.expiry:
                    raise RuntimeError("No saved card found. Set CARD_NUMBER and CARD_EXPIRY in .env for new card mode")
                _select_new_card(page)
                _fill_billing_details(page, card)
                _fill_opayo_iframe(page, card)

            # Step 3: accept T&Cs and pay
            _accept_terms_inline(page)

            log.info("Clicking Pay / Continue…")
            pay_btn = page.locator(
                'button[aria-label="Pay now"], button:has-text("Pay now"), '
                'button:has-text("Pay £"), button:has-text("Continue")'
            ).first
            expect(pay_btn).to_be_enabled(timeout=15_000)
            pay_btn.click(timeout=10_000)

            # Accept T&Cs modal if it appears after clicking Pay (fallback)
            _accept_terms(page)

            log.info("Waiting for booking confirmation…")
            ref = _wait_for_confirmation(page, timeout_s)
            log.info("Booking confirmed: %s", ref)
            return ref
        finally:
            browser.close()


# ------------------------------------------------------------------
# Credit helpers
# ------------------------------------------------------------------


def _read_total_to_pay(page: Page) -> str | None:
    """Best-effort read of the 'Total to pay' summary line, for diagnostics."""
    try:
        return page.evaluate("""
            () => {
                const els = [...document.querySelectorAll('*')];
                for (const el of els) {
                    if (el.children.length === 0 && /Total to pay/i.test(el.textContent)) {
                        const next = el.nextElementSibling;
                        if (next) return next.textContent.trim();
                    }
                }
                return null;
            }
        """)
    except Exception:
        return None


def _is_zero_balance(page: Page) -> bool:
    """Return True if the total to pay is £0 after credit was applied."""
    try:
        text = page.locator('button:has-text("Pay £0"), button:has-text("Pay £0.00")').first
        if text.is_visible(timeout=2_000):
            log.info("Total to pay is £0 (Pay £0 button visible)")
            return True
    except Exception:
        pass
    # Fallback: parse summary total from page text
    total = _read_total_to_pay(page)
    if total:
        log.info("Total to pay after credit check: %s", total)
        if "0.00" in total:
            return True
    else:
        log.info("Could not read 'Total to pay' summary line")
    return False


def _has_saved_card(page: Page) -> bool:
    """Return True if a saved card radio button is visible on the checkout page."""
    try:
        radio = page.locator('input[id="saved-card"], input[value="saved_card"]').first
        return radio.is_visible(timeout=3_000)
    except Exception:
        return False


def _apply_credit(page: Page) -> None:
    """Apply available account credit toward the total.

    Tries 'Pay full amount using credit' first (shown when credit fully covers
    the total). If that button isn't there - typically because credit is less
    than the total - falls back to the manual 'Enter the amount to pay in
    credit' box, redeeming whatever credit is available as a partial payment
    and leaving the remainder to be charged to card as normal.
    """
    total_before = _read_total_to_pay(page)
    if total_before:
        log.info("Total to pay before credit check: %s", total_before)

    try:
        btn = page.locator('button:has-text("Pay full amount using credit")').first
        if btn.is_visible(timeout=5_000):
            btn.click()
            log.info("Clicked 'Pay full amount using credit'")
            # Wait for page to reflect updated total (network idle or URL change)
            page.wait_for_load_state("networkidle", timeout=10_000)
            time.sleep(1)
            return
        log.info("'Pay full amount using credit' button not visible - trying partial credit redemption")
    except Exception as exc:
        log.warning("'Pay full amount using credit' button click failed: %s", exc)

    _apply_partial_credit(page, total_hint=_parse_money(total_before))


def _read_credit_balance(page: Page) -> float | None:
    """Parse the account credit balance from 'You have £X.XX credit...' on the page."""
    try:
        text = page.evaluate("() => document.body.innerText")
        m = re.search(r"You have\s*£\s*([\d.]+)\s*credit", text, re.IGNORECASE)
        return float(m.group(1)) if m else None
    except Exception:
        return None


def _parse_money(text: str | None) -> float | None:
    """Extract a £X.XX amount from a free-text money string, e.g. 'Total: £4.20'."""
    if not text:
        return None
    m = re.search(r"([\d,]+\.\d{2})", text)
    return float(m.group(1).replace(",", "")) if m else None


def _apply_partial_credit(page: Page, total_hint: float | None = None) -> None:
    """Redeem available credit via the manual amount box + Submit.

    Used when the full-credit button isn't shown - either because credit is
    below the booking total, or (observed live: a credit balance exactly
    equal to the total can also fail to show it) some other page quirk.
    Redeems min(balance, total_hint) - capped to the total so it never asks
    to redeem more credit than is actually owed - and leaves any remainder to
    be charged to card as normal.
    """
    balance = _read_credit_balance(page)
    if not balance:
        log.info("No redeemable credit balance found on page - skipping partial credit")
        return

    amount = min(balance, total_hint) if total_hint else balance
    if amount != balance:
        log.info("Capping credit redemption to total owed: £%.2f (balance is £%.2f)", amount, balance)

    label = page.locator('label:has-text("Enter the amount to pay in credit")').first
    try:
        if not label.is_visible(timeout=3_000):
            log.info("Partial credit input not present - skipping")
            return
    except Exception:
        log.info("Partial credit input not present - skipping")
        return

    amount_input = page.locator(
        "xpath=//label[contains(., 'Enter the amount to pay in credit')]/following::input[1]"
    ).first
    submit_btn = page.locator(
        "xpath=//label[contains(., 'Enter the amount to pay in credit')]/following::button[1]"
    ).first

    try:
        amount_input.click()
        amount_input.fill(f"{amount:.2f}")
        expect(submit_btn).to_be_enabled(timeout=5_000)
        submit_btn.click()
        log.info("Redeemed £%.2f partial credit", amount)
        page.wait_for_load_state("networkidle", timeout=10_000)
        time.sleep(1)
    except Exception as exc:
        log.warning("Partial credit redemption failed: %s", exc)


# ------------------------------------------------------------------
# Payment mode helpers
# ------------------------------------------------------------------


def _select_saved_card(page: Page) -> None:
    """Click the saved card radio button."""
    for selector in [
        'input[type="radio"]:not([value*="different"])',
        'label:has-text("Pay with saved card")',
        'input[value*="saved"]',
    ]:
        try:
            el = page.locator(selector).first
            if el.is_visible(timeout=3_000):
                el.click()
                time.sleep(1)
                log.debug(f"Selected saved card via {selector}")
                return
        except Exception:
            continue
    log.debug("Saved card radio not found - assuming already selected")


def _fill_saved_card_cvv(page: Page, cvv: str) -> None:
    """Fill CVV into the plain textbox shown for saved card mode."""
    for selector in [
        'input[placeholder="CVV"]',
        'input[placeholder*="CV"]',
        'input[aria-label*="CVV"]',
        'input[aria-label*="Security"]',
        'input[name*="cvv"]',
        'input[name*="security"]',
    ]:
        try:
            loc = page.locator(selector).first
            if loc.is_visible(timeout=3_000):
                loc.click()
                loc.type(cvv, delay=80)
                log.debug("CVV filled via %s", selector)
                return
        except Exception:
            continue
    raise RuntimeError("Could not locate CVV textbox in saved card mode")


def _fill_billing_details(page: Page, card: CardDetails) -> None:
    """Fill First name, Last name, Address, Town/city, Postcode for new card mode."""
    fields = [
        (card.first_name, ['input[id="billingFirstName"]', 'input[name="billingFirstName"]']),
        (card.last_name, ['input[id="billingLastName"]', 'input[name="billingLastName"]']),
        (card.address1, ['input[name="billingAddressLineOne"]', 'input[id="billingAddressLineOne"]']),
        (card.address2, ['input[id="billingAddressLineTwo"]', 'input[name="billingAddressLineTwo"]']),
        (card.city, ['input[id="billingAddressCity"]', 'input[name="billingCity"]']),
        (card.postcode, ['input[id="billingAddressPostcode"]', 'input[name="billingPostcode"]']),
    ]
    for value, selectors in fields:
        if not value:
            continue
        for selector in selectors:
            try:
                loc = page.locator(selector).first
                if loc.is_visible(timeout=2_000):
                    loc.click()
                    loc.fill(value)
                    log.debug(f"Billing field filled via {selector}")
                    break
            except Exception:
                continue


def _select_new_card(page: Page) -> None:
    """Click the 'Pay with a different card' radio/button.

    If no such radio exists (new user with no saved card), the card form is
    already showing - log and continue.
    """
    for selector in [
        'label:has-text("Pay with a different card")',
        'input[value*="different"]',
        'input[id="new-card"]',
        'button:has-text("different card")',
        '[data-testid*="new-card"]',
    ]:
        try:
            page.click(selector, timeout=5_000)
            time.sleep(1)
            log.debug(f"Selected new card via {selector}")
            return
        except Exception:
            continue
    log.debug("'Pay with a different card' radio not found - assuming card form already visible")


def _fill_opayo_iframe(page: Page, card: CardDetails) -> None:
    """Locate the Opayo iframe and fill the required fields."""
    # Wait for Opayo iframe src to be populated in the DOM, then use
    # frame_locator (finds by element selector, handles frame load timing).
    log.debug("Waiting for Opayo iframe to load...")
    try:
        page.wait_for_function(
            "() => { const f = document.querySelector('iframe'); return f && f.src && f.src !== 'about:blank'; }",
            timeout=30_000,
        )
        log.debug("Iframe src populated")
    except Exception as exc:
        log.debug("wait_for_function timed out: %s", exc)

    # Log iframe src for debugging
    try:
        src = page.evaluate("() => { const f = document.querySelector('iframe'); return f ? f.src : 'NO IFRAME'; }")
        log.debug("Iframe src: %s", src)
    except Exception:
        pass

    opayo = page.frame_locator(
        'iframe#payment-iframe, iframe[src*="opayo"], iframe[src*="elavon"], iframe[src*="pi."], iframe:not([src="about:blank"])'
    )

    cardholder_name = " ".join(filter(None, [card.first_name, card.last_name])) or None
    if cardholder_name:
        _type_in_frame(
            opayo,
            cardholder_name,
            [
                'input[name="cardholder-name"]',
                'input[id="cardholder-name"]',
                'input[autocomplete="cc-name"]',
                'input[placeholder*="Name"]',
                'input[placeholder*="name"]',
            ],
            "cardholder name",
        )

    if card.number:
        _type_in_frame(
            opayo,
            card.number,
            [
                'input[name="card-number"]',
                'input[id="card-number"]',
                'input[autocomplete="cc-number"]',
            ],
            "card number",
        )

    if card.expiry:
        _type_in_frame(
            opayo,
            card.expiry,
            [
                'input[name="expiry-date"]',
                'input[id="expiry-date"]',
                'input[autocomplete="cc-exp"]',
            ],
            "expiry",
        )

    _type_in_frame(
        opayo,
        card.cvv,
        [
            'input[name="security-code"]',
            'input[id="security-code"]',
            'input[autocomplete="cc-csc"]',
            'input[placeholder*="CV"]',
        ],
        "CVV",
    )

    if card.save_card:
        try:
            cb = page.locator('input[name="saveCard"]').first
            if cb.is_visible(timeout=3_000) and not cb.is_checked():
                cb.check()
                log.debug("'Save card' checkbox checked")
        except Exception:
            pass


def _find_opayo_frame(page: Page) -> Frame | None:
    for frame in page.frames:
        url = frame.url
        if url and url != "about:blank" and ("opayo" in url or "elavon" in url):
            return frame
    return None


def _type_in_frame(frame_loc: Any, value: str, selectors: list[str], label: str) -> None:
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


def _accept_terms_inline(page: Page) -> None:
    """Check the inline T&Cs checkbox on the checkout page (pre-pay step).

    Better's checkout page has a checkbox "I agree to the Terms and Conditions"
    near the bottom. Must be checked before clicking Continue/Pay.
    """
    for selector in [
        'input[type="checkbox"][id*="terms"]',
        'input[type="checkbox"][name*="terms"]',
        'label:has-text("Terms and Conditions") input[type="checkbox"]',
        'input[type="checkbox"]',  # last-resort: any checkbox on page
    ]:
        try:
            cb = page.locator(selector).first
            if cb.is_visible(timeout=2_000):
                if not cb.is_checked():
                    cb.check()
                    log.debug("T&Cs inline checkbox checked via %s", selector)
                else:
                    log.debug("T&Cs inline checkbox already checked via %s", selector)
                return
        except Exception:
            continue
    log.debug("T&Cs inline checkbox not found - may already be accepted")


def _accept_terms(page: Page) -> None:
    """Click 'I Agree' on T&Cs modal, or check T&Cs checkbox if present."""
    # Modal with "I Agree" button (appears after clicking Continue)
    # We pre-click Continue then handle modal - but better to handle before.
    # The modal may appear on page load; try to dismiss it first.
    try:
        btn = page.locator('button:has-text("I Agree")').first
        if btn.is_visible(timeout=3_000):
            # Scroll modal content to bottom so "I Agree" enables
            page.evaluate("""
                () => {
                    const modal = document.querySelector('[role="dialog"], .modal, [class*="modal"], [class*="dialog"]');
                    if (modal) modal.scrollTop = modal.scrollHeight;
                    // Also scroll any overflow containers inside
                    document.querySelectorAll('*').forEach(el => {
                        if (el.scrollHeight > el.clientHeight && el.clientHeight > 50 && el.clientHeight < 600) {
                            el.scrollTop = el.scrollHeight;
                        }
                    });
                }
            """)
            time.sleep(0.3)
            btn.scroll_into_view_if_needed()
            btn.click()
            log.debug("T&Cs accepted via 'I Agree' button")
            time.sleep(0.5)
            return
    except Exception:
        pass
    # Fallback: checkbox
    for selector in [
        'input[type="checkbox"][id*="terms"]',
        'input[type="checkbox"][name*="terms"]',
        'label:has-text("Terms and Conditions") input[type="checkbox"]',
    ]:
        try:
            cb = page.locator(selector).first
            if cb.is_visible(timeout=1_000) and not cb.is_checked():
                cb.check()
                log.debug("T&Cs accepted via %s", selector)
                return
        except Exception:
            continue


def _block_analytics(page: Page) -> None:
    # Block OneTrust cookie banner CDN only.
    # Do NOT block GTM - the Better SPA uses a GTM event to trigger Opayo initialization.
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
