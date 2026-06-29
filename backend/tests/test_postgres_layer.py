"""End-to-end coverage for the Postgres persistence layer.

Strategy: spin up an in-memory aiosqlite engine using the same ORM models, install
it on `db.postgres_adapter` via a fixture, and exercise the repositories like real
production code would. This avoids needing a live Postgres in CI while still
catching schema/query mistakes (the SQLAlchemy Core layer is the same).

Notes on portability:
- aiosqlite doesn't support `pool_size`/`max_overflow` — fixture creates the engine
  directly without those kwargs.
- The ORM uses portable types (String, Integer, Float, DateTime, ForeignKey,
  UniqueConstraint) so the SQLite engine reflects the same schema Alembic creates
  against Postgres.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from db import postgres_adapter
from db.orm_models import Base, CalibrationAggregate, PhaseHistory
from db.repositories import (
    count_estimate_history,
    delete_estimate_history,
    get_calibration,
    get_calibration_for_all_phases,
    get_default_rates,
    get_estimate_envelope,
    get_staffing_coefficients,
    list_estimate_history,
    refresh_calibration_for_phase,
    replace_rate_card,
    save_estimate_history,
    upsert_staffing_coefficients,
)
from models.project_schema import (
    CodebaseContext,
    EstimateEnvelope,
    EstimateStatus,
    ProjectType,
    Stage2Context,
    Stage3Context,
)
from models.twin_outputs import (
    DualScenarioEstimate,
    HourRange,
    Phase,
    PhaseEstimate,
    RoleCategory,
    RoleHours,
    RoleSeniority,
)

# ---------- fixtures ----------


@pytest_asyncio.fixture
async def in_memory_db() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Install an aiosqlite engine on postgres_adapter for the duration of one test."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    # Patch module-level cache on the adapter so session_scope() yields our sessions.
    postgres_adapter._reset_for_tests()
    postgres_adapter._engine = engine
    postgres_adapter._sessionmaker = maker
    postgres_adapter._init_attempted = True

    try:
        yield maker
    finally:
        await engine.dispose()
        postgres_adapter._reset_for_tests()


def _force_postgres_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the "Postgres disabled" path regardless of the host environment.

    `_reset_for_tests()` alone only clears the cached engine — the next repository
    call re-reads live settings and would actually connect if a real Postgres happens
    to be listening on localhost:5432 (so the disabled-path assertions would fail on a
    dev box / CI runner with Postgres up). Patching `get_sessionmaker` to return None
    makes `session_scope()` yield None deterministically, exercising the disabled
    branch every repository function guards with `if session is None`.
    """
    postgres_adapter._reset_for_tests()
    monkeypatch.setattr(postgres_adapter, "get_sessionmaker", lambda: None)


def _make_envelope(
    *,
    estimate_id: str = "11111111-2222-3333-4444-555555555555",
    status: EstimateStatus = EstimateStatus.COMPLETED,
    include_final: bool = True,
    phase_pairs: list[tuple[Phase, float, float]] | None = None,
    method: str = "twins",
) -> EstimateEnvelope:
    """Build a minimal EstimateEnvelope suitable for save_estimate_history. `method="wbs"` attaches a
    minimal tree so the envelope's method↔wbs_tree lockstep validator passes."""
    pairs = phase_pairs or [
        (Phase.DISCOVERY, 100.0, 150.0),
        (Phase.DEVELOPMENT, 1200.0, 1800.0),
    ]
    def _role_hours(total: float) -> list[RoleHours]:
        return [
            RoleHours(
                role_id="sr_engineer",
                role_description="Senior software engineer",
                category=RoleCategory.ENGINEERING,
                seniority=RoleSeniority.SENIOR,
                hours=total,
            )
        ]

    phases = [
        PhaseEstimate(
            phase=phase,
            twin_name=f"{phase.value}_twin",
            algorithm="X",
            ai_assisted_hours=HourRange(
                optimistic=ai * 0.8, most_likely=ai, pessimistic=ai * 1.3
            ),
            manual_only_hours=HourRange(
                optimistic=manual * 0.8, most_likely=manual, pessimistic=manual * 1.3
            ),
            ai_assisted_role_hours=_role_hours(ai),
            manual_only_role_hours=_role_hours(manual),
            confidence=0.7,
        )
        for phase, ai, manual in pairs
    ]
    final = (
        DualScenarioEstimate(
            total_ai_assisted_hours=HourRange(
                optimistic=sum(p.ai_assisted_hours.optimistic for p in phases),
                most_likely=sum(p.ai_assisted_hours.most_likely for p in phases),
                pessimistic=sum(p.ai_assisted_hours.pessimistic for p in phases),
            ),
            total_manual_only_hours=HourRange(
                optimistic=sum(p.manual_only_hours.optimistic for p in phases),
                most_likely=sum(p.manual_only_hours.most_likely for p in phases),
                pessimistic=sum(p.manual_only_hours.pessimistic for p in phases),
            ),
            ai_hours_saved_pert=300.0,
            ai_cost_saved_usd=55000.0,
            phases=phases,
            confidence=0.7,
            duration_weeks_low=10,
            duration_weeks_high=14,
            total_cost_ai_assisted_usd=200_000.0,
            total_cost_manual_only_usd=255_000.0,
        )
        if include_final
        else None
    )
    extra: dict = {}
    if method == "wbs":
        from models.wbs_task import WbsTaskInput

        extra["wbs_tree"] = [
            WbsTaskInput(
                id="l", name="l", phase=Phase.DEVELOPMENT, role_id="sr_engineer",
                optimistic=1, most_likely=2, pessimistic=3,
            )
        ]
    return EstimateEnvelope(
        estimate_id=estimate_id,
        project_name="Test project",
        status=status,
        method=method,
        created_at=datetime.utcnow(),
        pass2_estimates=phases,
        final_estimate=final,
        **extra,
    )


# ---------- save_estimate_history ----------


