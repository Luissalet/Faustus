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
    by = {row["id"]: row for row in out["by_rule"]}
    assert by["fetch-domains"]["deny"] == 1
    assert by["no-rm"]["ask"] == 1
    assert by["never"] == {"id": "never", "tool": "write_file", "deny": 0, "ask": 0}
    assert [s["rule_id"] for s in out["samples"]] == ["fetch-domains", "no-rm"]
    assert out["samples"][1]["args_head"] == "rm -rf build"


def test_rehearse_with_no_rules_touches_nothing():
    out = reh.rehearse([], CALLS)
    assert out["checked"] == 4 and out["would_deny"] == 0 and out["would_ask"] == 0
    assert out["by_rule"] == [] and out["samples"] == []


def test_calls_from_metadata_reads_tool_events():
    meta = {"tool_events": [{"tool": "bash", "command": "ls"}, {"tool": "", "command": "x"}, 3]}
    assert reh.calls_from_metadata(json.dumps(meta)) == [{"tool": "bash", "args": "ls"}]
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
    monkeypatch.setattr(reh, "recent_tool_calls", lambda limit=500, db=None: list(CALLS[:4]))

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
