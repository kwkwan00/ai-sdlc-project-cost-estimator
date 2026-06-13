"""The LangGraph checkpoint serializer must register our state models on its
msgpack allowlist, so resuming a checkpoint reconstructs real Pydantic instances
instead of warning (and, in a future LangGraph, blocking → silent raw dicts).
"""

from __future__ import annotations

from db.neo4j_adapter import _checkpoint_serde
from models.project_schema import Stage2Context, Stage3Context
from models.twin_outputs import ClarifyingQuestion, Phase


def test_allowlist_covers_state_models() -> None:
    allow = _checkpoint_serde()._allowed_msgpack_modules
    # An explicit set (strict mode), not the permissive ``True`` default.
    assert isinstance(allow, set)
    for key in [
        ("models.twin_outputs", "ClarifyingQuestion"),
        ("models.twin_outputs", "PhaseEstimate"),
        ("models.twin_outputs", "Phase"),
        ("models.twin_outputs", "RoleCategory"),
        ("models.twin_outputs", "RoleSeniority"),
        ("models.project_schema", "Stage2Context"),
        ("models.project_schema", "Stage3Context"),
        ("models.project_schema", "AiToolingLevel"),
        ("models.project_schema", "CodebaseContext"),
    ]:
        assert key in allow, f"{key} missing from checkpoint allowlist"


def test_custom_state_type_round_trips_to_model_not_dict() -> None:
    """If a type were blocked, the serde would return raw data (a dict), not the
    model — and downstream attribute access would break. Assert reconstruction."""
    serde = _checkpoint_serde()
    q = ClarifyingQuestion(
        id="q1",
        text="How many integrations?",
        source_phases=[Phase.DEVELOPMENT],
        suggested_default="3",
        impact_hours=100,
    )
    restored = serde.loads_typed(serde.dumps_typed(q))
    assert isinstance(restored, ClarifyingQuestion)  # not a raw dict → not blocked
    assert restored.text == q.text
    assert restored.source_phases == [Phase.DEVELOPMENT]


def test_nested_state_models_round_trip() -> None:
    serde = _checkpoint_serde()
    for obj in (Stage2Context(), Stage3Context()):
        restored = serde.loads_typed(serde.dumps_typed(obj))
        assert type(restored) is type(obj)
    # An enum nested deep in Stage 3 survives as the enum, not a bare string.
    s3 = serde.loads_typed(serde.dumps_typed(Stage3Context()))
    assert s3.ai_tooling.development.value == "none"
