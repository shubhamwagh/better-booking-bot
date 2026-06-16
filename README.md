# better-booking-bot

Automated activity booking bot for [Better (GLL)](https://www.better.org.uk) leisure centres.

Monitors slot availability, applies account credit, and completes payment automatically — via saved card, new card, or credit-only.

## Quick start (Docker)

```bash
git clone https://github.com/shubhamwagh/better-booking-bot.git
cd better-booking-bot
cp .env.example .env          # fill in your credentials
docker compose up -d          # runs as daemon, self-schedules from config.yaml
docker compose logs -f        # watch logs
```

The image is pre-built and published to GHCR — no build step needed.

## Configuration

### Credentials — `.env`

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

### Booking targets — `config.yaml`

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
