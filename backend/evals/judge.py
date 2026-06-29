"""LLM-as-judge client for the eval harness.

The judge defaults to OpenAI **GPT-5.5** (``OPENAI_MODEL_EVAL``), reached through the
OpenAI SDK's structured-output helper ``chat.completions.parse`` so the verdict comes
back as a validated Pydantic instance — the OpenAI analogue of the twins' forced
tool-use. Using a *different* provider to grade the Anthropic twins also reduces
same-model self-preference bias.

``judge_structured`` dispatches by model name, so pointing the harness at an Anthropic
model (``--judge-model claude-...``) still works and transparently reuses the production
``orchestrator.llm.call_structured`` plumbing.

Judge token spend is intentionally NOT recorded onto ``orchestrator.usage`` — eval cost
is kept separate from the per-estimate production cost (see ``evals/runner.py``).
"""

from __future__ import annotations

import logging

from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel

# Shared with the production path — one OpenAI client getter + model-prefix check (a `claude-*`
# judge-model routes to the Anthropic `call_structured` fallback below). Imported (not re-defined) so
# there's a single copy; tests still `monkeypatch.setattr(judge, "_get_openai_client", ...)`.
from orchestrator.llm import _get_openai_client, _is_openai_model, call_structured

logger = logging.getLogger(__name__)


async def judge_structured[T: BaseModel](
    *,
    system: str,
    user: str,
    response_model: type[T],
    model: str,
    tool_name: str = "submit_evaluation",
) -> T:
    """Return a parsed ``response_model`` verdict from judge ``model``.

    Provider-aware: OpenAI models use the SDK's strict structured-output
    ``chat.completions.parse``; Anthropic models fall back to ``call_structured``
    (forced tool-use). Reasoning models reject a custom ``temperature``, so none is
    sent — the model default is used. Raises on an API error or a model refusal; the
    caller (``rubrics._judge_one``) captures it into a ``RubricScore``.
    """
    if not _is_openai_model(model):
        return await call_structured(
            system=system,
            user=user,
            response_model=response_model,
            tool_name=tool_name,
            model=model,
        )

    client = _get_openai_client()
    messages: list[ChatCompletionMessageParam] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    completion = await client.chat.completions.parse(
        model=model,
        messages=messages,
        response_format=response_model,
    )
    message = completion.choices[0].message
    if message.parsed is None:
        raise RuntimeError(
            f"judge model={model} returned no parsed verdict (refusal={message.refusal!r})"
        )
    return message.parsed
