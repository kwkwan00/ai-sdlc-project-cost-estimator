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
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from db import postgres_adapter
from db.orm_models import Base, CalibrationAggregate, PhaseHistory
from db.repositories import (
    get_calibration,
    get_calibration_for_all_phases,
    refresh_calibration_for_phase,
    save_estimate_history,
)
from models.project_schema import (
    EstimateEnvelope,
    EstimateStatus,
    ProjectType,
    Stage2Context,
    Stage3Maturity,
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


def _make_envelope(
    *,
    estimate_id: str = "11111111-2222-3333-4444-555555555555",
    status: EstimateStatus = EstimateStatus.COMPLETED,
    include_final: bool = True,
    phase_pairs: list[tuple[Phase, float, float]] | None = None,
) -> EstimateEnvelope:
    """Build a minimal EstimateEnvelope suitable for save_estimate_history."""
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
    return EstimateEnvelope(
        estimate_id=estimate_id,
        project_name="Test project",
        status=status,
        created_at=datetime.utcnow(),
        pass2_estimates=phases,
        final_estimate=final,
    )


# ---------- save_estimate_history ----------


@pytest.mark.asyncio
async def test_save_estimate_history_writes_envelope_and_phases(in_memory_db) -> None:
    env = _make_envelope()
    stage2 = Stage2Context(industry="fintech", project_type=ProjectType.GREENFIELD)
    stage3 = Stage3Maturity()

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
            assert p.maturity_level == 1  # Stage3Maturity default


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
async def test_save_estimate_history_noops_when_postgres_disabled() -> None:
    """When session_scope yields None, save_estimate_history must not raise."""
    postgres_adapter._reset_for_tests()
    # No engine has been installed; the adapter will try to build one and fail
    # gracefully because there's no DSN in test env.
    env = _make_envelope()
    # Should complete cleanly (no raise) even with no DB available.
    await save_estimate_history(env, stage2=None, stage3=None)


# ---------- refresh_calibration_for_phase ----------


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
        Stage3Maturity(discovery_maturity=3),
        Stage3Maturity(discovery_maturity=3),
        Stage3Maturity(discovery_maturity=2),
    ]
    for env, s2, s3 in zip(envs, stages2, stages3, strict=True):
        await save_estimate_history(env, stage2=s2, stage3=s3)

    written = await refresh_calibration_for_phase("discovery")
    # 1 "any" rollup + 2 per-dimension groupings (fintech/greenfield/3, healthcare/greenfield/2).
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
    # "Any" rollup spans all three samples.
    any_row = by_key[("", "", 0)]
    assert any_row.sample_count == 3
    assert any_row.avg_ai_assisted_mid == pytest.approx((100 + 150 + 300) / 3)
    assert any_row.avg_manual_only_mid == pytest.approx((200 + 200 + 400) / 3)
    # Fintech/greenfield/L3 averages over its two samples.
    fintech_row = by_key[("fintech", "greenfield", 3)]
    assert fintech_row.sample_count == 2
    assert fintech_row.avg_ai_assisted_mid == pytest.approx(125.0)
    # Reduction percentage: manual=200, ai=125 → 37.5%.
    assert fintech_row.avg_ai_reduction_pct == pytest.approx(37.5)


@pytest.mark.asyncio
async def test_refresh_calibration_returns_zero_when_disabled() -> None:
    """When Postgres isn't installed, refresh must no-op without crashing."""
    postgres_adapter._reset_for_tests()
    written = await refresh_calibration_for_phase("discovery")
    assert written == 0


# ---------- get_calibration ----------


@pytest.mark.asyncio
async def test_get_calibration_prefers_most_specific_match(in_memory_db) -> None:
    # Seed two estimates in the same fintech/greenfield/L3 bucket and one in a
    # different bucket so refresh_calibration produces multiple rows.
    for i in range(2):
        await save_estimate_history(
            _make_envelope(
                estimate_id=f"fg-{i}",
                phase_pairs=[(Phase.DEVELOPMENT, 100.0 + i * 10, 200.0)],
            ),
            stage2=Stage2Context(industry="fintech", project_type=ProjectType.GREENFIELD),
            stage3=Stage3Maturity(development_maturity=3),
        )
    await save_estimate_history(
        _make_envelope(
            estimate_id="hc-0",
            phase_pairs=[(Phase.DEVELOPMENT, 500.0, 600.0)],
        ),
        stage2=Stage2Context(industry="healthcare", project_type=ProjectType.ENHANCEMENT),
        stage3=Stage3Maturity(development_maturity=2),
    )
    await refresh_calibration_for_phase("development")

    # Ask for fintech / greenfield / L3 — the matching specific row should rank
    # ahead of the "any" rollup.
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
async def test_get_calibration_returns_empty_when_disabled() -> None:
    postgres_adapter._reset_for_tests()
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
        stage3=Stage3Maturity(discovery_maturity=3, development_maturity=3),
    )
    for phase in ("discovery", "development"):
        await refresh_calibration_for_phase(phase)

    by_phase = await get_calibration_for_all_phases(
        industry="fintech", project_type="greenfield", stage3=Stage3Maturity(discovery_maturity=3)
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
