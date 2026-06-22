"""Shared role-id slug helpers (used by both the roster agent and the rate-card admin so the
collision/truncation rule can't drift between the two places that mint role_ids)."""

from __future__ import annotations

import re

# role_id columns are String(64); keep slugs within that. The truncation-aware suffix below
# guarantees a collision suffix never pushes a slug past the cap.
MAX_SLUG_LEN = 64

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    """Lowercase + collapse non-alphanumerics to ``_`` → a role-id-safe slug (idempotent on an
    existing slug). Capped to leave room for a collision suffix."""
    slug = _SLUG_RE.sub("_", text.strip().lower()).strip("_")
    return (slug or "role")[: MAX_SLUG_LEN - 8]


def unique_slug(base: str, used: set[str], *, max_len: int = MAX_SLUG_LEN) -> str:
    """A slug ≤ ``max_len`` not already in ``used``; on collision append ``_2``/``_3``/… with the
    base truncated so the suffix is never lost when it would exceed the cap. Mutates ``used``."""
    base = base[:max_len]
    candidate, n = base, 2
    while candidate in used:
        suffix = f"_{n}"
        candidate = base[: max_len - len(suffix)] + suffix
        n += 1
    used.add(candidate)
    return candidate
