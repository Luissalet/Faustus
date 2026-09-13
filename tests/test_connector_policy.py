"""CONTRATO_CONECTORES Lote F2 — src/connector_policy.py + its wiring.

Covers F2.5's scenarios:
  * pure precedence/parsing (`resolve_allowed_servers`, `is_tool_allowed`)
    including weird tool names and the None-is-no-regression case;
  * isolation — two sessions of the same MCP server with different
    allowlists each only execute their own;
  * a scheduled task declaring `connector_ids=[]` cannot call a connector
    even though the server exists (mcp__-qualified names — this lot's
    documented minimum scope, see docs/api/tool_selection.md §Decisions);
  * resumption (same session_id, a second dispatcher call) keeps enforcing
    the same list — enforcement is resolved fresh from persisted state, not
    from in-run context, so nothing about "resuming" can drop it;
  * a project's `connectors` is inherited by a session with no override of
    its own;
  * tool-support-false produces an advisory notice, never a fallback.
"""
from __future__ import annotations

import asyncio
import tempfile
import uuid

import pytest

from src.connector_policy import (
    is_tool_allowed,
    resolve_allowed_servers,
    resolve_allowed_servers_for_session,
    tool_support_notice,
)


# ---------------------------------------------------------------------------
# Pure functions — no DB, no I/O.
# ---------------------------------------------------------------------------

def test_none_none_none_is_unrestricted_the_pre_f2_behavior():
    assert resolve_allowed_servers() is None
    assert resolve_allowed_servers(session=None, project=None, task=None) is None


def test_task_beats_session_beats_project():
    assert resolve_allowed_servers(session=["a"], project=["b"], task=["c"]) == {"c"}
    assert resolve_allowed_servers(session=["a"], project=["b"], task=None) == {"a"}
    assert resolve_allowed_servers(session=None, project=["b"], task=None) == {"b"}


def test_explicit_empty_list_is_honoured_not_upgraded_to_unrestricted():
    # An explicit "zero connectors" at the winning tier must stay zero, even
    # though a lower tier would have allowed something.
    assert resolve_allowed_servers(session=[], project=["b"], task=None) == set()
    assert resolve_allowed_servers(session=None, project=[], task=None) == set()


def test_is_tool_allowed_unrestricted_always_true():
    assert is_tool_allowed("mcp__jobhunter__list_jobs", None) is True
    assert is_tool_allowed("bash", None) is True
    assert is_tool_allowed("", None) is True


def test_is_tool_allowed_gates_only_mcp_qualified_names():
    allowed = {"jobhunter"}
    assert is_tool_allowed("bash", allowed) is True
    assert is_tool_allowed("send_email", allowed) is True
    assert is_tool_allowed("list_emails", allowed) is True  # bare built-in email tool: out of scope


def test_is_tool_allowed_matches_the_server_segment():
    allowed = {"jobhunter"}
    assert is_tool_allowed("mcp__jobhunter__list_jobs", allowed) is True
    assert is_tool_allowed("mcp__writer__list_jobs", allowed) is False
    assert is_tool_allowed("mcp__jobhunter__list_jobs", set()) is False


def test_is_tool_allowed_handles_double_underscore_tool_segments():
    # Browser-style tool names carry their own "__" past the server id
    # (mcp__<server>__browser_click) — maxsplit=2 must keep that intact.
    allowed = {"srv"}
    assert is_tool_allowed("mcp__srv__browser__click", allowed) is True
    assert is_tool_allowed("mcp__other__browser__click", allowed) is False


@pytest.mark.parametrize("weird_name", ["mcp__", "mcp__srv__", "mcp"])
def test_is_tool_allowed_weird_names_are_let_through(weird_name):
    # A malformed mcp__-shaped name (no server, or no tool segment) cannot
    # correspond to any real connector; the dispatcher's own "unknown tool"
    # path is where that gets rejected, not the connector gate.
    assert is_tool_allowed(weird_name, set()) is True


# ---------------------------------------------------------------------------
# DB-backed: sessions, projects, scheduled tasks, and the real dispatcher.
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_session_row():
    """A real `sessions` row this test owns, cleaned up afterwards."""
    from core.database import Session as DbSession, SessionLocal

    sid = f"f2-test-{uuid.uuid4()}"
    db = SessionLocal()
    try:
        db.add(DbSession(id=sid, name="f2 test session", owner="f2-tester", endpoint_url="http://localhost", model="test-model"))
        db.commit()
    finally:
        db.close()
    yield sid
    db = SessionLocal()
    try:
        db.query(DbSession).filter(DbSession.id == sid).delete()
        db.commit()
    finally:
        db.close()