@pytest.mark.asyncio
async def test_save_estimate_history_writes_envelope_and_phases(in_memory_db) -> None:
    env = _make_envelope()
    stage2 = Stage2Context(industry="fintech", project_type=ProjectType.GREENFIELD)
    stage3 = Stage3Context()

    await save_estimate_history(env, stage2=stage2, stage3=stage3)

    async with in_memory_db() as session:
        from db.orm_models import EstimateHistory

        row = await session.get(EstimateHistory, env.estimate_id)
        assert row is not None
        assert row.status == EstimateStatus.COMPLETED.value
        assert row.industry == "fintech"
        assert row.project_type == ProjectType.GREENFIELD.value
        # Final totals were populated.
        assert row.total_ai_assisted_mid_hours == pytest.approx(1300.0)
        assert row.total_manual_only_mid_hours == pytest.approx(1950.0)
        assert row.ai_cost_saved_usd == pytest.approx(55000.0)

        result = await session.execute(
            __import__("sqlalchemy").select(PhaseHistory).where(
                PhaseHistory.estimate_id == env.estimate_id
            )
        )
        phases = result.scalars().all()
        assert {p.phase for p in phases} == {"discovery", "development"}
        # Denormalized industry/project_type/maturity came through.
        for p in phases:
            assert p.industry == "fintech"
            assert p.project_type == "greenfield"
            assert p.maturity_level == 0  # Stage3Context default greenfield → codebase code 0


@pytest.mark.asyncio
async def test_save_estimate_history_preserves_metadata_when_context_absent(
    in_memory_db,
) -> None:
    """Pass-1 populates stage2-derived metadata; a later save with stage2=None (e.g. a
    Pass-2 FAILURE) must NOT null those columns — it preserves the stored values."""
    env = _make_envelope(status=EstimateStatus.AWAITING_ANSWERS)
    stage2 = Stage2Context(
        industry="fintech",
        project_type=ProjectType.GREENFIELD,
        target_timeline_weeks=12,
    )
    stage3 = Stage3Context()
    await save_estimate_history(env, stage2=stage2, stage3=stage3)

    # Now re-save with NO contexts (the Pass-2 failure path) and a new status.
    env.status = EstimateStatus.FAILED
    await save_estimate_history(env, stage2=None, stage3=None)

    async with in_memory_db() as session:
        from db.orm_models import EstimateHistory

        row = await session.get(EstimateHistory, env.estimate_id)
        assert row is not None
        # Status (always updated) advanced...
        assert row.status == EstimateStatus.FAILED.value
        # ...but the stage2-derived metadata from Pass 1 survived.
        assert row.industry == "fintech"
        assert row.project_type == ProjectType.GREENFIELD.value
        assert row.target_timeline_weeks == 12


@pytest.mark.asyncio
async def test_history_list_and_envelope_roundtrip(in_memory_db) -> None:
    env = _make_envelope()
    await save_estimate_history(env, stage2=None, stage3=None)

    # The list surfaces a summary row.
    items = await list_estimate_history()
    assert len(items) == 1
    item = items[0]
    assert item["estimate_id"] == env.estimate_id
    assert item["status"] == EstimateStatus.COMPLETED.value
    assert item["total_ai_assisted_hours"] == pytest.approx(1300.0)
    assert item["method"] == "twins"
    assert item["created_at"] is not None

    # The stored envelope JSON round-trips back to a full EstimateEnvelope for redisplay.
    data = await get_estimate_envelope(env.estimate_id)
    assert data is not None
    restored = EstimateEnvelope.model_validate(data)
    assert restored.estimate_id == env.estimate_id
    assert restored.final_estimate is not None
    assert env.final_estimate is not None
    assert len(restored.final_estimate.phases) == len(env.final_estimate.phases)


@pytest.mark.asyncio
async def test_history_list_method_comes_from_column(in_memory_db) -> None:
    # The dashboard list reads the authoritative estimate_history.method column (0017), same as
    # Observability — not the envelope_json blob — so the two pages can't disagree on the flow badge.
    await save_estimate_history(
        _make_envelope(estimate_id="wbs-list", method="wbs"), stage2=None, stage3=None
    )
    items = await list_estimate_history()
    assert {i["estimate_id"]: i["method"] for i in items}["wbs-list"] == "wbs"


@pytest.mark.asyncio
async def test_aggregate_llm_usage_from_call_rows(in_memory_db) -> None:
    # Backs the Observability page: the grand total + by_model + by_agent are SUM/GROUP BY over the
    # per-call `llm_call` table (DB-side), and the per-estimate breakdown is assembled from a
    # per-(estimate, agent) GROUP BY. Estimates with no calls don't appear.
    from db.repositories import aggregate_llm_usage, save_llm_calls

    for est_id in ("agg-1", "agg-2", "agg-3"):
        method = "wbs" if est_id == "agg-2" else "twins"
        await save_estimate_history(
            _make_envelope(estimate_id=est_id, method=method), stage2=None, stage3=None
        )

    await save_llm_calls("agg-1", [
        {"agent": "submit_cocomo_assessment", "model": "claude-sonnet-4-6", "input_tokens": 1000,
         "output_tokens": 400, "cache_read_tokens": 0, "cost_usd": 0.09, "called_at": "2026-06-26T12:00:00+00:00"},
        {"agent": "submit_cocomo_assessment", "model": "claude-sonnet-4-6", "input_tokens": 500,
         "output_tokens": 100, "cache_read_tokens": 0, "cost_usd": 0.03, "called_at": "2026-06-26T12:00:05+00:00"},
        {"agent": "propose_team_roster", "model": "claude-sonnet-4-6", "input_tokens": 200,
         "output_tokens": 50, "cache_read_tokens": 0, "cost_usd": 0.01, "called_at": "2026-06-26T11:59:00+00:00"},
    ])
    await save_llm_calls("agg-2", [
        {"agent": "propose_wbs", "model": "claude-sonnet-4-6", "input_tokens": 300,
         "output_tokens": 60, "cache_read_tokens": 0, "cost_usd": 0.02, "called_at": "2026-06-26T10:00:00+00:00"},
    ])
    # agg-3 has a history row but no calls → excluded from the breakdown.

    agg = await aggregate_llm_usage()
    total = agg["total"]
    assert total["call_count"] == 4
    assert total["input_tokens"] == 2000
    assert total["cost_usd"] == pytest.approx(0.15)
    by_agent = {a["agent"]: a for a in total["by_agent"]}
    assert set(by_agent) == {"submit_cocomo_assessment", "propose_team_roster", "propose_wbs"}
    assert by_agent["submit_cocomo_assessment"]["calls"] == 2  # GROUP BY agent folded its 2 calls
    assert [m["model"] for m in total["by_model"]] == ["claude-sonnet-4-6"]
    assert total["by_model"][0]["calls"] == 4

    by_est = {e["estimate_id"]: e for e in agg["by_estimate"]}
    assert set(by_est) == {"agg-1", "agg-2"}  # agg-3 (no calls) absent
    e1 = by_est["agg-1"]
    assert e1["method"] == "twins"
    assert e1["created_at"]  # the estimate's own timestamp
    e1_agents = {a["agent"]: a for a in e1["llm_usage"]["by_agent"]}
    assert e1_agents["submit_cocomo_assessment"]["calls"] == 2
    # The agent's call span (MIN/MAX of its call timestamps) is present + ordered.
    dev = e1_agents["submit_cocomo_assessment"]
    assert dev["started_at"] is not None and dev["finished_at"] is not None
    assert dev["started_at"] <= dev["finished_at"]
    # agg-2 was saved as a WBS estimate → flow read from the estimate_history.method column.
    assert by_est["agg-2"]["method"] == "wbs"


