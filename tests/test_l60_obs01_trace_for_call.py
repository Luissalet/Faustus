"""OBS-01 — `agent_runs.trace_for_call(call_id)` reconstructs event +
artifact + receipt from one id, without grepping three different stores by
hand.

Before this lote, `call_id` already reached the SSE event stream
(tests/test_obs_call_id.py), `src.artifact_store.persist`'s manifest row,
and `src.command_guard.append_receipt`'s receipt — but nothing in the repo
ever joined the three back together by that id (`grep -rn "trace_for_call"
src/` on the pre-lote tree: no matches).
"""
from __future__ import annotations

import json
import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database as db_mod
from core.database import Base
from src import agent_runs, artifact_identity as ident, command_guard
from src.contracts.blob import ArtifactOccurrence, new_occurrence_id


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    import src.constants as consts
    monkeypatch.setattr(consts, "DATA_DIR", str(tmp_path / "data"), raising=False)
    monkeypatch.setattr(agent_runs, "_setting",
                         lambda key, default=None: {"agent_runs_persist": True}.get(key, default),
                         raising=False)
    monkeypatch.setattr(command_guard, "DATA_DIR", str(tmp_path / "data"), raising=False)
    agent_runs._RUNS.clear()
    agent_runs._LANES.clear()
    agent_runs._INTERRUPTED.clear()
    yield
    agent_runs._RUNS.clear()
    agent_runs._LANES.clear()
    agent_runs._INTERRUPTED.clear()


@pytest.fixture()
def own_database(tmp_path, monkeypatch):
    """Same pattern as tests/test_artifact_identity.py's own fixture: a real
    file both `record_manifest_version`'s raw SQL and the ORM helpers share."""
    url = "sqlite:///" + (tmp_path / "identity.db").as_posix()
    engine = create_engine(url, connect_args={"check_same_thread": False})
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal",
                        sessionmaker(autocommit=False, autoflush=False, bind=engine))
    Base.metadata.create_all(bind=engine)
    yield engine
    engine.dispose()


async def _never():
    import asyncio
    await asyncio.sleep(3600)
    yield "data: [DONE]\n\n"  # pragma: no cover


async def _quiesce(run):
    for t in (run.task, run.evict_task):
        if t is not None and not t.done():
            t.cancel()
    import asyncio
    await asyncio.sleep(0.01)


def _tool_start(call_id, tool="bash", round_=1):
    return "data: " + json.dumps(
        {"type": "tool_start", "tool": tool, "command": "x", "round": round_, "call_id": call_id}
    ) + "\n\n"


def _tool_output(call_id, tool="bash", round_=1):
    return "data: " + json.dumps(
        {"type": "tool_output", "tool": tool, "output": "ok", "exit_code": 0,
         "round": round_, "call_id": call_id}
    ) + "\n\n"


# ── not found: an honest empty answer, nothing fabricated ──────────────────

def test_unknown_call_id_is_reported_as_not_found():
    result = agent_runs.trace_for_call("call_never_happened")
    assert result == {
        "call_id": "call_never_happened", "events": [], "artifacts": [],
        "receipt": None, "found": False,
    }


def test_empty_call_id_is_a_no_op_not_a_crash():
    result = agent_runs.trace_for_call("")
    assert result["found"] is False
    assert result["events"] == []


# ── events: live buffer and persisted log ───────────────────────────────────

@pytest.mark.asyncio
async def test_finds_events_for_a_call_id_in_the_live_buffer():
    run = agent_runs.start("sid-1", _never())
    agent_runs._publish(run, _tool_start("call_1_0"))
    agent_runs._publish(run, _tool_output("call_1_0"))
    # a second, unrelated call in the same run must not leak in
    agent_runs._publish(run, _tool_start("call_1_1", tool="read_file"))

    result = agent_runs.trace_for_call("call_1_0", session_id="sid-1")
    assert result["found"] is True
    types = [e["type"] for e in result["events"]]
    assert types == ["tool_start", "tool_output"]
    assert all(e["call_id"] == "call_1_0" for e in result["events"])
    # OBS-01: every returned event already carries trace_id/step_id
    assert all(e.get("trace_id") == run.run_id for e in result["events"])
    assert all(e.get("step_id") for e in result["events"])
    await _quiesce(run)


