"""Assemble a resolved ``SowDocument`` from a completed estimate.

Walks the template in order, fills each section from its source (boilerplate / agent prose /
estimate renderer), then runs the **token-resolution pass**: substitute grounded values
(agent-extracted client facts + estimate-derived figures) into the ``[TOKENS]`` and report
the ones that remain literal so the UI can flag "fill these in Word".
"""

from __future__ import annotations

import logging
import re

from models.project_schema import EstimateEnvelope

from .agent import generate_prose
from .config import load_sow_template
from .mapper import RENDERERS, money, total_investment
from .models import (
    Scenario,
    SowClientFacts,
    SowDocument,
    SowSectionContent,
    SowSectionSpec,
    SowSignatory,
    SowTable,
    SowTemplate,
)
from .vendor_guard import generalize_vendor_tech

logger = logging.getLogger(__name__)


def _estimate_tokens(envelope: EstimateEnvelope, scenario: Scenario) -> dict[str, str]:
    """Values for the ``estimate.*`` placeholder fact_keys (deterministic from the envelope)."""
    final = envelope.final_estimate
    assert final is not None
    low, high = final.duration_weeks_low, final.duration_weeks_high
    duration = f"{low:.0f}–{high:.0f} weeks" if high > low else f"{high:.0f} weeks"
    return {
        "estimate.year": str(envelope.created_at.year),
        "estimate.total_investment": money(total_investment(envelope, scenario)),
        "estimate.duration": duration,
    }


def _resolve_tokens(
    template: SowTemplate,
    facts: SowClientFacts,
    envelope: EstimateEnvelope,
    scenario: Scenario,
) -> dict[str, str]:
    """token → concrete value, for every placeholder we can ground. Unground tokens omitted."""
    est = _estimate_tokens(envelope, scenario)
    # [COMPANY] is always resolved from the template's single branding.company source — it is
    # never a user-facing placeholder (so the company name is never hardcoded in code).
    resolved: dict[str, str] = {"[COMPANY]": template.branding.company}
    for ph in template.placeholders:
        if not ph.fact_key:
            continue
        if ph.fact_key.startswith("estimate."):
            value = est.get(ph.fact_key)
        else:
            value = getattr(facts, ph.fact_key, None)
        if value:
            resolved[ph.token] = str(value)
    return resolved


def _apply(text: str, resolved: dict[str, str]) -> str:
    for token, value in resolved.items():
        if token in text:
            text = text.replace(token, value)
    return text


# Force any "the Client" phrasing the agent might emit into the [CLIENT NAME] token, so the
# SOW only ever shows the real client name (when extracted) or the placeholder — never the
# generic "the Client". Matches "the Client"/"the client" only as a standalone client
# reference (not "the client-side ..."), preserving a trailing possessive/punctuation.
_CLIENT_REF = re.compile(r"\bthe [Cc]lient(?=['’\s.,;:)]|$)")


def _normalize_client_refs(text: str) -> str:
    return _CLIENT_REF.sub("[CLIENT NAME]", text)


# Deterministic vendor-name safety net — generalize specific vendor products / cloud services
# to their capability so the SOW never names a brand the user didn't ask for, even when one
# leaks in from the estimate's own notes. The brand→capability config lives in
# `vendor_generalizations.yaml` (assembled into regexes by `sow/vendor_guard.py`).


def _bullets_from_prose(text: str) -> list[str]:
    """Split an llm bullet section's prose into clean bullets (one per non-empty line)."""
    out: list[str] = []
    for line in text.splitlines():
        cleaned = line.strip().lstrip("-*•– ").strip()
        if cleaned:
            out.append(cleaned)
    return out