@pytest.mark.asyncio
async def test_by_estimate_method_comes_from_column_not_agent_inference(in_memory_db) -> None:
    # Regression: the flow label must come from estimate_history.method, NOT from whether a
    # `propose_wbs` agent row was captured. A WBS estimate whose planner usage wasn't persisted (only
    # a reparented roster call) must STILL show as "wbs", not fall back to "twins".
    from db.repositories import aggregate_llm_usage, save_llm_calls

    await save_estimate_history(
        _make_envelope(estimate_id="wbs-no-planner", method="wbs"), stage2=None, stage3=None
    )
    await save_llm_calls("wbs-no-planner", [
        {"agent": "propose_team_roster", "model": "claude-sonnet-4-6", "input_tokens": 200,
         "output_tokens": 50, "cache_read_tokens": 0, "cost_usd": 0.01, "called_at": "2026-06-26T11:00:00+00:00"},
    ])

    by_est = {e["estimate_id"]: e for e in (await aggregate_llm_usage())["by_estimate"]}
    assert by_est["wbs-no-planner"]["method"] == "wbs"  # no propose_wbs row, still labeled wbs


@pytest.mark.asyncio
async def test_by_estimate_folds_one_agent_across_two_models(in_memory_db) -> None:
    # _by_estimate groups by (estimate_id, agent, model); the per-estimate by_agent must fold those
    # back to ONE row per agent (else the Observability table renders duplicate React keys).
    from db.repositories import aggregate_llm_usage, save_llm_calls

    await save_estimate_history(_make_envelope(estimate_id="two-model"), stage2=None, stage3=None)
    await save_llm_calls("two-model", [
        {"agent": "submit_cocomo_assessment", "model": "claude-sonnet-4-6", "input_tokens": 100,
         "output_tokens": 40, "cache_read_tokens": 0, "cost_usd": 0.05, "called_at": "2026-06-26T12:00:00+00:00"},
        {"agent": "submit_cocomo_assessment", "model": "claude-opus-4-8", "input_tokens": 200,
         "output_tokens": 80, "cache_read_tokens": 0, "cost_usd": 0.20, "called_at": "2026-06-26T12:05:00+00:00"},
    ])

    by_est = {e["estimate_id"]: e for e in (await aggregate_llm_usage())["by_estimate"]}
    agents = by_est["two-model"]["llm_usage"]["by_agent"]
    rows = [a for a in agents if a["agent"] == "submit_cocomo_assessment"]
    assert len(rows) == 1  # folded, not two rows
    assert rows[0]["calls"] == 2
    assert rows[0]["cost_usd"] == pytest.approx(0.25)
    # Span widened across both calls.
    assert rows[0]["started_at"] <= rows[0]["finished_at"]


@pytest.mark.asyncio
async def test_aggregate_llm_usage_empty_when_postgres_off(monkeypatch) -> None:
    # No in_memory_db fixture → session_scope yields None → empty case, never raises.
    from db.repositories import aggregate_llm_usage

    postgres_adapter._reset_for_tests()
    # Force Postgres OFF regardless of the dev's root .env: _reset_for_tests only clears the cached
    # engine, so get_sessionmaker() would otherwise lazily rebuild a LIVE engine from POSTGRES_PASSWORD
    # and these "empty" assertions would hit real rows in a populated local/CI DB.
    monkeypatch.setattr(postgres_adapter, "get_sessionmaker", lambda: None)
    agg = await aggregate_llm_usage()
    assert agg["by_estimate"] == []
    assert agg["total"]["call_count"] == 0
    assert agg["total"]["by_model"] == []
    assert agg["total"]["by_agent"] == []


@pytest.mark.asyncio
async def test_save_llm_calls_replaces_rows_and_cascades(in_memory_db) -> None:
    # save_llm_calls is replace-on-save (like phase rows); a re-save supersedes prior rows.
    from db.repositories import aggregate_llm_usage, save_llm_calls

    await save_estimate_history(_make_envelope(estimate_id="rep-1"), stage2=None, stage3=None)
    await save_llm_calls("rep-1", [
        {"agent": "a", "model": "m", "input_tokens": 10, "output_tokens": 5,
         "cache_read_tokens": 0, "cost_usd": 0.01, "called_at": "2026-06-26T12:00:00+00:00"},
    ])
    await save_llm_calls("rep-1", [  # replaces the prior row
        {"agent": "b", "model": "m", "input_tokens": 20, "output_tokens": 8,
         "cache_read_tokens": 0, "cost_usd": 0.02, "called_at": "2026-06-26T12:01:00+00:00"},
    ])
    total = (await aggregate_llm_usage())["total"]
    assert total["call_count"] == 1
    assert {a["agent"] for a in total["by_agent"]} == {"b"}


@pytest.mark.asyncio
async def test_presubmission_calls_count_in_total_not_per_estimate(in_memory_db) -> None:
    # Pre-submission agent calls (roster/prefill/tooling) run before an estimate exists → persisted
    # with NO estimate id. They count toward the grand total + by_agent, but the per-estimate rollup
    # (which inner-joins on estimate_id) excludes them.
    from db.repositories import aggregate_llm_usage, insert_llm_calls, save_llm_calls

    await save_estimate_history(_make_envelope(estimate_id="est-1"), stage2=None, stage3=None)
    await save_llm_calls("est-1", [
        {"agent": "submit_cocomo_assessment", "model": "m", "input_tokens": 100, "output_tokens": 50,
         "cache_read_tokens": 0, "cost_usd": 0.05, "called_at": "2026-06-26T12:00:00+00:00"},
    ])
    await insert_llm_calls([  # a pre-submission roster call, no estimate id
        {"agent": "propose_team_roster", "model": "m", "input_tokens": 200, "output_tokens": 80,
         "cache_read_tokens": 0, "cost_usd": 0.03, "called_at": "2026-06-26T11:00:00+00:00"},
    ])

    agg = await aggregate_llm_usage()
    assert agg["total"]["call_count"] == 2  # both counted in the grand total
    assert "propose_team_roster" in {a["agent"] for a in agg["total"]["by_agent"]}
    # Per-estimate rollup has only est-1; the orphan roster call isn't tied to an estimate.
    by_est = {e["estimate_id"]: e for e in agg["by_estimate"]}
    assert set(by_est) == {"est-1"}
    assert "propose_team_roster" not in {
        a["agent"] for a in by_est["est-1"]["llm_usage"]["by_agent"]
    }


