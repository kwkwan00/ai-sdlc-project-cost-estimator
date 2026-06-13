"""Golden-case loader.

Cases live as JSON under ``evals/datasets/*.json``; each file is a list of
``EvalCase`` objects. ``load_cases`` reads them all and (optionally) filters by
agent. Kept dead simple — no network, no DB.
"""

from __future__ import annotations

import json
from pathlib import Path

from .models import EvalCase

DATASETS_DIR = Path(__file__).parent / "datasets"


def _load_all() -> list[EvalCase]:
    cases: list[EvalCase] = []
    for path in sorted(DATASETS_DIR.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        for entry in raw:
            cases.append(EvalCase.model_validate(entry))
    return cases


def load_cases(agent: str | None = None) -> list[EvalCase]:
    """Load every golden case, optionally filtered to a single agent."""
    cases = _load_all()
    if agent is not None:
        cases = [c for c in cases if c.agent == agent]
    return cases
