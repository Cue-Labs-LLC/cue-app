# Technical Design Document — Market Competition Agent

**Status:** Approved (hardened via `/plan-eng-review` 2026-10-09) · **Scope:** new feature (per-event competitive-density signal) · **Delivery:** phased, test-first (each phase independently shippable)

> This is a TDD, not a single-shot build. Each phase below lists the tests to write **first**, the implementation scope, and acceptance criteria. Phases land in order; most are demoable on their own. This document is the living guide for the phased implementation — each phase maps to a PR; check off phases in the Progress tracker (§8) as they land. Engineering-review decisions (D1–D8, DO1–DO5) are recorded in §10.

---

## 1. Context & problem

Organizers want to know, for a given event, **how much competition there is from
similar events in the same market around that event's date** — e.g. "3 other
hip-hop shows in your metro that weekend." Cue **cannot answer this today**: every
`Event`/`Venue`/`Market` is hard-scoped to one organization (no cross-org
visibility), there is **no geolocation** (only `venue.city/state/country` text,
no lat/lng — `address_utils.py` geocodes but discards coordinates), and there is
**no event genre/category taxonomy** (only per-org `CustomField` dropdowns and
`EventTalent` lineup names). So the metric fundamentally requires pulling in
**external** data about events Cue doesn't own.

### Goals
- A per-event competition read: the **list of similar competing events** found,
  a **coarse count** ("~4 similar shows in your metro that weekend"), and a short
  **AI narrative**. A **0–100 score + Low/Med/High label** is computed, but its
  numeric display is **gated behind the Phase-6 eval meeting a precision bar**
  (DO1) — until then the panel leads with the list + count + a coverage signal,
  not a number, so the POC never shows false precision.
- Reach ticketing platforms **without** a discovery API (Eventbrite, DICE, See
  Tickets, venue box-office sites) — via a web-search layer, no per-site scrapers.
- Ship as a **POC**; keep the data-source swap point so a structured events API
  (Ticketmaster/SeatGeek/PredictHQ) drops in later (D1).
- On-demand from the event detail page; results cached on the event.

### Non-goals (this POC)
- True distance/radius search + venue geocoding (metro-text only for now — see D3).
- A structured events-API source, and the `CompetitorEventSource` ABC itself —
  deferred until a real second source exists to inform the interface (D1=B).
- Price/size (same-wallet) similarity dimension (D4-D, deferred).
- A ReAct/agentic search loop — the POC uses a fixed query plan + LLM extraction
  (DO3=B); an adaptive agent is a documented future option.
- Scheduled/auto re-scan, or a portfolio-wide competition rollup.
- Per-site scrapers (ToS/maintenance risk; search-API layer only).

## 2. Confirmed decisions (with the user)

- **D1 — Data source = web layer first (POC), interface extracted later.** Start
  with one concrete web-search source. Do **not** build the `CompetitorEventSource`
  ABC yet — the calculator picks its source behind one function call, and the
  abstract interface is extracted when a real structured-API source arrives to
  inform its shape (DO3-adjacent; "make the change easy, then make the easy change").
- **D5 — Mechanism = web-search API + LLM extraction.** One API key, no
  per-site scrapers (lowest ToS risk, zero per-site maintenance). **POC provider:
  Tavily** via `requests` (no new pip dependency), provider name configurable.
- **D2 — Output = (eval-gated) score + label + breakdown + AI narrative.** The
  breakdown (the competing events) keeps the read honest; the narrative makes it
  actionable; the numeric score is gated (DO1).
- **D3 — Geo scope = same city/metro (POC).** Metro membership is decided by an
  LLM-normalized metro emitted during extraction (DO5-e), not a hand-maintained
  map. Upgrade to radius when the structured-API phase lands.
- **D4 — "Competitor" = genre/category + date proximity (±N days) +
  geographic proximity (metro).** Price/size tier deferred.
- **DO3 — Orchestration = fixed query plan, not a ReAct agent.** The queries are
  a pure function of the event (per genre hint, city+date-range, named platforms),
  so code builds the query list and issues a fixed set; the LLM does the fuzzy
  work (extraction/normalization), the scorer is deterministic. No agentic loop,
  no per-scan tool-call cap needed.

