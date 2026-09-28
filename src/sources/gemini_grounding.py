"""Discovery via Gemini with Google Search grounding.

One paid call per (city, month), cached. Grounding is what makes this a real
sourcing strategy rather than a recall exercise: every candidate is expected to
carry a URL that the model actually saw in search results.
"""

from __future__ import annotations

import calendar
import json
import logging
import re

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import ValidationError
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from ..cache import JsonCache
from ..fetch import resolve_url
from ..models import RawEvent, RawSource, SourceType
from ..prompts import PromptLibrary

log = logging.getLogger(__name__)

_FENCE = re.compile(r"^\s*```(?:json)?|```\s*$", re.MULTILINE)
_WORD = re.compile(r"[a-z0-9]+")
_STOPWORDS = {"the", "and", "of", "for", "in", "fair", "festival", "munich", "international", "2026"}


def _is_transient(exc: BaseException) -> bool:
    """Retry rate limits, server faults and transport errors; nothing else.

    A bad model name or a rejected key fails identically on every attempt, so
    retrying it only delays the error message.
    """
    if isinstance(exc, genai_errors.ClientError):
        return getattr(exc, "code", None) == 429
    return True


class GeminiGroundingSource:
    name = "gemini_grounding"

    def __init__(
        self,
        client: genai.Client,
        http: httpx.Client,
        model: str,
        prompts: PromptLibrary,
        cache: JsonCache,
    ) -> None:
        self._client = client
        self._http = http
        self._model = model
        self._prompts = prompts
        self._cache = cache

    def search(self, city: str, country: str, month: str, max_events: int) -> list[RawEvent]:
        key = f"{city.lower()}_{month}_{max_events}"
        cached = self._cache.get("discovery", key)
        if cached is not None:
            log.info("Discovery: %d candidates from cache", len(cached))
            return [RawEvent.model_validate(item) for item in cached]

        log.info("Discovery: searching %s / %s (calling Gemini)", city, month)
        prompt = self._prompts.render(
            "discovery",
            city=city,
            country=country,
            month=month,
            month_label=_month_label(month),
            max_events=max_events,
        )
        response = self._generate(prompt)

        events = _parse_events(response.text or "")
        # Some of the urls might still google proxy here 
        _attach_grounding_sources(events, _grounding_chunks(response))
        # resulve any exist google proxy url
        for event in events:
            _resolve_sources(self._http, event)

        events = events[:max_events]
        self._cache.set("discovery", key, [e.model_dump(mode="json") for e in events])
        log.info("Discovery: found %d candidates", len(events))
        return events

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=20),
        retry=retry_if_exception(_is_transient),
        before_sleep=before_sleep_log(log, logging.WARNING),
        reraise=True,
    )
    def _generate(self, prompt: str) -> types.GenerateContentResponse:
        return self._client.models.generate_content(
            model=self._model,
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
                temperature=0.2,
            ),
        )


def _month_label(month: str) -> str:
    year, mon = (int(part) for part in month.split("-"))
    return f"{calendar.month_name[mon]} {year}"


def _parse_events(text: str) -> list[RawEvent]:
    """Pull a JSON array out of a grounded response.

    Grounded calls cannot use ``response_schema``, so the text is parsed
    leniently: code fences are stripped and the outermost array is extracted.
    """
    payload = _FENCE.sub("", text).strip()
    start, end = payload.find("["), payload.rfind("]")
    if start == -1 or end <= start:
        log.error("Discovery response contained no JSON array")
        return []

    try:
        items = json.loads(payload[start : end + 1])
    except json.JSONDecodeError as exc:
        log.error("Discovery response was not valid JSON: %s", exc)
        return []

    events: list[RawEvent] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            event = RawEvent.model_validate(item)
        except ValidationError as exc:
            log.debug("Skipping malformed candidate: %s", exc)
            continue
        event.sources = [s for s in event.sources if s.url.startswith("http")]
        if event.name.strip():
            events.append(event)
    return events


def _grounding_chunks(response: types.GenerateContentResponse) -> list[RawSource]:
    chunks: list[RawSource] = []
    for candidate in response.candidates or []:
        metadata = candidate.grounding_metadata
        for chunk in (metadata.grounding_chunks or []) if metadata else []:
            web = getattr(chunk, "web", None)
            if web and web.uri:
                chunks.append(
                    RawSource(title=web.title or "Search result", url=web.uri, type=SourceType.search_grounding)
                )
    return chunks


def _attach_grounding_sources(events: list[RawEvent], chunks: list[RawSource]) -> None:
    """Give sourceless candidates a grounding URL, but only a plausibly related one.

    Matching on name tokens keeps the evidence requirement meaningful: a random
    search result is worse than no source, because it would sneak an
    unevidenced event past the Tier-1 gate.
    """
    if not chunks:
        return
    for event in events:
        if event.sources:
            continue
        tokens = _tokens(event.name)
        for chunk in chunks:
            haystack = f"{chunk.title} {chunk.url}".lower()
            if tokens and any(token in haystack for token in tokens):
                event.sources.append(chunk)
                break


def _tokens(name: str) -> set[str]:
    return {w for w in _WORD.findall(name.lower()) if len(w) > 3 and w not in _STOPWORDS}


def _resolve_sources(http: httpx.Client, event: RawEvent) -> None:
    seen: set[str] = set()
    resolved: list[RawSource] = []
    for source in event.sources:
        url = resolve_url(http, source.url)
        if url in seen:
            continue
        seen.add(url)
        resolved.append(source.model_copy(update={"url": url}))
    event.sources = resolved