@pytest.mark.asyncio
async def test_associate_llm_calls_reparents_session_rows_to_estimate(in_memory_db) -> None:
    # A pre-submission call persisted with the wizard-run session id but no estimate id gets
    # reparented to the estimate once it exists, so it shows in that estimate's per-agent rollup.
    from db.repositories import (
        aggregate_llm_usage,
        associate_llm_calls,
        insert_llm_calls,
        save_llm_calls,
    )

    await save_estimate_history(_make_envelope(estimate_id="est-assoc"), stage2=None, stage3=None)
    await save_llm_calls("est-assoc", [
        {"agent": "submit_cocomo_assessment", "model": "m", "input_tokens": 100, "output_tokens": 50,
         "cache_read_tokens": 0, "cost_usd": 0.05, "called_at": "2026-06-26T12:00:00+00:00"},
    ])
    # Roster call made during the wizard run, before the estimate id existed.
    await insert_llm_calls(
        [{"agent": "propose_team_roster", "model": "m", "input_tokens": 200, "output_tokens": 80,
          "cache_read_tokens": 0, "cost_usd": 0.03, "called_at": "2026-06-26T11:00:00+00:00"}],
        session_id="wiz-9",
    )

    # Before association the roster call is an orphan — not under est-assoc.
    before = {e["estimate_id"]: e for e in (await aggregate_llm_usage())["by_estimate"]}
    assert "propose_team_roster" not in {
        a["agent"] for a in before["est-assoc"]["llm_usage"]["by_agent"]
    }

    await associate_llm_calls("wiz-9", "est-assoc")

    # After association the roster call rolls up under est-assoc (and the grand total is unchanged).
    agg = await aggregate_llm_usage()
    assert agg["total"]["call_count"] == 2
    after = {e["estimate_id"]: e for e in agg["by_estimate"]}
    assert "propose_team_roster" in {
        a["agent"] for a in after["est-assoc"]["llm_usage"]["by_agent"]
    }


@pytest.mark.asyncio
async def test_insert_llm_calls_appends_within_session_never_replaces(in_memory_db) -> None:
    # Re-running a session-scoped agentic action (Reconcile / Completeness clicked more than once)
    # APPENDS its calls so the cumulative cost is tracked — it must NOT replace the prior run's rows.
    # insert_llm_calls is pure-append (unlike save_llm_calls' replace-on-save), so repeated same-session
    # inserts accumulate, and reparenting on commit moves every accumulated row (drops/duplicates none).
    from db.repositories import aggregate_llm_usage, associate_llm_calls, insert_llm_calls

    await save_estimate_history(_make_envelope(estimate_id="est-cum"), stage2=None, stage3=None)

    def _reconcile_call(cost: float) -> list[dict]:
        return [{"agent": "development_architect", "model": "m", "input_tokens": 200,
                 "output_tokens": 80, "cache_read_tokens": 0, "cost_usd": cost,
                 "called_at": "2026-06-26T11:00:00+00:00"}]

    await insert_llm_calls(_reconcile_call(0.03), session_id="wiz-cum")   # first Reconcile run
    await insert_llm_calls(_reconcile_call(0.04), session_id="wiz-cum")   # second run, SAME session

    agg = await aggregate_llm_usage()
    assert agg["total"]["call_count"] == 2  # both runs counted (cumulative), not collapsed to 1
    assert agg["total"]["cost_usd"] == pytest.approx(0.07)  # total = sum of both runs

    # On commit the accumulated rows reparent onto the estimate — every row moves, none lost/duplicated.
    await associate_llm_calls("wiz-cum", "est-cum")
    agg2 = await aggregate_llm_usage()
    assert agg2["total"]["call_count"] == 2
    after = {e["estimate_id"]: e for e in agg2["by_estimate"]}
    assert "development_architect" in {a["agent"] for a in after["est-cum"]["llm_usage"]["by_agent"]}


@pytest.mark.asyncio
async def test_associate_llm_calls_leaves_other_sessions_untouched(in_memory_db) -> None:
    # associate_llm_calls only claims rows matching the session id (and only unparented ones) —
    # a different wizard run's orphan call stays an orphan.
    from db.repositories import (
        aggregate_llm_usage,
        associate_llm_calls,
        insert_llm_calls,
    )

    await save_estimate_history(_make_envelope(estimate_id="est-mine"), stage2=None, stage3=None)
    await insert_llm_calls(
        [{"agent": "propose_team_roster", "model": "m", "input_tokens": 10, "output_tokens": 4,
          "cache_read_tokens": 0, "cost_usd": 0.01, "called_at": "2026-06-26T11:00:00+00:00"}],
        session_id="mine",
    )
    await insert_llm_calls(
        [{"agent": "classify_ai_tooling", "model": "m", "input_tokens": 10, "output_tokens": 4,
          "cache_read_tokens": 0, "cost_usd": 0.01, "called_at": "2026-06-26T11:05:00+00:00"}],
        session_id="other",
    )

    await associate_llm_calls("mine", "est-mine")

    by_est = {e["estimate_id"]: e for e in (await aggregate_llm_usage())["by_estimate"]}
    agents = {a["agent"] for a in by_est["est-mine"]["llm_usage"]["by_agent"]}
    assert "propose_team_roster" in agents  # my session's call was claimed
    assert "classify_ai_tooling" not in agents  # the other session's call was not


