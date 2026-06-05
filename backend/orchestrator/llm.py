"""Anthropic SDK wrapper that returns Pydantic-typed structured output.

Pattern: define the response shape as a Pydantic model, expose it to Claude as a
single tool, force `tool_choice` to that tool, then parse the tool-use block back
into the model. Faster + more reliable than JSON-mode prompting.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, TypeVar

from anthropic import AsyncAnthropic
from pydantic import BaseModel

from config import get_settings
from observability.langfuse_wrapper import traced

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

_client: AsyncAnthropic | None = None


def _get_client() -> AsyncAnthropic:
    global _client
    if _client is None:
        settings = get_settings()
        if not settings.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        _client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _client


def _pydantic_to_tool_schema(model: type[BaseModel], name: str) -> dict[str, Any]:
    """Convert a Pydantic model to an Anthropic tool definition."""
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
) -> T:
    """Call Claude and return a parsed instance of `response_model`.

    Forces tool-use so we get a JSON object matching the model's schema.
    """
    settings = get_settings()
    client = _get_client()
    use_model = model or settings.anthropic_model
    tool = _pydantic_to_tool_schema(response_model, tool_name)

    user_blocks: list[dict[str, Any]] = [{"type": "text", "text": user}]
    if extra_user_blocks:
        user_blocks.extend(extra_user_blocks)

    response = await client.messages.create(
        model=use_model,
        max_tokens=max_tokens,
        temperature=temperature,
        system=system,
        tools=[tool],
        tool_choice={"type": "tool", "name": tool_name},
        messages=[{"role": "user", "content": user_blocks}],
    )

    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == tool_name:
            return response_model.model_validate(block.input)

    raise RuntimeError(
        f"Claude did not return tool_use for {tool_name}. "
        f"Stop reason: {response.stop_reason}. Content types: "
        f"{[getattr(b, 'type', '?') for b in response.content]}"
    )


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
