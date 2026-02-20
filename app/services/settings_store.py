from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings as env_settings
from app.models import Setting


class SettingsStore:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def get_db_value(self, key: str) -> str | None:
        result = await self._session.execute(select(Setting).where(Setting.key == key))
        row = result.scalar_one_or_none()
        return None if row is None else row.value

    async def get_raw(self, key: str) -> Any:
        row = await self.get_db_value(key)
        if row is not None:
            return row
        return getattr(env_settings, key, None)

    async def get_str(self, key: str, default: str | None = None) -> str | None:
        value = await self.get_raw(key)
        if value is None:
            return default
        if isinstance(value, str):
            return value
        return str(value)

    async def get_bool(self, key: str, default: bool | None = None) -> bool | None:
        value = await self.get_raw(key)
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    async def get_int(self, key: str, default: int | None = None) -> int | None:
        value = await self.get_raw(key)
        if value is None:
            return default
        if isinstance(value, int):
            return value
        try:
            return int(str(value).strip())
        except ValueError:
            return default

    async def set_value(self, key: str, value: str) -> None:
        result = await self._session.execute(select(Setting).where(Setting.key == key))
        row = result.scalar_one_or_none()
        if row is None:
            row = Setting(key=key, value=value)
            self._session.add(row)
        else:
            row.value = value

    async def seed_from_env(self, keys: list[str]) -> None:
        for key in keys:
            env_value = getattr(env_settings, key, None)
            if env_value is None:
                continue
            existing = await self.get_db_value(key)
            if existing is None:
                await self.set_value(key, str(env_value))
