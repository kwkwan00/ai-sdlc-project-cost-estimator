"""Vector-similarity calibration: index completed estimates into Qdrant and retrieve nearest cases.

This is the orchestration seam between the estimate models, the embedder, and the Qdrant store. On
completion it offloads four derived views of an estimate into Qdrant (see ``db/qdrant_adapter``):

1. **reference_cases** — the whole estimate (brief + context → realized totals): reference-class
   forecasting ("how did projects like this one actually come out?").
2. **phase_cases** — one per phase (brief + phase → that phase's realized hours / AI reduction).
3. **wbs_tasks** — one per WBS leaf (task name+description → its 3-point hours): retrieval-augmented
   per-leaf estimation (the ``suggest-hours`` button) + archetype templates.
4. **clarifying_questions** — the questions the twins raised, for semantic recall/dedup.

It is **purely additive** — the Neo4j envelope snapshot and Postgres history are unchanged; this just
adds a vector index over the same facts. Every function is best-effort and never raises: when
embeddings or Qdrant are unavailable, indexing no-ops and the reads return ``[]``.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from db import qdrant_adapter as qa
from models.project_schema import EstimateEnvelope, Stage2Context, Stage3Context
from models.twin_outputs import PHASE_LABELS, Phase
from models.wbs_task import WbsTaskInput, iter_leaves
from orchestrator.embeddings import embed_texts

logger = logging.getLogger(__name__)

# Fixed namespace so a re-persist of the same estimate upserts (overwrites) rather than duplicating —
# Qdrant point ids must be uint or UUID, so we hash the natural string keys into deterministic uuid5s.
_NS = uuid.UUID("b9c1f3a2-7e44-4d6e-9a1b-2c5d8e0f1234")


def _pid(*parts: str) -> str:
    return str(uuid.uuid5(_NS, "|".join(parts)))


def _context_summary(stage2: Stage2Context | None, stage3: Stage3Context | None) -> str:
    """A compact natural-language context blurb that joins the brief in the embedding text."""
    bits: list[str] = []
    if stage2:
        if stage2.industry:
            bits.append(f"Industry: {stage2.industry}.")
        bits.append(f"Project type: {stage2.project_type.value}.")
        if stage2.regulatory_requirements:
            bits.append(f"Regulatory: {', '.join(stage2.regulatory_requirements)}.")
        if stage2.integration_list:
            bits.append(f"Integrations: {', '.join(stage2.integration_list)}.")
    if stage3:
        bits.append(f"Codebase: {stage3.codebase_context.value}.")
    return " ".join(bits)


def wbs_task_text(leaf: WbsTaskInput) -> str:
    """The embedding text for a WBS leaf task — shared by indexing AND the suggest-hours retrieval
    query, so the query and the stored documents live in the same embedding space."""
    label = PHASE_LABELS.get(leaf.phase, "") if leaf.phase else ""
    return f"{label}: {leaf.name}. {leaf.description}".strip(": ").strip()


def _tags(stage2: Stage2Context | None, stage3: Stage3Context | None) -> dict[str, str]:
    """The discrete payload tags both stored and usable as exact-match search filters."""
    return {
        "industry": (stage2.industry if stage2 else "") or "",
        "project_type": stage2.project_type.value if stage2 else "",
        "codebase_context": stage3.codebase_context.value if stage3 else "",
    }


def _phases_of(env: EstimateEnvelope) -> list[Any]:
    """The estimate's final per-phase results — twins carry them on pass2/pass1, WBS on final_estimate."""
    return list(
        env.pass2_estimates
        or env.pass1_estimates
        or (env.final_estimate.phases if env.final_estimate else [])
    )


