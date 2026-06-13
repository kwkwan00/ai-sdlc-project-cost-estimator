"""Tests for the Stage 3 AI-tooling classifier (tooling_classifier.py).

The LLM (`call_structured`) and the docs-mcp research call (`research_with_local_mcp`)
are stubbed so the two-step classify → research → reclassify flow and its fallbacks
are exercised deterministically without any network access.
"""

from __future__ import annotations

import pytest

import tooling_classifier as tc
from models.project_schema import AiToolingLevel as T
from models.project_schema import PhaseToolingLevels


class _FakeSettings:
    def __init__(
        self,
        *,
        key: str = "test-key",
        model: str = "claude-sonnet-4-6",
        docs_url: str = "http://docs-mcp-server:6280/mcp",
        docs_token: str = "",
        docs_timeout: float = 25.0,
        docs_auto_scrape: bool = True,
        docs_scrape_timeout: float = 240.0,
    ) -> None:
        self.anthropic_api_key = key
        self.anthropic_model_tooling = model
        self.docs_mcp_url = docs_url
        self.docs_mcp_auth_token = docs_token
        self.docs_mcp_research_timeout_s = docs_timeout
        self.docs_mcp_auto_scrape = docs_auto_scrape
        self.docs_mcp_scrape_timeout_s = docs_scrape_timeout


def _patch_settings(monkeypatch, **kwargs) -> None:
    monkeypatch.setattr(tc, "get_settings", lambda: _FakeSettings(**kwargs))


def _classification(*, dev=T.NONE, review=T.NONE, ux=T.NONE, unknown=None):
    return tc.ToolingClassification(
        ai_tooling=PhaseToolingLevels(development=dev, code_review=review, ux_design=ux),
        unknown_tools=unknown or [],
        notes="",
    )


def _patch_classifier(monkeypatch, results, *, calls=None):
    """Stub call_structured to return successive `results` (list) per invocation."""
    seq = list(results)

    async def _fake_call_structured(**kwargs):
        if calls is not None:
            calls.append(kwargs)
        if not seq:
            raise AssertionError("call_structured invoked more times than expected")
        out = seq.pop(0)
        if isinstance(out, Exception):
            raise out
        return out

    monkeypatch.setattr(tc, "call_structured", _fake_call_structured)


def _patch_research(monkeypatch, *, returns="", raise_exc=None, calls=None):
    async def _fake_research(**kwargs):
        if calls is not None:
            calls.append(kwargs)
        if raise_exc is not None:
            raise raise_exc
        return returns

    monkeypatch.setattr(tc, "research_with_local_mcp", _fake_research)


@pytest.mark.asyncio
async def test_blank_description_returns_all_none_without_llm(monkeypatch) -> None:
    calls: list = []
    _patch_settings(monkeypatch)
    _patch_classifier(monkeypatch, [], calls=calls)  # any call → AssertionError
    result = await tc.classify_ai_tooling("   ")
    assert result.ai_tooling == PhaseToolingLevels()  # all none
    assert calls == []  # LLM never invoked


@pytest.mark.asyncio
async def test_recognized_tools_classified_in_one_pass(monkeypatch) -> None:
    calls: list = []
    _patch_settings(monkeypatch)
    _patch_classifier(monkeypatch, [_classification(dev=T.AGENTIC, review=T.AGENTIC, ux=T.CHAT)], calls=calls)
    _patch_research(monkeypatch)  # should not be needed
    result = await tc.classify_ai_tooling("Claude Code for dev+review, Figma AI for design")
    assert result.ai_tooling.development is T.AGENTIC
    assert result.ai_tooling.code_review is T.AGENTIC
    assert result.ai_tooling.ux_design is T.CHAT
    assert len(calls) == 1  # no research → no second classification


@pytest.mark.asyncio
async def test_unknown_tool_triggers_research_then_reclassify(monkeypatch) -> None:
    cs_calls: list = []
    research_calls: list = []
    _patch_settings(monkeypatch)
    # First pass flags the tool unknown (dev stays none); second pass classifies it.
    _patch_classifier(
        monkeypatch,
        [
            _classification(dev=T.NONE, unknown=["ZebraAI"]),
            _classification(dev=T.AGENTIC, unknown=[]),
        ],
        calls=cs_calls,
    )
    _patch_research(monkeypatch, returns="ZebraAI is an agentic coding agent.", calls=research_calls)
    result = await tc.classify_ai_tooling("We use ZebraAI for coding")
    assert result.ai_tooling.development is T.AGENTIC
    assert result.unknown_tools == []
    assert len(cs_calls) == 2  # classify, then reclassify
    assert len(research_calls) == 1
    # The research digest was threaded into the second classify call's user prompt.
    assert "Research notes" in cs_calls[1]["user"]


@pytest.mark.asyncio
async def test_auto_scrape_on_instructs_indexing_before_research(monkeypatch) -> None:
    research_calls: list = []
    _patch_settings(monkeypatch, docs_auto_scrape=True)
    _patch_classifier(
        monkeypatch,
        [_classification(dev=T.NONE, unknown=["ZebraAI"]), _classification(dev=T.AGENTIC)],
    )
    _patch_research(monkeypatch, returns="ZebraAI is an agentic coding agent.", calls=research_calls)
    await tc.classify_ai_tooling("We use ZebraAI for coding")
    # The research call must tell Claude to scrape/index the missing tool first.
    system = research_calls[0]["system"]
    assert "scrape_docs" in system
    assert "indexed" in system.lower()


