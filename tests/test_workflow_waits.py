"""
tests/test_workflow_waits.py — `wait_until` and `wait_for_event`.

Same durability contract as `wait` (`test_workflow_handlers.py`): a node
that pauses has to come back from a *process restart*, not only from
another `advance()` call in the same process — so several tests here build
a brand new `WorkflowEngine`/`WorkflowScheduler` on the same store to prove
the second pass is reading rows, not memory.
"""
from __future__ import annotations

import os
import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database as db_mod
from core.database import Base
from src.contracts import WorkflowDefinition
from src.workflows import WorkflowEngine, WorkflowStore, default_handlers
from src.workflows.scheduler import WorkflowScheduler


@pytest.fixture()
def store(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "wfw.db").as_posix()
    engine = create_engine(url, connect_args={"check_same_thread": False})
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal",
                        sessionmaker(autocommit=False, autoflush=False, bind=engine))
    Base.metadata.create_all(bind=engine)
    yield WorkflowStore()
    engine.dispose()


def wf(*nodes, wid="waits.flow"):
    return WorkflowDefinition.parse(
        {"id": wid, "version": "1.0.0", "title": "waits", "nodes": list(nodes)})


# ── wait_until ──────────────────────────────────────────────────────────

def test_wait_until_completes_once_the_condition_is_true(store):
    d = wf(
        {"id": "hold", "type": "wait_until",
         "config": {"when": {"left": {"path": "inputs.ready"}, "op": "eq", "right": True},
                    "timeout_seconds": 3600, "interval_seconds": 5}},
        {"id": "send", "type": "deliver", "needs": ["hold"], "config": {"to": "a@b.c"}},
    )
    sent = []
    engine = WorkflowEngine(default_handlers(deliver=lambda p, c: sent.append(1) or {}), store)
    run_id = store.create_run(d, inputs={"ready": False})["run_id"]

    first = engine.advance(run_id)
    assert first["status"] == "paused"
    assert first["waiting_on"] == "hold"
    assert sent == []

    # The condition still isn't true: still waiting, nothing sent.
    assert engine.advance(run_id)["status"] == "paused"
    assert sent == []

    # A fresh engine on the same store (simulating a restarted worker) reads
    # the persisted deadline rather than recomputing it, and completes once
    # the underlying input actually changes.
    run2 = store.get_run(run_id)
    inputs2 = dict(run2["run"].inputs)
    inputs2["ready"] = True
    from core.database import SessionLocal, WorkflowRunRow
    with SessionLocal() as db:
        row = db.query(WorkflowRunRow).filter(WorkflowRunRow.id == run_id).one()
        import json
        row.inputs_json = json.dumps(inputs2)
        db.commit()
    # Force the node to be reopened as if the clock had ticked (same trick
    # `test_clock_wait_continues_after_scheduler_restart` uses for `wait`).
    store.finish_node(run_id, "hold", status="paused",
                      result={**store.node_runs(run_id)["hold"].result, "wake_at": "2000-01-01T00:00:00Z"})
    out = WorkflowEngine(default_handlers(deliver=lambda p, c: sent.append(1) or {}), store).advance(run_id)

    assert out["status"] == "completed"
    assert sent == [1]


def test_wait_until_timeout_fails_by_default(store):
    d = wf({"id": "hold", "type": "wait_until",
            "config": {"when": {"left": {"path": "inputs.ready"}, "op": "truthy"},
                       "timeout_seconds": 1, "interval_seconds": 1}})
    run_id = store.create_run(d, inputs={"ready": False})["run_id"]
    engine = WorkflowEngine(default_handlers(), store)
    assert engine.advance(run_id)["status"] == "paused"

    # Push the deadline into the past — the equivalent of "the clock caught
    # up" a normal test would otherwise sleep 1s for.
    result = dict(store.node_runs(run_id)["hold"].result)
    result["deadline"] = "2000-01-01T00:00:00Z"
    store.finish_node(run_id, "hold", status="paused", result={**result, "wake_at": "2000-01-01T00:00:00Z"})

    out = engine.advance(run_id)
    assert out["status"] == "failed"
    assert "timed out" in out["ran"][0]["reason"]


