# Design Decisions Log

Living record of key decisions for the Event Discovery & Audience Profiling task
(Munich, October 2026). Feeds directly into the final README.

## Task recap

- Target: Find real-world events in **Munich, October 2026**.
- For each event describe: event type, audience profile (who attends), geographic
  origins (where attendees come from), and key features (participants, venue, date,
  source/evidence).
- Output: machine-consumable **JSON** with a clear schema, saved as
  `events_oct_2026.json`.
- Deliverables: public GitHub repo + JSON data + README.
- Constraints: $10 NZD Gemini budget (key valid until 30 Sep 2026); submit by
  Tue 29 Sep 2026 (NZ time).

## Decisions made

### 1. Language / stack

- **Python.** Best Gemini SDK support, `pydantic` for schema validation, clean/readable.

### 2. Data-sourcing strategy

- **Original thinking:** hybrid sourcing: real event APIs (Eventbrite / Ticketmaster
  / Meetup / Messe München) + Gemini Google-Search grounding for discovery, plus
  Gemini enrichment to fill audience profile & geographic origins.
- **Trade-off (why I adjusted):** no existing API keys for those providers, several
  now restrict public search access anyway (Eventbrite locked down since ~2020,
  Meetup requires paid OAuth). Grounding alone still yields real URLs as
  evidence, satisfying the source requirement with one integration surface instead
  of several.
- **How:** keep the hybrid _architecture_, phase the _build_. Discovery sits behind a
  small `EventSource` adapter interface (`search(city, month) -> list[RawEvent]`).
  Phase 1 (now) implements one adapter, `GeminiGroundingSource`. Phase 2 (future
  work) can add `TicketmasterSource` / `EventbriteSource` etc. without touching the
  rest of the pipeline. Gemini enrichment (separate step, always used) fills audience
  profile & geographic origins and normalizes any discovered event to the schema.

### 3. Scope & config parameters

- `city`, `month`, `max_events`, and `use_cache` are all **config parameters**, not
  hard-coded.
- Task fixes `city`/`month` to Munich / Oct 2026, but generalizing them makes the
  system reusable for any city/month with no code changes.
- `max_events` caps results, trading off cost vs. coverage.
- `use_cache` toggles cached vs. fresh API/model calls (see Cost-control principles).

### 4. Output format & schema

**Philosophy.** Output is consumed by **downstream models**, so prefer **structured
numeric/enum fields** everywhere. Reserve **free text only** for `description`, all
`notes`, and `sources[].title`.

**Estimates & evidence.** Audience/origin figures are **modeled estimates**, not
survey data. Every event carries a `data_quality.confidence` score and non-empty
`sources`. Stated explicitly in the README.

