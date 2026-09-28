"""Loads versioned prompt templates from ``prompts/{version}/{name}.txt``.

Changing prompt behaviour means bumping ``PROMPT_VERSION``, never editing code
(DECISIONS.md #8). Placeholders use ``${name}`` rather than ``{name}`` so that
the JSON examples inside the prompts do not need their braces escaped; a
literal dollar sign is written ``$$``.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from string import Template


class PromptLibrary:
    def __init__(self, prompt_dir: Path, version: str) -> None:
        self.base = prompt_dir / version
        if not self.base.is_dir():
            raise FileNotFoundError(f"No prompt directory at {self.base}")
        self.version = version

    @lru_cache(maxsize=None)
    def _template(self, template: str) -> Template:
        path = self.base / f"{template}.txt"
        if not path.exists():
            raise FileNotFoundError(f"No prompt template at {path}")
        return Template(path.read_text(encoding="utf-8"))

    def render(self, template: str, /, **values: object) -> str:
        """``template`` is positional-only so a ``${name}`` placeholder is free to use."""
        return self._template(template).substitute(**values)