def test_wait_until_timeout_takes_the_on_timeout_branch_instead_of_failing(store):
    d = wf(
        {"id": "hold", "type": "wait_until",
         "config": {"when": {"left": {"path": "inputs.ready"}, "op": "truthy"},
                    "timeout_seconds": 1, "interval_seconds": 1, "on_timeout": True}},
        {"id": "timed_out", "type": "condition", "needs": ["hold"],
         "config": {"when": {"left": {"path": "results.hold.timed_out"}, "op": "eq", "right": True}}},
        {"id": "recover", "type": "deliver", "needs": ["timed_out"], "config": {"to": "ops@x"}},
    )
    sent = []
    run_id = store.create_run(d, inputs={"ready": False})["run_id"]
    engine = WorkflowEngine(default_handlers(deliver=lambda p, c: sent.append(1) or {}), store)
    assert engine.advance(run_id)["status"] == "paused"

    result = dict(store.node_runs(run_id)["hold"].result)
    store.finish_node(run_id, "hold", status="paused",
                      result={**result, "deadline": "2000-01-01T00:00:00Z",
                              "wake_at": "2000-01-01T00:00:00Z"})
    out = engine.advance(run_id)

    assert out["status"] == "completed", out
    assert store.node_runs(run_id)["hold"].status == "completed"
    assert store.node_runs(run_id)["hold"].result["timed_out"] is True
    assert sent == [1], "the on_timeout branch never ran"


def test_wait_until_needs_when_and_a_positive_timeout(store):
    d = wf({"id": "hold", "type": "wait_until", "config": {}})
    out = WorkflowEngine(default_handlers(), store).advance(store.create_run(d)["run_id"])
    assert out["status"] == "failed"
    assert "config.when" in out["ran"][0]["reason"]

    d2 = wf({"id": "hold", "type": "wait_until",
             "config": {"when": {"left": 1, "op": "truthy"}, "timeout_seconds": 0}}, wid="w2")
    out2 = WorkflowEngine(default_handlers(), WorkflowStore()).advance(
        WorkflowStore().create_run(d2)["run_id"])
    assert out2["status"] == "failed"
    assert "timeout_seconds" in out2["ran"][0]["reason"]


def test_wait_until_cancellation_stops_the_run_like_any_other_pause(store):
    d = wf({"id": "hold", "type": "wait_until",
            "config": {"when": {"left": {"path": "inputs.x"}, "op": "truthy"},
                       "timeout_seconds": 3600}})
    run_id = store.create_run(d)["run_id"]
    engine = WorkflowEngine(default_handlers(), store)
    assert engine.advance(run_id)["status"] == "paused"
    assert store.set_run_status(run_id, "cancelled", reason="operator stop")
    out = engine.advance(run_id)
    assert out["status"] == "cancelled"


# ── wait_for_event (file_change) ──────────────────────────────────────────

def _touch(path, name, content="x"):
    with open(os.path.join(path, name), "w", encoding="utf-8") as fh:
        fh.write(content)


def _wake_now(store, run_id, node_id):
    """Force the next `advance()` to actually re-run this paused node,
    without waiting out its real `poll_ms` — the same trick
    `test_clock_wait_continues_after_scheduler_restart` uses for a plain
    `wait`. Only the SCHEDULING wake time moves; `deadline`/`settle_until`
    (compared against the real clock inside the handler) are untouched, so
    settle/timeout behaviour is still tested against real elapsed time."""
    prev = store.node_runs(run_id)[node_id]
    if prev.status != "paused":
        return
    store.finish_node(run_id, node_id, status="paused",
                      result={**dict(prev.result), "wake_at": "2000-01-01T00:00:00Z"})


def test_wait_for_event_completes_after_settle_with_every_event_collected(store, tmp_path):
    watched = tmp_path / "watched"
    watched.mkdir()
    d = wf(
        {"id": "hold", "type": "wait_for_event",
         "config": {"source": "file_change", "path": str(watched),
                    "settle_ms": 300, "timeout_ms": 30_000, "poll_ms": 50}},
        {"id": "send", "type": "deliver", "needs": ["hold"], "config": {"to": "a@b.c"}},
    )
    sent = []
    engine = WorkflowEngine(default_handlers(deliver=lambda p, c: sent.append(1) or {}), store)
    run_id = store.create_run(d)["run_id"]

    # First pass: establishes the baseline (empty dir), pauses without any
    # event yet.
    first = engine.advance(run_id)
    assert first["status"] == "paused"
    assert sent == []

    # Three touches, 0.5s apart — each one has to reset the settle timer.
    for i in range(3):
        _touch(str(watched), f"f{i}.txt")
        time.sleep(0.5)
        out = engine.advance(run_id)
        # Still inside the 300ms settle window every time a new touch just
        # landed (the previous advance ran right after the write).
        assert out["status"] in ("paused", "completed")

    # The dust has to settle: poll until it does (bounded, so a broken
    # debounce fails the test instead of hanging it).
    deadline = time.time() + 5
    out = {"status": "paused"}
    while out["status"] == "paused" and time.time() < deadline:
        time.sleep(0.1)
        out = engine.advance(run_id)

    assert out["status"] == "completed", out
    assert sent == [1]
    hold = store.node_runs(run_id)["hold"].result
    assert hold["count"] == 3
    assert {os.path.basename(e["path"]) for e in hold["events"]} == {"f0.txt", "f1.txt", "f2.txt"}


