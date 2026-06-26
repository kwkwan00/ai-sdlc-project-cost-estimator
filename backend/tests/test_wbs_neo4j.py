"""WBS Neo4j persistence: the never-raise contract when the driver is unavailable.

Live-DB behavior is exercised manually (see the plan's Neo4j Browser queries); these tests
pin the degrade path — with no driver, every read returns the empty case and every write
no-ops, none raise — and the flatten parent-pointer shape the Cypher relies on.

The adapter is async (neo4j AsyncGraphDatabase), so the fakes below are async context managers
with awaitable run/single/execute_write, and `get_driver` is monkeypatched with an async stub.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import db.neo4j_adapter as adapter
from models.twin_outputs import Phase
from models.wbs_task import WbsTaskInput, flatten_tree


def _tree() -> list[WbsTaskInput]:
    return [
        WbsTaskInput(
            id="p1",
            name="Pkg",
            children=[
                WbsTaskInput(
                    id="l1", name="Leaf", phase=Phase.DEVELOPMENT, role_id="sr_engineer",
                    optimistic=1, most_likely=2, pessimistic=3,
                )
            ],
        )
    ]


def _async_returning(value: Any) -> Callable[[], Awaitable[Any]]:
    """An async stub for monkeypatching the async ``get_driver`` (returns ``value`` when awaited)."""

    async def _get_driver() -> Any:
        return value

    return _get_driver


async def test_writes_noop_and_reads_empty_when_driver_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(adapter, "get_driver", _async_returning(None))
    tasks = flatten_tree(_tree(), "d1")

    # None of these may raise; reads return the empty case.
    await adapter.save_wbs_draft({"draft_id": "d1", "tasks": tasks})
    await adapter.save_wbs_tree("e1", tasks)
    await adapter.delete_wbs_draft("d1")
    assert await adapter.load_wbs_draft("d1") is None
    assert await adapter.list_wbs_drafts() == []


def test_flatten_tree_parent_pointers() -> None:
    rows = flatten_tree(_tree(), "owner")
    by_id = {r["task_id"]: r for r in rows}
    # Top-level package points at the owner; the leaf points at its package; order set.
    assert by_id["p1"]["parent_id"] == "owner"
    assert by_id["p1"]["is_leaf"] is False
    assert by_id["l1"]["parent_id"] == "p1"
    assert by_id["l1"]["is_leaf"] is True
    assert by_id["l1"]["phase"] == "development"
    assert by_id["l1"]["most_likely"] == 2


def test_iso_helper_handles_none_and_str() -> None:
    assert adapter._iso(None) is None
    assert adapter._iso("2026-06-18T00:00:00") == "2026-06-18T00:00:00"


# --- atomicity (managed-transaction) + empty-draft guard -----------------------------------


class _FakeTx:
    """Records the statements run inside one managed transaction (awaitable run)."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    async def run(self, cypher: str, **_: object) -> None:
        self.statements.append(cypher)


class _FakeSession:
    def __init__(self, recorder: dict) -> None:
        self._recorder = recorder

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def execute_write(self, fn, *args, **kwargs):  # type: ignore[no-untyped-def]
        # Every multi-statement write MUST go through a single managed transaction so it is
        # all-or-nothing; assert the unit-of-work runs its statements on one tx handle.
        self._recorder["execute_write_calls"] += 1
        tx = _FakeTx()
        result = await fn(tx, *args, **kwargs)
        self._recorder["tx_statements"].append(tx.statements)
        return result

    async def run(self, *_a: object, **_k: object) -> None:  # pragma: no cover - must NOT be used
        raise AssertionError("write path must use execute_write, not session.run (auto-commit)")


class _FakeDriver:
    def __init__(self, recorder: dict) -> None:
        self._recorder = recorder

    def session(self, **_: object) -> _FakeSession:
        return _FakeSession(self._recorder)


async def test_save_wbs_draft_uses_single_managed_transaction(monkeypatch) -> None:
    recorder = {"execute_write_calls": 0, "tx_statements": []}
    monkeypatch.setattr(adapter, "get_driver", _async_returning(_FakeDriver(recorder)))
    tasks = flatten_tree(_tree(), "d1")

    await adapter.save_wbs_draft({"draft_id": "d1", "tasks": tasks})

    # Exactly one transaction; all five statements (MERGE draft, DELETE old, CREATE+SET,
    # task→task link, draft→top-level link) ran on that one tx — so a mid-write failure rolls
    # back the owner node too.
    assert recorder["execute_write_calls"] == 1
    assert len(recorder["tx_statements"]) == 1
    assert len(recorder["tx_statements"][0]) == 5


