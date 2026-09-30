"""Read-only transcript of a delegate_agents worker: builder + route + ACL."""
from __future__ import annotations

import json
import os

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.database import Base, ChatMessage as DbChatMessage, Session as DbSession
from src import subagent_transcript as st


def _events():
    return [
        {"round": 1, "tool": "read_file", "command": "read_file src/a.py", "output": "x" * 7000,
         "exit_code": 0, "duration_ms": 12, "model": "m", "call_id": "c1"},
        {"round": 2, "tool": "bash", "command": "pytest -q", "output": "1 failed", "exit_code": 1},
        {"plan_update": {"plan": "x"}},           # not a tool call: skipped
    ]


# ── builder ────────────────────────────────────────────────────────────────

def test_message_view_bounds_and_shapes_tool_events():
    msg = st.message_view(3, "assistant", "hello " * 6000,
                          {"tool_events": _events(), "input_tokens": 100, "output_tokens": 20, "model": "m"})
    assert msg["content_truncated"] and len(msg["content"]) == st.MAX_CONTENT_CHARS
    assert [e["tool"] for e in msg["tool_events"]] == ["read_file", "bash"]
    first = msg["tool_events"][0]
    assert first["output_truncated"] and len(first["output"]) == st.MAX_TOOL_OUTPUT_CHARS
    assert first["output_chars"] == 7000 and first["duration_ms"] == 12
    assert msg["tool_events"][1]["exit_code"] == 1
    assert msg["tokens"] == {"input": 100, "output": 20}


def test_tool_events_are_capped():
    events = [{"tool": "bash", "command": f"echo {i}", "output": "ok"} for i in range(st.MAX_TOOL_EVENTS_PER_MESSAGE + 7)]
    msg = st.message_view(0, "assistant", "x", {"tool_events": events})
    assert len(msg["tool_events"]) == st.MAX_TOOL_EVENTS_PER_MESSAGE and msg["tool_events_omitted"] == 7


def test_usage_prefers_trace_and_computes_heat():
    calls = [
        {"seq": 1, "model": "m", "usage": {"input_tokens": 1000, "output_tokens": 100}},
        {"seq": 2, "model": "m", "usage": {"prompt_tokens": 4000, "completion_tokens": 400, "cached_tokens": 3000}},
        {"seq": 3, "model": "m", "usage": {}},             # no usage recorded: not invented
    ]
    u = st.usage_summary([{"tokens": {"input": 1, "output": 1}}], calls)
    assert u["source"] == "trace" and u["calls"] == 2
    assert u["input_tokens"] == 5000 and u["output_tokens"] == 500 and u["total_tokens"] == 5500
    assert [c["heat"] for c in u["per_call"]] == [0.25, 1.0]
    assert u["per_call"][1]["cached"] == 3000


def test_usage_falls_back_to_messages_then_none():
    msgs = [{"tokens": {"input": 10, "output": 5}}, {"role": "user"}, {"tokens": {"input": 20, "output": 1}}]
    u = st.usage_summary(msgs, [])
    assert u["source"] == "messages" and u["total_tokens"] == 36 and u["per_call"] == []
    none = st.usage_summary([{"role": "user"}], None)
    assert none["source"] == "none" and none["total_tokens"] == 0


def test_usage_ignores_non_numeric_and_nan():
    u = st.usage_summary([], [{"seq": 1, "usage": {"input_tokens": float("nan"), "output_tokens": True}}])
    assert u["source"] == "none"


def test_build_skips_hidden_messages_and_reports_paging():
    rows = [{"role": "user", "content": "task", "metadata": {}},
            {"role": "system", "content": "summary", "metadata": {"hidden": True}},
            {"role": "assistant", "content": "done", "metadata": {}}]
    out = st.build("s", "n", "m", rows, offset=10, total=20, calls=None)
    assert [m["index"] for m in out["messages"]] == [10, 12]
    assert out["has_more_after"] is True and out["offset"] == 10


# ── route + ownership ──────────────────────────────────────────────────────

@pytest.fixture()
def client(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Maker = sessionmaker(bind=engine)
    db = Maker()
    db.add(DbSession(id="child-a", name="Worker 1", endpoint_url="http://x", model="qwen", owner="alice"))
    db.add(DbSession(id="child-b", name="Worker 2", endpoint_url="http://x", model="qwen", owner="bob"))
    db.add(DbChatMessage(id="m1", session_id="child-a", role="user", content="Investigate the parser"))
    db.add(DbChatMessage(id="m2", session_id="child-a", role="assistant", content="Found it.",
                         meta_data=json.dumps({"tool_events": _events(), "input_tokens": 900, "output_tokens": 80})))
    db.add(DbChatMessage(id="m3", session_id="child-b", role="user", content="bob's private task"))
    db.commit()
    db.close()

    import routes.session_routes as sr
    import routes.subagent_transcript_routes as rt
    monkeypatch.setattr(sr, "SessionLocal", Maker)
    monkeypatch.setattr(rt, "SessionLocal", Maker)
    monkeypatch.setattr(sr, "effective_user", lambda request: "alice")
    monkeypatch.setattr(rt, "require_user", lambda request: "alice")
    monkeypatch.setattr("src.llm_trace.list_calls", lambda sid: [
        {"seq": 1, "model": "qwen", "usage": {"input_tokens": 500, "output_tokens": 50}},
        {"seq": 2, "model": "qwen", "usage": {"input_tokens": 400, "output_tokens": 30}},
    ])
    app = FastAPI()
    app.include_router(rt.setup_subagent_transcript_routes())
    return TestClient(app)


def test_route_returns_messages_tools_and_usage(client):
    r = client.get("/api/chat/subagent/transcript/child-a")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "Worker 1" and body["model"] == "qwen" and body["total"] == 2
    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]
    tools = body["messages"][1]["tool_events"]
    assert [t["tool"] for t in tools] == ["read_file", "bash"]
    assert body["usage"]["source"] == "trace" and body["usage"]["total_tokens"] == 980
    assert body["usage"]["per_call"][0]["heat"] == 1.0


def test_route_paging(client):
    body = client.get("/api/chat/subagent/transcript/child-a?offset=1&limit=1").json()
    assert body["offset"] == 1 and body["returned"] == 1 and body["has_more_after"] is False
    assert body["messages"][0]["index"] == 1


def test_another_users_subagent_is_not_found(client):
    r = client.get("/api/chat/subagent/transcript/child-b")
    assert r.status_code == 404
    assert "private" not in r.text


def test_missing_session_is_not_found(client):
    assert client.get("/api/chat/subagent/transcript/nope").status_code == 404


def test_route_is_registered_in_the_app_source():
    text = open(os.path.join(os.path.dirname(__file__), "..", "app.py"), encoding="utf-8").read()
    assert "setup_subagent_transcript_routes" in text
