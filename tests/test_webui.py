"""Tests for the config web UI."""

from __future__ import annotations

import textwrap
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from better_bot.api import BetterAPIError, Slot
from better_bot.webui import SECURED_STATUSES, app, load_status, save_status

client = TestClient(app)


@pytest.fixture(autouse=True)
def _config(tmp_path: Path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        textwrap.dedent("""\
        targets:
          - name: "My Target"
            venue_slug: "v"
            activity_slug: "a"
            target_time: "19:30"
            days_ahead: 7
            release_hour: 21
            cron: "0 20 * * 1"
            enabled: true
    """)
    )
    monkeypatch.setenv("CONFIG_PATH", str(cfg))
    monkeypatch.setenv("BETTER_USERNAME", "user@example.com")
    monkeypatch.setenv("BETTER_PASSWORD", "pass")
    return cfg


def _record(name: str, status: str, session_date: date, target_time: str = "19:30", detail: str = ""):
    data = load_status()
    data[name] = {
        "status": status,
        "session_date": session_date.isoformat(),
        "target_time": target_time,
        "detail": detail,
        "ran_at": "2026-08-01T00:00:00+00:00",
    }
    save_status(data)


def _make_slot(starts_at="19:30", status="BOOK", booking_id=None) -> Slot:
    return Slot(id="s1", starts_at=starts_at, status=status, spaces=3, composite_key="ck", booking_id=booking_id)


# ------------------------------------------------------------------
# History page - secured_for_this_session date logic (the bug fix)
# ------------------------------------------------------------------


class TestStatusPageAction:
    def test_booked_session_still_upcoming_shows_cancel_not_mark_booked(self):
        _record("My Target", "booked", date.today() + timedelta(days=5))
        r = client.get("/status")
        assert "cancel booking" in r.text
        assert "mark booked" not in r.text

    def test_booked_session_today_shows_cancel(self):
        _record("My Target", "booked", date.today())
        r = client.get("/status")
        assert "cancel booking" in r.text
        assert "mark booked" not in r.text

    def test_booked_session_in_past_shows_mark_booked_again(self):
        """Regression: recomputing session_date from days_ahead used to make this
        show 'mark booked' for a target the bot legitimately secured, unless viewed
        on the exact day the cron job ran. Now it should stay secured until the
        session date itself has passed."""
        _record("My Target", "booked", date.today() - timedelta(days=1))
        r = client.get("/status")
        assert "mark booked" in r.text
        assert "cancel booking" not in r.text

    def test_booked_manually_upcoming_shows_cancel(self):
        _record("My Target", "booked_manually", date.today() + timedelta(days=1))
        r = client.get("/status")
        assert "cancel booking" in r.text

    def test_failed_status_shows_mark_booked(self):
        _record("My Target", "failed", date.today() + timedelta(days=5))
        r = client.get("/status")
        assert "mark booked" in r.text
        assert "cancel booking" not in r.text

    def test_no_entry_shows_mark_booked(self):
        r = client.get("/status")
        assert "not run yet" in r.text
        assert "mark booked" in r.text


# ------------------------------------------------------------------
# mark-booked
# ------------------------------------------------------------------


def test_mark_booked_records_booked_manually():
    r = client.post("/targets/My%20Target/mark-booked", follow_redirects=False)
    assert r.status_code == 303
    entry = load_status()["My Target"]
    assert entry["status"] == "booked_manually"
    assert entry["session_date"] == (date.today() + timedelta(days=7)).isoformat()


# ------------------------------------------------------------------
# cancel booking
# ------------------------------------------------------------------


