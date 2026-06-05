"""Coverage for the LLM-driven Stage 2 prefill.

Splits into three concerns:
1. `_normalize_regulatory` + `_coerce_project_type` — pure helpers with no I/O.
2. `parsed_context_to_stage2` — the ParsedContext → Stage2Context mapper.
3. `POST /estimates/draft/prefill` — HTTP route end-to-end, with the LLM call
   stubbed so the test runs without an API key.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from models.project_schema import EngagementModel, ProjectType, RoleRoster
from orchestrator.nodes.parse_input import ParsedContext
from prefill import (
    _coerce_project_type,
    _normalize_regulatory,
    parsed_context_to_stage2,
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


# ---------- parsed_context_to_stage2 ----------


def test_parsed_context_to_stage2_carries_industry_and_project_type() -> None:
    parsed = ParsedContext(
        industry_hint="healthcare",
        project_type_hint="legacy_replacement",
        summary="Patient portal rebuild",
    )
    stage2 = parsed_context_to_stage2(parsed)
    assert stage2.industry == "healthcare"
    assert stage2.project_type is ProjectType.LEGACY_REPLACEMENT


def test_parsed_context_to_stage2_derives_integration_count_from_mentions() -> None:
    parsed = ParsedContext(
        integration_mentions=["Epic EHR", "Stripe", "Twilio", "Okta"],
    )
    stage2 = parsed_context_to_stage2(parsed)
    assert stage2.integration_count == 4
    assert stage2.integration_list == ["Epic EHR", "Stripe", "Twilio", "Okta"]


def test_parsed_context_to_stage2_caps_integration_list_at_20() -> None:
    parsed = ParsedContext(integration_mentions=[f"sys_{i}" for i in range(50)])
    stage2 = parsed_context_to_stage2(parsed)
    assert stage2.integration_count == 50  # raw count preserved
    assert len(stage2.integration_list) == 20  # but list is capped


def test_parsed_context_to_stage2_normalizes_regulatory_mentions() -> None:
    parsed = ParsedContext(regulatory_mentions=["hipaa", "SOC2", "ISO 27001"])
    stage2 = parsed_context_to_stage2(parsed)
    assert stage2.regulatory_requirements == ["HIPAA", "SOC 2"]


def test_parsed_context_to_stage2_treats_zero_screen_count_as_unset() -> None:
    parsed = ParsedContext(screen_count_estimate=0)
    stage2 = parsed_context_to_stage2(parsed)
    assert stage2.screen_count_estimate is None


def test_parsed_context_to_stage2_preserves_positive_screen_count() -> None:
    parsed = ParsedContext(screen_count_estimate=25)
    stage2 = parsed_context_to_stage2(parsed)
    assert stage2.screen_count_estimate == 25


def test_parsed_context_to_stage2_leaves_uninferable_fields_at_defaults() -> None:
    """engagement_model, target_timeline_weeks, roster aren't derivable from a
    free-form description — they must match Stage2Context defaults so the user
    fills them in deliberately."""
    parsed = ParsedContext(summary="Build something")
    stage2 = parsed_context_to_stage2(parsed)
    assert stage2.engagement_model is EngagementModel.TIME_AND_MATERIALS
    assert stage2.target_timeline_weeks is None
    # Roster matches the canonical default exactly.
    assert [r.role_id for r in stage2.roster.roles] == [
        r.role_id for r in RoleRoster.default().roles
    ]


# ---------- POST /estimates/draft/prefill ----------


@pytest.fixture()
def client() -> TestClient:
    from main import app

    return TestClient(app)


def test_prefill_endpoint_rejects_short_raw_input(client: TestClient) -> None:
    res = client.post("/estimates/draft/prefill", json={"raw_input": "too short"})
    assert res.status_code == 422  # Pydantic min_length=10 violation


def test_prefill_endpoint_returns_stage2_when_llm_call_succeeds(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """Stub the LLM extractor so the test runs without ANTHROPIC_API_KEY."""

    async def fake_extract(raw_input: str) -> ParsedContext:
        return ParsedContext(
            industry_hint="fintech",
            project_type_hint="greenfield",
            screen_count_estimate=12,
            integration_mentions=["Plaid", "Stripe"],
            regulatory_mentions=["SOC 2", "PCI"],
            summary="Greenfield fintech onboarding portal",
            ambiguity_score=0.3,
        )

    monkeypatch.setattr("prefill.extract_context_from_raw", fake_extract)

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


def test_prefill_endpoint_returns_defaults_when_llm_call_fails(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """When the underlying extract_context_from_raw falls back internally, the
    endpoint should still return a valid Stage2Context — just one with empty /
    default fields and the conservative 0.7 ambiguity score."""

    async def fallback_extract(raw_input: str) -> ParsedContext:
        # Mimic the fallback ParsedContext shape from parse_input._fallback_context.
        return ParsedContext(summary=raw_input[:280], ambiguity_score=0.7)

    monkeypatch.setattr("prefill.extract_context_from_raw", fallback_extract)

    res = client.post(
        "/estimates/draft/prefill",
        json={"raw_input": "An ambiguous one-liner about a project."},
    )
    assert res.status_code == 200
    body = res.json()
    # Industry empty, default greenfield, etc.
    assert body["stage2"]["industry"] == ""
    assert body["stage2"]["project_type"] == "greenfield"
    assert body["stage2"]["integration_count"] == 0
    assert body["stage2"]["regulatory_requirements"] == []
    assert body["ambiguity_score"] == pytest.approx(0.7)
