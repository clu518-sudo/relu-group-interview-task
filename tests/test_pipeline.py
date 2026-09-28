"""Tests for the free stages: normalisation, dedupe, validation, assembly.

These are the stages that run on every re-run, so they are worth pinning down
without spending any API budget.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.config import Config
from src.dedupe import dedupe, is_same_event, normalize, normalize_name
from src.enrichment import EnrichmentOutcome
from src.models import (
    Category,
    Enrichment,
    GenderSkew,
    GroupType,
    ProfessionalLevel,
    RawEvent,
    RawSource,
    RejectionReason,
    SourceType,
)
from src.pipeline import build_event, check_tier1, event_id


@pytest.fixture
def config() -> Config:
    return Config(
        city="Munich",
        country="Germany",
        month="2026-10",
        max_events=15,
        use_cache=True,
        output_path=Path("events.json"),
        rejected_path=Path("rejected.json"),
        cache_dir=Path("cache"),
        prompt_dir=Path("prompts"),
        prompt_version="v1",
        model="gemini-2.5-flash",
        api_key="test",
        verbose=False,
    )


def raw(**overrides) -> RawEvent:
    base = dict(
        name="EXPO REAL 2026",
        start_date="2026-10-05",
        end_date="2026-10-07",
        venue_name="Messe Munchen",
        sources=[RawSource(title="Official", url="https://exporeal.net/", type=SourceType.official_site)],
    )
    return RawEvent(**{**base, **overrides})


def enrichment(**overrides) -> Enrichment:
    base = dict(
        category=Category.trade_fair,
        description="A property trade fair.",
        estimated_participants=40000,
        interests=["Real Estate", "investment"],
        industries=["real estate"],
        age_min=30,
        age_max=60,
        professional_level=[ProfessionalLevel.senior, ProfessionalLevel.unknown],
        group_types=[GroupType.corporate_teams],
        gender_skew=GenderSkew.male_leaning,
        local_share=0.2,
        national_share=0.35,
        international_share=0.45,
        top_countries=["Germany", "Austria"],
        confidence=0.8,
    )
    return Enrichment(**{**base, **overrides})


def outcome(data: Enrichment, page_fetched: bool = True) -> EnrichmentOutcome:
    return EnrichmentOutcome(data=data, cache_hit=False, page_fetched=page_fetched)


# --- normalisation and dedupe ---------------------------------------------


def test_normalize_name_strips_year_and_punctuation():
    assert normalize_name("EXPO REAL 2026 - Trade Fair") == "expo real trade fair"


def test_normalize_parses_german_dates():
    cleaned = normalize([raw(start_date="05.10.2026", end_date=None)])
    assert cleaned[0].start_date == "2026-10-05"


def test_normalize_swaps_reversed_range():
    cleaned = normalize([raw(start_date="2026-10-07", end_date="2026-10-05")])
    assert (cleaned[0].start_date, cleaned[0].end_date) == ("2026-10-05", "2026-10-07")


def test_subtitle_variant_is_the_same_event():
    a = raw(name="EXPO REAL")
    b = raw(name="EXPO REAL - International Trade Fair for Property and Investment")
    assert is_same_event(a, b)


def test_same_name_different_dates_is_not_a_duplicate():
    a = raw(start_date="2026-10-05", end_date="2026-10-07")
    b = raw(start_date="2026-10-20", end_date="2026-10-22")
    assert not is_same_event(a, b)


def test_dedupe_merges_sources_and_keeps_richest_fields():
    a = raw(name="EXPO REAL", venue_name="Messe", venue_address=None)
    b = raw(
        name="EXPO REAL - International Trade Fair",
        venue_name="Messe Munchen",
        venue_address="Am Messesee 2",
        sources=[RawSource(title="News", url="https://news.example/expo", type=SourceType.news)],
    )
    merged = dedupe([a, b])
    assert len(merged) == 1
    assert merged[0].name == "EXPO REAL - International Trade Fair"
    assert merged[0].venue_address == "Am Messesee 2"
    assert {s.url for s in merged[0].sources} == {"https://exporeal.net/", "https://news.example/expo"}


def test_event_id_is_stable_across_title_variants():
    assert event_id(raw(name="EXPO REAL 2026"), "Munich") == event_id(raw(name="expo real!"), "Munich")


# --- Tier-1 gate ----------------------------------------------------------


def test_tier1_accepts_a_complete_candidate():
    assert check_tier1(raw(), "2026-10") is None


@pytest.mark.parametrize(
    "overrides, expected",
    [
        ({"name": ""}, RejectionReason.missing_name),
        ({"start_date": None}, RejectionReason.missing_date),
        ({"venue_name": None}, RejectionReason.missing_venue),
        ({"sources": []}, RejectionReason.missing_sources),
        ({"start_date": "2026-11-05", "end_date": "2026-11-07"}, RejectionReason.out_of_range_date),
    ],
)
def test_tier1_rejects_with_the_right_reason(overrides, expected):
    failure = check_tier1(raw(**overrides), "2026-10")
    assert failure is not None and failure[0] is expected


def test_tier1_accepts_an_event_spanning_into_the_month():
    assert check_tier1(raw(start_date="2026-09-20", end_date="2026-10-04"), "2026-10") is None


# --- assembly -------------------------------------------------------------


def test_build_event_nests_the_flat_enrichment(config):
    event = build_event(raw(), outcome(enrichment()), config)
    assert event.category is Category.trade_fair
    assert event.audience_profile.demographics.age_range.max == 60
    assert event.geographic_origins.international == 0.45
    assert event.venue.city == "Munich"
    assert event.sources[0].type is SourceType.official_site


def test_build_event_drops_reserved_unknown_when_real_values_exist(config):
    event = build_event(raw(), outcome(enrichment()), config)
    assert event.audience_profile.demographics.professional_level == [ProfessionalLevel.senior]


def test_build_event_deduplicates_and_lowercases_interests(config):
    event = build_event(raw(), outcome(enrichment(interests=["Real Estate", "real estate", " "])), config)
    assert event.audience_profile.interests == ["real estate"]


def test_inconsistent_origin_shares_degrade_to_null_instead_of_dropping(config):
    event = build_event(
        raw(),
        outcome(enrichment(local_share=0.2, national_share=0.2, international_share=0.2)),
        config,
    )
    assert event.geographic_origins.local is None
    assert "geographic_origins" in event.data_quality.fields_unknown


def test_missing_fields_lower_confidence_and_are_listed(config):
    thin = enrichment(
        estimated_participants=None,
        interests=[],
        industries=[],
        age_min=None,
        age_max=None,
        local_share=None,
        national_share=None,
        international_share=None,
        top_countries=[],
        gender_skew=GenderSkew.unknown,
        professional_level=[ProfessionalLevel.unknown],
        confidence=0.8,
    )
    event = build_event(raw(), outcome(thin, page_fetched=False), config)
    assert event.data_quality.confidence < 0.4
    assert "estimated_participants" in event.data_quality.fields_unknown
    assert event.data_quality.fields_estimated == []


def test_single_day_event_has_no_end_date(config):
    event = build_event(raw(start_date="2026-10-05", end_date="2026-10-05"), outcome(enrichment()), config)
    assert event.date.end is None
