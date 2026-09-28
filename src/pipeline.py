"""
Stages 1-6 of the pipeline (DECISIONS.md #7).

Paid stages (1 discovery, 4 enrichment) are cache-backed; everything else is
pure code and free to re-run. Two validation gates, at stage 3 and stage 5, so
a discovery-side gap and an enrichment-side gap are logged distinctly.
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from datetime import date

from google import genai
from pydantic import ValidationError

from .cache import JsonCache
from .config import Config, load_config
from .dedupe import dedupe, normalize, normalize_name, parse_date
from .enrichment import Enricher, EnrichmentError, EnrichmentOutcome
from .fetch import make_client
from .models import (
    AgeRange,
    AudienceProfile,
    Category,
    DataQuality,
    DateRange,
    Demographics,
    Enrichment,
    Event,
    EventsFile,
    GenderSkew,
    GeographicOrigins,
    GroupType,
    Meta,
    ProfessionalLevel,
    RawEvent,
    RejectedEvent,
    RejectedFile,
    RejectedMeta,
    RejectionReason,
    RejectionStage,
    Source,
    Venue,
    utc_now,
)
from .prompts import PromptLibrary
from .sources import GeminiGroundingSource

log = logging.getLogger("pipeline")

# Fixed namespace so the same event keeps the same id across runs, which is
# what makes the per-event enrichment cache worth having.
ID_NAMESPACE = uuid.UUID("6f3b1f0a-6c4e-5a8d-9b2f-7c1d4e2a9b30")

# The model's own confidence is only a starting point; it does not know how much
# of its answer was guesswork. These deductions apply what the code can observe.
UNKNOWN_PENALTY = 0.05  # per Tier-2 field that fell back to unknown/null
NO_PAGE_PENALTY = 0.15  # enrichment ran on the prompt alone, with no source page
CONFIDENCE_FLOOR = 0.05
CONFIDENCE_CEILING = 0.95  # never claim certainty: every figure here is an estimate


# --------------------------------------------------------------------------
# Stage 2: identity
# --------------------------------------------------------------------------


def event_id(event: RawEvent, city: str) -> str:
    """Derive the stable event id from the name, start date and city.

    Stamped after dedupe so the seed comes from the merged record. The name is
    normalised first, so the same event found under a slightly different title
    still hashes to the same id.
    """
    seed = f"{normalize_name(event.name)}|{event.start_date or ''}|{city.lower()}"
    return str(uuid.uuid5(ID_NAMESPACE, seed))


# --------------------------------------------------------------------------
# Stage 3: Tier-1 gate
# --------------------------------------------------------------------------


def check_tier1(event: RawEvent, month: str) -> tuple[RejectionReason, str] | None:
    """Return the reason this candidate cannot be kept, or None if it passes.

    Tier-1 fields are the ones an event cannot be published without: without
    them the row is unusable downstream, so it is dropped rather than degraded
    (DECISIONS.md #4, field tiers).
    """
    if not event.name.strip():
        return RejectionReason.missing_name, "candidate has no name"
    if not event.start_date:
        return RejectionReason.missing_date, "date.start could not be confirmed from any source"
    if not event.venue_name:
        return RejectionReason.missing_venue, "no venue name found"
    if not event.sources:
        return RejectionReason.missing_sources, "no evidence URL found"

    start = parse_date(event.start_date)
    if start is None:
        return RejectionReason.missing_date, f"unparseable start date {event.start_date!r}"
    # Grounding regularly returns the right event from the wrong year or month,
    # so the requested month is enforced here rather than trusted.
    end = parse_date(event.end_date) or start
    if not _overlaps_month(start, end, month):
        return (
            RejectionReason.out_of_range_date,
            f"runs {start.isoformat()} to {end.isoformat()}, outside {month}",
        )
    return None


def _overlaps_month(start: date, end: date, month: str) -> bool:
    """True if a run from ``start`` to ``end`` covers any part of ``month``.

    ``YYYY-MM`` strings sort chronologically, so comparing them directly avoids
    having to build the month's calendar boundaries.
    """
    return start.strftime("%Y-%m") <= month <= end.strftime("%Y-%m")


# --------------------------------------------------------------------------
# Stage 5: assembly
# --------------------------------------------------------------------------


def build_event(raw: RawEvent, outcome: EnrichmentOutcome, config: Config) -> Event:
    """Merge the candidate and its enrichment into a validated Event.

    Every Tier-2 field is sanitised rather than trusted: whatever the model left
    empty degrades to null/[] and is recorded in ``fields_unknown``, which then
    feeds the confidence score. The schema is fixed, so the output shape is the
    same whether enrichment succeeded fully or barely at all.
    """
    data = outcome.data
    unknown: list[str] = []  # Tier-2 fields with no usable value, named as in the schema.

    participants = data.estimated_participants
    if participants is None:
        unknown.append("estimated_participants")

    interests = _clean_strings(data.interests)
    industries = _clean_strings(data.industries)
    if not interests:
        unknown.append("audience_profile.interests")
    if not industries:
        unknown.append("audience_profile.industries")

    age_range = _age_range(data)
    if age_range is None:
        unknown.append("audience_profile.demographics.age_range")

    levels = _clean_enums(data.professional_level, ProfessionalLevel.unknown)
    groups = _clean_enums(data.group_types, GroupType.unknown)
    if levels == [ProfessionalLevel.unknown]:
        unknown.append("audience_profile.demographics.professional_level")
    if groups == [GroupType.unknown]:
        unknown.append("audience_profile.demographics.group_types")
    if data.gender_skew is GenderSkew.unknown:
        unknown.append("audience_profile.demographics.gender_skew")

    origins = _origins(data)
    if origins.local is None:
        unknown.append("geographic_origins")
    if not origins.top_countries:
        unknown.append("geographic_origins.top_countries")

    # The mirror of `unknown`: fields that do carry a value, but a modelled one
    # rather than a sourced fact. Consumers need to be able to tell them apart.
    estimated = [
        field
        for field, present in (
            ("estimated_participants", participants is not None),
            ("audience_profile", bool(interests or industries or age_range)),
            ("geographic_origins", origins.local is not None),
        )
        if present
    ]

    start = parse_date(raw.start_date)
    assert start is not None  # guaranteed by the Tier-1 gate
    end = parse_date(raw.end_date)

    return Event(
        id=raw.id or event_id(raw, config.city),
        name=raw.name,
        category=data.category or Category.other,
        description=data.description.strip() or raw.description,
        # A single-day event carries no end date rather than a repeat of start.
        date=DateRange(start=start, end=end if end and end != start else None),
        venue=Venue(
            name=raw.venue_name or "",
            address=raw.venue_address,
            # Discovery is scoped to one city, so these come from config instead
            # of being re-derived from whatever string the model returned.
            city=config.city,
            country=config.country,
        ),
        estimated_participants=participants,
        audience_profile=AudienceProfile(
            interests=interests,
            industries=industries,
            demographics=Demographics(
                age_range=age_range,
                professional_level=levels,
                group_types=groups,
                gender_skew=data.gender_skew,
                notes=data.demographics_notes.strip(),
            ),
        ),
        geographic_origins=origins,
        sources=[
            Source(
                title=s.title.strip() or raw.name,
                url=s.url,
                type=s.type,
                retrieved_at=utc_now(),  # code-stamped; never taken from the model
            )
            for s in raw.sources
        ],
        data_quality=DataQuality(
            confidence=_confidence(data.confidence, len(unknown), outcome.page_fetched),
            fields_estimated=estimated,
            fields_unknown=unknown,
        ),
    )


def _clean_strings(values: list[str]) -> list[str]:
    """Lowercase, de-duplicate and drop literal "unknown" placeholders.

    Free-text tags are only useful downstream if they group, so casing is
    flattened here. A dict preserves first-seen order, unlike a set.
    """
    seen: dict[str, None] = {}
    for value in values:
        cleaned = value.strip().lower()
        if cleaned and cleaned != "unknown":
            seen.setdefault(cleaned, None)
    return list(seen)


def _clean_enums(values: list, unknown_member):
    """Drop the reserved 'unknown' member when real values are present.

    The model sometimes returns ``unknown`` alongside real answers; the list is
    only genuinely unknown if nothing else survives.
    """
    real = list(dict.fromkeys(v for v in values if v is not unknown_member))
    return real or [unknown_member]


def _age_range(data: Enrichment) -> AgeRange | None:
    """Build the age range, or None if it is incomplete or implausible.

    A reversed pair is a formatting slip and is simply sorted; anything the
    model rejects (out-of-bounds ages) degrades to null, as a Tier-2 field.
    """
    if data.age_min is None or data.age_max is None:
        return None
    low, high = sorted((data.age_min, data.age_max))
    try:
        return AgeRange(min=low, max=high)
    except ValidationError:
        return None


def _origins(data: Enrichment) -> GeographicOrigins:
    """Keep the split only if it is internally consistent.

    Origin shares are a Tier-2 field, so a bad split degrades to null and costs
    confidence; it never drops the event.
    """
    shares = [data.local_share, data.national_share, data.international_share]
    countries = [c.strip() for c in data.top_countries if c.strip()]
    notes = data.origins_notes.strip()

    # A partial split cannot be interpreted, so it is all three shares or none.
    if any(s is None for s in shares):
        return GeographicOrigins(top_countries=countries, notes=notes)

    # Rounding first, then checking the sum, keeps the published numbers and the
    # numbers that were validated identical. Tolerance absorbs rounding drift.
    rounded = [round(s, 2) for s in shares]  # type: ignore[arg-type]
    if abs(sum(rounded) - 1.0) > 0.05:
        log.debug("Discarding origin shares summing to %.2f", sum(rounded))
        return GeographicOrigins(top_countries=countries, notes=notes)

    return GeographicOrigins(
        local=rounded[0],
        national=rounded[1],
        international=rounded[2],
        top_countries=countries,
        notes=notes,
    )


def _confidence(model_confidence: float, unknown_count: int, page_fetched: bool) -> float:
    """Discount the model's self-reported confidence by what the run observed."""
    score = model_confidence - UNKNOWN_PENALTY * unknown_count
    if not page_fetched:
        score -= NO_PAGE_PENALTY
    return round(min(CONFIDENCE_CEILING, max(CONFIDENCE_FLOOR, score)), 2)


def _final_reason(error: ValidationError) -> tuple[RejectionReason, str]:
    """Map a pydantic failure onto a machine-filterable rejection code.

    Sanitisation should have caught these already, so reaching here means a gap
    in the Tier-2 fallbacks; the specific codes make that visible in the log.
    """
    detail = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in error.errors())
    if "geographic_origins" in detail:
        return RejectionReason.invalid_geographic_origins_sum, detail
    if "age_range" in detail:
        return RejectionReason.invalid_age_range, detail
    return RejectionReason.other, detail


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def _reject(
    raw: RawEvent,
    stage: RejectionStage,
    reason: RejectionReason,
    detail: str,
) -> RejectedEvent:
    """Record a dropped candidate, keeping enough of it to diagnose the drop.

    The snapshot is deliberately partial data, so a rejection can be inspected
    without re-running discovery.
    """
    return RejectedEvent(
        name=raw.name or "(unnamed)",
        stage=stage,
        reason=reason,
        reason_detail=detail,
        raw_snapshot={
            "name": raw.name,
            "start_date": raw.start_date,
            "end_date": raw.end_date,
            "venue": raw.venue_name,
            "sources": [s.url for s in raw.sources],
        },
        rejected_at=utc_now(),
    )