@pytest.fixture()
def known_mcp_servers():
    """`jobhunter`/`writer`/`mail` as real `McpServer` rows — needed only by
    tests that go through `ProjectStore`'s connector validation (a session's
    own `connector_ids` is not validated against this table; see
    `services.projects._sanitize_connector_ids`)."""
    from core.database import McpServer, SessionLocal

    ids = [f"jobhunter-{uuid.uuid4().hex[:6]}", f"writer-{uuid.uuid4().hex[:6]}"]
    db = SessionLocal()
    try:
        for sid in ids:
            db.add(McpServer(id=sid, name=sid, transport="stdio"))
        db.commit()
    finally:
        db.close()
    yield {"jobhunter": ids[0], "writer": ids[1]}
    db = SessionLocal()
    try:
        db.query(McpServer).filter(McpServer.id.in_(ids)).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def _set_connector_ids(session_id, ids):
    from core.database import set_session_connector_ids
    assert set_session_connector_ids(session_id, ids) is True


def _run_tool_block(tool_name, session_id, content="{}"):
    from src.agent_tools import ToolBlock
    from src.tool_execution import NO_TOOL_SECURITY_CONTEXT, execute_tool_block

    return asyncio.run(execute_tool_block(
        ToolBlock(tool_name, content),
        session_id=session_id,
        security_context=NO_TOOL_SECURITY_CONTEXT,
    ))


def test_none_connector_ids_is_no_regression_against_the_real_dispatcher(db_session_row):
    # No session/project/task ever declared a restriction: the connector
    # check must not block (whatever else happens — e.g. "MCP manager not
    # available" in a test process with none configured — is not this
    # policy's concern).
    _desc, result = _run_tool_block("mcp__jobhunter__list_jobs", db_session_row)
    assert result.get("error") != "Connector jobhunter is not enabled for this task"


def test_two_sessions_same_server_different_lists_are_isolated(db_session_row):
    from core.database import Session as DbSession, SessionLocal

    sid_a = db_session_row
    sid_b = f"f2-test-{uuid.uuid4()}"
    db = SessionLocal()
    try:
        db.add(DbSession(id=sid_b, name="f2 test session b", owner="f2-tester", endpoint_url="http://localhost", model="test-model"))
        db.commit()
    finally:
        db.close()
    try:
        _set_connector_ids(sid_a, ["jobhunter"])
        _set_connector_ids(sid_b, ["writer"])

        _desc, result_a = _run_tool_block("mcp__jobhunter__list_jobs", sid_a)
        assert result_a.get("error") != "Connector jobhunter is not enabled for this task"

        _desc, result_b = _run_tool_block("mcp__jobhunter__list_jobs", sid_b)
        assert result_b == {
            "error": "Connector jobhunter is not enabled for this task",
            "exit_code": 1,
        }

        _desc, result_a2 = _run_tool_block("mcp__writer__list_jobs", sid_a)
        assert result_a2 == {
            "error": "Connector writer is not enabled for this task",
            "exit_code": 1,
        }
        _desc, result_b2 = _run_tool_block("mcp__writer__list_jobs", sid_b)
        assert result_b2.get("error") != "Connector writer is not enabled for this task"
    finally:
        db = SessionLocal()
        try:
            db.query(DbSession).filter(DbSession.id == sid_b).delete()
            db.commit()
        finally:
            db.close()


def test_resumption_keeps_enforcing_the_same_list(db_session_row):
    """The dispatcher resolves the allowlist fresh from `session_id` every
    call — nothing is snapshotted into a run — so a second call against the
    SAME session_id (standing in for a resumed background run reconnecting
    to the same live session) blocks exactly the same way the first did."""
    _set_connector_ids(db_session_row, [])

    for _ in range(2):
        _desc, result = _run_tool_block("mcp__mail__list_emails", db_session_row)
        assert result == {
            "error": "Connector mail is not enabled for this task",
            "exit_code": 1,
        }


