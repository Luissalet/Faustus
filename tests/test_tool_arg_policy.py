"""Tests for src/tool_arg_policy.py — argument-level tool policy rules.

Covers: each op's violation semantics, glob tool matching, nested arg paths,
missing-argument semantics, rule validation, the deny result text (and that
the tool never executes), the ask-to-approval bridge (reusing the existing
ToolApprovalStore, same as tests/test_tool_approval_single_action_scope.py),
the admin API routes, and an MCP-style tool name.
"""
from __future__ import annotations

import asyncio

import pytest

from src.tool_arg_policy import (
    RuleError,
    evaluate,
    extract_tool_args,
    override_security_decision,
    validate_rules,
)


def _set_rules(monkeypatch, rules):
    import src.tool_arg_policy as tap
    monkeypatch.setattr(
        "src.settings.get_setting",
        lambda key, default=None: rules if key == "tool_arg_rules" else default,
    )


# ---------------------------------------------------------------------------
# Empty setting is a no-op
# ---------------------------------------------------------------------------

def test_empty_rules_never_fires(monkeypatch):
    _set_rules(monkeypatch, [])
    assert evaluate("bash", {"command": "rm -rf /"}) is None


# ---------------------------------------------------------------------------
# Each op
# ---------------------------------------------------------------------------

def test_equals_op(monkeypatch):
    rule = {"id": "r1", "tool": "manage_tasks", "arg": "action", "op": "equals",
            "value": "list", "action": "deny"}
    _set_rules(monkeypatch, [rule])
    assert evaluate("manage_tasks", {"action": "delete"}) is not None
    assert evaluate("manage_tasks", {"action": "list"}) is None


def test_one_of_op(monkeypatch):
    rule = {"id": "r1", "tool": "manage_tasks", "arg": "action", "op": "one_of",
            "value": ["list", "add"], "action": "deny"}
    _set_rules(monkeypatch, [rule])
    assert evaluate("manage_tasks", {"action": "delete"}) is not None
    assert evaluate("manage_tasks", {"action": "add"}) is None


def test_prefix_op(monkeypatch):
    rule = {"id": "r1", "tool": "write_file", "arg": "path", "op": "prefix",
            "value": "/workspace/", "action": "deny"}
    _set_rules(monkeypatch, [rule])
    assert evaluate("write_file", {"path": "/etc/passwd"}) is not None
    assert evaluate("write_file", {"path": "/workspace/notes.md"}) is None


def test_not_prefix_op(monkeypatch):
    rule = {"id": "r1", "tool": "write_file", "arg": "path", "op": "not_prefix",
            "value": "/etc", "action": "deny"}
    _set_rules(monkeypatch, [rule])
    assert evaluate("write_file", {"path": "/etc/passwd"}) is not None
    assert evaluate("write_file", {"path": "/workspace/notes.md"}) is None


def test_regex_op(monkeypatch):
    rule = {"id": "r1", "tool": "read_file", "arg": "path", "op": "regex",
            "value": r"[A-Za-z0-9_./-]+\.md", "action": "deny"}
    _set_rules(monkeypatch, [rule])
    # Must FULLMATCH the pattern to satisfy the constraint; anything that
    # doesn't fullmatch fires the rule.
    assert evaluate("read_file", {"path": "notes.txt"}) is not None
    assert evaluate("read_file", {"path": "notes.md"}) is None


def test_max_len_op(monkeypatch):
    rule = {"id": "r1", "tool": "send_email", "arg": "subject", "op": "max_len",
            "value": 10, "action": "deny"}
    _set_rules(monkeypatch, [rule])
    assert evaluate("send_email", {"subject": "way too long a subject"}) is not None
    assert evaluate("send_email", {"subject": "short"}) is None


