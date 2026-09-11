"""tests/test_cmp05_attention.py — CMP-05 (CONTRATO_CMP_W2.md, W2-C).

Deepens ADP-11 rather than replacing it — `tests/test_adp11_attention.py`'s
28 tests keep covering `kind`/`priority`/read-marks/HTTP exactly as before
(this lot never touched their assertions or `classify()`'s public contract).
This file covers what is NEW:

  * `classify()` now also returns `lifecycle`, `wait_cause`,
    `connection_health`+signal (`source`/`age_s`), and `next_action` — kept
    independent of `kind` (see `src/attention.py`'s module docstring).
  * `src/agent_runs.py`'s durable "finished run" marker
    (`finished_marker`/`finished_markers`, written once in `_drain`'s
    `finally`) and `attention_for_owner` preferring it over the coarse
    DB-timestamp guess.
  * `project_id` per row (`Session.project_id`, for the studio's "by
    project" view).
  * the HTTP surface still forwards whatever `attention_for_owner` returns
    (no shape assumed beyond that at the route layer).
  * the decisive test from the ficha: several sessions in different states
    (approval / gpu queue / disconnected / plain finished) are all
    distinguishable from ONE call, without opening a chat.
  * the frontend has a real caller for every new adapter export (the same
    "an endpoint/function with no caller looks exactly like a feature that
    never existed" check `test_adp11_attention.py` already runs for the
    ADP-11 surface) and the JS-side pure-logic checks pass.
"""
from __future__ import annotations

import asyncio
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import attention
from src.attention import Attention, RunInfo, classify


# ---------------------------------------------------------------------------
# classify() — the three new axes, pure, fakes only
# ---------------------------------------------------------------------------

def test_lifecycle_running_stays_running_even_with_a_pending_approval():
    """The exact thing ADP-11's single `kind` used to hide: an approval GATE
    is layered state, not a change to the run's own machine state."""
    run = RunInfo(session_id="s1", status="running", has_pending_approval=True, last_event_at=1000.0)
    att = classify(run, now=1000.0)
    assert att.kind == "approval"
    assert att.lifecycle == "running"
    assert att.wait_cause == "approval"


def test_lifecycle_maps_terminal_statuses():
    cases = {"done": "finished", "error": "failed", "stopped": "cancelled"}
    for status, lifecycle in cases.items():
        run = RunInfo(session_id="s1", status=status, finished_at=500.0)
        att = classify(run, now=1000.0)
        assert att.lifecycle == lifecycle, status
        assert att.wait_cause == "none"


def test_lifecycle_queued_without_a_live_run():
    run = RunInfo(session_id="s1", status="", queued_position=2)
    att = classify(run, now=1000.0)
    assert att.lifecycle == "queued"
    assert att.wait_cause == "gpu_queue"


def test_wait_cause_priority_matches_kind_priority():
    """`wait_cause` and `kind` agree on WHICH thing is blocking, even though
    `wait_cause` never itself decides urgency (that's still `kind`/priority)."""
    run = RunInfo(session_id="s1", status="running", has_pending_approval=True,
                  has_pending_question=True, queued_position=3, dependency="worker", last_event_at=1000.0)
    att = classify(run, now=1000.0)
    assert att.kind == "approval"
    assert att.wait_cause == "approval"


def test_wait_cause_dependency_when_nothing_else_blocks():
    run = RunInfo(session_id="s1", status="running", last_event_at=999.0, dependency="worker-a")
    att = classify(run, now=1000.0)
    assert att.wait_cause == "dependency"
    assert att.kind == "dependency"


def test_wait_cause_none_for_a_healthy_working_run():
    run = RunInfo(session_id="s1", status="running", last_event_at=999.0)
    att = classify(run, now=1000.0)
    assert att.wait_cause == "none"
    assert att.kind == "working"


def test_connection_health_disconnected_matches_the_stale_threshold():
    now = 10_000.0
    stale = RunInfo(session_id="s1", status="running", last_event_at=now - attention.STALE_AFTER_S - 1)
    att = classify(stale, now=now)
    assert att.connection_health == "disconnected"
    assert att.signal_age_s == pytest.approx(attention.STALE_AFTER_S + 1)
    assert att.signal_source == "events"


