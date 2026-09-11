"""ADP-11 — src/attention.py + routes/attention_routes.py.

Covers, per the W1-A ficha:
  * classification with fakes of `RunInfo` (all eight kinds);
  * the stale/`disconnected` threshold (a `running` status with an old
    `last_event_at` is never read as `working` by inertia);
  * fixed priority order (approval > question > finished_unreviewed >
    disconnected > queued_model > dependency);
  * per-owner read marks (isolated storage, `finished_unreviewed` dropping
    off the list once read, other kinds staying but no longer `unread`);
  * the HTTP surface (owner scoping, `run_ids` validation).
"""
from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import attention
from src.attention import Attention, RunInfo, classify


# ---------------------------------------------------------------------------
# classify() — pure, fakes only
# ---------------------------------------------------------------------------

def test_classify_approval_beats_everything():
    run = RunInfo(session_id="s1", status="running", has_pending_approval=True,
                  has_pending_question=True, queued_position=3, dependency="worker",
                  last_event_at=1000.0)
    att = classify(run, now=1000.0)
    assert att.kind == "approval"
    assert att.priority == 0


def test_classify_question_beats_queue_and_dependency():
    run = RunInfo(session_id="s1", status="running", has_pending_question=True,
                  queued_position=2, dependency="worker", last_event_at=1000.0)
    att = classify(run, now=1000.0)
    assert att.kind == "question"


def test_classify_finished_terminal_statuses():
    for status in ("done", "error", "stopped"):
        run = RunInfo(session_id="s1", status=status, finished_at=500.0)
        att = classify(run, now=1000.0)
        assert att.kind == "finished_unreviewed", status
        assert att.since == 500.0


def test_classify_running_recent_event_is_working_not_disconnected():
    run = RunInfo(session_id="s1", status="running", last_event_at=999.0)
    att = classify(run, now=1000.0)
    assert att.kind == "working"


def test_classify_stale_threshold_is_disconnected_never_working():
    """The exact requirement from the ficha: a run without recent events is
    `disconnected`/stale, NEVER `working` by inertia."""
    just_under = attention.STALE_AFTER_S - 1
    just_over = attention.STALE_AFTER_S + 1
    now = 10_000.0

    fresh = RunInfo(session_id="s1", status="running", last_event_at=now - just_under)
    assert classify(fresh, now=now).kind == "working"

    stale = RunInfo(session_id="s2", status="running", last_event_at=now - just_over)
    assert classify(stale, now=now).kind == "disconnected"


def test_classify_disconnected_outranks_queued_and_dependency():
    now = 10_000.0
    run = RunInfo(session_id="s1", status="running", last_event_at=now - attention.STALE_AFTER_S - 5,
                  queued_position=4, dependency="worker-a")
    att = classify(run, now=now)
    assert att.kind == "disconnected"


def test_classify_queued_beats_dependency():
    run = RunInfo(session_id="s1", status="running", last_event_at=999.0,
                  queued_position=2, dependency="worker-a")
    att = classify(run, now=1000.0)
    assert att.kind == "queued_model"
    assert att.detail == "#2"


def test_classify_dependency_when_no_queue():
    run = RunInfo(session_id="s1", status="running", last_event_at=999.0, dependency="worker-a")
    att = classify(run, now=1000.0)
    assert att.kind == "dependency"
    assert att.detail == "worker-a"


def test_classify_queued_without_a_live_run_status():
    """A run that is queued but for which `status` was never set (the
    caller only knows the queue position) still classifies as queued, not
    as a silent `none`."""
    run = RunInfo(session_id="s1", status="", queued_position=1)
    assert classify(run, now=1000.0).kind == "queued_model"


def test_classify_none_when_nothing_is_true():
    run = RunInfo(session_id="s1", status="")
    att = classify(run, now=1000.0)
    assert att.kind == "none"
    assert att.priority == attention._PRIORITY["none"]
    assert att.since is None


def test_priority_order_matches_ficha():
    order = ["approval", "question", "finished_unreviewed", "disconnected",
             "queued_model", "dependency", "working", "none"]
    values = [attention._PRIORITY[k] for k in order]
    assert values == sorted(values)