def test_domain_in_op(monkeypatch):
    rule = {"id": "r1", "tool": "web_fetch", "arg": "url", "op": "domain_in",
            "value": ["example.com"], "action": "deny"}
    _set_rules(monkeypatch, [rule])
    assert evaluate("web_fetch", {"url": "https://evil.example.org/x"}) is not None
    assert evaluate("web_fetch", {"url": "https://api.example.com/x"}) is None  # subdomain allowed
    assert evaluate("web_fetch", {"url": "https://example.com/x"}) is None


# ---------------------------------------------------------------------------
# Glob tool match
# ---------------------------------------------------------------------------

def test_glob_tool_match(monkeypatch):
    rule = {"id": "r1", "tool": "mcp__github__*", "arg": "repo", "op": "equals",
            "value": "allowed/repo", "action": "deny"}
    _set_rules(monkeypatch, [rule])
    assert evaluate("mcp__github__create_issue", {"repo": "other/repo"}) is not None
    assert evaluate("mcp__github__create_issue", {"repo": "allowed/repo"}) is None
    assert evaluate("mcp__gitlab__create_issue", {"repo": "other/repo"}) is None


# ---------------------------------------------------------------------------
# Nested arg path
# ---------------------------------------------------------------------------

def test_nested_arg_path(monkeypatch):
    rule = {"id": "r1", "tool": "mcp__fs__write", "arg": "options.path",
            "op": "prefix", "value": "/workspace/", "action": "deny"}
    _set_rules(monkeypatch, [rule])
    assert evaluate("mcp__fs__write", {"options": {"path": "/etc/shadow"}}) is not None
    assert evaluate("mcp__fs__write", {"options": {"path": "/workspace/a.txt"}}) is None
    # Not a dict at the intermediate step -> resolves to missing.
    assert evaluate("mcp__fs__write", {"options": "not-a-dict"}) is not None


# ---------------------------------------------------------------------------
# Missing-arg semantics
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("op,value", [
    ("equals", "x"),
    ("one_of", ["x"]),
    ("prefix", "x"),
    ("domain_in", ["example.com"]),
])
def test_missing_arg_violates_for_required_ops(monkeypatch, op, value):
    rule = {"id": "r1", "tool": "t", "arg": "missing_field", "op": op,
            "value": value, "action": "deny"}
    _set_rules(monkeypatch, [rule])
    decision = evaluate("t", {})
    assert decision is not None
    assert decision.op == op


@pytest.mark.parametrize("op,value", [
    ("not_prefix", "x"),
    ("regex", "x.*"),
    ("max_len", 5),
])
def test_missing_arg_does_not_violate_for_optional_ops(monkeypatch, op, value):
    rule = {"id": "r1", "tool": "t", "arg": "missing_field", "op": op,
            "value": value, "action": "deny"}
    _set_rules(monkeypatch, [rule])
    assert evaluate("t", {}) is None


# ---------------------------------------------------------------------------
# Rule validation
# ---------------------------------------------------------------------------

def test_validate_rules_accepts_a_well_formed_list():
    rules = [{"id": "r1", "tool": "bash", "arg": "command", "op": "not_prefix",
              "value": "rm -rf", "action": "deny", "note": "no rm -rf"}]
    checked = validate_rules(rules)
    assert checked[0]["id"] == "r1"
    assert checked[0]["action"] == "deny"


@pytest.mark.parametrize("bad_rule,msg_fragment", [
    ({"tool": "bash", "arg": "command", "op": "equals", "value": "x"}, "id"),
    ({"id": "r1", "arg": "command", "op": "equals", "value": "x"}, "tool"),
    ({"id": "r1", "tool": "bash", "op": "equals", "value": "x"}, "arg"),
    ({"id": "r1", "tool": "bash", "arg": "command", "op": "bogus", "value": "x"}, "op"),
    ({"id": "r1", "tool": "bash", "arg": "command", "op": "equals", "value": "x",
      "action": "maybe"}, "action"),
    ({"id": "r1", "tool": "bash", "arg": "command", "op": "regex", "value": "("}, "regex"),
    ({"id": "r1", "tool": "bash", "arg": "command", "op": "regex",
      "value": "a" * 500}, "too long"),
    ({"id": "r1", "tool": "bash", "arg": "command", "op": "max_len", "value": "5"}, "max_len"),
    ({"id": "r1", "tool": "bash", "arg": "command", "op": "one_of", "value": "x"}, "one_of"),
    ({"id": "r1", "tool": "bash", "arg": "command", "op": "domain_in", "value": []}, "domain_in"),
])
def test_validate_rules_rejects_bad_rules(bad_rule, msg_fragment):
    with pytest.raises(RuleError) as exc:
        validate_rules([bad_rule])
    assert msg_fragment in str(exc.value)