def test_connection_health_live_for_a_fresh_running_signal():
    att = classify(RunInfo(session_id="s1", status="running", last_event_at=999.0), now=1000.0)
    assert att.connection_health == "live"


def test_connection_health_unknown_when_no_signal_was_ever_observed():
    att = classify(RunInfo(session_id="s1", status=""), now=1000.0)
    assert att.connection_health == "stale"
    assert att.signal_age_s is None


def test_signal_source_rides_along_from_run_info():
    """The hook CMP-06's Herdr adapter needs later — nothing in THIS module
    ever sets it to anything but the default, but the field is real."""
    run = RunInfo(session_id="s1", status="running", last_event_at=999.0, signal_source="heuristic")
    att = classify(run, now=1000.0)
    assert att.signal_source == "heuristic"


def test_next_action_matches_the_fichas_vocabulary():
    cases = {
        "approval": "approve",
        "question": "answer",
        "disconnected": "reconnect",
        "queued_model": "open",
        "dependency": "open",
    }
    for kind, action in cases.items():
        # Build a RunInfo that classifies to exactly this kind.
        if kind == "approval":
            run = RunInfo(session_id="s", status="running", has_pending_approval=True, last_event_at=1.0)
        elif kind == "question":
            run = RunInfo(session_id="s", status="running", has_pending_question=True, last_event_at=1.0)
        elif kind == "disconnected":
            run = RunInfo(session_id="s", status="running", last_event_at=0.0)
        elif kind == "queued_model":
            run = RunInfo(session_id="s", status="running", last_event_at=1.0, queued_position=2)
        else:
            run = RunInfo(session_id="s", status="running", last_event_at=1.0, dependency="worker")
        att = classify(run, now=200.0 if kind == "disconnected" else 1.0)
        assert att.kind == kind
        assert att.next_action == action, kind


def test_next_action_finished_unreviewed_retry_vs_open_depends_on_outcome():
    failed_run = RunInfo(session_id="s1", status="error", finished_at=1.0)
    ok_run = RunInfo(session_id="s2", status="done", finished_at=1.0)
    stopped_run = RunInfo(session_id="s3", status="stopped", finished_at=1.0)
    assert classify(failed_run, now=10.0).next_action == "retry"
    assert classify(ok_run, now=10.0).next_action == "open"
    assert classify(stopped_run, now=10.0).next_action == "open"


def test_backward_compat_existing_attention_shape_untouched():
    """ADP-11's own 28 tests keep passing unmodified (see that file) — this
    is the same guarantee restated at the dataclass level: the new fields
    are ADDITIVE, every old field/keyword still means exactly what it did."""
    att = classify(RunInfo(session_id="s1", status="running", has_pending_approval=True, last_event_at=1.0), now=2.0)
    assert isinstance(att, Attention)
    assert att.kind == "approval"
    assert att.reason == attention._REASONS["approval"]
    assert att.priority == attention._PRIORITY["approval"]


# ---------------------------------------------------------------------------
# attention_for_owner() — project_id + durable finished-status override
# ---------------------------------------------------------------------------

def test_attention_for_owner_attaches_project_id():
    now = 10_000.0
    activity = {"s1": {"last_event_at": now - 1, "started_at": now - 5, "queued_position": 0, "label": "Chat A"}}
    rows = attention.attention_for_owner(
        "alice", now=now, agent_activity=activity,
        pending_approval_sessions=["s1"], open_questions=[], worker_cards={},
        finished_rows=[], reads={}, project_ids={"s1": "proj-42"},
    )
    assert len(rows) == 1
    assert rows[0]["project_id"] == "proj-42"


def test_attention_for_owner_project_id_none_when_unknown():
    now = 10_000.0
    activity = {"s1": {"last_event_at": now - 1, "started_at": now - 5, "queued_position": 0, "label": ""}}
    rows = attention.attention_for_owner(
        "alice", now=now, agent_activity=activity,
        pending_approval_sessions=["s1"], open_questions=[], worker_cards={},
        finished_rows=[], reads={}, project_ids={},
    )
    assert rows[0]["project_id"] is None


