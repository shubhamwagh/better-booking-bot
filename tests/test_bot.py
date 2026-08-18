"""Tests for bot orchestration logic."""

from __future__ import annotations

import textwrap
import threading
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from better_bot.api import BetterAPIError, CartItem, OccurrenceDetails, Slot
from better_bot.bot import (
    MAX_WAIT_S,
    POLL_INTERVAL_S,
    PRE_RELEASE_POLL_S,
    STRIKE_WINDOW_S,
    VENUE_TZ,
    Armed,
    _prearm,
    _strike,
    _wait_for_slot,
    already_secured,
    build_parser,
    load_config,
    load_status,
    record_status,
    release_instant,
    run_target,
    venue_now,
    venue_today,
    watch_and_book,
)
from better_bot.checkout import CardDetails

# ------------------------------------------------------------------
# load_config
# ------------------------------------------------------------------


def test_load_config(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        textwrap.dedent("""\
        targets:
          - name: "Test Session"
            venue_slug: "my-venue"
            activity_slug: "my-activity"
            target_time: "10:00"
            days_ahead: 7
            release_hour: 21
            cron: "0 20 * * 1"
            enabled: true
    """)
    )
    targets = load_config(str(cfg))
    assert len(targets) == 1
    assert targets[0]["name"] == "Test Session"
    assert targets[0]["venue_slug"] == "my-venue"


def test_load_config_multiple_targets(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        textwrap.dedent("""\
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
    """)
    )
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
    with (
        patch("better_bot.bot.BetterAPI", return_value=api),
        patch("better_bot.bot.complete_checkout", return_value="https://booking-confirmed/1"),
    ):
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
    with (
        patch("better_bot.bot.BetterAPI", return_value=api),
        patch("better_bot.bot.complete_checkout", side_effect=RuntimeError("checkout boom")),
    ):
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


def _make_slot(starts_at: str = "19:30", status: str | None = "BOOK") -> Slot:
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

    with patch("better_bot.bot.time.sleep"), patch("better_bot.bot.MAX_WAIT_S", 0):
        slot = _wait_for_slot(api, "venue", "activity", date.today(), "19:30", release_hour=0)

    assert slot is None


def test_wait_for_slot_full_status_not_returned():
    api = MagicMock()
    api.get_slots.return_value = [_make_slot("19:30", "FULL")]

    with patch("better_bot.bot.time.sleep"), patch("better_bot.bot.MAX_WAIT_S", 0):
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

    with patch("better_bot.bot.time.sleep"), patch("better_bot.bot.MAX_WAIT_S", 0):
        slot = _wait_for_slot(api, "venue", "activity", date.today(), "19:30", release_hour=0)

    assert slot is None


# ------------------------------------------------------------------
# Release-time arithmetic - the regression that lost the 2026-08-17 race
# ------------------------------------------------------------------


def test_release_instant_is_venue_local_not_container_local():
    """21:00 BST is 20:00 UTC. The container runs UTC, so a naive hour
    comparison never reaches release_hour=21 and the bot slow-polls straight
    through the release."""
    now = datetime(2026, 8, 17, 20, 57, tzinfo=VENUE_TZ)
    release = release_instant(21, now=now)
    assert release.isoformat() == "2026-08-17T21:00:00+01:00"
    assert release.astimezone(UTC).hour == 20


def test_release_instant_handles_gmt_half_of_the_year():
    now = datetime(2026, 12, 7, 20, 57, tzinfo=VENUE_TZ)
    release = release_instant(21, now=now)
    assert release.astimezone(UTC).hour == 21


def _run_wait_for_slot_at(now: datetime, release_hour: int) -> list[float]:
    """Run one _wait_for_slot iteration with the clock pinned to `now`,
    returning the sleeps it asked for."""
    api = MagicMock()
    api.get_slots.return_value = []
    clock = {"now": now}
    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["now"] += timedelta(seconds=MAX_WAIT_S + 1)  # end the loop

    with (
        patch("better_bot.bot.venue_now", lambda: clock["now"]),
        patch("better_bot.bot.time.sleep", fake_sleep),
    ):
        _wait_for_slot(api, "venue", "activity", date(2026, 8, 24), "19:30", release_hour=release_hour)
    return sleeps


def test_wait_for_slot_polls_fast_once_venue_clock_passes_release():
    # 21:00:05 BST = 20:00:05 UTC - the exact moment the old code got wrong.
    sleeps = _run_wait_for_slot_at(datetime(2026, 8, 17, 21, 0, 5, tzinfo=VENUE_TZ), release_hour=21)
    assert sleeps == [POLL_INTERVAL_S]


