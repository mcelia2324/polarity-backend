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

## GCP Cost

Target: well under $30/month. Actual billed cost for project `polarity-488001`
(net of free tier) as of September 2026:

| Service | Per month | Notes |
| --- | --- | --- |
| Cloud SQL (`db-f1-micro`, 10 GB HDD) | ~$8.50 | Always-on floor; >95% of the bill |
| Secret Manager | ~$0.42 | 6 active versions are free; older versions count |
| Artifact Registry | ~$0.02 | |
| Cloud Run | $0.00 | Request-based billing; idle instances are free |
| Cloud Scheduler | $0.00 | 2 jobs (daily cron + keep-warm); 3 are free per billing account |
| Serverless VPC connector | removed | Was ~$2.10/mo; replaced by Direct VPC egress |

Things that quietly add a fixed monthly cost, so don't reintroduce them without a reason:

- **`min_instance_count >= 1`** on Cloud Run: ~$4.75/mo (ran Mar–Jun 2026).
- **A Serverless VPC Access connector**: 2+ VMs running 24/7. Cloud Run reaches Cloud SQL's
  private IP through Direct VPC egress instead (`vpc_access.network_interfaces` in
  `terraform/cloud_run.tf`), which has no idle cost.
- **A larger Cloud SQL tier or SSD storage**: the database is ~76 MB at ~0.1% CPU.

Cold starts take ~7s (container + Python startup), so the `polarity-keep-warm` Scheduler job
fetches `/api/word-of-day` every 5 minutes. That keeps an instance warm and makes sure the new
day's pair is generated right after midnight by the job, not by the first person to open the app.
It costs nothing; `min_instance_count = 1` would do the same for ~$4.75/mo.

`terraform/budget.tf` emails the billing admins at 50% / 90% / 100% of `monthly_budget_usd`
($30) and when the month is forecast to exceed it.
