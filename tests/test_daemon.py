"""Tests for daemon scheduler helpers."""

from __future__ import annotations

import textwrap
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from better_bot.bot import load_status, record_status
from better_bot.checkout import CardDetails
from better_bot.daemon import (
    _cleanup_stale_watches,
    _job_id,
    _resume_watch_if_pending,
    _run_and_maybe_watch,
    _start_watch,
    _sync_jobs,
    _watch_job_id,
)


# ------------------------------------------------------------------
# _job_id
# ------------------------------------------------------------------

def test_job_id_format():
    target = {
        "venue_slug": "my-venue",
        "activity_slug": "pickleball",
        "target_time": "19:30",
    }
    assert _job_id(target) == "my-venue|pickleball|19:30"


def test_job_id_unique_per_time():
    t1 = {"venue_slug": "v", "activity_slug": "a", "target_time": "09:00"}
    t2 = {"venue_slug": "v", "activity_slug": "a", "target_time": "10:00"}
    assert _job_id(t1) != _job_id(t2)


# ------------------------------------------------------------------
# _sync_jobs
# ------------------------------------------------------------------

def _card() -> CardDetails:
    return CardDetails(cvv="123")


def test_sync_jobs_adds_new_job(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(textwrap.dedent("""\
        targets:
          - name: "Test"
            venue_slug: "v"
            activity_slug: "a"
            target_time: "19:30"
            cron: "57 20 * * 1"
            enabled: true
    """))
    scheduler = MagicMock()
    ids = _sync_jobs(scheduler, cfg, set(), "user", "pass", _card())
    assert scheduler.add_job.called
    assert len(ids) == 1


def test_sync_jobs_skips_disabled(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(textwrap.dedent("""\
        targets:
          - name: "Test"
            venue_slug: "v"
            activity_slug: "a"
            target_time: "19:30"
            cron: "57 20 * * 1"
            enabled: false
    """))
    scheduler = MagicMock()
    ids = _sync_jobs(scheduler, cfg, set(), "user", "pass", _card())
    assert not scheduler.add_job.called
    assert ids == set()


def test_sync_jobs_removes_old_job(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("targets: []\n")
    scheduler = MagicMock()
    existing = {"v|a|19:30"}
    ids = _sync_jobs(scheduler, cfg, existing, "user", "pass", _card())
    scheduler.remove_job.assert_called_once_with("v|a|19:30")
    assert ids == set()


def test_sync_jobs_skips_missing_cron(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(textwrap.dedent("""\
        targets:
          - name: "No Cron"
            venue_slug: "v"
            activity_slug: "a"
            target_time: "10:00"
            enabled: true
    """))
    scheduler = MagicMock()
    ids = _sync_jobs(scheduler, cfg, set(), "user", "pass", _card())
    # job_id enters desired set but scheduler.add_job is never called (no cron)
    assert not scheduler.add_job.called
    assert ids == {"v|a|10:00"}


def test_sync_jobs_invalid_config_returns_existing(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(": invalid: yaml: [[[")
    scheduler = MagicMock()
    existing = {"old-job"}
    ids = _sync_jobs(scheduler, cfg, existing, "user", "pass", _card())
    # Returns existing unchanged on parse error
    assert ids == existing
    assert not scheduler.add_job.called


def test_sync_jobs_does_not_readd_existing(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(textwrap.dedent("""\
        targets:
          - name: "Test"
            venue_slug: "v"
            activity_slug: "a"
            target_time: "19:30"
            cron: "57 20 * * 1"
            enabled: true
    """))
    scheduler = MagicMock()
    existing = {"v|a|19:30"}  # already scheduled
    ids = _sync_jobs(scheduler, cfg, existing, "user", "pass", _card())
    assert not scheduler.add_job.called  # no new job added
    assert ids == existing


# ------------------------------------------------------------------
# _watch_job_id
# ------------------------------------------------------------------

def test_watch_job_id_format():
    target = {"name": "My Target"}
    assert _watch_job_id(target, date(2026, 8, 17)) == "watch::My Target::2026-08-17"


# ------------------------------------------------------------------
# _start_watch
# ------------------------------------------------------------------

def test_start_watch_adds_job_and_records_status(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "config.yaml"))
    scheduler = MagicMock()
    scheduler.get_job.return_value = None  # not already watching
    target = {"name": "My Target", "target_time": "19:30"}
    _start_watch(scheduler, target, date(2026, 8, 17), "user", "pass", _card())
    assert scheduler.add_job.called
    kwargs = scheduler.add_job.call_args.kwargs
    assert kwargs["id"] == "watch::My Target::2026-08-17"
    status = load_status()
    assert status["My Target"]["status"] == "watching"


def test_start_watch_is_idempotent(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "config.yaml"))
    scheduler = MagicMock()
    scheduler.get_job.return_value = MagicMock()  # already watching
    target = {"name": "My Target", "target_time": "19:30"}
    _start_watch(scheduler, target, date(2026, 8, 17), "user", "pass", _card())
    assert not scheduler.add_job.called


# ------------------------------------------------------------------
# _cleanup_stale_watches
# ------------------------------------------------------------------

def test_cleanup_stale_watches_removes_disabled_targets():
    scheduler = MagicMock()
    kept_job = MagicMock(id="watch::Still Enabled::2026-08-17")
    stale_job = MagicMock(id="watch::Now Disabled::2026-08-17")
    cron_job = MagicMock(id="venue|activity|19:30")
    scheduler.get_jobs.return_value = [kept_job, stale_job, cron_job]
    _cleanup_stale_watches(scheduler, {"Still Enabled"})
    scheduler.remove_job.assert_called_once_with("watch::Now Disabled::2026-08-17")


def test_cleanup_stale_watches_noop_when_all_enabled():
    scheduler = MagicMock()
    scheduler.get_jobs.return_value = [MagicMock(id="watch::Still Enabled::2026-08-17")]
    _cleanup_stale_watches(scheduler, {"Still Enabled"})
    assert not scheduler.remove_job.called


# ------------------------------------------------------------------
# _resume_watch_if_pending
# ------------------------------------------------------------------

def test_resume_watch_if_pending_restarts_active_watch(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "config.yaml"))
    future_date = date.today() + timedelta(days=3)
    record_status("My Target", "watching", future_date, "19:30")
    scheduler = MagicMock()
    scheduler.get_job.return_value = None
    target = {"name": "My Target", "target_time": "19:30"}
    _resume_watch_if_pending(scheduler, target, "user", "pass", _card())
    assert scheduler.add_job.called


def test_resume_watch_if_pending_skips_when_not_watching(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "config.yaml"))
    scheduler = MagicMock()
    target = {"name": "My Target", "target_time": "19:30"}
    _resume_watch_if_pending(scheduler, target, "user", "pass", _card())
    assert not scheduler.add_job.called


def test_resume_watch_if_pending_skips_when_already_secured(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "config.yaml"))
    future_date = date.today() + timedelta(days=3)
    record_status("My Target", "booked", future_date, "19:30")
    scheduler = MagicMock()
    target = {"name": "My Target", "target_time": "19:30"}
    _resume_watch_if_pending(scheduler, target, "user", "pass", _card())
    assert not scheduler.add_job.called


def test_resume_watch_if_pending_skips_when_expired(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "config.yaml"))
    past_date = date.today() - timedelta(days=1)
    record_status("My Target", "watching", past_date, "19:30")
    scheduler = MagicMock()
    target = {"name": "My Target", "target_time": "19:30"}
    _resume_watch_if_pending(scheduler, target, "user", "pass", _card())
    assert not scheduler.add_job.called


# ------------------------------------------------------------------
# _run_and_maybe_watch
# ------------------------------------------------------------------

def test_run_and_maybe_watch_starts_watch_on_miss(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "config.yaml"))
    target = {"name": "My Target", "target_time": "19:30", "days_ahead": 7}
    session_date = date.today() + timedelta(days=7)

    def fake_run_target(t, u, p, c):
        record_status("My Target", "no_slot", session_date, "19:30")

    scheduler = MagicMock()
    scheduler.get_job.return_value = None
    with patch("better_bot.daemon.run_target", side_effect=fake_run_target):
        _run_and_maybe_watch(scheduler, target, "user", "pass", _card())
    assert scheduler.add_job.called


def test_run_and_maybe_watch_no_watch_when_booked(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "config.yaml"))
    target = {"name": "My Target", "target_time": "19:30", "days_ahead": 7}
    session_date = date.today() + timedelta(days=7)

    def fake_run_target(t, u, p, c):
        record_status("My Target", "booked", session_date, "19:30", detail="ref-1")

    scheduler = MagicMock()
    with patch("better_bot.daemon.run_target", side_effect=fake_run_target):
        _run_and_maybe_watch(scheduler, target, "user", "pass", _card())
    assert not scheduler.add_job.called
