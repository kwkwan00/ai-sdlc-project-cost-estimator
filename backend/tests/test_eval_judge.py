"""Tests for the eval LLM-judge client (``evals.judge``).

Offline: the OpenAI client is faked, so no network/key is needed. Covers the
provider dispatch (gpt-* → OpenAI structured outputs; claude-* → the Anthropic
``call_structured`` fallback), parsed-verdict return, refusal handling, and the
missing-key guard.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from evals import judge


class _Dummy(BaseModel):
    score: float


# --- pure model-name dispatch ------------------------------------------------


@pytest.mark.parametrize(
    "model,expected",
    [
        ("gpt-5.5", True),
        ("gpt-4o", True),
        ("o3-mini", True),
        ("chatgpt-4o-latest", True),
        ("claude-sonnet-4-6", False),
        ("claude-opus-4-8", False),
    ],
)
def test_is_openai_model(model: str, expected: bool) -> None:
    assert judge._is_openai_model(model) is expected


# --- fake OpenAI client plumbing ---------------------------------------------


class _FakeMessage:
    def __init__(self, parsed: object, refusal: str | None = None) -> None:
        self.parsed = parsed
        self.refusal = refusal


class _FakeCompletions:
    def __init__(self, message: _FakeMessage) -> None:
        self._message = message
        self.kwargs: dict | None = None

    async def parse(self, **kwargs: object) -> SimpleNamespace:
        self.kwargs = dict(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=self._message)])


class _FakeClient:
    def __init__(self, message: _FakeMessage) -> None:
        self.chat = SimpleNamespace(completions=_FakeCompletions(message))


# --- OpenAI branch -----------------------------------------------------------


async def test_judge_structured_openai_parses_verdict(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    fake = _FakeClient(_FakeMessage(_Dummy(score=0.83)))
    monkeypatch.setattr(judge, "_get_openai_client", lambda: fake)
    # The OpenAI path must NOT touch the Anthropic fallback.
    monkeypatch.setattr(
        judge, "call_structured", _fail("call_structured should not run for gpt-*")
    )

    out = await judge.judge_structured(
        system="sys", user="usr", response_model=_Dummy, model="gpt-5.5"
    )

    assert isinstance(out, _Dummy)
    assert out.score == 0.83
    sent = fake.chat.completions.kwargs
    assert sent is not None
    assert sent["model"] == "gpt-5.5"
    assert sent["response_format"] is _Dummy
    assert [m["role"] for m in sent["messages"]] == ["system", "user"]
    assert sent["messages"][0]["content"] == "sys"
    assert sent["messages"][1]["content"] == "usr"
    # Reasoning models reject a custom temperature — none must be sent.
    assert "temperature" not in sent


async def test_judge_structured_openai_refusal_raises(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    fake = _FakeClient(_FakeMessage(None, refusal="cannot comply"))
    monkeypatch.setattr(judge, "_get_openai_client", lambda: fake)
    with pytest.raises(RuntimeError, match="no parsed verdict"):
        await judge.judge_structured(
            system="s", user="u", response_model=_Dummy, model="gpt-5.5"
        )


# --- Anthropic fallback branch ----------------------------------------------


async def test_judge_structured_anthropic_falls_back_to_call_structured(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    captured: dict = {}

    async def _fake_call_structured(**kwargs: object) -> _Dummy:
        captured.update(kwargs)
        return _Dummy(score=0.42)

    monkeypatch.setattr(judge, "call_structured", _fake_call_structured)
    # A claude-* model must never construct the OpenAI client.
    monkeypatch.setattr(
        judge, "_get_openai_client", _fail("OpenAI client built for a claude-* model")
    )

    out = await judge.judge_structured(
        system="s", user="u", response_model=_Dummy, model="claude-sonnet-4-6"
    )

    assert out.score == 0.42
    assert captured["model"] == "claude-sonnet-4-6"
    assert captured["response_model"] is _Dummy
    assert captured["tool_name"] == "submit_evaluation"


# --- missing-key guard -------------------------------------------------------


def test_get_openai_client_requires_key(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # The OpenAI client getter is now shared in orchestrator.llm (judge re-exports it); patch the
    # client cache + settings there, where the function reads them.
    import orchestrator.llm as llm

    monkeypatch.setattr(llm, "_openai_client", None)
    monkeypatch.setattr(llm, "get_settings", lambda: SimpleNamespace(openai_api_key=""))
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        judge._get_openai_client()


def _fail(message: str):  # type: ignore[no-untyped-def]
    """Return a callable that fails if invoked — asserts a branch is NOT taken."""

    def _raise(*_a: object, **_k: object) -> object:
        raise AssertionError(message)

    return _raise