def run(config: Config) -> int:
    """Run stages 1-6 and return a process exit code.

    Failure is per-event wherever possible: a candidate that cannot be validated
    or enriched is logged to the rejection file and the run continues. Only a
    failed discovery is fatal, because it leaves nothing to process.
    """
    cache = JsonCache(config.cache_dir, config.use_cache)
    prompts = PromptLibrary(config.prompt_dir, config.prompt_version)
    client = genai.Client(api_key=config.api_key)
    http = make_client()
    rejected: list[RejectedEvent] = []

    try:
        # Stage 1: the only source adapter for now, behind the EventSource seam.
        source = GeminiGroundingSource(client, http, config.model, prompts, cache)
        try:
            candidates = source.search(config.city, config.country, config.month, config.max_events)
        except Exception as exc:
            # The adapter has already retried anything transient, so whatever
            # reaches here will not succeed on another attempt.
            log.error("Discovery failed after retries, aborting: %s", exc)
            return 1

        # Stage 2: ids are stamped after dedupe so merged duplicates share one id
        # and therefore one enrichment cache entry.
        candidates = dedupe(normalize(candidates))
        for candidate in candidates:
            candidate.id = event_id(candidate, config.city)

        # Stage 3: the discovery-side gate.
        accepted: list[RawEvent] = []
        for candidate in candidates:
            failure = check_tier1(candidate, config.month)
            if failure is None:
                accepted.append(candidate)
            else:
                reason, detail = failure
                log.info("Rejected at discovery: %s (%s)", candidate.name, reason.value)
                rejected.append(
                    _reject(candidate, RejectionStage.discovery_validation, reason, detail)
                )

        # Stages 4 and 5, interleaved per candidate: enrich, then assemble and
        # validate at one iteration, so one bad event costs one row instead of the whole run.
        enricher = Enricher(client, http, config.model, prompts, cache)
        events: list[Event] = []
        for index, candidate in enumerate(accepted, start=1):
            prefix = f"[{index}/{len(accepted)}] Enriching: {candidate.name}"
            try:
                outcome = enricher.enrich(candidate, config.city, config.country)
            except EnrichmentError as exc:
                log.info("%s (failed, dropped)", prefix)
                rejected.append(
                    _reject(
                        candidate,
                        RejectionStage.final_validation,
                        RejectionReason.api_error,
                        str(exc),
                    )
                )
                continue
            log.info("%s (%s)", prefix, "cache hit" if outcome.cache_hit else "calling Gemini")

            try:
                events.append(build_event(candidate, outcome, config))
            except ValidationError as exc:
                reason, detail = _final_reason(exc)
                log.info("Rejected at final validation: %s (%s)", candidate.name, reason.value)
                rejected.append(
                    _reject(candidate, RejectionStage.final_validation, reason, detail)
                )
    finally:
        http.close()

    # Stage 6. The rejection file is written on every run, even when empty.
    _write_output(config, events, rejected)
    log.info(
        "Done: %d events written to %s, %d rejected to %s.",
        len(events),
        config.output_path.name,
        len(rejected),
        config.rejected_path.name,
    )
    return 0


