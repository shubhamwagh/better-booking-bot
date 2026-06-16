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
from pathlib import Path

import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

from better_bot.bot import run_target
from better_bot.checkout import CardDetails

log = logging.getLogger(__name__)

CONFIG_POLL_S = 30


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description="Better booking bot - daemon scheduler")
    p.add_argument("--config", default=None)
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )

    load_dotenv()

    username = os.environ["BETTER_USERNAME"]
    password = os.environ["BETTER_PASSWORD"]
    cvv = os.environ.get("CARD_CVV") or ""
    card = CardDetails(
        cvv=cvv,
        number=os.getenv("CARD_NUMBER"),
        expiry=os.getenv("CARD_EXPIRY"),
        first_name=os.getenv("BILLING_FIRST_NAME"),
        last_name=os.getenv("BILLING_LAST_NAME"),
        address1=os.getenv("BILLING_ADDRESS1"),
        address2=os.getenv("BILLING_ADDRESS2"),
        city=os.getenv("BILLING_CITY"),
        postcode=os.getenv("BILLING_POSTCODE"),
        save_card=os.getenv("SAVE_CARD", "false").lower() in ("1", "true", "yes"),
    )

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
                    scheduler, config_path, current_job_ids,
                    username, password, card,
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

    for target in targets:
        if not target.get("enabled", True):
            continue

        job_id = _job_id(target)
        desired_ids.add(job_id)

        if job_id not in existing_ids:
            _add_job(scheduler, job_id, target, username, password, card)

    for old_id in existing_ids - desired_ids:
        try:
            scheduler.remove_job(old_id)
            log.info("Removed job: %s", old_id)
        except Exception:
            pass

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
        trigger = CronTrigger.from_crontab(cron, timezone="Europe/London")
    except Exception as exc:
        log.error("Invalid cron '%s' for target '%s': %s", cron, target["name"], exc)
        return

    scheduler.add_job(
        func=run_target,
        trigger=trigger,
        id=job_id,
        name=target["name"],
        args=[target, username, password, card],
        replace_existing=True,
        misfire_grace_time=120,
    )
    log.info("Scheduled '%s'  cron='%s'", target["name"], cron)


def _job_id(target: dict) -> str:
    return f"{target['venue_slug']}|{target['activity_slug']}|{target['target_time']}"


if __name__ == "__main__":
    main()
