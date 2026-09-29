"""Playwright-based checkout flow.

Handles Opayo payment via three modes:
  - credit only: full balance covered by account credit - no card entry needed.
  - saved card:  radio already selected, inject CVV only.
  - new card:    click "Pay with a different card", fill number + expiry + CVV.

Credit is applied against the total before card payment: the "pay full amount
using credit" button when credit covers the whole cost, else the manual
"enter amount to redeem" box for whatever partial balance is available - the
remainder is then charged to the card as normal.

open_checkout_session()/run_checkout() split the browser launch from the
actual payment steps, so a release-time strike can open the browser (and
render the checkout page) minutes early, during the pre-arm window, then
just reload once a slot is actually won - see bot.py's CheckoutWarmer.
complete_checkout() is the cold-start convenience wrapper for callers that
don't pre-warm.

Every interactive step draws its timeout from a single shared Deadline
instead of its own independent constant - a real incident traced to exactly
this gap: one `.click()` with no explicit timeout silently used Playwright's
~30s default and stalled there, and the *sum* of a dozen such independent
timeouts had no overall ceiling. A Deadline bounds the whole attempt by
construction. Once the Pay button has actually been clicked, any further
failure is raised as PaymentAmbiguousError rather than a plain error - Opayo
may already have charged the card even though our own confirmation-page
detection failed, and callers must never treat that the same as "never
attempted payment, safe to retry" (see bot.py's CheckoutWarmer and
_finish_checkout for what "never treat the same" actually means).

Only this module needs a browser - everything else is pure API.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from playwright.sync_api import Browser, BrowserContext, Frame, Page, Playwright, expect, sync_playwright
from pydantic import BaseModel

BOOKINGS_BASE = "https://bookings.better.org.uk"

log = logging.getLogger(__name__)

# The floor under _wait_for_confirmation()'s own budget, applied on top of
# whatever's left on the shared Deadline. This is the highest-stakes step -
# money may already have moved - so it always gets a fair chance to observe
# a genuine confirmation, even if earlier steps (which are now bounded and
# should be fast) ran unexpectedly close to the wire.
CONFIRMATION_MIN_S = 20.0


class PaymentAmbiguousError(RuntimeError):
    """Checkout failed AFTER the Pay button was clicked.

    This means Opayo may already have charged the card (or the account
    credit) even though the booking never confirmed on our side. A real
    incident: two separate attempts each clicked Pay, each then failed on
    "Checkout did not confirm within 30s", and each was treated as an
    ordinary miss and retried automatically - producing two real charges
    for a slot that was never actually secured.

    Callers (bot.py's _finish_checkout / CheckoutWarmer) must never
    auto-retry on this - check whether the booking actually went through via
    the API first, and if it didn't, stop and require a human to check the
    account/bank before deciding what to do next.
    """


class Deadline:
    """A wall-clock budget shared across every step of one checkout attempt.

    See the module docstring for the incident this exists to prevent: each
    step drawing its timeout from this SAME instance means the total wall
    time for one attempt is bounded by construction, not by hoping a dozen
    independently-chosen numbers all stay small.
    """

    def __init__(self, budget_s: float) -> None:
        self._deadline = time.monotonic() + budget_s

    def remaining_s(self) -> float:
        return self._deadline - time.monotonic()

    def remaining_ms(self, cap_ms: int, floor_ms: int = 300) -> int:
        """Milliseconds left, capped at cap_ms and floored at floor_ms.

        floor_ms keeps an almost-expired deadline from handing Playwright a
        0ms timeout - some calls treat that as "no timeout", the opposite of
        what's intended here. A tiny floor still fails fast; it just doesn't
        skip the actionability check Playwright would otherwise perform.
        """
        return max(min(int(self.remaining_s() * 1000), cap_ms), floor_ms)

    def expired(self) -> bool:
        return self.remaining_s() <= 0


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


@dataclass
class CheckoutSession:
    """A live, logged-in browser sitting on the checkout page.

    Opening this (browser launch, TLS handshake, JS bundle download, first
    render) is the slow, flaky part of checkout - and under release-time
    load it's slow enough to make the saved-card detection race. Opening it
    minutes early, during the ample pre-arm window, means the only thing
    left to do once a slot is actually won is reload the same warm page.
    """

    playwright: Playwright
    browser: Browser
    context: BrowserContext
    page: Page

    def close(self) -> None:
        """Idempotent and exception-safe - safe to call more than once, and
        safe to call from a thread other than the one driving the page (see
        CheckoutWarmer._force_close_session, which does exactly that to
        unstick a genuinely wedged background thread)."""
        try:
            self.browser.close()
        except Exception:
            pass
        try:
            self.playwright.stop()
        except Exception:
            pass


def open_checkout_session(token: str, headless: bool = True) -> CheckoutSession:
    """Launch a browser, log in via cookie, and land on the checkout page.

    Safe to call well before a cart item exists - Better's checkout page
    renders its "your basket is empty" state in the same app shell, so this
    still warms the TLS connection, JS bundle, and render pipeline that
    run_checkout() will reuse the moment there's something to pay for.
    """
    pw = sync_playwright().start()
    browser = pw.chromium.launch(
        headless=headless,
        args=["--disable-blink-features=AutomationControlled"],
    )
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
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

    log.info("Pre-warming checkout page…")
    page.goto(f"{BOOKINGS_BASE}/basket/checkout", wait_until="networkidle", timeout=30_000)
    _dismiss_cookie_banner(page)
    log.info("Checkout session warm and ready")

    return CheckoutSession(playwright=pw, browser=browser, context=context, page=page)


def run_checkout(session: CheckoutSession, card: CardDetails, timeout_s: int = 60) -> str:
    """Complete payment on an already-open CheckoutSession.

    Reloads the checkout page first, so a session opened before the cart had
    anything in it picks up the real item - a warm reload against an
    already-established connection with the JS bundle already cached,
    rather than a cold navigation from a fresh browser process.

    Auto-detects payment mode from the page:
      1. Applies any available account credit.
      2. If total drops to £0 → confirms without card entry.
      3. Else if saved card radio present → saved card mode (CVV only).
      4. Else → new card mode (billing details + full card).

    Args:
        session: An open CheckoutSession (from open_checkout_session()).
        card: Card credentials. For saved card mode only cvv is needed.
              For new card mode also number, expiry, and billing fields.
        timeout_s: Overall wall-clock budget for the whole attempt (shared
            Deadline - see the module docstring), except the final
            confirmation wait, which is always given at least
            CONFIRMATION_MIN_S regardless of how much of this budget the
            earlier steps used.

    Returns:
        Booking reference URL or ID.

    Raises:
        RuntimeError: A failure before the Pay button was clicked - never
            attempted payment, safe to retry.
        PaymentAmbiguousError: A failure after the Pay button was clicked -
            payment may have already gone through. Never retry on this.
    """
    deadline = Deadline(timeout_s)
    page = session.page

    log.info("Loading checkout with the won cart item…")
    page.goto(
        f"{BOOKINGS_BASE}/basket/checkout", wait_until="networkidle", timeout=deadline.remaining_ms(cap_ms=30_000)
    )
    _dismiss_cookie_banner(page)
    time.sleep(2)

    # Step 1: apply any available credit
    _apply_credit(page, deadline)

    # Step 2: detect payment mode from page
    if _is_zero_balance(page, deadline):
        log.info("Credit covers full balance - no card entry needed")
    elif _has_saved_card(page, deadline):
        log.info("Saved card detected - selecting saved card and filling CVV")
        if not card.cvv:
            raise RuntimeError("CARD_CVV required for saved card checkout but not set")
        _select_saved_card(page, deadline)
        _fill_saved_card_cvv(page, deadline, card.cvv)
    elif card.number and card.expiry:
        log.info("No saved card - entering new card details")
        _select_new_card(page, deadline)
        _fill_billing_details(page, deadline, card)
        _fill_opayo_iframe(page, deadline, card)
    else:
        # A saved-card account should never genuinely lack a saved card at
        # checkout - a "not found" here after _has_saved_card()'s own
        # retries means the page didn't render it in time, not that it's
        # absent. Falling through to new-card mode would just fail anyway
        # (no CARD_NUMBER configured) after wasting more time - fail now
        # with a clear reason instead. Pay has not been clicked yet - safe
        # to retry.
        raise RuntimeError(
            "Saved card not detected on checkout page (and no CARD_NUMBER/CARD_EXPIRY configured "
            "for new-card mode) - likely a slow page render, not a genuinely missing card"
        )

    # Step 3: accept T&Cs and pay. Everything from here on is the
    # payment-ambiguity zone - once pay_btn.click() succeeds, ANY failure
    # gets wrapped as PaymentAmbiguousError instead of propagating as a
    # plain error, so callers can never mistake "already possibly charged"
    # for "never attempted, safe to retry".
    _accept_terms_inline(page, deadline)

    pay_clicked = False
    try:
        log.info("Clicking Pay / Continue…")
        pay_btn = page.locator(
            'button[aria-label="Pay now"], button:has-text("Pay now"), '
            'button:has-text("Pay £"), button:has-text("Continue")'
        ).first
        expect(pay_btn).to_be_enabled(timeout=deadline.remaining_ms(cap_ms=15_000))
        pay_btn.click(timeout=deadline.remaining_ms(cap_ms=10_000))
        pay_clicked = True

        # Accept T&Cs modal if it appears after clicking Pay (fallback)
        _accept_terms(page, deadline)

        log.info("Waiting for booking confirmation…")
        confirm_budget_s = max(deadline.remaining_s(), CONFIRMATION_MIN_S)
        ref = _wait_for_confirmation(page, confirm_budget_s)
        log.info("Booking confirmed: %s", ref)
        return ref
    except Exception as exc:
        if pay_clicked and not isinstance(exc, PaymentAmbiguousError):
            raise PaymentAmbiguousError(
                f"Payment may have been submitted but checkout did not confirm it: {exc}"
            ) from exc
        raise


def complete_checkout(
    card: CardDetails,
    token: str,
    timeout_s: int = 60,
    confirm: bool = False,
    headless: bool = True,
) -> str:
    """Open a checkout session and complete payment in one call.

    Convenience wrapper around open_checkout_session() + run_checkout() for
    callers that don't pre-warm (the cancellation watch, ad-hoc scripts).
    The release-time strike path pre-warms via open_checkout_session() well
    before the cart is won instead - see bot.py's CheckoutWarmer.
    """
    session = open_checkout_session(token, headless=headless)
    try:
        return run_checkout(session, card, timeout_s=timeout_s)
    finally:
        session.close()


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


def _is_zero_balance(page: Page, deadline: Deadline, settle_s: float = 5.0) -> bool:
    """Return True if the total to pay is £0 after credit was applied.

    Polls for up to settle_s (bounded by the shared deadline) rather than
    checking once - a real incident traced a card being charged on top of
    already-applied credit to exactly this gap: the page hadn't re-rendered
    the new £0.00 total yet, this returned a false "can't tell, not zero" on
    the very first check, and checkout fell through to ALSO filling in and
    charging the saved card, which should never have been needed.
    """
    settle_deadline = time.time() + settle_s
    while True:
        try:
            text = page.locator('button:has-text("Pay £0"), button:has-text("Pay £0.00")').first
            if text.is_visible(timeout=deadline.remaining_ms(cap_ms=1_000)):
                log.info("Total to pay is £0 (Pay £0 button visible)")
                return True
        except Exception:
            pass
        # Fallback: parse summary total from page text
        total = _read_total_to_pay(page)
        if total:
            if "0.00" in total:
                log.info("Total to pay after credit check: %s", total)
                return True
            log.info("Total to pay after credit check: %s (not zero)", total)
            return False
        if time.time() >= settle_deadline or deadline.expired():
            log.info("Could not read 'Total to pay' summary line after %.0fs - assuming non-zero", settle_s)
            return False
        time.sleep(0.5)


def _has_saved_card(page: Page, deadline: Deadline) -> bool:
    """Return True if a saved card radio button is visible on the checkout page.

    Polls against the shared deadline rather than checking once - under
    release-time load the payment section can take several seconds to
    render, and a false "not found" here sends checkout into new-card mode,
    which is a guaranteed failure for an account with no CARD_NUMBER
    configured.
    """
    while True:
        try:
            radio = page.locator('input[id="saved-card"], input[value="saved_card"]').first
            if radio.is_visible(timeout=deadline.remaining_ms(cap_ms=1_000)):
                return True
        except Exception:
            pass
        if deadline.expired():
            return False
        time.sleep(0.5)


def _apply_credit(page: Page, deadline: Deadline) -> None:
    """Apply available account credit toward the total.

    Tries 'Pay full amount using credit' first (shown when credit fully covers
    the total). If that button isn't there - typically because credit is less
    than the total, but observed live to also fail to show even when credit
    exactly equals the total - falls back to the manual 'Enter the amount to
    pay in credit' box, redeeming whatever credit is available as a partial
    payment and leaving the remainder to be charged to card as normal.
    """
    total_before = _read_total_to_pay(page)
    if total_before:
        log.info("Total to pay before credit check: %s", total_before)

    try:
        btn = page.locator('button:has-text("Pay full amount using credit")').first
        if btn.is_visible(timeout=deadline.remaining_ms(cap_ms=5_000)):
            btn.click(timeout=deadline.remaining_ms(cap_ms=5_000))
            log.info("Clicked 'Pay full amount using credit'")
            # Wait for page to reflect updated total (network idle or URL change)
            page.wait_for_load_state("networkidle", timeout=deadline.remaining_ms(cap_ms=10_000))
            time.sleep(1)
            return
        log.info("'Pay full amount using credit' button not visible - trying partial credit redemption")
    except Exception as exc:
        log.warning("'Pay full amount using credit' button click failed: %s", exc)

    _apply_partial_credit(page, deadline, total_hint=_parse_money(total_before))


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


def _apply_partial_credit(page: Page, deadline: Deadline, total_hint: float | None = None) -> None:
    """Redeem available credit via the manual amount box + Submit.

    Used when the full-credit button isn't shown - either because credit is
    below the booking total, or (observed live: a credit balance exactly
    equal to the total can also fail to show it) some other page quirk.
    Redeems min(balance, total_hint) - capped to the total so it never asks
    to redeem more credit than is actually owed - and leaves any remainder to
    be charged to card as normal.

    A real incident traced to exactly this function: the Submit button click
    below had no explicit timeout, silently used Playwright's ~30s default,
    and hung there - burning half of the whole attempt's budget on its own.
    Every action here now draws its timeout from the shared deadline instead.
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
        if not label.is_visible(timeout=deadline.remaining_ms(cap_ms=3_000)):
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
        amount_input.click(timeout=deadline.remaining_ms(cap_ms=5_000))
        amount_input.fill(f"{amount:.2f}", timeout=deadline.remaining_ms(cap_ms=5_000))
        expect(submit_btn).to_be_enabled(timeout=deadline.remaining_ms(cap_ms=5_000))
        submit_btn.click(timeout=deadline.remaining_ms(cap_ms=5_000))
        log.info("Redeemed £%.2f partial credit", amount)
        page.wait_for_load_state("networkidle", timeout=deadline.remaining_ms(cap_ms=10_000))
        time.sleep(1)
    except Exception as exc:
        log.warning("Partial credit redemption failed: %s", exc)


# ------------------------------------------------------------------
# Payment mode helpers
# ------------------------------------------------------------------


def _select_saved_card(page: Page, deadline: Deadline) -> None:
    """Click the saved card radio button.

    A real incident traced part of a multi-minute stall to this function:
    three candidate selectors, each clicked with no explicit timeout (so
    each could silently burn Playwright's ~30s default) - worst case,
    three unbounded clicks in a row. Every click here is now capped against
    the shared deadline instead.
    """
    for selector in [
        'input[type="radio"]:not([value*="different"])',
        'label:has-text("Pay with saved card")',
        'input[value*="saved"]',
    ]:
        try:
            el = page.locator(selector).first
            if el.is_visible(timeout=deadline.remaining_ms(cap_ms=3_000)):
                el.click(timeout=deadline.remaining_ms(cap_ms=3_000))
                time.sleep(1)
                log.debug(f"Selected saved card via {selector}")
                return
        except Exception:
            continue
    log.debug("Saved card radio not found - assuming already selected")


def _fill_saved_card_cvv(page: Page, deadline: Deadline, cvv: str) -> None:
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
            if loc.is_visible(timeout=deadline.remaining_ms(cap_ms=3_000)):
                loc.click(timeout=deadline.remaining_ms(cap_ms=3_000))
                loc.type(cvv, delay=80, timeout=deadline.remaining_ms(cap_ms=3_000))
                log.debug("CVV filled via %s", selector)
                return
        except Exception:
            continue
    raise RuntimeError("Could not locate CVV textbox in saved card mode")


def _fill_billing_details(page: Page, deadline: Deadline, card: CardDetails) -> None:
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
                if loc.is_visible(timeout=deadline.remaining_ms(cap_ms=2_000)):
                    loc.click(timeout=deadline.remaining_ms(cap_ms=2_000))
                    loc.fill(value, timeout=deadline.remaining_ms(cap_ms=2_000))
                    log.debug(f"Billing field filled via {selector}")
                    break
            except Exception:
                continue


def _select_new_card(page: Page, deadline: Deadline) -> None:
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
            page.click(selector, timeout=deadline.remaining_ms(cap_ms=5_000))
            time.sleep(1)
            log.debug(f"Selected new card via {selector}")
            return
        except Exception:
            continue
    log.debug("'Pay with a different card' radio not found - assuming card form already visible")


def _fill_opayo_iframe(page: Page, deadline: Deadline, card: CardDetails) -> None:
    """Locate the Opayo iframe and fill the required fields."""
    # Wait for Opayo iframe src to be populated in the DOM, then use
    # frame_locator (finds by element selector, handles frame load timing).
    log.debug("Waiting for Opayo iframe to load...")
    try:
        page.wait_for_function(
            "() => { const f = document.querySelector('iframe'); return f && f.src && f.src !== 'about:blank'; }",
            timeout=deadline.remaining_ms(cap_ms=30_000),
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
            deadline,
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
            deadline,
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
            deadline,
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
        deadline,
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
            if cb.is_visible(timeout=deadline.remaining_ms(cap_ms=3_000)) and not cb.is_checked():
                cb.check(timeout=deadline.remaining_ms(cap_ms=3_000))
                log.debug("'Save card' checkbox checked")
        except Exception:
            pass


def _find_opayo_frame(page: Page) -> Frame | None:
    for frame in page.frames:
        url = frame.url
        if url and url != "about:blank" and ("opayo" in url or "elavon" in url):
            return frame
    return None


def _type_in_frame(frame_loc: Any, deadline: Deadline, value: str, selectors: list[str], label: str) -> None:
    """Type value into first matching selector inside a frame_locator."""
    for selector in selectors:
        try:
            loc = frame_loc.locator(selector).first
            loc.wait_for(state="visible", timeout=deadline.remaining_ms(cap_ms=30_000))
            loc.click(timeout=deadline.remaining_ms(cap_ms=5_000))
            loc.type(value, delay=80, timeout=deadline.remaining_ms(cap_ms=5_000))
            log.debug("%s typed in frame via %s", label, selector)
            return
        except Exception:
            continue
    raise RuntimeError(f"Could not locate {label} field in Opayo iframe")


def _fill_field(frame: Frame, deadline: Deadline, value: str, selectors: list[str], label: str) -> None:
    for selector in selectors:
        try:
            frame.wait_for_selector(selector, timeout=deadline.remaining_ms(cap_ms=5_000))
            frame.fill(selector, value, timeout=deadline.remaining_ms(cap_ms=5_000))
            log.debug("%s filled via selector %s", label, selector)
            return
        except Exception:
            continue
    raise RuntimeError(f"Could not locate {label} field in Opayo iframe")


def _type_field(frame: Frame, deadline: Deadline, value: str, selectors: list[str], label: str) -> None:
    """Like _fill_field but uses type() to simulate real keypresses (needed for CVV)."""
    for selector in selectors:
        try:
            frame.wait_for_selector(selector, timeout=deadline.remaining_ms(cap_ms=5_000))
            frame.click(selector, timeout=deadline.remaining_ms(cap_ms=5_000))
            frame.type(selector, value, delay=80, timeout=deadline.remaining_ms(cap_ms=5_000))
            log.debug("%s typed via selector %s", label, selector)
            return
        except Exception:
            continue
    raise RuntimeError(f"Could not locate {label} field in Opayo iframe")


# ------------------------------------------------------------------
# Confirmation polling
# ------------------------------------------------------------------


def _wait_for_confirmation(page: Page, timeout_s: float) -> str:
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
    raise RuntimeError(f"Checkout did not confirm within {timeout_s:.0f}s")


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


def _accept_terms_inline(page: Page, deadline: Deadline) -> None:
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
            if cb.is_visible(timeout=deadline.remaining_ms(cap_ms=2_000)):
                if not cb.is_checked():
                    cb.check(timeout=deadline.remaining_ms(cap_ms=2_000))
                    log.debug("T&Cs inline checkbox checked via %s", selector)
                else:
                    log.debug("T&Cs inline checkbox already checked via %s", selector)
                return
        except Exception:
            continue
    log.debug("T&Cs inline checkbox not found - may already be accepted")


def _accept_terms(page: Page, deadline: Deadline) -> None:
    """Click 'I Agree' on T&Cs modal, or check T&Cs checkbox if present."""
    # Modal with "I Agree" button (appears after clicking Continue)
    # We pre-click Continue then handle modal - but better to handle before.
    # The modal may appear on page load; try to dismiss it first.
    try:
        btn = page.locator('button:has-text("I Agree")').first
        if btn.is_visible(timeout=deadline.remaining_ms(cap_ms=3_000)):
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
            btn.scroll_into_view_if_needed(timeout=deadline.remaining_ms(cap_ms=3_000))
            btn.click(timeout=deadline.remaining_ms(cap_ms=3_000))
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
            if cb.is_visible(timeout=deadline.remaining_ms(cap_ms=1_000)) and not cb.is_checked():
                cb.check(timeout=deadline.remaining_ms(cap_ms=1_000))
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
