# AI Travel Planner

A multi-agent travel-planning system built with **LangGraph**, served over
**FastAPI**, with a **human-in-the-loop (HITL)** approval gate. Give it your
trip preferences; it researches the destination, drafts a day-by-day
itinerary with a budget breakdown, then **pauses for your approval** before
finalizing — and lets you *approve*, *reject*, or *modify* the draft.

> Built for the Express Analytics AI/ML Engineer take-home. Scope is kept
> deliberately tight — a clean 5-node graph that works flawlessly — per the
> brief's stated value of *clear thinking over feature completeness*.

---

## Architecture

```
┌───────────────────────────────────────────────────────────────────────┐
│                            FastAPI Layer                                │
│   POST /plan │ GET /plan/{id} │ POST /plan/{id}/review │ GET …/final     │
└───────────────────────────────┬─────────────────────────────────────────┘
                                │  ainvoke / Command(resume=…)
                       ┌────────▼─────────┐
                       │   Orchestrator   │   LangGraph StateGraph
                       │   (StateGraph)   │◄──┐ AsyncSqliteSaver
                       └────────┬─────────┘   │ thread_id = plan_id
                                │             │ (state persists the pause)
        ┌───────────────┬───────┴───────┬─────┴──────────┐
   ┌────▼─────┐   ┌─────▼──────┐   ┌────▼─────┐    ┌──────▼─────┐
   │ Research │──▶│  Planner   │──▶│  Human   │    │  Finalize  │──▶ END
   │  Agent   │   │  Agent     │   │  Gate    │    └──────▲─────┘
   └────┬─────┘   └─────┬──────┘   │interrupt()│          │ approve
        │               │          └────┬──────┘          │
   web_search      allocate_budget       │  reject ───────┘ (→ Research)
   get_weather     build_day_schedule    │  modify ─────────► Planner
```

> A rendered diagram can be placed at `docs/architecture.png` and embedded
> here with `![architecture](docs/architecture.png)`.

**Five nodes:**

| Node | Responsibility | Tools |
|------|----------------|-------|
| `orchestrator` | Initialise run state, set status, route to research | — |
| `research_agent` | Gather destination context + weather, distil to highlights | `web_search`, `get_weather` |
| `planner_agent` | Produce a budgeted, day-by-day draft itinerary | `allocate_budget`, `build_day_schedule` |
| `human_gate` | **`interrupt()`** — pause and surface draft for review | — |
| `finalize` | Format the approved plan, mark complete | — |

**Three review paths** out of the gate:
`approve → finalize` · `reject → research_agent` (full redo with feedback) ·
`modify → planner_agent` (partial redo — research untouched).

---

## 5-Minute Quickstart

```bash
# 1. Clone & enter
git clone <your-repo-url> && cd travel-planner

# 2. Install (Python 3.11+). Editable install reads pyproject.toml.
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 3. Configure keys
cp .env.example .env
#   edit .env → add GOOGLE_API_KEY (https://aistudio.google.com/app/apikey)
#               and TAVILY_API_KEY (https://app.tavily.com)

# 4. Run
uvicorn app.main:app --reload
#   → open http://127.0.0.1:8000/docs  (interactive Swagger)
```

**No keys handy?** Run in stub mode — deterministic LLM/search, zero keys:

```bash
USE_STUBS=true uvicorn app.main:app --reload
```

### Try it end to end (stub mode shown)

```bash
# Create a plan → returns {plan_id, status}
curl -s -X POST localhost:8000/plan -H 'Content-Type: application/json' -d '{
  "destination":"Kyoto, Japan","start_date":"2026-09-10",
  "end_date":"2026-09-13","budget_usd":2500,"travelers":2,
  "interests":["food","temples","hiking"]}'

# Poll until status == awaiting_review, inspect the draft
curl -s localhost:8000/plan/<plan_id>

# Reject with feedback (loops back through research)
curl -s -X POST localhost:8000/plan/<plan_id>/review \
  -H 'Content-Type: application/json' \
  -d '{"action":"reject","feedback":"More outdoor activities please."}'

# Approve → finalize, then fetch the final plan
curl -s -X POST localhost:8000/plan/<plan_id>/review \
  -H 'Content-Type: application/json' -d '{"action":"approve"}'
curl -s localhost:8000/plan/<plan_id>/final
```

### Run the test