## 3. Architecture at a glance

Event detail page → organizer clicks **Scan market competition** → POST trigger
checks the per-org daily cap (D3), sets a TTL'd cache in-progress lock (D4), and
enqueues a Celery task → task runs `calculate_event_competition(org, event)`:

```
event fields (name, talent, genre hints, city/state, date span, ±window)
        │
        ▼
query_plan: build a FIXED list of search queries (DO3)
   - one per derived genre hint
   - "{city} events {date range}"
   - one per named non-API platform (eventbrite/dice/see tickets/…)
        │
        ▼
search_client.web_search(q)  × N   (Tavily via requests; cached; fail-silent)
        │   (messy, unstructured snippets)
        ▼
LLM extraction (ONE structured call) → list[CompetitorEvent]:
   normalizes each → {name, date|None, venue, metro_normalized (DO5-e),
   genre, platform, source_url}; dedupes across platforms (DO5-c);
   excludes the target event itself (DO5-a); records coverage (DO2)
        │
        ▼
scoring.score_competition(...) — PURE, deterministic:
   filters to D4 matches (same normalized metro, within ±window of the
   event's DATE SPAN (DO5-b), genre overlap w/ no-genre fallback (DO5-f));
   no-date events excluded from count/score, listed separately (DO5-d);
   → score 0–100 + label + counts + coverage/confidence (DO2)
        │
        ▼
LLM narrative (one call; failure degrades gracefully, score still persists — D6)
        │
        ▼
persist on Event row (cache-on-model + input-hash; hash written ONLY on
success — D5/DO4) → poll endpoint feeds the panel (bounded cadence — D8)
```

Runs eagerly in dev (no Redis needed for the POC).

**Why fixed-plan + deterministic scorer, not a ReAct agent (DO3):** the query set
is knowable from the event upfront, so an agentic loop would pay cost/latency/
nondeterminism to "decide" searches that are already known, and you couldn't unit
-test which searches ran. The LLM stays where it earns its keep — turning messy
snippets into structured, normalized rows. An adaptive agent is a future option
if multi-step reasoning ever earns it.

New package `tickets/services/market_competition/` (mirrors
`tickets/services/chat/` + `tickets/services/instagram/`):

```
market_competition/
├── __init__.py        # calculate_event_competition(organization, event) entry point
├── types.py           # CompetitorEvent + CompetitionResult dataclasses
├── scoring.py         # pure score_competition(target, competitors, *, window_days)
├── search_client.py   # requests-based web-search wrapper (weather.py style)
├── query_plan.py      # build_queries(event, genre_hints) -> list[str]  (DO3, fixed plan)
├── scanner.py         # run queries + LLM extraction/normalization → list[CompetitorEvent]
└── calculator.py      # MarketCompetitionCalculator(organization).calculate(event)
```

(No `sources.py` ABC in the POC — D1=B. The calculator calls a single scanner
function; the interface is extracted when a second source lands.)

Reuses: `weather.py` (external fetch discipline — timeout, versioned cache,
failure sentinel, fail-silently-and-log); `sms_strategist.py:451-466`
(`.with_structured_output` extraction); `event_summary.py:201-235`
(cache-on-Event + input-hash + `record_ai_token_usage`); `ai_tracing.trace_config`;
`recalculate_rfm_task` (`tasks.py:444-459`) task shape; per-event AJAX endpoints
(`urls.py:199-201`); the per-org daily-cap pattern (`IG_AGENT_DAILY_ANSWER_CAP`,
`settings.py:418`).

---

## 4. Detailed design reference

Shared reference the phases point to. (All queries org-scoped per the
multi-tenancy rule; new Event/Org fields are additive.)

