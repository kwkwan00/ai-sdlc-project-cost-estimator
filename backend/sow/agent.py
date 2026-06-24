"""The SOW "feature agent": one forced-tool-use call that writes the project-specific
prose sections and extracts client facts.

Mirrors the non-twin agent pattern (prefill / roster / tooling): ``load_prompt`` +
``call_structured`` against a Pydantic response model, pinned to its own model
(``anthropic_model_sow``), with a deterministic fallback so a SOW always generates — even
with no ``ANTHROPIC_API_KEY``.

The response model is built **dynamically from the template's ``llm`` sections** via
``pydantic.create_model`` (one prose field per section id) plus a nested ``SowClientFacts``
for extract-when-present client values — so adding/renaming an ``llm`` section in the YAML
needs no code change. The model is cached per template id so its tool schema (memoized in
``call_structured``) is reused across requests.
"""

from __future__ import annotations

import json
import logging

from pydantic import BaseModel, ConfigDict, Field, create_model

from config import get_settings
from models.project_schema import EstimateEnvelope
from orchestrator.llm import call_structured
from orchestrator.prompts import load_prompt

from .mapper import PHASE_LABELS, total_investment
from .models import Scenario, SowClientFacts, SowSectionSpec, SowTemplate

logger = logging.getLogger(__name__)


class _ProseBase(BaseModel):
    # extra="forbid" so a stray field triggers call_structured's one-shot corrective retry.
    model_config = ConfigDict(extra="forbid")


def _llm_sections(template: SowTemplate) -> list[SowSectionSpec]:
    return [s for s in template.sections if s.source == "llm"]


_RESPONSE_MODEL_CACHE: dict[str, type[BaseModel]] = {}


def build_response_model(template: SowTemplate) -> type[BaseModel]:
    """Dynamic forced-tool response model: one prose str field per llm section + client_facts.

    Cached by template id (templates are immutable singletons) so the same class object —
    and thus the same memoized tool schema — is reused across requests.
    """
    cached = _RESPONSE_MODEL_CACHE.get(template.id)
    if cached is not None:
        return cached
    fields: dict[str, object] = {}
    for section in _llm_sections(template):
        desc = (section.guidance or f"Prose for the '{section.id}' section.").strip()
        desc = f"{desc} Keep under {section.max_chars} characters."
        # No hard max_length: an over-long draft should be truncated downstream, not rejected.
        fields[section.id] = (str, Field(default="", description=desc))
    fields["client_facts"] = (
        SowClientFacts,
        Field(
            default_factory=SowClientFacts,
            description=(
                "Client-specific facts extracted from the context. Set a field ONLY when it "
                "is explicitly present; otherwise leave it null (it becomes a placeholder)."
            ),
        ),
    )
    model = create_model("SowProse", __base__=_ProseBase, **fields)  # type: ignore[call-overload]
    _RESPONSE_MODEL_CACHE[template.id] = model
    return model


def _estimate_context(envelope: EstimateEnvelope, scenario: Scenario) -> dict[str, object]:
    """Compact, prose-free view of the estimate for grounding the agent."""
    final = envelope.final_estimate
    assert final is not None
    phases = []
    for p in final.phases:
        hrs = p.ai_assisted_hours if scenario == "ai_assisted" else p.manual_only_hours
        phases.append(
            {
                "phase": PHASE_LABELS.get(p.phase.value, p.phase.value),
                "algorithm": p.algorithm,
                "estimated_hours": round(hrs.most_likely),
                "notes": p.notes,
                "assumptions": [a.text for a in p.assumptions],
                "risks": [r.description for r in p.risks],
            }
        )
    roster = [
        {
            "role": hc.role_description,
            "category": hc.category.value,
            "seniority": hc.seniority.value,
        }
        for hc in final.headcount_by_role
        if (hc.ai_assisted_hours if scenario == "ai_assisted" else hc.manual_only_hours) > 0
    ]
    return {
        "project_name": envelope.project_name,
        "scenario": scenario,
        "estimated_investment_usd": round(total_investment(envelope, scenario)),
        "duration_weeks": [final.duration_weeks_low, final.duration_weeks_high],
        "team_size": final.team_size,
        "phases": phases,
        "roster": roster,
    }


