# AI SDLC Project Cost Estimator

Multi-agent system that estimates **effort (hours), cost (USD), duration (weeks), and headcount** for AI-heavy software projects across the six SDLC phases — and produces a **dual-scenario** breakdown showing what each phase costs **with AI assistance** vs. **with manual delivery only**, so the gap is the realized AI ROI.

Six specialized LangGraph "twin" agents — each grounded in a formal estimation algorithm — collaborate through a two-pass orchestrator with a human-in-the-loop clarifying-questions step in the middle.

> The full design spec (3,462 lines, including worked examples) is in `ai-sdlc-project-cost-estimator-planning-outline.md`. This README summarizes what is implemented in the MVP.

**Persistence at a glance** — three stores, distinct jobs:

- **LangGraph in-memory checkpointer** — Pass 1 ↔ Pass 2 interrupt state (in-process only).
- **Neo4j** — graph-shaped envelope snapshots: one `Estimate` node per run, `INCLUDES_PHASE` edges to phase nodes. Useful for graph queries over the estimate corpus.
- **Postgres** — structured history (`estimate_history`, `phase_history`) and rolling per-(phase, industry, project_type, maturity) **calibration aggregates** the twins query during Pass 1 to anchor their LLM-derived numbers.

All three are best-effort: the backend keeps running when any of them is unavailable.

---

## Table of contents

