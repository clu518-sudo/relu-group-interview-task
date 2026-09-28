"""Pydantic models for the event pipeline.

Two schema families live here:

* the **output** models (``Event`` and friends) mirror the JSON schema agreed in
  DECISIONS.md #4 and are what gets written to ``events_oct_2026.json``;
* the **wire** models (``RawEvent``, ``Enrichment``) are what the Gemini calls
  produce. They are deliberately flatter and more forgiving, because a model is
  far more reliable at filling a shallow schema than a deeply nested one.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    field_validator,
    model_validator,
)

SCHEMA_VERSION = "1.0"

# Shares are allowed to miss 1.0 by this much before we treat them as unusable.
ORIGIN_SUM_TOLERANCE = 0.05


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


Timestamp = Annotated[datetime, PlainSerializer(_as_z, return_type=str)]


# --------------------------------------------------------------------------
# Controlled vocabularies (DECISIONS.md #4)
# --------------------------------------------------------------------------


class Category(str, Enum):
    trade_fair = "Trade Fair"
    conference = "Conference"
    festival = "Festival"
    concert = "Concert"
    sports = "Sports"
    cultural = "Cultural"
    community = "Community"
    other = "Other"


class ProfessionalLevel(str, Enum):
    student = "student"
    junior = "junior"
    mid = "mid"
    senior = "senior"
    executive = "executive"
    investor = "investor"
    mixed = "mixed"
    unknown = "unknown"


class GroupType(str, Enum):
    families = "families"
    solo_professionals = "solo_professionals"
    corporate_teams = "corporate_teams"
    students = "students"
    tourists = "tourists"
    couples = "couples"
    young_adults = "young_adults"
    unknown = "unknown"


class GenderSkew(str, Enum):
    male_leaning = "male-leaning"
    female_leaning = "female-leaning"
    balanced = "balanced"
    unknown = "unknown"


class SourceType(str, Enum):
    official_site = "official_site"
    ticketing_platform = "ticketing_platform"
    news = "news"
    search_grounding = "search_grounding"
    other = "other"


class RejectionStage(str, Enum):
    discovery_validation = "discovery_validation"
    final_validation = "final_validation"


class RejectionReason(str, Enum):
    missing_name = "missing_name"
    missing_date = "missing_date"
    missing_venue = "missing_venue"
    missing_sources = "missing_sources"
    out_of_range_date = "out_of_range_date"
    invalid_geographic_origins_sum = "invalid_geographic_origins_sum"
    invalid_age_range = "invalid_age_range"
    api_error = "api_error"
    fetch_failed = "fetch_failed"
    other = "other"


# --------------------------------------------------------------------------
# Output schema
# --------------------------------------------------------------------------


class DateRange(BaseModel):
    start: date
    end: date | None = None

    @model_validator(mode="after")
    def _end_after_start(self) -> DateRange:
        if self.end is not None and self.end < self.start:
            raise ValueError("date.end is before date.start")
        return self


class Venue(BaseModel):
    name: str = Field(min_length=1)
    address: str | None = None
    city: str
    country: str


class Source(BaseModel):
    title: str
    url: str
    type: SourceType
    retrieved_at: Timestamp


class AgeRange(BaseModel):
    min: int = Field(ge=0, le=120)
    max: int = Field(ge=0, le=120)

    @model_validator(mode="after")
    def _ordered(self) -> AgeRange:
        if self.min > self.max:
            raise ValueError("age_range.min is greater than age_range.max")
        return self


class Demographics(BaseModel):
    age_range: AgeRange | None = None
    professional_level: list[ProfessionalLevel] = Field(default_factory=list)
    group_types: list[GroupType] = Field(default_factory=list)
    gender_skew: GenderSkew = GenderSkew.unknown
    notes: str = ""


class AudienceProfile(BaseModel):
    interests: list[str] = Field(default_factory=list)
    industries: list[str] = Field(default_factory=list)
    demographics: Demographics = Field(default_factory=Demographics)


class GeographicOrigins(BaseModel):
    """Shares of attendees by origin. Either all three are set, or all are null."""

    local: float | None = Field(default=None, ge=0.0, le=1.0)
    national: float | None = Field(default=None, ge=0.0, le=1.0)
    international: float | None = Field(default=None, ge=0.0, le=1.0)
    top_countries: list[str] = Field(default_factory=list)
    notes: str = ""

    @model_validator(mode="after")
    def _shares_consistent(self) -> GeographicOrigins:
        shares = [self.local, self.national, self.international]
        if all(s is None for s in shares):
            return self
        if any(s is None for s in shares):
            raise ValueError("geographic_origins shares must be all set or all null")
        total = sum(shares)  # type: ignore[arg-type]
        if abs(total - 1.0) > ORIGIN_SUM_TOLERANCE:
            raise ValueError(f"geographic_origins shares sum to {total:.2f}, not ~1.0")
        return self


class DataQuality(BaseModel):
    confidence: float = Field(ge=0.0, le=1.0)
    fields_estimated: list[str] = Field(default_factory=list)
    fields_unknown: list[str] = Field(default_factory=list)


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str = Field(min_length=1)
    category: Category
    description: str
    date: DateRange
    venue: Venue
    estimated_participants: int | None = Field(default=None, ge=0)
    audience_profile: AudienceProfile
    geographic_origins: GeographicOrigins
    sources: list[Source] = Field(min_length=1)
    data_quality: DataQuality


class Meta(BaseModel):
    city: str
    country: str
    month: str
    generated_at: Timestamp
    model: str
    max_events: int
    event_count: int
    schema_version: str = SCHEMA_VERSION


class EventsFile(BaseModel):
    meta: Meta
    events: list[Event]


# --------------------------------------------------------------------------
# Rejection log (DECISIONS.md #8)
# --------------------------------------------------------------------------


class RejectedEvent(BaseModel):
    name: str
    stage: RejectionStage
    reason: RejectionReason
    reason_detail: str
    raw_snapshot: dict[str, Any]
    rejected_at: Timestamp


class RejectedMeta(BaseModel):
    city: str
    month: str
    generated_at: Timestamp
    rejected_count: int


class RejectedFile(BaseModel):
    meta: RejectedMeta
    rejected: list[RejectedEvent]


# --------------------------------------------------------------------------
# Wire schemas
# --------------------------------------------------------------------------


class RawSource(BaseModel):
    """A source as it comes back from discovery, before code stamps metadata."""

    title: str = ""
    url: str
    type: SourceType = SourceType.other


class RawEvent(BaseModel):
    """One discovery candidate. Dates stay strings until normalization."""

    name: str = ""
    start_date: str | None = None
    end_date: str | None = None
    venue_name: str | None = None
    venue_address: str | None = None
    category: str | None = None
    description: str = ""
    sources: list[RawSource] = Field(default_factory=list)

    # Stamped by the pipeline after dedupe, not by the model.
    id: str | None = None

    @field_validator("start_date", "end_date", "venue_name", "venue_address", "category", mode="before")
    @classmethod
    def _blank_to_none(cls, value: Any) -> Any:
        if isinstance(value, str) and value.strip().lower() in {"", "unknown", "null", "n/a", "none"}:
            return None
        return value


class Enrichment(BaseModel):
    """Flat schema handed to Gemini as ``response_schema`` for the enrichment call.

    Flat on purpose: nullable nested objects are the part of structured output
    that models get wrong most often, so the nesting is rebuilt in code.
    """

    category: Category
    description: str
    estimated_participants: int | None = None
    interests: list[str] = Field(default_factory=list)
    industries: list[str] = Field(default_factory=list)
    age_min: int | None = None
    age_max: int | None = None
    professional_level: list[ProfessionalLevel] = Field(default_factory=list)
    group_types: list[GroupType] = Field(default_factory=list)
    gender_skew: GenderSkew = GenderSkew.unknown
    demographics_notes: str = ""
    local_share: float | None = None
    national_share: float | None = None
    international_share: float | None = None
    top_countries: list[str] = Field(default_factory=list)
    origins_notes: str = ""
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