def test_validate_rules_rejects_duplicate_ids():
    rule = {"id": "r1", "tool": "bash", "arg": "command", "op": "equals", "value": "x"}
    with pytest.raises(RuleError, match="duplicate"):
        validate_rules([dict(rule), dict(rule)])


def test_validate_rules_rejects_non_list():
    with pytest.raises(RuleError, match="list"):
        validate_rules({"not": "a list"})


def test_default_action_is_deny():
    rule = {"id": "r1", "tool": "bash", "arg": "command", "op": "equals", "value": "x"}
    checked = validate_rules([rule])
    assert checked[0]["action"] == "deny"


# ---------------------------------------------------------------------------
# extract_tool_args
# ---------------------------------------------------------------------------

def test_extract_tool_args_json_object():
    assert extract_tool_args("mcp__github__x", '{"repo": "a/b"}') == {"repo": "a/b"}


def test_extract_tool_args_bash_uses_command_key():
    assert extract_tool_args("bash", "echo hi") == {"command": "echo hi"}


def test_extract_tool_args_falls_back_to_content_wrapper():
    assert extract_tool_args("some_legacy_tool", "raw text") == {"content": "raw text"}


def test_extract_tool_args_passthrough_dict():
    assert extract_tool_args("bash", {"command": "echo hi"}) == {"command": "echo hi"}


# ---------------------------------------------------------------------------
# Deny result text, and the tool never executes
# ---------------------------------------------------------------------------

def test_deny_message_format(monkeypatch):
    rule = {"id": "no-rm", "tool": "bash", "arg": "command", "op": "not_prefix",
            "value": "rm -rf", "action": "deny", "note": "destructive command"}
    _set_rules(monkeypatch, [rule])
    decision = evaluate("bash", {"command": "rm -rf /tmp/x"})
    assert decision is not None
    assert decision.message() == (
        "Blocked by policy rule no-rm: command must not_prefix rm -rf. destructive command"
    )


def test_execute_tool_block_denies_and_never_runs(tmp_path, monkeypatch):
    """Defense-in-depth path in src/tool_execution.py: a "deny" rule blocks
    the call before dispatch, and the side effect (a file write) never
    happens."""
    from src.agent_tools import ToolBlock
    from src.tool_execution import NO_TOOL_SECURITY_CONTEXT, execute_tool_block

    target = tmp_path / "should_not_exist.txt"
    rule = {"id": "no-writes", "tool": "write_file", "arg": "path", "op": "not_prefix",
            "value": str(tmp_path), "action": "deny", "note": "outside the sandbox"}
    _set_rules(monkeypatch, [rule])

    block = ToolBlock(tool_type="write_file", content=f"{target}\nhello")

    async def _run():
        return await execute_tool_block(
            block, security_context=NO_TOOL_SECURITY_CONTEXT,
        )

    desc, result = asyncio.run(_run())
    assert result.get("blocked") is True
    assert result.get("policy") == "tool_arg_policy"
    assert result.get("policy_rule_id") == "no-writes"
    assert "no-writes" in result.get("error", "")
    assert not target.exists()


