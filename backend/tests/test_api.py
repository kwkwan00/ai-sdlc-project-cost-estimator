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


def test_get_unknown_estimate_returns_404() -> None:
    import db.postgres_adapter as postgres_adapter

    postgres_adapter._reset_for_tests()  # no history fallback when Postgres is off
    with _client() as c:
        r = c.get("/estimates/does-not-exist")
        assert r.status_code == 404


def test_history_endpoint_empty_when_postgres_disabled() -> None:
    import db.postgres_adapter as postgres_adapter

    postgres_adapter._reset_for_tests()
    with _client() as c:
        r = c.get("/estimates/history")
        assert r.status_code == 200
        assert r.json() == []


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


# --- Event broker: fan-out + replay buffer durability guarantee ---------------


async def test_broker_replays_backlog_to_late_subscriber() -> None:
    from main import _EventBroker

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
    from main import _EventBroker

    broker = _EventBroker()
    q1 = broker.subscribe()
    q2 = broker.subscribe()
    await broker.publish({"event": "final", "data": "{}"})
    # Both subscribers receive a copy — no event stealing by a single consumer.
    assert (await asyncio.wait_for(q1.get(), timeout=1))["event"] == "final"
    assert (await asyncio.wait_for(q2.get(), timeout=1))["event"] == "final"


async def test_broker_history_is_bounded() -> None:
    from main import _EventBroker

    broker = _EventBroker()
    for _ in range(_EventBroker._MAX_HISTORY + 50):
        await broker.publish({"event": "status", "data": "{}"})
    assert len(broker.history) == _EventBroker._MAX_HISTORY


async def test_broker_drops_slow_subscriber_instead_of_blocking() -> None:
    """A subscriber that never drains is dropped once its bounded queue fills, so
    publish stays non-blocking and other subscribers keep receiving events."""
    import asyncio

    from main import _EventBroker

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

    import main
    from models.project_schema import EstimateEnvelope, EstimateStatus

    saved_env = dict(main._envelopes)
    saved_streams = dict(main._event_streams)
    saved_usage = dict(main._llm_usage)
    saved_cap = main._MAX_RETAINED_ESTIMATES
    try:
        main._envelopes.clear()
        main._event_streams.clear()
        main._llm_usage.clear()
        main._MAX_RETAINED_ESTIMATES = 3

        def _mk(eid: str, status: EstimateStatus) -> None:
            main._envelopes[eid] = EstimateEnvelope(
                estimate_id=eid,
                project_name=eid,
                status=status,
                created_at=datetime.now(UTC),
            )
            main._event_streams[eid] = main._EventBroker()
            main._llm_usage[eid] = []

        # Oldest is completed, then one in-flight, then more completed -> over cap.
        _mk("old-completed", EstimateStatus.COMPLETED)
        _mk("running", EstimateStatus.PASS_1_RUNNING)
        _mk("c1", EstimateStatus.COMPLETED)
        _mk("c2", EstimateStatus.FAILED)

        main._evict_if_over_capacity()

        assert "old-completed" not in main._envelopes  # oldest evictable dropped
        assert "running" in main._envelopes  # in-flight never evicted
        assert "old-completed" not in main._event_streams
        assert "old-completed" not in main._llm_usage
        assert len(main._envelopes) == main._MAX_RETAINED_ESTIMATES
    finally:
        main._envelopes.clear()
        main._envelopes.update(saved_env)
        main._event_streams.clear()
        main._event_streams.update(saved_streams)
        main._llm_usage.clear()
        main._llm_usage.update(saved_usage)
        main._MAX_RETAINED_ESTIMATES = saved_cap


# --- Background-task tracking --------------------------------------------------


async def test_spawn_background_logs_and_discards_on_failure(caplog) -> None:
    import logging

    import main

    async def _boom() -> None:
        raise ValueError("kaboom")

    with caplog.at_level(logging.ERROR):
        task = main._spawn_background(_boom(), label="unit-test")
        assert task in main._background_tasks
        with pytest.raises(ValueError):
            await task
        # done-callback runs on the loop after the task completes.
        await asyncio.sleep(0)

    assert task not in main._background_tasks  # discarded from the retained set
    assert any("unit-test" in r.getMessage() for r in caplog.records)


# --- _persist calibration refresh iterates the Phase enum ---------------------


def test_persist_refreshes_calibration_for_every_phase(monkeypatch) -> None:
    from datetime import UTC, datetime

    import main
    from models.project_schema import EstimateEnvelope, EstimateStatus
    from models.twin_outputs import Phase

    refreshed: list[str] = []

    async def _fake_refresh(phase_value: str) -> None:
        refreshed.append(phase_value)

    async def _fake_save_history(*args, **kwargs) -> None:
        return None

    monkeypatch.setattr(main, "refresh_calibration_for_phase", _fake_refresh)
    monkeypatch.setattr(main, "save_estimate_history", _fake_save_history)
    monkeypatch.setattr(main, "save_estimate_envelope", lambda *a, **k: None)

    env = EstimateEnvelope(
        estimate_id="e1",
        project_name="p",
        status=EstimateStatus.COMPLETED,
        created_at=datetime.now(UTC),
    )
    asyncio.run(main._persist(env, "", stage2=None, stage3=None))

    assert sorted(refreshed) == sorted(p.value for p in Phase)
