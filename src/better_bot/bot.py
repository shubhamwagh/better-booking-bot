"""better-booking-bot - main orchestrator.

Usage:
    uv run -m better_bot.bot --target "Abingdon Pickleball Monday 19:30"
    uv run -m better_bot.bot --list
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from better_bot.api import BetterAPI, BetterAPIError, CartItem, OccurrenceDetails, Slot
from better_bot.checkout import (
    CardDetails,
    CheckoutSession,
    PaymentAmbiguousError,
    complete_checkout,
    open_checkout_session,
    run_checkout,
)
from better_bot.notify import send as notify
from better_bot.settings import Settings

log = logging.getLogger(__name__)

# Better publishes and releases slots on venue local time. The container runs
# UTC, so every release-time decision has to be made in this zone explicitly -
# a naive datetime.now() is an hour off during BST and silently loses the race.
VENUE_TZ = ZoneInfo("Europe/London")


# ------------------------------------------------------------------
# Release-time arithmetic
# ------------------------------------------------------------------


def venue_now() -> datetime:
    return datetime.now(VENUE_TZ)


def venue_today() -> date:
    return venue_now().date()


def release_instant(release_hour: int, now: datetime | None = None) -> datetime:
    """The moment today's batch of slots opens, in venue local time."""
    return (now or venue_now()).replace(hour=release_hour, minute=0, second=0, microsecond=0)


# ------------------------------------------------------------------
# Config loading
# ------------------------------------------------------------------


def load_config(path: str | None = None) -> list[dict]:
    config_path = Path(path or os.getenv("CONFIG_PATH", "config.yaml"))
    with config_path.open() as f:
        data = yaml.safe_load(f)
    return data["targets"]


# ------------------------------------------------------------------
# Booking status - last-run result per target, for the web UI's
# history page. Lives next to config.yaml, so it rides along on the
# same shared volume with no extra deployment config.
# ------------------------------------------------------------------


def status_path() -> Path:
    config_path = Path(os.getenv("CONFIG_PATH", "config.yaml"))
    return config_path.parent / "status.json"


def log_path() -> Path:
    """Where the log file lives, so the web UI's Logs tab can tail it.

    Mirrors better_bot.daemon.log_path() - duplicated rather than imported to
    avoid a circular import (daemon.py already imports from this module).
    """
    override = os.getenv("LOG_PATH")
    if override:
        return Path(override)
    config_path = Path(os.getenv("CONFIG_PATH", "config.yaml"))
    return config_path.parent / "logs" / "daemon.log"


def load_status() -> dict:
    path = status_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def record_status(name: str, status: str, session_date: date, target_time: str, detail: str = "") -> None:
    path = status_path()
    data = load_status()
    data[name] = {
        "status": status,  # "booked" | "booked_manually" | "no_slot" | "failed"
        "session_date": session_date.isoformat(),
        "target_time": target_time,
        "detail": detail,
        "ran_at": datetime.now(UTC).isoformat(),
    }
    path.write_text(json.dumps(data, indent=2))


SECURED_STATUSES = {"booked", "booked_manually"}


def already_secured(name: str, session_date: date) -> bool:
    """True if this target's session is already booked (by the bot or manually)."""
    entry = load_status().get(name)
    if not entry:
        return False
    return entry.get("status") in SECURED_STATUSES and entry.get("session_date") == session_date.isoformat()


# ------------------------------------------------------------------
# Checkout pre-warming - opens the checkout browser during the pre-arm
# window (minutes of lead time) instead of after the strike wins (seconds
# of lead time, under the heaviest possible server load). See checkout.py's
# module docstring: the cold browser launch + render was the actual cause
# of Monday-slot checkout failures, not the cart race itself.
# ------------------------------------------------------------------


