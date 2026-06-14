"""Coverage for orchestrator.llm — specifically the sampling-parameter guard.

The Fable 5 / Opus 4.6+ / Sonnet 4.6 families REMOVED temperature/top_p/top_k and
return HTTP 400 if any are sent. `call_structured` must therefore omit
`temperature` for those models (otherwise every structured call 400s and silently
falls back to stub output) while still passing it for legacy models that honor it.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from orchestrator import llm
from orchestrator.llm import _model_accepts_sampling_params, call_structured


class _Echo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str = "ok"


# ---------- _model_accepts_sampling_params ----------


@pytest.mark.parametrize(
    "model",
    [
        "claude-fable-5",
        "claude-opus-4-6",
        "claude-opus-4-7",
        "claude-opus-4-8",
        "claude-sonnet-4-6",
    ],
)
def test_frontier_models_reject_sampling_params(model: str) -> None:
    assert _model_accepts_sampling_params(model) is False


@pytest.mark.parametrize(
    "model",
    [
        "claude-opus-4-5-20251101",
        "claude-opus-4-1-20250805",
        "claude-sonnet-4-5-20250929",
        "claude-haiku-4-5-20251001",
        "claude-3-haiku-20240307",
    ],
)
def test_legacy_models_accept_sampling_params(model: str) -> None:
    assert _model_accepts_sampling_params(model) is True


# ---------- call_structured request construction ----------


class _FakeToolUseBlock:
    type = "tool_use"

    def __init__(self, name: str, payload: dict) -> None:
        self.name = name
        self.input = payload


class _FakeResponse:
    def __init__(self, name: str) -> None:
        self.content = [_FakeToolUseBlock(name, {"value": "ok"})]
        self.stop_reason = "tool_use"


class _SpyMessages:
    def __init__(self) -> None:
        self.kwargs: dict | None = None

    async def create(self, **kwargs):  # noqa: ANN003
        self.kwargs = kwargs
        return _FakeResponse(kwargs["tool_choice"]["name"])


class _SpyClient:
    def __init__(self) -> None:
        self.messages = _SpyMessages()


@pytest.fixture()
def spy_client(monkeypatch: pytest.MonkeyPatch) -> _SpyClient:
    client = _SpyClient()
    # Bypass _get_client (which would require ANTHROPIC_API_KEY) and pin the model.
    monkeypatch.setattr(llm, "_get_client", lambda: client)

    class _Settings:
        anthropic_model = "unused-overridden-per-call"

    monkeypatch.setattr(llm, "get_settings", lambda: _Settings())
    return client


@pytest.mark.asyncio
async def test_call_structured_omits_temperature_for_frontier_model(
    spy_client: _SpyClient,
) -> None:
    await call_structured(
        system="s",
        user="u",
        response_model=_Echo,
        tool_name="echo",
        model="claude-opus-4-8",
    )
    assert "temperature" not in spy_client.messages.kwargs


@pytest.mark.asyncio
async def test_call_structured_sends_temperature_for_legacy_model(
    spy_client: _SpyClient,
) -> None:
    await call_structured(
        system="s",
        user="u",
        response_model=_Echo,
        tool_name="echo",
        model="claude-haiku-4-5-20251001",
        temperature=0.0,
    )
    assert spy_client.messages.kwargs["temperature"] == 0.0


# ---------- effort -> output_config passthrough ----------


@pytest.mark.asyncio
async def test_call_structured_sets_output_config_when_effort_given(
    spy_client: _SpyClient,
) -> None:
    await call_structured(
        system="s",
        user="u",
        response_model=_Echo,
        tool_name="echo",
        model="claude-sonnet-4-6",
        effort="low",
    )
    assert spy_client.messages.kwargs["output_config"] == {"effort": "low"}


@pytest.mark.asyncio
async def test_call_structured_omits_output_config_by_default(
    spy_client: _SpyClient,
) -> None:
    # Proves the six twins / prefill (which never pass effort) are unaffected.
    await call_structured(
        system="s",
        user="u",
        response_model=_Echo,
        tool_name="echo",
        model="claude-opus-4-8",
    )
    assert "output_config" not in spy_client.messages.kwargs


class _RetrySpyMessages:
    """Rejects any create call carrying output_config; succeeds without it."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def create(self, **kwargs):  # noqa: ANN003
        self.calls.append(dict(kwargs))
        if "output_config" in kwargs:
            raise RuntimeError("output_config is not supported for this model")
        return _FakeResponse(kwargs["tool_choice"]["name"])


