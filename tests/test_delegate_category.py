"""delegate_agents `category` (#248): the coordinator gives a worker one tool
category and every gate holds it -- the tools offered to the model, the
execution guard and the workspace tool floor."""
from __future__ import annotations

import json

import pytest

from src.agent_tools import subagent_tools as st


def _task_schema():
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    tool = next(t for t in FUNCTION_TOOL_SCHEMAS if t["function"]["name"] == "delegate_agents")
    return tool["function"]["parameters"]["properties"]["tasks"]["items"]


def test_schema_offers_an_optional_category():
    item = _task_schema()
    assert item["properties"]["category"]["type"] == "string"
    assert item["required"] == ["instruction"]


def test_parser_normalizes_categories_and_omits_them_when_absent():
    args = st.parse_delegation_args(json.dumps({"tasks": [
        {"instruction": "a", "category": "Web, FILES"},
        {"instruction": "b", "category": ["git", "mcp:github"]},
        {"instruction": "c"},
    ]}))
    rows = args["tasks"]
    assert rows[0]["category"] == ["web", "files"]
    assert rows[1]["category"] == ["git", "mcp:github"]
    assert "category" not in rows[2]


def test_an_unknown_category_refuses_instead_of_granting_everything():
    with pytest.raises(ValueError) as exc:
        st.parse_delegation_args(json.dumps({"tasks": [{"instruction": "a", "category": "weather"}]}))
    assert "weather" in str(exc.value) and "web" in str(exc.value)


def test_allowance_is_the_category_plus_the_helpers():
    web = st.category_allowance(["web"])
    assert {"web_search", "web_fetch", "lookup_tools", "todowrite"} <= web
    assert "bash" not in web and "read_file" not in web
    assert "mcp__github__*" in st.category_allowance(["mcp:github"])
    assert "mcp__*" in st.category_allowance(["mcp"])


def _attach(tasks, ctx=None):
    runs = [st.SubagentRun(i, t) for i, t in enumerate(tasks)]
    assert st._attach_permissions(runs, ctx or {}, None, None) == ""
    assert st._attach_categories(runs, ctx or {}, None, None) == ""
    return runs


def test_a_web_worker_is_offered_web_tools_and_denied_the_rest():
    web, plain = _attach([{"name": "w", "instruction": "investiga", "category": ["web"]},
                          {"name": "p", "instruction": "investiga"}])
    assert plain.permissions is None                      # untouched path
    denied = st.worker_disabled_tools(web.instruction, web.permissions)
    assert "bash" in denied and "read_file" in denied and "write_file" in denied
    assert "web_search" not in denied and "web_fetch" not in denied   # not lost to the lean guess
    perms = web.permissions
    assert perms.tool_denied("bash") and perms.tool_denied("mcp__github__create_issue")
    assert not perms.tool_denied("web_fetch") and not perms.tool_denied("lookup_tools")
    assert "category web" in " ".join(perms.caveats)
    assert web.report()["tool_category"] == ["web"] and "tool_category" not in plain.report()


def test_an_mcp_server_category_reaches_that_server_only():
    (run,) = _attach([{"name": "g", "instruction": "x", "category": ["mcp:github"]}])
    assert not run.permissions.tool_denied("mcp__github__create_issue")
    assert run.permissions.tool_denied("mcp__slack__post") and run.permissions.tool_denied("bash")


def test_a_category_only_narrows_an_agent_definition():
    runs = [st.SubagentRun(0, {"name": "r", "instruction": "x", "category": ["web"],
                               "agent": "reader", "agent_def": {"slug": "reader", "name": "Reader",
                                                                 "tools": ["read_file", "web_search"]}})]
    assert st._attach_permissions(runs, {}, None, None) == ""
    assert st._attach_categories(runs, {}, None, None) == ""
    perms = runs[0].permissions
    assert not perms.tool_denied("web_search")
    assert perms.tool_denied("web_fetch")     # in the category, never granted by the definition
    assert perms.tool_denied("read_file")     # granted by the definition, outside the category


