# Polarity Backend

Private backend services for the Polarity iOS app.

## Responsibilities

- Generate the daily contrasting word pair (OpenAI provider).
- Persist words and definitions so pairs are not reused.
- Serve iOS API endpoints for word-of-day and history.
- Register iOS devices and manage push notification preferences.
- Send APNs notifications for the daily prompt.

The backend does not store user journal content.

## Tech Stack

- FastAPI
- APScheduler
- PostgreSQL (async SQLAlchemy)
- APNs integration for iOS push

## Quick Start (Docker)

1. Copy `.env.example` to `.env` and fill in required values.
2. Run:
   ```bash
   docker compose up --build
   ```
3. App is served on `http://localhost:8069`.
4. Postgres is exposed on host port `5455`.

## Required Environment Variables

- `DATABASE_URL`
- `OPENAI_API_KEY`
- `OPENAI_MODEL` (optional)
- `APP_TIMEZONE`
- `SEND_HOUR`
- `SEND_MINUTE`

For APNs push:
- `APNS_KEY_ID`
- `APNS_TEAM_ID`
- `APNS_BUNDLE_ID`
- `APNS_AUTH_KEY`
- `APNS_USE_SANDBOX`

## iOS API Endpoints

- `GET /api/word-of-day`
- `GET /api/history?days=30`
- `POST /api/devices/register`
- `POST /api/devices/toggle`

## Operational Notes

- Run a single scheduler instance to avoid duplicate daily sends.
- Keep all secrets in environment variables or secret manager.
