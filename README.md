# AI SDLC Project Cost Estimator

**Author:** [Kevin Quon](https://www.linkedin.com/in/kwkwan00/)

Multi-agent system that estimates **effort (hours), cost (USD), duration (weeks), and headcount** for AI-heavy software projects across the six SDLC phases — and produces a **dual-scenario** breakdown showing what each phase costs **with AI assistance** vs. **with manual delivery only**, so the gap is the realized AI ROI.

Six specialized LangGraph "twin" agents — each grounded in a formal estimation algorithm — collaborate through a two-pass orchestrator with a human-in-the-loop clarifying-questions step in the middle.

> The full design spec (3,462 lines, including worked examples) is in `ai-sdlc-project-cost-estimator-planning-outline.md`. This README summarizes what is implemented in the MVP.

**Persistence at a glance** — three stores, distinct jobs:

- **LangGraph in-memory checkpointer** — Pass 1 ↔ Pass 2 interrupt state (in-process only).
- **Neo4j** — graph-shaped envelope snapshots: one `Estimate` node per run, `INCLUDES_PHASE` edges to phase nodes. Useful for graph queries over the estimate corpus.
- **Postgres** — structured history (`estimate_history`, including the full `envelope_json` for verbatim redisplay; `phase_history`), rolling per-(phase, industry, project_type, codebase-context) **calibration aggregates** the twins query during Pass 1 to anchor their LLM-derived numbers, and the admin-tunable `ai_reduction_bands` table.

All three are best-effort: the backend keeps running when any of them is unavailable.

---

## Table of contents

- [What it does](#what-it-does)
- [The six twins](#the-six-twins)
- [Monte Carlo uncertainty](#monte-carlo-uncertainty)
- [Orchestrator architecture](#orchestrator-architecture)
- [Team-scaling model](#team-scaling-model)
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
4. **Synthesize** — aggregates per-phase outputs into a `DualScenarioEstimate`: total hours, $ cost, duration band, weekly burn, headcount by role, **AI hours/cost saved** (manual − AI), and the **LLM usage/cost** of producing the estimate (per-model token + dollar breakdown).
5. **Review** (Stage 5) — frontend renders per-phase bars, a toggle between AI-assisted and manual-only views, a role-attributed cost table, graphical algorithm breakdowns, a confidence meter, a **Monte Carlo "Confidence" section** (fan chart + "80% confident: X–Y h" readout + "P(AI saves time)"), a **team-scaling section** (coordination-overhead cost row + scaling-efficiency / sweet-spot readout), an AI-assistance-savings section, and an LLM cost/usage modal.

Before submission, two pre-submission agents help fill the wizard: a **prefill** agent normalizes the Stage 1 free text into a Stage 2 context, and a **roster** agent proposes the team roster. On Stage 3 submit, a **tooling classifier** turns the user's freeform AI-tooling description into per-phase tooling levels (researching unfamiliar tools via a self-hosted docs-mcp-server). Past estimates are listed on the landing page and can be redisplayed.

Every `PhaseEstimate` carries **both scenarios** as `HourRange(optimistic, most_likely, pessimistic)` plus matching role splits — the two numbers travel together end-to-end so you can never accidentally collapse to a single answer.

---

## The six twins

| Phase | Twin | Algorithm | What it scales on |
|---|---|---|---|
| Discovery | Discovery Analyst | **UCP** (default) or **FP-based analysis effort** | use case / actor counts × TFactor/EFactor, or FP × analysis hours/FP — switchable in Settings |
| UX / Design | UX Design Strategist | **SCP** — Screen Complexity Points | screen count × complexity weights, design system maturity |
| Development | Development Architect | **COCOMO II** (default), **Function Points**, or **COSMIC FP** | SLOC × scale factors (super-linear), or FP × hours/FP / CFP × hours/CFP (linear) — switchable in Settings |
| Code Review | Code Review Sentinel | **Fagan inspection** | KLOC, inspection rate, rework factor |
| Deployment / DevOps | Deployment & DevOps | **CMP** — Configuration Management Points | environments, integrations, infra complexity |
| QA / Testing | QA & Testing Strategist | **TPA** (default), **Test Case Point**, or **Defect Removal (Capers-Jones)** + 3-plan recommendation | FP × dynamic/static quality chars, test-case count × checkpoint complexity, or defect potential × removal effort — switchable in Settings |

Each twin:

1. Loads its system prompt from `backend/orchestrator/prompts/<twin>.md`.
2. Renders the parsed context + Stage 2/3 + (on Pass 2) the user's answers as a JSON block.
3. Calls Claude via `orchestrator/llm.py::call_structured(...)`, which exposes a Pydantic response model as a single tool and **forces** `tool_choice` to it. The tool input is validated back into the model — far more reliable than free-text JSON.
4. Runs the deterministic algorithm in Python over the LLM-extracted inputs.
5. Derives the AI-assisted hours from the manual baseline by applying the **AI-reduction guardrail bands** (`orchestrator/ai_acceleration.py::effective_ai_reduction(...)`) — the twin's *proposed* reduction is clamped into the per-(phase, tooling) band, then moderated by codebase context + team seniority, with a small verification penalty that can push the net reduction slightly negative. The realized reduction is recorded on the estimate as `effective_ai_reduction_pct`.
6. Wraps the mid number in a **Monte Carlo distribution** (`orchestrator/montecarlo.py`) instead of a fixed ±factor band — `optimistic`/`pessimistic` become P10/P90, with the deterministic mid kept as `most_likely` (see [Monte Carlo uncertainty](#monte-carlo-uncertainty)). It then splits hours across roles via the shared `role_attribution.attribute_roles(...)` (off the deterministic mid, so role hours sum to `most_likely`), and records the algorithm intermediates in a structured `breakdown: dict[str, float]` (not in prose `notes`).

The shared shape lives in `backend/orchestrator/nodes/_twin_base.py`. When adding or modifying a twin, the differences should live in the prompt file + post-processing math, **not** in the plumbing.

### AI-reduction guardrail bands

How much AI realistically reduces (or sometimes *increases*) a phase's effort is governed by **guardrail bands**, not fixed maturity multipliers. `DEFAULT_BANDS` (in `orchestrator/ai_acceleration.py`) keys `(Phase, AiToolingLevel)` → an allowed reduction range `[lo, hi]` (as a fraction). Tooling levels are `none` / `autocomplete` / `chat` / `agentic`; `autocomplete` is a code-writing assist, so it doesn't apply to discovery / ux_design / code_review (those phases have no autocomplete band).

`effective_ai_reduction(...)` clamps the twin's proposed reduction into the band (phases with no LLM proposal use the band midpoint), then multiplies by a codebase-context factor (greenfield 1.0 → brownfield-large-familiar 0.4) and a seniority factor, and subtracts verification penalties (regulated + large-familiar brownfield). The result is floored at `-0.15`, so risky brownfield work can net slightly negative (AI net-slower — METR 2025).

The bands are **DB-tunable** via the `ai_reduction_bands` table (admin endpoints `GET` / `PUT /admin/reduction-bands`), loaded into `state["reduction_bands"]` by `parse_input`; the in-code `DEFAULT_BANDS` are the fallback when Postgres is unavailable.

### QA's three plans

The QA twin uniquely recommends one of three test strategies. All three plan totals are computed and emitted structurally in `breakdown.plan_a_hours` / `plan_b_hours` / `plan_c_hours` (the selected plan drives the phase hours):

- **Plan A — automated harness** (best for AI-heavy, low regulatory)
- **Plan B — dedicated QA team** (best for high regulatory, no AI features)
- **Plan C — hybrid** (best when both AI and regulatory pressure are high)

Selection is rule-based (`auto_select_plan(has_ai, has_reg)`) but the twin may override; both are logged.

### Development self-consistency

COCOMO's most-likely is a *product* of several independently LLM-sampled drivers (SLOC × effort multipliers × scale factors), so the Development phase is the one estimate whose run-to-run number swings widely (~±30%) — and the frontier twin model ignores `temperature`, so it can't be pinned to greedy decoding. The Development twin therefore opts into **Pass-2 self-consistency**: it fires **K=5** independent `call_structured` calls concurrently (`asyncio.gather`) and folds them by the **median** of each numeric driver (carried on the median-hours sample), cutting the run-to-run noise to ~±15% without imposing any anchor. The other five twins run a single Pass-2 call. The plumbing is generic (`make_twin_nodes(ensemble_k=…, ensemble_aggregate_fn=…)` in `_twin_base.py`); only Development enables it. As a cheap guard against gross sizing errors, `consistency_check` also cross-checks the realized SLOC against an independent screen/integration estimate and emits a warning when they diverge badly (it never changes the numbers).

### Pre-submission and support agents

Alongside the six estimation twins, the backend runs four lighter LLM helpers (each pins its own model tier — see [Configuration](#configuration)):

- **Prefill** (`backend/prefill.py`, Haiku) — turns the Stage 1 raw text into a normalized Stage 2 context for the wizard form; chains into the roster agent. Endpoint: `POST /estimates/draft/prefill`.
- **Roster** (`backend/roster_agent.py`, Sonnet) — proposes the Stage 2 `RoleRoster` from the project context, then deterministically rebalances percentages to 100% and assigns rates/ids. Exposed to the frontend over AG-UI via `POST /estimates/draft/roster/agui` (`backend/roster_agui.py`).
- **Tooling classifier** (`backend/tooling_classifier.py`, Sonnet) — maps the freeform AI-tooling description to per-phase `AiToolingLevel`s, researching tools it doesn't recognize via a co-located **docs-mcp-server** (MCP client over streamable HTTP, with an optional scrape-then-index step). Falls back to `none` on any failure/timeout. Endpoint: `POST /estimates/draft/classify-tooling`.
- **Question consolidator** (inside `orchestrator/nodes/merge_pass1.py`, Haiku) — semantic dedup of the twins' overlapping clarifying questions, with a deterministic topic-dedup fallback when unset/unreachable.

Their prompts live in `backend/orchestrator/prompts/` alongside the six twins: `prefill_agent.md`, `roster_agent.md`, `tooling_classifier.md`, `question_consolidator.md`.

---

## Monte Carlo uncertainty

Each twin's three-point `HourRange` is no longer a fixed ±factor PERT band around the mid. `orchestrator/montecarlo.py` runs a Monte Carlo layer (default **2,000 draws**, `MC_DRAWS`-overridable) that propagates **three uncertainty sources** through the **unchanged** deterministic `compute_*` algorithm, per draw `i`:

```
base_i   = compute_*(sampled size drivers)            # 1. input-size uncertainty (nonlinear)
r_i      = reduction_sampler(rng)                      # 2. AI-effectiveness uncertainty
risk_i   = Σ_k Bernoulli(p_k) · PERT(low_k, high_k)    # 3. discrete risk events
manual_i = base_i + risk_i
ai_i     = base_i · (1 − r_i) + risk_i                 # risks hit both scenarios undiscounted
```

1. **Input-size** — the twin's LLM proposes a `low/high` interval (`montecarlo.Range3`) on its dominant driver (SLOC/KSLOC for COCOMO, FP for TPA, `cmp_score`, productivity, UX iteration factor, …) plus an `estimate_cov` fallback. Each draw perturbs that field via Beta-PERT and **re-runs the same `compute_*`** — so the nonlinearity (e.g. COCOMO's `KSLOC^E`) is captured, not linearized.
2. **AI-effectiveness** — the LLM's *proposed* reduction is sampled (from an LLM `reduction_range`, a default spread, or — for Discovery/UX, which don't propose one — the guardrail band itself), and `ai_acceleration.effective_ai_reduction(...)` is **re-run on every draw** so the clamp + codebase·seniority moderation + verification penalty are honored each time. The default spread is **left-skewed and heavier-tailed** (reaches farther below the proposed point than above it, with a reduced Beta-PERT shape) — empirically (METR 2025) realized AI speedup has a bounded upside but a long downside toward zero/net-negative, so the band leans pessimistic. The deterministic point reduction is unchanged; only the band is reshaped.
3. **Discrete risks** — the LLM now proposes structured `RiskInput {description, probability, impact_hours_low/high}` items, fired as independent Bernoulli events that add sampled hours to **both** scenarios undiscounted (so risks lift the *mean*, not the mode).

Load-bearing **invariants** (kept stable so the rest of the system and the eval rubrics don't break):

- The **modal draw** is "no risk fires + point reduction", so `most_likely` stays the exact deterministic mid; the band only widens to bracket it (it is never clamped away). `result_to_hour_range` expands optimistic/pessimistic to P10/P90 around the mode without moving it.
- `ai.most_likely == manual.most_likely × (1 − r)` holds exactly, and role hours sum to `most_likely` (attribution still runs off the deterministic mid).
- All new fields are **Optional** → persisted envelopes and the deterministic stub/legacy path remain backward-compatible (stub ranges carry no `std`, so they keep the old behavior end-to-end).

The module is **pure stdlib** (`random` + `statistics` + `math`; no numpy/scipy): Beta-PERT via `random.betavariate`, RNG seeded per `(estimate_id, phase, pass)` so streams are reproducible and phase-independent (safe under the parallel twin fan-out). `HourRange` gained `std`, `mean`, and a full `percentiles` vector (`{p5,p10,p25,p50,p75,p90,p95}`); `RiskInputList` tolerates a JSON-string `risks` array (a forced-tool-use quirk) instead of stubbing the whole phase.

**Project total** — `synthesize_estimate` now treats per-phase distributions as **independent** and variance-combines them (sum the means, root-sum-square the stds, then a guarded method-of-moments lognormal fit yields P10/P90 + the percentile fan), giving a correct, **narrower** project band than the old comonotonic sum. The comonotonic per-percentile sum is kept as the stub/legacy fallback when any phase lacks `std`.

**Frontend** — the review page renders a "Confidence" section: nested-confidence-band fan charts (`components/FanChart.tsx`, math in `lib/fan-chart.ts`), an "80% confident: X–Y h" readout from P10–P90, and a "P(AI saves time): NN%" overlap statistic. All three degrade cleanly to the three-point triangle when an estimate carries no percentiles.

**Eval** — `evals/rubrics.py`'s `algorithm_conformance` was reworked for the MC ranges (it now asserts the `most_likely` identity + `ai ≤ manual` sign at each percentile when `r ≥ 0`; the old per-percentile `ai == manual×(1−r)` equality is dropped, since sampling adds variance the comonotonic identity can't model), and a new `interval_calibration` rubric scores whether a known actual lands inside the predicted `[optimistic, pessimistic]` band.

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
- `commercial_processing` looks up per-role rates from the user's `Stage2Context.roster` (by `role_id`) to produce per-scenario dollar totals.
- `synthesize_estimate` aggregates the per-phase ranges across phases (variance-combining the Monte Carlo distributions as independent — see [Monte Carlo uncertainty](#monte-carlo-uncertainty)), computes `ai_hours_saved_pert = manual.pert_mean − ai.pert_mean`, derives headcount + duration band against the target timeline (or, with no target, from the throughput-optimal team — see the team-scaling model below), and rolls up a weekly burn rate. It also applies the **team-scaling model** at the project level (see [Team-scaling model](#team-scaling-model)).

Phase 1 explicitly excludes A2A peer-to-peer cross-phase signaling — twins share **only** what they read from state.

### Team-scaling model

Project-level staffing reality is modeled in `orchestrator/staffing.py` (pure stdlib) and applied by `synthesize_estimate` — the six twins stay independent. Two effects act on team size `n` (= Σ headcount):

- **Brooks coordination overhead** `o(n)` (`coordination_overhead(n)`) — capacity lost to the growing number of communication links, capped. `(1 + o(n))` inflates **both** total cost and duration.
- **Diminishing returns** `n^β` (β < 1) — imperfect partitionability. `team_throughput(n) = n^β·(1 − o(n))` shapes the no-target duration curve and the recommended team size (`optimal_team_size(...)`, the "sweet spot"), but does **not** inflate cost — the per-algorithm effort already embeds a normal team's productivity (COCOMO's scale exponent is itself a diseconomy term), so a second cost penalty would double-count.

Defaults `DEFAULT_STAFFING_COEFFS = {link_cost: 0.06, free_team_size: 3, overhead_cap: 0.40, diminishing_returns_exponent: 0.90}` are **DB-tunable** via the `staffing_coefficients` table (admin endpoints `GET`/`PUT /admin/staffing-coefficients`, edited from `/settings`). Per-role headcount, weekly burn, hours, and role-hours are unchanged; `DualScenarioEstimate` gains `brooks_overhead_pct`, `staffing_efficiency_pct`, `team_size`, and `optimal_team_size`, which the review page surfaces as a "Coordination overhead (+X%)" cost row + a scaling-efficiency readout with an over/under-staffing flag (`frontend/lib/staffing.ts`).

---

## Tech stack

**Backend**

- Python 3.12, `uv` for dependency management
- FastAPI + `uvicorn[standard]`
- LangGraph (`langgraph>=0.6`) + Anthropic SDK (`anthropic>=0.104`) via forced tool-use
- Pydantic v2 (`extra="forbid"` on every model)
- MCP SDK (`mcp>=1.12`) — client for the docs-mcp-server tool research
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

- Docker Compose: Neo4j 5.20-community + Postgres + Qdrant + a `docs-mcp-server` (host port `6280`, backs the tooling classifier) + (optional) the dockerized backend & frontend
- The self-hosted Langfuse stack (Langfuse web + worker, ClickHouse, Redis, MinIO) is **gated behind the `langfuse` compose profile** (`profiles: ["langfuse"]`) — a plain `docker compose up` excludes it. Enable with `COMPOSE_PROFILES=langfuse` in `.env`.
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
│   ├── main.py                # FastAPI app: draft/prefill, draft/classify-tooling,
│   │                          #   draft/roster/agui, admin/reduction-bands, admin/staffing-coefficients,
│   │                          #   /estimates(+history,+stream,+answers,+delete), /health
│   ├── config.py              # pydantic-settings, reads ../.env or .env
│   ├── prefill.py             # Stage 1 → Stage 2 prefill agent (Haiku)
│   ├── roster_agent.py        # team-roster proposal agent (Sonnet)
│   ├── roster_agui.py         # AG-UI endpoint wrapping the roster agent
│   ├── tooling_classifier.py  # freeform AI-tooling → per-phase levels (+docs-mcp research)
│   ├── reduction_bands_admin.py # GET/PUT /admin/reduction-bands handlers
│   ├── staffing_admin.py      # GET/PUT /admin/staffing-coefficients handlers
│   ├── pyproject.toml         # uv-managed deps
│   ├── Dockerfile             # python:3.12-slim + uv, non-root, HEALTHCHECK /health
│   │
│   ├── orchestrator/
│   │   ├── graph.py           # StateGraph topology
│   │   ├── llm.py             # call_structured(...) — forced tool-use → Pydantic; per-agent model resolution
│   │   ├── ai_acceleration.py # AI-reduction guardrail bands + effective_ai_reduction()
│   │   ├── montecarlo.py      # Monte Carlo uncertainty propagation (pure stdlib Beta-PERT)
│   │   ├── staffing.py        # team-scaling model: Brooks coordination + diminishing returns (pure stdlib)
│   │   ├── usage.py           # per-run Anthropic token-usage capture + cost estimation
│   │   ├── role_attribution.py# shared role-split with phase-specific overrides
│   │   ├── smoke.py           # CLI: `uv run python -m orchestrator.smoke [--no-llm]`
│   │   ├── nodes/
│   │   │   ├── _twin_base.py  # shared twin shape (prompt loader, user-prompt builder, stub)
│   │   │   ├── parse_input.py # raw text → parsed_context (with LLM-free fallback)
│   │   │   ├── discovery_analyst.py        # UCP
│   │   │   ├── ux_design_strategist.py     # SCP
│   │   │   ├── development_architect.py    # COCOMO II
│   │   │   ├── code_review_sentinel.py     # Fagan
│   │   │   ├── deployment_devops.py        # CMP
│   │   │   ├── qa_testing_strategist.py    # TPA + 3-plan recommendation
│   │   │   ├── merge_pass1.py  # gap→question consolidation (Haiku, deterministic fallback)
│   │   │   ├── merge_pass2.py
│   │   │   ├── await_user_answers.py       # interrupt()
│   │   │   ├── consistency_check.py
│   │   │   ├── commercial_processing.py    # per-role rates from the roster
│   │   │   └── synthesize_estimate.py      # DualScenarioEstimate (+ headcount_by_role, llm_usage)
│   │   └── prompts/           # six twins + prefill_agent, roster_agent, tooling_classifier, question_consolidator
│   │
│   ├── models/
│   │   ├── estimation_state.py  # LangGraph EstimationState TypedDict (incl. reduction_bands, calibration_examples)
│   │   ├── twin_outputs.py      # Phase, PhaseEstimate, HourRange (+std/mean/percentiles), RiskInput(List), DualScenarioEstimate (+ brooks_overhead_pct/staffing_efficiency_pct/team_size/optimal_team_size), LlmUsage, ...
│   │   └── project_schema.py    # CreateEstimateRequest, EstimateEnvelope, Stage2Context (roster), Stage3Context (codebase + AI-tooling), CodebaseContext, AiToolingLevel
│   │
│   ├── db/
│   │   ├── neo4j_adapter.py   # driver + make_checkpointer (InMemorySaver in MVP) + save_estimate_envelope
│   │   ├── postgres_adapter.py# async engine + session_scope() — no-ops when DSN unset
│   │   ├── orm_models.py      # SQLAlchemy models: EstimateHistory (+envelope_json), PhaseHistory, CalibrationAggregate, AiReductionBand, StaffingCoefficient
│   │   ├── repositories/      # history, calibration, bands, staffing repos (save/list/get + delete, refresh_calibration_for_phase, get_calibration*, reduction-band + staffing-coefficient reads/writes)
│   │   ├── migrate.py         # programmatic `alembic upgrade head` for the FastAPI lifespan
│   │   └── qdrant_adapter.py  # client init (no ingestion in MVP)
│   │
│   ├── alembic/               # async migrations (env.py reads settings.resolved_postgres_dsn)
│   │   ├── env.py
│   │   ├── script.py.mako
│   │   └── versions/          # 0001 history+calibration … 0012 (reduction bands, envelope_json, nullable raw_input, band retunes, staffing_coefficients, dev-agentic band raise, default rate card, app_settings)
│   ├── alembic.ini
│   │
│   ├── observability/
│   │   ├── langfuse_wrapper.py# @traced(...) decorator — no-op when env keys absent, async-preserving
│   │   ├── logging_config.py  # configure_logging() — root log level + format
│   │   └── request_logging.py # ASGI middleware: method / path / status / latency per request
│   │
│   └── tests/                 # pytest, asyncio auto-mode (~470 tests)
│
└── frontend/
    ├── app/
    │   ├── layout.tsx / page.tsx / providers.tsx   # landing page lists + redisplays past estimates
    │   ├── settings/page.tsx  # edit AI-reduction bands + team-scaling coefficients (gear icon)
    │   ├── globals.css        # html { font-size: 14px } — global UI scale
    │   └── estimate/
    │       ├── new/                          # Stage 1
    │       ├── draft/{create,context,maturity}/  # Stages 2-3 wizard (client-side, pre-submit)
    │       └── [id]/{questions,review}/      # Stages 4-5 (server-driven)
    ├── components/            # PhaseBar, DualScenarioToggle, RoleRosterEditor, StageProgress,
    │                          #   ConfidenceMeter, FanChart (Monte Carlo), AlgorithmBreakdownChart,
    │                          #   AlgorithmTooltip/Badge, AiSavingsSection, BreakdownView, Modal,
    │                          #   Tabs (review-page panels), GanttChart + PertChart (Timeline),
    │                          #   DocumentUpload (Stage 1 file upload), RosterRationaleModal, FieldHint
    ├── lib/                   # schemas (Zod), api-client (fetch + SSE), wizard-store, types, format,
    │                          #   algorithms, breakdown, fan-chart (MC math), staffing (team-scaling),
    │                          #   schedule (Gantt/PERT/critical-path + MC finish-risk), document-extract (PDF/Word/text),
    │                          #   review-ui, estimate-status, roster-agui
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

The backend and frontend can run as compose services alongside Neo4j, Postgres, Qdrant, and the docs-mcp-server.

```bash
cp .env.example .env             # fill in ANTHROPIC_API_KEY + NEO4J_PASSWORD + POSTGRES_PASSWORD
docker compose up -d --build     # builds estimator apps + brings up neo4j / postgres / qdrant / docs-mcp-server
docker compose ps                # all services should be healthy
docker compose logs estimator-backend  | grep "Backend ready"
docker compose logs estimator-frontend | grep "Frontend ready"
```

**Langfuse is optional and off by default.** The self-hosted Langfuse stack is gated behind the `langfuse` compose profile, so a plain `docker compose up` skips it. To run it:

```bash
# 1. Enable the profile and rotate the Langfuse secrets in .env before anything non-throwaway:
#      COMPOSE_PROFILES=langfuse
#      openssl rand -hex 32      # -> LANGFUSE_ENCRYPTION_KEY
#      openssl rand -base64 32   # -> LANGFUSE_NEXTAUTH_SECRET / LANGFUSE_SALT
# 2. Bring up the full stack (profile is read from .env's COMPOSE_PROFILES):
docker compose up -d --build
# 3. First-run Langfuse setup (only needed once):
#      a. open http://localhost:3100 → sign up → create org → create project
#      b. project settings → API keys → "Create new API key"
#      c. paste pk-lf-... / sk-lf-... into .env's LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY
#      d. docker compose restart estimator-backend
# (Alternative: set the LANGFUSE_INIT_* env vars in .env to seed an org/user on first
# boot — see .env.example for the full list.)
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
| `ANTHROPIC_MODEL` | no | `claude-sonnet-4-6` | Model id the six estimation twins use. The four support agents pin their own tier below. |
| `ANTHROPIC_MODEL_PREFILL` | no | `claude-haiku-4-5` | Stage 1 → Stage 2 prefill agent (cheap/bounded → Haiku). |
| `ANTHROPIC_MODEL_ROSTER` | no | `claude-sonnet-4-6` | Team-roster proposal agent (knowledge-heavy → Sonnet). |
| `ANTHROPIC_MODEL_MERGE` | no | `claude-haiku-4-5` | Clarifying-question consolidation in `merge_pass1` (cheap → Haiku; deterministic fallback). |
| `ANTHROPIC_MODEL_TOOLING` | no | `claude-sonnet-4-6` | AI-tooling classifier (broad tool knowledge → Sonnet). |
| `OPENAI_API_KEY` | no | `""` | Authenticates the eval harness LLM-as-judge (`make evals`). Not used by the production estimator. Also satisfies the docs-mcp-server embeddings provider when scraping. |
| `OPENAI_MODEL_EVAL` | no | `gpt-5.5` | Default judge model for the eval harness's LLM rubrics. Override per run with `--judge-model` (an Anthropic id routes to the `call_structured` fallback). |
| `DOCS_MCP_URL` | no | `http://localhost:6280/mcp` | Self-hosted docs-mcp-server the tooling classifier consults (MCP over streamable HTTP). Blank disables lookups (unknown tools → `none`). Compose overrides this to the in-network hostname. |
| `DOCS_MCP_AUTH_TOKEN` | no | `""` | Optional bearer token for docs-mcp-server. |
| `DOCS_MCP_RESEARCH_TIMEOUT_S` | no | `25.0` | Hard ceiling on the docs-mcp search lookup (it runs in the Stage 3 submit path). On timeout, unknown tools → `none`. |
| `DOCS_MCP_AUTO_SCRAPE` | no | `true` | When set, an unindexed tool is scraped (docs crawled + embedded) before continuing, not just searched. Requires an embeddings provider (`OPENAI_API_KEY`) on docs-mcp-server. |
| `DOCS_MCP_SCRAPE_TIMEOUT_S` | no | `240.0` | Larger ceiling for the scrape path. On timeout/failure tools → `none`. |
| `COMPOSE_PROFILES` | no | `""` | Docker Compose profile selector. Set to `langfuse` to bring up the self-hosted Langfuse stack (off by default). |
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
| `LANGFUSE_HOST` | no | `https://cloud.langfuse.com` (`.env.example` sets `http://localhost:3100` for the self-hosted stack) | Langfuse ingest host. Docker-compose overrides this to `http://langfuse-web:3000` for the dockerized estimator backend. |
| `LANGFUSE_NEXTAUTH_SECRET` / `LANGFUSE_NEXTAUTH_URL` / `LANGFUSE_SALT` | no | placeholder | NextAuth secrets for the Langfuse UI. Rotate before anything non-throwaway. |
| `LANGFUSE_ENCRYPTION_KEY` | no | placeholder zeros | 32-byte hex key for Langfuse field-level encryption. Generate with `openssl rand -hex 32`. |
| `LANGFUSE_CLICKHOUSE_USER` / `LANGFUSE_CLICKHOUSE_PASSWORD` | no | `clickhouse` / placeholder | ClickHouse credentials (Langfuse event store). |
| `LANGFUSE_REDIS_AUTH` | no | placeholder | Redis password (Langfuse queues + cache). |
| `LANGFUSE_MINIO_ROOT_USER` / `LANGFUSE_MINIO_ROOT_PASSWORD` / `LANGFUSE_S3_BUCKET` | no | `minio` / placeholder / `langfuse` | MinIO root creds + bucket name for Langfuse event uploads. |
| `LANGFUSE_POSTGRES_DB` | no | `langfuse` | Name of the Langfuse metadata DB on the shared Postgres instance. Auto-created on a fresh volume via `data/postgres-init/01-create-langfuse-db.sh`. |
| `LANGFUSE_INIT_*` | no | `""` | Optional org/project/user seeding on first boot. Leave blank to use the UI signup flow. |
| `BACKEND_HOST` / `BACKEND_PORT` | no | `0.0.0.0` / `8000` | |
| `BACKEND_CORS_ORIGINS` | no | `http://localhost:3000` | Comma-separated. |
| `LOG_LEVEL` | no | `INFO` | Root backend log level (`DEBUG`/`INFO`/`WARNING`/`ERROR`), applied by `observability.logging_config`. |
| `NEXT_PUBLIC_API_URL` | no | `http://localhost:8000` | Inlined at frontend **build** time. |

Graceful degradation is intentional — every external dependency (Anthropic, Neo4j, Qdrant, Langfuse) can be absent and the system still starts. You'll get stubs or warnings instead of crashes.

---

## HTTP API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/estimates/draft/prefill` | Pre-submission: normalize Stage 1 raw text into a Stage 2 context (prefill agent). Always returns a valid context (degrades to defaults on LLM failure). |
| `POST` | `/estimates/draft/classify-tooling` | Pre-submission: classify the freeform Stage 3 AI-tooling description into per-phase `AiToolingLevel`s. Degrades to all-`none` on failure. |
| `POST` | `/estimates/draft/roster/agui` | Pre-submission: AG-UI agent run that proposes the Stage 2 team roster (streams a `STATE_SNAPSHOT`). |
| `GET` | `/admin/reduction-bands` | Read the effective AI-reduction guardrail bands (code defaults merged with DB overrides) as editable percentages — backs the Settings screen. |
| `PUT` | `/admin/reduction-bands` | Persist edited reduction bands. No-ops (response `editable: false`) when Postgres is disabled. |
| `GET` | `/admin/staffing-coefficients` | Read the effective team-scaling coefficients (Brooks's Law + diminishing returns; code defaults merged with DB overrides) — backs the Settings screen. |
| `PUT` | `/admin/staffing-coefficients` | Persist edited staffing coefficients. No-ops (response `editable: false`) when Postgres is disabled. |
| `GET` | `/admin/default-rates` | Read the effective default rate card (per role category × seniority; code defaults merged with DB overrides) — backs the Settings screen. |
| `PUT` | `/admin/default-rates` | Persist edited default rates. No-ops (response `editable: false`) when Postgres is disabled. |
| `GET` | `/admin/discovery-sizing-method` | Read the Discovery twin's sizing method (`ucp` default \| `function_points`) + the allowed choices — backs the Settings screen. |
| `PUT` | `/admin/discovery-sizing-method` | Persist the chosen sizing method. No-ops (response `editable: false`) when Postgres is disabled. |
| `GET` | `/admin/development-sizing-method` | Read the Development twin's sizing method (`cocomo` default \| `function_points` \| `cosmic_function_points`) + the allowed choices — backs the Settings screen. |
| `PUT` | `/admin/development-sizing-method` | Persist the chosen sizing method. No-ops (response `editable: false`) when Postgres is disabled. |
| `GET` | `/admin/qa-sizing-method` | Read the QA/testing twin's sizing method (`tpa` default \| `test_case_point` \| `defect_removal`) + the allowed choices — backs the Settings screen. |
| `PUT` | `/admin/qa-sizing-method` | Persist the chosen QA sizing method. No-ops (response `editable: false`) when Postgres is disabled. |
| `GET` | `/admin/contingency` | Read the global contingency reserve % (uplifts final cost + timeline) + bounds — backs the Settings screen. |
| `PUT` | `/admin/contingency` | Persist the contingency reserve % (`[0, 100]`). No-ops (response `editable: false`) when Postgres is disabled. |
| `POST` | `/estimates` | Start a new estimation. Body: `CreateEstimateRequest { project_name?, raw_input, stage2?, stage3? }`. Returns the envelope with status `pending`; Pass 1 runs as a background task. |
| `GET` | `/estimates/history` | Paginated persisted estimates (newest first) for the dashboard history list. Query: `?limit=&offset=`; returns `{ items, total }`. Empty when Postgres is disabled. |
| `GET` | `/estimates/{id}` | Fetch the current envelope (status, pass1/pass2 estimates, clarifying questions, final). **Authoritative source of truth.** On in-memory cache miss it falls back to the persisted `envelope_json` (when Postgres is connected) so completed estimates redisplay after a restart / in a fresh session. |
| `DELETE` | `/estimates/{id}` | Delete an estimate — removes it from the in-memory registries and Postgres history (+ phase rows). Idempotent → `204`. |
| `GET` | `/estimates/{id}/stream` | **SSE** event stream — emits `status` / `questions` / `final` / `error` as the graph progresses. Best-effort, via a per-estimate fan-out broker with a replay buffer: late / reconnecting / multiple concurrent subscribers all receive the backlog (no event stealing). Closes after `final` or `error`. |
| `POST` | `/estimates/{id}/answers` | Submit Stage 4 answers and resume the graph into Pass 2. Body: `{ answers: { question_id: text }, skip_remaining?: bool }`. Returns 409 if status ≠ `awaiting_answers`. |
| `GET` | `/health` | `{ "status": "ok", "service": "ai-sdlc-estimator" }`. |

Status machine: `pending → pass_1_running → awaiting_answers → pass_2_running → synthesizing → completed` (or `failed` with `.error`).

OpenAPI docs are served at `http://localhost:8000/docs` once the backend is up.

---

## Frontend wizard (Stages 1–5)

| Route | Stage | Notes |
|---|---|---|
| `/estimate/new` | 1. Raw input | Paste the description, pick an example, or **upload a document** (`<DocumentUpload>`) — PDF / Word `.docx` / `.txt` / `.md` are parsed **client-side** (`lib/document-extract.ts`: pdf.js + mammoth, dynamically imported) and dropped into the editable description box. Wrapped in `<Suspense>` to satisfy Next.js 15's `useSearchParams` rule. |
| `/estimate/draft/create` | (transition) | Wraps `useSearchParams` in Suspense; submits to `POST /estimates`. |
| `/estimate/draft/context` | 2. Project context | MVP subset of planning outline §4.2 — industry, project type, screen count, integrations, engagement model, **and the team roster** (description + category + seniority + rate + percentage per role). The `<RoleRosterEditor>` lives here, with a separate "Auto-adjust to 100%" button (no auto-rebalance on blur). A prefill/roster agent can pre-populate both. Client-side state in `lib/wizard-store.ts`. |
| `/estimate/draft/maturity` | 3. AI tooling & codebase | A **freeform AI-tooling description** text field (classified into per-phase tooling levels on submit via `POST /estimates/draft/classify-tooling`) plus a codebase-context selector (greenfield / brownfield small / large-unfamiliar / large-familiar). The old per-phase L0–L4 maturity sliders are gone. Team composition lives in Stage 2. |
| `/estimate/[id]/questions` | 4. Clarifying questions | Renders questions returned by Pass 1; POSTs answers to resume Pass 2. |
| `/estimate/[id]/review` | 5. Review | Organized into four tabs (`<Tabs>`) — **Cost breakdown**, **Timeline**, **AI assistance**, **Risk & uncertainty** — so it reads as focused views (only the active panel is mounted). Across them: per-phase bar chart, AI-vs-manual toggle, role-attributed cost table, graphical algorithm breakdown charts, a confidence meter, a **Monte Carlo "Confidence" section** (fan chart + "80% confident: X–Y h" + "P(AI saves time)"), a **team-scaling section** (coordination-overhead cost row + scaling-efficiency / sweet-spot readout via `lib/staffing.ts`), a **Timeline** (overlapping-phase **Gantt** with a milestone strip + a **PERT** critical-path/slack network + a Monte-Carlo finish-risk readout — P10–P90 weeks, P(finish ≤ target), per-phase criticality — all derived on the client in `lib/schedule.ts`), algorithm tooltips, an AI-assistance-savings section, risks/assumptions in modals off the phase cards, and an LLM cost/token-usage modal. Copy-as-markdown. |

The landing page at `/` lists historical estimates pulled from the backend and redisplays the review page for completed ones. A gear icon opens `/settings`, which edits the AI-reduction guardrail bands (`GET`/`PUT /admin/reduction-bands`), the team-scaling (Brooks's Law + diminishing-returns) coefficients (`GET`/`PUT /admin/staffing-coefficients`), and the default hourly **rate card** per role category × seniority (`GET`/`PUT /admin/default-rates`).

Global font scale: `app/globals.css` sets `html { font-size: 14px; }` so all Tailwind rem-based utilities shrink uniformly. Change it in one place to rescale the whole UI.

---

## Estimation algorithms in one breath

- **UCP (Discovery)** — `UUCW = 5·simple + 10·avg + 15·complex`, `UAW = 1·simple + 2·avg + 3·complex`, `TCF = 0.6 + 0.01·TFactor`, `ECF = 1.4 − 0.03·EFactor`, `UCP = (UUCW + UAW)·TCF·ECF`. Hours = `UCP · productivity · phase_ratio · stakeholder_multiplier`.
- **SCP (UX)** — screen count weighted by complexity buckets, divided by design-system maturity.
- **COCOMO II (Development)** — SLOC × effort multipliers (EM) × scale factors (SF). MVP uses the Early Design model.
- **Fagan (Code Review)** — `KLOC / inspection_rate` planning hours + rework factor.
- **CMP (Deployment)** — Configuration Management Points across environments × integration count × infra complexity.
- **TPA (QA)** — `dynamic_tp = FP · DF · (QD/24)` + `static_tp = FP · QI / 500`. Selected plan (A / B / C) drives the base + per-TP factor.

Every twin produces a manual baseline, derives the AI-assisted mid by applying the **AI-reduction guardrail bands** — the twin's proposed reduction clamped into its `(phase, tooling)` band, moderated by codebase context + team seniority, minus a verification penalty (floored at −0.15) — then propagates input-size, AI-effectiveness, and discrete-risk uncertainty through the algorithm with a **Monte Carlo** pass to produce the P10/P90 band (see [Monte Carlo uncertainty](#monte-carlo-uncertainty)). See [AI-reduction guardrail bands](#ai-reduction-guardrail-bands) above. The realized fraction is recorded as `effective_ai_reduction_pct`, and algorithm intermediates land in the structured `breakdown`.

---

## Role attribution and rates

The team is a **user-defined roster** — Stage 2 lets the user add/remove roles, assign each one a description, a category, a seniority, an hourly rate, and a percentage of total effort. The default roster mirrors the original four-role split (Sr/Jr × Product/Engineering) but the user can replace it entirely.

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

| Description | Category | Seniority | Default rate | Default % |
|---|---|---|---|---|
| Senior product manager | `product` | `senior` | $220/h | 20% |
| Junior product manager | `product` | `junior` | $140/h | 10% |
| Senior software engineer | `engineering` | `senior` | $240/h | 50% |
| Junior software engineer | `engineering` | `junior` | $150/h | 20% |

The frontend Stage 2 page hosts the `<RoleRosterEditor>` component — add/remove rows, dropdowns for category and seniority, an hourly-rate input, and percentage inputs with a separate **"Auto-adjust to 100%"** button (percentages are not auto-rebalanced on blur). A roster proposal agent (over AG-UI) can pre-populate the whole roster from the project context.

---

## Persistence and observability

- **LangGraph checkpointer** — `db/neo4j_adapter.py::make_checkpointer()` returns `langgraph.checkpoint.memory.InMemorySaver` in MVP. State survives within a process (so `interrupt()` works) but **not** across restarts. A real Neo4j-backed `BaseCheckpointSaver` is a Phase-3 swap at this exact call site.
- **Neo4j estimate snapshots** — `save_estimate_envelope(...)` writes one `Estimate` node + N `Phase` nodes via idempotent Cypher `MERGE`. Called at status transitions in `main.py`. **Silently no-ops** when Neo4j is unavailable.
- **Postgres history + calibration** — `save_estimate_history(...)` upserts the envelope into `estimate_history` (including the full `envelope_json` for verbatim redisplay) and replaces its rows in `phase_history` on every status transition (Pass 1 phases get superseded by Pass 2 in place). On status `completed`, `refresh_calibration_for_phase(...)` recomputes the rolling per-(phase, industry, project_type, **codebase-context**) aggregates in `calibration_aggregates`. The codebase-context code (0–3, `-1` = "any") rides in the column historically named `maturity_level` — it no longer holds an AI-maturity level. Twins read these aggregates during Pass 1 via `parse_input → state["calibration_examples"]` so the LLM has historical anchors for its UCP / FP / SLOC → hours mapping. `list_estimate_history(...)` / `get_estimate_envelope(...)` back the history list and the redisplay-after-restart fallback. **Silently no-ops** when Postgres is unavailable. Alembic migrations (`0001`–`0009`) run on startup when `POSTGRES_MIGRATE_ON_START=true` (default).
- **AI-reduction bands** — the admin-tunable `ai_reduction_bands` table holds the per-(phase, tooling) guardrail bands, merged with the in-code defaults and loaded into graph state by `parse_input`. Editable from the `/settings` screen via `GET`/`PUT /admin/reduction-bands`.
- **Staffing coefficients** — the admin-tunable `staffing_coefficients` table holds the team-scaling parameters (Brooks's Law coordination + diminishing returns), merged with the in-code `DEFAULT_STAFFING_COEFFS` fallback. Read/written by `get_staffing_coefficients` / `upsert_staffing_coefficients` (never-raise) and editable from the `/settings` screen via `GET`/`PUT /admin/staffing-coefficients`.
- **LLM usage/cost** — `orchestrator/usage.py` captures each Anthropic call's token usage into a per-estimate accumulator (bound around the Pass 1/Pass 2 run), then summarizes it into `DualScenarioEstimate.llm_usage` (per-model token + dollar breakdown) — the meta-cost of producing the estimate, surfaced in the review page's LLM cost modal. Best-effort: a no-op when no accumulator is bound.
- **Langfuse** — `@traced(name=..., as_type=...)` decorates LLM calls and graph nodes. With keys absent, it installs a no-op decorator that **preserves `inspect.iscoroutinefunction`** — important because LangGraph inspects node fns to decide sync vs async dispatch. Self-hosted via docker-compose but **gated behind the `langfuse` compose profile** (off by default; enable with `COMPOSE_PROFILES=langfuse`): a `langfuse-web` (UI on `http://localhost:3100`) + `langfuse-worker` + ClickHouse + Redis + MinIO stack, sharing the project's Postgres for metadata under a separate `langfuse` database. The estimator backend points at `http://langfuse-web:3000` inside the compose network; on the host (`make be`) it uses whatever `LANGFUSE_HOST` is set to (`.env.example` ships `http://localhost:3100`).
- **docs-mcp-server** — a co-located compose service (host port `6280`) the tooling classifier queries (and optionally scrapes-then-indexes) to research unfamiliar AI tools. Degrades gracefully: when unreachable or timed out, unknown tools stay `none`.
- **Qdrant** — client + collection bootstrap is in place but no data is ingested. Vector calibration is Phase 3 (the SQL aggregates above are the MVP version).

---

## Testing

Backend (~470 tests, pytest with asyncio auto-mode):

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

Eval harness (`backend/evals/`) — the deterministic rubrics (`algorithm_conformance`, `interval_calibration`) need no LLM, but the **LLM-as-judge** rubrics (`faithfulness`, `plan_quality`, `summarization`) default to **OpenAI GPT-5.5**. `evals/judge.py::judge_structured` is provider-aware: `gpt`/`o`-series models use the OpenAI SDK's structured-output `chat.completions.parse`, while `claude-*` models fall back to the production `orchestrator.llm.call_structured`. Grading the Anthropic twins with a different provider reduces same-model self-preference bias.

```bash
make evals                                             # full harness (default judge gpt-5.5)
cd backend && uv run python -m evals.run --judge-model claude-sonnet-4-6   # Anthropic judge
```

Set `OPENAI_API_KEY` + `OPENAI_MODEL_EVAL` (default `gpt-5.5`) for the judge, or pass `--judge-model` to override per run.

Lifespan tests assert the ready-log line shape (`✓ Backend ready ...` / `✓ Frontend ready ...`); operators grep for these in container logs. Don't change the format without updating those tests.

---

## MVP scope and what's deferred

**In scope (Phase 1 — implemented)**

- Claude (forced tool-use structured output) for all twins, with a **per-agent multi-model strategy** (twins on Sonnet; prefill + question-merge on Haiku; roster + tooling on Sonnet)
- Two-pass orchestration with LangGraph `interrupt()` for clarifying questions
- All six twin algorithms (UCP, SCP, COCOMO II, Fagan, CMP, TPA + 3-plan QA)
- AI-reduction guardrail bands (admin-tunable per phase × tooling) replacing the old maturity caps
- Monte Carlo uncertainty propagation per phase (input-size + AI-effectiveness + discrete risks) with variance-combined project totals + fan-chart visualization (pure stdlib, no numpy)
- Project-level team-scaling model (Brooks's Law coordination overhead + diminishing returns, admin-tunable) feeding cost, duration, and a recommended team size
- Freeform AI-tooling classification with docs-mcp-server research, plus the prefill + roster pre-submission agents
- Dual-scenario aggregation (AI-assisted vs. manual-only) end-to-end
- LLM cost / token-usage tracking surfaced on the estimate
- Stage 1 (raw text **or** client-side document upload — PDF / Word / text), simplified Stages 2–3 (Stage 3 = freeform AI tooling + codebase context), full Stages 4–5
- Estimate history landing page + verbatim redisplay of completed estimates
- Neo4j envelope persistence + Postgres history & twin calibration aggregates (when reachable)
- Alembic migrations + programmatic upgrade on startup
- Langfuse SDK wired but optional (gated behind a compose profile)
- Dockerized full stack

**Deferred (Phase 2 / 3 / 4 — scaffolded, not implemented)**

- A2A peer-to-peer cross-phase signaling between twins
- Server-side / OCR document parsing (the MVP extracts text **client-side** for PDF / Word / text; scanned image-only PDFs aren't OCR'd)
- Full Stage 2 / 3 field set per planning outline §4.2
- Qdrant vector-similarity calibration (Postgres SQL aggregates are the MVP version)
- Neo4j-backed LangGraph checkpointer (in-memory only today)
- Side-by-side estimate comparison views (history list + single-estimate redisplay exist; multi-estimate diffing does not)
- Langfuse trace viewer page (`/estimate/[id]/explain`)
- Proposal document export / PM-tool integration

Each deferred area has either a corresponding TODO comment or a scaffolded folder pointing to the relevant planning-outline section.

---

## Troubleshooting

- **Neo4j fails to start with `JettyWebServer.loadStaticContent: Path is null`** — newer 5.x community images regress on arm64. The image is pinned to `neo4j:5.20-community` for that reason. Don't bump without testing on arm64.
- **Neo4j "no space left on device"** — the Docker VM's virtual disk is full. The compose file uses **bind mounts** under `./data/neo4j/{data,logs}` so the host (which has space) holds the data. If you also see Docker layer build failures, prune: `docker image prune -af && docker builder prune -f`.
- **Backend says "Langfuse disabled"** — expected when `LANGFUSE_PUBLIC_KEY` or `LANGFUSE_SECRET_KEY` is empty. Langfuse is also **off by default** at the compose level — set `COMPOSE_PROFILES=langfuse` in `.env` to start the stack, then sign up at `http://localhost:3100`, create a project, and paste the generated `pk-lf-…` / `sk-lf-…` into `.env`.
- **Tooling classifier maps unknown tools to `none`** — the docs-mcp-server is unreachable, timed out (`DOCS_MCP_RESEARCH_TIMEOUT_S` / `DOCS_MCP_SCRAPE_TIMEOUT_S`), or has no embeddings provider (`OPENAI_API_KEY`) to search/index against. This is the conservative fallback; the rest of the estimate proceeds. Confirm the `docs-mcp-server` service is healthy on `:6280` and `DOCS_MCP_URL` is reachable from the backend.
- **Langfuse UI 500s on first load** — usually a `langfuse-web` ↔ Postgres migration race. `docker compose logs langfuse-web | grep -i prisma` will show the migration; wait for it to finish (~30s on first boot) then refresh.
- **`langfuse-web` healthcheck failing with `password authentication failed` for `estimator`** — the Postgres init script didn't run because `./data/postgres` already had content. Create the DB manually: `docker exec sdlc-postgres psql -U estimator -c "CREATE DATABASE langfuse;"` then `docker compose restart langfuse-web langfuse-worker`.
- **ClickHouse / MinIO data taking space** — bind-mounted at `./data/clickhouse/` and `./data/minio/`. `docker compose down -v` does NOT wipe them; remove the directories manually for a fully clean reset.
- **Backend says "Neo4j connect failed; persistence disabled"** — `NEO4J_PASSWORD` not set or Neo4j is down. The backend keeps working without persistence.
- **Backend says "Postgres disabled (no POSTGRES_DSN / POSTGRES_PASSWORD)"** — expected when neither is set. History writes + twin calibration silently no-op; the rest of the API works. Set `POSTGRES_PASSWORD` (or `POSTGRES_DSN`) to enable.
- **Backend says "Alembic upgrade failed"** — the lifespan logs but doesn't crash. Run `uv run alembic upgrade head` from `backend/` to apply migrations manually and inspect the error.
- **Twins not improving across runs** — calibration only refreshes when an estimate reaches status `completed`. Check `calibration_aggregates` in Postgres (`psql -U estimator -d estimator -c "select * from calibration_aggregates"`) to see what's accumulated. Note `maturity_level` there is a codebase-context code (0–3, `-1` = any), not an AI-maturity level.
- **Twin returns a stub estimate** — the twin's LLM call failed (often: `ANTHROPIC_API_KEY` missing or model id wrong). Check the low confidence + stub note in the response. Set the env var and restart.
- **"Expected dict, got coroutine" from LangGraph** — something wrapped an async node with a sync decorator. The Langfuse no-op decorator already branches on `inspect.iscoroutinefunction`; if you add new decorators, mirror that pattern.
- **Next.js build fails on `useSearchParams()`** — wrap the page component in `<Suspense>`. `/estimate/new` and `/estimate/draft/create` already do this; copy the pattern.
- **Frontend can't reach the backend in Docker** — `NEXT_PUBLIC_API_URL` is build-time and is called from the browser. It must be `http://localhost:8000`, never the internal service name.

---

## License

Internal — not yet licensed.
