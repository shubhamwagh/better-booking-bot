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
import json
import logging
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import yaml
from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.status import HTTP_303_SEE_OTHER

from better_bot.api import BetterAPI, BetterAPIError

log = logging.getLogger(__name__)

app = FastAPI(title="Better Booking Bot - Config")

TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

# cron day-of-week: Mon=1 .. Sat=6, Sun=0 (matches config.yaml's existing entries)
WEEKDAYS = [
    ("1", "Monday"), ("2", "Tuesday"), ("3", "Wednesday"), ("4", "Thursday"),
    ("5", "Friday"), ("6", "Saturday"), ("0", "Sunday"),
]

PREWARM_MINUTES = 3  # fire this many minutes before release_hour, matching existing targets


def _svg(path: str, size: int = 18) -> str:
    return (
        f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        f'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">{path}</svg>'
    )


ICON_LOGO = _svg('<rect x="3" y="4" width="18" height="18" rx="3"/><path d="M16 2v4M8 2v4M3 10h18"/><path d="m9 16 2 2 4-4"/>', 26)
ICON_LIST = _svg('<path d="M8 6h13M8 12h13M8 18h13"/><path d="M3 6h.01M3 12h.01M3 18h.01"/>', 16)
ICON_PLUS = _svg('<path d="M12 5v14M5 12h14"/>', 16)
ICON_POWER = _svg('<path d="M12 2v10"/><path d="M18.4 6.6a9 9 0 1 1-12.77.04"/>', 15)
ICON_TRASH = _svg('<path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2m3 0-1 14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2L4 6"/>', 15)
ICON_EMPTY = _svg('<rect x="3" y="4" width="18" height="18" rx="3"/><path d="M16 2v4M8 2v4M3 10h18"/><path d="M8 15h.01M12 15h.01M16 15h.01"/>', 40)
ICON_CHECK = _svg('<circle cx="12" cy="12" r="9"/><path d="m9 12 2 2 4-4"/>', 15)
ICON_DASH = _svg('<circle cx="12" cy="12" r="9"/><path d="M8 12h8"/>', 15)
ICON_X = _svg('<circle cx="12" cy="12" r="9"/><path d="m9.5 9.5 5 5m0-5-5 5"/>', 15)
ICON_EYE = _svg('<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>', 15)

STATUS_ICONS = {"booked": ICON_CHECK, "booked_manually": ICON_CHECK, "watching": ICON_EYE, "no_slot": ICON_DASH, "failed": ICON_X}
STATUS_LABELS = {
    "booked": "booked", "booked_manually": "booked (manual)", "watching": "watching for cancellation",
    "no_slot": "no slot found", "failed": "failed",
}
SECURED_STATUSES = {"booked", "booked_manually"}

_api = BetterAPI()  # unauthenticated: only used for the public venue/activity/times lookups below

_CACHE_TTL_S = 3600
_venues_cache: tuple[float, list[dict]] | None = None
_activities_cache: dict[str, tuple[float, list[dict]]] = {}


def _cached_venues() -> list[dict]:
    global _venues_cache
    if _venues_cache is None or time.monotonic() - _venues_cache[0] > _CACHE_TTL_S:
        venues = sorted(
            ({"slug": v.slug, "name": v.name, "town": v.town} for v in _api.list_venues()),
            key=lambda v: (v["town"].lower(), v["name"].lower()),
        )
        _venues_cache = (time.monotonic(), venues)
    return _venues_cache[1]


def _cached_activities(venue_slug: str) -> list[dict]:
    cached = _activities_cache.get(venue_slug)
    if cached is None or time.monotonic() - cached[0] > _CACHE_TTL_S:
        activities = sorted(
            ({"slug": a.slug, "name": a.name, "category": a.category} for a in _api.list_activities(venue_slug)),
            key=lambda a: (a["category"], a["name"]),
        )
        _activities_cache[venue_slug] = (time.monotonic(), activities)
    return _activities_cache[venue_slug][1]


