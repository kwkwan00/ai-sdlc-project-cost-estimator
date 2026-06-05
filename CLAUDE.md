# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

Multi-agent SDLC cost estimator. Six specialized LangGraph "twin" agents (Discovery, UX/Design, Development, Code Review, Deployment/DevOps, QA/Testing) each apply a formal estimation algorithm (UCP, SCP, COCOMO II, Fagan, CMP, TPA) and feed a two-pass orchestrator with a human-in-the-loop clarifying-questions step in the middle.

Canonical design spec: `ai-sdlc-project-cost-estimator-planning-outline.md` (3,462 lines). When in doubt about scope, algorithm details, or worked numbers, that document is authoritative. Phase 1 / MVP scope is summarized in `README.md`.

Monorepo: `backend/` (Python 3.12 + FastAPI + LangGraph) and `frontend/` (Next.js 15 App Router) as siblings, plus `docker-compose.yml` for Neo4j + Postgres + Qdrant.

## Common commands

All targets are driven from the root `Makefile`. Prefer `make` over raw commands so the working directory and flags stay consistent.

```bash
# Infra (Neo4j on :7474/:7687, Postgres on :5432, Qdrant on :6333/:6334)
make up                  # docker compose up -d
make down                # docker compose down
make ps / make logs      # status / tailed logs
make clean               # down -v (removes named volumes; bind mounts under ./data/{neo4j,postgres} persist)

# Dependencies (one-time)
make install-be          # cd backend && uv sync
make install-fe          # cd frontend && npm install

# Dev servers
make be                  # uvicorn main:app --reload --host 0.0.0.0 --port 8000
make fe                  # next dev on :3000
make smoke               # uv run python -m orchestrator.smoke  (one-shot Pass 1 cycle)
```

Backend tests (pytest, asyncio auto-mode):

```bash
cd backend && uv run pytest                              # full suite (~116 tests)
cd backend && uv run pytest tests/test_discovery_twin.py # one file
cd backend && uv run pytest tests/test_postgres_layer.py # Postgres history + calibration
cd backend && uv run pytest tests/test_api.py::test_health -q
cd backend && uv run ruff check .                        # lint
cd backend && uv run mypy .                              # type-check
```

Alembic migrations (Postgres history + calibration schema):

```bash
cd backend && uv run alembic upgrade head           # apply
cd backend && uv run alembic revision --autogenerate -m "describe"
cd backend && uv run alembic downgrade -1
```

`env.py` resolves the DSN via `config.get_settings().resolved_postgres_dsn`, so the same commands work from the host shell or inside the dockerized backend. The FastAPI lifespan also runs `upgrade head` automatically when `POSTGRES_MIGRATE_ON_START=true` (default).

Frontend tests (vitest, node env) + lint:

```bash
cd frontend && npm test                  # vitest run (one-shot)
cd frontend && npm run test:watch        # vitest watch
cd frontend && npm run lint              # next lint (eslint-config-next)
cd frontend && npm run type-check        # tsc --noEmit
cd frontend && npm run build             # produces standalone output for Docker
```

`vitest.config.ts` only globs `lib/**/*.test.ts`, `components/**/*.test.ts`, and `instrumentation.test.ts`. Add new test paths to the `include` array — tests outside those globs won't run.

Dockerized full stack (use after `cp .env.example .env` and filling in `ANTHROPIC_API_KEY`):

```bash
docker compose up -d --build                          # full stack
docker compose up -d --build estimator-backend estimator-frontend   # rebuild apps only
docker compose logs estimator-backend | grep "Backend ready"
docker compose logs estimator-frontend | grep "Frontend ready"
```

## Architecture

### Orchestrator graph (backend/orchestrator/graph.py)

LangGraph `StateGraph(EstimationState)` with the topology:

```
START → parse_input
      → [discovery_p1, ux_p1, dev_p1, code_review_p1, deployment_p1, qa_p1]   fan-out
      → merge_pass1
      → await_user_answers          (LangGraph interrupt — Stage 4)
      → [discovery_p2, ux_p2, dev_p2, code_review_p2, deployment_p2, qa_p2]   fan-out
      → merge_pass2 → consistency_check → commercial_processing → synthesize_estimate
      → END
```

