from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient


def _client() -> TestClient:
    from main import app

    return TestClient(app)


def test_health_endpoint_returns_ok() -> None:
    with _client() as c:
        r = c.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok", "service": "ai-sdlc-estimator"}


def test_get_unknown_estimate_returns_404(monkeypatch) -> None:
    import db.postgres_adapter as postgres_adapter

    # Force the disabled path so there's no history fallback even if a real Postgres
    # is listening locally — otherwise the lookup could resolve a stored estimate.
    postgres_adapter._reset_for_tests()
    monkeypatch.setattr(postgres_adapter, "get_sessionmaker", lambda: None)
    with _client() as c:
        r = c.get("/estimates/does-not-exist")
        assert r.status_code == 404


def test_history_endpoint_empty_when_postgres_disabled(monkeypatch) -> None:
    import db.postgres_adapter as postgres_adapter

    # Force disabled regardless of host env so the history list is deterministically
    # empty (a live Postgres could otherwise return previously-persisted rows).
    postgres_adapter._reset_for_tests()
    monkeypatch.setattr(postgres_adapter, "get_sessionmaker", lambda: None)
    with _client() as c:
        r = c.get("/estimates/history")
        assert r.status_code == 200
        assert r.json() == {"items": [], "total": 0}


def test_create_estimate_rejects_oversized_raw_input() -> None:
    # raw_input is capped at max_length=20000 to keep arbitrarily large text out of
    # LLM prompts / storage; the request should be rejected before any background run.
    with _client() as c:
        r = c.post("/estimates", json={"raw_input": "x" * 20001})
        assert r.status_code == 422


def test_create_estimate_accepts_raw_input_at_max_length(monkeypatch) -> None:
    # The boundary value (exactly 20000 chars) must still validate. Stub out the
    # background run so we only exercise request validation + envelope creation.
    import runtime

    spawned: list = []

    def _swallow(coro, *a, **k) -> None:
        spawned.append(coro)
        coro.close()  # discard the un-awaited coroutine cleanly

    monkeypatch.setattr(runtime, "_spawn_background", _swallow)
    with _client() as c:
        r = c.post("/estimates", json={"raw_input": "x" * 20000})
        assert r.status_code == 200
    # Assert creation actually routed through runtime._spawn_background — otherwise a refactor
    # that inlines asyncio.create_task would silently leak a real background run (and a
    # non-awaited-coroutine warning) instead of failing this test.
    assert len(spawned) == 1


def test_submit_answers_for_unknown_estimate_returns_404() -> None:
    with _client() as c:
        r = c.post(
            "/estimates/does-not-exist/answers",
            json={"answers": {}, "skip_remaining": True},
        )
        assert r.status_code == 404


def test_stream_unknown_estimate_returns_404() -> None:
    with _client() as c:
        r = c.get("/estimates/does-not-exist/stream")
        assert r.status_code == 404


def test_delete_estimate_returns_204_and_is_idempotent(monkeypatch) -> None:
    import db.postgres_adapter as postgres_adapter

    # Postgres disabled: the delete still succeeds (in-memory pop + repo no-op) and is
    # idempotent — deleting an unknown id returns 204 with an empty body.
    postgres_adapter._reset_for_tests()
    monkeypatch.setattr(postgres_adapter, "get_sessionmaker", lambda: None)
    with _client() as c:
        r = c.delete("/estimates/does-not-exist")
        assert r.status_code == 204
        assert r.content == b""