def _build_user_prompt(
    template: SowTemplate, envelope: EstimateEnvelope, scenario: Scenario
) -> str:
    sections = [
        {"id": s.id, "heading": s.heading, "style": s.style, "guidance": s.guidance}
        for s in _llm_sections(template)
    ]
    payload = {
        "estimate": _estimate_context(envelope, scenario),
        "sections_to_write": sections,
    }
    return (
        "Write the project-specific SOW prose grounded in this estimate, and extract "
        "client_facts only where explicitly present.\n\n```json\n"
        + json.dumps(payload, indent=2, default=str)
        + "\n```"
    )


async def run_sow_agent(
    template: SowTemplate, envelope: EstimateEnvelope, scenario: Scenario
) -> tuple[dict[str, str], SowClientFacts]:
    """One forced-tool-use call → (prose by section id, extracted client facts).

    Raises on LLM failure (no API key, network); the caller handles the fallback.
    """
    model = build_response_model(template)
    system = load_prompt("sow_generator")
    user = _build_user_prompt(template, envelope, scenario)
    logger.debug(
        "running SOW agent (model=%s, sections=%d)",
        get_settings().anthropic_model_sow,
        len(_llm_sections(template)),
    )
    result = await call_structured(
        system=system,
        user=user,
        response_model=model,
        tool_name="draft_sow",
        model=get_settings().anthropic_model_sow,
        max_tokens=8192,
    )
    prose = {s.id: str(getattr(result, s.id, "") or "") for s in _llm_sections(template)}
    facts = getattr(result, "client_facts", None) or SowClientFacts()
    return prose, facts


def _fill(template_str: str, *, name: str, phase_list: str, label: str = "") -> str:
    """Substitute the fallback-prose tokens. Uses ``replace`` (not ``str.format``) so a stray
    brace in the config can't crash, and the SOW ``[TOKENS]`` pass through untouched."""
    return (
        template_str.replace("{name}", name)
        .replace("{phase_list}", phase_list)
        .replace("{label}", label)
    )


def _fallback_prose(
    template: SowTemplate, envelope: EstimateEnvelope, scenario: Scenario
) -> tuple[dict[str, str], SowClientFacts]:
    """Deterministic, estimate-grounded prose for when the LLM is unavailable.

    The fallback text is **config-driven** — each llm section's ``fallback`` (and, for bullet
    sections, ``fallback_item``) lives in the template YAML. Always returns content for every
    llm section and **all client_facts null** (so every client token degrades to a placeholder,
    never a guessed value).
    """
    final = envelope.final_estimate
    assert final is not None
    name = envelope.project_name or "the project"
    phase_labels = [PHASE_LABELS.get(p.phase.value, p.phase.value) for p in final.phases]
    phase_list = ", ".join(phase_labels) if phase_labels else "the agreed delivery phases"

    prose: dict[str, str] = {}
    for s in _llm_sections(template):
        if s.style == "bullets" and s.fallback_item and phase_labels:
            prose[s.id] = "\n".join(
                _fill(s.fallback_item, name=name, phase_list=phase_list, label=label)
                for label in phase_labels
            )
        elif s.fallback:
            prose[s.id] = _fill(s.fallback, name=name, phase_list=phase_list)
        else:
            # No fallback configured for this section — last-resort grounded line.
            prose[s.id] = f"{name}: {s.heading}".strip(": ")
    return prose, SowClientFacts()


async def generate_prose(
    template: SowTemplate, envelope: EstimateEnvelope, scenario: Scenario
) -> tuple[dict[str, str], SowClientFacts]:
    """Top-level prose generation: the agent, degrading to the deterministic fallback.

    Never raises — a SOW always generates (no API key / LLM error → grounded stub prose +
    all-null client facts). Usage is recorded by ``call_structured`` when an accumulator is
    bound (the router binds one).
    """
    try:
        prose, facts = await run_sow_agent(template, envelope, scenario)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "SOW agent failed (%s); using deterministic fallback prose. "
            "Set ANTHROPIC_API_KEY for LLM-written prose.",
            exc,
        )
        return _fallback_prose(template, envelope, scenario)
    logger.info(
        "SOW prose generated (sections=%d, client_name=%s)",
        len(prose),
        "extracted" if facts.client_name else "placeholder",
    )
    return prose, facts
