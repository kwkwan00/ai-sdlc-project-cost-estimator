"""Deterministic vendor-name safety guard, assembled from a config file.

The brand names live in ``vendor_generalizations.yaml`` as plain lists. At load time each
``products`` list is **assembled into a single regex alternation string** (longest brand
first so multi-word names win; spaces become ``\\s+`` for flexible whitespace; optional
cloud-provider prefixes folded in), so the guard is tunable by editing the YAML — no code
change. ``generalize_vendor_tech`` then replaces any matched brand in SOW prose with its
capability, so no specific vendor product or cloud service the user didn't ask for appears.

Degrades safely: if the config is missing or malformed, it logs and generalizes nothing
(rather than crashing SOW generation).
"""

from __future__ import annotations

import logging
import re
from functools import cache
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent / "vendor_generalizations.yaml"


def _product_to_regex(product: str) -> str:
    """One brand → a regex fragment: escape each token, join with ``\\s+`` so wrapping/extra
    whitespace still matches (e.g. "ECS Fargate" → ``ECS\\s+Fargate``)."""
    return r"\s+".join(re.escape(tok) for tok in product.split())


@cache
def _compiled() -> list[tuple[re.Pattern[str], str]]:
    """Assemble the YAML lists into ``(compiled_pattern, capability)`` pairs, in file order."""
    try:
        data = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:  # bundled file; degrade rather than crash a SOW
        logger.warning("vendor-guard config unreadable (%s); skipping vendor generalization", exc)
        return []

    prefixes = [str(p) for p in data.get("cloud_prefixes", []) if str(p).strip()]
    prefix_group = ""
    if prefixes:
        prefix_group = r"(?:(?:" + "|".join(re.escape(p) for p in prefixes) + r")\s+)?"

    compiled: list[tuple[re.Pattern[str], str]] = []
    for entry in data.get("generalizations", []):
        capability = str(entry.get("capability", "")).strip()
        products = [str(p) for p in entry.get("products", []) if str(p).strip()]
        if not capability or not products:
            continue
        # Longest brand first so e.g. "ECS Fargate" matches before "ECS"; assemble into one
        # alternation string — this is the "string assembled from the array list".
        alternation = "|".join(
            _product_to_regex(p) for p in sorted(products, key=len, reverse=True)
        )
        pattern = re.compile(rf"\b{prefix_group}(?:{alternation})\b", re.IGNORECASE)
        compiled.append((pattern, capability))
    return compiled


def generalize_vendor_tech(text: str) -> str:
    """Replace any configured vendor brand in ``text`` with its generic capability."""
    for pattern, capability in _compiled():
        text = pattern.sub(capability, text)
    return text
