# Implementation Plan — Selectable SDLC Phases (Quick-Estimate / twin flow)

**Goal.** Let a user choose a *subset* of the six SDLC phases (`discovery`, `ux_design`,
`development`, `code_review`, `deployment`, `qa_testing`) to estimate, instead of always
running all six twins. Omitted phases contribute nothing to hours, cost, headcount, timeline,
or the review page.

**Status of the codebase (why this is small).** Three of the four things you'd expect to build
already exist:

1. **The shared rollup tail is already phase-subset-safe.** `synthesize_from_phase_estimates`,
   `commercial_processing.compute_total_costs`, `_combine_range`, the persistence layer
   (`phase_history`, Neo4j `UNWIND $phases`), and the data model
   (`DualScenarioEstimate.phases: list[PhaseEstimate]` — no fixed length, no validator) all
   iterate "whatever phases are present." The **WBS flow already exercises this in production**:
   `orchestrator/wbs/rollup.py` emits a `PhaseEstimate` only `for phase in Phase if grouped.get(phase)`.
2. **The review page is already phase-agnostic** — it `.map()`s over `final_estimate.phases`
   and resolves labels via `PHASE_LABELS[p.phase]`. Renders 2 phases or 6 unchanged.
3. **No LangGraph topology change is needed.** All twin boilerplate is centralized in
   `_twin_base.py::make_twin_nodes`, so a single guard there covers all six twins.

So the work is: a request field, threading it into graph state, **one guard in the twin
scaffold**, a few cross-phase-sanity guards, and a frontend picker.

---

## Contract & semantics (decisions)

- **Field:** top-level `CreateEstimateRequest.selected_phases: list[Phase] | None`. `None` (or
  omitted) ⇒ **all six** (back-compat with every existing caller, the smoke test, and the eval
  harness). A non-empty list ⇒ run exactly those phases.
- **Wire format:** JSON array of phase string values, e.g. `["development", "qa_testing"]`.
  Pydantic coerces to `list[Phase]`.
- **Validation:** if provided, must be non-empty and deduplicated; unknown values 422 via the
  enum. (We do **not** force a minimum of 2 — a single-phase estimate is valid.)
- **Deselected phases are omitted, not zeroed.** The estimate covers only the chosen phases —
  same model the WBS flow already uses. No "0-hour placeholder rows."
- **WBS flow is unaffected** — its tree's phases *are* the selection; it never goes through
  `create_estimate` or the twin graph.

---

## Backend changes

### B1. Request model + validation — `models/project_schema.py`

`CreateEstimateRequest` is `ConfigDict(extra="forbid")`, so the field must be declared.

```python
from models.twin_outputs import Phase  # already importable

class CreateEstimateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_name: str | None = None
    raw_input: str = Field(min_length=10, max_length=20000, ...)
    stage2: Stage2Context | None = None
    stage3: Stage3Context | None = None
    # None / omitted ⇒ estimate all six phases (back-compat). A non-empty list runs exactly
    # those twins; the others are skipped and contribute nothing.
    selected_phases: list[Phase] | None = None

    @model_validator(mode="after")
    def _normalize_phases(self) -> "CreateEstimateRequest":
        if self.selected_phases is not None:
            deduped = list(dict.fromkeys(self.selected_phases))  # order-preserving dedup
            if not deduped:
                raise ValueError("selected_phases, when provided, must be non-empty")
            self.selected_phases = deduped
        return self
```

### B2. State field — `models/estimation_state.py`

Single-writer (set once at graph entry; **no reducer**). Survives the `await_user_answers`
interrupt via the checkpointer, so Pass-2 twins see it too.

```python
class EstimationState(TypedDict, total=False):
    ...
    # Phases the user chose to estimate. Absent/empty ⇒ all six (back-compat). Read by the
    # twin scaffold to skip unselected twins, and by consistency_check to gate cross-phase checks.
    selected_phases: list[Phase]
```

### B3. Thread into the initial state — `routers/estimates.py::create_estimate`

