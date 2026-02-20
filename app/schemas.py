from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, Field


class WordPairResponse(BaseModel):
    date: dt.date
    word_a: str
    word_b: str
    word_a_definition: str | None = None
    word_b_definition: str | None = None


class HistoryResponse(BaseModel):
    items: list[WordPairResponse]


class DeviceRegisterRequest(BaseModel):
    token: str = Field(..., min_length=16)
    platform: str = "ios"
    timezone: str | None = None
    enabled: bool = True
    notify_hour: int | None = None
    notify_minute: int | None = None


class DeviceToggleRequest(BaseModel):
    token: str = Field(..., min_length=16)
    enabled: bool
