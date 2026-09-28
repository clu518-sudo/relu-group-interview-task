"""The adapter seam between discovery and the rest of the pipeline.

Phase 1 ships a single implementation (``GeminiGroundingSource``). A
``TicketmasterSource`` or ``EventbriteSource`` can be added later by satisfying
this protocol, with no change to normalisation, validation or enrichment.
"""

from __future__ import annotations

from typing import Protocol

from ..models import RawEvent


class EventSource(Protocol):
    name: str

    def search(self, city: str, country: str, month: str, max_events: int) -> list[RawEvent]:
        """Return up to ``max_events`` candidate events for ``city`` in ``month``."""
        ...