def test_wait_for_event_new_event_resets_the_settle_deadline(store, tmp_path):
    """The debounce contract in one call: touch once, advance (settling
    starts), touch again before it settles, and prove the node is STILL
    paused past when the first touch alone would have settled."""
    watched = tmp_path / "watched"
    watched.mkdir()
    d = wf({"id": "hold", "type": "wait_for_event",
            "config": {"source": "file_change", "path": str(watched),
                       "settle_ms": 400, "timeout_ms": 30_000, "poll_ms": 50}})
    run_id = store.create_run(d)["run_id"]
    engine = WorkflowEngine(default_handlers(), store)
    engine.advance(run_id)                       # baseline

    _touch(str(watched), "a.txt")
    _wake_now(store, run_id, "hold")
    out = engine.advance(run_id)                 # settle_until = now + 400ms
    assert out["status"] == "paused"
    first_settle_until = store.node_runs(run_id)["hold"].result["settle_until"]

    time.sleep(0.25)
    _touch(str(watched), "b.txt")
    _wake_now(store, run_id, "hold")
    out = engine.advance(run_id)                 # resets the timer
    assert out["status"] == "paused"
    second_settle_until = store.node_runs(run_id)["hold"].result["settle_until"]
    assert second_settle_until > first_settle_until

    time.sleep(0.25)                              # 250ms since the reset: not 400ms yet
    _wake_now(store, run_id, "hold")
    out = engine.advance(run_id)
    assert out["status"] == "paused", "the second touch's settle window was not honoured"

    time.sleep(0.3)
    _wake_now(store, run_id, "hold")
    out = engine.advance(run_id)
    assert out["status"] == "completed"
    assert store.node_runs(run_id)["hold"].result["count"] == 2


def test_wait_for_event_overall_timeout_fails_with_no_events(store, tmp_path):
    watched = tmp_path / "watched"
    watched.mkdir()
    d = wf({"id": "hold", "type": "wait_for_event",
            "config": {"source": "file_change", "path": str(watched),
                       "settle_ms": 200, "timeout_ms": 200, "poll_ms": 50}})
    run_id = store.create_run(d)["run_id"]
    engine = WorkflowEngine(default_handlers(), store)
    engine.advance(run_id)                        # baseline, pauses

    result = dict(store.node_runs(run_id)["hold"].result)
    store.finish_node(run_id, "hold", status="paused",
                      result={**result, "deadline": "2000-01-01T00:00:00Z",
                              "wake_at": "2000-01-01T00:00:00Z"})
    out = engine.advance(run_id)
    assert out["status"] == "failed"
    assert "timed out" in out["ran"][0]["reason"]


def test_wait_for_event_restart_mid_wait_resumes_from_persisted_state(store, tmp_path):
    """A process restart mid-wait: a brand new engine/store handle on the
    SAME database has to see the events the old process already collected,
    not start its baseline over."""
    watched = tmp_path / "watched"
    watched.mkdir()
    d = wf({"id": "hold", "type": "wait_for_event",
            "config": {"source": "file_change", "path": str(watched),
                       "settle_ms": 5000, "timeout_ms": 60_000, "poll_ms": 50}})
    run_id = store.create_run(d)["run_id"]
    WorkflowEngine(default_handlers(), store).advance(run_id)   # baseline
    _touch(str(watched), "a.txt")
    _wake_now(store, run_id, "hold")
    WorkflowEngine(default_handlers(), store).advance(run_id)
    assert store.node_runs(run_id)["hold"].result["events"], "the first touch was never seen"
    assert len(store.node_runs(run_id)["hold"].result["events"]) == 1

    # "Restart": a new WorkflowStore + WorkflowEngine, same on-disk DB.
    restarted_store = WorkflowStore()
    restarted_engine = WorkflowEngine(default_handlers(), restarted_store)
    _touch(str(watched), "b.txt")
    _wake_now(restarted_store, run_id, "hold")
    out = restarted_engine.advance(run_id)
    assert out["status"] == "paused"
    assert len(restarted_store.node_runs(run_id)["hold"].result["events"]) == 2, \
        "the restarted process forgot the event the old one already saw"


