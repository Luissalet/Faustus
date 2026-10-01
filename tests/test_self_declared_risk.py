"""Self-declared risk (src/self_declared_risk.py): state-changing tools carry
an optional `security_risk` parameter; HIGH can force the approval card, nothing
the model declares ever lowers the gate, and the parameter never reaches a tool.
"""
import asyncio
import copy
import json

import pytest

import src.agent_loop as al
from src import self_declared_risk as risk
from src.tool_capabilities import ToolGateDecision


@pytest.fixture(autouse=True)
def _clean():
    risk._reset_stats()
    yield
    risk._reset_stats()


def _schema(name, props=None):
    return {"type": "function", "function": {"name": name, "description": "d",
            "parameters": {"type": "object", "properties": dict(props or {"x": {"type": "string"}}),
                           "required": []}}}


# ---------------------------------------------------------------- schemas --

def test_param_is_added_to_state_changing_tools_only_and_originals_are_untouched():
    write, read = _schema("write_file"), _schema("read_file")
    originals = copy.deepcopy([write, read])
    out = risk.with_risk_param([write, read])
    props = out[0]["function"]["parameters"]["properties"]
    assert props["security_risk"]["enum"] == ["LOW", "MEDIUM", "HIGH", "UNKNOWN"]
    assert "security_risk" not in out[1]["function"]["parameters"]["properties"]
    assert [write, read] == originals, "module-level schemas must not be edited in place"
    assert out[1] is read, "untouched schemas are passed through, not copied"


def test_shell_network_and_unknown_tools_get_it_and_an_existing_param_is_kept():
    names = ["bash", "python", "send_email", "mcp__srv__do_thing"]
    out = risk.with_risk_param([_schema(n) for n in names])
    assert all("security_risk" in s["function"]["parameters"]["properties"] for s in out)
    own = _schema("write_file", {"security_risk": {"type": "string", "description": "mine"}})
    kept = risk.with_risk_param([own])[0]
    assert kept["function"]["parameters"]["properties"]["security_risk"]["description"] == "mine"


def test_odd_schemas_do_not_raise():
    odd = [None, {}, {"function": None}, {"function": {"name": "bash"}}, {"function": {"name": "bash", "parameters": []}}]
    assert len(risk.with_risk_param(odd)) == len(odd)
    assert risk.with_risk_param(None) == []


# ---------------------------------------------------------------- arguments --

def test_strip_removes_the_parameter_and_reports_the_declaration():
    cleaned, declared = risk.strip_from_arguments('{"path": "a.py", "security_risk": "high"}')
    assert json.loads(cleaned) == {"path": "a.py"} and declared == "HIGH"
    cleaned, declared = risk.strip_from_arguments({"command": "ls", "security_risk": "bogus"})
    assert cleaned == {"command": "ls"} and declared is None
    text = '{"path": "a.py"}'
    assert risk.strip_from_arguments(text) == (text, None)
    garbage = "not json security_risk"
    assert risk.strip_from_arguments(garbage) == (garbage, None)
    assert risk.strip_from_arguments('["security_risk"]') == ('["security_risk"]', None)


# --------------------------------------------------------------------- gate --

ALLOWED = ToolGateDecision(True)


def test_only_high_raises_and_nothing_lowers():
    for declared in (None, "LOW", "MEDIUM", "UNKNOWN"):
        decision, forced = risk.raise_decision(ALLOWED, declared, "bash")
        assert decision is ALLOWED and forced is False
    decision, forced = risk.raise_decision(ALLOWED, "HIGH", "bash")
    assert forced and decision.allowed is False and "HIGH risk" in decision.reason
    refused = ToolGateDecision(False, "the policy's own reason")
    for declared in ("LOW", "MEDIUM", "HIGH", None):
        decision, forced = risk.raise_decision(refused, declared, "bash")
        assert decision is refused and forced is False, "a refusal is never overwritten or lowered"