class CheckoutWarmer:
    """Opens a CheckoutSession on a background thread and holds it open
    until the strike either wins a cart item (finish()) or the window
    closes without one (abandon()).

    All Playwright calls for a given session happen on that session's own
    thread throughout its life, as Playwright's sync API requires - the
    warm-up and the eventual checkout run are the same thread, just woken
    up at a different time.
    """

    def __init__(self, token: str, headless: bool = True) -> None:
        self._token = token
        self._headless = headless
        self._session: CheckoutSession | None = None
        self._session_ready = threading.Event()
        self._inbox: queue.Queue[tuple[CardDetails, int] | None] = queue.Queue(maxsize=1)
        self._outbox: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)
        self._thread = threading.Thread(target=self._run, daemon=True, name="checkout-warmer")
        self._thread.start()

    def _run(self) -> None:
        try:
            self._session = open_checkout_session(self._token, headless=self._headless)
        except Exception as exc:
            log.warning(f"Checkout pre-warm failed, will cold-start at strike time instead: {exc}")
            self._session = None
        finally:
            self._session_ready.set()

        item = self._inbox.get()
        if item is None:
            if self._session is not None:
                self._session.close()
            return

        card, timeout_s = item
        try:
            if self._session is not None:
                ref = run_checkout(self._session, card, timeout_s=timeout_s)
            else:
                ref = complete_checkout(card, self._token, timeout_s=timeout_s, headless=self._headless)
            self._outbox.put(("ok", ref))
        except Exception as exc:
            self._outbox.put(("err", exc))
        finally:
            if self._session is not None:
                self._session.close()

    # How long to wait on the background thread beyond run_checkout()'s own
    # timeout_s before giving up. run_checkout() now enforces its own hard
    # deadline internally (every interactive step draws its timeout from a
    # shared budget - see checkout.py's Deadline) and is guaranteed to
    # return, one way or another, within timeout_s plus a small cleanup
    # margin. This pad is a backstop for something worse than a slow page:
    # a genuinely wedged browser/IPC connection that run_checkout()'s own
    # deadline can't reach because the underlying call never returns at
    # all. That should be rare - if it fires, _force_close_session() below
    # is what actually stops a stuck session from silently continuing to
    # charge a card nobody's watching any more.
    FINISH_PAD_S = 20

    def finish(self, card: CardDetails, timeout_s: int = 60) -> str:
        """Hand off the won cart item and block for the booking reference.

        Falls back to a cold complete_checkout() on the same thread if the
        pre-warm itself failed - never worse than not pre-warming at all.
        """
        self._session_ready.wait(timeout=20)
        self._inbox.put((card, timeout_s))
        try:
            kind, payload = self._outbox.get(timeout=timeout_s + self.FINISH_PAD_S)
        except queue.Empty as exc:
            closed = self._force_close_session()
            raise PaymentAmbiguousError(
                f"checkout did not respond within {timeout_s + self.FINISH_PAD_S}s even though "
                f"run_checkout() enforces its own {timeout_s}s deadline internally - the browser "
                "session itself was wedged (network/IPC hang), not a slow page. "
                + (
                    "Forcibly closed the browser session to stop it (or none was open to begin "
                    "with) - still treat this as payment-ambiguous and check the account/bank "
                    "before retrying."
                    if closed
                    else "Attempting to forcibly close the browser session ALSO failed - "
                    "treat this as a genuine payment-ambiguity risk and check the account/bank."
                )
            ) from exc
        if kind == "err":
            raise payload
        return payload

    def _force_close_session(self) -> bool:
        """Best-effort forced termination of a session whose background
        thread is still blocked past the full timeout+pad budget - i.e.
        genuinely wedged, since run_checkout()'s own Deadline otherwise
        guarantees a response well within that time. Closing the browser
        out from under a blocked Playwright call is a supported way to
        unstick it: the blocked call raises a connection error, which
        _run()'s own exception handling turns into an (now-unread) "err"
        put - but the browser process itself actually stops, which is what
        matters here. Called from THIS thread (the caller of finish()),
        while the background thread may still be blocked inside the
        session - CheckoutSession.close() is written to tolerate that.
        """
        if self._session is None:
            return True  # pre-warm itself never produced a session to close
        try:
            self._session.close()
            return True
        except Exception as exc:
            log.warning(f"Forced session close after timeout also failed: {exc}")
            return False

    def abandon(self) -> None:
        """Discard the pre-warmed session - the strike window closed without a cart."""
        try:
            self._inbox.put_nowait(None)
        except queue.Full:
            pass