def test_remove_estimate_pops_all_registries() -> None:
    from datetime import UTC, datetime

    import runtime
    from models.project_schema import EstimateEnvelope, EstimateStatus

    saved_env = dict(runtime._envelopes)
    saved_streams = dict(runtime._event_streams)
    saved_usage = dict(runtime._llm_usage)
    try:
        runtime._envelopes["x"] = EstimateEnvelope(
            estimate_id="x",
            project_name="x",
            status=EstimateStatus.COMPLETED,
            created_at=datetime.now(UTC),
        )
        runtime._event_streams["x"] = runtime._EventBroker()
        runtime._llm_usage["x"] = []

        runtime.remove_estimate("x")

        assert "x" not in runtime._envelopes
        assert "x" not in runtime._event_streams
        assert "x" not in runtime._llm_usage
        runtime.remove_estimate("x")  # idempotent — unknown id doesn't raise
    finally:
        runtime._envelopes.clear()
        runtime._envelopes.update(saved_env)
        runtime._event_streams.clear()
        runtime._event_streams.update(saved_streams)
        runtime._llm_usage.clear()
        runtime._llm_usage.update(saved_usage)


def test_staffing_coefficients_admin_get_and_validation() -> None:
    with _client() as c:
        r = c.get("/admin/staffing-coefficients")
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body["editable"], bool)
        keys = {row["key"] for row in body["coefficients"]}
        assert keys == {
            "link_cost",
            "free_team_size",
            "overhead_cap",
            "diminishing_returns_exponent",
        }
        for row in body["coefficients"]:
            assert row["min_value"] <= row["value"] <= row["max_value"]
        # Out-of-range and unknown keys are rejected (422).
        bad = c.put(
            "/admin/staffing-coefficients",
            json={"coefficients": [{"key": "diminishing_returns_exponent", "value": 5.0}]},
        )
        assert bad.status_code == 422
        unknown = c.put(
            "/admin/staffing-coefficients",
            json={"coefficients": [{"key": "nope", "value": 1.0}]},
        )
        assert unknown.status_code == 422


# --- Event broker: fan-out + replay buffer durability guarantee ---------------


async def test_broker_replays_backlog_to_late_subscriber() -> None:
    from runtime import _EventBroker

    broker = _EventBroker()
    await broker.publish({"event": "status", "data": "{}"})
    await broker.publish({"event": "questions", "data": "{}"})

    # A subscriber that joins late still sees the full backlog via broker.history.
    q = broker.subscribe()
    backlog = list(broker.history)
    assert [e["event"] for e in backlog] == ["status", "questions"]

    # Live events published after subscribing land on the queue (disjoint from backlog).
    await broker.publish({"event": "final", "data": "{}"})
    live = await asyncio.wait_for(q.get(), timeout=1)
    assert live["event"] == "final"


async def test_broker_fans_out_to_multiple_subscribers() -> None:
    from runtime import _EventBroker

    broker = _EventBroker()
    q1 = broker.subscribe()
    q2 = broker.subscribe()
    await broker.publish({"event": "final", "data": "{}"})
    # Both subscribers receive a copy — no event stealing by a single consumer.
    assert (await asyncio.wait_for(q1.get(), timeout=1))["event"] == "final"
    assert (await asyncio.wait_for(q2.get(), timeout=1))["event"] == "final"


async def test_broker_history_is_bounded() -> None:
    from runtime import _EventBroker

    broker = _EventBroker()
    for _ in range(_EventBroker._MAX_HISTORY + 50):
        await broker.publish({"event": "status", "data": "{}"})
    assert len(broker.history) == _EventBroker._MAX_HISTORY


async def test_broker_drops_slow_subscriber_instead_of_blocking() -> None:
    """A subscriber that never drains is dropped once its bounded queue fills, so
    publish stays non-blocking and other subscribers keep receiving events."""
    import asyncio

    from runtime import _EventBroker

    broker = _EventBroker()
    slow = broker.subscribe()  # never drained → overflows and is dropped
    fast = broker.subscribe()  # drained each tick → stays subscribed

    for _ in range(_EventBroker._MAX_QUEUE + 5):
        await broker.publish({"event": "status", "data": "{}"})  # must not block
        try:
            fast.get_nowait()
        except asyncio.QueueEmpty:
            pass

    assert slow not in broker.subscribers  # dropped on QueueFull
    assert fast in broker.subscribers  # kept up, still subscribed