```python
initial_state: dict[str, Any] = {
    "estimate_id": estimate_id,
    "project_name": env.project_name,
    "raw_input": req.raw_input,
    "stage2": req.stage2,
    "stage3": req.stage3,
    "parsed_context": {},
}
if req.selected_phases:                       # omit the key entirely when None → guard treats as "all"
    initial_state["selected_phases"] = req.selected_phases
```

No `parse_input` change required — the field rides in the initial state and is visible to every
node. (Optionally persist it on `EstimateEnvelope` for redisplay/audit; not required for MVP.)

### B4. The guard — `orchestrator/nodes/_twin_base.py::make_twin_nodes` (the core change)

The `pass1`/`pass2` node functions already close over `phase` and receive `state`. Add one
helper + a one-line guard in each. A skipped twin returns `{}` — a clean no-op on the
`operator.add` reducer — **and never makes an LLM call** (the guard precedes `_run`). The graph
topology, the fan-out edges, and the implicit join at `merge_pass*` are all untouched (the node
still "runs," it just returns nothing).

```python
def _phase_selected(state: EstimationState, phase: Phase) -> bool:
    """Absent/empty selected_phases ⇒ every phase runs (back-compat)."""
    selected = state.get("selected_phases")
    return not selected or phase in selected

@traced(name=f"{trace_name}.p1")
async def pass1(state: EstimationState) -> dict:
    if not _phase_selected(state, phase):
        return {}
    return {"pass1_estimates": [await _run(state, pass_num=1)]}

@traced(name=f"{trace_name}.p2")
async def pass2(state: EstimationState) -> dict:
    if not _phase_selected(state, phase):
        return {}
    return {"pass2_estimates": [await _run(state, pass_num=2)]}
```

### B5. Cross-phase sanity guards — `orchestrator/nodes/consistency_check.py`

These checks are *advisory only* (they never change the numbers — they append strings to
`consistency_warnings`). But with a partial lifecycle they'd emit misleading warnings, so gate
them on their inputs being present:

- `_capers_jones_qa_ratio_warning`: the "QA should be 30–40% of total" heuristic only makes
  sense for a roughly full lifecycle. Skip it unless `QA_TESTING` **and** at least one
  development-side phase are present (e.g. `return None` when `Phase.QA_TESTING not in present`
  or `len(present) < 3`). It already guards `total <= 0`, so there is **no** divide-by-zero —
  this is purely about suppressing a spurious advisory.
