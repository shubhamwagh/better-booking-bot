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
from better_bot.checkout import CardDetails, complete_checkout
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


def _finish_checkout(
    api: BetterAPI,
    target: dict,
    cart_item: CartItem,
    session_date: date,
    card: CardDetails,
    headless: bool = True,
) -> None:
    """Cart -> payment -> confirmation for an item already held in the cart.

    Split from _book_slot so the release-time strike, which arrives with a
    cart item already won, can finish without repeating any lookups.
    """
    name = target["name"]
    target_time = target["target_time"]

    log.info(f"Added to cart: {cart_item.name}  £{cart_item.price_pence / 100:.2f}")

    token = api._token  # noqa: SLF001
    assert token is not None, "api.login() must be called before _book_slot()"

    try:
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
        )
        record_status(name, "booked", session_date, target_time, detail=ref)
    except Exception as exc:
        try:
            api.cart_remove(cart_item.cart_item_id)
        except Exception:
            pass
        log.error(f"Checkout failed: {exc}")
        notify(
            subject=f"Booking failed: {name}",
            body=f"Checkout failed for {name} on {session_date} {target_time}.\n\nError: {exc}",
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
                cart_item = _strike(api, armed, release_at)
                if cart_item is not None:
                    _finish_checkout(api, target, cart_item, session_date, card, headless)
                    return
                log.warning(f"{name}: strike window closed without a cart - falling back to polling")

            slot = _wait_for_slot(api, venue, activity, session_date, target_time, release_hour)

            if slot is None:
                log.error(f"{name}: no bookable slot found for {session_date} {target_time}")
                notify(
                    subject=f"No slot: {name}",
                    body=f"No bookable slot found for {name} on {session_date} at {target_time}.",
                )
                record_status(name, "no_slot", session_date, target_time)
                return

            _book_slot(api, target, slot, session_date, card, headless)
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