def test_attention_for_owner_every_row_carries_the_new_fields():
    now = 10_000.0
    activity = {"s1": {"last_event_at": now - 1, "started_at": now - 5, "queued_position": 2, "label": ""}}
    rows = attention.attention_for_owner(
        "alice", now=now, agent_activity=activity,
        pending_approval_sessions=[], open_questions=[], worker_cards={},
        finished_rows=[], reads={},
    )
    assert len(rows) == 1
    row = rows[0]
    for key in ("lifecycle", "wait_cause", "connection_health", "signal", "next_action", "project_id"):
        assert key in row, key
    assert row["signal"]["source"] == "events"
    assert "age_s" in row["signal"] and "last_event_at" in row["signal"]


def test_attention_for_owner_prefers_durable_marker_status_over_db_guess():
    """Before CMP-05, a finished row was ALWAYS presented as `status="done"`
    — even a run that actually failed. With a durable marker present, the
    real outcome surfaces (`lifecycle == "failed"`, `next_action == "retry"`),
    exactly the gap `docs/api/attention.md` used to flag as a known limit."""
    now = 10_000.0
    finished_at = now - 60
    rows = attention.attention_for_owner(
        "alice", now=now, agent_activity={}, pending_approval_sessions=[],
        open_questions=[], worker_cards={},
        finished_rows=[("sess-x", "My chat", finished_at)],
        reads={},
        finished_statuses={"sess-x": {"status": "error", "finished_at": finished_at + 5}},
    )
    assert len(rows) == 1
    assert rows[0]["kind"] == "finished_unreviewed"
    assert rows[0]["lifecycle"] == "failed"
    assert rows[0]["next_action"] == "retry"
    assert rows[0]["since"] == finished_at + 5  # the marker's own precise timestamp, not the coarse DB one


def test_attention_for_owner_falls_back_when_no_marker_exists():
    """A session with no durable marker (predates it, or never ran a
    detached run) behaves EXACTLY as before: guessed `done`, DB timestamp."""
    now = 10_000.0
    finished_at = now - 60
    rows = attention.attention_for_owner(
        "alice", now=now, agent_activity={}, pending_approval_sessions=[],
        open_questions=[], worker_cards={},
        finished_rows=[("sess-x", "My chat", finished_at)],
        reads={}, finished_statuses={},
    )
    assert rows[0]["lifecycle"] == "finished"
    assert rows[0]["since"] == finished_at


def test_attention_for_owner_ignores_a_marker_with_an_unknown_status():
    """A marker with a corrupted/unexpected `status` value never overrides
    the safe `done` default — never trust an on-disk value blindly."""
    now = 10_000.0
    finished_at = now - 60
    rows = attention.attention_for_owner(
        "alice", now=now, agent_activity={}, pending_approval_sessions=[],
        open_questions=[], worker_cards={},
        finished_rows=[("sess-x", "My chat", finished_at)],
        reads={}, finished_statuses={"sess-x": {"status": "sideways", "finished_at": finished_at}},
    )
    assert rows[0]["lifecycle"] == "finished"


# ---------------------------------------------------------------------------
# src/agent_runs.py — durable finished marker
# ---------------------------------------------------------------------------

from src import agent_runs


@pytest.fixture(autouse=True)
def _isolated_agent_runs(tmp_path, monkeypatch):
    import src.constants as consts
    monkeypatch.setattr(consts, "DATA_DIR", str(tmp_path / "data"), raising=False)
    agent_runs._RUNS.clear()
    agent_runs._LANES.clear()
    agent_runs._INTERRUPTED.clear()
    yield
    agent_runs._RUNS.clear()
    agent_runs._LANES.clear()
    agent_runs._INTERRUPTED.clear()


async def _finishing_gen():
    yield "data: [DONE]\n\n"


async def _erroring_gen():
    raise RuntimeError("boom")
    yield  # pragma: no cover - unreachable, keeps this an async generator