### 4.1 Data model (`tickets/models.py`)
- **`Event`** new fields (mirror `ai_summary*`, `models.py:1500-1506`), one
  additive migration:
  - `competition_score` `PositiveSmallIntegerField(null=True)`
  - `competition_label` `CharField(max_length=20, blank=True)`
  - `competition_status` `CharField(max_length=20, blank=True)` — one of
    `ready | unavailable | inconclusive` (D2/DO2): `unavailable` = never scanned /
    key missing / search outage; `inconclusive` = scanned but coverage too low to
    trust a 0; `ready` = trustworthy result (incl. a genuine 0).
  - `competition_data` `JSONField(default=dict)` — serialized competitor list +
    `counts` breakdown + coverage + narrative + no-date-event list (renders
    without recompute)
  - `competition_generated_at` `DateTimeField(null=True)`
  - `competition_input_hash` `CharField(max_length=64, blank=True)` — sha256 of
    `(name, talent, city, state, start_date, end_date, window)` (DO5-b includes the
    date span); **written only on a successful scan** (D5). Used only to suppress
    re-scans **within** the freshness TTL window (DO4) — see §4.2.
- **`Organization`** new fields (`*_enabled` convention, near `models.py:301`):
  - `market_competition_enabled` `BooleanField(default=False)` — master rollout
    gate; feature ships dark, turned on per org in the admin Feature Flags fieldset.
- **`AITokenUsage`** (`models.py:~3195`): add `FEATURE_MARKET_COMPETITION =
  'market_competition'` + choice; distinguish calls with
  `metadata={'stage':'extract'|'narrative'}`.
- **In-progress guard (D4):** a cache lock `market_comp:scan:{event_id}` set with a
  **TTL** (e.g. 300s, safely above worst-case scan time) before enqueue and cleared
  in the task `finally`. The TTL lets a crashed/killed worker self-heal instead of
  wedging the event in "scanning…" forever.

### 4.2 Key interfaces (`tickets/services/market_competition/`)
- `types.py`:
  - `@dataclass CompetitorEvent(name, date: date|None, venue_name,
    metro_normalized, genre, platform, source_url)`.
  - `@dataclass CompetitionResult(score: int, label: str, status: str,
    competitors: list[CompetitorEvent], undated: list[CompetitorEvent], counts:
    dict, coverage: dict, summary: str)`.
- `query_plan.py` (DO3): `build_queries(event, genre_hints) -> list[str]` — pure,
  fully testable: one query per genre hint, a `"{city} events {date range}"`
  query, and one per configured non-API platform. No LLM, no I/O.
- `search_client.py`: `web_search(query, *, max_results) -> list[dict]` on
  `requests` exactly like `weather.py` — `HTTP_TIMEOUT_SECONDS`, custom UA,
  `django_cache` versioned key (`market_comp:search:v1:{hash(query)}`), ~6h TTL +
  short failure-sentinel TTL, `try/except (requests.RequestException, ValueError)`
  → log + return `[]`. POC provider Tavily (`POST https://api.tavily.com/search`,
  key in body), selected by `settings.MARKET_COMPETITION_SEARCH_PROVIDER`. Tracks,
  per scan, how many queries returned usable results → feeds `coverage` (DO2).
- `scanner.py`: `scan_competitors(organization, event, genre_hints) ->
  (list[CompetitorEvent], coverage: dict, TokenUsage)`. Runs `build_queries`,
  issues each via `web_search`, then a single `.with_structured_output(ExtractedEvents)`
  LLM pass (pattern from `sms_strategist.py:451-466`, traced via
  `trace_config(name='market-competition-extract', ...)`) that for every found
  event emits `{name, date|None, venue, metro_normalized (DO5-e), genre, platform,
  source_url}`, **dedupes** across platforms by fuzzy (name + date + venue) key
  (DO5-c), and **excludes the target event itself** by (name + date + venue) match
  (DO5-a). `metro_normalized` is the LLM's normalized metro for the event; the
  target venue's city is normalized the same way once, so the scorer compares
  normalized values (no hand-maintained map — DO5-e). Metro-normalization accuracy
  is an eval target (§5 P6).
- `scoring.py`: `score_competition(target, competitors, undated, coverage, *,
  window_days) -> CompetitionResult` — **pure, deterministic, no I/O**. Filters to
  D4 matches: same `metro_normalized`; within `±window_days` of the event's **date
  span** (`start_date`..`end_date`, DO5-b); genre overlap with target hints, with a
  **no-confident-genre fallback** (DO5-f: if the target has no confident genre,
  fall back to a city+date density count and drop the genre weight). **No-date
  events are excluded from the count/score** and returned in `undated` to be listed
  separately (DO5-d). Computes a transparent weighted score (count × date-closeness
  × genre-strength) → 0–100, mapped to bands via module constants (`LOW_MAX=24`,
  `MEDIUM_MAX=59`, else High). Sets `status`: `inconclusive` when `coverage` is
  below a threshold and the score would be ~0 (DO2), else `ready`. Returns the
  filtered list + `undated` + `counts` + `coverage`.