class TestCancelRoute:
    def test_no_status_entry_is_a_noop(self):
        with patch("better_bot.webui.BetterAPI") as mock_api_cls:
            r = client.post("/targets/My%20Target/cancel", follow_redirects=False)
        assert r.status_code == 303
        mock_api_cls.assert_not_called()

    def test_unsecured_status_is_a_noop(self):
        _record("My Target", "failed", date.today() + timedelta(days=5))
        with patch("better_bot.webui.BetterAPI") as mock_api_cls:
            client.post("/targets/My%20Target/cancel", follow_redirects=False)
        mock_api_cls.assert_not_called()
        assert load_status()["My Target"]["status"] == "failed"

    def test_secured_and_slot_found_cancels_and_records_status(self):
        session_date = date.today() + timedelta(days=5)
        _record("My Target", "booked", session_date, detail="https://booking-confirmed/1")
        api = MagicMock()
        api.__enter__.return_value = api
        api.__exit__.return_value = False
        api.get_slots.return_value = [_make_slot("19:30", "BOOK", booking_id=9200576)]

        with patch("better_bot.webui.BetterAPI", return_value=api):
            r = client.post("/targets/My%20Target/cancel", follow_redirects=False)

        assert r.status_code == 303
        api.login.assert_called_once_with("user@example.com", "pass")
        api.cancel_booking.assert_called_once_with(9200576)
        entry = load_status()["My Target"]
        assert entry["status"] == "cancelled"
        assert entry["session_date"] == session_date.isoformat()

    def test_no_matching_slot_leaves_status_untouched(self):
        session_date = date.today() + timedelta(days=5)
        _record("My Target", "booked", session_date, detail="ref")
        api = MagicMock()
        api.__enter__.return_value = api
        api.__exit__.return_value = False
        api.get_slots.return_value = [_make_slot("20:00", "BOOK", booking_id=None)]  # wrong time, no booking

        with patch("better_bot.webui.BetterAPI", return_value=api):
            client.post("/targets/My%20Target/cancel", follow_redirects=False)

        api.cancel_booking.assert_not_called()
        assert load_status()["My Target"]["status"] == "booked"

    def test_api_error_during_cancel_leaves_status_untouched(self):
        session_date = date.today() + timedelta(days=5)
        _record("My Target", "booked", session_date, detail="ref")
        api = MagicMock()
        api.__enter__.return_value = api
        api.__exit__.return_value = False
        api.get_slots.return_value = [_make_slot("19:30", "BOOK", booking_id=42)]
        api.cancel_booking.side_effect = BetterAPIError(403, "Forbidden")

        with patch("better_bot.webui.BetterAPI", return_value=api):
            client.post("/targets/My%20Target/cancel", follow_redirects=False)

        assert load_status()["My Target"]["status"] == "booked"


# ------------------------------------------------------------------
# toggle / delete
# ------------------------------------------------------------------


def test_toggle_flips_enabled():
    r = client.post("/targets/My%20Target/toggle", follow_redirects=False)
    assert r.status_code == 303
    from better_bot.webui import load_config

    targets = load_config()["targets"]
    assert targets[0]["enabled"] is False


def test_delete_removes_target():
    r = client.post("/targets/My%20Target/delete", follow_redirects=False)
    assert r.status_code == 303
    from better_bot.webui import load_config

    assert load_config()["targets"] == []


def test_secured_statuses_constant():
    assert SECURED_STATUSES == {"booked", "booked_manually"}


# ------------------------------------------------------------------
# add target - name is derived, not typed in
# ------------------------------------------------------------------

_VENUES = [{"slug": "white-horse", "name": "White Horse Leisure Centre", "town": "Abingdon"}]
_ACTIVITIES = [{"slug": "pickleball-drop-in", "name": "Pickleball", "category": "Racket sports"}]


def _post_add_target(**overrides):
    form = {
        "venue_slug": "white-horse",
        "activity_slug": "pickleball-drop-in",
        "weekday": "1",
        "target_time": "19:30",
        "days_ahead": "7",
        "release_hour": "21",
    }
    form.update(overrides)
    with (
        patch("better_bot.webui._cached_venues", return_value=_VENUES),
        patch("better_bot.webui._cached_activities", return_value=_ACTIVITIES),
    ):
        return client.post("/targets", data=form, follow_redirects=False)


def test_add_target_derives_name_from_venue_activity_day_time():
    from better_bot.webui import load_config

    r = _post_add_target()
    assert r.status_code == 303
    names = [t["name"] for t in load_config()["targets"]]
    assert "Abingdon Pickleball Monday 19:30" in names


def test_add_target_dedupes_name_collision():
    from better_bot.webui import load_config

    _post_add_target()
    _post_add_target()  # same venue/activity/day/time again
    names = [t["name"] for t in load_config()["targets"]]
    assert names.count("Abingdon Pickleball Monday 19:30") == 1
    assert "Abingdon Pickleball Monday 19:30 (2)" in names


def test_add_target_logs_lookup_failure_and_falls_back_to_slugs(caplog):
    from better_bot.webui import load_config

    with (
        patch("better_bot.webui._cached_venues", side_effect=RuntimeError("venue api down")),
        patch("better_bot.webui._cached_activities", side_effect=RuntimeError("activity api down")),
        caplog.at_level("DEBUG", logger="better_bot.webui"),
    ):
        r = client.post(
            "/targets",
            data={
                "venue_slug": "white-horse",
                "activity_slug": "pickleball-drop-in",
                "weekday": "1",
                "target_time": "19:30",
                "days_ahead": "7",
                "release_hour": "21",
            },
            follow_redirects=False,
        )

    assert r.status_code == 303
    names = [t["name"] for t in load_config()["targets"]]
    # Falls back to the raw slugs when venue/activity lookups fail, instead of crashing.
    assert "white-horse pickleball-drop-in Monday 19:30" in names
    assert "venue api down" in caplog.text
    assert "activity api down" in caplog.text


def test_add_target_rejects_missing_venue():
    r = _post_add_target(venue_slug="")
    assert r.status_code == 200
    assert "Pick a venue and an activity" in r.text
