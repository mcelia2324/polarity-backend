from __future__ import annotations

import datetime as dt
import logging
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models import Delivery
from app.services.llm import build_provider
from app.services.push.apns import APNSClient
from app.services.settings_store import SettingsStore
from app.services.llm import LLMProviderError
from app.services.word_service import WordGenerationError, WordService, format_pair_display

logger = logging.getLogger(__name__)


class SchedulerService:
    def __init__(self, session_factory: async_sessionmaker):
        self._session_factory = session_factory
        self._scheduler = AsyncIOScheduler()

    async def start(self) -> None:
        await self.reschedule()
        self._scheduler.start()

    async def shutdown(self) -> None:
        self._scheduler.shutdown(wait=False)

    async def reschedule(self) -> None:
        if self._scheduler.get_job("daily_send"):
            self._scheduler.remove_job("daily_send")

        async with self._session_factory() as session:
            settings_store = SettingsStore(session)
            hour = await settings_store.get_int("send_hour", 8) or 8
            minute = await settings_store.get_int("send_minute", 0) or 0
            tz_name = await settings_store.get_str("app_timezone", "UTC") or "UTC"

        try:
            timezone = ZoneInfo(tz_name)
        except Exception:
            logger.warning("Invalid timezone '%s', falling back to UTC.", tz_name)
            timezone = ZoneInfo("UTC")

        trigger = CronTrigger(hour=hour, minute=minute, timezone=timezone)
        self._scheduler.add_job(self.run_daily, trigger=trigger, id="daily_send", replace_existing=True)
        logger.info("Scheduled daily send at %02d:%02d %s", hour, minute, timezone.key)

    async def run_daily(self) -> None:
        async with self._session_factory() as session:
            settings_store = SettingsStore(session)
            tz_name = await settings_store.get_str("app_timezone", "UTC") or "UTC"
            try:
                timezone = ZoneInfo(tz_name)
            except Exception:
                timezone = ZoneInfo("UTC")
            today = dt.datetime.now(timezone).date()
            try:
                provider = await build_provider(settings_store)
                word_service = WordService(session, provider)
                pair = await word_service.ensure_pair_for_date(today)
            except (LLMProviderError, WordGenerationError) as exc:
                logger.error("Daily generation failed: %s", exc)
                return

            message = (
                f"Polarity for {today.strftime('%B %d, %Y')}:\n"
                f"{format_pair_display(pair.word_a, pair.word_b)}\n"
                "Reflect on the meanings, differences, and which calibrates higher."
            )

            apns_client = await APNSClient.from_settings(settings_store)
            if apns_client is None:
                logger.info("APNs not configured; skipping push notifications.")
                return

            if await self._already_delivered(session, today, "apns"):
                return

            try:
                sent_count, failed = await apns_client.send_daily(pair, today, message, session)
                status = "sent" if failed == 0 else "partial"
                await self._record_delivery(
                    session,
                    today,
                    "apns",
                    status,
                    None if failed == 0 else f"{failed} failed",
                )
                logger.info("APNs sent: %d, failed: %d", sent_count, failed)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Failed sending via apns")
                await self._record_delivery(session, today, "apns", "failed", str(exc))

    async def _already_delivered(self, session, date: dt.date, channel: str) -> bool:
        result = await session.execute(
            select(Delivery).where(
                Delivery.date == date,
                Delivery.channel == channel,
                Delivery.status == "sent",
            )
        )
        return result.scalar_one_or_none() is not None

    async def _record_delivery(self, session, date: dt.date, channel: str, status: str, error: str | None) -> None:
        result = await session.execute(
            select(Delivery).where(Delivery.date == date, Delivery.channel == channel)
        )
        delivery = result.scalar_one_or_none()
        if delivery is None:
            delivery = Delivery(date=date, channel=channel, status=status, error=error)
            session.add(delivery)
        else:
            delivery.status = status
            delivery.error = error
        await session.commit()