def test_the_persons_explicit_choices_are_honoured():
    d, forced = risk.raise_decision(ALLOWED, "HIGH", "bash", approval_mode="full")
    assert d is ALLOWED and not forced
    d, forced = risk.raise_decision(ALLOWED, "HIGH", "bash", gate_bypassed=True)
    assert d is ALLOWED and not forced


def test_setting_off_disables_everything(monkeypatch):
    import src.settings as settings
    monkeypatch.setattr(settings, "get_setting",
                        lambda key, default=None: False if key == "self_declared_risk" else default)
    assert not risk.enabled()
    d, forced = risk.raise_decision(ALLOWED, "HIGH", "bash")
    assert d is ALLOWED and not forced


def test_policy_risk_levels():
    assert risk.policy_risk("read_file", "a.py") == "LOW"
    assert risk.policy_risk("write_file", "a.py") == "MEDIUM"
    assert risk.policy_risk("bash", "ls") in ("MEDIUM", "HIGH")
    assert risk.policy_risk("read_file", "a.py", gate_allowed=False) == "HIGH"
    assert risk.policy_risk("mcp__nobody__thing", "{}") == "UNKNOWN"


def test_agreement_and_counters():
    assert risk.agreement(None, "HIGH") == "none"
    assert risk.agreement("HIGH", "HIGH") == "same"
    assert risk.agreement("HIGH", "MEDIUM") == "higher"
    assert risk.agreement("LOW", "MEDIUM") == "lower"
    assert risk.agreement("UNKNOWN", "LOW") == "unknown"
    risk.record(risk.make_record("write_file", "HIGH", "MEDIUM", forced=True))
    risk.record(risk.make_record("write_file", None, "MEDIUM", forced=False))
    risk.record(risk.make_record("bash", "LOW", "MEDIUM", forced=False))
    snap = risk.stats()
    assert snap["calls"] == 3 and snap["declared"] == 2 and snap["forced_approval"] == 1
    assert snap["disagreements"] == 2 and snap["by_agreement"] == {"higher": 1, "none": 1, "lower": 1}
    assert snap["by_tool"]["write_file"] == {"calls": 2, "declared": 1, "disagreed": 1}
    assert snap["declaration_rate"] == round(2 / 3, 4)


# ---------------------------------------------------------------- the loop --

def _collect(gen):
    async def _go():
        return [c async for c in gen]
    return asyncio.run(_go())


def _events(chunks):
    out = []
    for c in chunks:
        if c.startswith("data: ") and not c.startswith("data: [DONE]"):
            try:
                out.append(json.loads(c[6:]))
            except Exception:
                pass
    return out


def _run(monkeypatch, tmp_path, declared, *, tool="write_file", args=None, bypass=False, exec_log=None, tools_seen=None):
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)
    monkeypatch.setattr(al, "blocked_tools_for_owner", lambda owner: set(), raising=False)
    monkeypatch.setattr(al, "_agent_route_tool_mode", lambda *a, **k: (True, False, True), raising=False)

    class _Pending:
        def public_payload(self, reason=None):
            return {"kind": "tool_approval", "reason": reason}

    class _Store:
        created = []

        def create(self, **kw):
            _Store.created.append(kw)
            return _Pending()
    monkeypatch.setattr(al, "tool_approval_store", _Store(), raising=False)

    async def _fake_exec(block, *a, **k):
        if exec_log is not None:
            exec_log.append(block)
        return (block.tool_type, {"output": "ok", "exit_code": 0})
    monkeypatch.setattr(al, "execute_tool_block", _fake_exec, raising=False)
    payload = dict(args or {"path": "a.py", "content": "x = 1"})
    if declared is not None:
        payload["security_risk"] = declared
    n = 0

    async def _fake_stream(_candidates, messages, **kwargs):
        nonlocal n
        n += 1
        if tools_seen is not None:
            tools_seen.append(kwargs.get("tools"))
        if n == 1:
            yield f'data: {json.dumps({"type": "tool_calls", "calls": [{"name": tool, "arguments": json.dumps(payload)}]})}\n\n'
            yield f'data: {json.dumps({"type": "finish", "finish_reason": "tool_calls"})}\n\n'
        else:
            yield f'data: {json.dumps({"delta": "done"})}\n\n'
            yield f'data: {json.dumps({"type": "finish", "finish_reason": "stop"})}\n\n'
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    kwargs = dict(max_rounds=3, relevant_tools={tool, "read_file"})
    if bypass:
        kwargs["security_gate_bypass"] = True
    chunks = _collect(al.stream_agent_loop(
        "http://x/v1", "m", [{"role": "user", "content": "edit a.py"}], workspace=str(tmp_path), **kwargs))
    return _events(chunks)


