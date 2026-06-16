"""better-booking-bot — main orchestrator.

Usage:
    uv run -m better_bot.bot --target "Abingdon Pickleball Monday 19:30"
    uv run -m better_bot.bot --list
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import date, timedelta
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
# Core booking flow
# ------------------------------------------------------------------

def run_target(target: dict, username: str, password: str, card: CardDetails) -> None:
    name = target["name"]
    venue = target["venue_slug"]
    activity = target["activity_slug"]
    target_time = target["target_time"]          # e.g. "19:30"
    days_ahead = int(target.get("days_ahead", 7))
    release_hour = int(target.get("release_hour", 21))

    session_date = date.today() + timedelta(days=days_ahead)
    log.info("Target: %s | Date: %s | Time: %s", name, session_date, target_time)

    with BetterAPI() as api:
        # 1. Login
        api.login(username, password)
        token = api._token  # noqa: SLF001  — needed for checkout browser session
        api.fetch_membership_user_id()

        # 2. Poll until slot opens
        slot = _wait_for_slot(api, venue, activity, session_date, target_time, release_hour)

        if slot is None:
            log.error("%s: no bookable slot found for %s %s", name, session_date, target_time)
            notify(
                subject=f"No slot: {name}",
                body=f"No bookable slot found for {name} on {session_date} at {target_time}.",
            )
            return

        # 3. Get occurrence details
        log.info("Slot found: %s spaces=%d", slot.id, slot.spaces)
        occurrence = api.get_occurrence_details(slot.id)

        # 4. Add to cart
        cart_item = api.cart_add(slot, occurrence)
        log.info("Added to cart: %s  £%.2f", cart_item.name, cart_item.price_pence / 100)

        # 5. Complete checkout (Playwright + Opayo CVV)
        try:
            ref = complete_checkout(card=card, token=token)
            log.info("Booking complete: %s", ref)
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
        except Exception as exc:
            try:
                api.cart_remove(cart_item.cart_item_id)
            except Exception:
                pass
            log.error("Checkout failed: %s", exc)
            notify(
                subject=f"Booking failed: {name}",
                body=f"Checkout failed for {name} on {session_date} {target_time}.\n\nError: {exc}",
            )
            raise


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
            log.warning("Slot poll error: %s — retrying", exc)
            time.sleep(POLL_INTERVAL_S)
            continue

        bookable = [s for s in slots if s.starts_at == target_time and s.status == "BOOK"]
        if bookable:
            return bookable[0]

        if not at_release:
            log.debug("Pre-release — waiting %ds before next poll", PRE_RELEASE_POLL_S)
            time.sleep(PRE_RELEASE_POLL_S)
        else:
            log.debug("Slot not yet available — polling in %ds", POLL_INTERVAL_S)
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
    return p


def main() -> None:
    args = build_parser().parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
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

    if not cvv:
        print(
            "Error: CARD_CVV not set in .env\n"
            "  Saved card mode:  set CARD_CVV=<3-digit CVV>\n"
            "  New card mode:    set CARD_NUMBER, CARD_EXPIRY, and CARD_CVV",
            file=sys.stderr,
        )
        sys.exit(1)

    if card_number and not card_expiry:
        print("Error: CARD_NUMBER set but CARD_EXPIRY missing in .env", file=sys.stderr)
        sys.exit(1)

    card = CardDetails(cvv=cvv, number=card_number, expiry=card_expiry)
    log.info("Payment mode: %s", "new card" if card_number else "saved card")

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
        log.info("Dry-run mode — will not complete checkout")

    for target in enabled:
        if args.dry_run:
            _dry_run(target, username, password)
        else:
            try:
                run_target(target, username, password, card)
            except Exception as exc:
                log.error("Target '%s' failed: %s", target["name"], exc)


def _dry_run(target: dict, username: str, password: str) -> None:
    session_date = date.today() + timedelta(days=int(target.get("days_ahead", 7)))
    log.info("[DRY RUN] %s — checking slots for %s @ %s", target["name"], session_date, target["target_time"])
    with BetterAPI() as api:
        api.login(username, password)
        api.fetch_membership_user_id()
        slots = api.get_slots(target["venue_slug"], target["activity_slug"], session_date)
        for s in slots:
            log.info("  %s  status=%-6s  spaces=%d  id=%s", s.starts_at, s.status, s.spaces, s.id)


if __name__ == "__main__":
    main()
