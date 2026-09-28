"""Discovery adapters implementing the ``EventSource`` protocol."""

from .base import EventSource
from .gemini_grounding import GeminiGroundingSource

__all__ = ["EventSource", "GeminiGroundingSource"]
