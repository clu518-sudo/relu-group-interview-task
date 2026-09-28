"""Run configuration: everything the pipeline needs, nothing hard-coded.

Precedence is CLI flag > environment / ``.env`` > built-in default.
"""

from __future__ import annotations

import argparse
import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_MODEL = "gemini-3.8-flash"
MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


@dataclass(frozen=True)
class Config:
    city: str
    country: str
    month: str  # YYYY-MM
    max_events: int
    use_cache: bool
    output_path: Path
    rejected_path: Path
    cache_dir: Path
    prompt_dir: Path
    prompt_version: str
    model: str
    api_key: str
    verbose: bool

    @property
    def slug(self) -> str:
        """Filename-safe city token, e.g. 'Munich' -> 'munich'."""
        return re.sub(r"[^a-z0-9]+", "_", self.city.lower()).strip("_")


def _slugify(city: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", city.lower()).strip("_")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.pipeline",
        description="Discover events for a city/month and profile their likely audience.",
    )
    parser.add_argument("--city", default="Munich")
    parser.add_argument("--country", default="Germany")
    parser.add_argument("--month", default="2026-10", help="YYYY-MM")
    parser.add_argument("--max-events", type=int, default=15, help="upper bound, never a guarantee")
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="force fresh API/model calls and overwrite the cache (costs money)",
    )
    parser.add_argument("--output", help="default: events_{city}_{month}.json")
    parser.add_argument("--rejected", default="rejected_events.json")
    parser.add_argument("--prompt-version", help="default: PROMPT_VERSION env or v1")
    parser.add_argument("--model", help="default: GEMINI_MODEL env or " + DEFAULT_MODEL)
    parser.add_argument("-v", "--verbose", action="store_true", help="include debug-level logs")
    return parser


def load_config(argv: list[str] | None = None) -> Config:
    load_dotenv(PROJECT_ROOT / ".env")
    args = build_parser().parse_args(argv)

    if not MONTH_PATTERN.match(args.month):
        raise SystemExit(f"--month must look like YYYY-MM, got {args.month!r}")
    if args.max_events < 1:
        raise SystemExit("--max-events must be at least 1")

    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("GEMINI_API_KEY is not set. Copy .env.example to .env and fill it in.")

    default_output = f"events_{_slugify(args.city)}_{args.month}.json"
    prompt_dir = Path(os.getenv("PROMPT_DIR", "prompts"))
    if not prompt_dir.is_absolute():
        prompt_dir = PROJECT_ROOT / prompt_dir

    return Config(
        city=args.city,
        country=args.country,
        month=args.month,
        max_events=args.max_events,
        use_cache=not args.no_cache,
        output_path=_resolve(args.output or default_output),
        rejected_path=_resolve(args.rejected),
        cache_dir=PROJECT_ROOT / "cache",
        prompt_dir=prompt_dir,
        prompt_version=args.prompt_version or os.getenv("PROMPT_VERSION", "v1"),
        model=args.model or os.getenv("GEMINI_MODEL", DEFAULT_MODEL),
        api_key=api_key,
        verbose=args.verbose,
    )


def _resolve(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path
