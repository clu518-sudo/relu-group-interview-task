"""Sanity-check a generated events file.

Re-validates the JSON against the schema and prints one line per event so the
output can be eyeballed quickly. With ``--check-urls`` it also confirms every
source URL still resolves, which is the one thing the pipeline cannot assert
for itself.

    python scripts/verify_output.py events_oct_2026.json --check-urls
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.models import EventsFile  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", nargs="?", default="events_oct_2026.json")
    parser.add_argument("--check-urls", action="store_true")
    args = parser.parse_args()

    data = EventsFile.model_validate(json.loads(Path(args.path).read_text(encoding="utf-8")))
    print(f"{data.meta.city} {data.meta.month} | {data.meta.event_count} events | model {data.meta.model}\n")

    problems: list[str] = []
    seen_ids: set[str] = set()

    for event in data.events:
        origins = event.geographic_origins
        share_sum = (
            "null"
            if origins.local is None
            else f"{origins.local + origins.national + origins.international:.2f}"
        )
        window = f"{event.date.start}..{event.date.end or event.date.start}"
        print(
            f"{window}  {event.category.value:<10}  conf={event.data_quality.confidence:<5}"
            f"  shares={share_sum:<5}  src={len(event.sources)}  {event.name}"
        )

        if event.id in seen_ids:
            problems.append(f"duplicate id: {event.name}")
        seen_ids.add(event.id)

        end = event.date.end or event.date.start
        if not (event.date.start.strftime("%Y-%m") <= data.meta.month <= end.strftime("%Y-%m")):
            problems.append(f"outside {data.meta.month}: {event.name}")

    if args.check_urls:
        import httpx

        # 401/403/405 mean the page exists but refuses a non-browser client,
        # which is common on sites behind a bot filter. Only 404/410 and
        # transport failures say the evidence is actually gone.
        bot_blocked = {401, 403, 405, 429}

        print()
        with httpx.Client(timeout=20, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as client:
            for event in data.events:
                for source in event.sources:
                    try:
                        status: int | str = client.get(source.url).status_code
                    except httpx.HTTPError as exc:
                        status = type(exc).__name__
                    if status == 200:
                        marker = "ok  "
                    elif status in bot_blocked:
                        marker = "bot "
                    else:
                        marker = "DEAD"
                        problems.append(f"{status} for {source.url}")
                    print(f"{marker} {status}  {source.url}")

    print()
    if problems:
        print(f"{len(problems)} problem(s):")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("No problems found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