`pass1_estimates` and `pass2_estimates` use `Annotated[list, operator.add]` reducers so the six parallel twins can append without conflict. Other state fields are single-writer.

The graph is paused at `await_user_answers` via LangGraph's `interrupt()`. The HTTP layer (`main.py`) resumes it with `Command(resume={"answers": ...})` after the frontend POSTs to `/estimates/{id}/answers`.

### Twin node pattern (backend/orchestrator/nodes/_twin_base.py)

Every twin follows the same shape — only the prompt file and post-processing math differ:

1. `load_prompt("<twin>")` reads `orchestrator/prompts/<twin>.md`.
2. `build_twin_user_prompt(state, pass_num)` renders parsed context + Stage 2/3 + (on pass 2) the user's clarifying answers as a JSON block.
3. `orchestrator.llm.call_structured(...)` calls Claude with a Pydantic response model exposed as a forced-choice tool (tool_use is forced; the JSON tool input is validated back into the model). This is more reliable than JSON-mode prompting — keep using it.
4. `orchestrator.role_attribution.attribute_roles(total_hours, role_pct, phase)` is the single shared role-split helper. All six twins call it for both AI-assisted and manual-only hour buckets; never inline this logic in a twin.
5. Return `{"pass1_estimates": [PhaseEstimate(...)]}` or `{"pass2_estimates": [...]}` keyed on the pass number.

When adding a seventh twin or modifying an existing one, replicate this five-step pattern. The plumbing lives in `_twin_base.py` and `llm.py`; do not re-implement it per twin.

### Two-output cost model

Every `PhaseEstimate` carries **both** `ai_assisted_hours` and `manual_only_hours` as `HourRange(optimistic, most_likely, pessimistic)`, plus matching `*_role_hours: list[RoleHours]` for each. Downstream `synthesize_estimate` aggregates both scenarios into a `DualScenarioEstimate`. The frontend's `<DualScenarioToggle>` flips the view between them. When changing any twin, both scenarios must be produced — never collapse to a single number.

### User-defined role roster (Stage 2)

The team is **not** four fixed roles — it's a user-defined `RoleRoster` of `CustomRole` entries, each carrying `role_id`, `name`, `category` (`product` / `engineering` / `ui_ux` / `qa` / `devops` / `data` / `other`), `seniority` (`senior` / `mid` / `junior` / `other`), `rate_per_hour`, and `percentage`. The roster lives in `Stage2Context.roster`. Stage 3 only carries the AI maturity sliders.

`orchestrator/role_attribution.py::attribute_roles(total_hours, roster, phase)` returns `list[RoleHours]` (one entry per roster role, including zeroed-out entries) with phase-specific overrides keyed on the **tags**, not on fixed role IDs:

- DISCOVERY caps junior-seniority roles at 25%, pushing excess to a same-category senior (fallback: any senior).
- UX_DESIGN ensures `product` + `ui_ux` ≥ 40%, preferring `ui_ux` for shortfall.
- CODE_REVIEW caps juniors at 15%.
- DEPLOYMENT ensures `engineering` + `devops` + `data` ≥ 75%, preferring `devops` for shortfall.
- DEVELOPMENT, QA_TESTING honor user input as-is.

`commercial_processing` looks up rates from the roster by `role_id`. `synthesize_estimate` aggregates per-role hours across phases and emits `headcount_by_role: list[RoleHeadcount]`. The frontend renders headcount using the user's own role names + category/seniority labels.