- [What it does](#what-it-does)
- [The six twins](#the-six-twins)
- [Orchestrator architecture](#orchestrator-architecture)
- [Tech stack](#tech-stack)
- [Repository layout](#repository-layout)
- [Quickstart — local development](#quickstart--local-development)
- [Quickstart — full Docker stack](#quickstart--full-docker-stack)
- [Configuration](#configuration)
- [HTTP API](#http-api)
- [Frontend wizard (Stages 1–5)](#frontend-wizard-stages-15)
- [Estimation algorithms in one breath](#estimation-algorithms-in-one-breath)
- [Role attribution and rates](#role-attribution-and-rates)
- [Persistence and observability](#persistence-and-observability)
- [Testing](#testing)
- [MVP scope and what's deferred](#mvp-scope-and-whats-deferred)
- [Troubleshooting](#troubleshooting)
- [License](#license)

---

## What it does

Given a free-text project description (Stage 1) and optional context / maturity inputs (Stages 2–3), the system:

1. **Pass 1** — runs all six twins in parallel; each produces a phase estimate **plus** the gaps it needs to firm up.
2. **Interrupt** — the orchestrator dedupes overlapping gaps into 5–10 clarifying questions and pauses the graph (Stage 4).
3. **Pass 2** — once the user answers (or skips with defaults), all six twins re-run with the new context.
4. **Synthesize** — aggregates per-phase outputs into a `DualScenarioEstimate`: total hours, $ cost, duration band, weekly burn, headcount by role, and **AI hours/cost saved** (manual − AI).
5. **Review** (Stage 5) — frontend renders per-phase bars, a toggle between AI-assisted and manual-only views, and a role-attributed cost table.

Every `PhaseEstimate` carries **both scenarios** as `HourRange(optimistic, most_likely, pessimistic)` plus matching role splits — the two numbers travel together end-to-end so you can never accidentally collapse to a single answer.

---

## The six twins

| Phase | Twin | Algorithm | What it scales on |
|---|---|---|---|
| Discovery | Discovery Analyst | **UCP** — Use Case Points | use case / actor counts, TFactor, EFactor |
| UX / Design | UX Design Strategist | **SCP** — Screen Complexity Points | screen count × complexity weights, design system maturity |
| Development | Development Architect | **COCOMO II** | SLOC estimate × effort multipliers × scale factors |
| Code Review | Code Review Sentinel | **Fagan inspection** | KLOC, inspection rate, rework factor |
| Deployment / DevOps | Deployment & DevOps | **CMP** — Configuration Management Points | environments, integrations, infra complexity |
| QA / Testing | QA & Testing Strategist | **TPA** + 3-plan recommendation | Function Points × dynamic/static quality chars, supplementary hours |

Each twin:

1. Loads its system prompt from `backend/orchestrator/prompts/<twin>.md`.
2. Renders the parsed context + Stage 2/3 + (on Pass 2) the user's answers as a JSON block.
3. Calls Claude via `orchestrator/llm.py::call_structured(...)`, which exposes a Pydantic response model as a single tool and **forces** `tool_choice` to it. The tool input is validated back into the model — far more reliable than free-text JSON.
4. Runs the deterministic algorithm in Python over the LLM-extracted inputs.
5. Applies an **AI maturity cap** (per-phase, drawn from planning outline §3.x worked examples) to derive the AI-assisted hours from the manual baseline.
6. Wraps the mid number in a PERT range and splits hours across roles via the shared `role_attribution.attribute_roles(...)`.

The shared shape lives in `backend/orchestrator/nodes/_twin_base.py`. When adding or modifying a twin, the differences should live in the prompt file + post-processing math, **not** in the plumbing.

### QA's three plans

The QA twin uniquely recommends one of three test strategies and stashes the other two in `notes`:

- **Plan A — automated harness** (best for AI-heavy, low regulatory)
- **Plan B — dedicated QA team** (best for high regulatory, no AI features)
- **Plan C — hybrid** (best when both AI and regulatory pressure are high)

Selection is rule-based (`auto_select_plan(has_ai, has_reg)`) but the twin may override; both are logged.

---

## Orchestrator architecture

LangGraph `StateGraph(EstimationState)` (see `backend/orchestrator/graph.py`):

```
START → parse_input
      → [discovery_p1, ux_p1, dev_p1, code_review_p1, deployment_p1, qa_p1]   (fan-out)
      → merge_pass1
      → await_user_answers          (LangGraph interrupt — Stage 4)
      → [discovery_p2, ux_p2, dev_p2, code_review_p2, deployment_p2, qa_p2]   (fan-out)
      → merge_pass2 → consistency_check → commercial_processing → synthesize_estimate
      → END
```

Key mechanics:

- `pass1_estimates` and `pass2_estimates` use `Annotated[list, operator.add]` reducers — the six parallel twins can append independently without write conflicts.
- The graph pauses at `await_user_answers` via LangGraph's `interrupt()`. The HTTP layer resumes it with `Command(resume={"answers": ...})` after Stage 4.
- `parse_input` calls Claude to convert raw text into a structured `parsed_context`. It has an **LLM-free fallback** (`_fallback_context`) so the graph still runs without an `ANTHROPIC_API_KEY` (smoke tests rely on this).
- `commercial_processing` applies the role-rate table from `Stage2Context.role_rates` to produce per-scenario dollar totals.
- `synthesize_estimate` aggregates PERT ranges across phases, computes `ai_hours_saved_pert = manual.pert_mean − ai.pert_mean`, derives headcount + duration band against the target timeline (or a default 5-person team if none was given), and rolls up a weekly burn rate.

Phase 1 explicitly excludes A2A peer-to-peer cross-phase signaling — twins share **only** what they read from state.

---

## Tech stack

**Backend**

- Python 3.12, `uv` for dependency management
- FastAPI + `uvicorn[standard]`
- LangGraph (`langgraph>=0.6`) + Anthropic SDK (`anthropic>=0.40`) via forced tool-use
- Pydantic v2 (`extra="forbid"` on every model)
- Neo4j driver (`neo4j>=5.25`) for graph-snapshot persistence
- SQLAlchemy 2.0 async + asyncpg + Alembic for Postgres history + calibration
- Qdrant client (`qdrant-client>=1.12`) — scaffolded, not populated in MVP
- Langfuse SDK (`langfuse>=2.50`) — optional; transparent no-op when env keys are absent
- SSE via `sse-starlette`

**Frontend**

- Next.js 15 App Router (`output: "standalone"` for Docker)
- React 19
- TailwindCSS 3, `react-hook-form` + Zod (`@hookform/resolvers`)
- `@tanstack/react-query` for backend calls
- `recharts` for per-phase bars
- Vitest (node env) for unit tests

**Infra**

- Docker Compose: Neo4j 5.20-community + Postgres 16-alpine + Qdrant + self-hosted Langfuse stack (Langfuse web + worker, ClickHouse, Redis, MinIO) + (optional) the dockerized backend & frontend
- Bind-mount under `./data/{neo4j,postgres,clickhouse,redis,minio}/` (sidesteps the Docker VM disk limit)

---

## Repository layout

```
.
├── ai-sdlc-project-cost-estimator-planning-outline.md   # canonical design spec
├── docker-compose.yml         # neo4j + postgres + qdrant + (optional) estimator-backend / estimator-frontend
├── Makefile                   # up / down / install-be / install-fe / be / fe / smoke / clean
├── .env.example               # required + optional env vars
├── data/neo4j/                # bind-mounted neo4j data + logs
├── data/postgres/             # bind-mounted postgres data
│
├── backend/
│   ├── main.py                # FastAPI app: /estimates, /stream, /answers, /health
│   ├── config.py              # pydantic-settings, reads ../.env or .env
│   ├── pyproject.toml         # uv-managed deps
│   ├── Dockerfile             # python:3.12-slim + uv, non-root, HEALTHCHECK /health
│   │
│   ├── orchestrator/
│   │   ├── graph.py           # StateGraph topology
│   │   ├── llm.py             # call_structured(...) — forced tool-use → Pydantic
│   │   ├── role_attribution.py# shared role-split with phase-specific overrides
│   │   ├── smoke.py           # CLI: `uv run python -m orchestrator.smoke`
│   │   ├── nodes/
│   │   │   ├── _twin_base.py  # shared twin shape (prompt loader, user-prompt builder, stub)
│   │   │   ├── parse_input.py # raw text → parsed_context (with LLM-free fallback)
│   │   │   ├── discovery_analyst.py        # UCP
│   │   │   ├── ux_design_strategist.py     # SCP
│   │   │   ├── development_architect.py    # COCOMO II
│   │   │   ├── code_review_sentinel.py     # Fagan
│   │   │   ├── deployment_devops.py        # CMP
│   │   │   ├── qa_testing_strategist.py    # TPA + 3-plan recommendation
│   │   │   ├── merge_pass1.py / merge_pass2.py
│   │   │   ├── await_user_answers.py       # interrupt()
│   │   │   ├── consistency_check.py
│   │   │   ├── commercial_processing.py
│   │   │   └── synthesize_estimate.py      # DualScenarioEstimate
│   │   └── prompts/<twin>.md  # system prompts grounded in the algorithm
│   │
│   ├── models/
│   │   ├── estimation_state.py  # LangGraph EstimationState TypedDict
│   │   ├── twin_outputs.py      # Phase, PhaseEstimate, HourRange, RoleAttribution, DualScenarioEstimate, ...
│   │   └── project_schema.py    # CreateEstimateRequest, EstimateEnvelope, Stage2Context, Stage3Maturity
│   │
│   ├── db/
│   │   ├── neo4j_adapter.py   # driver + make_checkpointer (InMemorySaver in MVP) + save_estimate_envelope
│   │   ├── postgres_adapter.py# async engine + session_scope() — no-ops when DSN unset
│   │   ├── orm_models.py      # SQLAlchemy models: EstimateHistory, PhaseHistory, CalibrationAggregate
│   │   ├── repositories.py    # save_estimate_history, refresh_calibration_for_phase, get_calibration*
│   │   ├── migrate.py         # programmatic `alembic upgrade head` for the FastAPI lifespan
│   │   └── qdrant_adapter.py  # client init (no ingestion in MVP)
│   │
│   ├── alembic/               # async migrations (env.py reads settings.resolved_postgres_dsn)
│   │   ├── env.py
│   │   ├── script.py.mako
│   │   └── versions/0001_initial_history_and_calibration.py
│   ├── alembic.ini
│   │
│   ├── observability/
│   │   └── langfuse_wrapper.py# @traced(...) decorator — no-op when env keys absent, async-preserving
│   │
│   └── tests/                 # pytest, asyncio auto-mode (~100 tests)
│
└── frontend/
    ├── app/
    │   ├── layout.tsx / page.tsx / providers.tsx
    │   ├── globals.css        # html { font-size: 14px } — global UI scale
    │   └── estimate/
    │       ├── new/                          # Stage 1
    │       ├── draft/{create,context,maturity}/  # Stages 2-3 wizard (client-side, pre-submit)
    │       └── [id]/{questions,review}/      # Stages 4-5 (server-driven)
    ├── components/            # PhaseBar, DualScenarioToggle, MaturitySlider, RolePercentageSliders, StageProgress
    ├── lib/                   # schemas (Zod), api-client (fetch + SSE), wizard-store, types, format
    ├── instrumentation.ts     # Next.js startup hook — logs `✓ Frontend ready ...`
    ├── next.config.mjs        # output: "standalone"
    ├── vitest.config.ts       # globs: lib/**, components/**, instrumentation.test.ts
    └── Dockerfile             # multi-stage node:22-alpine, copies .next/standalone
```

---

## Quickstart — local development

Prereqs: `uv` (https://docs.astral.sh/uv/), Node 22+, Docker, an Anthropic API key.

```bash
# 1. Configure
cp .env.example .env
# edit .env: set ANTHROPIC_API_KEY (required), NEO4J_PASSWORD, POSTGRES_PASSWORD

# 2. Start datastores (Neo4j :7474/:7687, Postgres :5432, Qdrant :6333/:6334)
make up

# 3. One-time install
make install-be       # cd backend && uv sync
make install-fe       # cd frontend && npm install

# 4. In two shells:
make be               # FastAPI with --reload on :8000
make fe               # Next.js dev on :3000

# 5. Open http://localhost:3000 — Stage 1
```

CLI smoke (one full Pass-1 → resume-with-defaults → synthesis cycle, no browser):

```bash
make smoke                       # uses fixture healthcare-portal description
# Or skip the LLM call for parse_input:
cd backend && uv run python -m orchestrator.smoke --no-llm
```

---

## Quickstart — full Docker stack

The backend and frontend can run as compose services alongside Neo4j + Qdrant.

```bash
cp .env.example .env             # fill in ANTHROPIC_API_KEY + NEO4J_PASSWORD + POSTGRES_PASSWORD
# Rotate the Langfuse secrets before anything non-throwaway:
#   openssl rand -hex 32   # -> paste into LANGFUSE_ENCRYPTION_KEY
#   openssl rand -base64 32   # -> paste into LANGFUSE_NEXTAUTH_SECRET / LANGFUSE_SALT
docker compose up -d --build     # builds estimator apps + brings up neo4j / postgres / qdrant / langfuse stack
docker compose ps                # all services should be healthy

# First-run Langfuse setup (only needed once):
#   1. open http://localhost:3100 → sign up → create org → create project
#   2. project settings → API keys → "Create new API key"
#   3. paste pk-lf-... / sk-lf-... into .env's LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY
#   4. docker compose restart estimator-backend
# (Alternative: set the LANGFUSE_INIT_* env vars in .env to seed an org/user
# on first boot — see .env.example for the full list.)
docker compose logs estimator-backend  | grep "Backend ready"
docker compose logs estimator-frontend | grep "Frontend ready"
```

Notes:

- The frontend image inlines `NEXT_PUBLIC_API_URL` at **build** time (passed as a `--build-arg`). It must be a URL reachable from the user's **browser** — i.e. `http://localhost:8000`, not `http://estimator-backend:8000`. The default in `docker-compose.yml` is correct for desktop use.
- Health probes:
  - Backend: `HEALTHCHECK` hits `GET /health`.
  - Neo4j: `wget --spider http://localhost:7474`.
  - Qdrant: TCP probe on `:6333` via bash `/dev/tcp/...` (the image ships without curl/wget).
- Rebuild apps only: `docker compose up -d --build estimator-backend estimator-frontend`.
- `make clean` removes named volumes (`qdrant_data`). Neo4j data lives under `./data/neo4j/` and is **not** wiped by `make clean` — delete the directory manually if you want a fresh DB.

---

## Configuration

All settings are read by `backend/config.py` (pydantic-settings) from `.env`. The full set:

| Var | Required | Default | Purpose |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | yes (for real estimates) | — | Claude calls. Twins fall back to stub estimates if missing; `parse_input` uses a deterministic fallback. |
| `ANTHROPIC_MODEL` | no | `claude-opus-4-5-20250929` | Model id for all twins. Per-twin overrides are out of MVP scope. |
| `NEO4J_URI` | no | `bolt://localhost:7687` | Bolt URI. |
| `NEO4J_USER` | no | `neo4j` | |
| `NEO4J_PASSWORD` | no (recommended) | `""` | When empty, persistence is silently disabled — the backend still runs. |
| `NEO4J_DATABASE` | no | `neo4j` | |
| `QDRANT_URL` | no | `http://localhost:6333` | Scaffolded — not used in MVP. |
| `QDRANT_API_KEY` | no | `""` | |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` / `POSTGRES_HOST` / `POSTGRES_PORT` | no | `estimator` / `""` / `estimator` / `localhost` / `5432` | Discrete vars used to assemble the DSN if `POSTGRES_DSN` is empty. When `POSTGRES_PASSWORD` is empty, Postgres is silently disabled. |
| `POSTGRES_DSN` | no | `""` | Full async DSN (e.g. `postgresql+asyncpg://user:pass@host:5432/db`). Overrides the discrete vars. |
| `POSTGRES_MIGRATE_ON_START` | no | `true` | Run Alembic upgrade in the FastAPI lifespan. Set to `false` in CI/CD if migrations run separately. |
| `POSTGRES_POOL_SIZE` / `POSTGRES_MAX_OVERFLOW` | no | `5` / `5` | SQLAlchemy connection-pool tuning. |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | no | `""` | Both must be set to enable tracing. Otherwise `@traced` is a no-op. Generated in the Langfuse UI after first launch (`http://localhost:3100`). |
| `LANGFUSE_HOST` | no | `http://localhost:3100` | Host-mapped port of the self-hosted Langfuse UI. Docker-compose overrides this to `http://langfuse-web:3000` for the dockerized estimator backend. |
| `LANGFUSE_NEXTAUTH_SECRET` / `LANGFUSE_NEXTAUTH_URL` / `LANGFUSE_SALT` | no | placeholder | NextAuth secrets for the Langfuse UI. Rotate before anything non-throwaway. |
| `LANGFUSE_ENCRYPTION_KEY` | no | placeholder zeros | 32-byte hex key for Langfuse field-level encryption. Generate with `openssl rand -hex 32`. |
| `LANGFUSE_CLICKHOUSE_USER` / `LANGFUSE_CLICKHOUSE_PASSWORD` | no | `clickhouse` / placeholder | ClickHouse credentials (Langfuse event store). |
| `LANGFUSE_REDIS_AUTH` | no | placeholder | Redis password (Langfuse queues + cache). |
| `LANGFUSE_MINIO_ROOT_USER` / `LANGFUSE_MINIO_ROOT_PASSWORD` / `LANGFUSE_S3_BUCKET` | no | `minio` / placeholder / `langfuse` | MinIO root creds + bucket name for Langfuse event uploads. |
| `LANGFUSE_POSTGRES_DB` | no | `langfuse` | Name of the Langfuse metadata DB on the shared Postgres instance. Auto-created on a fresh volume via `data/postgres-init/01-create-langfuse-db.sh`. |
| `LANGFUSE_INIT_*` | no | `""` | Optional org/project/user seeding on first boot. Leave blank to use the UI signup flow. |
| `BACKEND_HOST` / `BACKEND_PORT` | no | `0.0.0.0` / `8000` | |
| `BACKEND_CORS_ORIGINS` | no | `http://localhost:3000` | Comma-separated. |
| `NEXT_PUBLIC_API_URL` | no | `http://localhost:8000` | Inlined at frontend **build** time. |

Graceful degradation is intentional — every external dependency (Anthropic, Neo4j, Qdrant, Langfuse) can be absent and the system still starts. You'll get stubs or warnings instead of crashes.

---

## HTTP API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/estimates` | Start a new estimation. Body: `CreateEstimateRequest { project_name?, raw_input, stage2?, stage3? }`. Returns the envelope with status `pending`; Pass 1 runs as a background task. |
| `GET` | `/estimates/{id}` | Fetch the current envelope (status, pass1/pass2 estimates, clarifying questions, final). |
| `GET` | `/estimates/{id}/stream` | **SSE** event stream — emits `status` / `questions` / `final` / `error` as the graph progresses. Closes after `final` or `error`. |
| `POST` | `/estimates/{id}/answers` | Submit Stage 4 answers and resume the graph into Pass 2. Body: `{ answers: { question_id: text }, skip_remaining?: bool }`. Returns 409 if status ≠ `awaiting_answers`. |
| `GET` | `/health` | `{ "status": "ok", "service": "ai-sdlc-estimator" }`. |

Status machine: `pending → pass_1_running → awaiting_answers → pass_2_running → completed` (or `failed` with `.error`).

OpenAPI docs are served at `http://localhost:8000/docs` once the backend is up.

---

## Frontend wizard (Stages 1–5)

| Route | Stage | Notes |
|---|---|---|
| `/estimate/new` | 1. Raw input | Wrapped in `<Suspense>` to satisfy Next.js 15's `useSearchParams` rule. |
| `/estimate/draft/create` | (transition) | Wraps `useSearchParams` in Suspense; submits to `POST /estimates`. |
| `/estimate/draft/context` | 2. Project context | MVP subset of planning outline §4.2 — industry, project type, screen count, integrations, engagement model, **and the team roster** (name + category + seniority + rate + percentage per role). Client-side state in `lib/wizard-store.ts`. |
| `/estimate/draft/maturity` | 3. AI maturity | Six per-phase sliders (1–5). Team composition lives in Stage 2 — adjust the roster there, not here. |
| `/estimate/[id]/questions` | 4. Clarifying questions | Renders questions returned by Pass 1; POSTs answers to resume Pass 2. |
| `/estimate/[id]/review` | 5. Review | Per-phase bar chart, AI-vs-manual toggle, role-attributed cost table, copy-as-markdown. |

The dashboard at `/` lists past estimates pulled from the backend.

Global font scale: `app/globals.css` sets `html { font-size: 14px; }` so all Tailwind rem-based utilities shrink uniformly. Change it in one place to rescale the whole UI.

---

## Estimation algorithms in one breath

- **UCP (Discovery)** — `UUCW = 5·simple + 10·avg + 15·complex`, `UAW = 1·simple + 2·avg + 3·complex`, `TCF = 0.6 + 0.01·TFactor`, `ECF = 1.4 − 0.03·EFactor`, `UCP = (UUCW + UAW)·TCF·ECF`. Hours = `UCP · productivity · phase_ratio · stakeholder_multiplier`. AI maturity cap: `{1:0, 2:0.15, 3:0.30, 4:0.50, 5:0.65}`.
- **SCP (UX)** — screen count weighted by complexity buckets, divided by design-system maturity. Caps similar to UCP.
- **COCOMO II (Development)** — SLOC × effort multipliers (EM) × scale factors (SF). MVP uses the Early Design model.
- **Fagan (Code Review)** — `KLOC / inspection_rate` planning hours + rework factor. AI cap reflects review-assistant tooling.
- **CMP (Deployment)** — Configuration Management Points across environments × integration count × infra complexity.
- **TPA (QA)** — `dynamic_tp = FP · DF · (QD/24)` + `static_tp = FP · QI / 500`. Selected plan (A / B / C) drives the base + per-TP factor.

Every twin then wraps the manual mid in a PERT three-point range and applies the AI-maturity cap to derive the AI-assisted mid.

---

## Role attribution and rates

The team is a **user-defined roster** — Stage 2 lets the user add/remove roles, assign each one a name, a category, a seniority, an hourly rate, and a percentage of total effort. The default roster mirrors the original four-role split (Sr/Jr × Product/Engineering) but the user can replace it entirely.

**Role categories** (tag drives phase overrides — see below):

| Category | Examples |
|---|---|
| `product` | PM, PO |
| `engineering` | SWE, Tech Lead, Architect |
| `ui_ux` | Designer, UX Researcher |
| `qa` | QA Engineer, SDET, Test Lead |
| `devops` | SRE, Platform Engineer |
| `data` | Data Engineer, ML Engineer, Analyst |
| `other` | Anything else — opts out of overrides |

**Seniority** is one of `senior`, `mid`, `junior`, `other`.

`orchestrator/role_attribution.py::attribute_roles(total_hours, roster, phase)` is the **single shared splitter**. It starts from the user's per-role percentages, then applies phase-specific overrides keyed on the tags (not on fixed role IDs):

- **Discovery** — senior-biased: cap each role tagged `seniority=junior` at 25%, push excess to a same-category senior (fall back to any senior).
- **UX/Design** — product/design-biased: ensure (`product` + `ui_ux`) categories total ≥ 40%, pulling shortfall from other categories. Shortfall lands on `ui_ux` first, then `product`.
- **Code Review** — strongly senior-biased: cap each junior-tagged role at 15%.
- **Deployment** — technical-biased: ensure (`engineering` + `devops` + `data`) ≥ 75%. Shortfall lands on `devops` first.
- **Development** and **QA/Testing** — honor user input as-is.

All percentages are renormalized to 1.0 after overrides. A roster of `OTHER`/`OTHER`-tagged roles bypasses every override (pure pass-through). Never inline this logic in a twin — call `attribute_roles`.

**Default roster** (used when the user doesn't customize):

| Name | Category | Seniority | Default rate | Default % |
|---|---|---|---|---|
| Sr. Product | `product` | `senior` | $220/h | 20% |
| Jr. Product | `product` | `junior` | $140/h | 10% |
| Sr. Engineer | `engineering` | `senior` | $240/h | 50% |
| Jr. Engineer | `engineering` | `junior` | $150/h | 20% |

The frontend Stage 2 page hosts the `<RoleRosterEditor>` component — add/remove rows, dropdowns for category and seniority, an hourly-rate input, and a percentage slider that auto-rebalances to keep the total at 100%.

---

## Persistence and observability

- **LangGraph checkpointer** — `db/neo4j_adapter.py::make_checkpointer()` returns `langgraph.checkpoint.memory.InMemorySaver` in MVP. State survives within a process (so `interrupt()` works) but **not** across restarts. A real Neo4j-backed `BaseCheckpointSaver` is a Phase-3 swap at this exact call site.
- **Neo4j estimate snapshots** — `save_estimate_envelope(...)` writes one `Estimate` node + N `Phase` nodes via idempotent Cypher `MERGE`. Called at status transitions in `main.py`. **Silently no-ops** when Neo4j is unavailable.
- **Postgres history + calibration** — `save_estimate_history(...)` upserts the envelope into `estimate_history` and replaces its rows in `phase_history` on every status transition (Pass 1 phases get superseded by Pass 2 in place). On status `completed`, `refresh_calibration_for_phase(...)` recomputes the rolling per-(phase, industry, project_type, maturity) aggregates in `calibration_aggregates`. Twins read these aggregates during Pass 1 via `parse_input → state["calibration_examples"]` so the LLM has historical anchors for its UCP / FP / SLOC → hours mapping. **Silently no-ops** when Postgres is unavailable. Alembic migrations run on startup when `POSTGRES_MIGRATE_ON_START=true` (default).
- **Langfuse** — `@traced(name=..., as_type=...)` decorates LLM calls and graph nodes. With keys absent, it installs a no-op decorator that **preserves `inspect.iscoroutinefunction`** — important because LangGraph inspects node fns to decide sync vs async dispatch. Self-hosted via docker-compose: a `langfuse-web` (UI on `http://localhost:3100`) + `langfuse-worker` + ClickHouse + Redis + MinIO stack, sharing the project's Postgres for metadata under a separate `langfuse` database. The estimator backend points at `http://langfuse-web:3000` inside the compose network and `http://localhost:3100` when run on the host.
- **Qdrant** — client + collection bootstrap is in place but no data is ingested. Vector calibration is Phase 3 (the SQL aggregates above are the MVP version).

---

## Testing

Backend (~116 tests, pytest with asyncio auto-mode):

```bash
cd backend && uv run pytest                              # full suite
cd backend && uv run pytest tests/test_api.py -q         # one file
cd backend && uv run pytest tests/test_postgres_layer.py # the new persistence layer
cd backend && uv run pytest -k discovery -q              # by keyword
cd backend && uv run ruff check .                        # lint
cd backend && uv run mypy .                              # type-check
```

Postgres tests use an **in-memory aiosqlite** engine wired into `postgres_adapter` via a fixture — no live Postgres is required for CI. The ORM uses portable column types so the SQLite schema matches the Alembic-generated Postgres schema 1:1.

Alembic from the host shell:

```bash
cd backend && uv run alembic upgrade head           # apply migrations
cd backend && uv run alembic revision --autogenerate -m "describe"
cd backend && uv run alembic downgrade -1
```

Frontend (Vitest, node env):

```bash
cd frontend && npm test                # one-shot
cd frontend && npm run test:watch      # watch
cd frontend && npm run lint            # next lint
cd frontend && npm run type-check      # tsc --noEmit
cd frontend && npm run build           # produces .next/standalone for Docker
```

`vitest.config.ts` only globs `lib/**/*.test.ts`, `components/**/*.test.ts`, and `instrumentation.test.ts` — add new test paths to the `include` array or they won't run.

Lifespan tests assert the ready-log line shape (`✓ Backend ready ...` / `✓ Frontend ready ...`); operators grep for these in container logs. Don't change the format without updating those tests.

---

## MVP scope and what's deferred

**In scope (Phase 1 — implemented)**

- Single LLM (Claude) for all twins via forced tool-use structured output
- Two-pass orchestration with LangGraph `interrupt()` for clarifying questions
- All six twin algorithms (UCP, SCP, COCOMO II, Fagan, CMP, TPA + 3-plan QA)
- Dual-scenario aggregation (AI-assisted vs. manual-only) end-to-end
- Stage 1 (raw text), simplified Stages 2–3, full Stages 4–5
- Neo4j envelope persistence + Postgres history & twin calibration aggregates (when reachable)
- Alembic migrations + programmatic upgrade on startup
- Langfuse SDK wired but optional
- Dockerized full stack

**Deferred (Phase 2 / 3 / 4 — scaffolded, not implemented)**

- A2A peer-to-peer cross-phase signaling between twins
- File upload parsing (RFP / SOW PDFs)
- Full Stage 2 / 3 field set per planning outline §4.2
- Qdrant vector-similarity calibration (Postgres SQL aggregates are the MVP version)
- Neo4j-backed LangGraph checkpointer (in-memory only today)
- Multi-model strategy (different tiers per twin)
- Project-profile templates and estimate history / comparison views
- Langfuse trace viewer page (`/estimate/[id]/explain`)
- Proposal document export / PM-tool integration

Each deferred area has either a corresponding TODO comment or a scaffolded folder pointing to the relevant planning-outline section.

---

## Troubleshooting

- **Neo4j fails to start with `JettyWebServer.loadStaticContent: Path is null`** — newer 5.x community images regress on arm64. The image is pinned to `neo4j:5.20-community` for that reason. Don't bump without testing on arm64.
- **Neo4j "no space left on device"** — the Docker VM's virtual disk is full. The compose file uses **bind mounts** under `./data/neo4j/{data,logs}` so the host (which has space) holds the data. If you also see Docker layer build failures, prune: `docker image prune -af && docker builder prune -f`.
- **Backend says "Langfuse disabled"** — expected when `LANGFUSE_PUBLIC_KEY` or `LANGFUSE_SECRET_KEY` is empty. Self-hosted Langfuse is up at `http://localhost:3100`; sign up there, create a project, and paste the generated `pk-lf-…` / `sk-lf-…` into `.env`.
- **Langfuse UI 500s on first load** — usually a `langfuse-web` ↔ Postgres migration race. `docker compose logs langfuse-web | grep -i prisma` will show the migration; wait for it to finish (~30s on first boot) then refresh.
- **`langfuse-web` healthcheck failing with `password authentication failed` for `estimator`** — the Postgres init script didn't run because `./data/postgres` already had content. Create the DB manually: `docker exec sdlc-postgres psql -U estimator -c "CREATE DATABASE langfuse;"` then `docker compose restart langfuse-web langfuse-worker`.
- **ClickHouse / MinIO data taking space** — bind-mounted at `./data/clickhouse/` and `./data/minio/`. `docker compose down -v` does NOT wipe them; remove the directories manually for a fully clean reset.
- **Backend says "Neo4j connect failed; persistence disabled"** — `NEO4J_PASSWORD` not set or Neo4j is down. The backend keeps working without persistence.
- **Backend says "Postgres disabled (no POSTGRES_DSN / POSTGRES_PASSWORD)"** — expected when neither is set. History writes + twin calibration silently no-op; the rest of the API works. Set `POSTGRES_PASSWORD` (or `POSTGRES_DSN`) to enable.
- **Backend says "Alembic upgrade failed"** — the lifespan logs but doesn't crash. Run `uv run alembic upgrade head` from `backend/` to apply migrations manually and inspect the error.
- **Twins not improving across runs** — calibration only refreshes when an estimate reaches status `completed`. Check `calibration_aggregates` in Postgres (`psql -U estimator -d estimator -c "select * from calibration_aggregates"`) to see what's accumulated.
- **Twin returns a stub estimate** — the twin's LLM call failed (often: `ANTHROPIC_API_KEY` missing or model id wrong). Check `confidence: 0.3` and `notes: "Stub output ..."` in the response. Set the env var and restart.
- **"Expected dict, got coroutine" from LangGraph** — something wrapped an async node with a sync decorator. The Langfuse no-op decorator already branches on `inspect.iscoroutinefunction`; if you add new decorators, mirror that pattern.
- **Next.js build fails on `useSearchParams()`** — wrap the page component in `<Suspense>`. `/estimate/new` and `/estimate/draft/create` already do this; copy the pattern.
- **Frontend can't reach the backend in Docker** — `NEXT_PUBLIC_API_URL` is build-time and is called from the browser. It must be `http://localhost:8000`, never the internal service name.

---

## License

Internal — not yet licensed.
