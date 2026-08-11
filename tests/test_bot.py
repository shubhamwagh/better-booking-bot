"""Tests for bot orchestration logic."""

from __future__ import annotations

import textwrap
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from better_bot.bot import _wait_for_slot, already_secured, build_parser, load_config, load_status, record_status, run_target, watch_and_book
from better_bot.api import BetterAPIError, CartItem, OccurrenceDetails, Slot
from better_bot.checkout import CardDetails


# ------------------------------------------------------------------
# load_config
# ------------------------------------------------------------------

def test_load_config(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(textwrap.dedent("""\
        targets:
          - name: "Test Session"
            venue_slug: "my-venue"
            activity_slug: "my-activity"
            target_time: "10:00"
            days_ahead: 7
            release_hour: 21
            cron: "0 20 * * 1"
            enabled: true
    """))
    targets = load_config(str(cfg))
    assert len(targets) == 1
    assert targets[0]["name"] == "Test Session"
    assert targets[0]["venue_slug"] == "my-venue"


def test_load_config_multiple_targets(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(textwrap.dedent("""\
        targets:
          - name: "A"
            venue_slug: "v1"
            activity_slug: "a1"
            target_time: "09:00"
            enabled: true
          - name: "B"
            venue_slug: "v2"
            activity_slug: "a2"
            target_time: "10:00"
            enabled: false
    """))
    targets = load_config(str(cfg))
    assert len(targets) == 2
    assert targets[1]["enabled"] is False


# ------------------------------------------------------------------
# status.json - record_status / load_status / already_secured
# ------------------------------------------------------------------

def test_record_and_load_status(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "config.yaml"))
    record_status("My Target", "booked", date(2026, 8, 17), "19:30", detail="ref-123")
    data = load_status()
    assert data["My Target"]["status"] == "booked"
    assert data["My Target"]["session_date"] == "2026-08-17"
    assert data["My Target"]["detail"] == "ref-123"


def test_already_secured_true_for_matching_booked_session(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "config.yaml"))
    record_status("My Target", "booked_manually", date(2026, 8, 17), "19:30")
    assert already_secured("My Target", date(2026, 8, 17)) is True


def test_already_secured_false_for_different_session_date(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "config.yaml"))
    record_status("My Target", "booked", date(2026, 8, 17), "19:30")
    assert already_secured("My Target", date(2026, 8, 24)) is False


def test_already_secured_false_for_unsettled_status(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "config.yaml"))
    record_status("My Target", "failed", date(2026, 8, 17), "19:30")
    assert already_secured("My Target", date(2026, 8, 17)) is False


def test_already_secured_false_when_no_status_file(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "config.yaml"))
    assert already_secured("Nonexistent", date(2026, 8, 17)) is False


def test_run_target_skips_when_already_secured(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "config.yaml"))
    target = {
        "name": "My Target",
        "venue_slug": "v",
        "activity_slug": "a",
        "target_time": "19:30",
        "days_ahead": 7,
    }
    session_date = date.today() + timedelta(days=7)
    record_status("My Target", "booked_manually", session_date, "19:30")
    with patch("better_bot.bot.BetterAPI") as mock_api_cls:
        run_target(target, "user", "pass", CardDetails(cvv="123"))
    mock_api_cls.assert_not_called()


# ------------------------------------------------------------------
# watch_and_book
# ------------------------------------------------------------------

def _watch_target() -> dict:
    return {"name": "My Target", "venue_slug": "v", "activity_slug": "a", "target_time": "19:30"}


def _mock_api() -> MagicMock:
    api = MagicMock()
    api.__enter__.return_value = api
    api.__exit__.return_value = False
    return api


def test_watch_and_book_stops_if_already_secured(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "config.yaml"))
    session_date = date.today() + timedelta(days=3)
    record_status("My Target", "booked_manually", session_date, "19:30")
    with patch("better_bot.bot.BetterAPI") as mock_api_cls:
        stopped = watch_and_book(_watch_target(), session_date, "user", "pass", CardDetails(cvv="123"))
    assert stopped is True
    mock_api_cls.assert_not_called()


def test_watch_and_book_stops_and_records_no_slot_when_expired(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "config.yaml"))
    past_date = date.today() - timedelta(days=1)
    with patch("better_bot.bot.BetterAPI") as mock_api_cls:
        stopped = watch_and_book(_watch_target(), past_date, "user", "pass", CardDetails(cvv="123"))
    assert stopped is True
    mock_api_cls.assert_not_called()
    assert load_status()["My Target"]["status"] == "no_slot"


def test_watch_and_book_keeps_watching_when_no_match(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "config.yaml"))
    session_date = date.today() + timedelta(days=3)
    api = _mock_api()
    api.get_slots.return_value = [_make_slot("20:00", "BOOK")]  # different time, no match
    with patch("better_bot.bot.BetterAPI", return_value=api):
        stopped = watch_and_book(_watch_target(), session_date, "user", "pass", CardDetails(cvv="123"))
    assert stopped is False


