"""Tests for bot orchestration logic."""

from __future__ import annotations

import textwrap
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from better_bot.bot import _wait_for_slot, build_parser, load_config
from better_bot.api import BetterAPIError, Slot


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
