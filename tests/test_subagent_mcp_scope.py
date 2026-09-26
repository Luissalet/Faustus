"""An agent definition governs MCP tools as well as built-in ones.

`tools:` is an allowlist and `deny:` a denylist, and MCP tools used to fall
through both: they are not in the built-in vocabulary, so a reviewer pinned to
`[read_file, grep]` was still offered every connected server's tools. Entries
may now name MCP tools, with `*` for the parts a definition cannot know in
advance (`mcp__github__*`), and a coordinator's allowlist reaches MCP tools of
the workers it starts.
"""

import pytest

import src.agent_tools  # noqa: F401  - resolves the circular schema imports first
from src.agent_defs import AgentDef, AgentDefError, parse
from src.agent_tools import subagent_tools as st
from src.mcp_manager import McpManager
from src.subagent_permissions import coordinator_permissions, derive, tool_name_matches

from tests.test_agent_loop_workspace_tool_floor import tools_sent, workspace  # noqa: F401


def _mcp_manager():
    mgr = McpManager()
    schema = {"type": "object", "properties": {"q": {"type": "string"}}}
    mgr._tools = {
        "github": [
            {"name": "get_issue", "description": "Read one GitHub issue", "inputSchema": schema},
            {"name": "delete_repo", "description": "Delete a repository", "inputSchema": schema},
        ],
        "slack": [{"name": "post_message", "description": "Post a chat message", "inputSchema": schema}],
    }
    return mgr


def test_patterns_match_only_mcp_names_they_cover():
    assert tool_name_matches("mcp__github__*", "mcp__github__get_issue")
    assert not tool_name_matches("mcp__github__*", "mcp__slack__post_message")
    assert tool_name_matches("mcp__*", "mcp__slack__post_message")
    assert tool_name_matches("read_file", "read_file")
    assert not tool_name_matches("read_file", "read_file_x")


def test_definition_accepts_mcp_entries_and_refuses_malformed_ones():
    text = "---\nname: triage\ntools: [read_file, grep, mcp__github__*]\ndeny: [mcp__github__delete_*]\n---\nTriage issues."
    d = parse(text, slug="triage")
    assert "mcp__github__*" in d.tools and "mcp__github__delete_*" in d.deny
    with pytest.raises(AgentDefError):
        parse("---\nname: bad\ntools: [mcp__]\n---\nx", slug="bad")


def test_worker_allowlist_and_denylist_reach_mcp_tools():
    d = AgentDef(slug="triage", prompt="x", tools=("read_file", "mcp__github__*"), deny=("mcp__github__delete_*",))
    perms = derive(None, d)
    assert not perms.tool_denied("mcp__github__get_issue")
    assert perms.tool_denied("mcp__github__delete_repo")
    assert perms.tool_denied("mcp__slack__post_message")
    assert not perms.tool_denied("read_file")


def test_coordinator_allowlist_flows_down_to_mcp_tools(monkeypatch):
    from src import subagent_permissions as sp
    monkeypatch.setattr(sp, "max_depth", lambda: 2)
    lead = AgentDef(slug="lead", prompt="x", tools=("read_file", "delegate_agents", "mcp__github__*"))
    parent = derive(None, lead)
    child = derive(parent, AgentDef(slug="helper", prompt="x"), parent_depth=1)
    assert child.allowed_tools is None           # the helper names no allowlist of its own
    assert child.tool_denied("mcp__slack__post_message")
    assert not child.tool_denied("mcp__github__get_issue")


def test_swarm_round_trip_keeps_inherited_allowlists():
    from src.swarm.runner import _perms_from_dict
    from src import subagent_permissions as sp
    perms = sp.ChildPermissions(slug="w", inherited_allowlists=(frozenset({"mcp__github__*"}),))
    again = _perms_from_dict(perms.to_dict())
    assert again.tool_denied("mcp__slack__post_message")
    assert not again.tool_denied("mcp__github__get_issue")


def test_manager_reports_blocked_tools_by_predicate():
    mgr = _mcp_manager()
    by_server, qualified = mgr.blocked_mcp_where(lambda name: not name.startswith("mcp__github__get"))
    assert qualified == {"mcp__github__delete_repo", "mcp__slack__post_message"}
    assert by_server == {"github": {"delete_repo"}, "slack": {"post_message"}}


class _FixedIndex:
    """Tool retrieval that always picks the three MCP tools, so the test is
    about what the worker's definition removes, not about what retrieval chose."""

    def index_mcp_tools(self, *a, **k):
        return None

    def get_tools_for_query(self, *a, **k):
        from src.tool_index import ALWAYS_AVAILABLE
        return set(ALWAYS_AVAILABLE) | {"read_file", "mcp__github__get_issue",
                                         "mcp__github__delete_repo", "mcp__slack__post_message"}


def test_loop_offers_a_worker_only_the_mcp_tools_its_definition_allows(workspace, monkeypatch):
    import src.tool_index as tool_index
    monkeypatch.setattr(tool_index, "get_tool_index", lambda: _FixedIndex())
    request = "Lee el issue 12 de github y resume"
    # Control first: with no worker definition the selector offers the tool.
    free = set(tools_sent(request, str(workspace), mcp_manager=_mcp_manager()))
    assert {"mcp__github__get_issue", "mcp__github__delete_repo", "mcp__slack__post_message"} <= free
    for deny, gone in ((("mcp__github__*",), "mcp__github__get_issue"),
                       (("mcp__github__get_*",), "mcp__github__get_issue")):
        perms = derive(None, AgentDef(slug="triage", prompt="x", deny=deny), workspace=str(workspace))
        token = st._PERMS_CTX.set(perms)
        try:
            names = set(tools_sent(request, str(workspace), mcp_manager=_mcp_manager()))
        finally:
            st._PERMS_CTX.reset(token)
        assert gone not in names
        assert "mcp__slack__post_message" in names
    # An allowlist that names the server keeps it.
    perms = derive(None, AgentDef(slug="triage", prompt="x", tools=("read_file", "mcp__github__get_issue")), workspace=str(workspace))
    token = st._PERMS_CTX.set(perms)
    try:
        names = set(tools_sent(request, str(workspace), mcp_manager=_mcp_manager()))
    finally:
        st._PERMS_CTX.reset(token)
    assert "mcp__github__get_issue" in names
    assert not names & {"mcp__github__delete_repo", "mcp__slack__post_message"}
