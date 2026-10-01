"""A step announces MCP tools with the loop's own security_risk parameter;
the live server definition does not have it. That is not a contract change."""
import copy

from src import self_declared_risk
from src.step_snapshot import authorize_call, capture_step

NAME = "mcp__srv1__faustus_attention"
RAW = {"type": "object", "title": "Args",
       "properties": {"wait_min": {"type": "number", "default": 10, "minimum": 0, "maximum": 1440, "title": "Wait Min"}}}


def _snapshot(params):
    tools = [{"type": "function", "function": {"name": NAME, "description": "[MCP:x] y", "parameters": params}}]
    tools = self_declared_risk.with_risk_param(tools)
    return tools, capture_step(tools, session_id="s", run_id="r", round_num=1, candidate_index=0)


def test_injected_risk_param_is_not_a_contract_change(monkeypatch):
    monkeypatch.setattr(self_declared_risk, "wants_param", lambda name: True)
    tools, snap = _snapshot(copy.deepcopy(RAW))
    assert "security_risk" in tools[0]["function"]["parameters"]["properties"]
    live = {"name": NAME, "description": "other text", "parameters": copy.deepcopy(RAW)}
    verdict = authorize_call(snap, NAME, session_id="s", live_definition=lambda n: live)
    assert verdict.allowed and verdict.status == "ok", verdict


def test_a_real_change_is_still_caught(monkeypatch):
    monkeypatch.setattr(self_declared_risk, "wants_param", lambda name: True)
    _, snap = _snapshot(copy.deepcopy(RAW))
    changed = copy.deepcopy(RAW)
    changed["properties"]["wait_min"]["type"] = "string"
    live = {"name": NAME, "description": "", "parameters": changed}
    verdict = authorize_call(snap, NAME, session_id="s", live_definition=lambda n: live)
    assert not verdict.allowed and verdict.status == "contract_changed"


def test_a_server_that_declares_security_risk_itself_is_compared_as_is(monkeypatch):
    monkeypatch.setattr(self_declared_risk, "wants_param", lambda name: True)
    own = copy.deepcopy(RAW)
    own["properties"]["security_risk"] = {"type": "string"}
    _, snap = _snapshot(copy.deepcopy(own))
    live = {"name": NAME, "description": "", "parameters": copy.deepcopy(RAW)}
    verdict = authorize_call(snap, NAME, session_id="s", live_definition=lambda n: live)
    assert verdict.status == "contract_changed"


def test_tool_with_no_properties_gets_the_param_and_still_passes(monkeypatch):
    monkeypatch.setattr(self_declared_risk, "wants_param", lambda name: True)
    _, snap = _snapshot({"type": "object"})
    live = {"name": NAME, "description": "", "parameters": {"type": "object"}}
    verdict = authorize_call(snap, NAME, session_id="s", live_definition=lambda n: live)
    assert verdict.allowed, verdict