@pytest.mark.asyncio
async def test_pass2_repersist_preserves_reparented_presubmission_rows(in_memory_db) -> None:
    # The two-pass twin flow persists twice (Pass 1 → AWAITING_ANSWERS → Pass 2). Pass 1 saves the
    # twin rows and reparents the wizard's pre-submission rows; Pass 2 re-persists. save_llm_calls'
    # delete is scoped to its own (session_id IS NULL) rows, so the reparented prefill/roster/tooling
    # rows MUST survive the Pass-2 re-persist (regression: a blanket delete-by-estimate_id wiped them).
    from db.repositories import (
        aggregate_llm_usage,
        associate_llm_calls,
        insert_llm_calls,
        save_llm_calls,
    )

    await save_estimate_history(_make_envelope(estimate_id="two-pass"), stage2=None, stage3=None)
    # Pre-submission roster call made during the wizard run (session id, no estimate yet).
    await insert_llm_calls(
        [{"agent": "propose_team_roster", "model": "sonnet", "input_tokens": 200, "output_tokens": 80,
          "cache_read_tokens": 0, "cost_usd": 0.03, "called_at": "2026-06-26T11:00:00+00:00"}],
        session_id="wiz-2p",
    )
    # Pass 1: save twin rows, then associate the wizard's pre-submission rows onto the estimate.
    await save_llm_calls("two-pass", [
        {"agent": "submit_cocomo_assessment", "model": "sonnet", "input_tokens": 100, "output_tokens": 50,
         "cache_read_tokens": 0, "cost_usd": 0.05, "called_at": "2026-06-26T12:00:00+00:00"},
    ])
    await associate_llm_calls("wiz-2p", "two-pass")
    # Pass 2: re-persist twin rows (supersedes Pass 1's), associate again (idempotent no-op).
    await save_llm_calls("two-pass", [
        {"agent": "submit_cocomo_assessment", "model": "sonnet", "input_tokens": 120, "output_tokens": 60,
         "cache_read_tokens": 0, "cost_usd": 0.06, "called_at": "2026-06-26T13:00:00+00:00"},
    ])
    await associate_llm_calls("wiz-2p", "two-pass")

    agg = await aggregate_llm_usage()
    by_est = {e["estimate_id"]: e for e in agg["by_estimate"]}
    by_agent = {a["agent"]: a for a in by_est["two-pass"]["llm_usage"]["by_agent"]}
    # The reparented roster call survived Pass 2, AND the twin row is the single Pass-2 version.
    assert set(by_agent) == {"submit_cocomo_assessment", "propose_team_roster"}
    assert by_agent["submit_cocomo_assessment"]["calls"] == 1  # Pass-1 twin row replaced, not duplicated
    # Grand total: 1 twin + 1 roster = 2 (no leftover Pass-1 twin row, no lost roster row).
    assert agg["total"]["call_count"] == 2


@pytest.mark.asyncio
async def test_delete_estimate_history_removes_llm_call_rows(in_memory_db) -> None:
    # delete_estimate_history must drop the estimate's llm_call rows too (explicit delete, since SQLite
    # doesn't enforce the FK cascade) — otherwise they orphan and keep inflating the grand total.
    from db.repositories import (
        aggregate_llm_usage,
        associate_llm_calls,
        delete_estimate_history,
        insert_llm_calls,
        save_llm_calls,
    )

    await save_estimate_history(_make_envelope(estimate_id="to-delete"), stage2=None, stage3=None)
    await save_llm_calls("to-delete", [
        {"agent": "submit_cocomo_assessment", "model": "sonnet", "input_tokens": 100, "output_tokens": 50,
         "cache_read_tokens": 0, "cost_usd": 0.05, "called_at": "2026-06-26T12:00:00+00:00"},
    ])
    await insert_llm_calls(
        [{"agent": "propose_team_roster", "model": "sonnet", "input_tokens": 200, "output_tokens": 80,
          "cache_read_tokens": 0, "cost_usd": 0.03, "called_at": "2026-06-26T11:00:00+00:00"}],
        session_id="wiz-del",
    )
    await associate_llm_calls("wiz-del", "to-delete")
    assert (await aggregate_llm_usage())["total"]["call_count"] == 2

    assert await delete_estimate_history("to-delete") is True

    # No orphan llm_call rows remain — the grand total is back to zero (both the twin row and the
    # reparented pre-submission row were removed).
    assert (await aggregate_llm_usage())["total"]["call_count"] == 0


@pytest.mark.asyncio
async def test_wbs_envelope_roundtrips_with_new_fields(in_memory_db) -> None:
    """A method='wbs' envelope (with wbs_tree + wbs_stage2/3) survives save → get verbatim, so a
    completed WBS estimate redisplays its tree and stays duplicable from envelope_json."""
    from models.wbs_task import WbsTaskInput

    env = _make_envelope(estimate_id="wbs-roundtrip")
    env.method = "wbs"
    env.wbs_tree = [
        WbsTaskInput(
            id="p1",
            name="Build",
            children=[
                WbsTaskInput(
                    id="l1", name="task", phase=Phase.DEVELOPMENT, role_id="sr_engineer",
                    optimistic=8, most_likely=16, pessimistic=32,
                )
            ],
        )
    ]
    await save_estimate_history(env, stage2=None, stage3=None)

    data = await get_estimate_envelope(env.estimate_id)
    assert data is not None
    restored = EstimateEnvelope.model_validate(data)
    assert restored.method == "wbs"
    assert restored.wbs_tree is not None
    assert restored.wbs_tree[0].children[0].role_id == "sr_engineer"
    assert restored.wbs_tree[0].children[0].phase == Phase.DEVELOPMENT


def test_envelope_rejects_incoherent_method_and_tree() -> None:
    """The model validator keeps method ↔ wbs_tree in lockstep: a 'wbs' envelope must carry a tree,
    and a 'twins' envelope must not (else the review page renders the WBS panel for a parametric
    estimate, or a WBS estimate redisplays with no tree)."""
    from models.wbs_task import WbsTaskInput

    leaf_tree = [WbsTaskInput(id="p1", name="Build", children=[
        WbsTaskInput(id="l1", name="t", phase=Phase.DEVELOPMENT, role_id="sr_engineer",
                     optimistic=8, most_likely=16, pessimistic=32)])]

    def _env(**kw: object) -> EstimateEnvelope:
        return EstimateEnvelope(
            estimate_id="x", project_name="p", status=EstimateStatus.COMPLETED,
            created_at=datetime.utcnow(), **kw,  # type: ignore[arg-type]
        )

    with pytest.raises(ValidationError):
        _env(method="wbs")  # wbs without a tree
    with pytest.raises(ValidationError):
        _env(method="twins", wbs_tree=leaf_tree)  # twins with a tree
    # The coherent combinations construct cleanly.
    assert _env(method="wbs", wbs_tree=leaf_tree).method == "wbs"
    assert _env().method == "twins"


@pytest.mark.asyncio
async def test_history_paging_and_count(in_memory_db) -> None:
    for i in range(5):
        await save_estimate_history(
            _make_envelope(estimate_id=f"e{i}"), stage2=None, stage3=None
        )

    assert await count_estimate_history() == 5

    first = await list_estimate_history(limit=2, offset=0)
    second = await list_estimate_history(limit=2, offset=2)
    third = await list_estimate_history(limit=2, offset=4)
    assert [len(first), len(second), len(third)] == [2, 2, 1]

    # Pages are disjoint and together cover every persisted row.
    ids = {r["estimate_id"] for page in (first, second, third) for r in page}
    assert ids == {f"e{i}" for i in range(5)}