def test_watch_and_book_books_when_cancellation_opens_slot(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "config.yaml"))
    session_date = date.today() + timedelta(days=3)
    api = _mock_api()
    api.get_slots.return_value = [_make_slot("19:30", "BOOK")]
    api.get_occurrence_details.return_value = OccurrenceDetails(ticket_id="t1", pricing_option_id=1)
    api.cart_add.return_value = CartItem(cart_item_id=1, name="Pickleball", price_pence=315)
    with patch("better_bot.bot.BetterAPI", return_value=api), \
         patch("better_bot.bot.complete_checkout", return_value="https://booking-confirmed/1"):
        stopped = watch_and_book(_watch_target(), session_date, "user", "pass", CardDetails(cvv="123"))
    assert stopped is True
    status = load_status()["My Target"]
    assert status["status"] == "booked"
    assert status["detail"] == "https://booking-confirmed/1"


def test_watch_and_book_keeps_watching_after_checkout_failure(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "config.yaml"))
    session_date = date.today() + timedelta(days=3)
    api = _mock_api()
    api.get_slots.return_value = [_make_slot("19:30", "BOOK")]
    api.get_occurrence_details.return_value = OccurrenceDetails(ticket_id="t1", pricing_option_id=1)
    api.cart_add.return_value = CartItem(cart_item_id=1, name="Pickleball", price_pence=315)
    with patch("better_bot.bot.BetterAPI", return_value=api), \
         patch("better_bot.bot.complete_checkout", side_effect=RuntimeError("checkout boom")):
        stopped = watch_and_book(_watch_target(), session_date, "user", "pass", CardDetails(cvv="123"))
    assert stopped is False
    assert load_status()["My Target"]["status"] == "failed"
    api.cart_remove.assert_called_once_with(1)


# ------------------------------------------------------------------
# build_parser
# ------------------------------------------------------------------

class TestBuildParser:
    def test_defaults(self):
        p = build_parser()
        args = p.parse_args([])
        assert args.target is None
        assert args.list is False
        assert args.dry_run is False
        assert args.verbose is False
        assert args.no_headless is False

    def test_target_flag(self):
        args = build_parser().parse_args(["--target", "My Session"])
        assert args.target == "My Session"

    def test_list_flag(self):
        args = build_parser().parse_args(["--list"])
        assert args.list is True

    def test_dry_run_flag(self):
        args = build_parser().parse_args(["--dry-run"])
        assert args.dry_run is True

    def test_verbose_flag(self):
        args = build_parser().parse_args(["-v"])
        assert args.verbose is True


# ------------------------------------------------------------------
# _wait_for_slot
# ------------------------------------------------------------------

def _make_slot(starts_at: str = "19:30", status: str = "BOOK") -> Slot:
    return Slot(id="s1", starts_at=starts_at, status=status, spaces=3, composite_key="ck")


def test_wait_for_slot_found_immediately():
    api = MagicMock()
    api.get_slots.return_value = [_make_slot("19:30", "BOOK")]

    with patch("better_bot.bot.time.sleep"):
        slot = _wait_for_slot(api, "venue", "activity", date.today(), "19:30", release_hour=0)

    assert slot is not None
    assert slot.id == "s1"


def test_wait_for_slot_wrong_time_not_returned():
    api = MagicMock()
    # Slot exists but at wrong time
    api.get_slots.return_value = [_make_slot("20:00", "BOOK")]

    with patch("better_bot.bot.time.sleep"), \
         patch("better_bot.bot.MAX_WAIT_S", 0):
        slot = _wait_for_slot(api, "venue", "activity", date.today(), "19:30", release_hour=0)

    assert slot is None


def test_wait_for_slot_full_status_not_returned():
    api = MagicMock()
    api.get_slots.return_value = [_make_slot("19:30", "FULL")]

    with patch("better_bot.bot.time.sleep"), \
         patch("better_bot.bot.MAX_WAIT_S", 0):
        slot = _wait_for_slot(api, "venue", "activity", date.today(), "19:30", release_hour=0)

    assert slot is None


def test_wait_for_slot_api_error_retries():
    api = MagicMock()
    api.get_slots.side_effect = [
        BetterAPIError(422, "Not yet released"),
        [_make_slot("19:30", "BOOK")],
    ]

    with patch("better_bot.bot.time.sleep"):
        slot = _wait_for_slot(api, "venue", "activity", date.today(), "19:30", release_hour=0)

    assert slot is not None
    assert api.get_slots.call_count == 2


def test_wait_for_slot_timeout_returns_none():
    api = MagicMock()
    api.get_slots.return_value = []  # never has a bookable slot

    with patch("better_bot.bot.time.sleep"), \
         patch("better_bot.bot.MAX_WAIT_S", 0):
        slot = _wait_for_slot(api, "venue", "activity", date.today(), "19:30", release_hour=0)

    assert slot is None