def test_execute_tool_block_allows_when_no_rule_fires(tmp_path, monkeypatch):
    from src.agent_tools import ToolBlock
    from src.tool_execution import NO_TOOL_SECURITY_CONTEXT, execute_tool_block

    # write_file is admin-gated; run as single-user (auth disabled) so this
    # test isolates the arg-policy path rather than the admin gate.
    monkeypatch.setenv("AUTH_ENABLED", "false")
    target = tmp_path / "ok.txt"
    rule = {"id": "no-writes", "tool": "write_file", "arg": "path", "op": "not_prefix",
            "value": "/etc", "action": "deny"}
    _set_rules(monkeypatch, [rule])

    block = ToolBlock(tool_type="write_file", content=f"{target}\nhello")

    async def _run():
        return await execute_tool_block(
            block, security_context=NO_TOOL_SECURITY_CONTEXT,
        )

    desc, result = asyncio.run(_run())
    assert not result.get("blocked")
    assert target.exists()
    assert target.read_text() == "hello"


def test_mcp_style_tool_name_is_matched(monkeypatch):
    rule = {"id": "r1", "tool": "mcp__slack__post_message", "arg": "channel",
            "op": "not_prefix", "value": "#exec-", "action": "deny"}
    _set_rules(monkeypatch, [rule])
    decision = evaluate("mcp__slack__post_message", {"channel": "#exec-private"})
    assert decision is not None
    assert evaluate("mcp__slack__post_message", {"channel": "#general"}) is None


# ---------------------------------------------------------------------------
# "ask" reuses the EXISTING human approval flow
# ---------------------------------------------------------------------------

def test_override_security_decision_turns_ask_into_not_allowed():
    from src.tool_capabilities import ToolGateDecision

    rule = {"id": "ask-me", "tool": "send_email", "arg": "to", "op": "domain_in",
            "value": ["acme.example"], "action": "ask", "note": "external recipient"}
    checked = validate_rules([rule])[0]
    from src.tool_arg_policy import Decision
    arg_decision = Decision(
        rule_id=checked["id"], tool="send_email", arg="to", op="domain_in",
        value=checked["value"], action="ask", note=checked["note"],
    )
    allowed = ToolGateDecision(allowed=True, reason=None)
    overridden = override_security_decision(allowed, arg_decision)
    assert overridden.allowed is False
    assert "ask-me" in overridden.reason


def test_override_security_decision_leaves_deny_untouched():
    from src.tool_capabilities import ToolGateDecision
    from src.tool_arg_policy import Decision

    deny_decision = Decision(rule_id="r1", tool="bash", arg="command", op="equals",
                              value="x", action="deny")
    allowed = ToolGateDecision(allowed=True, reason=None)
    overridden = override_security_decision(allowed, deny_decision)
    assert overridden is allowed


def test_override_security_decision_does_not_clobber_existing_denial():
    from src.tool_capabilities import ToolGateDecision
    from src.tool_arg_policy import Decision

    ask_decision = Decision(rule_id="r1", tool="bash", arg="command", op="equals",
                             value="x", action="ask")
    not_allowed = ToolGateDecision(allowed=False, reason="already gated for another reason")
    overridden = override_security_decision(not_allowed, ask_decision)
    assert overridden is not_allowed


def test_ask_decision_routes_into_the_existing_approval_store():
    """Same pattern as tests/test_tool_approval_single_action_scope.py: the
    "ask" path does not invent a second approval mechanism — it creates a
    normal ToolApprovalStore card carrying the rule's message as the shown
    reason, and consuming it grants access exactly like any other card."""
    from src.tool_approvals import ToolApprovalStore
    from src.tool_capabilities import ToolRunSecurityContext, capabilities_for_action
    from src.tool_arg_policy import Decision

    content = "to: someone@partner.example\nsubject: hi\nbody: hi"
    arg_decision = Decision(
        rule_id="external-recipient", tool="send_email", arg="to", op="domain_in",
        value=["acme.example"], action="ask", note="external recipient — confirm",
    )

    store = ToolApprovalStore()
    pending = store.create(
        owner="alice", session_id="session-1", origin_run_id="run-1",
        tool_name="send_email", content=content, workspace=None,
        external_untrusted_context_seen=True,
        capabilities=capabilities_for_action("send_email", content),
    )
    payload = pending.public_payload(reason=arg_decision.message())
    assert payload["description"] == arg_decision.message()
    assert "external-recipient" in payload["description"]

    grant = store.consume(
        pending.approval_id, decision="approve", owner="alice",
        session_id="session-1", allow_continuation=False,
    )
    assert grant is not None

    resumed = ToolRunSecurityContext(
        external_untrusted_context_seen=True,
        approval_gate_bypassed=grant.allow_remaining_actions,
    )
    # Single-use grant: the gate stays armed for anything after the sealed call.
    assert resumed.decision_for("send_email").allowed is False