@pytest.mark.asyncio
async def test_delete_estimate_history_removes_rows(in_memory_db) -> None:
    from sqlalchemy import select

    await save_estimate_history(
        _make_envelope(estimate_id="to-delete"), stage2=None, stage3=None
    )
    assert await count_estimate_history() == 1

    # Deleting an existing estimate removes it (and its phase rows) and reports True.
    assert await delete_estimate_history("to-delete") is True
    assert await count_estimate_history() == 0
    assert await list_estimate_history() == []
    assert await get_estimate_envelope("to-delete") is None
    async with in_memory_db() as session:
        result = await session.execute(
            select(PhaseHistory).where(PhaseHistory.estimate_id == "to-delete")
        )
        assert result.scalars().all() == []

    # Deleting a now-missing id is a no-op that reports False.
    assert await delete_estimate_history("to-delete") is False


@pytest.mark.asyncio
async def test_custom_rate_roles_add_update_delete(in_memory_db) -> None:
    # The rate card's custom roles support add / edit / delete via the atomic full set-replace
    # (replace_rate_card with grid=[] touches only the custom-role table).
    from db.repositories import CustomRoleRecord, get_custom_roles, replace_rate_card

    assert await get_custom_roles() == []
    ok = await replace_rate_card(
        [],
        [
            CustomRoleRecord("principal_architect", "Principal Architect", "engineering", "senior", 300.0),
            CustomRoleRecord("scrum_master", "Scrum Master", "product", "mid", 175.0),
        ],
    )
    assert ok is True
    got = await get_custom_roles()
    assert {r.role_id for r in got} == {"principal_architect", "scrum_master"}

    # Replace with a single edited row → the other is DELETED, the survivor is UPDATED.
    ok = await replace_rate_card(
        [], [CustomRoleRecord("principal_architect", "Principal Architect", "engineering", "senior", 320.0)]
    )
    assert ok is True
    got = await get_custom_roles()
    assert len(got) == 1
    assert got[0].role_id == "principal_architect"
    assert got[0].rate == 320.0  # updated in place

    # An explicit empty set clears all custom roles; None would leave them untouched.
    assert await replace_rate_card([], []) is True
    assert await get_custom_roles() == []


@pytest.mark.asyncio
async def test_replace_rate_card_writes_grid_and_custom_together(in_memory_db) -> None:
    # One call applies BOTH the grid override and the custom-role set (atomic transaction).
    from db.repositories import (
        CustomRoleRecord,
        get_custom_roles,
        get_default_rates,
        replace_rate_card,
    )
    from models.twin_outputs import RoleCategory as RC
    from models.twin_outputs import RoleSeniority as RS

    ok = await replace_rate_card(
        [(RC.ENGINEERING, RS.SENIOR, 275.0)],
        [CustomRoleRecord("scrum_master", "Scrum Master", "product", "mid", 175.0)],
    )
    assert ok is True
    assert (await get_default_rates())[(RC.ENGINEERING, RS.SENIOR)] == 275.0
    assert {r.role_id for r in await get_custom_roles()} == {"scrum_master"}

    # custom=None leaves the custom roles UNTOUCHED while still updating the grid.
    ok = await replace_rate_card([(RC.QA, RS.JUNIOR, 90.0)], None)
    assert ok is True
    assert (await get_default_rates())[(RC.QA, RS.JUNIOR)] == 90.0
    assert {r.role_id for r in await get_custom_roles()} == {"scrum_master"}  # not cleared


@pytest.mark.asyncio
async def test_get_custom_roles_skips_unrecognized_enum_rows(in_memory_db) -> None:
    # A row whose stored category/seniority is no longer a valid enum (rename / hand-edited row)
    # is dropped on read — mirroring get_default_rates — so the frontend never gets an out-of-enum
    # tag it can't render.
    from db.orm_models import CustomRateRole
    from db.repositories import get_custom_roles

    async with in_memory_db() as session:
        session.add(CustomRateRole(role_id="good", label="Good", category="engineering",
                                   seniority="senior", rate=200.0))
        session.add(CustomRateRole(role_id="bad", label="Bad", category="wizardry",
                                   seniority="senior", rate=200.0))
        await session.commit()

    got = await get_custom_roles()
    assert [r.role_id for r in got] == ["good"]  # the invalid-enum row is skipped


@pytest.mark.asyncio
async def test_custom_rate_roles_empty_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from db.repositories import CustomRoleRecord, get_custom_roles, replace_rate_card

    _force_postgres_disabled(monkeypatch)
    assert await get_custom_roles() == []
    # Write degrades to False (not persisted), never raises.
    assert await replace_rate_card([], [CustomRoleRecord("x", "X", "engineering", "senior", 100.0)]) is False


@pytest.mark.asyncio
async def test_history_helpers_empty_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_postgres_disabled(monkeypatch)
    assert await list_estimate_history() == []
    assert await count_estimate_history() == 0
    assert await delete_estimate_history("anything") is False
    assert await get_estimate_envelope("anything") is None


@pytest.mark.asyncio
async def test_staffing_coefficients_roundtrip(in_memory_db) -> None:
    # Empty table → no overrides (callers fall back to code defaults).
    assert await get_staffing_coefficients() == {}
    assert (
        await upsert_staffing_coefficients([("link_cost", 0.1), ("overhead_cap", 0.5)])
        is True
    )
    assert await get_staffing_coefficients() == {"link_cost": 0.1, "overhead_cap": 0.5}
    # Upsert updates an existing key in place.
    assert await upsert_staffing_coefficients([("link_cost", 0.2)]) is True
    assert (await get_staffing_coefficients())["link_cost"] == 0.2


@pytest.mark.asyncio
async def test_default_rates_roundtrip(in_memory_db) -> None:
    from models.twin_outputs import RoleCategory as RC
    from models.twin_outputs import RoleSeniority as RS

    # The grid is upserted via replace_rate_card (the production path); custom=None leaves the
    # custom-role table untouched.
    # Empty table → no overrides (callers fall back to pricing.DEFAULT_RATES per cell).
    assert await get_default_rates() == {}
    assert (
        await replace_rate_card([(RC.ENGINEERING, RS.SENIOR, 300.0), (RC.QA, RS.JUNIOR, 95.0)], None)
        is True
    )
    rates = await get_default_rates()
    assert rates[(RC.ENGINEERING, RS.SENIOR)] == 300.0
    assert rates[(RC.QA, RS.JUNIOR)] == 95.0
    # A subsequent edit updates an existing (category, seniority) cell in place.
    assert await replace_rate_card([(RC.ENGINEERING, RS.SENIOR, 320.0)], None) is True
    assert (await get_default_rates())[(RC.ENGINEERING, RS.SENIOR)] == 320.0


