"""Web UI - edit config.yaml targets from a browser.

Runs alongside the daemon (which polls config.yaml for changes), so edits
made here take effect within CONFIG_POLL_S seconds without a restart.

Usage:
    uv run -m better_bot.webui
    uv run -m better_bot.webui --config /path/to/config.yaml
"""

from __future__ import annotations

import argparse
import html
import logging
import os
import re
from pathlib import Path
from urllib.parse import quote

import yaml
from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.status import HTTP_303_SEE_OTHER

log = logging.getLogger(__name__)

app = FastAPI(title="Better Booking Bot - Config")

TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

# cron day-of-week: Mon=1 .. Sat=6, Sun=0 (matches config.yaml's existing entries)
WEEKDAYS = [
    ("1", "Monday"), ("2", "Tuesday"), ("3", "Wednesday"), ("4", "Thursday"),
    ("5", "Friday"), ("6", "Saturday"), ("0", "Sunday"),
]

PREWARM_MINUTES = 3  # fire this many minutes before release_hour, matching existing targets


def _config_path() -> Path:
    return Path(os.getenv("CONFIG_PATH", "config.yaml"))


def load_config() -> dict:
    path = _config_path()
    if not path.exists():
        return {"targets": []}
    with path.open() as f:
        return yaml.safe_load(f) or {"targets": []}


def save_config(data: dict) -> None:
    path = _config_path()
    with path.open("w") as f:
        yaml.safe_dump(data, f, sort_keys=False)


def build_cron(weekday: str, release_hour: int) -> str:
    minute = 60 - PREWARM_MINUTES
    hour = (release_hour - 1) % 24
    return f"{minute} {hour} * * {weekday}"


# ------------------------------------------------------------------
# HTML rendering
# ------------------------------------------------------------------

def _page(body: str) -> str:
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Better Booking Bot</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 780px; margin: 2rem auto; padding: 0 1rem; }}
table {{ width: 100%; border-collapse: collapse; margin-bottom: 2rem; }}
th, td {{ text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid #ddd; }}
form.inline {{ display: inline; }}
fieldset {{ border: 1px solid #ccc; border-radius: 6px; }}
label {{ display: block; margin-top: 0.6rem; font-size: 0.9rem; }}
input, select {{ width: 100%; padding: 0.3rem; box-sizing: border-box; }}
button {{ margin-top: 1rem; padding: 0.4rem 1rem; }}
.disabled {{ color: #999; }}
.err {{ color: #b00; }}
</style></head>
<body>
<h1>Better Booking Bot - targets</h1>
{body}
</body></html>"""


def _targets_table(targets: list[dict]) -> str:
    if not targets:
        return "<p>No targets configured yet.</p>"
    rows = []
    for t in targets:
        name = html.escape(t["name"])
        url_name = quote(t["name"], safe="")
        state_cls = "" if t.get("enabled", True) else "disabled"
        rows.append(f"""<tr class="{state_cls}">
<td>{name}</td>
<td>{html.escape(t['venue_slug'])}</td>
<td>{html.escape(t['activity_slug'])}</td>
<td>{html.escape(t['target_time'])}</td>
<td>{t.get('days_ahead', 7)}</td>
<td>{t.get('release_hour', 21)}</td>
<td>{'yes' if t.get('enabled', True) else 'no'}</td>
<td>
<form class="inline" method="post" action="/targets/{url_name}/toggle"><button>{'disable' if t.get('enabled', True) else 'enable'}</button></form>
<form class="inline" method="post" action="/targets/{url_name}/delete" onsubmit="return confirm('Delete {name}?')"><button>delete</button></form>
</td>
</tr>""")
    return f"""<table>
<tr><th>name</th><th>venue</th><th>activity</th><th>time</th><th>days ahead</th><th>release hr</th><th>enabled</th><th></th></tr>
{''.join(rows)}
</table>"""


def _add_form(error: str | None = None) -> str:
    weekday_options = "".join(f'<option value="{v}">{label}</option>' for v, label in WEEKDAYS)
    err = f'<p class="err">{html.escape(error)}</p>' if error else ""
    return f"""<fieldset>
<legend>Add target</legend>
{err}
<form method="post" action="/targets">
<label>Name<input name="name" required></label>
<label>Venue slug<input name="venue_slug" required></label>
<label>Activity slug<input name="activity_slug" required></label>
<label>Session day<select name="weekday">{weekday_options}</select></label>
<label>Session time (24h HH:MM)<input name="target_time" placeholder="19:30" required></label>
<label>Days ahead slot opens<input name="days_ahead" type="number" value="7" required></label>
<label>Release hour (local, 0-23)<input name="release_hour" type="number" value="21" min="0" max="23" required></label>
<button type="submit">Add</button>
</form>
</fieldset>"""


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    config = load_config()
    return _page(_targets_table(config.get("targets", [])) + _add_form())


@app.post("/targets")
def add_target(
    name: str = Form(...),
    venue_slug: str = Form(...),
    activity_slug: str = Form(...),
    weekday: str = Form(...),
    target_time: str = Form(...),
    days_ahead: int = Form(...),
    release_hour: int = Form(...),
):
    config = load_config()
    targets = config.setdefault("targets", [])

    if not TIME_RE.match(target_time):
        return HTMLResponse(_page(_targets_table(targets) + _add_form("target_time must be HH:MM, 24h")))
    if any(t["name"] == name for t in targets):
        return HTMLResponse(_page(_targets_table(targets) + _add_form(f'A target named "{name}" already exists.')))
    if not (0 <= release_hour <= 23):
        return HTMLResponse(_page(_targets_table(targets) + _add_form("release_hour must be 0-23")))

    targets.append({
        "name": name,
        "venue_slug": venue_slug,
        "activity_slug": activity_slug,
        "target_time": target_time,
        "days_ahead": days_ahead,
        "release_hour": release_hour,
        "cron": build_cron(weekday, release_hour),
        "enabled": True,
    })
    save_config(config)
    return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)


@app.post("/targets/{name}/toggle")
def toggle_target(name: str) -> RedirectResponse:
    config = load_config()
    for t in config.get("targets", []):
        if t["name"] == name:
            t["enabled"] = not t.get("enabled", True)
            break
    save_config(config)
    return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)


@app.post("/targets/{name}/delete")
def delete_target(name: str) -> RedirectResponse:
    config = load_config()
    config["targets"] = [t for t in config.get("targets", []) if t["name"] != name]
    save_config(config)
    return RedirectResponse("/", status_code=HTTP_303_SEE_OTHER)


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

def main() -> None:
    import uvicorn

    p = argparse.ArgumentParser(description="Better booking bot - config web UI")
    p.add_argument("--config", default=None)
    args = p.parse_args()

    if args.config:
        os.environ["CONFIG_PATH"] = args.config

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))


if __name__ == "__main__":
    main()
