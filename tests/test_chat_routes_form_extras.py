"""Chat-stream form extras added by the reliability work: per-session
generation overrides and the /agents delegation payload."""

import json

from routes.chat_routes import _parse_gen_overrides, _parse_delegate_tasks, _delegation_instruction


def test_gen_overrides_validation():
    raw = json.dumps({"temperature": 0.3, "max_tokens": 8192, "top_p": 0.9, "think": False, "bogus": 1, "top_k": 5000})
    out = _parse_gen_overrides(raw)
    assert out["temperature"] == 0.3 and out["max_tokens"] == 8192 and out["top_p"] == 0.9
    assert "bogus" not in out and "top_k" not in out  # unknown / out of range dropped
    assert _parse_gen_overrides("not json") == {} and _parse_gen_overrides(None) == {}
    assert _parse_gen_overrides(json.dumps({"temperature": 7})) == {}


def test_delegate_tasks_payload_is_validated_and_capped():
    raw = json.dumps({"tasks": [
        {"name": "a", "instruction": "Añade el botón de borrar en las tarjetas"},
        "Escribe tests para sessions.js",
        {"name": "empty", "instruction": "   "},
        {"instruction": "t4"}, {"instruction": "t5"}, {"instruction": "t6"},
    ], "parallel": False})
    out = _parse_delegate_tasks(raw)
    assert out["parallel"] is False
    names = [t["name"] for t in out["tasks"]]
    assert names[0] == "a" and names[1].startswith("Escribe tests")
    assert len(out["tasks"]) <= 4 and all(t["instruction"].strip() for t in out["tasks"])
    assert _parse_delegate_tasks("") is None and _parse_delegate_tasks("{}") is None
    assert _parse_delegate_tasks(json.dumps({"tasks": [{"instruction": ""}]})) is None


def test_delegation_instruction_names_the_tool_once_and_keeps_tasks():
    payload = _parse_delegate_tasks(json.dumps({"tasks": ["Tarea uno", "Tarea dos"]}))
    text = _delegation_instruction(payload)
    assert text.count("delegate_agents") == 1 and "EXACTLY ONCE" in text
    assert "Tarea uno" in text and "Tarea dos" in text
    # Evidence-based reporting is part of the instruction.
    assert "evidence" in text


def test_workspace_bound_coding_chat_is_escalated_to_agent():
    """Plain chat + bound workspace + a coding request (any language the
    heuristic knows) must run the agent loop: without tools the model can only
    narrate edits it never made."""
    from pathlib import Path
    from src.agent_loop import _looks_like_workspace_coding_request as looks
    src = (Path(__file__).resolve().parent.parent / "routes" / "chat_routes.py").read_text(encoding="utf-8")
    block = src[src.index("workspace-bound coding request"):]
    assert "_looks_like_workspace_coding_request(message)" in src
    assert 'chat_mode == "chat"' in src[src.index("_looks_like_workspace_coding_request(message)") - 400:src.index("_looks_like_workspace_coding_request(message)")]
    # the heuristic itself is multilingual
    assert looks("Añade a la interfaz botones para eliminar proyectos y chats en sus tarjetas")
    assert looks("Corrige el bug de static/js/cards.js")
    assert looks("Fix the failing test in the repo")
    assert not looks("¿Qué tal estás hoy?")
    assert not looks("Explain what a closure is")


def test_activity_snapshot_helpers():
    """Sidebar activity dots: detached runs + pending approvals in one call."""
    from src import agent_runs
    from src.tool_approvals import ToolApprovalStore

    class _R:
        def __init__(self, status):
            self.status = status
    saved = dict(agent_runs._RUNS)
    try:
        agent_runs._RUNS.clear()
        agent_runs._RUNS.update({"a": _R("running"), "b": _R("done"), "c": _R("running")})
        assert sorted(agent_runs.active_session_ids()) == ["a", "c"]
        # Sub-agent worker chats have no detached run of their own: they are
        # flagged busy explicitly for the duration of the worker.
        agent_runs.mark_busy("w1")
        agent_runs.mark_busy("a")   # already running → not duplicated
        assert sorted(agent_runs.active_session_ids()) == ["a", "c", "w1"]
        agent_runs.clear_busy("w1")
        agent_runs.clear_busy("nope")  # unknown id is a no-op
        assert sorted(agent_runs.active_session_ids()) == ["a", "c"]
        assert not agent_runs.is_active("w1")
    finally:
        agent_runs._RUNS.clear()
        agent_runs._RUNS.update(saved)
        agent_runs._EXTERNAL_BUSY.clear()

    from src.tool_capabilities import capabilities_for_action
    store = ToolApprovalStore()
    content = '{"path": "x", "old_string": "a", "new_string": "b"}'
    store.create(owner="luis", session_id="s1", origin_run_id="r1", tool_name="edit_file",
                 content=content, workspace=None, external_untrusted_context_seen=True,
                 capabilities=capabilities_for_action("edit_file", content))
    assert store.pending_session_ids(owner="luis") == ["s1"]
    assert store.pending_session_ids(owner="someone-else") == []
    store.retire_for_session(owner="luis", session_id="s1")
    assert store.pending_session_ids(owner="luis") == []
