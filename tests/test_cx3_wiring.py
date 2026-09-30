"""The places the H03/H08/H19/H23 records are joined to the rest of the app:
the mail MCP server's hidden effect argument, the run id stamped on a saved
reply, the steering note carried into the next turn, the restart recovery at
startup and the `run_report` MCP tool."""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import effect_outbox as eo, effect_tools, exec_ledger as xl, steering_journal as sj

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def stores(tmp_path):
    eo.set_db_path(str(tmp_path / "effects.sqlite3"))
    xl.set_db_path(str(tmp_path / "ledger.sqlite3"))
    yield
    eo.set_db_path(None)
    xl.set_db_path(None)


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- mail MCP

def test_the_hidden_effect_argument_joins_the_admission_the_tool_layer_opened():
    from src import tool_execution
    es = _load("email_server_under_test", "mcp_servers/email_server.py")
    desc = effect_tools.describe_call("send_email", '{"to":"a@example.invalid","body":"x"}')
    adm = effect_tools.admit_call(desc, owner="alice", session_id="s1", run_id="r1", call_id="c1")
    ctx = effect_tools.context_for(adm, owner="alice", session_id="s1", run_id="r1", call_id="c1")
    token = eo.bind_context(ctx)
    try:
        linked = tool_execution._with_effect_link("send_email", {"to": "a@example.invalid"})
        assert set(linked["_faustus_effect"]) >= {"admission_id", "attempt_id", "run_id", "call_id"}
        assert tool_execution._with_effect_link("list_emails", {"limit": 3}) == {"limit": 3}, \
            "only the send tools get the hidden argument"
    finally:
        eo.reset_context(token)
    assert "_faustus_effect" not in tool_execution._with_effect_link("send_email", {}), "no admission, no link"

    # What the mail server receives: its dispatch settles the SAME row.
    es_token = es._bind_effect_context(linked["_faustus_effect"], "alice")
    try:
        out = eo.dispatch_effect("email.smtp", lambda: "sent", owner="alice", destination="a@example.invalid",
                                 identifier="<m1@example.invalid>", identifier_kind="message_id")
    finally:
        eo.reset_context(es_token)
    assert out.effect["id"] == adm.id
    rows = eo.list_effects(owner="alice")
    assert len(rows) == 1, "joining the admission must not open a second row"
    assert rows[0]["state"] == "succeeded" and rows[0]["attempt_id"] == adm.attempt_id
    assert rows[0]["run_id"] == "r1" and rows[0]["call_id"] == "c1"


def test_the_mail_server_consumes_the_hidden_argument_and_unbinds_it():
    es = _load("email_server_under_test2", "mcp_servers/email_server.py")
    raw = {"admission_id": "nope", "attempt_id": "a1", "run_id": "r", "session_id": "s", "call_id": "c", "tool": "send_email"}
    out = asyncio.run(es.call_tool("list_email_accounts", {"_faustus_effect": raw}))
    assert out and "_faustus_effect" not in out[0].text
    assert eo.current_context() is None, "the context must not leak out of the call"
    assert es._bind_effect_context("not a dict", "alice") is None


# ------------------------------------------------------- reply and next turn

def test_a_saved_reply_carries_the_run_that_produced_it(monkeypatch):
    from routes.chat_helpers import save_assistant_response
    from src import agent_runs
    monkeypatch.setattr(agent_runs, "get_run_id", lambda sid: "run-123" if sid == "sess-1" else None)
    added = []
    sess = SimpleNamespace(owner=None, add_message=added.append)
    sm = SimpleNamespace(save_sessions=lambda: None)
    save_assistant_response(sess, sm, "sess-1", "hello", {})
    assert added[0].metadata["run_id"] == "run-123"
    save_assistant_response(sess, sm, "sess-1", "hello", {"run_id": "already"})
    assert added[1].metadata["run_id"] == "already", "an id the turn already recorded wins"
    save_assistant_response(sess, sm, "other", "hello", {})
    assert "run_id" not in added[2].metadata


