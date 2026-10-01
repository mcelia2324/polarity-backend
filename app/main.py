from __future__ import annotations

import datetime as dt
import logging
import os
import time
from collections import defaultdict
from zoneinfo import ZoneInfo

import httpx

from fastapi import Body, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import delete, func, select
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response as StarletteResponse

from app.config import settings as env_settings
from app.db import SessionLocal, init_db, wait_for_db
from app.models import Delivery, DeviceToken, Setting, WordDefinition, WordPair
from app.schemas import DeviceRegisterRequest, DeviceToggleRequest, HistoryResponse, WordPairResponse
from app.services.daily_content_service import DailyContentService
from app.services.definition_service import DefinitionService
from app.services.llm import build_provider
from app.services.push.apns import APNSClient
from app.services.settings_store import SettingsStore
from app.services.text_utils import normalize_dashes
from app.services.word_service import (
    WORD_REUSE_AFTER_DAYS,
    WordService,
    format_pair_display,
    prune_used_words,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# In-memory cache for today's word-of-day response (single worker, safe as a dict).
# Keyed by date string; automatically invalidated when the date rolls over.
_word_of_day_cache: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Rate limiter (in-memory, per-IP, sliding window)
# ---------------------------------------------------------------------------
class _RateLimitStore:
    """Simple per-IP rate limiter. Tracks request timestamps in memory."""

    def __init__(self) -> None:
        self._requests: dict[str, list[float]] = defaultdict(list)

    def is_allowed(self, key: str, max_requests: int, window_seconds: int) -> bool:
        now = time.monotonic()
        timestamps = self._requests[key]
        # Evict old entries
        cutoff = now - window_seconds
        self._requests[key] = [t for t in timestamps if t > cutoff]
        if len(self._requests[key]) >= max_requests:
            return False
        self._requests[key].append(now)
        return True


_rate_limiter = _RateLimitStore()

# Rate limits: (max_requests, window_seconds)
_RATE_LIMITS: dict[str, tuple[int, int]] = {
    "/api/word-of-day": (10, 60),       # 10 req/min
    "/api/history": (10, 60),            # 10 req/min
    "/api/devices/register": (5, 60),    # 5 req/min
    "/api/devices/toggle": (5, 60),      # 5 req/min
}


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        limit = _RATE_LIMITS.get(path)
        if limit:
            client_ip = request.client.host if request.client else "unknown"
            key = f"{client_ip}:{path}"
            max_req, window = limit
            if not _rate_limiter.is_allowed(key, max_req, window):
                return StarletteResponse(
                    content='{"detail":"Too many requests"}',
                    status_code=429,
                    media_type="application/json",
                )
        return await call_next(request)


app = FastAPI(title="Polarity")
app.add_middleware(RateLimitMiddleware)


async def _get_timezone(settings_store: SettingsStore) -> ZoneInfo:
    tz_name = await settings_store.get_str("app_timezone", "UTC") or "UTC"
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo("UTC")


@app.on_event("startup")
async def on_startup() -> None:
    await wait_for_db()
    await init_db()
    async with SessionLocal() as session:
        store = SettingsStore(session)
        await store.seed_from_env(
            [
                "app_timezone",
                "send_hour",
                "send_minute",
                "openai_api_key",
                "openai_model",
                "apns_key_id",
                "apns_team_id",
                "apns_bundle_id",
                "apns_auth_key",
                "apns_use_sandbox",
            ]
        )
        current_model = await store.get_str("openai_model", env_settings.openai_model)
        if current_model and current_model.startswith("gpt-5-nano"):
            await store.set_value("openai_model", "gpt-5.2")
        await session.commit()


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Privacy Policy
# ---------------------------------------------------------------------------

PRIVACY_POLICY_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Polarity Journal Privacy Policy</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         max-width: 680px; margin: 0 auto; padding: 24px 16px; line-height: 1.6;
         color: #1e1a17; background: #f5f0e8; }
  h1 { font-size: 1.6rem; }
  h2 { font-size: 1.15rem; margin-top: 1.5em; }
  p, li { font-size: 0.95rem; }
  .updated { color: #6b5e52; font-size: 0.85rem; }
</style>
</head>
<body>
<h1>Privacy Policy</h1>
<p class="updated">Last updated: March 6, 2026</p>

<p><strong>Polarity Journal</strong> ("the App") is developed by an independent developer.
Your privacy is important to us. This policy explains what data we collect and how we use it.</p>

<h2>Data Stored on Your Device</h2>
<ul>
  <li><strong>Journal entries:</strong> All journal entries you write are stored locally on your device.
      If you enable iCloud sync in Settings, entries are stored in your private iCloud Drive.
      Journal entries are never sent to our servers.</li>
</ul>

<h2>Data We Collect</h2>
<ul>
  <li><strong>Device tokens:</strong> If you enable push notifications, your device's push token is
      stored on our server so we can send you daily reminders. You can disable notifications at any
      time in Settings.</li>
</ul>

<h2>Data We Do Not Collect</h2>
<ul>
  <li>We do not collect or store your journal entries on our servers.</li>
  <li>We do not collect your name, email address, or Apple ID.</li>
  <li>We do not use analytics, tracking, or advertising SDKs.</li>
  <li>We do not sell or share your data with third parties.</li>
</ul>

<h2>Third-Party Services</h2>
<p>The App uses OpenAI's API to generate daily word pairs. No personal data is sent to OpenAI,
only requests for word generation.</p>

<h2>Data Storage &amp; Security</h2>
<p>Push notification tokens are stored on Google Cloud Platform infrastructure with encryption at rest
and in transit. Journal entries remain on your device or in your private iCloud account.</p>

<h2>Your Rights</h2>
<p>You can delete your journal entries at any time within the app. Uninstalling the App and
disabling notifications will stop all data collection. You may contact us to request deletion of
your device token from our servers.</p>

<h2>Changes</h2>
<p>We may update this policy from time to time. Changes will be reflected on this page with an
updated date.</p>

<h2>Contact</h2>
<p>If you have questions about this policy, please open an issue on our
<a href="https://github.com/mcelia2324/polarity">GitHub repository</a>.</p>
</body>
</html>"""


@app.get("/privacy", response_class=HTMLResponse)
async def privacy_policy():
    return PRIVACY_POLICY_HTML


# ---------------------------------------------------------------------------
# Cron endpoint (replaces APScheduler, called by Cloud Scheduler)
# ---------------------------------------------------------------------------

async def _run_daily() -> dict:
    """Generate today's word pair and send push notifications."""
    async with SessionLocal() as session:
        settings_store = SettingsStore(session)
        tz_name = await settings_store.get_str("app_timezone", "UTC") or "UTC"
        try:
            timezone = ZoneInfo(tz_name)
        except Exception:
            timezone = ZoneInfo("UTC")
        today = dt.datetime.now(timezone).date()

        provider = await build_provider(settings_store)
        word_service = WordService(session, provider)
        pair = await word_service.ensure_pair_for_date(today)

        # Pre-cache definitions so the app never waits for them
        definition_service = DefinitionService(session, provider)
        try:
            await definition_service.get_definition(pair.word_a)
            await definition_service.get_definition(pair.word_b)
            logger.info("Pre-cached definitions for %s, %s", pair.word_a, pair.word_b)
        except Exception:
            logger.warning("Failed to pre-cache definitions", exc_info=True)

        # Pre-generate the daily quote + contemplation so they are ready (one generation per day).
        try:
            await DailyContentService(session, provider).get_or_create(today, pair.word_a, pair.word_b)
        except Exception:
            logger.warning("Failed to pre-generate daily content", exc_info=True)

        # Automation: each day, clear the internal word bank of entries older than the reuse
        # window so old words are free to return and the daily pair keeps feeling fresh.
        try:
            removed = await prune_used_words(session)
            if removed:
                logger.info("Pruned %d used-word entries older than the reuse window.", removed)
        except Exception:
            logger.warning("Used-word prune failed", exc_info=True)

        message = (
            f"Polarity for {today.strftime('%B %d, %Y')}:\n"
            f"{format_pair_display(pair.word_a, pair.word_b)}\n"
            "Reflect on the meanings, differences, and which calibrates higher."
        )

        apns_client = await APNSClient.from_settings(settings_store)
        if apns_client is None:
            logger.info("APNs not configured; skipping push notifications.")
            return {"status": "ok", "date": today.isoformat(), "push": "skipped"}

        # Check if already delivered today
        result = await session.execute(
            select(Delivery).where(
                Delivery.date == today,
                Delivery.channel == "apns",
                Delivery.status == "sent",
            )
        )
        if result.scalar_one_or_none() is not None:
            return {"status": "ok", "date": today.isoformat(), "push": "already_sent"}

        try:
            # `failed` excludes tokens APNs reported dead (now disabled), so status covers active tokens only.
            sent_count, failed, disabled = await apns_client.send_daily(pair, today, message, session)
            status = "sent" if failed == 0 else "partial"

            # Record delivery
            result = await session.execute(
                select(Delivery).where(Delivery.date == today, Delivery.channel == "apns")
            )
            delivery = result.scalar_one_or_none()
            error_msg = None if failed == 0 else f"{failed} failed"
            if delivery is None:
                delivery = Delivery(date=today, channel="apns", status=status, error=error_msg)
                session.add(delivery)
            else:
                delivery.status = status
                delivery.error = error_msg
            await session.commit()

            logger.info("APNs sent: %d, failed: %d, invalid tokens disabled: %d", sent_count, failed, disabled)
            return {
                "status": "ok",
                "date": today.isoformat(),
                "push": status,
                "sent": sent_count,
                "failed": failed,
                "disabled": disabled,
            }
        except Exception as exc:
            logger.exception("Failed sending via apns")
            result = await session.execute(
                select(Delivery).where(Delivery.date == today, Delivery.channel == "apns")
            )
            delivery = result.scalar_one_or_none()
            if delivery is None:
                delivery = Delivery(date=today, channel="apns", status="failed", error=str(exc))
                session.add(delivery)
            else:
                delivery.status = "failed"
                delivery.error = str(exc)
            await session.commit()
            raise


def _require_cron_secret(x_cron_secret: str | None) -> None:
    expected = env_settings.cron_secret
    if not expected or x_cron_secret != expected:
        raise HTTPException(status_code=403, detail="Forbidden")


@app.post("/cron/daily")
async def cron_daily(x_cron_secret: str | None = Header(None, alias="X-Cron-Secret")):
    _require_cron_secret(x_cron_secret)
    return await _run_daily()


@app.post("/cron/cleanup")
async def cron_cleanup(
    x_cron_secret: str | None = Header(None, alias="X-Cron-Secret"),
    older_than_days: int | None = None,
):
    """Clear the internal word bank (used-word hint list) of entries older than N days (defaults
    to the reuse window). Old words become reusable through the pair-history window, so this only
    tidies the hint table and never touches the daily pair history. The daily cron runs this
    automatically; this endpoint is for clearing on demand."""
    _require_cron_secret(x_cron_secret)
    days = older_than_days if (older_than_days and older_than_days > 0) else WORD_REUSE_AFTER_DAYS
    async with SessionLocal() as session:
        removed = await prune_used_words(session, days)
    return {"status": "ok", "pruned": removed, "older_than_days": days}


# ---------------------------------------------------------------------------
# Push diagnostics + runtime admin (guarded by the cron secret)
#
# These turn "push does not work" from a guessing game into a single call. They
# never return secret values, only whether each piece is present and what APNs
# actually replied.
# ---------------------------------------------------------------------------

def _http2_available() -> bool:
    try:
        import h2  # noqa: F401
        return True
    except Exception:
        return False


@app.get("/cron/diagnostics")
async def cron_diagnostics(x_cron_secret: str | None = Header(None, alias="X-Cron-Secret")):
    _require_cron_secret(x_cron_secret)
    async with SessionLocal() as session:
        store = SettingsStore(session)

        key_id = await store.get_str("apns_key_id")
        team_id = await store.get_str("apns_team_id")
        bundle_id = await store.get_str("apns_bundle_id")
        inline = await store.get_str("apns_auth_key")
        resolved = APNSClient._resolve_auth_key(inline)
        use_sandbox = await store.get_bool("apns_use_sandbox", True)

        def _is_placeholder(v: str | None) -> bool:
            return (v or "").strip().upper() == "PLACEHOLDER"

        inline_usable = bool(inline and (inline.strip().startswith("-----BEGIN") or os.path.exists(inline.strip())))
        if not resolved:
            auth_key_source = "none"
        elif inline_usable:
            auth_key_source = "inline"
        else:
            auth_key_source = "volume"

        apns_configured = bool(
            key_id and team_id and bundle_id and resolved
            and not _is_placeholder(key_id) and not _is_placeholder(team_id)
        )

        total = await session.scalar(select(func.count()).select_from(DeviceToken)) or 0
        enabled = await session.scalar(
            select(func.count()).select_from(DeviceToken).where(DeviceToken.enabled == True)
        ) or 0

        recent = (
            await session.execute(
                select(DeviceToken).order_by(DeviceToken.updated_at.desc()).limit(5)
            )
        ).scalars().all()
        tokens_sample = [
            {
                "token_suffix": t.token[-6:],
                "platform": t.platform,
                "enabled": t.enabled,
                "last_notified_at": t.last_notified_at.isoformat() if t.last_notified_at else None,
                "last_error": t.last_error,
                "created_at": t.created_at.isoformat() if t.created_at else None,
            }
            for t in recent
        ]

        deliveries = (
            await session.execute(select(Delivery).order_by(Delivery.date.desc()).limit(5))
        ).scalars().all()
        deliveries_sample = [
            {"date": d.date.isoformat(), "channel": d.channel, "status": d.status, "error": d.error}
            for d in deliveries
        ]

        return {
            "apns_configured": apns_configured,
            "apns_fields_present": {
                "key_id_present": bool(key_id),
                "key_id_is_placeholder": _is_placeholder(key_id),
                "team_id_present": bool(team_id),
                "team_id_is_placeholder": _is_placeholder(team_id),
                "bundle_id": bundle_id,  # public value, safe to show; verify it is mcelia.PolarityApp
                "auth_key_source": auth_key_source,  # 'inline' | 'volume' | 'none'
                "auth_key_is_valid_pem": bool(resolved),  # false when the .p8 secret is still a placeholder
            },
            "use_sandbox": use_sandbox,
            "apns_host": "api.sandbox.push.apple.com" if use_sandbox else "api.push.apple.com",
            "http2_available": _http2_available(),
            "device_tokens": {"total": total, "enabled": enabled},
            "recent_tokens": tokens_sample,
            "recent_deliveries": deliveries_sample,
        }


@app.post("/cron/settings")
async def cron_set_setting(
    x_cron_secret: str | None = Header(None, alias="X-Cron-Secret"),
    payload: dict = Body(...),
):
    """Set a runtime setting (e.g. flip apns_use_sandbox to false without a redeploy).
    Only a small allowlist of non-secret operational keys is writable here."""
    _require_cron_secret(x_cron_secret)
    allowed = {
        "apns_use_sandbox", "apns_bundle_id", "apns_key_id", "apns_team_id",
        "send_hour", "send_minute", "app_timezone",
    }
    key = payload.get("key")
    value = payload.get("value")
    if key not in allowed:
        raise HTTPException(status_code=400, detail=f"Key not allowed. Allowed: {sorted(allowed)}")
    if value is None:
        raise HTTPException(status_code=400, detail="Missing value")
    async with SessionLocal() as session:
        store = SettingsStore(session)
        await store.set_value(key, str(value))
        await session.commit()
    return {"status": "ok", "key": key, "value": str(value)}


@app.post("/cron/push-test")
async def cron_push_test(
    x_cron_secret: str | None = Header(None, alias="X-Cron-Secret"),
    payload: dict | None = Body(None),
):
    """Send a single test push and return exactly what APNs replied (status + reason),
    so a failure like BadDeviceToken or BadTopic is visible immediately. Targets the
    token suffix in the body {"token_suffix": "abc123"} if given, else the most recently
    updated enabled token."""
    _require_cron_secret(x_cron_secret)
    async with SessionLocal() as session:
        store = SettingsStore(session)
        client = await APNSClient.from_settings(store)
        if client is None:
            return {"status": "apns_not_configured"}

        suffix = (payload or {}).get("token_suffix")
        query = select(DeviceToken).where(DeviceToken.enabled == True)
        if suffix:
            query = query.where(DeviceToken.token.like(f"%{suffix}"))
        target = (await session.execute(query.order_by(DeviceToken.updated_at.desc()).limit(1))).scalar_one_or_none()
        if target is None:
            return {"status": "no_matching_token"}

        headers = {
            "apns-topic": client._config.bundle_id,
            "authorization": f"bearer {client._get_jwt()}",
            "apns-push-type": "alert",
        }
        body = {"aps": {"alert": {"title": "Polarity", "body": "Test notification. You can ignore this."}, "sound": "default"}}
        url = f"{client._endpoint()}/3/device/{target.token}"
        try:
            async with httpx.AsyncClient(http2=True, timeout=20) as http:
                resp = await http.post(url, json=body, headers=headers)
            return {
                "status": "sent" if resp.status_code == 200 else "failed",
                "http_status": resp.status_code,
                "apns_reason": resp.text or None,
                "apns_host": client._endpoint(),
                "token_suffix": target.token[-6:],
            }
        except Exception as exc:  # noqa: BLE001
            return {"status": "exception", "error": f"{type(exc).__name__}: {exc}", "http2_available": _http2_available()}


@app.post("/cron/backfill")
async def cron_backfill(
    date: str,
    force: bool = False,
    x_cron_secret: str | None = Header(None, alias="X-Cron-Secret"),
):
    """Generate the pair (and cache definitions) for a specific date, e.g. to fill a gap.

    Guarded by the cron secret. Idempotent by default: if a pair already exists for the date it
    is returned unchanged. Pass force=true to discard an existing pair and regenerate it (the old
    words stay marked used, so they are not reused). Does not send push notifications.
    """
    expected = env_settings.cron_secret
    if not expected or x_cron_secret != expected:
        raise HTTPException(status_code=403, detail="Forbidden")
    try:
        target = dt.date.fromisoformat(date)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date; expected YYYY-MM-DD")

    async with SessionLocal() as session:
        if force:
            # Direct DELETE so the DB's ON DELETE SET NULL handles referencing rows (async-safe,
            # no ORM lazy-load). Used/journal rows keep their words but detach from the pair.
            await session.execute(delete(WordPair).where(WordPair.date == target))
            await session.commit()
        store = SettingsStore(session)
        provider = await build_provider(store)
        word_service = WordService(session, provider)
        pair = await word_service.ensure_pair_for_date(target)
        definition_service = DefinitionService(session, provider)
        try:
            await definition_service.get_definition(pair.word_a)
            await definition_service.get_definition(pair.word_b)
        except Exception:
            logger.warning("Backfill: failed to pre-cache definitions for %s", target, exc_info=True)

    return {"status": "ok", "date": target.isoformat(), "word_a": pair.word_a, "word_b": pair.word_b}


# ---------------------------------------------------------------------------
# iOS API
# ---------------------------------------------------------------------------

@app.get("/api/word-of-day", response_model=WordPairResponse)
async def api_word_of_day():
    async with SessionLocal() as session:
        store = SettingsStore(session)
        timezone = await _get_timezone(store)
        today = dt.datetime.now(timezone).date()
        today_str = today.isoformat()

        # Return from in-memory cache if available
        if today_str in _word_of_day_cache:
            return JSONResponse(
                content=_word_of_day_cache[today_str],
                headers={"Cache-Control": "public, max-age=1800"},
            )

        provider = await build_provider(store)
        word_service = WordService(session, provider)
        pair = await word_service.ensure_pair_for_date(today)
        definition_service = DefinitionService(session, provider)
        word_a_definition = await definition_service.get_definition(pair.word_a)
        word_b_definition = await definition_service.get_definition(pair.word_b)

        # Daily quote + guided contemplation: generated once per day for everyone and persisted.
        daily = await DailyContentService(session, provider).get_or_create(today, pair.word_a, pair.word_b)

        response_data = {
            "date": today_str,
            "word_a": pair.word_a,
            "word_b": pair.word_b,
            "word_a_definition": normalize_dashes(word_a_definition),
            "word_b_definition": normalize_dashes(word_b_definition),
            "quote": normalize_dashes(daily.quote),
            "quote_author": normalize_dashes(daily.quote_author),
            "contemplation": normalize_dashes(daily.contemplation),
        }

        # Cache and evict stale entries (keep only today)
        _word_of_day_cache.clear()
        _word_of_day_cache[today_str] = response_data

        return JSONResponse(
            content=response_data,
            headers={"Cache-Control": "public, max-age=1800"},
        )


@app.get("/api/history", response_model=HistoryResponse)
async def api_history(days: int = 30):
    days = max(1, min(days, 365))
    async with SessionLocal() as session:
        result = await session.execute(
            select(WordPair).order_by(WordPair.date.desc()).limit(days)
        )
        pairs = result.scalars().all()
        # Batch-fetch all definitions for the words in this history
        all_words = set()
        for row in pairs:
            all_words.add(row.word_a.strip().lower())
            all_words.add(row.word_b.strip().lower())
        defs: dict[str, str] = {}
        if all_words:
            def_result = await session.execute(
                select(WordDefinition).where(WordDefinition.word.in_(all_words))
            )
            for wd in def_result.scalars().all():
                defs[wd.word] = wd.definition
        items = [
            WordPairResponse(
                date=row.date,
                word_a=row.word_a,
                word_b=row.word_b,
                word_a_definition=normalize_dashes(defs.get(row.word_a.strip().lower())),
                word_b_definition=normalize_dashes(defs.get(row.word_b.strip().lower())),
            )
            for row in pairs
        ]
        return HistoryResponse(items=items)


@app.post("/api/devices/register")
async def api_device_register(payload: DeviceRegisterRequest):
    async with SessionLocal() as session:
        result = await session.execute(select(DeviceToken).where(DeviceToken.token == payload.token))
        device = result.scalar_one_or_none()
        if device is None:
            device = DeviceToken(token=payload.token)
            session.add(device)
        device.platform = payload.platform
        device.timezone = payload.timezone
        device.enabled = payload.enabled
        device.notify_hour = payload.notify_hour
        device.notify_minute = payload.notify_minute
        await session.commit()
    return {"status": "ok"}


@app.post("/api/devices/toggle")
async def api_device_toggle(payload: DeviceToggleRequest):
    async with SessionLocal() as session:
        result = await session.execute(select(DeviceToken).where(DeviceToken.token == payload.token))
        device = result.scalar_one_or_none()
        if device is None:
            return {"status": "not_found"}
        device.enabled = payload.enabled
        await session.commit()
    return {"status": "ok"}
