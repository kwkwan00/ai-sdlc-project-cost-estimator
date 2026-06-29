"""Small reusable Pydantic validator factories shared across the model + agent layers.

A leaf module (stdlib-only imports) so both ``models/*`` schemas and ``agents/*`` response models
can share it without a circular import.
"""

from __future__ import annotations

from collections.abc import Callable


def clip_text(limit: int) -> Callable[[object], object]:
    """Truncate an over-long LLM string to ``limit`` chars instead of failing validation.

    The model sometimes ignores the schema's ``maxLength`` — e.g. a rationale a few words over the
    cap — and a slightly-too-long free-text field shouldn't sink the whole structured-output call,
    which forces a retry and, if that also overshoots, a fallback. We keep ``max_length`` in the
    schema as a hint to the model and clip as the safety net (use as a ``before`` validator, so it
    runs ahead of the length constraint)."""

    def _truncate(value: object) -> object:
        return value[:limit] if isinstance(value, str) else value

    return _truncate


def coerce_pert_ordering(
    optimistic: float, most_likely: float, pessimistic: float
) -> tuple[float, float, float]:
    """Repair a malformed three-point estimate to ``(min, mid, max)`` so optimistic ≤ most_likely ≤
    pessimistic holds. Shared by the WBS response models' ``@model_validator(mode="after")`` (repair,
    don't raise). ``HourRange`` keeps its own variant — it additionally clamps + logs a warning."""
    lo, mid, hi = sorted((optimistic, most_likely, pessimistic))
    return lo, mid, hi