When adding a seventh twin: import `RoleRoster` (not `RolePercentages` — that's gone), call `attribute_roles(hours, roster, phase)`, and populate `ai_assisted_role_hours` / `manual_only_role_hours` on the `PhaseEstimate`. The roster comes from `state.get("stage2").roster` with `RoleRoster.default()` as the fallback when Stage 2 is absent.

### Persistence (backend/db/)

Three stores, distinct jobs. All three degrade silently when unreachable — **the backend must keep serving requests when any persistence layer is down**, only that layer's writes/reads are lost. Do not raise from the persistence path.

- **LangGraph checkpointer** — `make_checkpointer()` returns `InMemorySaver` in MVP. LangGraph state lives in-process — surviving server restarts is **not** an MVP guarantee. There is a TODO to swap in a real Neo4j-backed `BaseCheckpointSaver` in Phase 3; the call site is already abstracted, so swap there without touching graph/nodes.
- **Neo4j envelope snapshots** — `save_estimate_envelope(...)` writes a denormalized snapshot (one `Estimate` node + N `Phase` nodes) via Cypher MERGE. Silently no-ops when `NEO4J_PASSWORD` is unset or the driver fails to connect.
- **Postgres history + calibration** — three tables (`estimate_history`, `phase_history`, `calibration_aggregates`). `save_estimate_history(envelope, stage2, stage3)` upserts the envelope and replaces its phase rows on every status transition (Pass 2 wholesale supersedes Pass 1 in-place, keyed on `estimate_id`). When status hits `completed`, `refresh_calibration_for_phase(phase)` is called for every phase, recomputing rolling per-(phase, industry, project_type, maturity) aggregates from `phase_history`. Repositories use `session_scope()` from `db/postgres_adapter.py` which yields **None when Postgres is disabled** — every repo function must handle that and return the empty case. Tests install an aiosqlite engine onto `postgres_adapter._engine / _sessionmaker` via `_reset_for_tests()`; the ORM uses portable column types so SQLite ↔ Postgres schemas match.
- **Twin calibration injection** — `parse_input` calls `get_calibration_for_all_phases(...)` and writes the flattened result into `state["calibration_examples"]` (already declared in `EstimationState`). `_twin_base.build_twin_user_prompt(state, pass_num=..., phase_value="discovery")` filters that list by phase and renders it into the prompt under a `"calibration"` key inside the JSON context block. The `phase_value` kwarg is required at every twin call site — keep it threaded if you add a seventh twin.
- **Qdrant** — `db/qdrant_adapter.py` is scaffolded but **not** populated in MVP (vector-similarity calibration is Phase 3; the SQL aggregates above are the MVP version).

### Observability (backend/observability/langfuse_wrapper.py)

`@traced(name=..., as_type=...)` is a drop-in for langfuse's `@observe`. When `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` are empty the wrapper installs a **no-op decorator that preserves `inspect.iscoroutinefunction` status** — this matters because LangGraph inspects node functions to decide sync vs async dispatch. If you write a new tracing wrapper, keep the async-preserving branch or LangGraph will start raising "Expected dict, got coroutine".

Langfuse is **self-hosted via docker-compose**: `langfuse-web` (UI on host port `3100` → container `3000`), `langfuse-worker`, ClickHouse (events store, host port `8123`), Redis (queues + cache, internal only), and MinIO (S3-compatible blob storage, host ports `9020` / `9021`). The metadata DB lives in the shared Postgres instance under a separate `langfuse` database created by `data/postgres-init/01-create-langfuse-db.sh` on first volume init. The estimator backend points at `http://langfuse-web:3000` inside the compose network; on the host (`make be`) it uses `http://localhost:3100`. Public/secret API keys are NOT auto-generated — sign in to the Langfuse UI, create a project, copy the keys back to `.env`.

### HTTP layer (backend/main.py)

- `POST /estimates` creates an envelope, kicks off `_run_pass1` as a background task, returns the envelope immediately.
- `GET /estimates/{id}/stream` is the SSE event stream (`status`, `questions`, `final`, `error`). Frontend uses this to render Pass 1/Pass 2 progress.
- `POST /estimates/{id}/answers` is only valid when status is `AWAITING_ANSWERS`; it resumes the graph and runs Pass 2.
- `_envelopes` + `_event_streams` are in-memory dicts. Restart loses both. Pair this with the `InMemorySaver` checkpointer above when reasoning about durability.

The FastAPI lifespan logs `✓ Backend ready at http://...` after the graph compiles — operators (and tests) grep for this. Don't remove or restructure the message format without updating `tests/test_lifespan_ready_log.py`.

### Frontend wizard (frontend/app/estimate/)

Routes follow the five planning-outline stages:

- `app/estimate/new/` — Stage 1 (raw text).
- `app/estimate/draft/{create,context,maturity}/` — pre-submission Stage 2/3 wizard backed by `lib/wizard-store.ts` (client-side state, no estimate id yet). **The team roster lives in Stage 2** (`<RoleRosterEditor>`), not Stage 3.
- `app/estimate/[id]/questions/` — Stage 4 (clarifying questions, posts answers back to resume the graph).
- `app/estimate/[id]/review/` — Stage 5 (dual-scenario review + role-attributed cost table).

Pages that read `useSearchParams()` must be wrapped in `<Suspense>` — Next.js 15 build will fail otherwise. `/estimate/new` and `/estimate/draft/create` already do this; mirror it for any new query-param page.

`lib/api-client.ts` is the only place that calls the backend. `NEXT_PUBLIC_API_URL` is inlined at build time, so the Docker build needs the URL passed as a `--build-arg` (already wired in `docker-compose.yml`). The URL must be reachable from the user's **browser**, not the container — i.e. `http://localhost:8000`, not `http://estimator-backend:8000`.

`next.config.mjs` sets `output: "standalone"`. The Dockerfile copies `.next/standalone/`, `.next/static/`, and `public/` to produce a minimal runtime image. `instrumentation.ts` runs once on server startup and logs `✓ Frontend ready at http://...`; the matching test is `instrumentation.test.ts`.

### Tailwind font scaling

`frontend/app/globals.css` sets `html { font-size: 14px; }` (~12.5% smaller than the 16px default). All Tailwind rem-based utilities scale uniformly. If a user asks for a global UI size change, adjust the root font-size here rather than rewriting class names.

## Project-specific gotchas

- **Neo4j image is pinned** to `neo4j:5.20-community`. Newer 5.x community images regress on arm64 with `JettyWebServer.loadStaticContent: Path is null`. The backend only uses Bolt (:7687) — the Browser at :7474 is non-essential but the healthcheck still hits HTTP. APOC is intentionally not installed.
- **All stateful services bind-mount under `./data/`** (Neo4j, Postgres, ClickHouse, Redis, MinIO) rather than using named volumes — sidesteps the Docker VM virtual-disk limit. Host has space; the VM does not. `make clean` does not wipe these directories.
- **Postgres init script** at `data/postgres-init/01-create-langfuse-db.sh` runs ONLY on a fresh volume. Existing volumes need the `langfuse` DB created manually: `docker exec sdlc-postgres psql -U estimator -c "CREATE DATABASE langfuse;"`.
- **Langfuse port collisions to know about**: container `3000` (langfuse-web) → host `3100` (Next.js owns host `3000`); container MinIO `9000` → host `9020` (ClickHouse native protocol claims `9000` but stays internal-only); ClickHouse HTTP `8123` is host-exposed for ad-hoc queries.
- **Postgres DSN precedence** — `POSTGRES_DSN` (full async DSN) wins over the discrete `POSTGRES_{USER,PASSWORD,DB,HOST,PORT}` vars. If both `POSTGRES_PASSWORD` and `POSTGRES_DSN` are empty, `settings.postgres_enabled` is False and the entire layer no-ops — useful for tests and partial-stack runs.
- **Alembic env.py is async** — it uses `async_engine_from_config` + `connection.run_sync(do_migrations)`. If you add a sync helper that imports `env.py` directly, wrap the call in `asyncio.run(...)`. The lifespan already does this via `asyncio.to_thread(upgrade_to_head)`.
- **`parse_input` has an LLM-free fallback path** (`backend/orchestrator/nodes/parse_input.py`) so the graph still runs when `ANTHROPIC_API_KEY` is missing. Tests rely on this. Don't make parse_input hard-fail without an env check.
- **Anthropic structured output**: every LLM call goes through `orchestrator/llm.py::call_structured(..., response_model=SomeModel)`. The tool is forced via `tool_choice={"type": "tool", "name": tool_name}` so Claude must call it. Free-text JSON parsing is **not** an accepted alternative — past attempts were flaky.
- **Plan to stay independent across the twins**: Phase 1 MVP explicitly excludes A2A peer-to-peer cross-phase signaling between twins. They share only what they read from the state. Don't introduce per-twin imports of other twins' output structures.

## Global rule: always fetch latest docs before LLM/agentic code

When writing or modifying code that touches LangGraph, Anthropic SDK, LangChain, MCP, or other LLM/agent frameworks, fetch the latest docs via `context7` MCP first (`resolve-library-id` → `query-docs`), and only fall back to `docs-mcp-server` if Context7 is unavailable. Training data is stale relative to these libraries' APIs. This rule comes from the global `~/.claude/CLAUDE.md`.