@pytest.mark.asyncio
async def test_a_session_scoped_lookup_never_sees_another_sessions_events():
    run_a = agent_runs.start("sid-a", _never())
    run_b = agent_runs.start("sid-b", _never())
    agent_runs._publish(run_a, _tool_start("shared_looking_id"))
    agent_runs._publish(run_b, _tool_start("shared_looking_id"))

    result = agent_runs.trace_for_call("shared_looking_id", session_id="sid-a")
    assert len(result["events"]) == 1

    result_all = agent_runs.trace_for_call("shared_looking_id")
    assert len(result_all["events"]) == 2
    await _quiesce(run_a)
    await _quiesce(run_b)


@pytest.mark.asyncio
async def test_finds_events_from_a_persisted_log_after_the_live_run_is_gone(tmp_path):
    run = agent_runs.start("sid-2", _never())
    agent_runs._publish(run, _tool_start("call_2_0"))
    agent_runs._publish(run, _tool_output("call_2_0"))
    await _quiesce(run)
    # Simulate the run having left memory entirely (process restart, or the
    # detached-run TTL evicting it) -- only the on-disk log remains.
    agent_runs._RUNS.pop("sid-2", None)

    result = agent_runs.trace_for_call("call_2_0", session_id="sid-2")
    assert result["found"] is True
    assert [e["type"] for e in result["events"]] == ["tool_start", "tool_output"]


# ── receipts ─────────────────────────────────────────────────────────────

def test_finds_the_command_guard_receipt_for_a_call_id():
    command_guard.append_receipt(
        session="sid-3", tool="bash", command="echo hi", tier="SAFE",
        rule="none", action="allowed", call_id="call_3_0",
    )
    result = agent_runs.trace_for_call("call_3_0")
    assert result["found"] is True
    assert result["receipt"]["action"] == "allowed"
    assert result["receipt"]["tool"] == "bash"
    assert result["receipt"]["call_id"] == "call_3_0"


def test_a_receipt_with_no_call_id_is_never_matched_by_accident():
    command_guard.append_receipt(
        session="sid-3", tool="bash", command="echo hi", tier="SAFE",
        rule="none", action="allowed",  # no call_id at all
    )
    result = agent_runs.trace_for_call("call_3_0")
    assert result["receipt"] is None


# ── artifacts ────────────────────────────────────────────────────────────

def test_finds_the_artifact_manifest_row_for_a_call_id(own_database):
    digest = "a" * 64
    ident.ensure_blob(sha256=digest, byte_size=4, filename=f"{digest}.txt", media_type="text/plain")
    occ_id = new_occurrence_id()
    ident.record_occurrence(ArtifactOccurrence.parse({
        "id": occ_id, "kind": "document", "blob_sha256": digest,
        "label": "report.txt", "owner": "alice", "run_id": "sid-4",
    }))
    ident.record_manifest_version(occ_id, state="generated", call_id="call_4_0",
                                   format="text/plain", byte_size=4, sha256=digest)

    result = agent_runs.trace_for_call("call_4_0")
    assert result["found"] is True
    assert len(result["artifacts"]) == 1
    entry = result["artifacts"][0]
    assert entry["occurrence_id"] == occ_id
    assert entry["state"] == "generated"
    assert entry["label"] == "report.txt"
    assert entry["kind"] == "document"
    assert entry["owner"] == "alice"


def test_no_artifact_table_yet_is_an_empty_list_not_an_error():
    # No own_database fixture here -- exercises the real (unmocked) engine,
    # which may or may not have the table; either way this must not raise.
    result = agent_runs.trace_for_call("call_nothing_ever_made")
    assert result["artifacts"] == []


# ── everything together ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_call_that_produced_all_three_returns_all_three(own_database):
    run = agent_runs.start("sid-5", _never())
    agent_runs._publish(run, _tool_start("call_5_0", tool="create_document"))
    agent_runs._publish(run, _tool_output("call_5_0", tool="create_document"))

    command_guard.append_receipt(
        session="sid-5", tool="create_document", command="write report.txt",
        tier="SAFE", rule="none", action="allowed", call_id="call_5_0",
    )

    digest = "b" * 64
    ident.ensure_blob(sha256=digest, byte_size=4, filename=f"{digest}.txt", media_type="text/plain")
    occ_id = new_occurrence_id()
    ident.record_occurrence(ArtifactOccurrence.parse({
        "id": occ_id, "kind": "document", "blob_sha256": digest,
        "label": "report.txt", "owner": "alice", "run_id": "sid-5",
    }))
    ident.record_manifest_version(occ_id, state="validated", call_id="call_5_0")

    result = agent_runs.trace_for_call("call_5_0", session_id="sid-5")
    assert result["found"] is True
    assert len(result["events"]) == 2
    assert result["receipt"] is not None
    assert len(result["artifacts"]) == 1
    await _quiesce(run)
