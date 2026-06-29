"""Qdrant vector-similarity calibration: indexing completed estimates + retrieving nearest cases.

Runs against an in-memory ``QdrantClient(':memory:')`` (real Qdrant local mode, no server) with a
deterministic stub embedder, so the index→search round-trip + payload filters + the never-raise
degrade paths are exercised without network or an OpenAI key.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest

from db import qdrant_adapter as qa
from models.project_schema import EstimateEnvelope, EstimateStatus, Stage2Context, Stage3Context
from models.twin_outputs import (
    ClarifyingQuestion,
    DualScenarioEstimate,
    HourRange,
    Phase,
    PhaseEstimate,
)
from models.wbs_task import WbsTaskInput
from orchestrator import calibration_index as ci
from orchestrator.embeddings import EMBED_DIMS

_CREATED = datetime(2026, 6, 28, tzinfo=UTC)


def _vec(text: str) -> list[float]:
    """A deterministic, non-zero unit-ish vector per text (so cosine is well-defined and texts are
    distinct). Good enough for round-trip + filter assertions; semantic ranking needs a real model."""
    h = int(hashlib.sha1(text.encode()).hexdigest(), 16)
    v = [0.0] * EMBED_DIMS
    v[h % EMBED_DIMS] = 1.0
    v[(h // 7) % EMBED_DIMS] += 0.5
    return v


async def _fake_embed(texts: list[str]) -> list[list[float]]:
    return [_vec(t) for t in texts]


@pytest.fixture
def qdrant(monkeypatch) -> None:
    from qdrant_client import QdrantClient

    qa._set_client_for_tests(QdrantClient(":memory:"))
    # Stub the embedder everywhere calibration_index uses it (index + read paths).
    monkeypatch.setattr(ci, "embed_texts", _fake_embed)
    yield
    qa._set_client_for_tests(None)


def _phase(phase: Phase, ai: float, manual: float) -> PhaseEstimate:
    return PhaseEstimate(
        phase=phase,
        twin_name=f"{phase.value}_twin",
        algorithm="X",
        ai_assisted_hours=HourRange(optimistic=ai * 0.8, most_likely=ai, pessimistic=ai * 1.3),
        manual_only_hours=HourRange(optimistic=manual * 0.8, most_likely=manual, pessimistic=manual * 1.3),
        confidence=0.7,
        effective_ai_reduction_pct=25.0,
    )


def _twin_env(estimate_id: str = "e-twin", questions: bool = False) -> EstimateEnvelope:
    phases = [_phase(Phase.DISCOVERY, 80, 100), _phase(Phase.DEVELOPMENT, 900, 1200)]
    final = DualScenarioEstimate(
        total_ai_assisted_hours=HourRange(optimistic=780, most_likely=980, pessimistic=1300),
        total_manual_only_hours=HourRange(optimistic=1040, most_likely=1300, pessimistic=1700),
        ai_hours_saved_pert=320.0,
        ai_cost_saved_usd=55000.0,
        phases=phases,
        confidence=0.7,
        duration_weeks_low=10,
        duration_weeks_high=14,
        total_cost_ai_assisted_usd=200_000.0,
        total_cost_manual_only_usd=255_000.0,
        team_size=6,
    )
    clarifying = (
        [ClarifyingQuestion(id="q1", text="Is PHI access audit-logged?", source_phases=[Phase.DEPLOYMENT],
                            suggested_default="yes", impact_hours=40.0)]
        if questions
        else []
    )
    return EstimateEnvelope(
        estimate_id=estimate_id,
        project_name="Patient portal",
        status=EstimateStatus.COMPLETED,
        method="twins",
        created_at=_CREATED,
        pass2_estimates=phases,
        final_estimate=final,
        clarifying_questions=clarifying,
    )


def _stage2() -> Stage2Context:
    return Stage2Context(industry="healthcare")


def _wbs_tree() -> list[WbsTaskInput]:
    return [
        WbsTaskInput(id="pkg", name="Auth", children=[
            WbsTaskInput(id="t1", name="Login API", description="OAuth login endpoint",
                         phase=Phase.DEVELOPMENT, role_id="r", optimistic=8, most_likely=16, pessimistic=32),
            WbsTaskInput(id="t2", name="Password reset", phase=Phase.DEVELOPMENT, role_id="r",
                         optimistic=4, most_likely=8, pessimistic=16),
        ]),
    ]


@pytest.mark.asyncio
async def test_index_and_retrieve_reference_case(qdrant) -> None:
    env = _twin_env()
    await ci.index_completed_estimate(
        env, raw_input="A HIPAA patient portal with HL7 integration.", stage2=_stage2(), stage3=Stage3Context()
    )
    hits = await ci.nearest_reference_cases("HIPAA healthcare portal", limit=3)
    assert hits, "the indexed estimate should be retrievable"
    top = hits[0]
    assert top["estimate_id"] == "e-twin"
    assert top["industry"] == "healthcare"
    assert top["total_manual_mid_hours"] == 1300.0
    assert "score" in top
    # Payload filter narrows to matching industry.
    assert await ci.nearest_reference_cases("x", industry="healthcare")
    assert await ci.nearest_reference_cases("x", industry="finance") == []


@pytest.mark.asyncio
async def test_index_phase_cases_and_questions(qdrant) -> None:
    env = _twin_env(questions=True)
    await ci.index_completed_estimate(env, raw_input="HIPAA portal", stage2=_stage2(), stage3=Stage3Context())

    dev = await ci.nearest_phase_cases("build the backend", phase=Phase.DEVELOPMENT, limit=5)
    assert dev and dev[0]["phase"] == "development"
    assert dev[0]["manual_mid_hours"] == 1200.0
    assert dev[0]["ai_reduction_pct"] == 25.0
    # Phase filter excludes other phases.
    assert all(h["phase"] == "development" for h in dev)

    qs = await ci.similar_questions("audit logging for protected health info", limit=3)
    assert qs and "audit-logged" in qs[0]["question"]


@pytest.mark.asyncio
async def test_index_wbs_tasks(qdrant) -> None:
    env = EstimateEnvelope(
        estimate_id="e-wbs", project_name="WBS proj", status=EstimateStatus.COMPLETED, method="wbs",
        created_at=_CREATED, final_estimate=_twin_env().final_estimate, wbs_tree=_wbs_tree(),
    )
    await ci.index_completed_estimate(env, raw_input="auth system", wbs_tree=_wbs_tree())

    tasks = await ci.nearest_wbs_tasks("login endpoint", phase=Phase.DEVELOPMENT, limit=5)
    names = {t["task_name"] for t in tasks}
    assert {"Login API", "Password reset"} <= names
    login = next(t for t in tasks if t["task_name"] == "Login API")
    assert (login["optimistic"], login["most_likely"], login["pessimistic"]) == (8.0, 16.0, 32.0)


@pytest.mark.asyncio
async def test_ensure_collections_creates_all(qdrant) -> None:
    assert await qa.ensure_collections() is True
    client = qa.get_client()
    existing = {c.name for c in client.get_collections().collections}
    assert set(qa.ALL_COLLECTIONS) <= existing


@pytest.mark.asyncio
async def test_never_raises_without_qdrant(monkeypatch) -> None:
    # Qdrant unavailable (get_client → None, no reconnect to a real server) → indexing no-ops and
    # reads return [] (never raises). Patching get_client also guarantees we never touch a live Qdrant.
    monkeypatch.setattr(qa, "get_client", lambda: None)
    monkeypatch.setattr(ci, "embed_texts", _fake_embed)
    await ci.index_completed_estimate(_twin_env(), raw_input="x")  # must not raise
    assert await ci.nearest_reference_cases("x") == []
    assert await ci.nearest_wbs_tasks("x") == []


@pytest.mark.asyncio
async def test_skips_when_embeddings_unavailable(qdrant, monkeypatch) -> None:
    # Embedder returns None (no OPENAI_API_KEY) → index no-ops; nothing lands in Qdrant.
    async def _no_embed(texts):
        return None

    monkeypatch.setattr(ci, "embed_texts", _no_embed)
    await ci.index_completed_estimate(_twin_env(), raw_input="x", stage2=_stage2())
    # The read path also degrades to [] when it can't embed the query.
    assert await ci.nearest_reference_cases("x") == []