# ------------------------------------------------------------------
# Core booking flow
# ------------------------------------------------------------------


def _book_slot(
    api: BetterAPI,
    target: dict,
    slot: Slot,
    session_date: date,
    card: CardDetails,
    headless: bool = True,
) -> None:
    """Occurrence details -> cart -> checkout for an already-found slot.

    Shared by run_target's initial burst and the cancellation watch's poll -
    both just need "we found a bookable slot, now actually get it".
    """
    log.info(f"Slot found: {slot.id} spaces={slot.spaces}")
    occurrence = api.get_occurrence_details(slot.id)

    cart_item = api.cart_add(slot, occurrence)
    _finish_checkout(api, target, cart_item, session_date, card, headless)


def _check_already_booked(api: BetterAPI, target: dict, session_date: date) -> str | None:
    """Best-effort check of whether OUR account already holds this slot.

    Used only to resolve a PaymentAmbiguousError - Better's Slot.booking_id
    is populated when the logged-in user already holds a booking for that
    slot, so a genuine success shows up here even if our own checkout code
    never saw the confirmation page. Returns a booking_id string if found,
    else None (found nothing - never means "definitely didn't book", since
    Better's own booking-finalisation could itself still be catching up).
    """
    try:
        slots = api.get_slots(target["venue_slug"], target["activity_slug"], session_date)
        match = next((s for s in slots if s.starts_at == target["target_time"]), None)
        return str(match.booking_id) if match and match.booking_id else None
    except Exception as exc:
        log.warning(f"Could not verify booking status via API: {exc}")
        return None


def _finish_checkout(
    api: BetterAPI,
    target: dict,
    cart_item: CartItem,
    session_date: date,
    card: CardDetails,
    headless: bool = True,
    warmer: CheckoutWarmer | None = None,
) -> None:
    """Cart -> payment -> confirmation for an item already held in the cart.

    Split from _book_slot so the release-time strike, which arrives with a
    cart item already won, can finish without repeating any lookups. If a
    CheckoutWarmer is given (the release-time strike path), hands off to its
    already-open browser session instead of cold-starting one here.
    """
    name = target["name"]
    target_time = target["target_time"]

    log.info(f"Added to cart: {cart_item.name}  £{cart_item.price_pence / 100:.2f}")

    try:
        if warmer is not None:
            ref = warmer.finish(card, timeout_s=60)
        else:
            token = api._token  # noqa: SLF001
            assert token is not None, "api.login() must be called before _book_slot()"
            ref = complete_checkout(card=card, token=token, headless=headless)
        log.info(f"Booking complete: {ref}")
        notify(
            subject=f"Booked: {name}",
            body=(
                f"Booking confirmed!\n\n"
                f"Activity: {name}\n"
                f"Session:  {session_date} {target_time}\n"
                f"Price:    £{cart_item.price_pence / 100:.2f}\n"
                f"Ref:      {ref}"
            ),
            tags="tada",
            priority="high",
            click=ref,
        )
        record_status(name, "booked", session_date, target_time, detail=ref)
    except PaymentAmbiguousError as exc:
        # A real incident: two separate failures here, each after Pay had
        # already been clicked, were each treated as an ordinary miss and
        # retried automatically - producing two real card charges for a
        # slot that was never secured. Never do that again: check whether
        # the booking actually went through before deciding anything, and
        # if it's still unresolved, stop - do not let the caller (run_target
        # / the cancellation watch) treat this as safe to retry.
        try:
            api.cart_remove(cart_item.cart_item_id)
        except Exception:
            pass

        booking_id = _check_already_booked(api, target, session_date)
        if booking_id:
            log.info(f"{name}: booking {booking_id} found on re-check - payment DID go through")
            notify(
                subject=f"Booked (recovered): {name}",
                body=(
                    f"Booking confirmed - recovered after an ambiguous checkout result.\n\n"
                    f"Activity: {name}\n"
                    f"Session:  {session_date} {target_time}\n"
                    f"Price:    £{cart_item.price_pence / 100:.2f}\n"
                    f"Booking ID: {booking_id}"
                ),
                tags="tada",
                priority="high",
            )
            record_status(name, "booked", session_date, target_time, detail=f"booking_id={booking_id} (recovered)")
            return

        log.error(f"{name}: PAYMENT AMBIGUOUS, not retrying automatically: {exc}")
        notify(
            subject=f"Payment may have been taken - {name}",
            body=(
                f"Checkout for {name} on {session_date} {target_time} failed AFTER the Pay button "
                "was clicked. The card or account credit may have already been charged even though "
                "no booking was confirmed - and this could NOT be verified against the account "
                "afterward either.\n\n"
                "This target will NOT be retried automatically. Please check your bank statement "
                "and Better account/credit balance, then re-enable the target once resolved.\n\n"
                f"Error: {exc}"
            ),
            tags="rotating_light,warning",
            priority="urgent",
        )
        record_status(name, "payment_ambiguous", session_date, target_time, detail=str(exc))
        raise
    except Exception as exc:
        try:
            api.cart_remove(cart_item.cart_item_id)
        except Exception:
            pass
        log.error(f"Checkout failed: {exc}")
        notify(
            subject=f"Booking failed: {name}",
            body=f"Checkout failed for {name} on {session_date} {target_time}.\n\nError: {exc}",
            tags="rotating_light",
            priority="urgent",
        )
        raise