def test_wait_for_event_needs_a_known_source_and_positive_windows(store, tmp_path):
    d = wf({"id": "hold", "type": "wait_for_event", "config": {"source": "carrier_pigeon"}})
    out = WorkflowEngine(default_handlers(), store).advance(store.create_run(d)["run_id"])
    assert out["status"] == "failed"
    assert "unknown source" in out["ran"][0]["reason"]

    watched = tmp_path / "w2"
    watched.mkdir()
    d2 = wf({"id": "hold", "type": "wait_for_event",
             "config": {"source": "file_change", "path": str(watched), "settle_ms": 0,
                        "timeout_ms": 1000}}, wid="w2")
    store2 = WorkflowStore()
    out2 = WorkflowEngine(default_handlers(), store2).advance(store2.create_run(d2)["run_id"])
    assert out2["status"] == "failed"
    assert "settle_ms" in out2["ran"][0]["reason"]


def test_wait_for_event_cancellation_stops_the_run(store, tmp_path):
    watched = tmp_path / "watched"
    watched.mkdir()
    d = wf({"id": "hold", "type": "wait_for_event",
            "config": {"source": "file_change", "path": str(watched),
                       "settle_ms": 1000, "timeout_ms": 60_000}})
    run_id = store.create_run(d)["run_id"]
    engine = WorkflowEngine(default_handlers(), store)
    assert engine.advance(run_id)["status"] == "paused"
    assert store.set_run_status(run_id, "cancelled", reason="operator stop")
    assert engine.advance(run_id)["status"] == "cancelled"


# ── reconciliation: the scheduler finds and resolves an overdue run ───────

def test_scheduler_reconciles_a_wait_until_stuck_past_its_deadline(store):
    """Simulates a crash: the node is paused with a deadline that has
    already passed (as if the process died and came back long after it
    should have timed out). One `WorkflowScheduler.tick()` — the same
    periodic mechanism that already wakes a plain `wait` — has to find it
    and resolve it, with no code path specific to this test."""
    d = wf({"id": "hold", "type": "wait_until",
            "config": {"when": {"left": {"path": "inputs.x"}, "op": "truthy"},
                       "timeout_seconds": 3600, "on_timeout": True}})
    run_id = store.create_run(d)["run_id"]
    assert WorkflowEngine(default_handlers(), store).advance(run_id)["status"] == "paused"

    result = dict(store.node_runs(run_id)["hold"].result)
    store.finish_node(run_id, "hold", status="paused",
                      result={**result, "deadline": "2000-01-01T00:00:00Z",
                              "wake_at": "2000-01-01T00:00:00Z"})
    assert store.get_run(run_id)["run"].status == "paused"

    results = WorkflowScheduler(WorkflowStore()).tick()
    assert results and results[0]["status"] == "completed"
    assert store.get_run(run_id)["run"].status == "completed"
    assert store.node_runs(run_id)["hold"].result["timed_out"] is True


def test_scheduler_reconciles_a_wait_for_event_stuck_past_its_deadline(store, tmp_path):
    watched = tmp_path / "watched"
    watched.mkdir()
    d = wf({"id": "hold", "type": "wait_for_event",
            "config": {"source": "file_change", "path": str(watched),
                       "settle_ms": 500, "timeout_ms": 60_000}})
    run_id = store.create_run(d)["run_id"]
    assert WorkflowEngine(default_handlers(), store).advance(run_id)["status"] == "paused"

    result = dict(store.node_runs(run_id)["hold"].result)
    store.finish_node(run_id, "hold", status="paused",
                      result={**result, "deadline": "2000-01-01T00:00:00Z",
                              "wake_at": "2000-01-01T00:00:00Z"})

    results = WorkflowScheduler(WorkflowStore()).tick()
    assert results and results[0]["status"] == "failed"
    assert store.node_runs(run_id)["hold"].status == "failed"
    assert "timed out" in store.node_runs(run_id)["hold"].reason


# ── simulate() treats both as a structural pass-through ───────────────────

def test_simulate_walks_through_wait_until_and_wait_for_event(tmp_path):
    from src.workflows.simulate import simulate
    d = wf(
        {"id": "hold1", "type": "wait_until",
         "config": {"when": {"left": 1, "op": "truthy"}, "timeout_seconds": 5}},
        {"id": "hold2", "type": "wait_for_event", "needs": ["hold1"],
         "config": {"source": "file_change", "path": str(tmp_path),
                    "settle_ms": 100, "timeout_ms": 100}},
        {"id": "done", "type": "manual", "needs": ["hold2"]},
    )
    result = simulate(d)
    assert result.activated == ("done", "hold1", "hold2")
    assert result.not_reached == ()
    assert result.awaiting_choice == {}
