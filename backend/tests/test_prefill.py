"""Coverage for the Stage 2 project-context normalization agent.

Splits into four concerns:
1. `_normalize_*` / `_coerce_project_type` — pure backstop helpers, no I/O.
2. `NormalizedProjectContext` — the agent's enum-constrained response model and
   its mode="before" backstop validators.
3. `_normalized_to_fields` — the normalized-output → Stage2Fields (roster-free) mapper.
4. `POST /estimates/draft/prefill` — HTTP route end-to-end, with the agent
   stubbed so the test runs without an API key.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from models.project_schema import EngagementModel, ProjectType
from prefill import (
    IndustryOption,
    NormalizedProjectContext,
    RegulatoryRequirement,
    _coerce_project_type,
    _normalize_industry,
    _normalize_regulatory,
    _normalized_to_fields,
)

# ---------- _normalize_regulatory ----------


@pytest.mark.parametrize(
    "raw,expected",
    [
        (["HIPAA"], ["HIPAA"]),
        (["hipaa"], ["HIPAA"]),
        (["soc 2"], ["SOC 2"]),
        (["SOC2"], ["SOC 2"]),
        (["soc-2"], ["SOC 2"]),
        (["PCI-DSS"], ["PCI-DSS"]),
        (["pci dss"], ["PCI-DSS"]),
        (["pci"], ["PCI-DSS"]),
        (["fed ramp"], ["FedRAMP"]),
        (["GDPR", "FERPA"], ["GDPR", "FERPA"]),
    ],
)
def test_normalize_regulatory_maps_known_variants_to_canonical(
    raw: list[str], expected: list[str]
) -> None:
    assert _normalize_regulatory(raw) == expected


def test_normalize_regulatory_drops_unknown_mentions() -> None:
    # "ISO 27001" isn't in the frontend's REGULATORY_OPTIONS list — drop it.
    assert _normalize_regulatory(["ISO 27001", "HIPAA", ""]) == ["HIPAA"]


def test_normalize_regulatory_dedupes_repeated_mentions() -> None:
    assert _normalize_regulatory(["HIPAA", "hipaa", "HIPAA"]) == ["HIPAA"]


# ---------- _normalize_industry ----------


@pytest.mark.parametrize(
    "hint,expected",
    [
        # Case-folded exact match against the canonical list.
        ("healthcare", "healthcare"),
        ("Healthcare", "healthcare"),
        ("  HEALTHCARE  ", "healthcare"),
        ("fintech", "fintech"),
        # Synonyms / adjacent phrasings.
        ("health care", "healthcare"),
        ("medical", "healthcare"),
        ("clinical", "healthcare"),
        ("pharma", "healthcare"),
        ("finance", "fintech"),
        ("banking", "fintech"),
        ("insurtech", "insurance"),
        ("e-commerce", "retail"),
        ("ecommerce", "retail"),
        ("edtech", "education"),
        ("public sector", "government"),
        ("telecommunications", "telecom"),
        # Word-level scan resolves descriptive phrases.
        ("regional clinic", "healthcare"),
        ("consumer banking app", "fintech"),
    ],
)
def test_normalize_industry_maps_to_canonical_option(hint: str, expected: str) -> None:
    assert _normalize_industry(hint) == expected


@pytest.mark.parametrize("hint", ["", "   ", "nonprofit advocacy group", "xyz"])
def test_normalize_industry_returns_empty_when_no_confident_match(hint: str) -> None:
    # Off-list hints must NOT pass through — they'd leave the <select> blank with
    # an unrenderable value. Returning "" keeps the placeholder for the user.
    assert _normalize_industry(hint) == ""


# ---------- _coerce_project_type ----------


@pytest.mark.parametrize(
    "hint,expected",
    [
        ("greenfield", ProjectType.GREENFIELD),
        ("Greenfield", ProjectType.GREENFIELD),
        ("integration", ProjectType.INTEGRATION),
        ("ai_ml_build", ProjectType.AI_ML_BUILD),
    ],
)
def test_coerce_project_type_accepts_known_values(
    hint: str, expected: ProjectType
) -> None:
    assert _coerce_project_type(hint) is expected


def test_coerce_project_type_falls_back_to_greenfield_for_unknown() -> None:
    assert _coerce_project_type("strategy_consulting") is ProjectType.GREENFIELD
    assert _coerce_project_type("") is ProjectType.GREENFIELD


# ---------- NormalizedProjectContext backstop validators ----------
# The agent's response model is enum-constrained (the LLM is meant to emit
# canonical values directly), but the mode="before" validators apply the
# deterministic tables so any drift the model produces is still coerced to a
# valid option rather than raising. These tests exercise that backstop layer.


def test_model_backstop_coerces_industry_casing_and_synonyms() -> None:
    m = NormalizedProjectContext.model_validate({"industry": "Healthcare"})
    assert m.industry is IndustryOption.HEALTHCARE
    m2 = NormalizedProjectContext.model_validate({"industry": "medical"})
    assert m2.industry is IndustryOption.HEALTHCARE


def test_model_backstop_maps_off_list_industry_to_unknown() -> None:
    m = NormalizedProjectContext.model_validate({"industry": "blockchain widgets"})
    assert m.industry is IndustryOption.UNKNOWN
    assert m.industry.value == ""  # renders as the blank "— pick one —" option


def test_model_backstop_coerces_off_list_project_type_to_greenfield() -> None:
    m = NormalizedProjectContext.model_validate({"project_type": "saas platform"})
    assert m.project_type is ProjectType.GREENFIELD


def test_model_backstop_normalizes_and_drops_regulatory_values() -> None:
    m = NormalizedProjectContext.model_validate(
        {"regulatory_requirements": ["hipaa", "SOC2", "ISO 27001"]}
    )
    # hipaa -> HIPAA, SOC2 -> SOC 2, ISO 27001 dropped (not in the allowed set).
    assert m.regulatory_requirements == [
        RegulatoryRequirement.HIPAA,
        RegulatoryRequirement.SOC2,
    ]


def test_model_backstop_accepts_canonical_values_unchanged() -> None:
    m = NormalizedProjectContext.model_validate(
        {
            "industry": "fintech",
            "project_type": "legacy_replacement",
            "regulatory_requirements": ["PCI-DSS"],
        }
    )
    assert m.industry is IndustryOption.FINTECH
    assert m.project_type is ProjectType.LEGACY_REPLACEMENT
    assert m.regulatory_requirements == [RegulatoryRequirement.PCI_DSS]


# ---------- _normalized_to_fields ----------


def test_normalized_to_fields_carries_canonical_values() -> None:
    n = NormalizedProjectContext(
        industry=IndustryOption.HEALTHCARE,
        project_type=ProjectType.LEGACY_REPLACEMENT,
        screen_count_estimate=25,
        integrations=["Epic EHR", "Stripe", "Twilio", "Okta"],
        regulatory_requirements=[RegulatoryRequirement.HIPAA],
        summary="Patient portal rebuild",
    )
    s = _normalized_to_fields(n)
    assert s.industry == "healthcare"  # str value the <select> renders
    assert s.project_type is ProjectType.LEGACY_REPLACEMENT
    assert s.screen_count_estimate == 25
    assert s.integration_count == 4
    assert s.integration_list == ["Epic EHR", "Stripe", "Twilio", "Okta"]
    assert s.regulatory_requirements == ["HIPAA"]


def test_normalized_to_fields_treats_zero_screens_as_unset() -> None:
    n = NormalizedProjectContext(screen_count_estimate=0)
    assert _normalized_to_fields(n).screen_count_estimate is None


def test_normalized_to_fields_caps_integration_list_at_20() -> None:
    n = NormalizedProjectContext(integrations=[f"sys_{i}" for i in range(50)])
    s = _normalized_to_fields(n)
    assert s.integration_count == 50  # raw count preserved
    assert len(s.integration_list) == 20  # but list is capped


def test_normalized_to_fields_unknown_industry_becomes_empty_string() -> None:
    n = NormalizedProjectContext(industry=IndustryOption.UNKNOWN)
    assert _normalized_to_fields(n).industry == ""


def test_normalized_to_fields_omits_roster_and_defaults_uninferable_fields() -> None:
    """The roster is intentionally absent (proposed later via AG-UI), and
    engagement_model / target_timeline_weeks default for the user to set."""
    s = _normalized_to_fields(NormalizedProjectContext(summary="Build something"))
    assert s.engagement_model is EngagementModel.TIME_AND_MATERIALS
    assert s.target_timeline_weeks is None
    # Stage2Fields has no roster field at all — prefill is fully roster-free.
    assert not hasattr(s, "roster")


# ---------- POST /estimates/draft/prefill ----------


@pytest.fixture()
def client() -> TestClient:
    from main import app

    return TestClient(app)


def test_prefill_endpoint_rejects_short_raw_input(client: TestClient) -> None:
    res = client.post("/estimates/draft/prefill", json={"raw_input": "too short"})
    assert res.status_code == 422  # Pydantic min_length=10 violation


def test_prefill_endpoint_returns_stage2_when_agent_succeeds(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """Stub the prefill agent so the endpoint test runs without ANTHROPIC_API_KEY."""

    async def fake_agent(raw_input: str) -> NormalizedProjectContext:
        return NormalizedProjectContext(
            industry=IndustryOption.FINTECH,
            project_type=ProjectType.GREENFIELD,
            screen_count_estimate=12,
            integrations=["Plaid", "Stripe"],
            regulatory_requirements=[
                RegulatoryRequirement.SOC2,
                RegulatoryRequirement.PCI_DSS,
            ],
            summary="Greenfield fintech onboarding portal",
            ambiguity_score=0.3,
            ai_tooling_description="Claude Code for development, CodeRabbit for review",
        )

    monkeypatch.setattr("prefill.run_prefill_agent", fake_agent)

    res = client.post(
        "/estimates/draft/prefill",
        json={"raw_input": "Build a greenfield fintech onboarding portal with KYC."},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["stage2"]["industry"] == "fintech"
    assert body["stage2"]["project_type"] == "greenfield"
    assert body["stage2"]["integration_count"] == 2
    assert body["stage2"]["regulatory_requirements"] == ["SOC 2", "PCI-DSS"]
    assert body["summary"] == "Greenfield fintech onboarding portal"
    assert body["ambiguity_score"] == pytest.approx(0.3)
    # AI tools named in the description flow through for Stage 3 pre-fill.
    assert body["ai_tooling_description"] == "Claude Code for development, CodeRabbit for review"
    # Prefill is fully roster-free — the roster is proposed separately via the
    # AG-UI roster agent on Stage 2 (see tests/test_roster_agui.py).
    assert "roster" not in body["stage2"]


def test_prefill_endpoint_returns_defaults_when_agent_fails(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """When the prefill agent's LLM call raises (e.g. no ANTHROPIC_API_KEY), the
    endpoint must still return a valid Stage2Context — empty / default fields and
    a conservative 0.7 ambiguity score — rather than erroring."""

    async def failing_agent(raw_input: str) -> NormalizedProjectContext:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")

    monkeypatch.setattr("prefill.run_prefill_agent", failing_agent)

    res = client.post(
        "/estimates/draft/prefill",
        json={"raw_input": "An ambiguous one-liner about a project."},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["stage2"]["industry"] == ""
    assert body["stage2"]["project_type"] == "greenfield"
    assert body["stage2"]["integration_count"] == 0
    assert body["stage2"]["regulatory_requirements"] == []
    assert body["ambiguity_score"] == pytest.approx(0.7)
    assert body["ai_tooling_description"] == ""  # nothing to pre-fill on fallback


async def test_run_prefill_agent_pins_haiku_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """The prefill agent must pin its configured (Haiku) model, not inherit the
    Opus ANTHROPIC_MODEL the six estimation twins use."""
    import prefill

    captured: dict = {}

    async def fake_call_structured(**kwargs):  # noqa: ANN003
        captured.update(kwargs)
        return NormalizedProjectContext(summary="x", ambiguity_score=0.5)

    monkeypatch.setattr("prefill.call_structured", fake_call_structured)

    await prefill.run_prefill_agent("Build a thing for a clinic.")
    from config import get_settings

    assert captured["model"] == get_settings().anthropic_model_prefill
    assert "haiku" in captured["model"]
