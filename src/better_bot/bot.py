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
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml
from dotenv import load_dotenv

from better_bot.api import BetterAPI, BetterAPIError, Slot
from better_bot.checkout import CardDetails, complete_checkout
from better_bot.notify import send as notify

log = logging.getLogger(__name__)


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
        "ran_at": datetime.now(timezone.utc).isoformat(),
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
    name = target["name"]
    target_time = target["target_time"]

    log.info(f"Slot found: {slot.id} spaces={slot.spaces}")
    occurrence = api.get_occurrence_details(slot.id)

    cart_item = api.cart_add(slot, occurrence)
    log.info(f"Added to cart: {cart_item.name}  £{cart_item.price_pence / 100:.2f}")

    try:
        ref = complete_checkout(card=card, token=api._token, headless=headless)  # noqa: SLF001
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
    target_time = target["target_time"]          # e.g. "19:30"
    days_ahead = int(target.get("days_ahead", 7))
    release_hour = int(target.get("release_hour", 21))

    session_date = date.today() + timedelta(days=days_ahead)
    log.info(f"Target: {name} | Date: {session_date} | Time: {target_time}")

    if already_secured(name, session_date):
        log.info(f"{name}: already secured for {session_date} - skipping")
        return

    try:
        with BetterAPI() as api:
            api.login(username, password)
            api.fetch_membership_user_id()

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

    session_start = datetime.combine(session_date, datetime.strptime(target_time, "%H:%M").time())
    if datetime.now() >= session_start:
        log.info(f"{name}: cancellation watch expired for {session_date} {target_time}")
        record_status(name, "no_slot", session_date, target_time, detail="cancellation watch expired unfilled")
        return True

    try:
        with BetterAPI() as api:
            api.login(username, password)
            api.fetch_membership_user_id()
            slots = api.get_slots(target["venue_slug"], target["activity_slug"], session_date)
            match = next((s for s in slots if s.starts_at == target_time and s.status == "BOOK"), None)
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
    import datetime as dt

    deadline = dt.datetime.now() + dt.timedelta(seconds=MAX_WAIT_S)

    while dt.datetime.now() < deadline:
        now_hour = dt.datetime.now().hour
        at_release = now_hour >= release_hour

        try:
            slots = api.get_slots(venue, activity, session_date)
        except BetterAPIError as exc:
            log.warning(f"Slot poll error: {exc} - retrying")
            time.sleep(POLL_INTERVAL_S)
            continue

        bookable = [s for s in slots if s.starts_at == target_time and s.status == "BOOK"]
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

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )

    load_dotenv()

    targets = load_config(args.config)

    if args.list:
        for t in targets:
            status = "enabled" if t.get("enabled", True) else "disabled"
            print(f"  [{status}] {t['name']}  ({t['venue_slug']}/{t['activity_slug']} @ {t['target_time']})")
        return

    username = os.environ["BETTER_USERNAME"]
    password = os.environ["BETTER_PASSWORD"]
    cvv = os.getenv("CARD_CVV")
    card_number = os.getenv("CARD_NUMBER")
    card_expiry = os.getenv("CARD_EXPIRY")

    # CVV is needed for card payment; may be absent if user always has enough credit.
    # We allow it to be unset but will fail at checkout if card payment is actually required.
    if not cvv and not card_number:
        log.warning("CARD_CVV not set - will only work if account credit covers the full booking cost")

    if card_number and not card_expiry:
        print("Error: CARD_NUMBER set but CARD_EXPIRY missing in .env", file=sys.stderr)
        sys.exit(1)

    card = CardDetails(
        cvv=cvv or "",
        number=card_number,
        expiry=card_expiry,
        first_name=os.getenv("BILLING_FIRST_NAME"),
        last_name=os.getenv("BILLING_LAST_NAME"),
        address1=os.getenv("BILLING_ADDRESS1"),
        address2=os.getenv("BILLING_ADDRESS2"),
        city=os.getenv("BILLING_CITY"),
        postcode=os.getenv("BILLING_POSTCODE"),
        save_card=os.getenv("SAVE_CARD", "false").lower() in ("1", "true", "yes"),
    )
    log.info(f"Payment mode: {'new card' if card_number else 'saved card'}")

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
    session_date = date.today() + timedelta(days=int(target.get("days_ahead", 7)))
    log.info(f"[DRY RUN] {target['name']} - checking slots for {session_date} @ {target['target_time']}")
    with BetterAPI() as api:
        api.login(username, password)
        api.fetch_membership_user_id()
        slots = api.get_slots(target["venue_slug"], target["activity_slug"], session_date)
        for s in slots:
            log.info(f"  {s.starts_at}  status={s.status:<6}  spaces={s.spaces}  id={s.id}")


if __name__ == "__main__":
    main()