def run_target(target: dict, username: str, password: str, card: CardDetails, headless: bool = True) -> None:
    name = target["name"]
    venue = target["venue_slug"]
    activity = target["activity_slug"]
    target_time = target["target_time"]  # e.g. "19:30"
    days_ahead = int(target.get("days_ahead", 7))
    release_hour = int(target.get("release_hour", 21))

    session_date = venue_today() + timedelta(days=days_ahead)
    release_at = release_instant(release_hour)
    log.info(f"Target: {name} | Date: {session_date} | Time: {target_time} | Release: {release_at:%H:%M:%S %Z}")

    if already_secured(name, session_date):
        log.info(f"{name}: already secured for {session_date} - skipping")
        return

    try:
        with BetterAPI() as api:
            api.login(username, password)
            api.fetch_membership_user_id()

            # Fast path: learn the slot id and its ticket/pricing ids while the
            # session is still listed unreleased, then fire the moment it opens.
            armed = _prearm(api, venue, activity, session_date, target_time, release_at)
            if armed is not None:
                # Open the checkout browser now, during this ample pre-arm
                # window (minutes), instead of after the strike wins
                # (seconds, under the heaviest possible server load) - see
                # CheckoutWarmer.
                token = api._token  # noqa: SLF001
                assert token is not None, "api.login() must be called before pre-warming checkout"
                warmer = CheckoutWarmer(token, headless=headless)
                cart_item = _strike(api, armed, release_at)
                if cart_item is not None:
                    _finish_checkout(api, target, cart_item, session_date, card, headless, warmer=warmer)
                    return
                warmer.abandon()
                log.warning(f"{name}: strike window closed without a cart - falling back to polling")

            slot = _wait_for_slot(api, venue, activity, session_date, target_time, release_hour)

            if slot is None:
                log.error(f"{name}: no bookable slot found for {session_date} {target_time}")
                notify(
                    subject=f"No slot: {name}",
                    body=f"No bookable slot found for {name} on {session_date} at {target_time}.",
                    tags="warning",
                    priority="high",
                )
                record_status(name, "no_slot", session_date, target_time)
                return

            _book_slot(api, target, slot, session_date, card, headless)
    except PaymentAmbiguousError:
        # _finish_checkout already recorded the correct outcome ("booked" if
        # its own API re-check resolved it, else "payment_ambiguous") and
        # already sent the appropriate notification - this only propagates
        # so _run_and_maybe_watch() (daemon.py) sees a status that is NOT
        # "failed"/"no_slot" and therefore does not arm a cancellation
        # watch. Overwriting that status back to "failed" here would defeat
        # the whole point: the cancellation watch is exactly what turned
        # one ambiguous failure into two real charges in the incident this
        # exists to prevent.
        raise
    except Exception as exc:
        record_status(name, "failed", session_date, target_time, detail=str(exc))
        raise


