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
from orchestrator.llm import (
    _model_accepts_sampling_params,
    _pydantic_to_tool_schema,
    call_structured,
)


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


# ---------- _pydantic_to_tool_schema memoization ----------


def test_pydantic_to_tool_schema_is_memoized() -> None:
    # The schema is immutable per (model class, tool_name) and recomputing
    # model_json_schema() on every call_structured is wasteful, so it's cached.
    first = _pydantic_to_tool_schema(_Echo, "echo")
    second = _pydantic_to_tool_schema(_Echo, "echo")
    assert first is second  # same cached object, not just equal
    # A different tool_name is a distinct cache entry with the right name.
    other = _pydantic_to_tool_schema(_Echo, "other")
    assert other is not first
    assert other["name"] == "other"
    # Content is still correct.
    assert first["name"] == "echo"
    assert "value" in first["input_schema"]["properties"]


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
        self.kwargs: dict = {}

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


class _AuthErrorMessages:
    """Every create call raises a non-output_config API error (e.g. auth/rate-limit).
    The narrowed retry must re-raise immediately rather than re-issuing the call."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def create(self, **kwargs):  # noqa: ANN003
        self.calls.append(dict(kwargs))
        raise RuntimeError("401 authentication_error: invalid x-api-key")


class _AuthErrorClient:
    def __init__(self) -> None:
        self.messages = _AuthErrorMessages()


@pytest.mark.asyncio
async def test_call_structured_does_not_retry_non_output_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An auth/rate-limit error carrying output_config in kwargs must NOT be retried
    # without output_config (that would double the failing call). The corrective
    # validation retry also re-issues, so each _attempt fires exactly once → 2 total.
    client = _AuthErrorClient()
    monkeypatch.setattr(llm, "_get_client", lambda: client)

    class _Settings:
        anthropic_model = "unused"

    monkeypatch.setattr(llm, "get_settings", lambda: _Settings())

    with pytest.raises(RuntimeError, match="authentication_error"):
        await call_structured(
            system="s",
            user="u",
            response_model=_Echo,
            tool_name="echo",
            model="claude-sonnet-4-6",
            effort="low",
        )
    # No output_config-less retry within either attempt: the first attempt raises,
    # the corrective attempt raises too — both still carry output_config.
    assert len(client.messages.calls) == 2
    assert all("output_config" in c for c in client.messages.calls)


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


# ---------- _GuardedMcpSession (docs-mcp research SSRF / allowlist / budget guard) ----------


class _FakeMcpSession:
    """Records forwarded tool calls; stands in for an MCP ClientSession."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict | None]] = []

    async def call_tool(self, name, arguments=None, *args, **kwargs):
        self.calls.append((name, arguments))
        return {"ok": name}

    async def list_tools(self):  # exercises __getattr__ delegation
        return "TOOLS"


def _guard(session, *, allowed=None, allowlist=None, max_calls=10):
    from orchestrator.llm import _GuardedMcpSession

    return _GuardedMcpSession(
        session, allowed_tools=allowed, url_allowlist=allowlist, max_calls=max_calls
    )


@pytest.mark.asyncio
async def test_guard_blocks_disallowed_tool() -> None:
    fake = _FakeMcpSession()
    g = _guard(fake, allowed=frozenset({"search_docs"}))
    with pytest.raises(PermissionError):
        await g.call_tool("fetch_url", {"url": "https://8.8.8.8"})
    assert fake.calls == []  # never forwarded to the server


@pytest.mark.asyncio
async def test_guard_blocks_internal_url_arg() -> None:
    fake = _FakeMcpSession()
    g = _guard(fake, allowed=None)  # tool allowed, but the URL is internal
    with pytest.raises(PermissionError):
        await g.call_tool("fetch_url", {"url": "http://169.254.169.254/latest/meta-data"})
    assert fake.calls == []


@pytest.mark.asyncio
async def test_guard_forwards_allowed_tool_with_public_url() -> None:
    fake = _FakeMcpSession()
    g = _guard(fake, allowed=frozenset({"scrape_docs"}))
    args = {"library": "x", "url": "https://8.8.8.8/docs"}  # public literal IP → no DNS
    assert await g.call_tool("scrape_docs", args) == {"ok": "scrape_docs"}
    assert fake.calls == [("scrape_docs", args)]


@pytest.mark.asyncio
async def test_guard_forwards_non_url_tool() -> None:
    fake = _FakeMcpSession()
    g = _guard(fake, allowed=frozenset({"search_docs"}))
    await g.call_tool("search_docs", {"query": "claude code"})
    assert fake.calls == [("search_docs", {"query": "claude code"})]


@pytest.mark.asyncio
async def test_guard_enforces_call_budget() -> None:
    fake = _FakeMcpSession()
    g = _guard(fake, allowed=None, max_calls=2)
    await g.call_tool("search_docs", {"q": "a"})
    await g.call_tool("search_docs", {"q": "b"})
    with pytest.raises(PermissionError):
        await g.call_tool("search_docs", {"q": "c"})


@pytest.mark.asyncio
async def test_guard_delegates_unknown_attrs() -> None:
    fake = _FakeMcpSession()
    g = _guard(fake)
    assert await g.list_tools() == "TOOLS"


# ---------- tool-call logging ----------


def test_summarize_tool_args_is_injection_safe_and_truncated() -> None:
    from orchestrator.llm import _summarize_tool_args

    assert _summarize_tool_args(None) == "{}"
    assert _summarize_tool_args({}) == "{}"
    # whitespace/newlines collapsed; long strings truncated with an ellipsis.
    out = _summarize_tool_args({"query": "claude  code\nguide"}, max_len=80)
    assert out == "{query='claude code guide'}"
    long = _summarize_tool_args({"q": "x" * 200}, max_len=10)
    assert long == "{q='" + "x" * 10 + "…'}"
    # non-string values are summarized by type, never dumped verbatim.
    assert _summarize_tool_args({"n": 5, "items": [1, 2]}) == "{n=<int>, items=<list>}"


@pytest.mark.asyncio
async def test_tool_call_logs_invocation_and_result(caplog) -> None:
    import logging

    fake = _FakeMcpSession()
    g = _guard(fake, allowed=frozenset({"search_docs"}))
    with caplog.at_level(logging.INFO, logger="orchestrator.llm"):
        await g.call_tool("search_docs", {"query": "claude code"})
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "tool call → search_docs [1/10]" in msgs
    assert "args={query='claude code'}" in msgs
    assert "tool call ✓ search_docs [1/10]" in msgs


@pytest.mark.asyncio
async def test_blocked_tool_call_logs_warning(caplog) -> None:
    import logging

    fake = _FakeMcpSession()
    g = _guard(fake, allowed=frozenset({"search_docs"}))
    with caplog.at_level(logging.WARNING, logger="orchestrator.llm"):
        with pytest.raises(PermissionError):
            await g.call_tool("fetch_url", {"url": "https://8.8.8.8"})
    assert any("tool call ✗ fetch_url" in r.getMessage() for r in caplog.records)