def _write_output(config: Config, events: list[Event], rejected: list[RejectedEvent]) -> None:
    """Write the deliverable and its companion rejection log.

    Both files get the same ``generated_at``, so a pair can be matched up as one
    run. ``event_count`` is the real total after validation, not ``max_events``.
    """
    now = utc_now()
    events_file = EventsFile(
        meta=Meta(
            city=config.city,
            country=config.country,
            month=config.month,
            generated_at=now,
            model=config.model,
            max_events=config.max_events,
            event_count=len(events),
        ),
        events=events,
    )
    rejected_file = RejectedFile(
        meta=RejectedMeta(
            city=config.city,
            month=config.month,
            generated_at=now,
            rejected_count=len(rejected),
        ),
        rejected=rejected,
    )
    _dump(config.output_path, events_file)
    _dump(config.rejected_path, rejected_file)


def _dump(path, model) -> None:
    """Serialise a pydantic model to indented UTF-8 JSON.

    ``ensure_ascii=False`` keeps German place names readable rather than escaped.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(model.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def setup_logging(verbose: bool) -> None:
    """Configure run output: bare messages on stdout, third-party noise muted."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        stream=sys.stdout,
    )
    for noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # The SDK warns about automatic function calling on every grounded call.
    logging.getLogger("google_genai").setLevel(logging.ERROR)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: load config, set up logging, run the pipeline."""
    config = load_config(argv)
    setup_logging(config.verbose)
    return run(config)


if __name__ == "__main__":
    raise SystemExit(main())