- `_dev_sloc_screen_consistency_warning`: already returns `None` when there's no development
  estimate; confirm and keep that early-out (it reads dev's `ksloc`).

### B6. Calibration refresh (minor) — `runtime.py` (~L475)

`refresh_calibration_for_phase` is currently called `for phase in Phase`. For a subset estimate
this is wasteful but **not incorrect** (phases this estimate didn't touch just recompute their
aggregates from other history, unchanged). Optional tidy-up: iterate only the phases present —
`{p.phase for p in (env.pass2_estimates or env.pass1_estimates)}`.

> **Review note:** the loop's comment (`runtime.py:473-474`) says iterating the full enum is
> *intentional* (forward-safety for a future 7th twin). Narrowing it trades that off for a tiny
> efficiency win. **Recommendation: leave B6 as-is** unless profiling shows the extra refreshes
> matter — it's correct either way. Lowest priority; arguably drop from scope.

### Explicitly **not** changed (don't manufacture work)

- `synthesize_estimate._combine_range` / staffing / Brooks / contingency — already combine N
  phases correctly (RSS of present-phase variances); WBS proves it with subsets.
- `commercial_processing.compute_total_costs` — summing only the selected phases is the
  *correct* cost, not an under-count.
- `calibration.get_calibration_for_all_phases` (the `strict=True` zip) — it is always fed the
  full six-phase tuple as *reference data* fed to twin prompts; the user's selection doesn't
  change its inputs, so it is never triggered. Leave it.

---

## Frontend changes

### F1. Phase picker — Stage 3 (`app/estimate/draft/maturity/`)

Stage 3 already collects codebase context + AI-tooling free text + technology stack — a 6-item
checkbox group ("Phases to estimate", all checked by default) belongs here. Use the existing
`PHASE_LABELS` (`lib/types.ts`) for labels/order. Disable "submit" if zero are checked.

### F2. Schema + store + payload (exact change points, verified)

- `lib/api-client.ts` — add `selected_phases?: Phase[]` to the **`CreateEstimateInput`** interface
  (the body type for `createEstimate(body: CreateEstimateInput)`, which already JSON-sends the
  whole body — no per-field plumbing in the request call itself).
- `lib/api-client.ts::buildCreatePayload` — add a `selectedPhases` arg and return
  `selected_phases: allSelected ? undefined : chosen` (send `undefined` when all six are chosen
  so the back-compat "omitted ⇒ all" path runs and existing snapshots are byte-unaffected).
- `lib/schemas.ts` — add `selected_phases: z.array(phaseEnum).optional()` to the Stage-3 slice
  for UI/store convenience (defaults to all six in the component); `lib/wizard-store.ts` needs
  no structural change (it serializes the slice as-is).

### F3. No change — **verified against the code**

- Review page (`app/estimate/[id]/review/page.tsx`): every render path is driven by the returned
  set — `fe.phases.map(...)` (per-phase cards, L168), `fe.phases.reduce(...)` (totals/risks),
  modal `fe.phases[openPhase]`, and the child viz components all take `fe.phases` as a prop:
  `<PhaseBar>`, `<AlgorithmBreakdownChart>`, `<TornadoChart>`, `<RiskRegister>`. All use
  `PHASE_LABELS[p.phase]` lookups and `width="100%"` responsive charts — **no** `grid-cols-6`,
  `slice(0,6)`, `length === 6`, lifecycle stepper, or hardcoded phase order. A 2-phase estimate
  renders smaller, not broken. The `consistency_warnings` panel just shows whatever the backend
  emits (so B5 — suppressing spurious partial-lifecycle warnings — is the right fix locus).
- WBS wizard/editor — phase selection is already per-task; untouched.

---

## Tests

**Backend**
- `tests/test_graph.py` (or a new `tests/test_phase_selection.py`): run the graph (stub/no-API
  path) with `selected_phases=[development, qa_testing]` ⇒ `final_estimate.phases` has exactly
  those two; total hours = Σ of just those; `ai.most_likely == manual.most_likely × (1−eff)`
  invariant still holds per present phase.
- Back-compat: omitting `selected_phases` ⇒ all six phases present (existing assertions).
- `test_twin_base`: `_phase_selected` truth table (None ⇒ all; subset ⇒ membership).
- `consistency_check`: with QA deselected, the Capers-Jones warning is suppressed (no spurious
  string); with a full set it still fires as before.
- Request validation: empty `selected_phases: []` ⇒ 422; unknown value ⇒ 422; dupes deduped.
- **Dormant-role row** (from the cross-phase review): select phases that exclude a roster
  role's "home" phase (e.g. a UX designer with `ux_design` deselected) ⇒ that role aggregates
  to **0 hours / 0 headcount / $0** and the headcount table still renders. Locks the
  "omitted phases contribute nothing" behavior end-to-end (`_sum_hours_by_role` +
  `_distribute_team` only staff roles with `hours > 0`).

**Frontend**
- A `lib/` vitest: `buildCreatePayload` sends `undefined` when all six selected, the chosen
  subset otherwise; store round-trips the field.

---

## Edge cases & follow-ups

- **Single phase** is allowed; the MC variance-combine degenerates to that one phase's
  distribution (correct).
- **Clarifying questions** from deselected phases never appear (those twins don't run) — no
  extra work.
- **Eval harness** (`evals/synthetic.py`) hardcodes 6 cases/project; not broken, but won't
  exercise subsets. Optional follow-up: a subset eval case. Out of scope for MVP.
- **Persisting the selection** on the envelope for audit/redisplay is a nice-to-have, not
  required (the persisted `phases` list already reflects what ran).

---

## Effort & sequencing

| Step | Scope | Est. |
|---|---|---|
| B1–B4 | Request field + state + guard (the functional core) | ~0.5 day |
| B5–B6 | Consistency-check guards + calibration tidy-up + validation | ~0.5 day |
| F1–F2 | Stage 3 picker + schema/store/payload threading | ~1 day |
| Tests | Backend subset/back-compat + a frontend test | ~0.5 day |
| Polish | Edge cases, manual e2e (twins + a 2-phase run), docs | ~0.5 day |

**Total ≈ 2.5–3 engineering days**, low risk. A minimal "guard + checkbox + default-all"
version is ~1 day if the consistency-guard polish and extra tests are deferred.

## Verification commands

```bash
cd backend && uv run pytest tests/test_phase_selection.py tests/test_graph.py tests/test_twin_base.py -q
cd backend && uv run ruff check . && uv run mypy .
cd frontend && npm test && npm run type-check && npm run lint
# manual: POST /estimates with {"raw_input": "...", "selected_phases": ["development","qa_testing"]}
#         → GET /estimates/{id} → final_estimate.phases == those two
```

---

## Design review outcomes

Three independent architecture reviews (orchestration/state, cross-phase correctness, frontend/
API/product) verified the plan against the real code. **Verdict: sound — proceed.** No BLOCKERs;
no number-distorting case found. Key confirmations and the few refinements (already folded in
above):

**Orchestration & state — sound.**
- Returning `{}` from a guarded fan-out node is a true no-op: LangGraph's `BinaryOperatorAggregate`
  early-returns on empty, so the `operator.add` channel for `pass*_estimates` is untouched. The
  node still executes, so the **unconditional** static join at `merge_pass*` still fires — no
  topology change, no superstep hazard.
- `selected_phases` set once at entry survives the `interrupt()`/resume cycle (LangGraph preserves
  checkpoint channel values for `Command(resume=...)`); genuinely single-writer, **no reducer**.
  `Phase` (str-Enum, defined in `twin_outputs`) round-trips the checkpointer serde — fine now and
  for the planned Neo4j checkpointer.
- `_twin_base` is the single chokepoint (all six twins use `make_twin_nodes`); the guard precedes
  `_run`, so a deselected dev twin also skips its Pass-2 `ensemble_k=5` fan-out. Top-level request
  field is the correct boundary (nesting under `stage3` would leak the flag into twin prompts +
  calibration rows). **Conditional edges / Send API are NOT worth it** — the guard is minimal-correct.

**Cross-phase correctness — sound (numbers, not just warnings).**
- `_combine_range`, Brooks/`optimal_team_size`, contingency, and `compute_total_costs` are all
  strictly additive/scaling over *present* phases — no `/6`, `len==6`, or `NUM_PHASES` anywhere.
  A smaller scope → honestly smaller team/cost/duration, which is correct, not an under-count.
- **The WBS flow genuinely proves it down to one phase**: `test_wbs_rollup.py::test_build_wbs_estimate_empty_phase_grouping`
  runs a single dev leaf through the *same* `compute_total_costs` → `synthesize_from_phase_estimates`
  tail and asserts `phases == [DEVELOPMENT]` **with** `headcount_by_role` populated — i.e. the full
  staffing/Brooks/contingency chain already runs green on a subset.
- Single-phase degenerate paths (`_lognormal_band`, `_combine_std`, `optimal_team_size`,
  no-target schedule) are all pre-guarded against div-by-zero/NaN.
- `consistency_check` is advisory-only — `consistency_warnings` is surfaced verbatim and never
  read into any number. The Capers-Jones gate (B5) is **real work** (it currently fires a spurious
  "QA share 0%" on partial lifecycles), correctly scoped as cosmetic.

**Frontend / API / product — sound, review page zero-change confirmed (evidence in F3).**
- Open product question (not a blocker): the product now has **two phase-selection models** —
  Quick-Estimate's global picker vs WBS's per-task phase. Acceptable (the flows are top-down vs
  bottom-up), but frame the picker as an optional **"scope"** affordance (all six checked by
  default) so users don't feel forced to choose. A roster role whose home phase is deselected
  shows as a dormant 0-hour row — covered by the new test above; consider a soft hint, not pruning.
