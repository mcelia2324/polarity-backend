from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, Field


class PromptsOut(BaseModel):
    notice: str
    turn: str
    rise: str


class MeaningOut(BaseModel):
    teaching: str
    disguise: str
    practice: str
    prompts: PromptsOut
    push_line: str


class WordPairResponse(BaseModel):
    # The shipped 2.1 app decodes exactly these eight keys: keep their names and types unchanged.
    date: dt.date
    word_a: str
    word_b: str
    word_a_definition: str | None = None
    word_b_definition: str | None = None
    quote: str | None = None
    quote_author: str | None = None
    contemplation: str | None = None

    # v3 additions. Additive only: 2.1 ignores keys it does not know. word_a is always the higher
    # word and word_b the lower one, so these repeat them under clearer names.
    higher: str | None = None
    lower: str | None = None
    pair_key: str | None = None  # the two words sorted and joined: "courage|fear"
    axis: str | None = None
    lower_level: str | None = None  # level names only, never numbers
    higher_level: str | None = None
    quote_source: str | None = None  # the work the quote comes from
    meaning: MeaningOut | None = None
    previous_dates: list[dt.date] = Field(default_factory=list)


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