def _next_dates_for_weekday(cron_weekday: str, count: int = 4) -> list[date]:
    """Upcoming dates (today + weekly steps) matching a cron day-of-week (0=Sun..6=Sat)."""
    python_weekday = (int(cron_weekday) + 6) % 7  # cron dow -> Mon=0..Sun=6
    today = date.today()
    first = today + timedelta(days=(python_weekday - today.weekday()) % 7)
    return [first + timedelta(weeks=i) for i in range(count)]


def _typical_times(venue_slug: str, activity_slug: str, cron_weekday: str) -> list[str]:
    """Distinct session start times seen on the next few occurrences of this weekday."""
    for candidate_date in _next_dates_for_weekday(cron_weekday):
        try:
            slots = _api.get_slots(venue_slug, activity_slug, candidate_date)
        except BetterAPIError:
            continue
        if slots:
            return sorted({s.starts_at for s in slots})
    return []


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


def _status_path() -> Path:
    return _config_path().parent / "status.json"


def load_status() -> dict:
    path = _status_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def save_status(data: dict) -> None:
    _status_path().write_text(json.dumps(data, indent=2))


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
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Crect width='24' height='24' rx='6' fill='%232563eb'/%3E%3Cg fill='none' stroke='white' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Crect x='4' y='5' width='16' height='16' rx='2'/%3E%3Cpath d='M15 3v4M9 3v4M4 10h16'/%3E%3Cpath d='m9 15 2 2 4-4'/%3E%3C/g%3E%3C/svg%3E">
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
html {{ -webkit-text-size-adjust: 100%; }}
body {{
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  background: var(--bg); color: var(--text);
  max-width: 860px; margin: 0 auto; padding: 2.5rem 1.25rem 4rem;
}}
.table-wrap {{ overflow-x: auto; -webkit-overflow-scrolling: touch; margin: 0 -0.25rem; padding: 0 0.25rem; }}
@media (max-width: 640px) {{
  body {{ padding: 1.5rem 1rem 3rem; }}
  .card {{ padding: 1rem; }}
  .grid {{ grid-template-columns: 1fr; }}
  table {{ font-size: 0.85rem; }}
  th, td {{ padding: 0.5rem 0.4rem; }}
  input, select, button {{ font-size: 1rem; }}
  .actions {{ display: flex; flex-direction: column; gap: 0.4rem; align-items: flex-start; }}
  form.inline {{ margin-right: 0; width: 100%; }}
  form.inline button {{ width: 100%; justify-content: center; }}
}}
h1 {{ font-size: 1.4rem; margin: 0 0 0.25rem; display: flex; align-items: center; gap: 0.5rem; }}
h1 .logo {{ color: var(--accent); display: inline-flex; }}
.subtitle {{ color: var(--muted); font-size: 0.9rem; margin-bottom: 1.75rem; }}
.card {{
  background: var(--card); border: 1px solid var(--border); border-radius: 12px;
  padding: 1.25rem 1.5rem; margin-bottom: 1.75rem; box-shadow: 0 1px 2px rgba(0,0,0,0.04);
}}
.card h2 {{ font-size: 1rem; margin: 0 0 1rem; display: flex; align-items: center; gap: 0.45rem; color: var(--text); }}
.card h2 svg {{ color: var(--muted); }}
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
select:disabled {{ opacity: 0.55; cursor: not-allowed; }}
button {{
  padding: 0.45rem 0.9rem; font-size: 0.85rem; font-weight: 600; border-radius: 8px;
  border: 1px solid var(--border); background: var(--card); color: var(--text); cursor: pointer;
  display: inline-flex; align-items: center; gap: 0.35rem; line-height: 1;
}}
button:hover {{ border-color: var(--accent); }}
button.primary {{ margin-top: 1.1rem; background: var(--accent); color: #fff; border-color: var(--accent); }}
button.primary:hover {{ background: var(--accent-hover); }}
button.danger:hover {{ background: var(--danger); color: #fff; border-color: var(--danger); }}
.badge {{ display: inline-flex; align-items: center; gap: 0.35rem; }}
.badge::before {{ content: ""; width: 6px; height: 6px; border-radius: 50%; background: currentColor; }}
.empty {{ color: var(--muted); font-size: 0.9rem; text-align: center; padding: 1.5rem 0; }}
.empty svg {{ display: block; margin: 0 auto 0.75rem; color: var(--border); }}
.err {{ color: var(--danger); font-size: 0.85rem; margin-bottom: 0.75rem; }}
.nav {{ display: flex; gap: 1rem; margin-bottom: 1.75rem; font-size: 0.85rem; }}
.nav a {{ color: var(--muted); text-decoration: none; }}
.nav a:hover, .nav a.active {{ color: var(--accent); }}
.history-status {{ display: inline-flex; align-items: center; gap: 0.4rem; }}
.history-status svg {{ flex-shrink: 0; }}
.history-status.booked {{ color: var(--ok-text); }}
.history-status.booked_manually {{ color: var(--ok-text); }}
.history-status.watching {{ color: var(--accent); }}
.history-status.no_slot {{ color: var(--muted); }}
.history-status.failed {{ color: var(--danger); }}
</style></head>
<body>
<h1><span class="logo">{ICON_LOGO}</span> Better Booking Bot</h1>
<div class="subtitle">Manage what to auto-book next week.</div>
<div class="nav"><a href="/">Targets</a><a href="/status">History</a></div>
{body}
</body></html>"""


def _relative_time(iso_str: str) -> str:
    try:
        then = datetime.fromisoformat(iso_str)
        now = datetime.now(then.tzinfo)
    except ValueError:
        return iso_str
    seconds = (now - then).total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def _status_page(targets: list[dict], status: dict) -> str:
    if not targets:
        return f'''<div class="card"><h2>{ICON_LIST} History</h2><p class="empty">{ICON_EMPTY}No targets configured yet.</p></div>'''
    rows = []
    for t in targets:
        name = html.escape(t["name"])
        url_name = quote(t["name"], safe="")
        session_date = date.today() + timedelta(days=int(t.get("days_ahead", 7)))
        entry = status.get(t["name"])
        secured_for_this_session = bool(
            entry and entry.get("status") in SECURED_STATUSES and entry.get("session_date") == session_date.isoformat()
        )
        if entry is None:
            result = '<span class="history-status" style="color: var(--muted)">not run yet</span>'
            when = "-"
        else:
            key = entry.get("status", "failed")
            icon = STATUS_ICONS.get(key, ICON_X)
            label = STATUS_LABELS.get(key, key)
            detail = html.escape(entry.get("detail", ""))
            title = f' title="{detail}"' if detail else ""
            result = f'<span class="history-status {key}"{title}>{icon}{label}</span>'
            when = html.escape(_relative_time(entry.get("ran_at", "")))
        action = (
            ""
            if secured_for_this_session
            else f'<form class="inline" method="post" action="/targets/{url_name}/mark-booked"><button>{ICON_CHECK}mark booked</button></form>'
        )
        rows.append(f"<tr><td>{name}</td><td>{result}</td><td>{when}</td><td class=\"actions\">{action}</td></tr>")
    return f"""<div class="card">
<h2>{ICON_LIST} History</h2>
<div class="table-wrap">
<table>
<tr><th>target</th><th>last result</th><th>when</th><th></th></tr>
{''.join(rows)}
</table>
</div>
</div>"""


def _targets_table(targets: list[dict]) -> str:
    if not targets:
        return f'''<div class="card"><h2>{ICON_LIST} Targets</h2><p class="empty">{ICON_EMPTY}No targets configured yet - add one below.</p></div>'''
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
<form class="inline" method="post" action="/targets/{url_name}/toggle"><button>{ICON_POWER}{'disable' if enabled else 'enable'}</button></form>
<form class="inline" method="post" action="/targets/{url_name}/delete" onsubmit="return confirm('Delete {name}?')"><button class="danger">{ICON_TRASH}delete</button></form>
</td>
</tr>""")
    return f"""<div class="card">
<h2>{ICON_LIST} Targets</h2>
<div class="table-wrap">
<table>
<tr><th>name</th><th>venue / activity</th><th>time</th><th>opens</th><th>status</th><th></th></tr>
{''.join(rows)}
</table>
</div>
</div>"""


def _add_form(error: str | None = None) -> str:
    weekday_options = "".join(f'<option value="{v}">{label}</option>' for v, label in WEEKDAYS)
    days_ahead_options = "".join(
        f'<option value="{n}"{" selected" if n == 7 else ""}>{n} day{"s" if n != 1 else ""}</option>'
        for n in range(1, 15)
    )
    release_hour_options = "".join(
        f'<option value="{h}"{" selected" if h == 21 else ""}>{h:02d}:00</option>' for h in range(24)
    )
    err = f'<p class="err">{html.escape(error)}</p>' if error else ""
    return f"""<div class="card">
<h2>{ICON_PLUS} Add target</h2>
{err}
<form method="post" action="/targets">
<div class="grid">
<div class="full"><label>Name</label><input name="name" required></div>
<div><label>Venue</label><select id="venue_select" name="venue_slug" required><option value="">Loading venues...</option></select></div>
<div><label>Activity</label><select id="activity_select" name="activity_slug" required disabled><option value="">Select a venue first</option></select></div>
<div><label>Session day</label><select id="weekday_select" name="weekday">{weekday_options}</select></div>
<div><label>Session time (24h)</label><select id="time_select" name="target_time" required disabled><option value="">Select venue, activity &amp; day first</option></select></div>
<div><label>Days ahead slot opens</label><select name="days_ahead">{days_ahead_options}</select></div>
<div><label>Release hour (local)</label><select name="release_hour">{release_hour_options}</select></div>
</div>
<button type="submit" class="primary">{ICON_PLUS} Add target</button>
</form>
</div>
<script>
(function() {{
  var venueSel = document.getElementById('venue_select');
  var activitySel = document.getElementById('activity_select');
  var weekdaySel = document.getElementById('weekday_select');
  var timeSel = document.getElementById('time_select');
  var TIME_RE = /^([01]\\d|2[0-3]):([0-5]\\d)$/;

  function setPlaceholder(select, text) {{
    select.innerHTML = '';
    var opt = document.createElement('option');
    opt.value = '';
    opt.textContent = text;
    select.appendChild(opt);
  }}

  function addManualTimeOption() {{
    var opt = document.createElement('option');
    opt.value = '__manual__';
    opt.textContent = 'Enter time manually...';
    timeSel.appendChild(opt);
  }}

  function loadVenues() {{
    fetch('/api/venues').then(function(r) {{ if (!r.ok) throw new Error(); return r.json(); }})
      .then(function(venues) {{
        setPlaceholder(venueSel, 'Select a venue...');
        venues.forEach(function(v) {{
          var opt = document.createElement('option');
          opt.value = v.slug;
          opt.textContent = v.town + ' - ' + v.name;
          venueSel.appendChild(opt);
        }});
      }})
      .catch(function() {{ setPlaceholder(venueSel, 'Could not load venues - reload to retry'); }});
  }}

  function loadActivities() {{
    activitySel.disabled = true;
    setPlaceholder(activitySel, venueSel.value ? 'Loading activities...' : 'Select a venue first');
    timeSel.disabled = true;
    setPlaceholder(timeSel, 'Select venue, activity & day first');
    if (!venueSel.value) return;
    fetch('/api/venues/' + encodeURIComponent(venueSel.value) + '/activities')
      .then(function(r) {{ if (!r.ok) throw new Error(); return r.json(); }})
      .then(function(activities) {{
        setPlaceholder(activitySel, 'Select an activity...');
        var group = null, lastCategory = null;
        activities.forEach(function(a) {{
          if (a.category !== lastCategory) {{
            group = document.createElement('optgroup');
            group.label = a.category;
            activitySel.appendChild(group);
            lastCategory = a.category;
          }}
          var opt = document.createElement('option');
          opt.value = a.slug;
          opt.textContent = a.name;
          group.appendChild(opt);
        }});
        activitySel.disabled = false;
      }})
      .catch(function() {{ setPlaceholder(activitySel, 'Could not load activities - reload to retry'); }});
  }}

  function loadTimes() {{
    timeSel.disabled = true;
    setPlaceholder(timeSel, 'Select venue, activity & day first');
    if (!venueSel.value || !activitySel.value) return;
    setPlaceholder(timeSel, 'Loading times...');
    fetch('/api/venues/' + encodeURIComponent(venueSel.value) + '/activities/' + encodeURIComponent(activitySel.value) + '/times?weekday=' + weekdaySel.value)
      .then(function(r) {{ if (!r.ok) throw new Error(); return r.json(); }})
      .then(function(times) {{
        setPlaceholder(timeSel, times.length ? 'Select a time...' : 'No upcoming slots found');
        times.forEach(function(t) {{
          var opt = document.createElement('option');
          opt.value = t;
          opt.textContent = t;
          timeSel.appendChild(opt);
        }});
        addManualTimeOption();
        timeSel.disabled = false;
      }})
      .catch(function() {{
        setPlaceholder(timeSel, 'Could not load times');
        addManualTimeOption();
        timeSel.disabled = false;
      }});
  }}

  timeSel.addEventListener('change', function() {{
    if (timeSel.value !== '__manual__') return;
    var manual = window.prompt('Session time (24h, HH:MM):', '19:30');
    if (manual && TIME_RE.test(manual)) {{
      var opt = document.createElement('option');
      opt.value = manual;
      opt.textContent = manual + ' (manual)';
      timeSel.insertBefore(opt, timeSel.lastElementChild);
      timeSel.value = manual;
    }} else {{
      if (manual !== null) window.alert('Enter a time as HH:MM, e.g. 19:30');
      timeSel.value = '';
    }}
  }});

  venueSel.addEventListener('change', function() {{ loadActivities(); }});
  activitySel.addEventListener('change', loadTimes);
  weekdaySel.addEventListener('change', loadTimes);
  loadVenues();
}})();
</script>"""


@app.get("/api/venues")
def api_venues() -> list[dict]:
    try:
        return _cached_venues()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not fetch venues: {exc}") from exc


@app.get("/api/venues/{venue_slug}/activities")
def api_activities(venue_slug: str) -> list[dict]:
    try:
        return _cached_activities(venue_slug)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not fetch activities: {exc}") from exc


@app.get("/api/venues/{venue_slug}/activities/{activity_slug}/times")
def api_times(venue_slug: str, activity_slug: str, weekday: str) -> list[str]:
    if weekday not in {v for v, _ in WEEKDAYS}:
        raise HTTPException(status_code=422, detail="weekday must be one of the cron day-of-week values 0-6")
    return _typical_times(venue_slug, activity_slug, weekday)


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    targets = load_config().get("targets", [])
    return _page(_targets_table(targets) + _add_form())


@app.get("/status", response_class=HTMLResponse)
def status_page() -> str:
    targets = load_config().get("targets", [])
    return _page(_status_page(targets, load_status()))


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
        return HTMLResponse(_page(_targets_table(targets) + _add_form(msg)))

    if not venue_slug.strip() or not activity_slug.strip():
        return error("Pick a venue and an activity.")
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


@app.post("/targets/{name}/mark-booked")
def mark_booked(name: str) -> RedirectResponse:
    targets = load_config().get("targets", [])
    target = next((t for t in targets if t["name"] == name), None)
    if target is not None:
        session_date = date.today() + timedelta(days=int(target.get("days_ahead", 7)))
        status = load_status()
        status[name] = {
            "status": "booked_manually",
            "session_date": session_date.isoformat(),
            "target_time": target["target_time"],
            "detail": "",
            "ran_at": datetime.now(timezone.utc).isoformat(),
        }
        save_status(status)
    return RedirectResponse("/status", status_code=HTTP_303_SEE_OTHER)


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