def test_scheduled_task_empty_connector_ids_blocks_mail_even_though_it_exists():
    """F2.5: a task declaring `connector_ids=[]` cannot call
    `mcp__mail__...` even though the server exists — this lot's documented
    minimum scope is MCP-qualified names (see docs/api/tool_selection.md
    §Decisions for why bare built-in email tool names are out of scope)."""
    from core.database import Session as DbSession, SessionLocal, ScheduledTask
    from src.task_scheduler import set_task_policy

    sid = f"f2-task-session-{uuid.uuid4()}"
    tid = f"f2-task-{uuid.uuid4()}"
    db = SessionLocal()
    try:
        db.add(DbSession(id=sid, name="[Task] f2 test", owner="f2-tester", endpoint_url="http://localhost", model="test-model"))
        db.add(ScheduledTask(id=tid, owner="f2-tester", name="f2 test task", session_id=sid))
        db.commit()
    finally:
        db.close()
    try:
        set_task_policy(tid, connector_ids=[])
        _desc, result = _run_tool_block("mcp__mail__list_emails", sid)
        assert result == {
            "error": "Connector mail is not enabled for this task",
            "exit_code": 1,
        }
        # Sanity: an allowed server on the SAME task-declared policy still runs.
        set_task_policy(tid, connector_ids=["mail"])
        _desc2, result2 = _run_tool_block("mcp__mail__list_emails", sid)
        assert result2.get("error") != "Connector mail is not enabled for this task"
    finally:
        db = SessionLocal()
        try:
            db.query(ScheduledTask).filter(ScheduledTask.id == tid).delete()
            db.query(DbSession).filter(DbSession.id == sid).delete()
            db.commit()
        finally:
            db.close()


def test_project_connectors_are_inherited_by_a_session_with_no_override(
    db_session_row, known_mcp_servers, monkeypatch
):
    import services.projects as projects_mod
    from core.database import Session as DbSession, SessionLocal

    jobhunter_id = known_mcp_servers["jobhunter"]
    writer_id = known_mcp_servers["writer"]
    tmp_dir = tempfile.mkdtemp(prefix="f2-project-store-")
    store = projects_mod.ProjectStore(tmp_dir)
    monkeypatch.setattr(projects_mod, "_store", store)

    project = store.create(name="F2 Project", owner="f2-tester", scaffold_memory=False)
    updated = store.update(project["id"], {"connectors": [jobhunter_id]}, owner="f2-tester")
    assert updated["connectors"] == [jobhunter_id]

    db = SessionLocal()
    try:
        db.query(DbSession).filter(DbSession.id == db_session_row).update({"project_id": project["id"]})
        db.commit()
    finally:
        db.close()

    # No session-level override: the project's allowlist is what's effective.
    effective = resolve_allowed_servers_for_session(db_session_row, "f2-tester")
    assert effective == {jobhunter_id}

    _desc, result = _run_tool_block(f"mcp__{writer_id}__list_jobs", db_session_row)
    assert result == {"error": f"Connector {writer_id} is not enabled for this task", "exit_code": 1}
    _desc2, result2 = _run_tool_block(f"mcp__{jobhunter_id}__list_jobs", db_session_row)
    assert result2.get("error") != f"Connector {jobhunter_id} is not enabled for this task"


def test_session_override_beats_its_own_project(db_session_row, known_mcp_servers, monkeypatch):
    import services.projects as projects_mod
    from core.database import Session as DbSession, SessionLocal

    jobhunter_id = known_mcp_servers["jobhunter"]
    writer_id = known_mcp_servers["writer"]
    tmp_dir = tempfile.mkdtemp(prefix="f2-project-store-")
    store = projects_mod.ProjectStore(tmp_dir)
    monkeypatch.setattr(projects_mod, "_store", store)
    project = store.create(name="F2 Project 2", owner="f2-tester", scaffold_memory=False)
    store.update(project["id"], {"connectors": [writer_id]}, owner="f2-tester")

    db = SessionLocal()
    try:
        db.query(DbSession).filter(DbSession.id == db_session_row).update({"project_id": project["id"]})
        db.commit()
    finally:
        db.close()

    _set_connector_ids(db_session_row, [jobhunter_id])
    effective = resolve_allowed_servers_for_session(db_session_row, "f2-tester")
    assert effective == {jobhunter_id}


# ---------------------------------------------------------------------------
# Project store validation — unknown connector ids are dropped, not raised.
# ---------------------------------------------------------------------------