# ------------------------------------------------------------------
# Pre-arm + strike - the release-time race.
#
# Better lists the session a few minutes before it opens, with a real slot id
# but action_to_show.status = null. That window is where all the slow work
# belongs: discovery, the occurrence lookup, payload construction, TLS setup.
# By the time the slot flips to BOOK the only thing left is one POST.
# ------------------------------------------------------------------

PREARM_CUTOFF_S = 2.0  # stop pre-arm polling this long before release
STRIKE_CONCURRENCY = 3
STRIKE_WINDOW_S = 60.0
STRIKE_PRE_FIRE_S = 0.5  # start firing just before release - early attempts simply retry
STRIKE_STAGGER_S = 0.12  # offset workers so they don't collide on the same instant
STRIKE_RETRY_JITTER_S = (0.05, 0.25)
WARM_LEAD_S = 3.0  # measure the clock and warm sockets this long before firing


@dataclass
class Armed:
    """Everything needed to book, gathered before the slot opened."""

    slot: Slot
    occurrence: OccurrenceDetails
    payload: dict[str, Any]


def _prearm(
    api: BetterAPI,
    venue: str,
    activity: str,
    session_date: date,
    target_time: str,
    release_at: datetime,
) -> Armed | None:
    """Find the target session while it is still listed unreleased and pre-fetch
    everything the cart call needs.

    Matches on start time regardless of status - an unreleased entry has a null
    status but the same slot id it will keep once it opens. Returns None if the
    session never showed up in time, leaving the caller to fall back to polling.
    """
    while True:
        try:
            slots = api.get_slots(venue, activity, session_date)
            match = next((s for s in slots if s.starts_at == target_time), None)
            if match is not None:
                occurrence = api.get_occurrence_details(match.id)
                log.info(
                    f"Pre-armed {target_time} on {session_date}: slot={match.id} "
                    f"status={match.status} ticket={occurrence.ticket_id}"
                )
                return Armed(match, occurrence, api.build_cart_payload(match, occurrence))
        except BetterAPIError as exc:
            log.warning(f"Pre-arm attempt failed: {exc} - retrying")
        except Exception as exc:
            log.warning(f"Pre-arm attempt failed: {exc} - retrying")

        remaining = release_at.timestamp() - PREARM_CUTOFF_S - time.time()
        if remaining <= 0:
            log.info(f"Pre-arm gave up - {target_time} on {session_date} not listed before release")
            return None
        time.sleep(min(PRE_RELEASE_POLL_S, remaining))