def _build_items(
    env: EstimateEnvelope,
    *,
    raw_input: str,
    stage2: Stage2Context | None,
    stage3: Stage3Context | None,
    wbs_tree: list[WbsTaskInput] | None,
) -> list[tuple[str, str, str, dict[str, Any]]]:
    """Collect ``(collection, point_id, embed_text, payload)`` rows to index for one estimate."""
    eid = env.estimate_id
    context = _context_summary(stage2, stage3)
    tags = _tags(stage2, stage3)
    items: list[tuple[str, str, str, dict[str, Any]]] = []

    # 1. reference_cases — the whole estimate.
    fe = env.final_estimate
    ref_payload: dict[str, Any] = {
        "estimate_id": eid,
        "project_name": env.project_name,
        "method": env.method,
        **tags,
    }
    if fe is not None:
        ref_payload.update(
            {
                "total_manual_mid_hours": fe.total_manual_only_hours.most_likely,
                "total_ai_mid_hours": fe.total_ai_assisted_hours.most_likely,
                "total_cost_ai_usd": fe.total_cost_ai_assisted_usd,
                "duration_weeks_low": fe.duration_weeks_low,
                "duration_weeks_high": fe.duration_weeks_high,
                "team_size": fe.team_size,
                "confidence": fe.confidence,
            }
        )
    items.append((qa.REFERENCE_CASES, _pid("ref", eid), f"{raw_input}\n\n{context}", ref_payload))

    # 2. phase_cases — one per phase.
    for p in _phases_of(env):
        label = PHASE_LABELS.get(p.phase, p.phase.value)
        text = f"{label} phase. {context} {raw_input[:600]}".strip()
        items.append(
            (
                qa.PHASE_CASES,
                _pid("phase", eid, p.phase.value),
                text,
                {
                    "estimate_id": eid,
                    "phase": p.phase.value,
                    "manual_mid_hours": p.manual_only_hours.most_likely,
                    "ai_mid_hours": p.ai_assisted_hours.most_likely,
                    "ai_reduction_pct": p.effective_ai_reduction_pct,
                    "confidence": p.confidence,
                    **tags,
                },
            )
        )

    # 3. wbs_tasks — one per leaf task (WBS estimates only).
    for leaf in iter_leaves(wbs_tree or []):
        text = wbs_task_text(leaf)
        items.append(
            (
                qa.WBS_TASKS,
                _pid("task", eid, leaf.id),
                text,
                {
                    "estimate_id": eid,
                    "task_name": leaf.name,
                    "phase": leaf.phase.value if leaf.phase else "",
                    "role_id": leaf.role_id or "",
                    "optimistic": leaf.optimistic or 0.0,
                    "most_likely": leaf.most_likely or 0.0,
                    "pessimistic": leaf.pessimistic or 0.0,
                },
            )
        )

    # 4. clarifying_questions — the questions the twins raised.
    for q in env.clarifying_questions:
        items.append(
            (
                qa.CLARIFYING_QUESTIONS,
                _pid("q", eid, q.id),
                q.text,
                {
                    "estimate_id": eid,
                    "question": q.text,
                    "phase": q.source_phases[0].value if q.source_phases else "",
                    "impact_hours": q.impact_hours,
                    **tags,
                },
            )
        )
    return items


async def index_completed_estimate(
    env: EstimateEnvelope,
    *,
    raw_input: str = "",
    stage2: Stage2Context | None = None,
    stage3: Stage3Context | None = None,
    wbs_tree: list[WbsTaskInput] | None = None,
) -> None:
    """Offload a completed estimate's four derived views into Qdrant (best-effort, never raises).
    No-op when there's nothing to index or embeddings/Qdrant are unavailable."""
    items = _build_items(
        env, raw_input=raw_input, stage2=stage2, stage3=stage3, wbs_tree=wbs_tree
    )
    if not items:
        return
    vectors = await embed_texts([text for _, _, text, _ in items])
    if vectors is None:  # no OPENAI_API_KEY / embedding failure → skip Qdrant entirely
        logger.debug("estimate %s: embeddings unavailable, skipping Qdrant index", env.estimate_id)
        return

    by_collection: dict[str, list[dict[str, Any]]] = {}
    for (collection, pid, _text, payload), vector in zip(items, vectors, strict=True):
        by_collection.setdefault(collection, []).append(
            {"id": pid, "vector": vector, "payload": payload}
        )
    for collection, points in by_collection.items():
        await qa.upsert(collection, points)
    logger.info(
        "estimate %s: indexed %d vectors into Qdrant (%s)",
        env.estimate_id,
        len(items),
        ", ".join(f"{c}={len(p)}" for c, p in by_collection.items()),
    )


# --- retrieval (reference-class lookups) -----------------------------------------------------------


async def _query(collection: str, text: str, *, limit: int, must_match: dict[str, Any] | None) -> list[dict[str, Any]]:
    vectors = await embed_texts([text])
    if not vectors:  # no embeddings (no key / failure) → no neighbors
        return []
    hits = await qa.search(collection, vectors[0], limit=limit, must_match=must_match)
    return [h["payload"] | {"score": h["score"]} for h in hits]


async def nearest_reference_cases(
    query_text: str, *, limit: int = 5, industry: str | None = None, project_type: str | None = None
) -> list[dict[str, Any]]:
    """The completed estimates most similar to ``query_text`` (a project brief), optionally filtered to
    a matching industry / project_type. Each result is the stored payload + a cosine ``score``."""
    must = {k: v for k, v in {"industry": industry, "project_type": project_type}.items() if v}
    return await _query(qa.REFERENCE_CASES, query_text, limit=limit, must_match=must or None)


async def nearest_wbs_tasks(
    query_text: str, *, limit: int = 5, phase: Phase | str | None = None
) -> list[dict[str, Any]]:
    """Past WBS leaf tasks most similar to ``query_text`` (a task name/description), optionally scoped to
    one phase — powers retrieval-augmented per-leaf estimation."""
    phase_value = phase.value if isinstance(phase, Phase) else phase
    return await _query(
        qa.WBS_TASKS, query_text, limit=limit, must_match={"phase": phase_value} if phase_value else None
    )


async def nearest_phase_cases(
    query_text: str, *, phase: Phase | str, limit: int = 5
) -> list[dict[str, Any]]:
    """Past per-phase outcomes for the given phase most similar to ``query_text`` (a brief)."""
    phase_value = phase.value if isinstance(phase, Phase) else phase
    return await _query(qa.PHASE_CASES, query_text, limit=limit, must_match={"phase": phase_value})


async def similar_questions(query_text: str, *, limit: int = 5) -> list[dict[str, Any]]:
    """Clarifying questions from past estimates most similar to ``query_text``."""
    return await _query(qa.CLARIFYING_QUESTIONS, query_text, limit=limit, must_match=None)