def test_the_refusal_names_the_category():
    (run,) = _attach([{"name": "w", "instruction": "x", "category": ["web"]}])
    from src.agent_loop import DENIAL_ORIGIN_REQUEST
    assert st._refusal_cause(run, {"bash"}, "", DENIAL_ORIGIN_REQUEST) == "outside this worker's tool category (web)"
    assert st._refusal_cause(run, {"delegate_agents"}, "", DENIAL_ORIGIN_REQUEST) == "not available to sub-agents"


from tests.test_agent_loop_workspace_tool_floor import (  # noqa: E402
    SPANISH_REQUEST,
    tools_sent,
    workspace,  # noqa: F401  - the fixture
)


def test_the_real_workspace_floor_does_not_hand_file_tools_back(workspace):
    """The floor restores read_file/ls/edit_file/apply_patch for a bound
    folder; for a worker limited to `web` it must not."""
    import src.agent_tools  # noqa: F401
    (run,) = _attach([{"name": "w", "instruction": SPANISH_REQUEST, "category": ["web"]}])
    token = st._PERMS_CTX.set(run.permissions)
    try:
        offered = set(tools_sent(SPANISH_REQUEST, workspace,
                                 disabled_tools=st.worker_disabled_tools(SPANISH_REQUEST, run.permissions)))
    finally:
        st._PERMS_CTX.reset(token)
    assert not offered & {"read_file", "ls", "edit_file", "apply_patch", "bash", "write_file"}
    plain = set(tools_sent(SPANISH_REQUEST, workspace,
                           disabled_tools=st.worker_disabled_tools(SPANISH_REQUEST, None)))
    assert {"read_file", "edit_file"} <= plain


class _SM:
    def __init__(self):
        self.sessions = {}

    def create_session(self, session_id, **kw):
        self.sessions[session_id] = type(
            "S", (), {"messages": [], "add_message": lambda self, m: self.messages.append(m)}
        )()

    def get_session(self, sid):
        return self.sessions.get(sid)

    def save_sessions(self):
        pass


def _summary():
    return "data: " + json.dumps({"type": "harness_summary", "data": {"mutations": [], "stop_reason": "complete"}}) + "\n\n"


@pytest.mark.asyncio
async def test_delegate_agents_runs_each_worker_with_its_category(tmp_path, monkeypatch):
    import src.agent_loop as al
    import src.ai_interaction as ai
    from src import tool_execution as te

    seen = {}

    async def _loop(endpoint_url, model, messages, **kwargs):
        text = messages[0]["content"]
        key = "web" if "busca fuentes" in text else "plain"
        seen[key] = {"disabled": set(kwargs.get("disabled_tools") or ()), "prompt": text}
        yield _summary()
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_agent_loop", _loop)
    monkeypatch.setattr(te, "get_active_workspace", lambda: str(tmp_path))
    monkeypatch.setattr(te, "get_active_workspace_roots", lambda: ())
    parent = type("P", (), {"endpoint_url": "http://127.0.0.1:8081/v1", "model": "m", "headers": None,
                            "name": "parent"})()
    sm = _SM()
    sm.sessions["parent"] = parent
    monkeypatch.setattr(ai, "get_session_manager", lambda: sm)

    tool = st.DelegateAgentsTool()
    result = await tool.execute(json.dumps({"tasks": [
        {"name": "W", "instruction": "busca fuentes sobre el tema", "category": "web"},
        {"name": "P", "instruction": "resume el fichero notas.md"},
    ], "parallel": True}), {"session_id": "parent", "owner": None, "progress_cb": None})

    by_name = {r["name"]: r for r in result["subagents"]}
    assert by_name["W"]["tool_category"] == ["web"] and "tool_category" not in by_name["P"]
    assert "bash" in seen["web"]["disabled"] and "web_search" not in seen["web"]["disabled"]
    assert "limited this run to the tool category web" in seen["web"]["prompt"]
    assert "bash" not in seen["plain"]["disabled"]

    refused = await tool.execute(json.dumps({"tasks": [{"instruction": "x", "category": "weather"}]}),
                                 {"session_id": "parent", "owner": None, "progress_cb": None})
    assert refused["exit_code"] == 1 and "weather" in refused["error"]


def test_the_persisted_board_keeps_the_category():
    import src.agent_loop as al
    rows = al._compact_subagent_reports([{"name": "W", "tool_category": ["web"]}, {"name": "P"}])
    assert rows[0]["tool_category"] == ["web"] and "tool_category" not in rows[1]