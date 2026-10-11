"""Rehearsal of argument rules against recorded tool calls (radar #390)."""
from __future__ import annotations

import json

import pytest

from src import tool_arg_rehearsal as reh
from src.tool_arg_policy import evaluate_rules

RULES = [
    {"id": "fetch-domains", "tool": "web_fetch", "arg": "url", "op": "domain_in",
     "value": ["example.org"], "action": "deny", "note": ""},
    {"id": "no-rm", "tool": "bash", "arg": "command", "op": "not_prefix",
     "value": "rm -rf", "action": "ask", "note": ""},
    {"id": "never", "tool": "write_file", "arg": "path", "op": "prefix",
     "value": "/workspace", "action": "deny", "note": ""},
]

CALLS = [
    {"tool": "web_fetch", "args": json.dumps({"url": "https://example.org/a"}), "ts": "3"},
    {"tool": "web_fetch", "args": json.dumps({"url": "https://news.example.com/b"}), "ts": "2"},
    {"tool": "bash", "args": "rm -rf build", "ts": "1"},
    {"tool": "bash", "args": "ls -la", "ts": "0"},
    {"tool": "", "args": "junk"},
    "not a dict",
]


def test_evaluate_rules_uses_given_list_not_settings(monkeypatch):
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: [])
    hit = evaluate_rules("web_fetch", {"url": "https://other.net"}, RULES)
    assert hit is not None and hit.rule_id == "fetch-domains"
    assert evaluate_rules("web_fetch", {"url": "https://other.net"}, []) is None


def test_rehearse_counts_by_action_and_rule():
    out = reh.rehearse(RULES, CALLS)
    assert out["checked"] == 4
    assert out["would_deny"] == 1
    assert out["would_ask"] == 1
    assert out["untouched"] == 2
    assert out["external_checked"] == 0 and out["already_stopped_by_other_gate"] == 0
    by = {row["id"]: row for row in out["by_rule"]}
    assert by["fetch-domains"]["deny"] == 1
    assert by["no-rm"]["ask"] == 1
    assert by["never"] == {"id": "never", "tool": "write_file", "deny": 0, "ask": 0}
    assert [s["rule_id"] for s in out["samples"]] == ["fetch-domains", "no-rm"]
    assert out["samples"][1]["args_head"] == "rm -rf build"


def test_gate_receipts_map_runner_tools_and_count_prior_stops():
    receipts = [
        {"session": "chat:abc", "tool": "bash", "command_head": "rm -rf x", "action": "blocked", "ts": "1"},
        {"session": "agent-gate:run1", "tool": "claude:Bash", "command_head": "rm -rf build",
         "action": "blocked", "ts": "2026-10-10T12:00:02"},
        {"session": "agent-gate:run1", "tool": "claude:Bash", "command_head": "ls -la",
         "action": "allowed", "ts": "2026-10-10T12:00:01"},
        {"session": "agent-gate:run2", "tool": "external:WebFetch",
         "command_head": json.dumps({"url": "https://news.example.com/b"}),
         "action": "asked", "ts": "2026-10-10T12:00:03"},
        {"session": "agent-gate:run2", "tool": "external:WebFetch",
         "command_head": json.dumps({"url": "https://example.org/ok"}),
         "action": "allowed", "ts": "2026-10-10T12:00:00"},
    ]
    calls = reh.calls_from_gate_receipts(receipts, limit=50)
    assert [c["tool"] for c in calls] == ["web_fetch", "bash", "bash", "web_fetch"]
    assert [c["prior_action"] for c in calls] == ["asked", "blocked", "allowed", "allowed"]
    assert all(c["source"] == "agent_gate" for c in calls)
    out = reh.rehearse(RULES, calls)
    assert out["external_checked"] == 4
    assert out["would_deny"] == 1          # news.example.com
    assert out["would_ask"] == 1           # rm -rf build
    assert out["already_stopped_by_other_gate"] == 2
    assert out["unparsed"] == 0
    assert {s["prior_action"] for s in out["samples"]} == {"asked", "blocked"}
    assert all(s["already_stopped_by_other_gate"] for s in out["samples"])


def test_real_webfetch_head_with_long_prompt_is_unparsed_not_false_deny():
    """Truncated sort_keys head (prompt before url) must not score as deny."""
    prompt = "x" * 130
    # Real old shape: sort_keys puts prompt before url; 160-char head cuts url off.
    full = json.dumps({"prompt": prompt, "url": "https://example.org/guide"}, sort_keys=True)
    head = full[:160]
    assert "example.org" not in head or not head.rstrip().endswith("}")
    calls = [{
        "tool": "web_fetch",
        "args": head,
        "source": "agent_gate",
        "prior_action": "allowed",
        "ts": "1",
    }]
    out = reh.rehearse(RULES, calls)
    assert out["checked"] == 1
    assert out["unparsed"] == 1
    assert out["would_deny"] == 0
    assert out["untouched"] == 0


