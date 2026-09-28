# Event Discovery & Audience Profiling: Munich, October 2026

This finds real events happening in a city and month. For each event, it describes who is likely to attend, where they travel from, and the event's key facts.

The output is JSON. It is meant to be read by another model, not by a person.

The deliverable is `[events_oct_2026.json](events_oct_2026.json)`. It has events in Munich in October 2026. Every event has at least one live source URL.

`DECISIONS.md` has the full design reasoning, including options I considered and rejected. This README covers the approach, how to run it, and what I'd do next.

## Process

`DECISIONS.md` is a brainstorming log. It is not a script written by a model.

I used a Claude model in Cursor as a brainstorming partner. It offered information, options, and trade-offs: event APIs vs. grounding, schema field tiers, dedupe strategy, retry policy, and so on. Every decision recorded there is mine, including the final wording.

The code was then built in Cursor, following that decisions log. I did not type it line by line. I read through all of it, and I understand each design decision, the module structure, and how the pipeline runs end to end.

## Approach

```
Config (city, month, max_events, use_cache)
   │
   ├─[1] DISCOVERY   Gemini + Google Search grounding (paid, cached)
   │                 finds candidate events
   ├─[2] NORMALIZE    parse dates, merge duplicates, stamp ids (free)
   ├─[3] TIER-1 GATE  needs name, date, venue, source (free)
   ├─[4] ENRICHMENT   scrape page, ask Gemini for audience info (paid, cached)
   ├─[5] ASSEMBLE     rebuild schema, score confidence, validate (free)
   └─[6] WRITE        events_oct_2026.json + rejected_events.json (free)
```

Three ideas drove the design:

**Evidence over recall.** Every event in the output must carry a URL a human can open. If the model is confident about an event but can't source it, that event is dropped to `[rejected_events.json](rejected_events.json)` instead of being included quietly. For the same reason, a candidate with no source is never just handed the nearest search result. That would defeat the whole point of the gate.

**Estimates are labelled as estimates.** Audience profiles and origin shares are modelled, not survey data. Rather than hide that, every event carries a `data_quality` block: a confidence score, plus which fields were estimated and which fell back to unknown. A downstream consumer can filter on `confidence >= 0.7` and get the well-evidenced subset.

**Paid work is cached. Free work is not.** Only stages 1 and 4 call an API. Both write their responses to `cache/`. That means the parsing, dedupe, validation, and assembly logic could be changed and re-run any number of times without spending more budget.

### Why Gemini grounding, not event APIs

The original plan was a hybrid: Eventbrite, Ticketmaster, or Meetup for structured listings, and Gemini to fill in audience and origin data.

In practice, none of those were usable in this timeframe. Eventbrite closed public event search. Meetup requires paid OAuth access. Neither would be approved fast enough for this task.

So the hybrid _architecture_ stayed, but the _build_ got phased. Discovery sits behind an `[EventSource](src/sources/base.py)` protocol. Phase 1 ships one implementation, `[GeminiGroundingSource](src/sources/gemini_grounding.py)`. A `TicketmasterSource` could be added later without touching normalisation, validation, or enrichment. Grounding still satisfies the evidence requirement, because it returns URLs the model actually saw.

## Running it

```bash
python -m venv .venv
.venv/Scripts/activate          # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env             # put your key in GEMINI_API_KEY
python -m src.pipeline --output events_oct_2026.json --max-events 15
```

Nothing about the run is hard-coded to Munich:

```bash
python -m src.pipeline --city Lisbon --country Portugal --month 2026-11 --max-events 10
```

Flags:

- `--city`, `--country`: default Munich, Germany.
- `--month`: default `2026-10`, format `YYYY-MM`.
- `--max-events`: default 15. A cap, never a guarantee. See "Cost control" below.
- `--no-cache`: forces fresh paid calls and overwrites the cache.
- `--output`: default `events_{city}_{month}.json`. Pinned to `events_oct_2026.json` for this submission.
- `--prompt-version`: default `v1`. Selects `prompts/{version}/`.
- `--model`: default `gemini-3.8-flash`. Also settable via `GEMINI_MODEL`.
- `--verbose`: off by default. Turns on debug logging.