def test_wait_for_slot_polls_gently_before_release():
    sleeps = _run_wait_for_slot_at(datetime(2026, 8, 17, 20, 57, 0, tzinfo=VENUE_TZ), release_hour=21)
    assert sleeps == [PRE_RELEASE_POLL_S]


# ------------------------------------------------------------------
# _prearm
# ------------------------------------------------------------------


def _prearm_api() -> MagicMock:
    api = MagicMock()
    api.get_occurrence_details.return_value = OccurrenceDetails(ticket_id="t1", pricing_option_id=8329)
    api.build_cart_payload.return_value = {"items": [{"id": "s1"}]}
    return api


def test_prearm_arms_from_unreleased_null_status_entry():
    """Better lists the session minutes early with a null status but the real
    slot id - that is the whole head start."""
    api = _prearm_api()
    api.get_slots.return_value = [_make_slot("19:30", None)]

    armed = _prearm(api, "venue", "activity", date(2026, 8, 24), "19:30", venue_now() + timedelta(minutes=3))

    assert armed is not None
    assert armed.slot.status is None
    assert armed.occurrence.ticket_id == "t1"
    assert armed.payload == {"items": [{"id": "s1"}]}
    api.get_occurrence_details.assert_called_once_with("s1")


def test_prearm_ignores_other_start_times():
    api = _prearm_api()
    api.get_slots.return_value = [_make_slot("20:00", None), _make_slot("18:00", "BOOK")]

    armed = _prearm(api, "venue", "activity", date(2026, 8, 24), "19:30", venue_now() - timedelta(seconds=1))

    assert armed is None
    api.get_occurrence_details.assert_not_called()


def test_prearm_retries_through_unreleased_date_error():
    api = _prearm_api()
    api.get_slots.side_effect = [BetterAPIError(422, "not within valid days"), [_make_slot("19:30", None)]]

    with patch("better_bot.bot.time.sleep"):
        armed = _prearm(api, "venue", "activity", date(2026, 8, 24), "19:30", venue_now() + timedelta(minutes=3))

    assert armed is not None
    assert api.get_slots.call_count == 2


def test_prearm_gives_up_when_session_never_listed():
    api = _prearm_api()
    api.get_slots.return_value = []

    armed = _prearm(api, "venue", "activity", date(2026, 8, 24), "19:30", venue_now() - timedelta(seconds=1))

    assert armed is None


# ------------------------------------------------------------------
# _strike
# ------------------------------------------------------------------

CONTENTION = "Sorry, a lot of people are trying to book at the moment and we were unable to process your request."


def _armed() -> Armed:
    return Armed(
        slot=_make_slot("19:30", None),
        occurrence=OccurrenceDetails(ticket_id="t1", pricing_option_id=8329),
        payload={"items": [{"id": "s1"}]},
    )


def _strike_api() -> MagicMock:
    api = MagicMock()
    api.server_clock_offset.return_value = 0.0
    return api


def test_strike_retries_through_contention_409_then_wins():
    """The 409 that killed the 2026-08-17 run is backpressure, not a refusal."""
    api = _strike_api()
    won = CartItem(cart_item_id=7, name="Pickleball", price_pence=315)
    api.cart_add_prepared.side_effect = [
        BetterAPIError(409, CONTENTION),
        BetterAPIError(409, CONTENTION),
        won,
    ]

    with patch("better_bot.bot.STRIKE_CONCURRENCY", 1), patch("better_bot.bot.time.sleep"):
        item = _strike(api, _armed(), venue_now())

    assert item == won
    assert api.cart_add_prepared.call_count == 3
    api.warm_connections.assert_called_once()


def test_strike_stops_on_final_refusal():
    api = _strike_api()
    api.cart_add_prepared.side_effect = BetterAPIError(409, "The session being booked is already full")

    with patch("better_bot.bot.STRIKE_CONCURRENCY", 1), patch("better_bot.bot.time.sleep"):
        try:
            _strike(api, _armed(), venue_now())
        except BetterAPIError as exc:
            assert "already full" in str(exc)
        else:
            raise AssertionError("a full session should not be retried")

    assert api.cart_add_prepared.call_count == 1


