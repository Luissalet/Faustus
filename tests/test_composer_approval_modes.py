import asyncio
import json
import pytest
from src import tool_capabilities as caps

@pytest.mark.parametrize("mode,desktop,external", [("ask",False,False),("auto",True,False),("full",True,True),("invalid",False,False)])
def test_approval_modes_reach_the_runtime_gate(monkeypatch, mode, desktop, external):
    monkeypatch.setattr(caps, "get_setting", lambda key, default=None: {"tool_approval_mode": mode, "desktop_control_mode":"ask_each"}.get(key, default))
    assert caps.ToolRunSecurityContext().decision_for("desktop_key", '{"combo":"alt+f4"}').allowed == desktop
    assert caps.ToolRunSecurityContext(external_untrusted_context_seen=True).decision_for("send_email", '{}').allowed == external


def test_auto_keeps_destructive_guard_and_full_is_explicit(monkeypatch):
    monkeypatch.setattr(caps, "_command_guard_denial", lambda *a, **k: "dangerous command")
    monkeypatch.setattr(caps, "tool_approval_mode", lambda: "auto")
    assert not caps.ToolRunSecurityContext(approval_gate_bypassed=True).decision_for("bash", "dangerous").allowed
    monkeypatch.setattr(caps, "tool_approval_mode", lambda: "full")
    assert caps.ToolRunSecurityContext().decision_for("bash", "dangerous").allowed


@pytest.mark.parametrize("action", ["set", "delete"])
def test_agent_cannot_change_its_approval_mode(monkeypatch, action):
    from src.agent_tools.admin_tools import do_manage_settings
    result = asyncio.run(do_manage_settings(json.dumps({"action":action,"key":"tool_approval_mode","value":"full"})))
    assert result["exit_code"] == 1
    assert "permissions selector" in result["error"]


def test_approval_resume_restores_task_and_recovers_card_echo(monkeypatch):
    from src import agent_loop as loop
    from src.tool_approvals import ToolApprovalStore
    from src.tool_capabilities import capabilities_for_action
    store = ToolApprovalStore()
    request = "Controla mi pantalla y cierra la ventana de Test Editor"
    pending = store.create(owner="admin", session_id="resume-test", origin_run_id="original", tool_name="desktop_focus_window", content='{"title":"Test Editor"}', workspace=None, external_untrusted_context_seen=True, continuation_query=request, selected_tools=["desktop_key","desktop_focus_window","desktop_list_windows"], capabilities=capabilities_for_action("desktop_focus_window", '{}'))
    grant = store.consume(pending.approval_id, decision="approve_task", owner="admin", session_id="resume-test")
    captured = []
    executed = []
    async def stream(candidates, messages, **kwargs):
        captured.append(list(messages))
        if len(captured) == 1:
            value = {"delta":"Allow this task to continue?"}
        elif len(captured) == 2:
            value = {"type":"tool_calls", "calls":[{"name":"desktop_key", "arguments":json.dumps({"combo":"alt+f4"})}]}
        else:
            value = {"delta":"La herramienta de cierre ha terminado."}
        yield "data: " + json.dumps(value) + "\n\n"
        yield "data: [DONE]\n\n"
    async def execute(block, **kwargs):
        executed.append(block.tool_type)
        return block.tool_type, {"output":"ok", "exit_code":0}
    monkeypatch.setattr(loop,"stream_llm_with_fallback",stream)
    monkeypatch.setattr(loop,"execute_tool_block",execute)
    monkeypatch.setattr(loop,"get_mcp_manager",lambda:None)
    monkeypatch.setattr(loop,"blocked_tools_for_owner",lambda owner:set())
    monkeypatch.setattr(loop,"_agent_route_tool_mode",lambda *a,**k:(True,False,True))
    monkeypatch.setattr(loop,"estimate_tokens",lambda *a,**k:10)
    monkeypatch.setattr(loop,"get_setting",lambda k,d=None: {"agent_auto_continue_cycles":0}.get(k,d))
    monkeypatch.setattr(caps,"tool_approval_mode",lambda:"auto")
    from src.agent_tools import desktop_tools
    monkeypatch.setattr(desktop_tools,"desktop_availability",lambda:(True,"test"))
    async def drive():
        return [chunk async for chunk in loop.stream_agent_loop(endpoint_url="http://127.0.0.1:11434/v1/chat/completions",model="qwen3.8:27b-q8_0",messages=[{"role":"user","content":request},{"role":"assistant","content":"Allow this task to continue?"}],headers={},owner="admin",session_id="resume-test",exact_approval=grant,max_rounds=4,context_length=32768,harness_options={"checkpoints":False,"run_tests":False,"repo_map":False})]
    events = asyncio.run(drive())
    assert any(request in str(m.get("content")) and "sealed action" in str(m.get("content")) for m in captured[0])
    assert "approval_echo" in "".join(events)
    assert executed == ["desktop_focus_window", "desktop_key"], "".join(events)