class _RetrySpyClient:
    def __init__(self) -> None:
        self.messages = _RetrySpyMessages()


@pytest.mark.asyncio
async def test_call_structured_retries_without_output_config_on_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _RetrySpyClient()
    monkeypatch.setattr(llm, "_get_client", lambda: client)

    class _Settings:
        anthropic_model = "unused"

    monkeypatch.setattr(llm, "get_settings", lambda: _Settings())

    out = await call_structured(
        system="s",
        user="u",
        response_model=_Echo,
        tool_name="echo",
        model="claude-sonnet-4-6",
        effort="low",
    )
    assert out.value == "ok"
    # First attempt carried output_config (rejected); retry dropped it.
    assert len(client.messages.calls) == 2
    assert "output_config" in client.messages.calls[0]
    assert "output_config" not in client.messages.calls[1]


# ---------- one-shot retry on a transient structured-output (validation) failure ----------


class _BadInputBlock:
    """A tool_use block whose input fails validation (extra field under extra=forbid)."""

    type = "tool_use"

    def __init__(self, name: str, payload: dict) -> None:
        self.name = name
        self.input = payload


class _ValidationRetryMessages:
    """First create returns an invalid tool input (extra field → ValidationError);
    later creates return a valid one. Mirrors the real ``extra_forbidden`` flake."""

    def __init__(self, *, fail_times: int) -> None:
        self.calls: list[dict] = []
        self._fail_times = fail_times

    async def create(self, **kwargs):  # noqa: ANN003
        self.calls.append(dict(kwargs))
        name = kwargs["tool_choice"]["name"]
        resp = _FakeResponse(name)
        if len(self.calls) <= self._fail_times:
            resp.content = [_BadInputBlock(name, {"bogus": 1})]  # extra field
        return resp


class _ValidationRetryClient:
    def __init__(self, *, fail_times: int) -> None:
        self.messages = _ValidationRetryMessages(fail_times=fail_times)


def _pin(monkeypatch: pytest.MonkeyPatch, client: object) -> None:
    monkeypatch.setattr(llm, "_get_client", lambda: client)

    class _Settings:
        anthropic_model = "unused"

    monkeypatch.setattr(llm, "get_settings", lambda: _Settings())


@pytest.mark.asyncio
async def test_call_structured_retries_once_on_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # First attempt's tool input is invalid (extra field), second is valid → recovers.
    client = _ValidationRetryClient(fail_times=1)
    _pin(monkeypatch, client)

    out = await call_structured(
        system="s", user="u", response_model=_Echo, tool_name="echo",
        model="claude-opus-4-8",
    )
    assert out.value == "ok"
    assert len(client.messages.calls) == 2  # retried exactly once
    # The retry reinforces the schema contract.
    assert "ONLY the fields" in client.messages.calls[1]["system"]
    assert "ONLY the fields" not in client.messages.calls[0]["system"]


@pytest.mark.asyncio
async def test_call_structured_propagates_after_two_validation_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Both attempts invalid → propagate so callers (twins) fall back to a stub.
    client = _ValidationRetryClient(fail_times=2)
    _pin(monkeypatch, client)

    with pytest.raises(ValidationError):
        await call_structured(
            system="s", user="u", response_model=_Echo, tool_name="echo",
            model="claude-opus-4-8",
        )
    assert len(client.messages.calls) == 2  # one retry, then give up
