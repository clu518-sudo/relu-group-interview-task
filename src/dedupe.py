"""Stage 2: normalise raw candidates and merge duplicates.

Pure code, no API cost. Duplicates are merged rather than discarded, because a
second hit on the same event is extra evidence, not noise (DECISIONS.md #8).
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime

from rapidfuzz import fuzz

from .models import RawEvent, RawSource

log = logging.getLogger(__name__)

NAME_MATCH_THRESHOLD = 85

_YEAR = re.compile(r"\b(19|20)\d{2}\b")
_EDITION = re.compile(r"\b\d{1,3}(st|nd|rd|th)\b", re.IGNORECASE)
_PUNCT = re.compile(r"[^a-z0-9\s]+")
_SPACE = re.compile(r"\s+")

_DATE_FORMATS = ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%B %d, %Y", "%d %B %Y", "%Y-%m")


def normalize_name(name: str) -> str:
    """Strip the parts of an event title that vary between editions and sources."""
    text = name.lower()
    text = _YEAR.sub(" ", text)
    text = _EDITION.sub(" ", text)
    text = _PUNCT.sub(" ", text)
    return _SPACE.sub(" ", text).strip()


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    text = value.strip()[:10] if len(value.strip()) >= 10 and "T" in value else value.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    log.debug("Unparseable date: %r", value)
    return None


def normalize(events: list[RawEvent]) -> list[RawEvent]:
    """Trim whitespace and rewrite dates into ISO form (or drop them)."""
    cleaned: list[RawEvent] = []
    for event in events:
        start = parse_date(event.start_date)
        end = parse_date(event.end_date)
        if start and end and end < start:
            start, end = end, start
        cleaned.append(
            event.model_copy(
                update={
                    "name": _SPACE.sub(" ", event.name).strip(),
                    "start_date": start.isoformat() if start else None,
                    "end_date": end.isoformat() if end else None,
                    "venue_name": (event.venue_name or "").strip() or None,
                    "venue_address": (event.venue_address or "").strip() or None,
                    "description": _SPACE.sub(" ", event.description).strip(),
                }
            )
        )
    return cleaned


def dedupe(events: list[RawEvent]) -> list[RawEvent]:
    merged: list[RawEvent] = []
    for event in events:
        match = next((m for m in merged if is_same_event(m, event)), None)
        if match is None:
            merged.append(event)
            continue
        log.debug("Merging duplicate %r into %r", event.name, match.name)
        merged[merged.index(match)] = merge(match, event)
    if len(merged) < len(events):
        log.info("Dedupe: %d candidates merged into %d", len(events), len(merged))
    return merged


def is_same_event(a: RawEvent, b: RawEvent) -> bool:
    """Same event if the titles match closely and the dates do not contradict.

    ``token_set_ratio`` because titles differ mostly by extra words: "EXPO REAL"
    versus "EXPO REAL - International Trade Fair for Property and Investment".
    """
    if fuzz.token_set_ratio(normalize_name(a.name), normalize_name(b.name)) < NAME_MATCH_THRESHOLD:
        return False
    return _dates_overlap(a, b)


def _dates_overlap(a: RawEvent, b: RawEvent) -> bool:
    a_start, b_start = parse_date(a.start_date), parse_date(b.start_date)
    if a_start is None or b_start is None:
        return True  # No date to contradict the name match.
    a_end = parse_date(a.end_date) or a_start
    b_end = parse_date(b.end_date) or b_start
    return a_start <= b_end and b_start <= a_end


def merge(a: RawEvent, b: RawEvent) -> RawEvent:
    """Keep the richest value for each field and union the evidence."""
    return RawEvent(
        name=max(a.name, b.name, key=len),
        start_date=a.start_date or b.start_date,
        end_date=a.end_date or b.end_date,
        venue_name=_richer(a.venue_name, b.venue_name),
        venue_address=_richer(a.venue_address, b.venue_address),
        category=a.category or b.category,
        description=max(a.description, b.description, key=len),
        sources=_union_sources(a.sources, b.sources),
    )


def _richer(a: str | None, b: str | None) -> str | None:
    if a and b:
        return max(a, b, key=len)
    return a or b


def _union_sources(a: list[RawSource], b: list[RawSource]) -> list[RawSource]:
    by_url: dict[str, RawSource] = {}
    for source in [*a, *b]:
        by_url.setdefault(source.url, source)
    return list(by_url.values())