# ---------------------------------------------------------------------------
# API routes: GET/PUT /api/tool-arg-rules, POST /api/tool-arg-rules/test
# ---------------------------------------------------------------------------

@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src import settings as settings_mod
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    settings_mod._invalidate_caches()
    monkeypatch.setenv("AUTH_ENABLED", "false")

    from routes.tool_arg_policy_routes import setup_tool_arg_policy_routes

    app = FastAPI()
    app.include_router(setup_tool_arg_policy_routes())
    with TestClient(app) as c:
        yield c
    settings_mod._invalidate_caches()


def test_api_get_defaults_to_empty(api_client):
    resp = api_client.get("/api/tool-arg-rules")
    assert resp.status_code == 200
    assert resp.json() == {"rules": []}


def test_api_put_then_get_round_trips(api_client):
    rules = [{"id": "r1", "tool": "bash", "arg": "command", "op": "not_prefix",
              "value": "rm -rf", "action": "deny", "note": "no rm -rf"}]
    put_resp = api_client.put("/api/tool-arg-rules", json={"rules": rules})
    assert put_resp.status_code == 200
    assert put_resp.json()["rules"][0]["id"] == "r1"

    get_resp = api_client.get("/api/tool-arg-rules")
    assert get_resp.json()["rules"][0]["id"] == "r1"


def test_api_put_rejects_bad_rule(api_client):
    rules = [{"id": "r1", "tool": "bash", "arg": "command", "op": "bogus", "value": "x"}]
    resp = api_client.put("/api/tool-arg-rules", json={"rules": rules})
    assert resp.status_code == 400
    assert "op" in resp.json()["error"]
    # A rejected PUT must not have partially persisted.
    assert api_client.get("/api/tool-arg-rules").json() == {"rules": []}


def test_api_test_endpoint_reports_block_and_allow(api_client):
    rules = [{"id": "r1", "tool": "bash", "arg": "command", "op": "not_prefix",
              "value": "rm -rf", "action": "deny", "note": "no rm -rf"}]
    api_client.put("/api/tool-arg-rules", json={"rules": rules})

    blocked = api_client.post("/api/tool-arg-rules/test",
                               json={"tool": "bash", "args": {"command": "rm -rf /"}})
    assert blocked.status_code == 200
    body = blocked.json()
    assert body["allowed"] is False
    assert body["rule_id"] == "r1"
    assert body["action"] == "deny"

    allowed = api_client.post("/api/tool-arg-rules/test",
                               json={"tool": "bash", "args": {"command": "echo hi"}})
    assert allowed.json() == {"allowed": True}


def test_api_admin_gate(tmp_path, monkeypatch):
    """Without AUTH_ENABLED=false and no admin session, the routes 403."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src import settings as settings_mod
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    settings_mod._invalidate_caches()
    monkeypatch.delenv("AUTH_ENABLED", raising=False)

    from routes.tool_arg_policy_routes import setup_tool_arg_policy_routes

    app = FastAPI()
    app.include_router(setup_tool_arg_policy_routes())
    with TestClient(app) as c:
        resp = c.get("/api/tool-arg-rules")
    assert resp.status_code == 403
    settings_mod._invalidate_caches()