# ---------------------------------------------------------------------------
# attention_for_owner() — full pipeline, every real source faked
# ---------------------------------------------------------------------------

def test_attention_for_owner_sorts_by_priority_and_filters_non_actionable():
    now = 10_000.0
    activity = {
        "working-one": {"last_event_at": now - 1, "started_at": now - 5, "queued_position": 0, "label": "Chat A"},
        "queued-one": {"last_event_at": now - 1, "started_at": now - 30, "queued_position": 3, "label": "Chat B"},
        "stale-one": {"last_event_at": now - attention.STALE_AFTER_S - 10, "started_at": now - 500, "queued_position": 0, "label": "Chat C"},
    }
    rows = attention.attention_for_owner(
        "alice", now=now,
        agent_activity=activity,
        pending_approval_sessions=["approval-one"],
        open_questions=[{"session_id": "question-one"}],
        worker_cards={},
        finished_rows=[],
        reads={},
    )
    kinds = [r["kind"] for r in rows]
    # working-one (kind='working') must be excluded entirely.
    assert "working" not in kinds
    assert "approval-one" == rows[0]["session_id"]
    assert rows[0]["kind"] == "approval"
    assert rows[1]["kind"] == "question"
    # disconnected outranks queued_model.
    disc_idx = kinds.index("disconnected")
    queue_idx = kinds.index("queued_model")
    assert disc_idx < queue_idx
    assert all(r["unread"] for r in rows)


def test_attention_for_owner_dependency_from_worker_board():
    now = 10_000.0
    activity = {"parent-1": {"last_event_at": now - 1, "started_at": now - 5, "queued_position": 0, "label": ""}}
    rows = attention.attention_for_owner(
        "alice", now=now, agent_activity=activity,
        pending_approval_sessions=[], open_questions=[],
        worker_cards={"child-1": {"parent": "parent-1", "name": "researcher"}},
        finished_rows=[], reads={},
    )
    assert len(rows) == 1
    assert rows[0]["kind"] == "dependency"
    assert rows[0]["detail"] == "researcher"


def test_attention_for_owner_finished_unreviewed_from_db_rows():
    now = 10_000.0
    rows = attention.attention_for_owner(
        "alice", now=now, agent_activity={}, pending_approval_sessions=[],
        open_questions=[], worker_cards={},
        finished_rows=[("sess-done", "My chat", now - 60)],
        reads={},
    )
    assert len(rows) == 1
    assert rows[0]["kind"] == "finished_unreviewed"
    assert rows[0]["label"] == "My chat"
    assert rows[0]["unread"] is True


def test_attention_for_owner_finished_unreviewed_disappears_once_read():
    now = 10_000.0
    finished_at = now - 60
    rows = attention.attention_for_owner(
        "alice", now=now, agent_activity={}, pending_approval_sessions=[],
        open_questions=[], worker_cards={},
        finished_rows=[("sess-done", "My chat", finished_at)],
        reads={"sess-done": finished_at + 1},  # read AFTER it finished
    )
    assert rows == []


def test_attention_for_owner_running_session_stays_listed_but_becomes_read():
    """Reading a still-pending approval marks it read but does NOT drop it —
    the underlying approval still needs a real decision (ACT-11's own
    acceptance criterion: changing conversation must not lose the pending
    decision)."""
    now = 10_000.0
    activity = {"sess-a": {"last_event_at": now - 1, "started_at": now - 5, "queued_position": 0, "label": ""}}
    rows = attention.attention_for_owner(
        "alice", now=now, agent_activity=activity,
        pending_approval_sessions=["sess-a"], open_questions=[], worker_cards={},
        finished_rows=[], reads={"sess-a": now},
    )
    assert len(rows) == 1
    assert rows[0]["kind"] == "approval"
    assert rows[0]["unread"] is False


# ---------------------------------------------------------------------------
# Read marks — isolated per owner, atomic file
# ---------------------------------------------------------------------------

@pytest.fixture()
def isolated_data_dir(tmp_path, monkeypatch):
    import src.constants as consts
    monkeypatch.setattr(consts, "DATA_DIR", str(tmp_path / "data"), raising=False)
    return tmp_path


