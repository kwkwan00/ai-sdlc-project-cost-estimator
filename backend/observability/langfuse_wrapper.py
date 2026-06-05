"""Optional Langfuse integration.

If LANGFUSE_PUBLIC_KEY + LANGFUSE_SECRET_KEY are set in env, traces are emitted.
Otherwise the `@traced` decorator is a transparent no-op so the rest of the codebase
doesn't need to branch.
"""

from __future__ import annotations

import functools
import inspect
import logging
from collections.abc import Callable
from typing import Any, TypeVar

from config import get_settings

logger = logging.getLogger(__name__)

_langfuse_client: Any = None
_observe_decorator: Callable[..., Any] | None = None


def _initialize() -> None:
    """Initialize the Langfuse client iff env keys are present.

    Safe to call repeatedly; subsequent calls are no-ops.
    """
    global _langfuse_client, _observe_decorator

    if _observe_decorator is not None:
        return

    settings = get_settings()
    if not settings.langfuse_enabled:
        logger.info("Langfuse disabled (no LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY). Tracing is a no-op.")
        _observe_decorator = _noop_decorator
        return

    try:
        from langfuse import Langfuse, observe

        _langfuse_client = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
        )
        _observe_decorator = observe
        logger.info("Langfuse initialized at %s", settings.langfuse_host)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Langfuse init failed (%s); falling back to no-op tracing", exc)
        _observe_decorator = _noop_decorator


F = TypeVar("F", bound=Callable[..., Any])


def _noop_decorator(*dargs: Any, **dkwargs: Any) -> Any:
    """Drop-in replacement for `@observe(...)` that does nothing.

    Preserves async-ness so that LangGraph's `inspect.iscoroutinefunction()` check
    on decorated node functions returns True for async nodes.
    """
    if dargs and callable(dargs[0]) and not dkwargs:
        return dargs[0]

    def deco(fn: F) -> F:
        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def awrapper(*args: Any, **kwargs: Any) -> Any:
                return await fn(*args, **kwargs)

            return awrapper  # type: ignore[return-value]

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return fn(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return deco


def traced(*dargs: Any, **dkwargs: Any) -> Any:
    """Use exactly like `@observe(...)` from langfuse.

    Works with `@traced` (no args) or `@traced(name="x", as_type="generation")`.
    """
    _initialize()
    assert _observe_decorator is not None
    return _observe_decorator(*dargs, **dkwargs)


def shutdown() -> None:
    if _langfuse_client is not None:
        try:
            _langfuse_client.flush()
        except Exception:  # noqa: BLE001
            pass