# --- Bounded eviction of the in-memory registries -----------------------------


def test_eviction_drops_oldest_completed_but_keeps_in_flight() -> None:
    from datetime import UTC, datetime

    import runtime
    from models.project_schema import EstimateEnvelope, EstimateStatus

    saved_env = dict(runtime._envelopes)
    saved_streams = dict(runtime._event_streams)
    saved_usage = dict(runtime._llm_usage)
    saved_cap = runtime._MAX_RETAINED_ESTIMATES
    try:
        runtime._envelopes.clear()
        runtime._event_streams.clear()
        runtime._llm_usage.clear()
        runtime._MAX_RETAINED_ESTIMATES = 3

        def _mk(eid: str, status: EstimateStatus) -> None:
            runtime._envelopes[eid] = EstimateEnvelope(
                estimate_id=eid,
                project_name=eid,
                status=status,
                created_at=datetime.now(UTC),
            )
            runtime._event_streams[eid] = runtime._EventBroker()
            runtime._llm_usage[eid] = []

        # Oldest is completed, then one in-flight, then more completed -> over cap.
        _mk("old-completed", EstimateStatus.COMPLETED)
        _mk("running", EstimateStatus.PASS_1_RUNNING)
        _mk("c1", EstimateStatus.COMPLETED)
        _mk("c2", EstimateStatus.FAILED)

        runtime._evict_if_over_capacity()

        assert "old-completed" not in runtime._envelopes  # oldest evictable dropped
        assert "running" in runtime._envelopes  # in-flight never evicted
        assert "old-completed" not in runtime._event_streams
        assert "old-completed" not in runtime._llm_usage
        assert len(runtime._envelopes) == runtime._MAX_RETAINED_ESTIMATES
    finally:
        runtime._envelopes.clear()
        runtime._envelopes.update(saved_env)
        runtime._event_streams.clear()
        runtime._event_streams.update(saved_streams)
        runtime._llm_usage.clear()
        runtime._llm_usage.update(saved_usage)
        runtime._MAX_RETAINED_ESTIMATES = saved_cap


# --- Background-task tracking --------------------------------------------------


async def test_spawn_background_logs_and_discards_on_failure(caplog) -> None:
    import logging

    import runtime

    async def _boom() -> None:
        raise ValueError("kaboom")

    with caplog.at_level(logging.ERROR):
        task = runtime._spawn_background(_boom(), label="unit-test")
        assert task in runtime._background_tasks
        with pytest.raises(ValueError):
            await task
        # done-callback runs on the loop after the task completes.
        await asyncio.sleep(0)

    assert task not in runtime._background_tasks  # discarded from the retained set
    assert any("unit-test" in r.getMessage() for r in caplog.records)


# --- persist_completed_estimate calibration refresh iterates the Phase enum ---


def test_persist_refreshes_calibration_for_every_phase(monkeypatch) -> None:
    from datetime import UTC, datetime

    import runtime
    from models.project_schema import EstimateEnvelope, EstimateStatus
    from models.twin_outputs import Phase

    refreshed: list[str] = []

    async def _fake_refresh(phase_value: str) -> None:
        refreshed.append(phase_value)

    async def _fake_save_history(*args, **kwargs) -> None:
        return None

    async def _fake_save_envelope(*args, **kwargs) -> None:  # awaited in persist_completed_estimate
        return None

    monkeypatch.setattr(runtime, "refresh_calibration_for_phase", _fake_refresh)
    monkeypatch.setattr(runtime, "save_estimate_history", _fake_save_history)
    monkeypatch.setattr(runtime, "save_estimate_envelope", _fake_save_envelope)

    env = EstimateEnvelope(
        estimate_id="e1",
        project_name="p",
        status=EstimateStatus.COMPLETED,
        created_at=datetime.now(UTC),
    )
    asyncio.run(runtime.persist_completed_estimate(env, raw_input="", stage2=None, stage3=None))

    assert sorted(refreshed) == sorted(p.value for p in Phase)
