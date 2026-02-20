from __future__ import annotations

import datetime as dt
import logging
import os
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from app.config import settings as env_settings
from app.db import SessionLocal, init_db, wait_for_db
from app.models import Delivery, DeviceToken, JournalEntry, WordPair
from app.schemas import DeviceRegisterRequest, DeviceToggleRequest, HistoryResponse, WordPairResponse
from app.services.definition_service import DefinitionService
from app.services.llm import LLMProviderError, build_provider
from app.services.push.apns import APNSClient
from app.services.settings_store import SettingsStore
from app.services.word_service import WordGenerationError, WordService, format_pair_display

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Polarity")
app.mount("/static", StaticFiles(directory="app/static"), name="static")

templates = Jinja2Templates(directory="app/templates")


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
# Cron endpoint (replaces APScheduler — called by Cloud Scheduler)
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
            sent_count, failed = await apns_client.send_daily(pair, today, message, session)
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

            logger.info("APNs sent: %d, failed: %d", sent_count, failed)
            return {"status": "ok", "date": today.isoformat(), "push": status, "sent": sent_count, "failed": failed}
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


@app.post("/cron/daily")
async def cron_daily(x_cron_secret: str | None = Header(None, alias="X-Cron-Secret")):
    expected = env_settings.cron_secret
    if not expected or x_cron_secret != expected:
        raise HTTPException(status_code=403, detail="Forbidden")
    return await _run_daily()


# ---------------------------------------------------------------------------
# Web dashboard
# ---------------------------------------------------------------------------

@app.get("/")
async def index(request: Request, error: str | None = None):
    async with SessionLocal() as session:
        store = SettingsStore(session)
        timezone = await _get_timezone(store)
        today = dt.datetime.now(timezone).date()

        pair = None
        local_error = None
        try:
            provider = await build_provider(store)
            word_service = WordService(session, provider)
            pair = await word_service.ensure_pair_for_date(today)
        except (LLMProviderError, WordGenerationError) as exc:
            local_error = str(exc)

        result = await session.execute(
            select(JournalEntry, WordPair)
            .join(WordPair, JournalEntry.pair_id == WordPair.id, isouter=True)
            .order_by(JournalEntry.date.desc())
        )
        journal_rows = result.all()

        formatted_pair = None
        if pair:
            formatted_pair = format_pair_display(pair.word_a, pair.word_b)

        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "today": today,
                "pair": formatted_pair,
                "error": error or local_error,
                "journal_rows": journal_rows,
            },
        )


@app.post("/journal")
async def create_journal(
    request: Request,
    note: str = Form(...),
    entry_date: str = Form("")
):
    async with SessionLocal() as session:
        store = SettingsStore(session)
        timezone = await _get_timezone(store)
        date_value = dt.datetime.now(timezone).date()
        if entry_date:
            try:
                date_value = dt.date.fromisoformat(entry_date)
            except ValueError:
                pass
        try:
            provider = await build_provider(store)
            word_service = WordService(session, provider)
            pair = await word_service.ensure_pair_for_date(date_value)
        except (LLMProviderError, WordGenerationError) as exc:
            return RedirectResponse(url=f"/?error={quote_plus(str(exc))}", status_code=303)

        result = await session.execute(select(JournalEntry).where(JournalEntry.date == date_value))
        entry = result.scalar_one_or_none()
        if entry:
            entry.note = note
            entry.pair_id = pair.id
        else:
            entry = JournalEntry(date=date_value, note=note, pair_id=pair.id)
            session.add(entry)

        await session.commit()

    return RedirectResponse(url="/", status_code=303)


@app.get("/settings")
async def settings_page(request: Request):
    async with SessionLocal() as session:
        store = SettingsStore(session)
        context = {
            "request": request,
            "app_timezone": await store.get_str("app_timezone", env_settings.app_timezone),
            "send_hour": await store.get_int("send_hour", env_settings.send_hour),
            "send_minute": await store.get_int("send_minute", env_settings.send_minute),
            "openai_model": await store.get_str("openai_model", env_settings.openai_model),
            "apns_key_id": await store.get_str("apns_key_id", env_settings.apns_key_id),
            "apns_team_id": await store.get_str("apns_team_id", env_settings.apns_team_id),
            "apns_bundle_id": await store.get_str("apns_bundle_id", env_settings.apns_bundle_id),
            "apns_use_sandbox": await store.get_bool("apns_use_sandbox", env_settings.apns_use_sandbox),
        }
        return templates.TemplateResponse("settings.html", context)


@app.post("/settings")
async def update_settings(
    request: Request,
    app_timezone: str = Form(""),
    send_hour: str = Form(""),
    send_minute: str = Form(""),
    openai_api_key: str = Form(""),
    openai_model: str = Form(""),
    apns_key_id: str = Form(""),
    apns_team_id: str = Form(""),
    apns_bundle_id: str = Form(""),
    apns_auth_key: str = Form(""),
    apns_use_sandbox: str | None = Form(None),
):
    async with SessionLocal() as session:
        store = SettingsStore(session)

        if app_timezone:
            await store.set_value("app_timezone", app_timezone.strip())
        if send_hour:
            await store.set_value("send_hour", send_hour.strip())
        if send_minute:
            await store.set_value("send_minute", send_minute.strip())

        if openai_api_key:
            await store.set_value("openai_api_key", openai_api_key.strip())
        if openai_model:
            await store.set_value("openai_model", openai_model.strip())

        if apns_key_id:
            await store.set_value("apns_key_id", apns_key_id.strip())
        if apns_team_id:
            await store.set_value("apns_team_id", apns_team_id.strip())
        if apns_bundle_id:
            await store.set_value("apns_bundle_id", apns_bundle_id.strip())
        if apns_auth_key:
            await store.set_value("apns_auth_key", apns_auth_key.strip())

        await store.set_value("apns_use_sandbox", "true" if apns_use_sandbox else "false")

        await session.commit()

    return RedirectResponse(url="/settings", status_code=303)


@app.post("/send-now")
async def send_now():
    await _run_daily()
    return RedirectResponse(url="/", status_code=303)


# ---------------------------------------------------------------------------
# iOS API
# ---------------------------------------------------------------------------

@app.get("/api/word-of-day", response_model=WordPairResponse)
async def api_word_of_day():
    async with SessionLocal() as session:
        store = SettingsStore(session)
        timezone = await _get_timezone(store)
        today = dt.datetime.now(timezone).date()
        provider = await build_provider(store)
        word_service = WordService(session, provider)
        pair = await word_service.ensure_pair_for_date(today)
        definition_service = DefinitionService(session, provider)
        word_a_definition = await definition_service.get_definition(pair.word_a)
        word_b_definition = await definition_service.get_definition(pair.word_b)
        return WordPairResponse(
            date=pair.date,
            word_a=pair.word_a,
            word_b=pair.word_b,
            word_a_definition=word_a_definition,
            word_b_definition=word_b_definition,
        )


@app.get("/api/history", response_model=HistoryResponse)
async def api_history(days: int = 30):
    days = max(1, min(days, 365))
    async with SessionLocal() as session:
        result = await session.execute(
            select(WordPair).order_by(WordPair.date.desc()).limit(days)
        )
        items = [
            WordPairResponse(date=row.date, word_a=row.word_a, word_b=row.word_b)
            for row in result.scalars().all()
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
