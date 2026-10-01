from __future__ import annotations

import datetime as dt
import json
import logging
import os
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

# APNs reasons meaning the device token itself is dead (app uninstalled, token malformed or
# from the other APNs environment), as opposed to a problem with our request or credentials.
_INVALID_TOKEN_REASONS = {"Unregistered", "BadDeviceToken"}


def is_invalid_token_response(status_code: int, body: str) -> bool:
    """True when APNs rejected the token itself: any 410 (Unregistered / ExpiredToken), or a
    reason of Unregistered / BadDeviceToken. Retrying such a token is pointless; it only becomes
    usable again if the device re-registers it (POST /api/devices/register re-enables the row)."""
    if status_code == 410:
        return True
    try:
        reason = json.loads(body).get("reason")
    except (ValueError, AttributeError):
        return False
    return reason in _INVALID_TOKEN_REASONS


@dataclass
class APNSConfig:
    key_id: str
    team_id: str
    bundle_id: str
    auth_key: str
    use_sandbox: bool


class APNSClient:
    def __init__(self, config: APNSConfig, transport: httpx.AsyncBaseTransport | None = None):
        self._config = config
        self._transport = transport  # None = real network; tests pass an httpx.MockTransport
        self._jwt_token: str | None = None
        self._jwt_expiry: float = 0

    @classmethod
    async def from_settings(cls, settings_store: SettingsStore) -> "APNSClient | None":
        key_id = await settings_store.get_str("apns_key_id")
        team_id = await settings_store.get_str("apns_team_id")
        bundle_id = await settings_store.get_str("apns_bundle_id")
        inline = await settings_store.get_str("apns_auth_key")
        use_sandbox = await settings_store.get_bool("apns_use_sandbox", True)

        auth_key = cls._resolve_auth_key(inline)

        if not key_id or not team_id or not bundle_id or not auth_key:
            return None

        return cls(APNSConfig(
            key_id=key_id,
            team_id=team_id,
            bundle_id=bundle_id,
            auth_key=auth_key,
            use_sandbox=bool(use_sandbox),
        ))

    @classmethod
    def _resolve_auth_key(cls, inline: str | None) -> str | None:
        """Pick a usable auth key, or None if there isn't a real one. An inline setting is
        honored only when it is an actual PEM or an existing file path. The mounted secret
        is honored only when its content is a real PEM. A placeholder (e.g. the literal
        string "PLACEHOLDER", used to scaffold the secret before the real key is uploaded)
        is treated as absent, so push degrades to 'not configured' instead of crashing."""
        if inline:
            candidate = inline.strip()
            if candidate.startswith("-----BEGIN") or os.path.exists(candidate):
                return candidate
        volume = cls._try_volume_key()
        if volume and volume.strip().startswith("-----BEGIN"):
            return volume
        return None

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
    ) -> tuple[int, int, int]:
        """Push to every enabled token. Returns (sent, failed, disabled).

        A token APNs reports as dead is disabled (row and last_error kept for audit) so it is
        skipped from then on. It counts toward `disabled`, not `failed`, so `failed` covers only
        tokens that are still active and the delivery status reflects real failures."""
        result = await session.execute(select(DeviceToken).where(DeviceToken.enabled == True))
        tokens = result.scalars().all()
        if not tokens:
            return 0, 0, 0

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
        disabled = 0
        async with httpx.AsyncClient(http2=True, timeout=20, transport=self._transport) as client:
            for token in tokens:
                url = f"{self._endpoint()}/3/device/{token.token}"
                resp = await client.post(url, json=payload, headers=headers)
                if resp.status_code == 200:
                    sent += 1
                    token.last_notified_at = dt.datetime.utcnow()
                elif is_invalid_token_response(resp.status_code, resp.text):
                    disabled += 1
                    token.enabled = False
                    token.last_error = resp.text
                    logger.info("APNs rejected token %s as invalid, disabled it: %s", token.token[-6:], resp.text)
                else:
                    failed += 1
                    token.last_error = resp.text
                    logger.warning("APNs send failed for token %s: %s", token.token[-6:], resp.text)

        await session.commit()
        return sent, failed, disabled