def _strike(api: BetterAPI, armed: Armed, release_at: datetime) -> CartItem | None:
    """Hammer cart/add from just before release until one attempt lands.

    Better sheds load at release with 409 "a lot of people are trying to book
    ... Please try again". That is a queue, not a rejection, so the winner is
    whoever keeps asking from the first instant. Returns None if the whole
    window expired without a cart.
    """
    time.sleep(max(0.0, release_at.timestamp() - WARM_LEAD_S - time.time()))

    offset = api.server_clock_offset()
    api.warm_connections(STRIKE_CONCURRENCY)

    # Fire slightly early against server time: an attempt before the slot opens
    # costs one retryable error, whereas arriving late costs the booking.
    start = release_at.timestamp() - offset - STRIKE_PRE_FIRE_S
    deadline = release_at.timestamp() - offset + STRIKE_WINDOW_S

    won: list[CartItem] = []
    fatal: list[BetterAPIError] = []
    lock = threading.Lock()
    done = threading.Event()
    attempts = 0

    def worker(index: int) -> None:
        nonlocal attempts
        time.sleep(max(0.0, start + index * STRIKE_STAGGER_S - time.time()))
        while not done.is_set() and time.time() < deadline:
            try:
                with lock:
                    attempts += 1
                item = api.cart_add_prepared(armed.payload)
            except BetterAPIError as exc:
                if exc.retryable:
                    log.debug(f"Strike worker {index}: {exc} - retrying")
                    time.sleep(random.uniform(*STRIKE_RETRY_JITTER_S))
                    continue
                log.error(f"Strike worker {index} hit a final refusal: {exc}")
                with lock:
                    fatal.append(exc)
                done.set()
                return
            except Exception as exc:
                log.debug(f"Strike worker {index}: transport error {exc} - retrying")
                time.sleep(random.uniform(*STRIKE_RETRY_JITTER_S))
                continue
            with lock:
                won.append(item)
            done.set()
            return

    log.info(f"Striking {armed.slot.starts_at} at {release_at:%H:%M:%S %Z} ({STRIKE_CONCURRENCY} in flight)")
    with ThreadPoolExecutor(max_workers=STRIKE_CONCURRENCY) as pool:
        for i in range(STRIKE_CONCURRENCY):
            pool.submit(worker, i)

    # Two workers can land in the same instant. Keep the first cart item and
    # release the rest, or checkout would pay for the session twice.
    for extra in won[1:]:
        log.warning(f"Discarding duplicate cart item {extra.cart_item_id}")
        try:
            api.cart_remove(extra.cart_item_id)
        except Exception as exc:
            log.warning(f"Could not remove duplicate cart item: {exc}")

    if won:
        elapsed = time.time() - (release_at.timestamp() - offset)
        log.info(f"Cart won {elapsed:+.2f}s from release after {attempts} attempt(s)")
        return won[0]
    if fatal:
        raise fatal[0]
    log.warning(f"Strike window expired after {attempts} attempt(s)")
    return None


# ------------------------------------------------------------------
# Cancellation watch - after an initial miss, keep checking (gently)
# for someone else's cancellation to open the same slot back up.
# ------------------------------------------------------------------


def watch_and_book(
    target: dict,
    session_date: date,
    username: str,
    password: str,
    card: CardDetails,
    headless: bool = True,
) -> bool:
    """One poll of an active cancellation watch. Returns True once the watch
    should stop - either the target got secured (by this poll, or manually
    via the web UI in the meantime) or the session's own start time has
    already passed.
    """
    name = target["name"]
    target_time = target["target_time"]

    if already_secured(name, session_date):
        return True

    session_start = datetime.combine(session_date, datetime.strptime(target_time, "%H:%M").time(), tzinfo=VENUE_TZ)
    if venue_now() >= session_start:
        log.info(f"{name}: cancellation watch expired for {session_date} {target_time}")
        record_status(name, "no_slot", session_date, target_time, detail="cancellation watch expired unfilled")
        return True

    try:
        with BetterAPI() as api:
            api.login(username, password)
            api.fetch_membership_user_id()
            slots = api.get_slots(target["venue_slug"], target["activity_slug"], session_date)
            match = next((s for s in slots if s.starts_at == target_time and s.bookable), None)
            if match is None:
                return False
            log.info(f"{name}: cancellation watch found an opening for {session_date} {target_time}")
            _book_slot(api, target, match, session_date, card, headless)
    except PaymentAmbiguousError:
        # _book_slot -> _finish_checkout already resolved this (recorded
        # "booked" if its API re-check found the payment did go through, or
        # "payment_ambiguous" + an urgent alert if it couldn't be verified)
        # - either way, STOP watching. This is the exact mechanism that
        # turned one ambiguous failure into two real card charges in the
        # incident this exists to prevent: continuing to watch meant the
        # next 3-minute poll tried to pay again for a booking whose first
        # payment attempt's outcome was never actually known.
        return True
    except Exception as exc:
        log.warning(f"{name}: cancellation watch attempt failed, still watching: {exc}")
        record_status(name, "failed", session_date, target_time, detail=str(exc))
        return False

    return already_secured(name, session_date)


# ------------------------------------------------------------------
# Slot polling
# ------------------------------------------------------------------

