"""Verify that the @traced decorator is a no-op when Langfuse env keys are absent."""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture(autouse=True)
def _clear_langfuse_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    # Reset cached settings + wrapper state by reloading the modules.
    import config
    import observability.langfuse_wrapper as lw

    importlib.reload(config)
    importlib.reload(lw)


def test_traced_with_no_args_returns_function_unchanged() -> None:
    from observability.langfuse_wrapper import traced

    @traced
    def add(a: int, b: int) -> int:
        return a + b

    assert add(2, 3) == 5


def test_traced_with_kwargs_returns_callable_wrapper() -> None:
    from observability.langfuse_wrapper import traced

    @traced(name="my-span", as_type="generation")
    def mul(a: int, b: int) -> int:
        return a * b

    assert mul(4, 5) == 20


def test_traced_preserves_async_behavior() -> None:
    import asyncio

    from observability.langfuse_wrapper import traced

    @traced(name="async-span")
    async def afn(x: int) -> int:
        return x + 1

    assert asyncio.run(afn(41)) == 42


def test_traced_async_function_remains_coroutine_function() -> None:
    """LangGraph relies on inspect.iscoroutinefunction() to know whether to await
    a node. The no-op decorator must NOT downgrade an async fn to a sync wrapper.
    """
    import inspect

    from observability.langfuse_wrapper import traced

    @traced(name="async-span")
    async def anode(state: dict) -> dict:
        return {"updated": True}

    assert inspect.iscoroutinefunction(anode), (
        "traced(...) wrapper must preserve coroutine-function status; "
        "LangGraph won't await it otherwise"
    )
