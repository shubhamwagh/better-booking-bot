"""Tests for daemon scheduler helpers."""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest

from better_bot.checkout import CardDetails
from better_bot.daemon import _job_id, _sync_jobs


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