@pytest.mark.asyncio
async def test_staffing_coefficients_empty_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_postgres_disabled(monkeypatch)
    assert await get_staffing_coefficients() == {}
    assert await upsert_staffing_coefficients([("link_cost", 0.1)]) is False


@pytest.mark.asyncio
async def test_save_estimate_history_is_idempotent_on_id(in_memory_db) -> None:
    env_v1 = _make_envelope(status=EstimateStatus.PASS_1_RUNNING, include_final=False)
    await save_estimate_history(env_v1, stage2=None, stage3=None)

    # Second save with the same id supersedes phases and updates totals.
    env_v2 = _make_envelope(
        status=EstimateStatus.COMPLETED,
        phase_pairs=[(Phase.QA_TESTING, 500.0, 600.0)],
    )
    await save_estimate_history(env_v2, stage2=None, stage3=None)

    async with in_memory_db() as session:
        from db.orm_models import EstimateHistory

        row = await session.get(EstimateHistory, env_v2.estimate_id)
        assert row.status == EstimateStatus.COMPLETED.value

        result = await session.execute(
            __import__("sqlalchemy").select(PhaseHistory).where(
                PhaseHistory.estimate_id == env_v2.estimate_id
            )
        )
        phases = result.scalars().all()
        # Pass 1 phases were replaced by Pass 2's single QA row.
        assert len(phases) == 1
        assert phases[0].phase == "qa_testing"


@pytest.mark.asyncio
async def test_save_estimate_history_noops_when_postgres_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When session_scope yields None, save_estimate_history must not raise."""
    _force_postgres_disabled(monkeypatch)
    env = _make_envelope()
    # Should complete cleanly (no raise) when Postgres is disabled.
    await save_estimate_history(env, stage2=None, stage3=None)


# ---------- refresh_calibration_for_phase ----------


@pytest.mark.asyncio
async def test_save_estimate_history_degrades_on_query_error(in_memory_db) -> None:
    """A transient query error inside the body must be swallowed, not re-raised
    (never-raise persistence contract), AND must not leave a partial write.

    The EstimateHistory upsert happens before the phase-replace; without a rollback in
    the except, session_scope's clean-exit commit would persist the upsert without its
    phase rows. The rollback must discard the whole transaction.
    """
    from sqlalchemy import text

    # Drop the phase_history table so the wholesale phase replace raises *after* the
    # EstimateHistory row has already been added to the session.
    async with in_memory_db() as session:
        await session.execute(text("DROP TABLE phase_history"))
        await session.commit()

    env = _make_envelope()
    # Must complete cleanly despite the broken schema.
    await save_estimate_history(env, stage2=None, stage3=None)

    # No partial write: the rolled-back transaction must leave nothing committed.
    assert await get_estimate_envelope(env.estimate_id) is None
    assert await list_estimate_history() == []


@pytest.mark.asyncio
async def test_refresh_calibration_degrades_on_query_error(in_memory_db) -> None:
    """A query error during refresh must degrade to 0, not propagate."""
    from sqlalchemy import text

    async with in_memory_db() as session:
        await session.execute(text("DROP TABLE phase_history"))
        await session.commit()

    written = await refresh_calibration_for_phase("discovery")
    assert written == 0


@pytest.mark.asyncio
async def test_get_calibration_degrades_on_query_error(in_memory_db) -> None:
    """A query error during read must degrade to [], not propagate."""
    from sqlalchemy import text

    async with in_memory_db() as session:
        await session.execute(text("DROP TABLE calibration_aggregates"))
        await session.commit()

    rows = await get_calibration("development", industry="fintech")
    assert rows == []


@pytest.mark.asyncio
async def test_refresh_calibration_aggregates_by_dimension(in_memory_db) -> None:
    # Seed three completed estimates with discovery phases at varying dimensions.
    envs = [
        _make_envelope(
            estimate_id=f"id-{i}",
            phase_pairs=[(Phase.DISCOVERY, ai, manual)],
        )
        for i, (ai, manual) in enumerate([(100.0, 200.0), (150.0, 200.0), (300.0, 400.0)])
    ]
    stages2 = [
        Stage2Context(industry="fintech", project_type=ProjectType.GREENFIELD),
        Stage2Context(industry="fintech", project_type=ProjectType.GREENFIELD),
        Stage2Context(industry="healthcare", project_type=ProjectType.GREENFIELD),
    ]
    stages3 = [
        Stage3Context(codebase_context=CodebaseContext.BROWNFIELD_LARGE_FAMILIAR),  # code 3
        Stage3Context(codebase_context=CodebaseContext.BROWNFIELD_LARGE_FAMILIAR),  # code 3
        Stage3Context(codebase_context=CodebaseContext.BROWNFIELD_SMALL),  # code 1
    ]
    for env, s2, s3 in zip(envs, stages2, stages3, strict=True):
        await save_estimate_history(env, stage2=s2, stage3=s3)

    written = await refresh_calibration_for_phase("discovery")
    # 1 "any" rollup + 2 per-dimension groupings (fintech/greenfield/3, healthcare/greenfield/1).
    assert written == 3

    async with in_memory_db() as session:
        result = await session.execute(
            __import__("sqlalchemy").select(CalibrationAggregate).where(
                CalibrationAggregate.phase == "discovery"
            )
        )
        rows = result.scalars().all()

    by_key = {
        (r.industry, r.project_type, r.maturity_level): r for r in rows
    }
    # "Any" rollup spans all three samples (maturity_level=-1 sentinel).
    any_row = by_key[("", "", -1)]
    assert any_row.sample_count == 3
    assert any_row.avg_ai_assisted_mid == pytest.approx((100 + 150 + 300) / 3)
    assert any_row.avg_manual_only_mid == pytest.approx((200 + 200 + 400) / 3)
    # Fintech/greenfield/codebase-code-3 averages over its two samples.
    fintech_row = by_key[("fintech", "greenfield", 3)]
    assert fintech_row.sample_count == 2
    assert fintech_row.avg_ai_assisted_mid == pytest.approx(125.0)
    # Reduction percentage: manual=200, ai=125 → 37.5%.
    assert fintech_row.avg_ai_reduction_pct == pytest.approx(37.5)


@pytest.mark.asyncio
async def test_refresh_calibration_returns_zero_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When Postgres isn't installed, refresh must no-op without crashing."""
    _force_postgres_disabled(monkeypatch)
    written = await refresh_calibration_for_phase("discovery")
    assert written == 0