def test_project_store_drops_unknown_connector_ids_without_raising():
    import services.projects as projects_mod
    from core.database import McpServer, SessionLocal

    known_id = f"f2-known-{uuid.uuid4()}"
    db = SessionLocal()
    try:
        db.add(McpServer(id=known_id, name="Known", transport="stdio"))
        db.commit()
    finally:
        db.close()
    try:
        tmp_dir = tempfile.mkdtemp(prefix="f2-project-store-")
        store = projects_mod.ProjectStore(tmp_dir)
        project = store.create(name="F2 Sanitize", owner="f2-tester", scaffold_memory=False)
        updated = store.update(
            project["id"], {"connectors": [known_id, "totally-unknown-id"]}, owner="f2-tester"
        )
        assert updated["connectors"] == [known_id]
    finally:
        db = SessionLocal()
        try:
            db.query(McpServer).filter(McpServer.id == known_id).delete()
            db.commit()
        finally:
            db.close()


def test_project_store_connectors_none_clears_and_empty_list_is_kept():
    import services.projects as projects_mod
    from core.database import McpServer, SessionLocal

    known_id = f"f2-known-{uuid.uuid4()}"
    db = SessionLocal()
    try:
        db.add(McpServer(id=known_id, name="Known", transport="stdio"))
        db.commit()
    finally:
        db.close()
    try:
        tmp_dir = tempfile.mkdtemp(prefix="f2-project-store-")
        store = projects_mod.ProjectStore(tmp_dir)
        project = store.create(name="F2 Clear", owner="f2-tester", scaffold_memory=False)

        updated = store.update(project["id"], {"connectors": []}, owner="f2-tester")
        assert updated["connectors"] == []

        updated2 = store.update(project["id"], {"connectors": None}, owner="f2-tester")
        assert updated2["connectors"] is None

        # Omitting the key entirely must not touch a previously-set value.
        store.update(project["id"], {"connectors": [known_id]}, owner="f2-tester")
        updated3 = store.update(project["id"], {"name": "F2 Clear renamed"}, owner="f2-tester")
        assert updated3["connectors"] == [known_id]
    finally:
        db = SessionLocal()
        try:
            db.query(McpServer).filter(McpServer.id == known_id).delete()
            db.commit()
        finally:
            db.close()


# ---------------------------------------------------------------------------
# Task policy storage.
# ---------------------------------------------------------------------------

def test_task_policy_connector_ids_round_trip_and_default_none():
    from src.task_scheduler import get_task_policy, set_task_policy

    tid = f"f2-policy-{uuid.uuid4()}"
    assert get_task_policy(tid)["connector_ids"] is None  # undeclared task: no regression

    set_task_policy(tid, connector_ids=["jobhunter", "writer"])
    assert get_task_policy(tid)["connector_ids"] == ["jobhunter", "writer"]

    # An explicit empty list is preserved, not coerced back to None.
    set_task_policy(tid, connector_ids=[])
    assert get_task_policy(tid)["connector_ids"] == []

    # Updating an unrelated field (permissions) must not clear connector_ids.
    set_task_policy(tid, permissions=["bash"])
    assert get_task_policy(tid)["connector_ids"] == []


# ---------------------------------------------------------------------------
# F2.4 — tool-support notice: advisory only, never a fallback.
# ---------------------------------------------------------------------------

def test_tool_support_notice_is_none_when_endpoint_is_not_text_only(db_session_row):
    _set_connector_ids(db_session_row, ["jobhunter"])
    assert tool_support_notice(db_session_row, "http://127.0.0.1:11434/v1", "f2-tester") is None


def test_tool_support_notice_is_none_when_no_connectors_are_selected(db_session_row):
    # Text-only transport, but this chat never narrowed its connectors —
    # nothing surprising happened, so nothing to say.
    assert tool_support_notice(db_session_row, "faustus-cli://local", "f2-tester") is None


def test_tool_support_notice_fires_for_text_only_transport_with_connectors_selected(db_session_row):
    _set_connector_ids(db_session_row, ["jobhunter"])
    notice = tool_support_notice(db_session_row, "faustus-cli://local", "f2-tester")
    assert notice is not None
    assert "not changed" in notice or "not called" in notice


def test_tool_support_notice_never_mentions_switching_model_or_endpoint(db_session_row):
    """No-silent-fallback principle: the notice must read as advisory, never
    as "so we used X instead"."""
    _set_connector_ids(db_session_row, ["jobhunter"])
    notice = tool_support_notice(db_session_row, "faustus-cli://local", "f2-tester")
    lowered = notice.lower()
    assert "instead" not in lowered
    assert "switched" not in lowered
    assert "fallback" not in lowered