def test_finished_marker_written_on_normal_completion():
    async def run_it():
        run = agent_runs.start("sess-done", _finishing_gen())
        await run.task
    asyncio.run(run_it())
    marker = agent_runs.finished_marker("sess-done")
    assert marker is not None
    assert marker["status"] == "done"
    assert isinstance(marker["finished_at"], float)


def test_finished_marker_written_on_error():
    async def run_it():
        run = agent_runs.start("sess-err", _erroring_gen())
        await run.task
    asyncio.run(run_it())
    marker = agent_runs.finished_marker("sess-err")
    assert marker is not None
    assert marker["status"] == "error"


def test_finished_marker_absent_for_an_unknown_session():
    assert agent_runs.finished_marker("never-ran") is None


def test_finished_marker_survives_in_memory_eviction():
    """The whole point: `finished_markers()` reads the ON-DISK file, not
    `_RUNS` — so it is still there after the in-memory `_Run` is gone."""
    async def run_it():
        run = agent_runs.start("sess-evicted", _finishing_gen())
        await run.task
    asyncio.run(run_it())
    agent_runs._RUNS.pop("sess-evicted", None)  # simulate eviction
    assert agent_runs.finished_marker("sess-evicted") is not None


def test_finished_marker_not_written_for_a_non_terminal_status():
    """Guard exercised directly: `_record_finished_marker` is a no-op for
    anything but done/error/stopped (e.g. `waiting_user`, a paused turn)."""
    run = agent_runs._Run()
    run.status = "waiting_user"
    agent_runs._record_finished_marker("sess-paused", run)
    assert agent_runs.finished_marker("sess-paused") is None


def test_finished_markers_survives_a_corrupt_file(tmp_path, monkeypatch):
    import os
    import src.constants as consts
    monkeypatch.setattr(consts, "DATA_DIR", str(tmp_path / "data2"), raising=False)
    path = agent_runs._finished_marker_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("{not json")
    assert agent_runs.finished_markers() == {}


# ---------------------------------------------------------------------------
# The decisive test (ficha): several sessions in different states, one call,
# no chat opened — the person can tell what needs intervention and what does
# not, correctly attributed to lifecycle/wait_cause/connection_health.
# ---------------------------------------------------------------------------

def test_decisive_several_runs_one_approval_a_gpu_queue_and_a_dropped_connection():
    now = 10_000.0
    activity = {
        "needs-approval": {"last_event_at": now - 2, "started_at": now - 20, "queued_position": 0, "label": "Deploy notes"},
        "in-gpu-queue": {"last_event_at": now - 1, "started_at": now - 10, "queued_position": 3, "label": "Summarize PDF"},
        "dropped-connection": {"last_event_at": now - attention.STALE_AFTER_S - 30, "started_at": now - 600, "queued_position": 0, "label": "Long research"},
        "healthy-working": {"last_event_at": now - 1, "started_at": now - 5, "queued_position": 0, "label": "Quick edit"},
    }
    rows = attention.attention_for_owner(
        "alice", now=now, agent_activity=activity,
        pending_approval_sessions=["needs-approval"], open_questions=[], worker_cards={},
        finished_rows=[], reads={}, project_ids={},
    )
    by_id = {r["session_id"]: r for r in rows}

    # A healthy, quietly-working run needs nobody — never in the tray.
    assert "healthy-working" not in by_id

    # Every OTHER row is not just present but tells the right story on its
    # own, without following a link into a chat transcript:
    approval = by_id["needs-approval"]
    assert approval["kind"] == "approval"
    assert approval["wait_cause"] == "approval"
    assert approval["next_action"] == "approve"
    assert approval["connection_health"] == "live"

    queued = by_id["in-gpu-queue"]
    assert queued["kind"] == "queued_model"
    assert queued["wait_cause"] == "gpu_queue"
    assert queued["lifecycle"] == "running"
    assert queued["next_action"] == "open"

    dropped = by_id["dropped-connection"]
    assert dropped["kind"] == "disconnected"
    assert dropped["connection_health"] == "disconnected"
    assert dropped["next_action"] == "reconnect"
    assert dropped["signal"]["age_s"] >= attention.STALE_AFTER_S

    # The approval outranks everything else — sorted first, exactly ADP-11's
    # fixed priority order, now carrying the richer, independently-true story
    # alongside it rather than instead of it.
    assert [r["session_id"] for r in rows][0] == "needs-approval"