def test_unread_steering_messages_reach_the_next_turn_once_and_join_the_existing_note():
    from routes.chat_helpers import _carry_steering
    sj.queued("s1", "r1", owner="alice", text="also use tabs", source="user", mode="steer")
    sj.drop_unsent("s1", "r1", reason="run_ended_before_delivery")
    note = _carry_steering("An earlier call may have taken effect.", "s1", "alice")
    assert note.startswith("An earlier call may have taken effect.")
    assert "also use tabs" in note and "run_ended_before_delivery" in note
    assert _carry_steering(None, "s1", "alice") is None, "once carried it is not repeated"
    assert _carry_steering("kept", "s1", "alice") == "kept"
    sj.queued("s2", "r2", owner="alice", text="private", source="user", mode="steer")
    sj.drop_unsent("s2", "r2", reason="x")
    assert _carry_steering(None, "s2", "bob") is None, "another owner never sees it"


# --------------------------------------------------------------- startup

def test_startup_closes_what_the_dead_process_left_open_in_all_three_records(monkeypatch):
    row = eo.prepare(kind="email.smtp", owner="alice", destination="a@example.invalid", identifier="<x@e>",
                     identifier_kind="message_id", arguments=b"x")
    eo.admit(row["id"])
    eo.begin_dispatch(row["id"])
    xl.record("run_started", run_id="r9", session_id="s9", owner="alice")
    sj.queued("s9", "r9", owner="alice", text="hello", source="user", mode="steer")
    for mod in (xl, eo):
        monkeypatch.setattr(mod, "BOOT_ID", "boot-next")
    # The same three calls app.py makes, in the same order.
    eo.recover_orphans()
    xl.recover_after_restart()
    sj.recover_after_restart()
    assert eo.get(row["id"])["state"] == "outcome_unknown"
    assert xl.replay("r9")["state"] == "interrupted"
    assert sj.receipts(session_id="s9")[0]["state"] == "dropped"
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    for call in ("_eo.recover_orphans", "_xl.recover_after_restart", "_sj.recover_after_restart"):
        assert call in source, f"app.py no longer calls {call}"


# --------------------------------------------------------------- MCP tool

def _workers(monkeypatch):
    mcp = types.ModuleType("mcp")
    srv = types.ModuleType("mcp.server")
    stdio = types.ModuleType("mcp.server.stdio")
    typesmod = types.ModuleType("mcp.types")

    class Server:
        def __init__(self, name):
            self.name = name

        def list_tools(self):
            return lambda f: f

        def call_tool(self):
            return lambda f: f

    srv.Server = Server
    stdio.stdio_server = None
    typesmod.Tool = lambda **kw: SimpleNamespace(**kw)
    typesmod.TextContent = lambda **kw: SimpleNamespace(**kw)
    for n, m in (("mcp", mcp), ("mcp.server", srv), ("mcp.server.stdio", stdio), ("mcp.types", typesmod)):
        monkeypatch.setitem(sys.modules, n, m)
    return _load("workers_server_run_report", "mcp_servers/workers_server.py")


def test_run_report_mcp_tool_reads_the_four_views(monkeypatch):
    ws = _workers(monkeypatch)
    assert "run_report" in {t.name for t in asyncio.run(ws.list_tools())}
    calls = []

    def fake_request(method, path, body=None, timeout=None, **kw):
        calls.append((method, path))
        return {"text": f"text for {path}"}

    monkeypatch.setattr(ws, "_request", fake_request)
    run = lambda **a: asyncio.run(ws.call_tool("run_report", a))[0].text
    assert run(view="effects", unresolved=True) == "text for /api/effects?unresolved=true"
    assert run(view="orphans") == "text for /api/agent/orphans"
    assert run(view="cost", session_id="s 1", run_id="r1", turn=2).startswith("text for /api/runs/s%201/turn-cost?")
    assert calls[-1][1] == "/api/runs/s%201/turn-cost?run_id=r1&turn=2"
    assert run(view="ledger", session_id="s1").startswith("text for /api/runs/s1/ledger?")
    assert run(view="cost").startswith("Error")
    assert run(view="nonsense").startswith("Error")
    assert all(method == "GET" for method, _ in calls), "the tool only reads"