def test_strike_returns_none_when_window_expires():
    api = _strike_api()
    api.cart_add_prepared.side_effect = BetterAPIError(409, CONTENTION)

    # Release long enough ago that the strike window has already closed.
    release_at = venue_now() - timedelta(seconds=STRIKE_WINDOW_S + 5)
    with patch("better_bot.bot.STRIKE_CONCURRENCY", 1), patch("better_bot.bot.time.sleep"):
        assert _strike(api, _armed(), release_at) is None


def test_strike_releases_duplicate_cart_items():
    """Two workers can land in the same instant - checkout must not pay twice."""
    api = _strike_api()
    barrier = threading.Barrier(2, timeout=5)
    ids = iter([11, 12])
    lock = threading.Lock()

    def add(_payload):
        barrier.wait()  # hold both workers in flight so both genuinely win
        with lock:
            return CartItem(cart_item_id=next(ids), name="Pickleball", price_pence=315)

    api.cart_add_prepared.side_effect = add

    with patch("better_bot.bot.STRIKE_CONCURRENCY", 2), patch("better_bot.bot.time.sleep"):
        item = _strike(api, _armed(), venue_now())

    assert item is not None
    api.cart_remove.assert_called_once()
    (removed,) = api.cart_remove.call_args.args
    assert removed != item.cart_item_id
    assert {removed, item.cart_item_id} == {11, 12}


# ------------------------------------------------------------------
# run_target end to end on the fast path
# ------------------------------------------------------------------


def test_run_target_books_via_prearm_and_strike(tmp_path: Path, monkeypatch):
    """The whole point: discovery and the occurrence lookup happen before
    release, so the release-time work is one prepared cart POST."""
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "config.yaml"))
    target = {
        "name": "My Target",
        "venue_slug": "v",
        "activity_slug": "a",
        "target_time": "19:30",
        "days_ahead": 7,
        "release_hour": 21,
    }

    api = _mock_api()
    api.get_slots.return_value = [_make_slot("19:30", None)]  # listed, not yet open
    api.get_occurrence_details.return_value = OccurrenceDetails(ticket_id="t1", pricing_option_id=8329)
    api.build_cart_payload.return_value = {"items": [{"id": "s1"}]}
    api.server_clock_offset.return_value = 0.0
    api.cart_add_prepared.return_value = CartItem(cart_item_id=7, name="Pickleball", price_pence=315)

    # release_instant is keyed off real wall-clock seconds inside _strike (only
    # time.sleep is mocked, not time.time()), so pin it to "right now" - any
    # value truncated to the top of an hour risks landing outside the strike's
    # window depending on which minute the test happens to run in.
    with (
        patch("better_bot.bot.BetterAPI", return_value=api),
        patch("better_bot.bot.STRIKE_CONCURRENCY", 1),
        patch("better_bot.bot.time.sleep"),
        patch("better_bot.bot.release_instant", return_value=venue_now()),
        patch("better_bot.bot.complete_checkout", return_value="https://booking-confirmed/9"),
    ):
        run_target(target, "user", "pass", CardDetails(cvv="123"))

    status = load_status()["My Target"]
    assert status["status"] == "booked"
    assert status["detail"] == "https://booking-confirmed/9"
    assert status["session_date"] == (venue_today() + timedelta(days=7)).isoformat()

    # Fired the prepared payload, never fell back to the slow discover-then-book path.
    api.cart_add_prepared.assert_called_once_with({"items": [{"id": "s1"}]})
    api.cart_add.assert_not_called()
    assert api.get_slots.call_count == 1


def test_run_target_falls_back_to_polling_when_prearm_finds_nothing(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "config.yaml"))
    target = {
        "name": "My Target",
        "venue_slug": "v",
        "activity_slug": "a",
        "target_time": "19:30",
        "days_ahead": 7,
        "release_hour": venue_now().hour,
    }

    api = _mock_api()
    # Never listed during pre-arm, then appears open on the polling fallback.
    api.get_slots.side_effect = [[], [_make_slot("19:30", "BOOK")]]
    api.get_occurrence_details.return_value = OccurrenceDetails(ticket_id="t1", pricing_option_id=8329)
    api.cart_add.return_value = CartItem(cart_item_id=7, name="Pickleball", price_pence=315)

    with (
        patch("better_bot.bot.BetterAPI", return_value=api),
        patch("better_bot.bot.time.sleep"),
        patch("better_bot.bot.complete_checkout", return_value="https://booking-confirmed/9"),
    ):
        run_target(target, "user", "pass", CardDetails(cvv="123"))

    assert load_status()["My Target"]["status"] == "booked"
    api.cart_add.assert_called_once()
    api.cart_add_prepared.assert_not_called()