def test_high_declaration_forces_the_approval_card_and_nothing_runs(monkeypatch, tmp_path):
    executed = []
    events = _run(monkeypatch, tmp_path, "HIGH", exec_log=executed)
    assert executed == [], "a HIGH call waits for the person"
    text = json.dumps(events)
    assert "APPROVAL REQUIRED" in text or "approval" in text.lower()
    metrics = [e for e in events if e.get("type") == "metrics"][-1]["data"]
    rec = metrics["risk_declarations"][0]
    assert rec["tool"] == "write_file" and rec["declared"] == "HIGH" and rec["forced_approval"] is True
    assert risk.stats()["forced_approval"] == 1


@pytest.mark.parametrize("declared", [None, "LOW", "MEDIUM", "UNKNOWN"])
def test_other_declarations_leave_the_call_running_and_the_param_never_reaches_the_tool(monkeypatch, tmp_path, declared):
    executed = []
    events = _run(monkeypatch, tmp_path, declared, exec_log=executed)
    assert len(executed) == 1
    assert "security_risk" not in executed[0].content
    assert json.loads(executed[0].content).get("path") == "a.py" if executed[0].content.strip().startswith("{") else True
    metrics = [e for e in events if e.get("type") == "metrics"][-1]["data"]
    assert metrics["risk_declarations"][0]["forced_approval"] is False
    assert metrics["risk_declarations"][0]["declared"] == (declared or "NONE")


def test_allow_for_this_task_is_honoured(monkeypatch, tmp_path):
    executed = []
    _run(monkeypatch, tmp_path, "HIGH", exec_log=executed, bypass=True)
    assert len(executed) == 1 and "security_risk" not in executed[0].content


def test_read_only_tools_are_not_recorded_or_affected(monkeypatch, tmp_path):
    executed = []
    events = _run(monkeypatch, tmp_path, "HIGH", tool="read_file", args={"path": "a.py"}, exec_log=executed)
    assert len(executed) == 1
    metrics = [e for e in events if e.get("type") == "metrics"][-1]["data"]
    assert "risk_declarations" not in metrics


def test_schemas_sent_to_the_model_carry_the_param_on_writers_only(monkeypatch, tmp_path):
    sent = []
    _run(monkeypatch, tmp_path, None, tools_seen=sent)
    by_name = {t["function"]["name"]: t for t in (sent[0] or [])}
    assert "write_file" in by_name and "read_file" in by_name, sorted(by_name)
    assert "security_risk" in by_name["write_file"]["function"]["parameters"]["properties"]
    assert "security_risk" not in by_name["read_file"]["function"]["parameters"]["properties"]


def test_stats_route(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes import agent_loop_stats_routes
    # the object the routes depend on: a test elsewhere may reload src.auth_helpers
    require_user = agent_loop_stats_routes.require_user
    risk.record(risk.make_record("bash", "HIGH", "MEDIUM", forced=True))
    app = FastAPI()
    app.include_router(agent_loop_stats_routes.setup_agent_loop_stats_routes())
    app.dependency_overrides[require_user] = lambda: "ada"
    body = TestClient(app).get("/api/agent/risk/stats").json()
    assert body["forced_approval"] == 1 and body["enabled"] is True and body["by_tool"]["bash"]["declared"] == 1