def _build_section(
    spec: SowSectionSpec,
    *,
    prose: dict[str, str],
    envelope: EstimateEnvelope,
    scenario: Scenario,
) -> SowSectionContent:
    """Resolve one template section into concrete content (pre token-substitution)."""
    editable = spec.style in ("paragraph", "bullets")
    content = SowSectionContent(id=spec.id, heading=spec.heading, kind=spec.style, editable=editable)

    if spec.source == "boilerplate":
        content.text = spec.text
        content.bullets = list(spec.bullets)
        if spec.table is not None:
            content.table = SowTable(columns=list(spec.table.columns), rows=[list(r) for r in spec.table.rows])
        content.signatories = [SowSignatory(party=s.party, fields=list(s.fields)) for s in spec.signatories]
    elif spec.source == "llm":
        raw = _normalize_client_refs(prose.get(spec.id, ""))
        if spec.style == "bullets":
            content.bullets = _bullets_from_prose(raw)
        else:
            content.text = raw.strip()
    elif spec.source in ("estimate", "hybrid"):
        rendered = RENDERERS[spec.renderer](envelope, scenario) if spec.renderer else None
        if spec.style == "table":
            content.table = rendered if isinstance(rendered, SowTable) else SowTable()
        elif spec.style == "bullets":
            est_bullets = list(rendered) if isinstance(rendered, list) else []
            # hybrid = static boilerplate bullets first, then the estimate-derived ones.
            content.bullets = list(spec.bullets) + est_bullets
        else:  # paragraph
            content.text = str(rendered or "")
    return content


def _substitute(content: SowSectionContent, resolved: dict[str, str]) -> None:
    """In-place token substitution across every text-bearing field of a section."""
    content.text = _apply(content.text, resolved)
    content.bullets = [_apply(b, resolved) for b in content.bullets]
    if content.table is not None:
        content.table.columns = [_apply(c, resolved) for c in content.table.columns]
        content.table.rows = [[_apply(c, resolved) for c in row] for row in content.table.rows]
    content.signatories = [
        SowSignatory(party=_apply(s.party, resolved), fields=s.fields) for s in content.signatories
    ]


def _remaining_placeholders(
    template: SowTemplate, sections: list[SowSectionContent]
) -> list[str]:
    """Template tokens still appearing literally after substitution (in template order)."""
    blob_parts: list[str] = []
    for s in sections:
        blob_parts.append(s.text)
        blob_parts.extend(s.bullets)
        if s.table is not None:
            blob_parts.extend(s.table.columns)
            for row in s.table.rows:
                blob_parts.extend(row)
        blob_parts.extend(sig.party for sig in s.signatories)
    blob = "\n".join(blob_parts)
    return [ph.token for ph in template.placeholders if ph.token in blob]


async def build_sow_document(
    envelope: EstimateEnvelope,
    scenario: Scenario = "ai_assisted",
    *,
    template_name: str | None = None,
) -> SowDocument:
    """Generate the full resolved SOW for a completed estimate. Never raises on LLM failure
    (the agent degrades to deterministic prose)."""
    template = load_sow_template(template_name) if template_name else load_sow_template()
    prose, facts = await generate_prose(template, envelope, scenario)
    resolved = _resolve_tokens(template, facts, envelope, scenario)

    sections: list[SowSectionContent] = []
    for spec in template.sections:
        content = _build_section(spec, prose=prose, envelope=envelope, scenario=scenario)
        # Fold the branding commitment into the signature block so its [YEAR] token resolves
        # in the same pass and it surfaces in the preview (rather than being renderer-only).
        if spec.style == "signature_block" and template.branding.commitment:
            content.text = f"{template.branding.commitment}\n\n{content.text}".strip()
        # Generalize any specific vendor product / cloud service to its capability (covers
        # agent prose AND estimate-derived assumptions sourced from twin notes).
        content.text = generalize_vendor_tech(content.text)
        content.bullets = [generalize_vendor_tech(b) for b in content.bullets]
        _substitute(content, resolved)
        sections.append(content)

    placeholders = _remaining_placeholders(template, sections)
    logger.info(
        "SOW document built (estimate=%s, scenario=%s, sections=%d, unresolved_tokens=%d)",
        envelope.estimate_id,
        scenario,
        len(sections),
        len(placeholders),
    )
    return SowDocument(
        estimate_id=envelope.estimate_id,
        template_id=template.id,
        title=template.title,
        project_name=envelope.project_name,
        scenario=scenario,
        sections=sections,
        placeholders=placeholders,
    )
