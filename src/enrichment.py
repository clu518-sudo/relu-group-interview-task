"""Stage 4: turn a validated candidate into an audience profile.

Code does the free part (fetch the source page, strip it to text) and Gemini
does the part that needs judgement. Structured output is used here because the
call is ungrounded, so the schema can be enforced by the SDK.

No retries by design (DECISIONS.md #8): a failed enrichment drops one event to
the rejection log and the run continues, rather than stalling on a bad URL.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx
from google import genai
from google.genai import types

from .cache import JsonCache
from .fetch import fetch_page_text
from .models import Enrichment, RawEvent
from .prompts import PromptLibrary

log = logging.getLogger(__name__)

MAX_PAGES_TRIED = 2


class EnrichmentError(RuntimeError):
    """The model call failed or returned something unusable."""


@dataclass
class EnrichmentOutcome:
    data: Enrichment
    cache_hit: bool
    page_fetched: bool


class Enricher:
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

    def enrich(self, event: RawEvent, city: str, country: str) -> EnrichmentOutcome:
        key = f"{event.id}_{self._prompts.version}"
        cached = self._cache.get("enrichment", key)
        if cached is not None:
            return EnrichmentOutcome(
                data=Enrichment.model_validate(cached["enrichment"]),
                cache_hit=True,
                page_fetched=cached.get("page_fetched", False),
            )

        page_text = self._page_text(event)
        prompt = self._prompts.render(
            "enrichment",
            name=event.name,
            dates=_format_dates(event),
            venue=event.venue_name or "unknown",
            city=city,
            country=country,
            description=event.description or "none provided",
            urls=", ".join(s.url for s in event.sources) or "none",
            page_text=page_text or "(no page text could be retrieved)",
        )

        try:
            response = self._client.models.generate_content(
                model=self._model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=Enrichment,
                    temperature=0.3,
                ),
            )
        except Exception as exc:  # SDK raises a family of transport/API errors
            raise EnrichmentError(str(exc)) from exc

        data = response.parsed
        if not isinstance(data, Enrichment):
            raise EnrichmentError(f"model returned unusable payload: {(response.text or '')[:200]}")

        self._cache.set(
            "enrichment",
            key,
            {"enrichment": data.model_dump(mode="json"), "page_fetched": page_text is not None},
        )
        return EnrichmentOutcome(data=data, cache_hit=False, page_fetched=page_text is not None)

    def _page_text(self, event: RawEvent) -> str | None:
        for source in event.sources[:MAX_PAGES_TRIED]:
            text = fetch_page_text(self._http, source.url)
            if text:
                return text
        log.debug("No page text available for %r", event.name)
        return None


def _format_dates(event: RawEvent) -> str:
    if event.start_date and event.end_date and event.end_date != event.start_date:
        return f"{event.start_date} to {event.end_date}"
    return event.start_date or "unknown"
