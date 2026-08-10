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


def known_venue_activity_pairs(targets: list[dict]) -> list[tuple[str, str]]:
    seen: dict[tuple[str, str], None] = {}
    for t in targets:
        seen.setdefault((t["venue_slug"], t["activity_slug"]), None)
    return sorted(seen)


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
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root {{
  --bg: #f5f6f8; --card: #ffffff; --text: #1c1e21; --muted: #6b7280;
  --border: #e5e7eb; --accent: #2563eb; --accent-hover: #1d4ed8;
  --danger: #dc2626; --danger-hover: #b91c1c;
  --ok-bg: #dcfce7; --ok-text: #166534; --off-bg: #f3f4f6; --off-text: #6b7280;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --bg: #15171a; --card: #1f2226; --text: #e8e9eb; --muted: #9aa0a8;
    --border: #30343a; --accent: #3b82f6; --accent-hover: #60a5fa;
    --danger: #ef4444; --danger-hover: #f87171;
    --ok-bg: #14321f; --ok-text: #4ade80; --off-bg: #2a2d32; --off-text: #9aa0a8;
  }}
}}
* {{ box-sizing: border-box; }}
body {{
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  background: var(--bg); color: var(--text);
  max-width: 860px; margin: 0 auto; padding: 2.5rem 1.25rem 4rem;
}}
h1 {{ font-size: 1.4rem; margin-bottom: 0.25rem; }}
.subtitle {{ color: var(--muted); font-size: 0.9rem; margin-bottom: 1.75rem; }}
.card {{
  background: var(--card); border: 1px solid var(--border); border-radius: 12px;
  padding: 1.25rem 1.5rem; margin-bottom: 1.75rem; box-shadow: 0 1px 2px rgba(0,0,0,0.04);
}}
.card h2 {{ font-size: 1rem; margin: 0 0 1rem; }}
table {{ width: 100%; border-collapse: collapse; font-size: 0.9rem; }}
th {{ text-align: left; padding: 0.5rem 0.6rem; color: var(--muted); font-weight: 600; font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.03em; border-bottom: 1px solid var(--border); }}
td {{ padding: 0.6rem 0.6rem; border-bottom: 1px solid var(--border); vertical-align: middle; }}
tr:last-child td {{ border-bottom: none; }}
tr.row-disabled td {{ color: var(--muted); }}
.slug {{ font-family: ui-monospace, monospace; font-size: 0.82rem; color: var(--muted); }}
.badge {{ display: inline-block; padding: 0.15rem 0.55rem; border-radius: 999px; font-size: 0.75rem; font-weight: 600; }}
.badge-on {{ background: var(--ok-bg); color: var(--ok-text); }}
.badge-off {{ background: var(--off-bg); color: var(--off-text); }}
form.inline {{ display: inline-block; margin-right: 0.4rem; }}
.actions {{ white-space: nowrap; }}
.grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 0.9rem 1.2rem; }}
.grid .full {{ grid-column: 1 / -1; }}
label {{ display: block; font-size: 0.82rem; color: var(--muted); margin-bottom: 0.3rem; }}
input, select {{
  width: 100%; padding: 0.5rem 0.6rem; font-size: 0.92rem; border-radius: 8px;
  border: 1px solid var(--border); background: var(--bg); color: var(--text);
}}
input:focus, select:focus {{ outline: none; border-color: var(--accent); }}
button {{
  padding: 0.45rem 0.9rem; font-size: 0.85rem; font-weight: 600; border-radius: 8px;
  border: 1px solid var(--border); background: var(--card); color: var(--text); cursor: pointer;
}}
button:hover {{ border-color: var(--accent); }}
button.primary {{ margin-top: 1.1rem; background: var(--accent); color: #fff; border-color: var(--accent); }}
button.primary:hover {{ background: var(--accent-hover); }}
button.danger:hover {{ background: var(--danger); color: #fff; border-color: var(--danger); }}
.empty {{ color: var(--muted); font-size: 0.9rem; }}
.err {{ color: var(--danger); font-size: 0.85rem; margin-bottom: 0.75rem; }}
</style></head>
<body>
<h1>Better Booking Bot</h1>
<div class="subtitle">Manage what to auto-book next week.</div>
{body}
</body></html>"""


def _targets_table(targets: list[dict]) -> str:
    if not targets:
        return '<div class="card"><h2>Targets</h2><p class="empty">No targets configured yet - add one below.</p></div>'
    rows = []
    for t in targets:
        name = html.escape(t["name"])
        url_name = quote(t["name"], safe="")
        enabled = t.get("enabled", True)
        row_cls = "" if enabled else "row-disabled"
        badge = '<span class="badge badge-on">enabled</span>' if enabled else '<span class="badge badge-off">disabled</span>'
        rows.append(f"""<tr class="{row_cls}">
<td>{name}</td>
<td class="slug">{html.escape(t['venue_slug'])} / {html.escape(t['activity_slug'])}</td>
<td>{html.escape(t['target_time'])}</td>
<td>{t.get('days_ahead', 7)}d / {t.get('release_hour', 21)}:00</td>
<td>{badge}</td>
<td class="actions">
<form class="inline" method="post" action="/targets/{url_name}/toggle"><button>{'disable' if enabled else 'enable'}</button></form>
<form class="inline" method="post" action="/targets/{url_name}/delete" onsubmit="return confirm('Delete {name}?')"><button class="danger">delete</button></form>
</td>
</tr>""")
    return f"""<div class="card">
<h2>Targets</h2>
<table>
<tr><th>name</th><th>venue / activity</th><th>time</th><th>opens</th><th>status</th><th></th></tr>
{''.join(rows)}
</table>
</div>"""


def _add_form(targets: list[dict], error: str | None = None) -> str:
    weekday_options = "".join(f'<option value="{v}">{label}</option>' for v, label in WEEKDAYS)
    pairs = known_venue_activity_pairs(targets)
    pair_options = "".join(
        f'<option value="{html.escape(v)}|{html.escape(a)}">{html.escape(v)} / {html.escape(a)}</option>'
        for v, a in pairs
    )
    err = f'<p class="err">{html.escape(error)}</p>' if error else ""
    return f"""<div class="card">
<h2>Add target</h2>
{err}
<form method="post" action="/targets">
<div class="grid">
<div class="full"><label>Name</label><input name="name" required></div>
<div class="full">
<label>Venue / activity</label>
<select id="known_pair" onchange="onPairChange()">
{pair_options}
<option value="__custom__">+ New venue / activity...</option>
</select>
</div>
<div id="custom_slugs" class="full grid" style="display: none; padding: 0;">
<div><label>Venue slug</label><input id="venue_slug_input" placeholder="white-horse-leisure-and-tennis-centre"></div>
<div><label>Activity slug</label><input id="activity_slug_input" placeholder="pickleball-drop-in"></div>
</div>
<input type="hidden" id="venue_slug" name="venue_slug">
<input type="hidden" id="activity_slug" name="activity_slug">
<div><label>Session day</label><select name="weekday">{weekday_options}</select></div>
<div><label>Session time (24h)</label><input name="target_time" placeholder="19:30" pattern="[0-2][0-9]:[0-5][0-9]" required></div>
<div><label>Days ahead slot opens</label><input name="days_ahead" type="number" value="7" required></div>
<div><label>Release hour (local, 0-23)</label><input name="release_hour" type="number" value="21" min="0" max="23" required></div>
</div>
<button type="submit" class="primary">Add target</button>
</form>
</div>
<script>
function onPairChange() {{
  var sel = document.getElementById('known_pair');
  var custom = document.getElementById('custom_slugs');
  var venueHidden = document.getElementById('venue_slug');
  var activityHidden = document.getElementById('activity_slug');
  var venueInput = document.getElementById('venue_slug_input');
  var activityInput = document.getElementById('activity_slug_input');
  if (sel.value === '__custom__') {{
    custom.style.display = 'grid';
    venueHidden.value = '';
    activityHidden.value = '';
  }} else {{
    custom.style.display = 'none';
    var parts = sel.value.split('|');
    venueHidden.value = parts[0] || '';
    activityHidden.value = parts[1] || '';
  }}
}}
document.getElementById('venue_slug_input') && document.getElementById('venue_slug_input').addEventListener('input', function() {{
  document.getElementById('venue_slug').value = this.value;
}});
document.getElementById('activity_slug_input') && document.getElementById('activity_slug_input').addEventListener('input', function() {{
  document.getElementById('activity_slug').value = this.value;
}});
onPairChange();
</script>"""


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    targets = load_config().get("targets", [])
    return _page(_targets_table(targets) + _add_form(targets))


@app.post("/targets")
def add_target(
    name: str = Form(...),
    venue_slug: str = Form(""),
    activity_slug: str = Form(""),
    weekday: str = Form(...),
    target_time: str = Form(...),
    days_ahead: int = Form(...),
    release_hour: int = Form(...),
):
    config = load_config()
    targets = config.setdefault("targets", [])

    def error(msg: str) -> HTMLResponse:
        return HTMLResponse(_page(_targets_table(targets) + _add_form(targets, msg)))

    if not venue_slug.strip() or not activity_slug.strip():
        return error("Pick a venue/activity, or fill in the new-venue fields.")
    if not TIME_RE.match(target_time):
        return error("target_time must be HH:MM, 24h")
    if any(t["name"] == name for t in targets):
        return error(f'A target named "{name}" already exists.')
    if not (0 <= release_hour <= 23):
        return error("release_hour must be 0-23")

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