async def test_save_wbs_tree_uses_single_managed_transaction(monkeypatch) -> None:
    recorder = {"execute_write_calls": 0, "tx_statements": []}
    monkeypatch.setattr(adapter, "get_driver", _async_returning(_FakeDriver(recorder)))
    tasks = flatten_tree(_tree(), "e1")

    await adapter.save_wbs_tree("e1", tasks)

    assert recorder["execute_write_calls"] == 1
    # DELETE old, CREATE+SET, task→task link, estimate→top-level link → 4 statements.
    assert len(recorder["tx_statements"][0]) == 4


class _SingleRecordSession:
    """Async session+result that returns one canned record for the load query."""

    def __init__(self, draft: dict | None, tasks: list) -> None:
        self._draft = draft
        self._tasks = tasks

    async def __aenter__(self) -> _SingleRecordSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def run(self, *_a: object, **_k: object) -> _SingleRecordSession:
        return self  # the result is self; the adapter then awaits .single()

    async def single(self) -> dict | None:
        if self._draft is None:
            return None
        return {"draft": self._draft, "tasks": self._tasks}


async def test_load_wbs_draft_returns_taskless_draft_with_empty_tree(monkeypatch) -> None:
    # Writes are atomic (save_wbs_draft runs one managed tx), so a draft node with no tasks is a
    # legitimately-emptied draft (the user deleted every task), NOT a partial write. It must load
    # WITH an empty task list — treating it as "not found" would brick a resumable draft. The
    # collect(t) of a task-less draft yields [None]; load_wbs_draft filters the None out.
    class _Driver:
        def session(self, **_: object) -> _SingleRecordSession:
            return _SingleRecordSession({"draft_id": "d1", "project_name": "x"}, [None])

    monkeypatch.setattr(adapter, "get_driver", _async_returning(_Driver()))
    loaded = await adapter.load_wbs_draft("d1")
    assert loaded is not None
    assert loaded["draft_id"] == "d1"
    assert loaded["tasks"] == []


async def test_load_wbs_draft_returns_payload_when_tasks_present(monkeypatch) -> None:
    class _Driver:
        def session(self, **_: object) -> _SingleRecordSession:
            return _SingleRecordSession(
                {"draft_id": "d1", "project_name": "x"},
                [{"task_id": "l1", "parent_id": "d1", "name": "Leaf"}],
            )

    monkeypatch.setattr(adapter, "get_driver", _async_returning(_Driver()))
    loaded = await adapter.load_wbs_draft("d1")
    assert loaded is not None
    assert loaded["draft_id"] == "d1"
    assert len(loaded["tasks"]) == 1


async def test_load_wbs_draft_surfaces_contingency_pct(monkeypatch) -> None:
    # The WBS-only contingency reserve persists on the draft node so it survives resume.
    class _Driver:
        def session(self, **_: object) -> _SingleRecordSession:
            return _SingleRecordSession(
                {"draft_id": "d1", "project_name": "x", "contingency_pct": 25.0}, []
            )

    monkeypatch.setattr(adapter, "get_driver", _async_returning(_Driver()))
    loaded = await adapter.load_wbs_draft("d1")
    assert loaded is not None
    assert loaded["contingency_pct"] == 25.0


async def test_load_wbs_draft_surfaces_llm_usage_json(monkeypatch) -> None:
    # The planner's LLM meta-cost is captured at draft time and persisted on the draft node so the
    # editor can show an LLM-cost icon after a resume (the deterministic rollup spends no tokens).
    blob = '{"call_count": 1, "cost_usd": 0.42, "by_model": []}'

    class _Driver:
        def session(self, **_: object) -> _SingleRecordSession:
            return _SingleRecordSession(
                {"draft_id": "d1", "project_name": "x", "llm_usage_json": blob}, []
            )

    monkeypatch.setattr(adapter, "get_driver", _async_returning(_Driver()))
    loaded = await adapter.load_wbs_draft("d1")
    assert loaded is not None
    assert loaded["llm_usage_json"] == blob