**Missing-data / fallback strategy.** Missing data must be **explicit and
predictable**: never empty strings, silent omission, or fabricated values. Schema
stays **stable**: every event always has every field. Per-field fallback values are
defined in "Field tiers" and "Field notes" below. **Enrichment prompt must instruct
Gemini to return** `unknown`**/**`null` **rather than hallucinate** when evidence is weak.

**Full JSON schema (consolidated).** Complete shape of `events_oct_2026.json`:

```json
{
  "meta": {
    "city": "Munich",
    "country": "Germany",
    "month": "2026-10",
    "generated_at": "2026-09-24T20:00:00Z",
    "model": "gemini-3.8-flash",
    "max_events": 15,
    "event_count": 12,
    "schema_version": "1.0"
  },
  "events": [
    {
      "id": "c1a1b2e4-1234-4a5b-9c3d-abcdef012345",
      "name": "EXPO REAL 2026",
      "category": "Trade Fair",
      "description": "Free-text summary of the event.",
      "date": { "start": "2026-10-05", "end": "2026-10-07" },
      "venue": {
        "name": "Messe München",
        "address": "Am Messesee 2, 81829 München",
        "city": "Munich",
        "country": "Germany"
      },
      "estimated_participants": 40000,
      "audience_profile": {
        "interests": ["real estate", "investment", "urban development"],
        "industries": ["real estate", "finance", "construction"],
        "demographics": {
          "age_range": { "min": 30, "max": 60 },
          "professional_level": ["senior", "executive", "investor"],
          "group_types": ["solo_professionals", "corporate_teams"],
          "gender_skew": "male-leaning",
          "notes": "Predominantly B2B real-estate professionals."
        }
      },
      "geographic_origins": {
        "local": 0.2,
        "national": 0.35,
        "international": 0.45,
        "top_countries": ["Germany", "Austria", "UK", "USA", "France"],
        "notes": "Large international investor presence."
      },
      "sources": [
        {
          "title": "EXPO REAL official site",
          "url": "https://exporeal.net/",
          "type": "official_site",
          "retrieved_at": "2026-09-24T20:00:00Z"
        }
      ],
      "data_quality": {
        "confidence": 0.8,
        "fields_estimated": ["geographic_origins", "estimated_participants"],
        "fields_unknown": []
      }
    }
  ]
}
```

**Field tiers (not all fields follow the same fallback rule):**

| Tier              | Fields                                                                 | If missing                                    |
| ----------------- | ---------------------------------------------------------------------- | --------------------------------------------- |
| **1: Must-have**  | `name`, `date.start`, `venue.name`, `sources` (non-empty)              | Drop the event; log to `rejected_events`      |
| **2: Enrichment** | `estimated_participants`, `audience_profile.`_,_ `geographic_origins.` | Use `unknown`/`null`/`[]`; lower `confidence` |
| **3: System**     | `meta.`, `id`                                                          | Never missing (generated by the pipeline)     |

`id`: generated by **code** as a `uuid5` over
`normalize(name) | date.start | city`, stamped after dedupe (stage 2). Never
requested from Gemini. `uuid5` rather than `uuid4` because the enrichment cache key
is `enrich_{event_id}_{prompt_version}`: a random ID would change on every run and
every cached enrichment would miss, re-spending budget. Deterministic IDs also mean
a consumer can diff two runs and see the same event as the same row.

**Field notes / controlled vocabularies.**

- `category`: prefer a small set, e.g.
  `Trade Fair | Conference | Festival | Concert | Sports | Cultural | Community | Other`.
- `estimated_participants`: integer, or `null` if unknown.
- `audience_profile.interests` / `.industries`: string arrays; `[]` if unknown.
- `audience_profile.demographics`: enums + `notes`. Each enum includes a reserved
  `unknown` member:
  - `professional_level`: `student | junior | mid | senior | executive | investor | mixed | unknown`
  - `group_types`: `families | solo_professionals | corporate_teams | students | tourists | couples | young_adults | unknown`
  - `gender_skew`: `male-leaning | female-leaning | balanced | unknown`
  - `age_range`: `{ "min": int, "max": int }` or `null`.
- `geographic_origins`: numeric shares `local + national + international ~= 1.0`
  (or `null`); `top_countries` `[]` if unknown.
- `sources`: array; MUST be non-empty for any event kept in the final output
  (evidence requirement). Each has `title`, `url`, `type`, `retrieved_at`.
  - `type`: `official_site | ticketing_platform | news | search_grounding | other`.
    Lets downstream consumers weight evidence quality; falls back to `other` if
    the provider gives a URL but its nature is unclear (e.g. no direct event page).
  - `retrieved_at`: generated by **code** (system clock when the source was fetched).
- `data_quality.confidence`: float `0.0–1.0`; `fields_estimated` / `fields_unknown`
  list which fields fell back.

### 5. Security

- **Never commit the Gemini API key.** Load from env / `.gitignore`d `.env`.

### 6. Cost-control principles

- Use a cheap model (Gemini Flash).
- Cache raw API/model responses to disk so any re-run (dev or production) avoids
  re-spending budget on repeated calls.
  - `use_cache` **config parameter** (default `true`): `true` reads from
    `cache/` when a matching key exists; `false` forces a fresh API/model call
    and overwrites the cache. Set `false` only when a genuinely fresh
    fetch/estimate is needed (e.g. bumped `prompt_version`, or intentionally
    refreshing stale data).
- Hard cap via `max_events`; batch where possible.

### 7. Pipeline workflow

```
Config (city, month, max_events, use_cache)
        │
        ▼
[1] DISCOVERY: GeminiGroundingSource.search(city, month)
    → raw candidate events: name, rough date, venue, source url/snippet
    Cached: discovery_{city}_{month}
        │
        ▼
[2] NORMALIZE & DEDUPE: clean fields; merge duplicate hits into one
    candidate, combining their sources; stamp id (deterministic uuid5)
    → must happen here: the stage-4 cache key is keyed on the id
        │
        ▼
[3] TIER-1 VALIDATION (gate): name, date.start, venue.name, sources non-empty
    PASS → continue        FAIL → rejected_events.json
        │
        ▼
[4] ENRICHMENT (per candidate): code fetches source URL, strips HTML to
    clean text (free); Gemini call: name + date + venue + cleaned text
    → audience_profile, geographic_origins, estimated_participants
    Cached: enrich_{event_id}_{prompt_version}
    (model instructed: return unknown/null, don't hallucinate)
        │
        ▼
[5] ASSEMBLE & FINAL VALIDATION: code stamps retrieved_at and data_quality;
    pydantic validates full Event
    Fail here → also routes to rejected_events.json
        │
        ▼
[6] WRITE OUTPUT: meta + events[] → events_oct_2026.json
    (+ rejected_events.json for transparency)
```

- Two validation checkpoints (stage 3, stage 5) so discovery-side gaps and
  enrichment-side gaps are distinguished, each with its own logged reason.
- Only stages 1 and 4 are "paid" (API/model calls) and cache-backed; stages
  2/3/5/6 are pure code, free, safe to re-run constantly.
- `max_events`: **no over-fetching.** Discovery is asked for up to `max_events`
  directly. If some candidates fail Tier-1 or final validation, the output simply
  has fewer events; `meta.event_count` reflects the real, possibly-smaller total.
  `max_events` is a cap ("at most"), never a guarantee ("exactly"). Simpler than
  over-fetch + rank/trim logic; acceptable trade-off given budget/time.
- **Dedupe rule (stage 2)**: pure code, no API cost, via `rapidfuzz`. Trade-off:
  semantic dedup via an LLM call or embedding model would catch more paraphrased
  duplicates, but costs money per comparison, so string-similarity is the
  budget-conscious choice here.
  - Two candidates are the same event if `fuzz.token_set_ratio(normalize(a.name), normalize(b.name)) >= 85` AND (date ranges overlap, if both have dates).
    `token_set_ratio` chosen because event titles vary by extra/subtitle words
    (e.g. "EXPO REAL" vs "EXPO REAL - International Trade Fair for Property and
    Investment"); it ignores duplicate/extra tokens rather than penalizing them.
  - On match, **merge, don't discard**: union `sources` (dedupe by URL, strengthens
    evidence), keep whichever record has a date/more detailed venue, keep the
    longest/most descriptive name as the merged name.
- `rejected_events.json` **log schema** (stage 3 and 5 write here):

```json
{
  "meta": {
    "city": "Munich",
    "month": "2026-10",
    "generated_at": "...",
    "rejected_count": 2
  },
  "rejected": [
    {
      "name": "Some Half-Found Event",
      "stage": "discovery_validation",
      "reason": "missing_date",
      "reason_detail": "date.start could not be confirmed from any source",
      "raw_snapshot": {
        "name": "Some Half-Found Event",
        "venue": "Messe München",
        "sources": ["https://example.com/event"]
      },
      "rejected_at": "..."
    }
  ]
}
```

- `stage`: enum `discovery_validation | final_validation` (maps to pipeline
  stage 3 vs. 5).
- `reason`: enum code (`missing_name | missing_date | missing_venue | missing_sources | out_of_range_date | invalid_geographic_origins_sum | invalid_age_range | api_error | fetch_failed | other`),
  machine-filterable.
  - `out_of_range_date` added during implementation: grounding happily returns the
    right event from the wrong month, so stage 3 also checks that the event's date
    range overlaps the requested month.
  - `invalid_geographic_origins_sum` / `invalid_age_range` are Tier-2 fields, so in
    practice they **degrade to null and cost confidence** rather than rejecting the
    event (per the Field-tiers table). They stay in the enum as the final-validation
    safety net for anything sanitization misses.
- `reason_detail`: free text, the actual validation error message.
- `raw_snapshot`: whatever partial data was collected before rejection, so a
  failure is inspectable without re-running the pipeline.
- `rejected_at`: code-generated timestamp, same treatment as `retrieved_at`.
- **Error/retry policy:** discovery retries up to 3 attempts (`tenacity`,
  exponential backoff); if still failing, abort the run. Enrichment does not
  retry; any failure drops that event to `rejected_events.json`
  (`reason: "api_error"`) and the pipeline continues.

### 8. Data management & file layout

```
ReluGroup-Interview-Task/
├── events_oct_2026.json        ← FINAL DELIVERABLE (task requires this exact name)
├── rejected_events.json        ← transparency log: events dropped at Tier-1/final validation
├── cache/                      ← gitignored: raw API/model responses (cost-saving, regenerable)
│   ├── discovery/
│   │   └── munich_2026-10.json
│   └── enrichment/
│       └── <event_id>_<prompt_version>.json
├── prompts/
│   └── v1/
│       ├── discovery.txt       ← versioned discovery prompt template
│       └── enrichment.txt      ← versioned enrichment prompt template
├── src/
│   ├── config.py
│   ├── models.py
│   ├── cache.py
│   ├── prompts.py
│   ├── fetch.py
│   ├── dedupe.py
│   ├── sources/
│   │   ├── base.py             ← EventSource protocol (the Phase-2 seam)
│   │   └── gemini_grounding.py
│   ├── enrichment.py
│   └── pipeline.py
├── scripts/
│   └── verify_output.py        ← re-validates a generated file, checks source URLs
├── tests/
│   └── test_pipeline.py        ← covers the free stages, no API calls
├── DECISIONS.md
├── README.md
├── .env.example                 ← env template (`GEMINI_API_KEY`, `GEMINI_MODEL`, `PROMPT_VERSION`, `PROMPT_DIR`)
├── .env                         ← gitignored: local secrets and prompt settings
├── requirements.txt
├── requirements-dev.txt
└── .gitignore
```

- **Committed:** `events_oct_2026.json`, `rejected_events.json` (deliverable +
  transparency).
- **Gitignored:** `cache/` (regenerable, run-specific), `.env` (never commit the
  key).
- **Naming rule:** default output filename is generic (`events_{city}_{month}.json`)
  to match the config-driven `city`/`month` (Decision #3), but this submission's
  run pins `output_path` explicitly to `events_oct_2026.json` as the task requires.
- **Prompt versioning rule:** prompt text is externalized under
  `prompts/{prompt_version}/`. Switching prompt behavior is done by changing
  `PROMPT_VERSION` (or `--prompt-version`), not by editing code.

### 9. Progress / observability during runs

- The pipeline prints a live progress line per stage so a run is visible while it
  happens, not just at the end. Pure code, no API cost, uses Python's `logging`
  module (not bare `print`) so verbosity is controllable via a `--verbose` flag.
- What gets logged, kept to just the three moments that matter:
  - **Discovery:** "Discovery: searching Munich / 2026-10..." then "Discovery:
    found N candidates" (or attempt/retry messages from `tenacity`).
  - **Enrichment:** one line per event as it's processed, e.g.
    "[3/15] Enriching: EXPO REAL 2026 (cache hit)" / "(calling Gemini)" /
    "(failed, dropped)".
  - **Final results:** "Done: N events written to events_oct_2026.json,
    M rejected to rejected_events.json."

## Open / next

Phase 1 is built, run, and delivered. What is left:

- Phase 2: `TicketmasterSource` / `EventbriteSource` adapters behind the existing
  `EventSource` protocol.
- Dedupe is string-similarity only. Embedding-based matching would catch
  paraphrased duplicates across sources, at a cost per comparison.
- Origin shares are modelled, never measured. The honest upgrade is a real source
  (exhibitor or visitor statistics published by Messe München and similar), parsed
  rather than estimated.
  ```json
  "geographic_origins": {
      "local": 0.2,
      "national": 0.35,
      "international": 0.45,
      ...}
  ```
