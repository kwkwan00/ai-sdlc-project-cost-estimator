# AI SDLC Project Cost Estimator

**Author:** [Kevin Quon](https://www.linkedin.com/in/kwkwan00/)

Multi-agent system that estimates **effort (hours), cost (USD), duration (weeks), and headcount** for AI-heavy software projects across the six SDLC phases — and produces a **dual-scenario** breakdown showing what each phase costs **with AI assistance** vs. **with manual delivery only**, so the gap is the realized AI ROI.

Six specialized LangGraph "twin" agents — each grounded in a formal estimation algorithm — collaborate through a two-pass orchestrator with a human-in-the-loop clarifying-questions step in the middle.

The app ships **two estimation flows** that converge on the same `DualScenarioEstimate`, review page, and history:

- **Quick Estimate (top-down, parametric)** — the six-twin flow above. You describe the project; the twins size it with formal algorithms.
- **WBS Estimate (bottom-up)** — you (seeded by an LLM-drafted task tree) decompose the project into a Work Breakdown Structure, attach 3-point hours + a role to each leaf, and the backend rolls it up through the **same** Monte-Carlo + cost + staffing tail. See [WBS bottom-up estimation](#wbs-bottom-up-estimation).

> The full design spec (3,462 lines, including worked examples) is in `ai-sdlc-project-cost-estimator-planning-outline.md`. This README summarizes what is implemented in the MVP.
>
> **Just want to run it?** See [`QUICKSTART.md`](./QUICKSTART.md) for a complete, step-by-step setup-and-run guide.

**Persistence at a glance** — three stores, distinct jobs:

- **LangGraph in-memory checkpointer** — Pass 1 ↔ Pass 2 interrupt state (in-process only).
- **Neo4j** — graph-shaped envelope snapshots: one `Estimate` node per run, `INCLUDES_PHASE` edges to phase nodes. Also the graph-native home for **resumable WBS drafts** and committed WBS task trees (`(:WbsDraft)`/`(:Estimate)-[:HAS_CHILD]->(:WbsTask)`). Useful for graph queries over the estimate corpus.
- **Postgres** — structured history (`estimate_history`, including the full `envelope_json` for verbatim redisplay; `phase_history`), rolling per-(phase, industry, project_type, codebase-context) **calibration aggregates** the twins query during Pass 1 to anchor their LLM-derived numbers, and the admin-tunable `ai_reduction_bands` table.

All three are best-effort: the backend keeps running when any of them is unavailable.

---

## Table of contents

- [What it does](#what-it-does)
- [The six twins](#the-six-twins)
- [Monte Carlo uncertainty](#monte-carlo-uncertainty)
- [Orchestrator architecture](#orchestrator-architecture)
- [Team-scaling model](#team-scaling-model)
- [WBS bottom-up estimation](#wbs-bottom-up-estimation)
- [Statement of Work export](#statement-of-work-export)
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
5. **Review** (Stage 5) — frontend renders per-phase bars, a toggle between AI-assisted and manual-only views, a role-attributed cost table, graphical algorithm breakdowns, a confidence meter, a **Monte Carlo "Confidence" section** (fan chart + "80% confident: X–Y h" readout + "P(AI saves time)"), a **team-scaling section** (coordination-overhead cost row + scaling-efficiency / sweet-spot readout), and an AI-assistance-savings section. (The Anthropic token cost of *producing* estimates now lives on a top-level **Observability** page next to Settings — see [HTTP API](#http-api) — not on the estimate itself.)

Before submission, two pre-submission agents help fill the wizard: a **prefill** agent normalizes the Stage 1 free text into a Stage 2 context, and a **roster** agent proposes the team roster. On Stage 3 submit, a **tooling classifier** turns the user's freeform AI-tooling description into per-phase tooling levels (researching unfamiliar tools via a self-hosted docs-mcp-server). Stage 3 also lets you **scope the estimate to a subset of the six phases**. Past estimates are listed on the landing page and can be redisplayed — and any completed estimate can be **exported as an editable Statement of Work** (`.docx`) from its review page (see [Statement of Work export](#statement-of-work-export)).

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

Alongside the six estimation twins, the backend runs four lighter pre-submission / support LLM helpers (each pins its own model tier — see [Configuration](#configuration)):

- **Prefill** (`backend/agents/prefill.py`, Haiku) — turns the Stage 1 raw text into a normalized Stage 2 context for the wizard form. **Roster-free by design** — the team roster is proposed separately (below), so the form renders instantly and the roster streams in a beat later. Endpoint: `POST /estimates/draft/prefill`.
- **Roster** (`backend/agents/roster_agent.py`, Sonnet) — proposes the Stage 2 `RoleRoster` from the project context, then deterministically rebalances percentages to 100% and assigns ids + rates (from the admin **rate card**). Exposed to the frontend over AG-UI via `POST /estimates/draft/roster/agui` (`backend/agents/roster_agui.py`).
- **Tooling classifier** (`backend/agents/tooling_classifier.py`, Sonnet) — maps the freeform AI-tooling description to per-phase `AiToolingLevel`s, researching tools it doesn't recognize via a co-located **docs-mcp-server** (MCP client over streamable HTTP, with an optional scrape-then-index step; SSRF / prompt-injection hardened since its tool inputs derive from untrusted Stage-3 text). Falls back to `none` on any failure/timeout. Endpoint: `POST /estimates/draft/classify-tooling`.
- **Question consolidator** (inside `orchestrator/nodes/merge_pass1.py`, Haiku) — semantic dedup of the twins' overlapping clarifying questions, with a deterministic topic-dedup fallback when unset/unreachable.

A fifth LLM helper, the **SOW generator** (`backend/sow/agent.py`, `ANTHROPIC_MODEL_SOW`), runs *after* an estimate completes — see [Statement of Work export](#statement-of-work-export).

Prompts live in `backend/orchestrator/prompts/` — now a package exposing a cached `load_prompt(name)` — alongside the six twins: `prefill_agent.md`, `roster_agent.md`, `tooling_classifier.md` (+ `tooling_research_*.md` fragments), `question_consolidator.md`, `wbs_planner.md`, `sow_generator.md`.

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

## WBS bottom-up estimation

The **WBS (Work Breakdown Structure)** flow is the bottom-up complement to the parametric twins, reachable from the front page ("WBS Estimate"). It is a **separate flow** — *not* wired into the twin LangGraph graph — but it deliberately **reuses the same tail** so a WBS estimate produces an identical `DualScenarioEstimate` and renders on the same review page + history (badged `method: "wbs"`).

**Draft → edit → deterministic re-roll.** The numbers the user sees are always a deterministic rollup of the *current* tree; the LLM only seeds the starting draft.

1. **LLM planner** (`backend/agents/wbs_agent.py`, `WBS_MODEL` + `WBS_REASONING_EFFORT` — default **Claude Opus 4.8 / `max`** effort, a whole-project brainstorm) drafts a two-level WBS (work packages → leaf tasks) from the project description via forced tool-use, assigning each leaf a phase, a roster `role_id`, and 3-point hours. It degrades to a deterministic full-lifecycle skeleton when the LLM is unavailable, so the editor always opens with something editable. Prompt: `orchestrator/prompts/wbs_planner.md`. The draft is **streamed to the editor over AG-UI** (`POST /wbs/draft/agui`): `stream_structured(...)` streams the planner's tool-input JSON, a small parser extracts each work-package + task name as it lands, and the endpoint narrates friendly status messages ("Planning work package 2: Authentication…", "Adding task to Authentication: Build login API…", reviewing → finalizing) as `wbs_progress` custom events — so the user watches the system work in real time. A transient streaming hiccup falls back to the non-streaming draft (which has a corrective retry), never the generic skeleton. (If `WBS_MODEL` is switched to an **OpenAI** id the draft uses the non-streaming path directly — OpenAI streams content only after its hidden reasoning phase, so the narration would burst at the end — identical tree, opening/closing milestone messages only.) The planner's own LLM token cost is captured and carried through commit onto the estimate, where it rolls up into the top-level **Observability** page (it's no longer shown per-draft). On no API key it falls back to the skeleton with only the milestone messages.
2. **Complexity-aware realism factor** — bottom-up task estimates are systematically optimistic, so `_complexity_effort_factor(...)` scales the drafted leaf hours by a factor derived from the project's hidden complexity (regulatory regimes, integrations, surface area, project type, brownfield codebase — inferred from the parsed description, since the WBS wizard collects only roster + codebase). Clamped to `[1.2, 3.0]` and globally tunable via `WBS_EFFORT_SCALE`.
3. **User edits the tree** in an interactive MUI X Tree View editor (`frontend/components/WbsTreeViewEditor.tsx`) — add/remove/move tasks, set each leaf's phase, role, and 3-point hours via edit-modal. Debounced autosave persists to the server draft. Three LLM-assisted editor actions help close the bottom-up estimate's blind spots: **Reconcile** (`POST /estimates/wbs/reconcile`) triangulates the rollup against a parametric/twin estimate of the same brief to flag omitted or double-counted phases — with an **Apply calibration** action that rescales the diverging phases' task hours toward the parametric (per-phase, clamped — magnitudes only, the breakdown is preserved); **Check completeness** (`POST /estimates/wbs/completeness`) lists project-specific tasks the WBS likely *omitted within a phase*, each addable in one click; and a per-leaf **✨ Suggest hours** (`POST /estimates/wbs/suggest-hours`) re-estimates one leaf's 3-point hours from the brief + its siblings.
4. **Deterministic rollup** (`backend/orchestrator/wbs/rollup.py`) groups leaves by `Phase`, builds one `PhaseEstimate` per phase via `montecarlo.combine_pert_leaves` (the bottom-up sibling of `propagate_phase` — sums the leaf Beta-PERT draws, each scaled by **one shared lognormal common-factor** so the leaves are *correlated*, flooring the band's CoV instead of letting it collapse ~1/√N as the tree grows, + the **same** skewed AI-reduction sampler the twins use), attributes role hours from the leaves' **explicit** assignments, then feeds those phase estimates straight into the twins' typed tail seams `compute_total_costs(...)` + `synthesize_from_phase_estimates(...)` — so cost, Brooks staffing, headcount, durations, and the variance-combined project band are all computed by the **exact same code** as the twin flow. The load-bearing invariants hold (`most_likely` = Σ leaf modes; `ai.most_likely == manual.most_likely × (1 − eff)`; Σ role-hours == `most_likely`).

   The rollup also computes a **critical-path duration floor** (`orchestrator/wbs/critical_path.py`): the staffing model derives duration from effort ÷ team throughput and is **sequencing-blind**, so the longest dependency chain through the leaf graph (pure sequencing, AI-assisted hours, mirroring the frontend Gantt's `depends_on` semantics) is passed as `duration_floor_weeks` and the timeline is set to `max(staffing, critical-path)`. A long serial chain and a fully-parallel tree with the same hours no longer get the same duration; when the floor binds, the review page flags the timeline as **sequencing-bound** (`DualScenarioEstimate.critical_path_weeks`).

**WBS-specific contingency.** Because bottom-up estimates run optimistic even after the realism factor, the WBS flow carries its **own** explicit contingency reserve — an editable input on the editor defaulting to **30%** (`WBS_DEFAULT_CONTINGENCY_PCT`) — that uplifts final cost + timeline. It is **independent** of the global `app_settings` contingency the Quick Estimate uses (that one is unchanged).

**Resumable, graph-native drafts.** The WBS hierarchy lives in **Neo4j** as real nodes + relationships: `(:WbsDraft)-[:HAS_CHILD]->(:WbsTask)-[:HAS_CHILD]->…`. Drafts are saved atomically (one managed transaction), so a user can leave and **resume** later (the editor falls back to a localStorage cache when Neo4j is off). A committed estimate hangs the same task subgraph under its `(:Estimate)` node. **Duplicate** clones either an in-progress draft or a completed WBS estimate into a fresh editable draft (new task ids, " (Copy)" name) — duplicating from a completed estimate works even with Neo4j off, sourcing the tree + context (including the original project description, carried on the envelope as `wbs_raw_input` so a re-draft has prose to plan from) from the persisted `envelope_json`.

The WBS compute reuses, unchanged: `commercial_processing` / `synthesize_estimate`'s typed seams, the Monte Carlo `result_to_hour_range` / `make_rng`, the AI-reduction guardrail bands, role attribution, the review page, and the dashboard history. New code is confined to `agents/wbs_agent.py`, `routers/wbs.py`, `orchestrator/wbs/rollup.py`, `models/wbs_schema.py` + `models/wbs_task.py`, the Neo4j WBS-draft functions, and the `frontend/app/wbs/*` pages.

---

## Statement of Work export

From a completed estimate's review page, an **Export SOW** action turns the `DualScenarioEstimate` (per-role hours & rates, totals, durations, assumptions, risks) into an editable **`.docx` Statement of Work** — no more copy-pasting estimate numbers into a Word template. The document structure, boilerplate, and company voice are **not hardcoded**: they live in a YAML **template spec** (`backend/sow/templates/default_sow.yaml`) that code only consumes (`backend/sow/`).

The flow separates generation (one LLM call) from rendering (pure) so the user edits **between** them:

1. `POST /estimates/{id}/sow` → the **SOW agent** (`sow/agent.py`, pinned to `ANTHROPIC_MODEL_SOW`) builds its response model **dynamically from the template's `llm` sections** (`pydantic.create_model`) and makes one forced-tool-use call for the project-specific prose + an extract-or-null `client_facts` block. Deterministic mappers (`sow/mapper.py`) fill the fee table / schedule / resource summary / assumptions straight from the envelope; boilerplate sections are static. A token-resolution pass substitutes the grounded client facts into the template's `[TOKENS]` and reports any that stayed literal. Degrades to estimate-grounded stubs (all client tokens left as `[PLACEHOLDERS]`) with no API key — a SOW always generates.
2. The modal renders an **editable preview** (prose as textareas, bullets as lists, tables read-only) + a banner listing the `[PLACEHOLDERS]` to fill in Word.
3. `POST /estimates/{id}/sow/docx` → the renderer (`sow/renderer.py`, `python-docx`) writes the (edited) document to `.docx` bytes. **No LLM** on this leg.

**Guardrails** (deterministic, config-driven): the agent never says "the Client" (it uses the real name or a `[CLIENT NAME]` placeholder — `sow/composer.py::_normalize_client_refs`) and never invents vendor products / cloud services for inputs the user didn't state (`sow/vendor_guard.py` generalizes them, assembled from `sow/vendor_generalizations.yaml`). The delivering firm's name is **dependency-injected**, never hardcoded: it comes from the template's `branding.company`, overridable at deploy time via `SOW_COMPANY_NAME`, and every reference uses the `[COMPANY]` token. Works for both `method: "twins"` and `"wbs"` estimates.

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
- Qdrant client (`qdrant-client>=1.12`) — vector-similarity calibration store (completed estimates embedded via OpenAI for reference-class lookups; additive to Neo4j/Postgres)
- SSE via `sse-starlette`
- `python-docx` + `pyyaml` — server-side `.docx` Statement-of-Work rendering from a YAML template spec

**Frontend**

- Next.js 15 App Router (`output: "standalone"` for Docker)
- React 19
- TailwindCSS 3, `react-hook-form` + Zod (`@hookform/resolvers`)
- `@tanstack/react-query` for backend calls
- `recharts` for per-phase bars
- Vitest (node env) for unit tests

**Infra**

- Docker Compose: Neo4j 5.20-community + Postgres + Qdrant + a `docs-mcp-server` (host port `6280`, backs the tooling classifier) + the dockerized backend & frontend
- Bind-mount under `./data/{neo4j,postgres}/` (sidesteps the Docker VM disk limit)

---

## Repository layout

```
.
├── ai-sdlc-project-cost-estimator-planning-outline.md   # canonical design spec
├── docker-compose.yml         # neo4j + postgres + qdrant + docs-mcp-server + estimator-backend + estimator-frontend
├── Makefile                   # up / down / install-be / install-fe / be / fe / smoke / clean
├── .env.example               # required + optional env vars
├── data/neo4j/                # bind-mounted neo4j data + logs
├── data/postgres/             # bind-mounted postgres data
│
├── backend/
│   ├── main.py                # FastAPI app: lifespan (graph compile + Alembic upgrade) + mounts routers/ below
│   ├── runtime.py             # in-memory registries + SSE event broker + background-run orchestration
│   │                          #   + persistence fan-out (resolve_envelope, persist_completed_estimate)
│   ├── config.py              # pydantic-settings, reads ../.env or .env
│   ├── routers/               # HTTP surface, mounted by main.py:
│   │   ├── estimates.py       #   POST /estimates (+history, +{id}, +stream, +answers, +delete), /health
│   │   ├── observability.py   #   GET /observability/llm-usage — aggregate LLM cost across estimates
│   │   ├── drafts.py          #   /estimates/draft/{prefill, classify-tooling, roster/agui}
│   │   ├── admin.py           #   /admin/* (reduction-bands, staffing-coefficients, default-rates,
│   │   │                      #     {discovery,development,qa}-sizing-method, contingency)
│   │   ├── catalog.py         #   static option lists for the wizard
│   │   ├── wbs.py             #   /wbs/draft(+/agui stream, drafts CRUD, duplicate), /estimates/wbs(+/preview, +duplicate)
│   │   └── sow.py             #   POST /estimates/{id}/sow(+/docx) — Statement-of-Work export
│   ├── agents/                # non-twin LLM helpers (each pins its own model tier):
│   │   ├── prefill.py         #   Stage 1 → Stage 2 prefill (Haiku, roster-free)
│   │   ├── roster_agent.py    #   team-roster proposal (Sonnet) — rates from the rate card
│   │   ├── roster_agui.py     #   AG-UI streaming wrapper for the roster agent
│   │   ├── tooling_classifier.py # freeform AI-tooling → per-phase levels (+docs-mcp research, SSRF-hardened)
│   │   └── wbs_agent.py       #   WBS planner (drafts the bottom-up task tree) + realism factor
│   ├── admin/                 # config services behind routers/admin.py (code default + DB override):
│   │   ├── reduction_bands_admin.py / staffing_admin.py / rate_card_admin.py / contingency_admin.py
│   │   └── {discovery,dev,qa}_sizing_admin.py # thin wrappers over sizing_method_admin.py
│   ├── sow/                   # Statement-of-Work export (config-driven — see the SOW section):
│   │   ├── agent.py / composer.py / mapper.py / renderer.py (python-docx) / config.py / models.py
│   │   ├── vendor_guard.py    #   generalizes un-stated vendor products → capabilities
│   │   └── templates/default_sow.yaml + vendor_generalizations.yaml   # the tunable template + guard lists
│   ├── pyproject.toml         # uv-managed deps
│   ├── Dockerfile             # python:3.12-slim + uv, non-root, HEALTHCHECK /health
│   │
│   ├── orchestrator/
│   │   ├── graph.py           # StateGraph topology
│   │   ├── llm.py             # call_structured(...) / stream_structured(...) — forced tool-use → Pydantic; per-agent model resolution
│   │   ├── ai_acceleration.py # AI-reduction guardrail bands + effective_ai_reduction()
│   │   ├── montecarlo.py      # Monte Carlo uncertainty propagation (pure stdlib Beta-PERT)
│   │   ├── staffing.py        # team-scaling model: Brooks coordination + diminishing returns (pure stdlib)
│   │   ├── wbs/rollup.py      # bottom-up WBS rollup → DualScenarioEstimate (reuses the twin tail seams)
│   │   ├── wbs/critical_path.py # longest dependency chain → duration floor (max(staffing, sequencing))
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
│   │   └── prompts/           # package (cached load_prompt): six twins + parse_input, prefill_agent,
│   │                          #   roster_agent, tooling_classifier(+ tooling_research_*), question_consolidator,
│   │                          #   wbs_planner, sow_generator
│   │
│   ├── models/
│   │   ├── estimation_state.py  # LangGraph EstimationState TypedDict (incl. reduction_bands, calibration_examples)
│   │   ├── twin_outputs.py      # Phase, PhaseEstimate, HourRange (+std/mean/percentiles), RiskInput(List), DualScenarioEstimate (+ brooks_overhead_pct/staffing_efficiency_pct/team_size/optimal_team_size), LlmUsage, ...
│   │   ├── project_schema.py    # CreateEstimateRequest, EstimateEnvelope (+ method/wbs_tree/wbs_stage2/3/wbs_raw_input), Stage2Context (roster), Stage3Context (codebase + AI-tooling), CodebaseContext, AiToolingLevel
│   │   ├── wbs_schema.py        # WBS request/response models (draft/save/calculate) + WBS_DEFAULT_CONTINGENCY_PCT
│   │   └── wbs_task.py          # WbsTaskInput tree node + flatten/rebuild/iter helpers (leaf module)
│   │
│   ├── db/
│   │   ├── neo4j_adapter.py   # driver + make_checkpointer (InMemorySaver in MVP) + save_estimate_envelope
│   │   ├── postgres_adapter.py# async engine + session_scope() — no-ops when DSN unset
│   │   ├── orm_models.py      # SQLAlchemy models: EstimateHistory (+envelope_json), PhaseHistory, CalibrationAggregate,
│   │   │                      #   AiReductionBand, StaffingCoefficient, DefaultRate, CustomRateRole, AppSetting
│   │   ├── repositories/      # history, calibration, bands, staffing, rate-card, app-settings repos
│   │   │                      #   (save/list/get + delete, refresh_calibration_for_phase, keyed reads/writes)
│   │   ├── migrate.py         # programmatic `alembic upgrade head` for the FastAPI lifespan
│   │   └── qdrant_adapter.py  # client init (no ingestion in MVP)
│   │
│   ├── alembic/               # async migrations (env.py reads settings.resolved_postgres_dsn)
│   │   ├── env.py
│   │   ├── script.py.mako
│   │   └── versions/          # 0001 history+calibration … 0015 (reduction bands, envelope_json, band retunes,
│   │                          #   staffing_coefficients, default rate card, app_settings, custom_rate_roles, llm_call)
│   ├── alembic.ini
│   │
│   ├── observability/
│   │   ├── correlation.py     # per-request correlation-id contextvar (threaded into logs)
│   │   ├── logging_config.py  # configure_logging() — root log level + format
│   │   └── request_logging.py # ASGI middleware: method / path / status / latency per request
│   │
│   └── tests/                 # pytest, asyncio auto-mode (~750 tests)
│
└── frontend/
    ├── app/
    │   ├── layout.tsx / page.tsx / providers.tsx   # landing page lists + redisplays past estimates
    │   ├── settings/page.tsx  # tabbed admin: sizing methods + AI-reduction bands + team-scaling + rates/contingency
    │   ├── observability/page.tsx # top-level LLM cost/usage view (icon next to Settings in the header)
    │   ├── globals.css        # html { font-size: 14px } — global UI scale
    │   ├── wbs/{,new,team,edit/[draftId]}/  # WBS bottom-up flow: landing+resume, describe, team, tree editor
    │   └── estimate/
    │       ├── new/                          # Stage 1
    │       ├── draft/{create,context,maturity}/  # Stages 2-3 wizard (client-side, pre-submit)
    │       └── [id]/{questions,review}/      # Stages 4-5 (server-driven; review also renders WBS estimates)
    ├── components/            # PhaseBar, DualScenarioToggle, RoleRosterEditor, StageProgress,
    │                          #   ConfidenceMeter, FanChart (Monte Carlo), AlgorithmBreakdownChart,
    │                          #   AlgorithmTooltip/Badge, AiSavingsSection, BreakdownView, Modal,
    │                          #   Tabs (review-page panels), GanttChart + PertChart (Timeline),
    │                          #   WbsTreeViewEditor (MUI X Tree View) + WbsTreePanel (read-only review),
    │                          #   SowExportModal (editable SOW preview → .docx download),
    │                          #   DocumentUpload (Stage 1 file upload), RosterRationaleModal, FieldHint,
    │                          #   ProgressBar (WBS draft status line) + LlmUsagePanel (LLM cost/usage, Observability page)
    ├── lib/                   # schemas (Zod), api-client (fetch + SSE), wizard-store, types, format,
    │                          #   algorithms, breakdown, fan-chart (MC math), staffing (team-scaling),
    │                          #   schedule (Gantt/PERT/critical-path + MC finish-risk), document-extract (PDF/Word/text),
    │                          #   wbs (client PERT rollup + tree helpers) + wbs-store (localStorage cache),
    │                          #   sow (docx filename + placeholder helpers), review-ui, estimate-status,
    │                          #   roster-agui + wbs-agui (AG-UI streaming clients), progress (trickle-bar math)
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

The core stack — Neo4j, Postgres, Qdrant, the docs-mcp-server, and the two estimator apps — comes up with a plain `docker compose up`; there are no optional profiles.

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
| `WBS_MODEL` | no | `claude-opus-4-8` | The WBS **planner** (drafts the task tree) + **completeness critic** + per-leaf **suggest-hours** estimator. A deep-reasoning decomposition task → Claude Opus 4.8 by default; routed by `call_structured`/`stream_structured`'s provider-aware path (set a `gpt-*` value to use OpenAI instead). Degrades to the deterministic skeleton / empty findings without the relevant API key. |
| `WBS_REASONING_EFFORT` | no | `max` | Reasoning effort sent with `WBS_MODEL` — `output_config.effort` (`low`/`medium`/`high`/`xhigh`*/`max`; *`xhigh` model-dependent, `max` universally supported) on Anthropic, `reasoning_effort` (adds `minimal`/`xhigh`) on OpenAI. Defaults to `max` (a 50–150-leaf decomposition is a whole-project brainstorm); drop to `high`/`medium` to trade depth for latency/cost. |
| `ANTHROPIC_MODEL_SOW` | no | `claude-sonnet-4-6` | SOW generator agent (project-specific prose + client-fact extraction → Sonnet). |
| `SOW_COMPANY_NAME` | no | `""` | Deploy-time override of the delivering firm's name in exported SOWs. Empty ⇒ use the template's `branding.company`. Keeps a specific firm out of the repo. |
| `WBS_EFFORT_SCALE` | no | `1.0` | Global multiplier on the WBS bottom-up realism factor (`_complexity_effort_factor`). Raise to push every WBS estimate up, lower to trust the LLM's hours more. |
| `OPENAI_API_KEY` | no | `""` | Authenticates OpenAI: the eval harness LLM-as-judge (`make evals`), the docs-mcp-server embeddings provider when scraping, and the WBS planner/completeness **only if** `WBS_MODEL` is set to a `gpt-*` id (the default is Anthropic Opus). |
| `OPENAI_MODEL_EVAL` | no | `gpt-5.5` | Default judge model for the eval harness's LLM rubrics. Override per run with `--judge-model` (an Anthropic id routes to the `call_structured` fallback). |
| `DOCS_MCP_URL` | no | `http://localhost:6280/mcp` | Self-hosted docs-mcp-server the tooling classifier consults (MCP over streamable HTTP). Blank disables lookups (unknown tools → `none`). Compose overrides this to the in-network hostname. |
| `DOCS_MCP_AUTH_TOKEN` | no | `""` | Optional bearer token for docs-mcp-server. |
| `DOCS_MCP_RESEARCH_TIMEOUT_S` | no | `25.0` | Hard ceiling on the docs-mcp search lookup (it runs in the Stage 3 submit path). On timeout, unknown tools → `none`. |
| `DOCS_MCP_AUTO_SCRAPE` | no | `true` | When set, an unindexed tool is scraped (docs crawled + embedded) before continuing, not just searched. Requires an embeddings provider (`OPENAI_API_KEY`) on docs-mcp-server. |
| `DOCS_MCP_SCRAPE_TIMEOUT_S` | no | `240.0` | Larger ceiling for the scrape path. On timeout/failure tools → `none`. |
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
| `BACKEND_HOST` / `BACKEND_PORT` | no | `0.0.0.0` / `8000` | |
| `BACKEND_CORS_ORIGINS` | no | `http://localhost:3000` | Comma-separated. |
| `LOG_LEVEL` | no | `INFO` | Root backend log level (`DEBUG`/`INFO`/`WARNING`/`ERROR`), applied by `observability.logging_config`. |
| `NEXT_PUBLIC_API_URL` | no | `http://localhost:8000` | Inlined at frontend **build** time. |

Graceful degradation is intentional — every external dependency (Anthropic, Neo4j, Qdrant) can be absent and the system still starts. You'll get stubs or warnings instead of crashes.

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
| `GET` | `/admin/contingency` | Read the global contingency reserve % (uplifts final cost + timeline; **Quick Estimate only**) + bounds — backs the Settings screen. The WBS flow carries its own per-estimate contingency (default 30%) instead. |
| `PUT` | `/admin/contingency` | Persist the contingency reserve % (`[0, 100]`). No-ops (response `editable: false`) when Postgres is disabled. |
| `POST` | `/wbs/draft` | WBS: LLM-draft a Work Breakdown Structure tree from a project description and persist it as a resumable draft. Always returns an editable tree (degrades to a deterministic skeleton). |
| `POST` | `/wbs/draft/agui` | WBS: **AG-UI streaming** variant of `/wbs/draft` — emits friendly `wbs_progress` custom events as the planner drafts each work package + task, then a `STATE_SNAPSHOT` carrying the persisted draft (`draft_id` + tree + notes + `llm_usage`). Same persisted result as the POST, with live progress. |
| `GET` | `/wbs/drafts` | WBS: the "resume a draft" list (newest first). `resumable: false` signals Neo4j is off (client falls back to its localStorage cache). |
| `GET` / `PUT` / `DELETE` | `/wbs/drafts/{id}` | WBS: load a draft to resume / autosave the editor state / discard a draft. `GET` 404s when absent / Neo4j off. |
| `POST` | `/wbs/drafts/{id}/duplicate` | WBS: clone an in-progress draft into a new editable draft (fresh task ids, " (Copy)" name). |
| `POST` | `/estimates/{id}/wbs/duplicate` | WBS: clone a completed WBS estimate (from its review) into a new draft. Sources the tree + context from `envelope_json`, so it works with Neo4j off. `409` if the estimate isn't a WBS estimate. |
| `POST` | `/estimates/wbs/preview` | WBS: roll the current tree up into a `DualScenarioEstimate` **without persisting** — powers the editor's "Re-evaluate" button. Body carries `tree`, `stage2?`, `stage3?`, `contingency_pct?` (default 30). |
| `POST` | `/estimates/wbs/reconcile` | WBS: triangulate the bottom-up rollup against a **parametric (twin)** estimate of the same brief — returns per-phase + total divergence classified `aligned`/`likely_omitted_work`/`likely_double_count` to catch forgotten or double-counted work before commit. Estimates the **full lifecycle** so a fully-**omitted phase** (no WBS tasks) surfaces with `omitted: true` (add tasks), distinct from an under-sized one (calibrate). Runs the six twins' Pass-1 (≈7 LLM calls), explicit "Reconcile" button; degrades to a structural-only comparison (`parametric_available: false`) with no API key. |
| `POST` | `/estimates/wbs/completeness` | WBS: a **completeness critic** — one LLM call audits the tree against the work a project of this kind typically needs and returns project-specific tasks the WBS likely **omitted** (within-phase omission the totals-only reconciliation can't see), each with phase + 3-point estimate so the editor can add it in one click. Degrades to an empty list with no API key. |
| `POST` | `/estimates/wbs/suggest-hours` | WBS: re-estimate a **single leaf's** 3-point hours from the brief + its sibling tasks (so the number stays proportionate to the rest of the tree) — powers the per-task **✨ Suggest hours** button in the editor's edit modal. Degrades to the leaf's existing hours with no API key. |
| `POST` | `/estimates/wbs` | WBS: commit the tree — computes, persists a `method: "wbs"` envelope (Postgres history + Neo4j subgraph), retires the draft, and returns the envelope. Synchronous (the rollup is fast/deterministic). |
| `POST` | `/estimates` | Start a new estimation. Body: `CreateEstimateRequest { project_name?, raw_input, stage2?, stage3?, selected_phases? }`. `selected_phases` (omitted ⇒ all six) restricts which twins run, so you can estimate a subset of the SDLC. Returns the envelope with status `pending`; Pass 1 runs as a background task. |
| `GET` | `/estimates/history` | Paginated persisted estimates (newest first) for the dashboard history list. Query: `?limit=&offset=`; returns `{ items, total }`. Empty when Postgres is disabled. |
| `GET` | `/observability/llm-usage` | The top-level **Observability** view. *Every* LLM call is persisted to the **`llm_call`** table (agent, model, tokens, cost, timestamp) — including the pre-submission prefill/roster/tooling agents, which persist with a null `estimate_id` but the **wizard-run `session_id`**. This endpoint aggregates it **DB-side** (SUM / GROUP BY) into a grand total + per-model **and per-agent** breakdown + a per-estimate list (`{ enabled, total, by_estimate }`); each estimate carries its `created_at` and expands to a per-agent breakdown with **call timestamps**. (When the wizard commits, those pre-submission calls are reparented onto the estimate via their `session_id`, so they roll up per-estimate too; an abandoned wizard's calls stay in the grand total + by-agent only.) `enabled: false` / empty when Postgres is off. |
| `GET` | `/estimates/{id}` | Fetch the current envelope (status, pass1/pass2 estimates, clarifying questions, final). **Authoritative source of truth.** On in-memory cache miss it falls back to the persisted `envelope_json` (when Postgres is connected) so completed estimates redisplay after a restart / in a fresh session. |
| `DELETE` | `/estimates/{id}` | Delete an estimate — removes it from the in-memory registries and Postgres history (+ phase rows). Idempotent → `204`. |
| `GET` | `/estimates/{id}/stream` | **SSE** event stream — emits `status` / `questions` / `final` / `error` as the graph progresses. Best-effort, via a per-estimate fan-out broker with a replay buffer: late / reconnecting / multiple concurrent subscribers all receive the backlog (no event stealing). Closes after `final` or `error`. |
| `POST` | `/estimates/{id}/answers` | Submit Stage 4 answers and resume the graph into Pass 2. Body: `{ answers: { question_id: text }, skip_remaining?: bool }`. Returns 409 if status ≠ `awaiting_answers`. |
| `POST` | `/estimates/{id}/sow` | Generate an editable **Statement of Work** from a completed estimate. Body: `{ scenario: "ai_assisted" \| "manual_only" }`. Returns the resolved `SowDocument` (sections + unfilled `[PLACEHOLDERS]`) + generation `llm_usage`. `400` if the estimate isn't completed; `404` if unknown. |
| `POST` | `/estimates/{id}/sow/docx` | Render a (possibly edited) `SowDocument` to a downloadable `.docx` (`python-docx`). **No LLM.** Returns the file with a `Content-Disposition: attachment`. |
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
| `/estimate/draft/maturity` | 3. AI tooling & codebase | A **freeform AI-tooling description** text field (classified into per-phase tooling levels on submit via `POST /estimates/draft/classify-tooling`), a codebase-context selector (greenfield / brownfield small / large-unfamiliar / large-familiar), an **existing/proposed technology-stack** field (a sizing signal the twins read; lets the estimate reference the real stack), and a **"Phases to estimate"** picker (all six checked by default; sends `selected_phases` only when a subset is chosen). The old per-phase L0–L4 maturity sliders are gone. Team composition lives in Stage 2. |
| `/estimate/[id]/questions` | 4. Clarifying questions | Renders questions returned by Pass 1; POSTs answers to resume Pass 2. |
| `/estimate/[id]/review` | 5. Review | Organized into four tabs (`<Tabs>`) — **Cost breakdown**, **Timeline**, **AI assistance**, **Risk & uncertainty** — so it reads as focused views (only the active panel is mounted). Across them: per-phase bar chart, AI-vs-manual toggle, role-attributed cost table, graphical algorithm breakdown charts, a confidence meter, a **Monte Carlo "Confidence" section** (fan chart + "80% confident: X–Y h" + "P(AI saves time)"), a **team-scaling section** (coordination-overhead cost row + scaling-efficiency / sweet-spot readout via `lib/staffing.ts`), a **Timeline** (overlapping-phase **Gantt** with a milestone strip + a **PERT** critical-path/slack network + a Monte-Carlo finish-risk readout — P10–P90 weeks, P(finish ≤ target), per-phase criticality — all derived on the client in `lib/schedule.ts`), algorithm tooltips, an AI-assistance-savings section, and risks/assumptions in modals off the phase cards. Copy-as-markdown, and an **Export SOW** action (`<SowExportModal>`) that generates an editable Statement of Work and downloads it as `.docx` — see [Statement of Work export](#statement-of-work-export). |

The landing page at `/` lists historical estimates pulled from the backend and redisplays the review page for completed ones. A gear icon opens `/settings`, which edits the AI-reduction guardrail bands (`GET`/`PUT /admin/reduction-bands`), the team-scaling (Brooks's Law + diminishing-returns) coefficients (`GET`/`PUT /admin/staffing-coefficients`), and the default hourly **rate card** per role category × seniority (`GET`/`PUT /admin/default-rates`).

### WBS flow (bottom-up)

A second, separate wizard under `frontend/app/wbs/` (reachable via "WBS Estimate" on the landing page):

| Route | Step | Notes |
|---|---|---|
| `/wbs` | Landing + resume | "New WBS estimate" plus a **"Resume a draft"** list (`GET /wbs/drafts`); each row links to the editor and offers Duplicate / Delete. Notes when resume needs Neo4j. |
| `/wbs/new` | 1. Describe | Project description + codebase-context picker + freeform AI-tooling field (with a prefill helper). |
| `/wbs/team` | 2. Team | Transition page that prefills the **roster** (roster agent) and classifies the **AI tooling** from the description before drafting; classification is awaited at submit so the per-phase tooling — hence the AI-savings — is never baked in as all-`none`. Drafting **streams live progress** over AG-UI (`lib/wbs-agui.ts` → `POST /wbs/draft/agui`): a `<ProgressBar>` shows a single rolling status line narrating each work package + task as the planner produces it (no fake percentage), falling back to the plain `POST /wbs/draft` if streaming fails. |
| `/wbs/edit/[draftId]` | 3. Edit & review | The interactive tree editor (`<WbsTreeViewEditor>`), a **Contingency reserve %** input (default 30%), debounced autosave (`PUT /wbs/drafts/{id}`), a **Re-evaluate** button (`POST /estimates/wbs/preview`, live total + duration), a **Reconcile** button (`POST /estimates/wbs/reconcile` — cross-checks the bottom-up total against the parametric/twin model to flag omitted or double-counted work before commit) with an **Apply calibration** action that rescales the diverging phases' task hours toward the parametric (per-phase, clamped — keeps the full task breakdown, only the magnitudes move; fixes bottom-up's systematic under-sizing), a **Check completeness** button (`POST /estimates/wbs/completeness` — an LLM critic that lists project-specific tasks the WBS likely *omitted*, each addable as a leaf in one click), a per-task **✨ Suggest hours** button in each leaf's edit modal (`POST /estimates/wbs/suggest-hours` — re-estimates that one leaf's 3-point hours from the brief + its sibling tasks, so the number stays proportionate), and **Submit** (`POST /estimates/wbs` → redirect to the shared `/estimate/{id}/review`; the planner-draft LLM cost rides along to the Observability page). |

The shared review page (`/estimate/[id]/review`) renders a WBS estimate from the same `DualScenarioEstimate`, hiding the twin-only algorithm badges/breakdown charts and adding a read-only WBS-tree panel; it offers "Duplicate as new draft".

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

The frontend Stage 2 page hosts the `<RoleRosterEditor>` component — add/remove rows, dropdowns for category and seniority, an hourly-rate input, and percentage inputs with a separate **"Auto-adjust to 100%"** button (percentages are not auto-rebalanced on blur). A roster proposal agent (over AG-UI) can pre-populate the whole roster from the project context.

---

## Persistence and observability

- **LangGraph checkpointer** — `db/neo4j_adapter.py::make_checkpointer()` returns `langgraph.checkpoint.memory.InMemorySaver` in MVP. State survives within a process (so `interrupt()` works) but **not** across restarts. A real Neo4j-backed `BaseCheckpointSaver` is a Phase-3 swap at this exact call site.
- **Neo4j estimate snapshots** — `save_estimate_envelope(...)` writes one `Estimate` node + N `Phase` nodes via idempotent Cypher `MERGE`. Called at status transitions via `runtime.py::persist_completed_estimate` (shared by the twin flow and the WBS commit). **Silently no-ops** when Neo4j is unavailable.
- **Neo4j WBS drafts** — the bottom-up flow stores its hierarchy graph-natively (`save_wbs_draft` / `load_wbs_draft` / `list_wbs_drafts` / `delete_wbs_draft` / `save_wbs_tree`): a `(:WbsDraft)` node with its `[:HAS_CHILD]` `(:WbsTask)` subgraph, written in a single managed transaction so resume never sees a half-saved tree. The committed estimate hangs the same subgraph under its `(:Estimate)` node. Same never-raise contract — when Neo4j is off, drafts degrade to the client's localStorage cache.
- **Postgres history + calibration** — `save_estimate_history(...)` upserts the envelope into `estimate_history` (including the full `envelope_json` for verbatim redisplay) and replaces its rows in `phase_history` on every status transition (Pass 1 phases get superseded by Pass 2 in place). On status `completed`, `refresh_calibration_for_phase(...)` recomputes the rolling per-(phase, industry, project_type, **codebase-context**) aggregates in `calibration_aggregates`. The codebase-context code (0–3, `-1` = "any") rides in the column historically named `maturity_level` — it no longer holds an AI-maturity level. Twins read these aggregates during Pass 1 via `parse_input → state["calibration_examples"]` so the LLM has historical anchors for its UCP / FP / SLOC → hours mapping. `list_estimate_history(...)` / `get_estimate_envelope(...)` back the history list and the redisplay-after-restart fallback. Per-call LLM usage is persisted to the relational **`llm_call`** table (`save_llm_calls`; the pre-submission prefill/roster/tooling agents persist their calls with a null `estimate_id` but the wizard-run `session_id` via `insert_llm_calls`, and `associate_llm_calls` reparents them onto the estimate on commit) and aggregated DB-side for the Observability page (`aggregate_llm_usage`). **Silently no-ops** when Postgres is unavailable. Alembic migrations (`0001`–`0016`) run on startup when `POSTGRES_MIGRATE_ON_START=true` (default).
- **AI-reduction bands** — the admin-tunable `ai_reduction_bands` table holds the per-(phase, tooling) guardrail bands, merged with the in-code defaults and loaded into graph state by `parse_input`. Editable from the `/settings` screen via `GET`/`PUT /admin/reduction-bands`.
- **Staffing coefficients** — the admin-tunable `staffing_coefficients` table holds the team-scaling parameters (Brooks's Law coordination + diminishing returns), merged with the in-code `DEFAULT_STAFFING_COEFFS` fallback. Read/written by `get_staffing_coefficients` / `upsert_staffing_coefficients` (never-raise) and editable from the `/settings` screen via `GET`/`PUT /admin/staffing-coefficients`.
- **Rate card + app settings** — the `default_rates` (+ `custom_rate_roles`) table holds the per-`(category, seniority)` hourly **rate card** the roster agent seeds new estimates from (`pricing.DEFAULT_RATES` fallback), and the generic `app_settings` key→value table holds the string-valued admin settings — the three per-twin **sizing methods** (`{discovery,development,qa}_sizing_method`) and the global **contingency** reserve %. All flow through the same code-default + DB-override pattern and are edited from `/settings`.
- **LLM usage/cost** — `orchestrator/usage.py` captures each Anthropic call's token usage into a per-estimate accumulator (bound around the Pass 1/Pass 2 run), then summarizes it into `DualScenarioEstimate.llm_usage` (per-model token + dollar breakdown) — the meta-cost of producing the estimate. Best-effort: a no-op when no accumulator is bound.
- **Observability page** — the LLM token cost of *producing* estimates is surfaced on an in-app **Observability** page at `/observability` (frontend `app/observability/page.tsx`, an icon next to the Settings gear in `app/layout.tsx`), backed by `GET /observability/llm-usage` (`backend/routers/observability.py`). It aggregates the `llm_call` table **DB-side** (SUM / GROUP BY): a grand total + per-model + per-agent breakdown, plus a per-estimate rollup that expands to per-agent rows with call timestamps. Pre-submission + WBS-assist calls (prefill / roster / tooling / reconcile / completeness / suggest-hours) are captured against a wizard `session_id` and **reparented** onto the estimate when the wizard commits (an abandoned wizard's calls stay in the grand total + by-agent only). `enabled: false` / empty when Postgres is off.
- **Backend logging** — structured request logging via `observability/request_logging.py` (a pure-ASGI middleware that's streaming/SSE-safe) plus per-request correlation ids (`observability/correlation.py`), configured by `observability/logging_config.py` (`LOG_LEVEL`).
- **docs-mcp-server** — a co-located compose service (host port `6280`) the tooling classifier queries (and optionally scrapes-then-indexes) to research unfamiliar AI tools. Degrades gracefully: when unreachable or timed out, unknown tools stay `none`.
- **Qdrant** — vector-similarity calibration store. On completion, `orchestrator/calibration_index.py` embeds each estimate (OpenAI `EMBEDDING_MODEL`) into four collections — `reference_cases`, `phase_cases`, `wbs_tasks`, `clarifying_questions` — as a third best-effort task **alongside** the Neo4j + Postgres writes (additive, never replacing them). Retrieval (`nearest_reference_cases` / `nearest_wbs_tasks` / `nearest_phase_cases` / `similar_questions`) powers reference-class lookups; the Postgres SQL aggregates remain the exact-bucket calibration and Qdrant complements them. Degrades silently without an OpenAI key or Qdrant. **First consumer:** the per-leaf "Suggest hours" estimator retrieves similar past tasks (`nearest_wbs_tasks`) and feeds their realized hours into the prompt as calibration anchors. The other retrieval functions + the twins aren't consumed yet, and true *accuracy* calibration awaits delivered actuals.

---

## Testing

Backend (~750 tests, pytest with asyncio auto-mode):

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
- All six twin algorithms (UCP, SCP, COCOMO II, Fagan, CMP, TPA + 3-plan QA), with **admin-switchable sizing methods** for Discovery (UCP / FP-analysis), Development (COCOMO II / FP / COSMIC FP), and QA (TPA / Test Case Point / Capers-Jones defect-removal)
- **Selectable SDLC phases** — estimate any subset of the six phases (`selected_phases`); the unselected twins are skipped end-to-end and the rollup covers only the chosen phases
- A second, **bottom-up WBS estimation flow** (LLM-drafted task tree → user edits → deterministic Monte-Carlo rollup through the same cost/staffing tail), with resumable Neo4j-native drafts, Duplicate, a WBS-specific 30% contingency, and shared review + history
- **Statement of Work (SOW) export** — a config-driven (YAML template), editable `.docx` generated from any completed estimate, with deterministic client-reference + vendor-generalization guards and a dependency-injected company name
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
- In-app **Observability** page aggregating the LLM token cost of producing estimates (per-model / per-agent / per-estimate, DB-side over the `llm_call` table)
- Dockerized full stack

**Deferred (Phase 2 / 3 / 4 — scaffolded, not implemented)**

- A2A peer-to-peer cross-phase signaling between twins
- Server-side / OCR document parsing (the MVP extracts text **client-side** for PDF / Word / text; scanned image-only PDFs aren't OCR'd)
- Full Stage 2 / 3 field set per planning outline §4.2
- Qdrant vector-similarity calibration — indexing + retrieval is built and its **first consumer is live** (the per-leaf "Suggest hours" estimator anchors to similar past tasks via `nearest_wbs_tasks`); the other retrieval functions + the twins aren't consumed yet, and it lacks delivered actuals for true accuracy calibration (the Postgres SQL aggregates remain the live exact-bucket version)
- Neo4j-backed LangGraph checkpointer (in-memory only today)
- Side-by-side estimate comparison views (history list + single-estimate redisplay exist; multi-estimate diffing does not)
- Proposal document export / PM-tool integration

Each deferred area has either a corresponding TODO comment or a scaffolded folder pointing to the relevant planning-outline section.

---

## Troubleshooting

- **Neo4j fails to start with `JettyWebServer.loadStaticContent: Path is null`** — newer 5.x community images regress on arm64. The image is pinned to `neo4j:5.20-community` for that reason. Don't bump without testing on arm64.
- **Neo4j "no space left on device"** — the Docker VM's virtual disk is full. The compose file uses **bind mounts** under `./data/neo4j/{data,logs}` so the host (which has space) holds the data. If you also see Docker layer build failures, prune: `docker image prune -af && docker builder prune -f`.
- **Tooling classifier maps unknown tools to `none`** — the docs-mcp-server is unreachable, timed out (`DOCS_MCP_RESEARCH_TIMEOUT_S` / `DOCS_MCP_SCRAPE_TIMEOUT_S`), or has no embeddings provider (`OPENAI_API_KEY`) to search/index against. This is the conservative fallback; the rest of the estimate proceeds. Confirm the `docs-mcp-server` service is healthy on `:6280` and `DOCS_MCP_URL` is reachable from the backend.
- **Backend says "Neo4j connect failed; persistence disabled"** — `NEO4J_PASSWORD` not set or Neo4j is down. The backend keeps working without persistence.
- **Backend says "Postgres disabled (no POSTGRES_DSN / POSTGRES_PASSWORD)"** — expected when neither is set. History writes + twin calibration silently no-op; the rest of the API works. Set `POSTGRES_PASSWORD` (or `POSTGRES_DSN`) to enable.
- **Backend says "Alembic upgrade failed"** — the lifespan logs but doesn't crash. Run `uv run alembic upgrade head` from `backend/` to apply migrations manually and inspect the error.
- **Twins not improving across runs** — calibration only refreshes when an estimate reaches status `completed`. Check `calibration_aggregates` in Postgres (`psql -U estimator -d estimator -c "select * from calibration_aggregates"`) to see what's accumulated. Note `maturity_level` there is a codebase-context code (0–3, `-1` = any), not an AI-maturity level.
- **Twin returns a stub estimate** — the twin's LLM call failed (often: `ANTHROPIC_API_KEY` missing or model id wrong). Check the low confidence + stub note in the response. Set the env var and restart.
- **Next.js build fails on `useSearchParams()`** — wrap the page component in `<Suspense>`. `/estimate/new` and `/estimate/draft/create` already do this; copy the pattern.
- **Frontend can't reach the backend in Docker** — `NEXT_PUBLIC_API_URL` is build-time and is called from the browser. It must be `http://localhost:8000`, never the internal service name.

---

## License

Internal — not yet licensed.
