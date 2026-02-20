from __future__ import annotations

import datetime as dt
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx
import jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DeviceToken, WordPair
from app.services.settings_store import SettingsStore

logger = logging.getLogger(__name__)


@dataclass
class APNSConfig:
    key_id: str
    team_id: str
    bundle_id: str
    auth_key: str
    use_sandbox: bool


class APNSClient:
    def __init__(self, config: APNSConfig):
        self._config = config
        self._jwt_token: str | None = None
        self._jwt_expiry: float = 0

    @classmethod
    async def from_settings(cls, settings_store: SettingsStore) -> "APNSClient | None":
        key_id = await settings_store.get_str("apns_key_id")
        team_id = await settings_store.get_str("apns_team_id")
        bundle_id = await settings_store.get_str("apns_bundle_id")
        auth_key = await settings_store.get_str("apns_auth_key")
        use_sandbox = await settings_store.get_bool("apns_use_sandbox", True)

        # Try Secret Manager volume mount if auth_key not in DB
        if not auth_key:
            auth_key = cls._try_volume_key()

        if not key_id or not team_id or not bundle_id or not auth_key:
            return None

        return cls(APNSConfig(
            key_id=key_id,
            team_id=team_id,
            bundle_id=bundle_id,
            auth_key=auth_key,
            use_sandbox=bool(use_sandbox),
        ))

    def _load_private_key(self) -> str:
        auth_key = self._config.auth_key.strip()
        if auth_key.startswith("-----BEGIN PRIVATE KEY-----"):
            return auth_key
        # Treat as file path if not inline.
        with open(auth_key, "r", encoding="utf-8") as handle:
            return handle.read()

    @staticmethod
    def _try_volume_key() -> str | None:
        """Try reading APNs key from Secret Manager volume mount."""
        path = "/secrets/apns/apns_key.p8"
        try:
            with open(path, "r", encoding="utf-8") as f:
                key = f.read().strip()
                if key:
                    return key
        except FileNotFoundError:
            pass
        return None

    def _get_jwt(self) -> str:
        now = int(time.time())
        if self._jwt_token and now < self._jwt_expiry:
            return self._jwt_token

        private_key = self._load_private_key()
        token = jwt.encode(
            {
                "iss": self._config.team_id,
                "iat": now,
            },
            private_key,
            algorithm="ES256",
            headers={"kid": self._config.key_id},
        )
        # APNs tokens are valid for 60 minutes; refresh slightly early.
        self._jwt_token = token
        self._jwt_expiry = now + 50 * 60
        return token

    def _endpoint(self) -> str:
        host = "https://api.sandbox.push.apple.com" if self._config.use_sandbox else "https://api.push.apple.com"
        return host

    async def send_daily(
        self,
        pair: WordPair,
        date: dt.date,
        message: str,
        session: AsyncSession,
    ) -> tuple[int, int]:
        result = await session.execute(select(DeviceToken).where(DeviceToken.enabled == True))
        tokens = result.scalars().all()
        if not tokens:
            return 0, 0

        payload = {
            "aps": {
                "alert": {
                    "title": "Polarity",
                    "body": message,
                },
                "sound": "default",
            },
            "date": date.isoformat(),
            "word_a": pair.word_a,
            "word_b": pair.word_b,
        }

        headers = {
            "apns-topic": self._config.bundle_id,
            "authorization": f"bearer {self._get_jwt()}",
            "apns-push-type": "alert",
        }

        sent = 0
        failed = 0
        async with httpx.AsyncClient(http2=True, timeout=20) as client:
            for token in tokens:
                url = f"{self._endpoint()}/3/device/{token.token}"
                resp = await client.post(url, json=payload, headers=headers)
                if resp.status_code == 200:
                    sent += 1
                    token.last_notified_at = dt.datetime.utcnow()
                else:
                    failed += 1
                    token.last_error = resp.text
                    logger.warning("APNs send failed for token %s: %s", token.token[-6:], resp.text)

        await session.commit()
        return sent, failed