def test_real_write_file_path_translates_to_path_for_rules():
    """Write receipts use file_path; rules match on path."""
    rules = [
        {"id": "env", "tool": "write_file", "arg": "path", "op": "regex",
         "value": r"\.env$", "action": "ask", "note": ""},
    ]
    head = json.dumps({"file_path": "D:/proj/.env", "content": "KEY=1"})
    # New payload shape drops content and keeps file_path first.
    from src import agent_gate
    reduced = agent_gate._payload_text("Write", {"file_path": "D:/proj/.env", "content": "KEY=1"})
    assert isinstance(reduced, str) and "file_path" in reduced and "KEY=1" not in reduced
    calls = [
        {"tool": "write_file", "args": head, "source": "agent_gate", "prior_action": "allowed", "ts": "2"},
        {"tool": "write_file", "args": reduced, "source": "agent_gate", "prior_action": "allowed", "ts": "1"},
    ]
    out = reh.rehearse(rules, calls)
    assert out["unparsed"] == 0
    assert out["would_ask"] == 2
    assert out["untouched"] == 0


def test_payload_text_puts_url_before_prompt_within_head():
    from src import agent_gate
    from src import command_guard
    prompt = "y" * 200
    text = agent_gate._payload_text(
        "WebFetch", {"url": "https://news.example.com/ok", "prompt": prompt})
    head = str(text)[: command_guard._COMMAND_HEAD_CHARS]
    assert "news.example.com" in head
    parsed = json.loads(head if head.endswith("}") else text)
    assert parsed.get("url") == "https://news.example.com/ok"


def test_rehearse_recent_accepts_merged_chat_and_gate_calls():
    merged = [
        {"tool": "bash", "args": "ls", "ts": "2026-10-10T12:00:06", "session_id": "agent-gate:r",
         "source": "agent_gate", "prior_action": "allowed"},
        {"tool": "bash", "args": "echo hi", "ts": "2026-10-10T12:00:05", "session_id": "s1",
         "source": "chat"},
    ]
    out = reh.rehearse_recent(RULES, limit=10, calls=merged)
    assert out["checked"] == 2 and out["external_checked"] == 1 and out["untouched"] == 2


def test_rehearse_with_no_rules_touches_nothing():
    out = reh.rehearse([], CALLS)
    assert out["checked"] == 4 and out["would_deny"] == 0 and out["would_ask"] == 0
    assert out["by_rule"] == [] and out["samples"] == []


def test_calls_from_metadata_reads_tool_events():
    meta = {"tool_events": [{"tool": "bash", "command": "ls"}, {"tool": "", "command": "x"}, 3]}
    assert reh.calls_from_metadata(json.dumps(meta)) == [
        {"tool": "bash", "args": "ls", "source": "chat"}]
    assert reh.calls_from_metadata("not json") == []
    assert reh.calls_from_metadata({"tool_events": "nope"}) == []


def test_rehearse_recent_window_and_bounds():
    out = reh.rehearse_recent(RULES, limit=2, calls=CALLS[:4])
    assert out["checked"] == 2 and out["limit"] == 2
    assert out["newest"] == "3" and out["oldest"] == "2"
    empty = reh.rehearse_recent(RULES, limit="junk", calls=[])
    assert empty["checked"] == 0 and empty["limit"] == reh.DEFAULT_LIMIT
    assert reh._clamp_limit(10**9) == reh.MAX_LIMIT


@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src import settings as settings_mod
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    settings_mod._invalidate_caches()
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setattr(reh, "recent_tool_calls",
                        lambda limit=500, db=None, include_gate=True: list(CALLS[:4]))

    from routes.tool_arg_policy_routes import setup_tool_arg_policy_routes

    app = FastAPI()
    app.include_router(setup_tool_arg_policy_routes())
    with TestClient(app) as c:
        yield c
    settings_mod._invalidate_caches()


def test_api_rehearse_unsaved_rules(api_client):
    resp = api_client.post("/api/tool-arg-rules/rehearse", json={"rules": RULES, "limit": 50})
    assert resp.status_code == 200
    body = resp.json()
    assert body["checked"] == 4 and body["would_deny"] == 1 and body["would_ask"] == 1
    # Nothing was saved by a rehearsal.
    assert api_client.get("/api/tool-arg-rules").json() == {"rules": []}


def test_api_rehearse_defaults_to_saved_rules(api_client):
    api_client.put("/api/tool-arg-rules", json={"rules": RULES[1:2]})
    body = api_client.post("/api/tool-arg-rules/rehearse", json={}).json()
    assert body["would_ask"] == 1 and body["would_deny"] == 0


def test_api_rehearse_rejects_bad_rule(api_client):
    resp = api_client.post("/api/tool-arg-rules/rehearse",
                           json={"rules": [{"id": "x", "tool": "bash", "arg": "a", "op": "bogus"}]})
    assert resp.status_code == 400