def test_mark_read_and_get_reads_round_trip(isolated_data_dir):
    marked = attention.mark_read("alice", ["run-1", "run-2"], now=123.0)
    assert marked == 2
    assert attention.get_reads("alice") == {"run-1": 123.0, "run-2": 123.0}


def test_reads_are_isolated_per_owner(isolated_data_dir):
    attention.mark_read("alice", ["run-1"], now=100.0)
    attention.mark_read("bob", ["run-1"], now=200.0)
    assert attention.get_reads("alice") == {"run-1": 100.0}
    assert attention.get_reads("bob") == {"run-1": 200.0}


def test_mark_read_ignores_blank_ids(isolated_data_dir):
    assert attention.mark_read("alice", ["", "  ", "run-1"], now=1.0) == 1
    assert list(attention.get_reads("alice")) == ["run-1"]


def test_mark_read_is_additive_across_calls(isolated_data_dir):
    attention.mark_read("alice", ["run-1"], now=1.0)
    attention.mark_read("alice", ["run-2"], now=2.0)
    assert attention.get_reads("alice") == {"run-1": 1.0, "run-2": 2.0}


def test_get_reads_survives_a_corrupt_file(isolated_data_dir):
    import os
    path = attention._reads_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("{not json")
    assert attention.get_reads("alice") == {}


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------

import routes.attention_routes as ar


@pytest.fixture()
def client(monkeypatch, isolated_data_dir):
    monkeypatch.setattr(ar, "effective_user", lambda request: "alice")
    app = FastAPI()
    app.include_router(ar.setup_attention_routes())
    return TestClient(app)


def test_get_attention_route_uses_owner_and_real_sources(client, monkeypatch):
    fake_rows = [{"session_id": "s1", "kind": "approval", "reason": "Waiting for your approval",
                  "priority": 0, "since": 1.0, "detail": "", "label": "", "unread": True}]

    def fake_for_owner(owner, *, limit=50):
        assert owner == "alice"
        assert limit == 25
        return fake_rows

    monkeypatch.setattr(ar.attention, "attention_for_owner", fake_for_owner)
    resp = client.get("/api/attention?limit=25")
    assert resp.status_code == 200
    body = resp.json()
    assert body["runs"] == fake_rows
    assert body["unread_count"] == 1


def test_post_attention_read_rejects_non_list_run_ids(client):
    resp = client.post("/api/attention/read", json={"run_ids": "not-a-list"})
    assert resp.status_code == 400
    body = resp.json()
    assert body["error_class"] == "attention.invalid_run_ids"


def test_post_attention_read_marks_and_persists(client):
    resp = client.post("/api/attention/read", json={"run_ids": ["s1", "s2"]})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "marked": 2}
    assert set(attention.get_reads("alice")) == {"s1", "s2"}


# ---------------------------------------------------------------------------
# Frontend wiring — a static "has a caller" check, the same pattern
# tests/test_studio_activity_js.py uses for /api/chat/activity et al.: an
# endpoint or a merge function with no caller in studio/src looks exactly
# like a feature that never existed (PARIDAD_FUNCIONAL §7). No node/esbuild
# runtime needed for these, unlike the heavier `.check.mjs` harness.
# ---------------------------------------------------------------------------

from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_STUDIO_SRC = _REPO / "studio" / "src"


def _studio_sources() -> dict:
    return {p: p.read_text(encoding="utf-8") for p in _STUDIO_SRC.rglob("*.ts*") if p.suffix in (".ts", ".tsx")}


@pytest.mark.parametrize("needle", ["/api/attention", "/api/attention/read"])
def test_attention_endpoints_have_a_studio_caller(needle):
    callers = [p.name for p, text in _studio_sources().items() if needle in text]
    assert callers, f"{needle} has no caller in studio/src"


def test_activity_screen_merges_attention_and_marks_read():
    text = (_STUDIO_SRC / "screens" / "Activity.tsx").read_text(encoding="utf-8")
    assert "mergeAttention(" in text
    assert "markAttentionRead(" in text
    assert "loadAttention(" in text


def test_activity_adapter_exports_merge_attention():
    text = (_STUDIO_SRC / "adapters" / "activity.ts").read_text(encoding="utf-8")
    assert "export function mergeAttention" in text
