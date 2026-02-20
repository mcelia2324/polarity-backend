from __future__ import annotations

from app.services.llm.openai_provider import OpenAIProvider
from app.services.settings_store import SettingsStore


async def build_provider(settings_store: SettingsStore):
    api_key = await settings_store.get_str("openai_api_key")
    model = await settings_store.get_str("openai_model", "gpt-5.2")
    return OpenAIProvider(api_key=api_key or "", model=model or "gpt-5.2")