POLL_INTERVAL_S = 2
PRE_RELEASE_POLL_S = 10
MAX_WAIT_S = 300


def _wait_for_slot(
    api: BetterAPI,
    venue: str,
    activity: str,
    session_date: date,
    target_time: str,
    release_hour: int,
) -> Slot | None:
    deadline = venue_now() + timedelta(seconds=MAX_WAIT_S)

    while venue_now() < deadline:
        # Venue local time, not container time - the container runs UTC and a
        # naive hour comparison never reaches release_hour during BST.
        at_release = venue_now() >= release_instant(release_hour)

        try:
            slots = api.get_slots(venue, activity, session_date)
        except BetterAPIError as exc:
            log.warning(f"Slot poll error: {exc} - retrying")
            time.sleep(POLL_INTERVAL_S)
            continue

        bookable = [s for s in slots if s.starts_at == target_time and s.bookable]
        if bookable:
            return bookable[0]

        if not at_release:
            log.debug(f"Pre-release - waiting {PRE_RELEASE_POLL_S}s before next poll")
            time.sleep(PRE_RELEASE_POLL_S)
        else:
            log.debug(f"Slot not yet available - polling in {POLL_INTERVAL_S}s")
            time.sleep(POLL_INTERVAL_S)

    return None


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Better (GLL) activity booking bot")
    p.add_argument("--target", help="Run a specific target by name")
    p.add_argument("--list", action="store_true", help="List configured targets and exit")
    p.add_argument("--config", default=None, help="Path to config.yaml")
    p.add_argument("--dry-run", action="store_true", help="Poll for slot but do not book")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--no-headless", action="store_true", help="Show browser window (for debugging)")
    return p


def main() -> None:
    args = build_parser().parse_args()

    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        handlers=[
            logging.StreamHandler(),
            RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3),
        ],
    )

    targets = load_config(args.config)

    if args.list:
        for t in targets:
            status = "enabled" if t.get("enabled", True) else "disabled"
            print(f"  [{status}] {t['name']}  ({t['venue_slug']}/{t['activity_slug']} @ {t['target_time']})")
        return

    settings = Settings()
    username = settings.better_username
    password = settings.better_password

    # CVV is needed for card payment; may be absent if user always has enough credit.
    # We allow it to be unset but will fail at checkout if card payment is actually required.
    if not settings.card_cvv and not settings.card_number:
        log.warning("CARD_CVV not set - will only work if account credit covers the full booking cost")

    if settings.card_number and not settings.card_expiry:
        print("Error: CARD_NUMBER set but CARD_EXPIRY missing in .env", file=sys.stderr)
        sys.exit(1)

    card = settings.to_card()
    log.info(f"Payment mode: {'new card' if settings.card_number else 'saved card'}")

    enabled = [t for t in targets if t.get("enabled", True)]

    if args.target:
        enabled = [t for t in enabled if t["name"] == args.target]
        if not enabled:
            print(f"No enabled target named '{args.target}'", file=sys.stderr)
            sys.exit(1)

    if not enabled:
        print("No enabled targets found in config.", file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        log.info("Dry-run mode - will not complete checkout")

    for target in enabled:
        if args.dry_run:
            _dry_run(target, username, password)
        else:
            try:
                run_target(target, username, password, card, headless=not args.no_headless)
            except Exception as exc:
                log.error(f"Target '{target['name']}' failed: {exc}")


def _dry_run(target: dict, username: str, password: str) -> None:
    session_date = venue_today() + timedelta(days=int(target.get("days_ahead", 7)))
    log.info(f"[DRY RUN] {target['name']} - checking slots for {session_date} @ {target['target_time']}")
    with BetterAPI() as api:
        api.login(username, password)
        api.fetch_membership_user_id()
        slots = api.get_slots(target["venue_slug"], target["activity_slug"], session_date)
        for s in slots:
            log.info(f"  {s.starts_at}  status={s.status:<6}  spaces={s.spaces}  id={s.id}")


if __name__ == "__main__":
    main()
