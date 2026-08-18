"""Daemon mode - long-running scheduler.

Reads config.yaml and schedules all enabled targets using their cron fields.
Watches config.yaml every CONFIG_POLL_S seconds; adds/removes jobs live
when targets are added, removed, or toggled without restarting.

Usage:
    uv run -m better_bot.daemon
    uv run -m better_bot.daemon --config /path/to/config.yaml
"""

from __future__ import annotations

import argparse
import logging
import os
import time
from datetime import date, datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from better_bot.bot import (
    VENUE_TZ,
    already_secured,
    load_status,
    record_status,
    run_target,
    venue_now,
    venue_today,
    watch_and_book,
)
from better_bot.checkout import CardDetails
from better_bot.settings import Settings

log = logging.getLogger(__name__)

CONFIG_POLL_S = 30
WATCH_INTERVAL_MINUTES = 3  # cancellation-watch poll cadence - gentle, not a release-time race


def log_path() -> Path:
    """Where the daemon's log file lives, so the web UI's Logs tab can tail it.

    Defaults next to config.yaml (same shared-volume convention as status.json);
    override with LOG_PATH if the two need to live in different places.
    """
    override = os.getenv("LOG_PATH")
    if override:
        return Path(override)
    config_path = Path(os.getenv("CONFIG_PATH", "config.yaml"))
    return config_path.parent / "logs" / "daemon.log"


def _configure_logging(verbose: bool) -> None:
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        handlers=[
            logging.StreamHandler(),
            RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3),
        ],
    )


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------


def main() -> None:
    p = argparse.ArgumentParser(description="Better booking bot - daemon scheduler")
    p.add_argument("--config", default=None)
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    _configure_logging(args.verbose)

    settings = Settings()
    username = settings.better_username
    password = settings.better_password
    card = settings.to_card()

    config_path = Path(args.config or os.getenv("CONFIG_PATH", "config.yaml"))

    scheduler = BackgroundScheduler(timezone="Europe/London")
    scheduler.start()
    log.info("Scheduler started (timezone=Europe/London)")

    last_mtime: float = 0.0
    current_job_ids: set[str] = set()

    try:
        while True:
            mtime = config_path.stat().st_mtime
            if mtime != last_mtime:
                log.info("Config changed - reloading %s", config_path)
                current_job_ids = _sync_jobs(
                    scheduler,
                    config_path,
                    current_job_ids,
                    username,
                    password,
                    card,
                )
                last_mtime = mtime
            time.sleep(CONFIG_POLL_S)
    except KeyboardInterrupt:
        log.info("Shutting down...")
    finally:
        scheduler.shutdown()


# ------------------------------------------------------------------
# Job sync
# ------------------------------------------------------------------


def _sync_jobs(
    scheduler: BackgroundScheduler,
    config_path: Path,
    existing_ids: set[str],
    username: str,
    password: str,
    card: CardDetails,
) -> set[str]:
    try:
        with config_path.open() as f:
            data = yaml.safe_load(f)
        targets = data.get("targets", [])
    except Exception as exc:
        log.error("Failed to parse config: %s", exc)
        return existing_ids

    desired_ids: set[str] = set()
    enabled_names: set[str] = set()

    for target in targets:
        if not target.get("enabled", True):
            continue

        enabled_names.add(target["name"])
        job_id = _job_id(target)
        desired_ids.add(job_id)

        if job_id not in existing_ids:
            _add_job(scheduler, job_id, target, username, password, card)

        _resume_watch_if_pending(scheduler, target, username, password, card)

    for old_id in existing_ids - desired_ids:
        try:
            scheduler.remove_job(old_id)
            log.info("Removed job: %s", old_id)
        except Exception:
            pass

    _cleanup_stale_watches(scheduler, enabled_names)

    return desired_ids


def _add_job(
    scheduler: BackgroundScheduler,
    job_id: str,
    target: dict,
    username: str,
    password: str,
    card: CardDetails,
) -> None:
    cron = target.get("cron")
    if not cron:
        log.warning("Target '%s' has no cron field - skipping", target["name"])
        return

    try:
        minute, hour, day, month, dow = cron.split()
        trigger = CronTrigger(
            minute=minute,
            hour=hour,
            day=day,
            month=month,
            day_of_week=_crontab_dow_to_apscheduler(dow),
            timezone="Europe/London",
        )
    except Exception as exc:
        log.error("Invalid cron '%s' for target '%s': %s", cron, target["name"], exc)
        return

    scheduler.add_job(
        func=_run_and_maybe_watch,
        trigger=trigger,
        id=job_id,
        name=target["name"],
        args=[scheduler, target, username, password, card],
        replace_existing=True,
        misfire_grace_time=120,
    )
    log.info("Scheduled '%s'  cron='%s'", target["name"], cron)