# ---------------------------------------------------------------------------
# HTTP surface — new fields simply pass through whatever attention_for_owner
# returns; the route makes no assumption about the row shape beyond that.
# ---------------------------------------------------------------------------

import routes.attention_routes as ar


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(ar, "effective_user", lambda request: "alice")
    app = FastAPI()
    app.include_router(ar.setup_attention_routes())
    return TestClient(app)


def test_get_attention_route_forwards_the_new_fields(client, monkeypatch):
    fake_row = {
        "session_id": "s1", "kind": "approval", "reason": "Waiting for your approval", "priority": 0,
        "since": 1.0, "detail": "", "label": "", "unread": True,
        "lifecycle": "running", "wait_cause": "approval", "connection_health": "live",
        "signal": {"source": "events", "age_s": 1.0, "last_event_at": 1.0}, "next_action": "approve",
        "project_id": "proj-1",
    }

    def fake_for_owner(owner, *, limit=50):
        return [fake_row]

    monkeypatch.setattr(ar.attention, "attention_for_owner", fake_for_owner)
    resp = client.get("/api/attention")
    assert resp.status_code == 200
    assert resp.json()["runs"] == [fake_row]


# ---------------------------------------------------------------------------
# Frontend — real callers for every new export (mirrors
# tests/test_adp11_attention.py's own "no caller = no feature" checks) and
# the JS-side pure-logic checks pass.
# ---------------------------------------------------------------------------

from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_STUDIO_SRC = _REPO / "studio" / "src"


def _read(rel: str) -> str:
    return (_REPO / rel).read_text(encoding="utf-8")


def test_activity_ts_exports_the_new_cmp05_functions():
    text = _read("studio/src/adapters/activity.ts")
    for needle in ("export function groupByProject", "export function stableAttentionOrder",
                   "export function nextRunParam", "export function runProjectId"):
        assert needle in text, needle


def test_attention_ts_types_the_new_fields():
    text = _read("studio/src/adapters/attention.ts")
    for needle in ("lifecycle:", "waitCause:", "connectionHealth:", "nextAction:", "projectId:"):
        assert needle in text, needle


def test_activity_screen_uses_the_new_exports():
    text = _read("studio/src/screens/Activity.tsx")
    for needle in ("groupByProject(", "stableAttentionOrder(", "nextRunParam(", "closeIfStillOpen("):
        assert needle in text, needle
    # The freeze-while-interacting mechanism is wired to real DOM events, not
    # just imported and unused.
    assert "onMouseEnter" in text and "onMouseLeave" in text


def test_activity_screen_offers_a_project_view_and_next_action():
    text = _read("studio/src/screens/Activity.tsx")
    assert "activity-view-project" in text
    assert "activity-next-action" in text


def test_the_files_this_lot_owns_exist():
    for rel in (
        "src/attention.py", "routes/attention_routes.py",
        "studio/src/adapters/attention.ts", "studio/src/adapters/activity.ts",
        "studio/src/screens/Activity.tsx", "studio/src/screens/activity.css",
        "src/agent_runs.py", "docs/api/attention.md",
    ):
        assert (_REPO / rel).exists(), f"missing {rel}"


import shutil
import subprocess

_CHECK = _REPO / "studio" / "checks" / "attention-cmp05.check.mjs"
_HAS_NODE = shutil.which("node") is not None


@pytest.mark.skipif(not _HAS_NODE, reason="node needed")
def test_attention_cmp05_js_checks_pass():
    proc = subprocess.run(
        ["node", str(_CHECK)], capture_output=True, text=True,
        encoding="utf-8", cwd=str(_REPO), timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ok attention-cmp05" in proc.stdout