- `calculator.py` / `__init__.py`:
  `MarketCompetitionCalculator(organization).calculate(event) -> CompetitionResult`
  (standard `__init__(self, organization)` + method pattern). Guard (venue+city+
  start_date, else `status='unavailable'`); if `TAVILY_API_KEY` unset or all
  searches fail → `status='unavailable'` (never a 0/Low — D2). Else derive genre
  hints from `event.name` + `EventTalent` + `description` → `scanner.scan_competitors`
  → `score_competition` → **narrative** via one `ChatOpenAI.invoke` wrapped so a
  failure logs + returns empty summary with the score still persisting (D6,
  `event_summary.py:192-194` style) → `record_ai_token_usage(feature=
  FEATURE_MARKET_COMPETITION)` → return. `__init__.py` exposes
  `calculate_event_competition(organization, event)` as the single public entry.

### 4.3 Config (`ltv_updater/settings.py`, `os.environ.get` pattern @ 216-220)
- `TAVILY_API_KEY` (**required to use the feature**; absent → `status='unavailable'`,
  logged — never a silent 0, D2).
- `MARKET_COMPETITION_SEARCH_PROVIDER` (default `'tavily'`).
- `MARKET_COMPETITION_DATE_WINDOW_DAYS` (default `3`).
- `MARKET_COMPETITION_MAX_RESULTS` (default `25`).
- `MARKET_COMPETITION_RESULT_TTL_DAYS` (freshness; a result older than this forces
  a re-scan regardless of input hash — DO4; default `7`).
- `MARKET_COMPETITION_DAILY_SCAN_CAP` (per-org scans/day; past it the trigger
  returns a capped status and does not enqueue — D3; mirrors
  `IG_AGENT_DAILY_ANSWER_CAP`; `0` disables).
- `MARKET_COMPETITION_PLATFORMS` (comma-separated non-API platform names seeded
  into the query plan; default `eventbrite,dice,seetickets`).
- No new pip dependency (Tavily via `requests`; extraction/narrative reuse
  existing `langchain_openai`).
- **Per-scan cost (DO5-g):** a scan issues N (≈ genres + 1 + platforms, typically
  5–8) Tavily searches + 1 extraction LLM call + 1 narrative LLM call. Size the
  daily cap against that; record actuals via `AITokenUsage` + log search counts.

---

## 5. Phased delivery (test-first)

### Phase 0 — Design doc + data model & foundations
- **Step 1 (doc):** this document committed at `docs/technical-design/market-competition-agent.md`.
- **Tests first** (`tickets/tests_market_competition.py`): Event new-field
  defaults (null score, empty label/status/hash, `{}` data);
  `Organization.market_competition_enabled` default False;
  `AITokenUsage.FEATURE_MARKET_COMPETITION` choice present. Pin all new settings
  with `@override_settings` (prior learning: `env-sensitive-sms-cap-tests` — an
  untracked local `.env` otherwise silently skews cap/window tests).
- **Build:** the 6 Event fields + Org flag + `AITokenUsage` constant; one
  additive migration (no data migration); admin Feature Flags fieldset entry.
- **Accept:** doc committed; `makemigrations` clean, `migrate` applies, model
  tests green. No user-facing change.

### Phase 1 — Deterministic scoring + query plan (pure, offline)
- **Tests first:** `score_competition` over crafted lists — empty → 0/Low;
  dense same-genre same-weekend same-metro → High; D4 filters each exclude
  correctly (wrong metro / outside ±window / non-matching genre); **date-span
  window** (DO5-b: a multi-day event matches competitors near either end);
  **no-date events excluded from score, returned in `undated`** (DO5-d);
  **no-confident-genre fallback** (DO5-f); **low coverage → `inconclusive`**
  (DO2); band boundaries 24/25, 59/60; `counts`/`coverage` shape. `build_queries`
  produces the exact expected query set for a given event (DO3 — pins the plan).