# ------------------------------------------------------------------
# Cancellation watch - if the scheduled burst misses, keep polling
# gently in the background for someone else's cancellation.
# ------------------------------------------------------------------


def _run_and_maybe_watch(
    scheduler: BackgroundScheduler,
    target: dict,
    username: str,
    password: str,
    card: CardDetails,
) -> None:
    # A failed run is exactly when the cancellation watch matters most, so the
    # exception must not escape before the watch below gets armed. run_target
    # has already recorded "failed" in status.json by this point.
    try:
        run_target(target, username, password, card)
    except Exception as exc:
        log.error("Target '%s' failed: %s", target["name"], exc)

    session_date = venue_today() + timedelta(days=int(target.get("days_ahead", 7)))
    entry = load_status().get(target["name"], {})
    if entry.get("session_date") == session_date.isoformat() and entry.get("status") in ("no_slot", "failed"):
        _start_watch(scheduler, target, session_date, username, password, card)


def _watch_job_id(target: dict, session_date: date) -> str:
    return f"watch::{target['name']}::{session_date.isoformat()}"


def _start_watch(
    scheduler: BackgroundScheduler,
    target: dict,
    session_date: date,
    username: str,
    password: str,
    card: CardDetails,
) -> None:
    job_id = _watch_job_id(target, session_date)
    if scheduler.get_job(job_id):
        return  # already watching this one

    def _poll() -> None:
        if watch_and_book(target, session_date, username, password, card):
            try:
                scheduler.remove_job(job_id)
            except Exception:
                pass
            log.info("Cancellation watch ended for '%s' (%s)", target["name"], session_date)

    record_status(target["name"], "watching", session_date, target["target_time"], detail="cancellation watch active")
    scheduler.add_job(
        func=_poll,
        trigger=IntervalTrigger(minutes=WATCH_INTERVAL_MINUTES),
        id=job_id,
        name=f"watch:{target['name']}",
        replace_existing=True,
        misfire_grace_time=120,
    )
    log.info(
        "Started cancellation watch for '%s' -> %s %s (every %sm)",
        target["name"],
        session_date,
        target["target_time"],
        WATCH_INTERVAL_MINUTES,
    )


def _resume_watch_if_pending(
    scheduler: BackgroundScheduler,
    target: dict,
    username: str,
    password: str,
    card: CardDetails,
) -> None:
    """Re-attach a watch job after a daemon restart wipes the in-memory scheduler.

    Covers both a watch already in progress ("watching") and a run that failed
    or found nothing but never got to arm a watch, e.g. because the daemon
    crashed or was redeployed between the failed run and the watch-arm step -
    exactly what happened for a target that hit the release-time race before
    this fix existed.
    """
    entry = load_status().get(target["name"])
    if not entry or entry.get("status") not in ("watching", "failed", "no_slot"):
        return
    try:
        session_date = date.fromisoformat(entry["session_date"])
    except (KeyError, ValueError):
        return
    if already_secured(target["name"], session_date):
        return
    session_start = datetime.combine(
        session_date, datetime.strptime(target["target_time"], "%H:%M").time(), tzinfo=VENUE_TZ
    )
    if venue_now() >= session_start:
        return
    _start_watch(scheduler, target, session_date, username, password, card)


def _cleanup_stale_watches(scheduler: BackgroundScheduler, enabled_names: set[str]) -> None:
    """Stop watching for any target that's since been disabled or deleted."""
    for job in scheduler.get_jobs():
        if not job.id.startswith("watch::"):
            continue
        target_name = job.id.split("::", 2)[1]
        if target_name not in enabled_names:
            try:
                scheduler.remove_job(job.id)
                log.info("Stopped cancellation watch for disabled/removed target '%s'", target_name)
            except Exception:
                pass


def _crontab_dow_to_apscheduler(dow: str) -> str:
    """Convert standard crontab day-of-week (0/7=Sun..6=Sat) to APScheduler's
    day_of_week convention (0=Mon..6=Sun). Named/wildcard tokens pass through
    unchanged since APScheduler's own names already match its convention.
    """

    def convert(token: str) -> str:
        base, _, step = token.partition("/")
        if "-" in base:
            start, end = base.split("-")
            if start.isdigit() and end.isdigit():
                base = f"{(int(start) - 1) % 7}-{(int(end) - 1) % 7}"
        elif base.isdigit():
            base = str((int(base) - 1) % 7)
        return f"{base}/{step}" if step else base

    return ",".join(convert(t) for t in dow.split(","))


def _job_id(target: dict) -> str:
    return f"{target['venue_slug']}|{target['activity_slug']}|{target['target_time']}"


if __name__ == "__main__":
    main()