```bash
USE_STUBS=true pytest -q   # one e2e HITL round-trip: submit→reject→modify→approve→final
```

---

## Design Decisions & Tradeoffs

- **Modern HITL primitive (`interrupt()` + `Command(resume=…)`).** Instead of
  the older `interrupt_before=[...]` compile flag, the pause lives *inside*
  `human_gate` via `langgraph.types.interrupt()`, and the API resumes with
  `Command(resume={"action","feedback"})`. Cleaner control flow and the
  resume payload is delivered exactly where it's needed.

- **`thread_id == plan_id` with `AsyncSqliteSaver`.** This is the entire
  "state persistence across the pause" requirement solved in one line of
  config. Every `/review` call against a `plan_id` automatically resumes the
  exact paused checkpoint — no hand-rolled in-memory state dict (the common
  failure mode that breaks on restart).

- **Tavily over Serper/Exa.** Tavily returns LLM-ready *summarised snippets*
  rather than raw HTML, which reduces token usage and shrinks the
  prompt-injection surface. One-line swap if you prefer another provider
  (`app/services/search.py`).

- **Open-Meteo for weather — and *why* it's not decorative.** It needs no API
  key, and weather **directly shapes the itinerary**: rainy days are tagged
  `indoor`-focused by `build_day_schedule`. Every tool feeds the next
  decision; none are padding.

- **Small, auditable LLM role.** Deterministic *pure functions*
  (`allocate_budget`, `build_day_schedule`) own the structure; the LLM only
  writes short narrative summaries. Less to hallucinate, easier to test.

- **`modify` ≠ `reject`.** `reject` re-runs research (the premise changed);
  `modify` re-runs only the planner (tweak the draft). Collapsing them would
  waste tokens and lose intent.

- **Services behind thin adapters + a stub switch.** Nodes never import
  `langchain`/`tavily` directly, so providers are swappable and the full
  graph runs key-free for tests via `USE_STUBS=true`.

- **`pyproject.toml`, Pydantic v2, full async.** Endpoints `await
  graph.ainvoke(...)`; sync tool I/O is off-loaded with `anyio.to_thread`.

---

## Deploying to Vercel

The repository is configured for zero-config Vercel deployment. It handles the ephemeral filesystem, static frontend CDN distribution, and serverless lifespan intricacies automatically.

