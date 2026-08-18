# better-booking-bot

[![CI](https://github.com/shubhamwagh/better-booking-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/shubhamwagh/better-booking-bot/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/shubhamwagh/better-booking-bot)](https://github.com/shubhamwagh/better-booking-bot/releases)
[![Python 3.13+](https://img.shields.io/badge/python-3.13%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Automated activity booking bot for [Better (GLL)](https://www.better.org.uk) leisure centres.

Monitors slot availability, applies account credit, and completes payment automatically - via saved card, new card, or credit-only.

> **WARNING:** This tool automates real payments using your Better account credentials and card details.
> Use it at your own risk. The authors take no responsibility for unintended bookings or charges.
>
> **Never commit your `.env` file to Git or push it to GitHub.** It contains passwords and card details.
> The `.gitignore` excludes `.env` by default - keep it that way.

## Self-hosting

This runs entirely on your own machine (or a VPS/NAS/home server) - nothing calls out to any
service besides Better's own site. Two long-running processes: a **daemon** that schedules and
runs bookings, and a **web UI** for managing targets. They talk to each other only through
`config.yaml`, `status.json`, and a shared `logs/` folder on disk - no other coupling.

### Quick start (Docker, recommended)

Requires Docker + Docker Compose.

```bash
git clone https://github.com/shubhamwagh/better-booking-bot.git
cd better-booking-bot
cp .env.example .env          # fill in your credentials
docker compose up -d          # runs the daemon + web UI, self-schedules from config.yaml
docker compose logs -f        # watch logs (or use the web UI's Logs tab)
```

The image is pre-built and published to GHCR (`ghcr.io/shubhamwagh/better-booking-bot`) - no
build step needed. `docker-compose.yml` mounts `config.yaml`, `status.json`, and `logs/` from
the current directory, so all state survives container restarts/upgrades.

**Updating** to a newer release:

```bash
docker compose pull
docker compose up -d
```

**Building the image yourself** instead of pulling the pre-built one (e.g. if you'd rather not
trust a binary you didn't build):

```bash
docker build -t better-booking-bot .
# then point docker-compose.yml's `image:` at `better-booking-bot` instead of the ghcr.io one
```

### Without Docker

Requires Python 3.13+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/shubhamwagh/better-booking-bot.git
cd better-booking-bot
cp .env.example .env          # fill in your credentials
uv sync
uv run -m better_bot.daemon &     # scheduler - keep this running
uv run -m better_bot.webui        # web UI on :8080 - keep this running too
```

Both need to stay running continuously, so in practice you'll want a process supervisor
(systemd, supervisord, pm2, tmux - whatever you already use) rather than backgrounding them by
hand as shown above. `CONFIG_PATH` and `LOG_PATH` env vars let you point either process at a
different `config.yaml`/log location if you're not running them from the same directory.

### Web UI

Open `http://localhost:8080` for the web UI:

- **Targets** - add/enable/disable/delete targets; venue and activity are picked from live
  dropdowns (backed by the Better API), so no slugs to type by hand. Edits take effect within
  `CONFIG_POLL_S` seconds without restarting the daemon.
- **History** - last-run result per target. "mark booked" flags a target as secured if you
  booked it yourself after the bot missed; "cancel booking" cancels a real booking (bot- or
  manually-booked) via the same API call Better's own site uses.
- **Logs** - tails the daemon's log file, refreshing every few seconds.

## Configuration

### Credentials - `.env`

```bash
BETTER_USERNAME=your@email.com
BETTER_PASSWORD=yourpassword

# Saved card mode (recommended after first booking)
CARD_CVV=123

# New card mode (first-time users)
# CARD_NUMBER=4111111111111111
# CARD_EXPIRY=12/27
# CARD_CVV=123
# SAVE_CARD=true

# Billing address (required for new card mode)
# BILLING_FIRST_NAME=John
# BILLING_LAST_NAME=Smith
# BILLING_ADDRESS1=123 High Street
# BILLING_CITY=Oxford
# BILLING_POSTCODE=OX1 1AA
```

### Booking targets - `config.yaml`

```yaml
targets:
  - name: "Abingdon Pickleball Monday 19:30"
    venue_slug: "white-horse-leisure-and-tennis-centre"
    activity_slug: "pickleball-drop-in"
    target_time: "19:30"
    days_ahead: 7
    release_hour: 21
    cron: "57 20 * * 1"   # fire at 20:57 Monday (3 mins before slots open)
    enabled: true
```

Find `venue_slug` and `activity_slug` from the URL on the Better website:
`https://bookings.better.org.uk/location/{venue_slug}/activity/{activity_slug}/...`

### Phone notifications - `.env` (optional)

Get a push notification on your phone the moment a target is booked, fails, or finds no
slot - via [ntfy](https://ntfy.sh), a free push service with apps for
[iOS](https://apps.apple.com/app/ntfy/id1625396347) and
[Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy).

**Quickest setup - the public `ntfy.sh` server, no account needed:**

1. Pick a topic name nobody will guess - anyone who knows it can read your notifications,
   since a public topic has no login. `openssl rand -hex 8` gives you something like
   `a1b2c3d4e5f6a7b8`.
2. In the ntfy app: add that topic name on server `ntfy.sh` (the default) and subscribe.
3. Add to `.env`:
   ```bash
   NTFY_URL=https://ntfy.sh
   NTFY_TOPIC=a1b2c3d4e5f6a7b8    # use your own random topic, not this one
   ```

No `NTFY_TOKEN` needed for a plain public topic.

**Self-hosting ntfy instead** (so message content never leaves your own server): follow
[ntfy's self-hosting docs](https://docs.ntfy.sh/install/) - Docker Compose or a container
image is enough, no Kubernetes required. Once it's running with auth enabled
(`auth-default-access: deny-all`), create a user and a scoped publish token:

```bash
ntfy user add <your-username>
ntfy access <your-username> <your-topic> rw
ntfy token add <your-username>          # -> tk_...
```

Then set `NTFY_URL` to your server, `NTFY_TOPIC` to your topic, and `NTFY_TOKEN` to the
`tk_...` token. Log into the same user/password in the phone app to subscribe.

If none of `NTFY_URL`/`NTFY_TOPIC`/`NTFY_TOKEN` are set, the bot just logs notifications
instead of pushing anywhere - no error, no crash.

### Checkout flow (automatic)

1. Adds session to cart
2. Applies full account credit if available
3. Detects payment mode from checkout page:
   - Credit covers full cost → confirms without card entry
   - Saved card present → fills CVV only
   - No saved card → fills billing details + full card

## Running a single target manually

```bash
docker run --rm --env-file .env \
  ghcr.io/shubhamwagh/better-booking-bot:latest \
  --target "Abingdon Pickleball Monday 19:30"
```

## Development

```bash
uv sync
uv run -m better_bot.bot --list
uv run -m better_bot.bot --target "name" --dry-run
uv run -m better_bot.daemon
```