- **Build:** `types.py`, `scoring.py`, `query_plan.py` (all pure; no I/O, no LLM).
- **Accept:** scoring + query-plan tests green; the metric's math and the search
  plan are pinned before any nondeterministic input touches them.

### Phase 2 — Search client (offline-testable)
- **Tests first:** `web_search` with **mocked `requests`** — success parses;
  timeout/HTTP-error/JSON-error → `[]` + warning; cache hit skips the call;
  failure sentinel short-circuits; missing key → `[]` (feeds `unavailable`).
  Coverage accounting (how many queries returned usable results).
- **Build:** `search_client.py` (weather.py-style) + a `market_search` management
  command to run a raw query for manual inspection.
- **Accept:** search-client tests green; raw search prints results with
  `TAVILY_API_KEY` set; graceful empty without it.

### Phase 3 — Scanner + calculator (LLM, mocked in tests)
- **Tests first (patch `langchain_openai.ChatOpenAI` + the search client):**
  scanner returns normalized `CompetitorEvent` rows from canned search results;
  **dedupe across platforms** (DO5-c: same show on 3 sites → 1 row); **self-
  exclusion** (DO5-a: the target event is not in its own competitor list — a
  pinned regression-class case); **metro normalization** emitted (DO5-e);
  `calculate` returns a `CompetitionResult`; guard → `status='unavailable'` when
  event lacks venue/city/date OR key unset (D2); **narrative failure → score +
  list persist, empty summary** (D6); `record_ai_token_usage` called with
  `FEATURE_MARKET_COMPETITION` (stages `extract` + `narrative`).
- **Build:** `scanner.py` (fixed-plan search + structured extraction/normalization/
  dedupe/self-exclude), `calculator.py` + `__init__.py`, narrative (graceful),
  tracing + metering. `scan_event_competition <event_id>` management command runs
  the full pipeline synchronously.
- **Accept / demo:** CLI command against a real seeded event (with
  `TAVILY_API_KEY`) prints list / count / coverage / (ungated) score / narrative.

### Phase 4 — Celery task + persistence + feature gate
- **Tests first (eager Celery):** task persists to Event fields on success;
  **input-hash written ONLY on success** (D5); **a failed/`unavailable` scan
  leaves hash empty and re-runs next time** (D5 — CRITICAL regression-class test);
  **unchanged hash within TTL → skip; past TTL → re-scan regardless of hash**
  (DO4); **daily cap reached → not enqueued** (D3); lock set with TTL + cleared in
  `finally` even on exception (D4); `self.retry` on failure; org-scoped load.
- **Build:** `scan_event_competition_task` (`@shared_task(bind=True,
  max_retries=2, default_retry_delay=30)`, string `event_id`, function-local
  imports), `_persist_competition` helper (event_summary style, success-only hash),
  TTL'd cache lock, daily-cap check.
- **Accept:** task persists; failed scan re-runs; stale (TTL) result re-scans;
  cap enforced; runs eagerly in dev without Redis.

### Phase 5 — Views + event-detail UI panel
- **Tests first (Django `Client`):** POST scan requires `market_competition_enabled`
  (else 404) + org-scoped (cross-org → 404) + daily-cap gate (D3) + single enqueue
  (lock, D4) + returns `scanning`; GET poll returns `idle|scanning|ready|
  unavailable|inconclusive`; `event_detail` context carries cached result;
  `unavailable`/`inconclusive` render distinctly from a genuine `ready` 0 (D2/DO2).
- **Build:** `events/<uuid:event_id>/competition/scan/` (POST) +
  `events/<uuid:event_id>/competition/` (GET) views + URLs (precedent
  `urls.py:199-201`); `event_detail` context; a panel in `event_detail.html`
  (dashboard.css; leads with the competitor list + coarse count + coverage;
  numeric score shown **only when P6 says it's earned**, DO1; "couldn't scan" /
  "inconclusive" / "summary unavailable" states; Scan button). Poll client uses a
  **bounded cadence** (~3s interval, max ~2min, stop on terminal state — D8). No
  new sidebar link (lives on the event page).