# ---------- get_calibration ----------


@pytest.mark.asyncio
async def test_get_calibration_prefers_most_specific_match(in_memory_db) -> None:
    # Seed two estimates in the same fintech/greenfield/codebase-code-3 bucket and one
    # in a different bucket so refresh_calibration produces multiple rows.
    for i in range(2):
        await save_estimate_history(
            _make_envelope(
                estimate_id=f"fg-{i}",
                phase_pairs=[(Phase.DEVELOPMENT, 100.0 + i * 10, 200.0)],
            ),
            stage2=Stage2Context(industry="fintech", project_type=ProjectType.GREENFIELD),
            stage3=Stage3Context(codebase_context=CodebaseContext.BROWNFIELD_LARGE_FAMILIAR),
        )
    await save_estimate_history(
        _make_envelope(
            estimate_id="hc-0",
            phase_pairs=[(Phase.DEVELOPMENT, 500.0, 600.0)],
        ),
        stage2=Stage2Context(industry="healthcare", project_type=ProjectType.ENHANCEMENT),
        stage3=Stage3Context(codebase_context=CodebaseContext.BROWNFIELD_SMALL),  # code 1
    )
    await refresh_calibration_for_phase("development")

    # Ask for fintech / greenfield / codebase-code-3 — the matching specific row should
    # rank ahead of the "any" rollup.
    rows = await get_calibration(
        "development",
        industry="fintech",
        project_type="greenfield",
        maturity=3,
    )
    assert rows, "expected at least one calibration row"
    assert rows[0]["industry"] == "fintech"
    assert rows[0]["project_type"] == "greenfield"
    assert rows[0]["maturity_level"] == 3


@pytest.mark.asyncio
async def test_get_calibration_returns_empty_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_postgres_disabled(monkeypatch)
    rows = await get_calibration("development", industry="fintech")
    assert rows == []


# ---------- get_calibration_for_all_phases ----------


@pytest.mark.asyncio
async def test_get_calibration_for_all_phases_returns_one_key_per_phase(in_memory_db) -> None:
    # Seed a single envelope spanning two phases.
    env = _make_envelope(
        phase_pairs=[(Phase.DISCOVERY, 100.0, 150.0), (Phase.DEVELOPMENT, 1000.0, 1500.0)]
    )
    await save_estimate_history(
        env,
        stage2=Stage2Context(industry="fintech", project_type=ProjectType.GREENFIELD),
        stage3=Stage3Context(codebase_context=CodebaseContext.BROWNFIELD_LARGE_FAMILIAR),
    )
    for phase in ("discovery", "development"):
        await refresh_calibration_for_phase(phase)

    by_phase = await get_calibration_for_all_phases(
        industry="fintech",
        project_type="greenfield",
        stage3=Stage3Context(codebase_context=CodebaseContext.BROWNFIELD_LARGE_FAMILIAR),
    )
    assert set(by_phase.keys()) == {
        "discovery",
        "ux_design",
        "development",
        "code_review",
        "deployment",
        "qa_testing",
    }
    # Phases without seeded history return empty lists.
    assert by_phase["ux_design"] == []
    assert by_phase["discovery"], "expected discovery calibration after seeding"


# ---------- reduction bands ----------


@pytest.mark.asyncio
async def test_get_reduction_bands_returns_nested_dict(in_memory_db) -> None:
    from db.orm_models import AiReductionBand
    from db.repositories import get_reduction_bands

    async with in_memory_db() as session:
        session.add_all(
            [
                AiReductionBand(
                    phase="development", tooling_level="agentic", min_reduction=0.12, max_reduction=0.22
                ),
                AiReductionBand(
                    phase="development", tooling_level="chat", min_reduction=0.08, max_reduction=0.16
                ),
                AiReductionBand(
                    phase="qa_testing", tooling_level="none", min_reduction=0.0, max_reduction=0.0
                ),
            ]
        )
        await session.commit()

    bands = await get_reduction_bands()
    assert bands["development"]["agentic"] == [0.12, 0.22]
    assert bands["development"]["chat"] == [0.08, 0.16]
    assert bands["qa_testing"]["none"] == [0.0, 0.0]


@pytest.mark.asyncio
async def test_get_reduction_bands_empty_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from db.repositories import get_reduction_bands

    _force_postgres_disabled(monkeypatch)
    assert await get_reduction_bands() == {}


@pytest.mark.asyncio
async def test_upsert_reduction_bands_inserts_then_updates(in_memory_db) -> None:
    from db.repositories import get_reduction_bands, upsert_reduction_bands

    # Insert.
    assert await upsert_reduction_bands(
        [("development", "agentic", 0.14, 0.24), ("ux_design", "chat", 0.02, 0.07)]
    )
    bands = await get_reduction_bands()
    assert bands["development"]["agentic"] == [0.14, 0.24]
    assert bands["ux_design"]["chat"] == [0.02, 0.07]

    # Update in place (same key, no duplicate row).
    assert await upsert_reduction_bands([("development", "agentic", 0.30, 0.45)])
    bands = await get_reduction_bands()
    assert bands["development"]["agentic"] == [0.30, 0.45]
    assert bands["ux_design"]["chat"] == [0.02, 0.07]  # untouched


@pytest.mark.asyncio
async def test_upsert_reduction_bands_returns_false_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from db.repositories import upsert_reduction_bands

    _force_postgres_disabled(monkeypatch)
    assert await upsert_reduction_bands([("development", "agentic", 0.1, 0.2)]) is False


@pytest.mark.asyncio
async def test_admin_effective_bands_merge_db_override(in_memory_db) -> None:
    from admin.reduction_bands_admin import get_effective_bands
    from db.repositories import upsert_reduction_bands

    await upsert_reduction_bands([("development", "agentic", 0.30, 0.45)])
    resp = await get_effective_bands()
    row = next(
        b for b in resp.bands
        if b.phase == "development" and b.tooling_level == "agentic"
    )
    assert (row.min_pct, row.max_pct) == (30.0, 45.0)  # the override, as percent
    assert row.is_override is True
    assert (row.default_min_pct, row.default_max_pct) == (45.0, 72.0)  # code default still shown
