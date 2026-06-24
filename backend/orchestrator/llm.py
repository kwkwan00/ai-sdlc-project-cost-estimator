"""Anthropic SDK wrapper that returns Pydantic-typed structured output.

Pattern: define the response shape as a Pydantic model, expose it to Claude as a
single tool, force `tool_choice` to that tool, then parse the tool-use block back
into the model. Faster + more reliable than JSON-mode prompting.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import time
from typing import Any, TypeVar

from anthropic import AsyncAnthropic
from pydantic import BaseModel, ValidationError

from config import get_settings
from observability.langfuse_wrapper import traced

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

_client: AsyncAnthropic | None = None

# Model families that REMOVED sampling parameters (temperature / top_p / top_k):
# Fable 5 and the Opus 4.6/4.7/4.8 + Sonnet 4.6 generation return HTTP 400 if any
# are sent. Sending temperature to one of these is what made every structured
# call silently fall back to stub output. Legacy models (Opus ≤4.5, Sonnet ≤4.5,
# Haiku 4.5, 3.x) still accept temperature, so we keep passing it there for
# determinism. Matched as substrings against the configured model id.
_NO_SAMPLING_PARAM_MODELS = (
    "fable-5",
    "opus-4-6",
    "opus-4-7",
    "opus-4-8",
    "sonnet-4-6",
)


def _model_accepts_sampling_params(model: str) -> bool:
    """True when the model honors temperature/top_p/top_k (legacy families).

    Defaults to True for unrecognized ids so we don't silently drop determinism
    on older models; the frontier families that 400 are explicitly listed.
    """
    return not any(tag in model for tag in _NO_SAMPLING_PARAM_MODELS)


def _record_call_usage_and_log(
    response: Any, *, use_model: str, tool_name: str, started: float
) -> None:
    """Record one Claude call's token usage and emit the INFO completion log line.

    Hoisted out of ``call_structured._attempt`` (which fires this once per attempt —
    both attempts cost tokens). Reads ``response.usage`` defensively (any field may be
    absent on a partial/mocked response) and records via the contextvar accumulator,
    a no-op when nothing is bound (tests / stub path / pre-submission agents). Pure
    side effects; no return value and no behavior change."""
    usage = getattr(response, "usage", None)
    from orchestrator.usage import record_usage

    record_usage(
        model=use_model,
        input_tokens=getattr(usage, "input_tokens", 0) or 0,
        output_tokens=getattr(usage, "output_tokens", 0) or 0,
        cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
    )
    logger.info(
        "llm call ✓ model=%s tool=%s latency=%dms stop=%s "
        "tokens(in/out/cache_read)=%s/%s/%s",
        use_model,
        tool_name,
        int((time.perf_counter() - started) * 1000),
        response.stop_reason,
        getattr(usage, "input_tokens", None),
        getattr(usage, "output_tokens", None),
        getattr(usage, "cache_read_input_tokens", None),
    )


def _get_client() -> AsyncAnthropic:
    global _client
    if _client is None:
        settings = get_settings()
        if not settings.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        _client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _client


@functools.cache
def _pydantic_to_tool_schema(model: type[BaseModel], name: str) -> dict[str, Any]:
    """Convert a Pydantic model to an Anthropic tool definition.

    The result is immutable per `(model class, tool_name)`, so it's memoized — every
    `call_structured` call would otherwise recompute `model_json_schema()`. Callers
    must treat the returned dict as read-only (it's a shared cached object).
    """
    schema = model.model_json_schema()
    # Strip `$defs` / `definitions` cycles by inlining refs.
    return {
        "name": name,
        "description": (model.__doc__ or f"Return a structured {name}.").strip(),
        "input_schema": schema,
    }


@traced(name="claude-structured-output", as_type="generation")
async def call_structured(
    *,
    system: str,
    user: str,
    response_model: type[T],
    tool_name: str = "submit",
    model: str | None = None,
    max_tokens: int = 4096,
    temperature: float = 0.0,
    extra_user_blocks: list[dict[str, Any]] | None = None,
    effort: str | None = None,
) -> T:
    """Call Claude and return a parsed instance of `response_model`.

    Forces tool-use so we get a JSON object matching the model's schema.

    `effort` ("low" | "medium" | "high" | "max") is an optional speed/quality
    lever sent as `output_config.effort`. Left None for every existing caller so
    behavior is unchanged; opt in (e.g. the roster agent uses "low") to trade a
    little depth for latency. Only supported on the newer families (Sonnet 4.6,
    Opus 4.6+, Fable 5) — the create call retries once without it if rejected.
    """
    settings = get_settings()
    client = _get_client()
    use_model = model or settings.anthropic_model
    tool = _pydantic_to_tool_schema(response_model, tool_name)

    user_blocks: list[dict[str, Any]] = [{"type": "text", "text": user}]
    if extra_user_blocks:
        user_blocks.extend(extra_user_blocks)

    create_kwargs: dict[str, Any] = {
        "model": use_model,
        "max_tokens": max_tokens,
        "system": system,
        "tools": [tool],
        "tool_choice": {"type": "tool", "name": tool_name},
        "messages": [{"role": "user", "content": user_blocks}],
    }
    # Only send temperature to models that still accept it. The frontier families
    # (Fable 5, Opus 4.6+, Sonnet 4.6) 400 on any sampling parameter; passing
    # temperature there is what silently broke structured output for prefill and
    # all six twins.
    if _model_accepts_sampling_params(use_model):
        create_kwargs["temperature"] = temperature

    # `output_config.effort` is GA on the current SDK/models, but degrade safely:
    # if a model/endpoint/older SDK rejects it (API 400 or unknown-kwarg
    # TypeError), retry once without it rather than failing the whole call.
    if effort is not None:
        create_kwargs["output_config"] = {"effort": effort}

    logger.debug(
        "llm call → model=%s tool=%s max_tokens=%s effort=%s response_model=%s",
        use_model,
        tool_name,
        max_tokens,
        effort or "default",
        response_model.__name__,
    )

    # One attempt: create (with the output_config fallback) → record usage → parse the
    # forced tool input into the response model.
    async def _attempt(*, corrective: bool) -> T:
        kwargs = dict(create_kwargs)
        if corrective:
            # Retried after a transient structured-output slip (the model emitted a
            # field outside the tool schema or skipped a required one). Reinforce the
            # contract so the second attempt recovers instead of falling back to a stub.
            kwargs["system"] = (
                str(kwargs["system"])
                + "\n\nIMPORTANT: Reply by calling the tool with ONLY the fields defined "
                "in its input schema — include every required field and add no extra fields."
            )
        started = time.perf_counter()
        try:
            response = await client.messages.create(**kwargs)
        except Exception as exc:  # noqa: BLE001
            # Only retry-without-output_config when the failure is actually about that
            # param (older SDK/model/endpoint rejecting it). Auth (401) / rate-limit
            # (429) / other API errors must NOT be retried here — doing so doubles the
            # call without recovering — so we re-raise them immediately.
            if "output_config" in kwargs and "output_config" in str(exc).lower():
                logger.warning(
                    "messages.create rejected output_config (%s); retrying without it", exc
                )
                kwargs.pop("output_config", None)
                response = await client.messages.create(**kwargs)
            else:
                raise

        # Every LLM / forced-tool call is logged at INFO with cost + latency so the
        # operational narrative shows model usage without needing Langfuse enabled.
        # Both attempts cost tokens, so usage is recorded on each.
        _record_call_usage_and_log(
            response, use_model=use_model, tool_name=tool_name, started=started
        )

        for block in response.content:
            if getattr(block, "type", None) == "tool_use" and block.name == tool_name:
                return response_model.model_validate(block.input)

        raise RuntimeError(
            f"Claude did not return tool_use for {tool_name}. "
            f"Stop reason: {response.stop_reason}. Content types: "
            f"{[getattr(b, 'type', '?') for b in response.content]}"
        )

    # One-shot retry on a transient structured-output failure — a tool input that
    # fails Pydantic validation (e.g. an extra field under extra="forbid"), or no
    # tool_use block at all. The model usually recovers on a second, more-strongly
    # instructed attempt; only a double failure propagates (twins then fall back to a
    # stub). Real API errors from messages.create surface immediately, not here.
    try:
        return await _attempt(corrective=False)
    except (ValidationError, RuntimeError) as exc:
        logger.warning(
            "structured output for tool %s failed (%s); retrying once", tool_name, exc
        )
        return await _attempt(corrective=True)


def _iter_strings(value: Any) -> list[str]:
    """Flatten any str leaves out of a tool-argument value (str / list / dict / nested)."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _iter_strings(v)]
    if isinstance(value, (list, tuple)):
        return [s for v in value for s in _iter_strings(v)]
    return []


def _summarize_tool_args(arguments: dict | None, *, max_len: int = 80) -> str:
    """One-line, truncated, injection-safe summary of MCP tool arguments for the tool-call log.

    The arguments derive from untrusted LLM/tool text (the same reason ``_GuardedMcpSession``
    SSRF-checks them), so never log them verbatim: collapse whitespace, truncate long strings,
    and summarize non-strings by type. Keeps the operational log readable and non-abusable."""
    if not arguments:
        return "{}"
    parts: list[str] = []
    for k, v in arguments.items():
        if isinstance(v, str):
            flat = " ".join(v.split())
            shown = flat if len(flat) <= max_len else flat[:max_len] + "…"
            parts.append(f"{k}={shown!r}")
        else:
            parts.append(f"{k}=<{type(v).__name__}>")
    return "{" + ", ".join(parts) + "}"


class _GuardedMcpSession:
    """Wraps an MCP ``ClientSession`` for the in-process research loop so the LLM can't turn it
    into an SSRF/abuse primitive: it (1) restricts which tools may be called, (2) validates every
    http(s) URL argument against the SSRF guard before forwarding, and (3) caps the total number of
    tool calls. Everything else delegates to the real session. Fails closed (raises) on a violation,
    which the caller's try/except degrades to "unknown tools stay none"."""

    def __init__(
        self,
        session: Any,
        *,
        allowed_tools: frozenset[str] | None,
        url_allowlist: frozenset[str] | None,
        max_calls: int,
    ) -> None:
        self._session = session
        self._allowed = allowed_tools
        self._url_allowlist = url_allowlist
        self._max_calls = max_calls
        self._calls = 0

    def __getattr__(self, name: str) -> Any:
        # Delegate everything we don't explicitly override (initialize, list_tools, ...).
        return getattr(self._session, name)

    async def call_tool(self, name: str, arguments: dict | None = None, *args: Any, **kwargs: Any) -> Any:
        from orchestrator.ssrf import UrlNotAllowed, assert_url_allowed, looks_like_url

        self._calls += 1
        if self._calls > self._max_calls:
            logger.warning(
                "tool call ✗ %s — docs-mcp tool-call budget exhausted (%d)", name, self._max_calls
            )
            raise PermissionError(f"docs-mcp research tool-call budget exhausted ({self._max_calls})")
        if self._allowed is not None and name not in self._allowed:
            logger.warning("tool call ✗ %s — not permitted in research (allowed=%s)", name, sorted(self._allowed))
            raise PermissionError(f"docs-mcp tool {name!r} is not permitted in research")
        for candidate in _iter_strings(arguments or {}):
            if looks_like_url(candidate):
                try:
                    await asyncio.to_thread(
                        assert_url_allowed, candidate, allowlist=self._url_allowlist
                    )
                except UrlNotAllowed as exc:
                    logger.warning("tool call ✗ %s — blocked URL by SSRF guard: %s", name, exc)
                    raise PermissionError(f"URL blocked by SSRF guard: {exc}") from exc
        logger.info(
            "tool call → %s [%d/%d] args=%s",
            name, self._calls, self._max_calls, _summarize_tool_args(arguments),
        )
        started = time.perf_counter()
        result = await self._session.call_tool(name, arguments, *args, **kwargs)
        logger.info(
            "tool call ✓ %s [%d/%d] latency=%dms is_error=%s content_blocks=%d",
            name,
            self._calls,
            self._max_calls,
            int((time.perf_counter() - started) * 1000),
            getattr(result, "isError", None),
            len(getattr(result, "content", None) or []),
        )
        return result


@traced(name="claude-mcp-research", as_type="generation")
async def research_with_local_mcp(
    *,
    system: str,
    user: str,
    mcp_url: str,
    headers: dict[str, str] | None = None,
    model: str | None = None,
    max_tokens: int = 2048,
    allowed_tools: frozenset[str] | None = None,
    url_allowlist: frozenset[str] | None = None,
    max_tool_calls: int = 25,
) -> str:
    """Free-text call backed by a SELF-HOSTED MCP server (e.g. docs-mcp-server).

    The backend is the MCP client here: it connects to `mcp_url` over streamable
    HTTP, lists the server's tools, exposes them to Claude via the SDK tool runner,
    lets Claude call them to research, and returns the concatenated text of its final
    answer. Because the tool round trips happen in-process (not via Anthropic's
    server-side connector), the MCP server can live on localhost / inside the compose
    network — unreachable from Anthropic's cloud.

    Unlike `call_structured`, this does NOT force a tool. Callers wrap it in
    try/except + a timeout: any failure (server down, `mcp` not installed, tool
    error) should degrade gracefully rather than break the flow.
    """
    import inspect as _inspect

    from anthropic.lib.tools.mcp import async_mcp_tool
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    client = _get_client()
    use_model = model or get_settings().anthropic_model
    started = time.perf_counter()

    async with streamablehttp_client(mcp_url, headers=headers or None) as streams:
        # v1 yields (read, write, get_session_id); v2 yields (read, write).
        read, write = streams[0], streams[1]
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools_result = await session.list_tools()
            # Only expose allowlisted tools to the model (defense in depth), and route every call
            # through the SSRF/budget guard before it reaches the server.
            exposed = [
                t for t in tools_result.tools if allowed_tools is None or t.name in allowed_tools
            ]
            logger.info(
                "local mcp research → model=%s exposed_tools=%s budget=%d",
                use_model, [t.name for t in exposed], max_tool_calls,
            )
            guarded = _GuardedMcpSession(
                session,
                allowed_tools=allowed_tools,
                url_allowlist=url_allowlist,
                max_calls=max_tool_calls,
            )
            runner = client.beta.messages.tool_runner(
                model=use_model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                # _GuardedMcpSession duck-types ClientSession (delegates everything but call_tool).
                tools=[async_mcp_tool(t, guarded) for t in exposed],  # type: ignore[arg-type]
            )
            if _inspect.isawaitable(runner):
                runner = await runner
            # The runner drives the tool loop; the LAST assistant message it yields
            # (stop_reason=end_turn, no tool_use) carries the final prose answer.
            last = None
            async for message in runner:
                last = message
            text = ""
            if last is not None:
                text = "".join(
                    getattr(b, "text", "")
                    for b in last.content
                    if getattr(b, "type", None) == "text"
                )

    logger.info(
        "local mcp research ✓ model=%s tools=%d latency=%dms out_chars=%d",
        use_model,
        len(exposed),
        int((time.perf_counter() - started) * 1000),
        len(text),
    )
    return text


def call_structured_sync(
    *,
    system: str,
    user: str,
    response_model: type[T],
    tool_name: str = "submit",
    model: str | None = None,
) -> T:
    """Sync convenience wrapper for tests / CLI usage."""
    return asyncio.run(
        call_structured(
            system=system,
            user=user,
            response_model=response_model,
            tool_name=tool_name,
            model=model,
        )
    )


def render_context_block(parsed: dict[str, Any], extras: dict[str, Any] | None = None) -> str:
    """Render the parsed context as a compact JSON block for inclusion in prompts."""
    payload = {**parsed, **(extras or {})}
    return "```json\n" + json.dumps(payload, indent=2, default=str) + "\n```"