- **Accept / demo:** enable the org flag, open an event, click Scan → panel polls
  (bounded), populates list+count+coverage, persists on reload; failure states
  render honestly.

### Phase 6 — Eval harness (in-phase, GATES the numeric score — DO1/D7)
This phase is **not optional and not after-the-fact**: the numeric score stays
hidden in the UI until this eval demonstrates extraction is trustworthy.
- **Tests first / corpus:** a seeded ground-truth corpus of events with known
  local-market competitors; a Langfuse dataset mirroring `eval_ig_agent`.
- **Build:** `eval_market_competition` command scoring **extraction precision/
  recall** of competing events, **date-extraction precision** (DO5-d gate),
  **metro-normalization accuracy** (DO5-e gate), and **scoring sanity** (dense →
  High, empty → Low). Define the precision bar that unlocks numeric-score display
  (DO1).
- **Accept:** eval run in Langfuse with the above scores; score display is enabled
  only once the bar is met (otherwise the POC keeps showing list + count only).

### Future phase — Structured events-API source + radius + agent (OUT OF SCOPE)
- A second source (Ticketmaster/SeatGeek/PredictHQ); **this is when the
  `CompetitorEventSource` ABC is extracted** (D1=B). D3=A radius via lat/lng
  (extend `address_utils.py` to keep coordinates it currently discards); D4-D
  price/size dimension; an adaptive agent if multi-step search ever earns it (DO3).

**Demo milestones:** Phase 3 (CLI) and Phase 5 (in-app) demo on the web-search
source with no structured API and no Redis; the numeric score lights up after P6.

---

## 6. Risks
- **False precision (DO1)** — a 0–100 score over-promises on noisy inputs. Mitigate:
  lead with the list + coarse count + coverage; gate the numeric score on the P6
  eval precision bar.
- **Absence-as-evidence (DO2)** — a successful-but-empty search looks like "Low (0),
  you're clear." Mitigate: coverage signal → `inconclusive` state distinct from a
  genuine 0 and from `unavailable`.
- **Noisy external data** — wrong dates, duplicates, non-events, the target itself.
  Mitigate: structured extraction with dedupe (DO5-c), self-exclusion (DO5-a),
  no-date exclusion (DO5-d); breakdown UI surfaces the raw list.
- **Date reliability (DO5-d)** — the ±window is only as good as extracted dates;
  no-date events are excluded from the score and date precision is a P6 gate.
- **Metro matching (DO5-e)** — free-text cities over/under-match; mitigate with
  LLM-normalized metro compared deterministically, accuracy measured in P6.
- **Lock wedging (D4)** — a crashed worker could pin "scanning…"; mitigate with a
  lock TTL.
- **Stale-forever (DO4)** — internal-only input-hash would never refresh an
  external landscape; mitigate by letting the TTL force re-scans.
- **Cost (D3/DO5-g)** — several searches + 2 LLM calls per scan; mitigate with the
  per-org daily cap (fixed query count already bounds the per-scan spend — no loop).
- **ToS/legal** — search-API layer (reads an index) is lower-risk than scraping;
  no per-site scrapers in the POC.

## 7. Run commands
`python manage.py makemigrations tickets && python manage.py migrate && python manage.py test tickets`
→ `python manage.py market_search "<query>"` (P2) → `python manage.py
scan_event_competition <event_id>` (P3, needs `TAVILY_API_KEY` + `OPENAI_API_KEY`)
→ `python manage.py eval_market_competition` (P6, needs `LANGFUSE_*`) →
`runserver`, enable the org flag, open an event, click Scan.

## 8. Progress tracker
- [ ] **P0** — Design doc committed + Event/Org fields (incl. `competition_status`) + `AITokenUsage` constant + migration
- [ ] **P1** — Pure scoring + query plan (`types.py`, `scoring.py`, `query_plan.py`) — test-pinned (DO5-b/d/f, DO2, DO3)
- [ ] **P2** — Search client (mocked-requests tests) + coverage accounting + `market_search` command
- [ ] **P3** — Scanner (fixed-plan + extraction/normalize/dedupe/self-exclude) + calculator + narrative (graceful) + metering + `scan_event_competition` command
- [ ] **P4** — Celery task + persistence (success-only hash, TTL re-scan) + TTL'd lock + daily cap + feature gate
- [ ] **P5** — Scan/poll endpoints + event-detail panel (list+count+coverage, bounded poll, honest failure states)
- [ ] **P6** — Eval harness (GATES numeric score): extraction/date/metro precision + scoring sanity

