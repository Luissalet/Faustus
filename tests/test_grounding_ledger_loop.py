"""The figure ledger wired into the agent loop: one correction request for a
figure no tool gave, then the figure is struck through instead of passing."""

import asyncio
import json

import src.agent_loop as al
from src.context_tool_gate import ContextAwareSecurityContext
from src.tool_capabilities import ToolGateDecision


def _collect(gen):
    async def _run():
        return [c async for c in gen]
    return asyncio.run(_run())


def _events(chunks):
    out = []
    for c in chunks:
        if c.startswith("data: ") and not c.startswith("data: [DONE]"):
            try:
                out.append(json.loads(c[6:]))
            except Exception:
                pass
    return out


def _patch(monkeypatch, enabled, answers):
    settings = {"agent_answer_grounding_ledger": enabled}
    monkeypatch.setattr(al, "get_setting", lambda k, d=None: settings.get(k, d), raising=False)
    import src.grounding_ledger as gl
    monkeypatch.setattr(gl, "is_enabled", lambda: enabled)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)
    monkeypatch.setattr(al, "blocked_tools_for_owner", lambda owner: set(), raising=False)
    monkeypatch.setattr(ContextAwareSecurityContext, "decision_for",
                        lambda self, tool_name, content=None: ToolGateDecision(True), raising=False)

    async def _exec(block, *a, **k):
        return (block.tool_type, {"output": "Ventas de marzo: 1200 unidades; abril: 1500 unidades.",
                                  "exit_code": 0})
    monkeypatch.setattr(al, "execute_tool_block", _exec, raising=False)
    seen = []

    async def _stream(_c, messages, **kwargs):
        seen.append(list(messages))
        i = len(seen) - 1
        if i == 0:
            yield "data: " + json.dumps({"type": "tool_calls", "calls": [
                {"name": "bash", "arguments": json.dumps({"command": "cat ventas.txt"})}]}) + "\n\n"
            yield "data: " + json.dumps({"type": "finish", "finish_reason": "tool_calls"}) + "\n\n"
        else:
            yield "data: " + json.dumps({"delta": answers[min(i - 1, len(answers) - 1)]}) + "\n\n"
            yield "data: " + json.dumps({"type": "finish", "finish_reason": "stop"}) + "\n\n"
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_llm_with_fallback", _stream, raising=False)
    return seen


def _run(tmp_path):
    return _events(_collect(al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "qwen3.8:27b",
        [{"role": "user", "content": "¿Cuánto crecieron las ventas de marzo a abril? Mira ventas.txt"}],
        max_rounds=6, relevant_tools={"bash"}, workspace=str(tmp_path))))


def test_an_unsupported_figure_gets_one_correction_then_is_struck_through(tmp_path, monkeypatch):
    seen = _patch(monkeypatch, True, ["Las ventas crecieron un 37 % (de 1200 a 1500).",
                                      "Las ventas crecieron un 37 % (de 1200 a 1500)."])
    events = _run(tmp_path)
    rejected = [e for e in events if e.get("type") == "harness_check" and e.get("status") == "rejected"]
    assert rejected and "ungrounded_figure" in rejected[0]["reasons"]
    assert any(e.get("type") == "harness_check" and e.get("status") == "figures_marked" for e in events)
    replaced = [e["text"] for e in events if e.get("type") == "response_replace"]
    assert replaced and "~~37" in replaced[-1]
    assert len(seen) == 3  # tool round, first answer, the one correction


def test_a_supported_figure_passes_untouched(tmp_path, monkeypatch):
    seen = _patch(monkeypatch, True, ["Las ventas crecieron un 25 % (de 1200 a 1500)."])
    events = _run(tmp_path)
    assert not any(e.get("status") in ("rejected", "figures_marked") for e in events
                   if e.get("type") == "harness_check")
    assert len(seen) == 2


def test_off_by_default_changes_nothing(tmp_path, monkeypatch):
    seen = _patch(monkeypatch, False, ["Las ventas crecieron un 37 % (de 1200 a 1500)."])
    events = _run(tmp_path)
    assert not any(e.get("status") in ("rejected", "figures_marked") for e in events
                   if e.get("type") == "harness_check")
    assert len(seen) == 2