1. Install the [Vercel CLI](https://vercel.com/cli) or import the repository in the Vercel Dashboard.
2. Ensure you add your `GOOGLE_API_KEY` and `TAVILY_API_KEY` in the Vercel project's Environment Variables.
3. Deploy!

```bash
vercel deploy --prod
```

---

## What I'd Do Differently in Production

- Swap `SqliteSaver` → **`PostgresSaver`** for multi-instance / HA deployments.
- Add **LangSmith tracing** (one env var) for debugging agent failures.
- **Idempotency keys** on `POST /plan` to safely absorb client retries.
- Put LLM calls behind a **circuit breaker** + per-`plan_id` budget caps.
- **Stream** the draft via SSE (or push via webhook) instead of polling
  `GET /plan/{id}`.
- Richer scheduling: respect opening hours and travel-time clustering in
  `build_day_schedule` (today it's an explainable round-robin heuristic).

---

## API Reference

Full interactive docs auto-generate at **`/docs`** (Swagger) and **`/redoc`**.

| Method | Path | Body | Returns |
|--------|------|------|---------|
| `POST` | `/plan` | `PlanRequest` | `201 {plan_id, status}` |
| `GET`  | `/plan/{id}` | — | `200` current state + draft |
| `POST` | `/plan/{id}/review` | `{action, feedback}` | `200` new state · `409` if not awaiting · `404` |
| `GET`  | `/plan/{id}/final` | — | `200` final plan · `409` if not completed · `404` |
| `GET`  | `/plan/{id}/pdf` | — | `200` Vector PDF itinerary download (via MCP) |
| `GET`  | `/plan/{id}/calendar.ics` | — | `200` RFC 5545 iCalendar feed (via MCP) |
| `GET`  | `/mcp/tools` | — | `200` Model Context Protocol tool discovery |
| `POST` | `/auth/signup` | `UserSignupRequest` | `201 {access_token, token_type, user}` |
| `POST` | `/auth/login` | `UserLoginRequest` | `200 {access_token, token_type, user}` |
| `GET`  | `/auth/me` | — | `200 User profile (Bearer token)` |
| `GET`  | `/community/trips` | `destination, tag, sort` | `200 Public community trips feed` |
| `POST` | `/community/trips` | `CommunityTripCreateRequest` | `201 Published community trip` |
| `GET`  | `/community/trips/{id}`| — | `200 Trip detail + structured reviews` |
| `POST` | `/community/trips/{id}/reviews`| `CommunityReviewCreateRequest` | `201 Submitted review with pro tips` |
| `POST` | `/community/trips/{id}/like`| — | `200 Toggle upvote / like` |
| `POST` | `/community/trips/{id}/remix-ai`| `CommunityRemixRequest` | `200 AI synthesized community remix` |
| `GET`  | `/health` | — | `200 {status:"ok"}` |

**`PlanRequest`:** `destination` (str), `start_date`/`end_date` (ISO date),
`budget_usd` (>0), `travelers` (1–20), `interests` (list[str]).
**`action`:** `approve` \| `reject` \| `modify` — `feedback` required for the
last two.

**Status lifecycle:** `researching → planning → awaiting_review → completed`
(loops back to `researching`/`planning` on reject/modify).

---

## Project Structure

```
travel-planner/
├── app/
│   ├── main.py            # FastAPI app + endpoints (async, lifespan)
│   ├── graph.py           # build_graph() + AsyncSqliteSaver checkpointer
│   ├── nodes.py           # 5 nodes incl. interrupt() HITL gate + MCP calls
│   ├── tools.py           # web_search, get_weather, allocate_budget, build_day_schedule
│   ├── state.py           # TripState TypedDict (the graph's contract)
│   ├── schemas.py         # Pydantic v2 request/response models
│   ├── config.py          # pydantic-settings (.env driven)
│   ├── mcp/
│   │   ├── server.py      # Standalone FastMCP Travel Concierge server
│   │   └── pdf_generator.py # Publication-grade vector PDF dossier generator
│   └── services/
│       ├── mcp_client.py  # Async MCP ClientManager (stdio IPC + stubs)
│       ├── llm.py         # Gemini client + stub
│       └── search.py      # Tavily client + stub
├── tests/test_e2e.py      # one e2e HITL round-trip (key-free, USE_STUBS)
├── tests/test_mcp_client.py # MCP protocol & export tool tests
├── docs/architecture.png  # (optional) rendered diagram
├── .env.example
├── pyproject.toml
└── README.md
```

---

## Data Model

`TripState` (in `app/state.py`) is the single source of truth that flows
through the graph and is checkpointed to SQLite:

| Field | Set by | Notes |
|-------|--------|-------|
| `plan_id`, `status` | orchestrator/API | status drives the lifecycle |
| `preferences` | API | validated `TripPreferences` |
| `research` | research_agent | `{highlights, tips, weather, sources}` |
| `draft_itinerary` | planner_agent | `{summary, budget, days, tips, …}` |
| `review` | API resume | `{action, feedback}` |
| `revision_notes`, `revision_count` | human_gate | accumulated feedback history |
| `final_plan` | finalize | immutable approved output |

Storage: a single SQLite file (`CHECKPOINT_DB`, default `checkpoints.sqlite`)
managed by LangGraph's `AsyncSqliteSaver`. Created automatically on first run.

---

## Known Limitations

- `build_day_schedule` uses a simple round-robin heuristic — it doesn't yet
  model opening hours or inter-activity travel time.
- Open-Meteo only forecasts ~16 days out; beyond that the planner degrades
  gracefully to a "weather unavailable" note rather than failing.
- Polling (`GET /plan/{id}`) rather than streaming — fine at this scale, but
  SSE/webhooks would be the production move.
- Single-process SQLite checkpointer; horizontal scaling needs Postgres.

---

## Configuration

| Env var | Default | Purpose |
|---------|---------|---------|
| `GOOGLE_API_KEY` | — | Gemini key (required unless `USE_STUBS=true`) |
| `GEMINI_MODEL` | `gemini-2.0-flash` | Model id |
| `TAVILY_API_KEY` | — | Tavily search key (required unless stubbed) |
| `CHECKPOINT_DB` | `checkpoints.sqlite` | SQLite checkpoint file (`:memory:` for tests) |
| `USE_STUBS` | `false` | Deterministic, key-free LLM/search for offline runs & tests |