Re-running is cheap. Cached events log `(cache hit)` instead of `(calling Gemini)`, so it's obvious while a run happens which calls cost money.

Checking and testing:

```bash
# re-validate the output and confirm each source URL resolves
python scripts/verify_output.py events_oct_2026.json --check-urls

# 20 tests, no API calls
pip install -r requirements-dev.txt
python -m pytest tests
```

## Output schema

The full shape and field-by-field rules live in [DECISIONS.md #4](DECISIONS.md#4-output-format--schema). The pydantic definition is `[src/models.py](src/models.py)`. Short version:

```jsonc
{
  "meta": {
    "city": "Munich",
    "month": "2026-10",
    "model": "...",
    "event_count": 12,
    "schema_version": "1.0",
  },
  "events": [
    {
      "id": "uuid5 of name + start date + city, stable across runs",
      "name": "EXPO REAL 2026",
      "category": "Trade Fair", // one of 8 enum values
      "date": { "start": "2026-10-05", "end": "2026-10-07" },
      "venue": {
        "name": "Messe München",
        "address": "...",
        "city": "...",
        "country": "...",
      },
      "estimated_participants": 40000, // or null
      "audience_profile": {
        "interests": [],
        "industries": [],
        "demographics": {
          "age_range": { "min": 22, "max": 70 },
          "professional_level": [],
          "group_types": [],
          "gender_skew": "male-leaning",
          "notes": "",
        },
      },
      "geographic_origins": {
        "local": 0.25,
        "national": 0.45,
        "international": 0.3,
        "top_countries": [],
        "notes": "",
      },
      "sources": [
        {
          "title": "...",
          "url": "...",
          "type": "official_site",
          "retrieved_at": "...",
        },
      ],
      "data_quality": {
        "confidence": 0.85,
        "fields_estimated": [],
        "fields_unknown": [],
      },
    },
  ],
}
```

The consumer is a model, so almost everything is a number or a controlled enum. Free text only shows up in `description`, the `notes` fields, and source titles.

Every enum reserves an `unknown` member. So a missing value is explicit, not an empty string or a missing key. The schema stays stable. Every event always has every field.

**Missing data has tiers.** `name`, `date.start`, `venue.name`, and a non-empty `sources` array are must-haves. Without them, the event is dropped and logged.

Everything from the enrichment step is optional. It degrades to `null`, `[]`, or `"unknown"`, gets named in `fields_unknown`, and costs some confidence. An event is never thrown away over a soft field.

`confidence` starts as the model's own rating. Code then subtracts 0.05 per unknown field, and 0.15 if no page text could be scraped. That spread shows up in this run: EXPO REAL scores 0.85 off its own site.

## Cost control

The task budget was $10 NZD. The whole Munich run is one grounded discovery call, plus one enrichment call per surviving candidate. That's 13 paid calls for 12 events, a few cents on Flash.

What kept it cheap:

- A cheap model, pinned and configurable.
- A disk cache on both paid stages. Every change to the free stages could be re-run for free. Developing at `--max-events 3` and then raising the cap reused the three enrichments already cached.
- `max_events` is a hard cap, with no over-fetching. Discovery is asked for at most 15. If candidates fail validation, the output is just smaller. `meta.event_count` reports the real number.
- Page scraping is plain `httpx` plus BeautifulSoup. Dedupe is `rapidfuzz` string similarity. A model could do both, but neither is worth paying for.

## What I can do next

1. **Phase 2 adapters.** Add `TicketmasterSource` and a Messe München scraper behind the existing protocol. Structured listings would give exact dates and participant counts instead of estimates. Agreement across sources would be a real confidence signal, not just a heuristic one.
2. **Multiple discovery queries.** Run one grounded search per category instead of one for everything, then dedupe across them. That's roughly one extra call per category, and it should meaningfully improve recall on smaller local events.
3. **Semantic dedupe** via embeddings, once there is more than one source feeding candidates in and lexical matching starts missing things.
4. **Evaluation.** Right now correctness is checked by `scripts/verify_output.py` plus reading the output. A small labelled set of known Munich events would turn that into a measurable recall number.

## Security

The Gemini API key is read from the environment, or from a gitignored `.env`, and is never committed.