## 9. NOT in scope (considered, deferred)
- Structured events-API source + the `CompetitorEventSource` ABC — extracted when a 2nd source exists (D1=B).
- True radius + venue geocoding (D3=A) — next phase.
- Price/size (same-wallet) similarity (D4-D).
- Adaptive ReAct agent — fixed query plan for the POC (DO3); revisit if multi-step search earns it.
- Per-site scrapers — ToS/maintenance; search-API layer only.
- Scheduled/auto re-scan; portfolio-wide competition rollup.

## 10. Review hardening (from /plan-eng-review, 2026-10-09)

| # | Decision | Lands in |
|---|----------|----------|
| D1 | One concrete web-search source now; extract `CompetitorEventSource` ABC when a 2nd source lands | §2, §3, §9 |
| D2 | Distinct `unavailable` status on missing key/failure — never a silent 0/Low | §4.1, §4.2, §4.3 |
| D3 | Per-org daily scan cap (fixed query plan already bounds per-scan spend) | §4.3, P4/P5 |
| D4 | In-progress cache lock has a TTL (crash self-heals) | §4.1, P4 |
| D5 | Input-hash written only on success; failed scans re-run | §4.1, §4.2, P4 |
| D6 | Narrative LLM failure degrades gracefully (score persists) | §4.2, P3 |
| D7 | Eval is in-phase and gates the numeric score (was optional) | P6 |
| D8 | Bounded browser poll cadence + stop condition | §4.2, P5 |
| DO1 | Score computed but numeric display gated on P6 precision bar; POC leads with list + count + coverage | §1, §4.1, P5/P6 |
| DO2 | Coverage/confidence signal; low-coverage-0 → `inconclusive`, not Low | §4.1, §4.2 |
| DO3 | Fixed query plan + LLM extraction, not a ReAct agent (no tool-call cap) | §2, §3, §4.2 |
| DO4 | TTL forces re-scan; input-hash suppresses only within the TTL window | §4.1, P4 |
| DO5-a | Self-exclusion: the target event isn't its own competitor | §4.2, P3 |
| DO5-b | Window + hash use the event's date span (`start_date`..`end_date`) | §4.1, §4.2, P1 |
| DO5-c | Specified fuzzy (name+date+venue) cross-platform dedup key | §4.2, P3 |
| DO5-d | No-date events excluded from score, listed separately; date precision a P6 gate | §4.2, P1/P6 |
| DO5-e | LLM-normalized metro emitted in extraction; scorer compares normalized values; accuracy a P6 gate | §4.2, P6 |
| DO5-f | No-confident-genre fallback (city+date density, drop genre weight) | §4.2, P1 |
| DO5-g | Per-scan cost estimate to size the daily cap | §4.3 |

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | — |
| Codex Review | `/codex review` | Independent 2nd opinion | 1 | errored | model unsupported for account; fell back to Claude subagent |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | issues_found | 8 review findings (D1–D8) + 13 outside-voice findings → 18 decisions folded (D1–D8, DO1–DO5); 0 unresolved |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | — |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | — |

- **CROSS-MODEL:** Codex was unavailable (gpt-5.4 not supported for this ChatGPT account); an independent Claude subagent ran as the outside voice and raised 13 findings the 4-section review missed. The highest-leverage ones reshaped the design: DO1 (gate the score — false precision), DO3 (drop the ReAct loop — the queries are static), DO4 (input-hash vs TTL staleness contradiction). All 13 were presented to the user; 12 accepted (one, the alias/metro map, was replaced with LLM metro normalization per the user), 0 rejected.
- **VERDICT:** ENG CLEARED — plan hardened, 18 decisions folded, Phase 0 ready to implement. Design/CEO reviews optional; a light design pass is worth it before P5's event-detail panel.

NO UNRESOLVED DECISIONS