@pytest.mark.asyncio
async def test_auto_scrape_off_uses_search_only_prompt(monkeypatch) -> None:
    research_calls: list = []
    _patch_settings(monkeypatch, docs_auto_scrape=False)
    _patch_classifier(
        monkeypatch,
        [_classification(dev=T.NONE, unknown=["ZebraAI"]), _classification(dev=T.AGENTIC)],
    )
    _patch_research(monkeypatch, returns="ZebraAI is an agentic coding agent.", calls=research_calls)
    await tc.classify_ai_tooling("We use ZebraAI for coding")
    system = research_calls[0]["system"]
    assert "scrape_docs" not in system  # search-only: no indexing instruction
    assert "search_docs" in system


@pytest.mark.asyncio
async def test_unknown_stays_none_when_docs_mcp_disabled(monkeypatch) -> None:
    cs_calls: list = []
    _patch_settings(monkeypatch, docs_url="")  # MCP disabled
    _patch_classifier(monkeypatch, [_classification(dev=T.NONE, unknown=["ZebraAI"])], calls=cs_calls)
    _patch_research(monkeypatch, raise_exc=AssertionError("research must not run when url empty"))
    result = await tc.classify_ai_tooling("We use ZebraAI for coding")
    assert result.ai_tooling.development is T.NONE  # unverified → stays none
    assert result.unknown_tools == ["ZebraAI"]
    assert len(cs_calls) == 1  # no reclassification


@pytest.mark.asyncio
async def test_unknown_stays_none_when_research_errors(monkeypatch) -> None:
    cs_calls: list = []
    _patch_settings(monkeypatch)
    _patch_classifier(monkeypatch, [_classification(dev=T.NONE, unknown=["ZebraAI"])], calls=cs_calls)
    _patch_research(monkeypatch, raise_exc=RuntimeError("mcp unreachable"))
    result = await tc.classify_ai_tooling("We use ZebraAI for coding")
    assert result.ai_tooling.development is T.NONE
    assert result.unknown_tools == ["ZebraAI"]
    assert len(cs_calls) == 1


@pytest.mark.asyncio
async def test_unknown_stays_none_when_research_times_out(monkeypatch) -> None:
    import asyncio

    cs_calls: list = []
    # Default auto_scrape=True uses the scrape timeout, so make THAT tiny.
    _patch_settings(monkeypatch, docs_scrape_timeout=0.01)
    _patch_classifier(monkeypatch, [_classification(dev=T.NONE, unknown=["ZebraAI"])], calls=cs_calls)

    async def _slow_research(**kwargs):
        await asyncio.sleep(1.0)  # exceeds the 0.01s timeout
        return "too late"

    monkeypatch.setattr(tc, "research_with_local_mcp", _slow_research)
    result = await tc.classify_ai_tooling("We use ZebraAI for coding")
    assert result.ai_tooling.development is T.NONE  # timed out → stays none
    assert result.unknown_tools == ["ZebraAI"]
    assert len(cs_calls) == 1  # no reclassification


@pytest.mark.asyncio
async def test_falls_back_to_all_none_when_classifier_errors(monkeypatch) -> None:
    _patch_settings(monkeypatch)
    _patch_classifier(monkeypatch, [RuntimeError("no api key")])
    result = await tc.classify_ai_tooling("Claude Code for dev")
    assert result.ai_tooling == PhaseToolingLevels()  # all none


@pytest.mark.asyncio
async def test_research_skipped_when_no_api_key(monkeypatch) -> None:
    cs_calls: list = []
    _patch_settings(monkeypatch, key="")  # no key → never reach research branch
    _patch_classifier(monkeypatch, [_classification(dev=T.NONE, unknown=["ZebraAI"])], calls=cs_calls)
    _patch_research(monkeypatch, raise_exc=AssertionError("research must not run without a key"))
    result = await tc.classify_ai_tooling("We use ZebraAI for coding")
    assert result.unknown_tools == ["ZebraAI"]
    assert len(cs_calls) == 1


# ---------- POST /estimates/draft/classify-tooling ----------


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from main import app

    return TestClient(app)


def test_classify_tooling_endpoint_returns_levels(monkeypatch, client) -> None:
    _patch_settings(monkeypatch)
    _patch_classifier(monkeypatch, [_classification(dev=T.AGENTIC, ux=T.CHAT)])
    res = client.post("/estimates/draft/classify-tooling", json={"description": "Claude Code, Figma AI"})
    assert res.status_code == 200
    body = res.json()
    assert body["ai_tooling"]["development"] == "agentic"
    assert body["ai_tooling"]["ux_design"] == "chat"
    assert body["ai_tooling"]["deployment"] == "none"


def test_classify_tooling_endpoint_blank_returns_all_none(monkeypatch, client) -> None:
    _patch_settings(monkeypatch)
    _patch_classifier(monkeypatch, [])  # any LLM call would raise; blank must not call
    res = client.post("/estimates/draft/classify-tooling", json={"description": ""})
    assert res.status_code == 200
    body = res.json()
    assert all(level == "none" for level in body["ai_tooling"].values())
