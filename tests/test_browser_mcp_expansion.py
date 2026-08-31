"""One peripheral browser tool must not drag in the whole Playwright set.

Seen live (bench t10): "Añade a server.py una función health() que devuelva
{"status": "ok"}" made the semantic tool index return
mcp__builtin_browser__browser_console_messages, whose expansion added 28 tool
schemas. The turn went out with 75 tools and a ~38k-token prompt on a 9B model
(10.8 s to first token) instead of ~30 tools and ~12k — and the project's own
AGENTS.md rules ended up at 0.1 % of the prompt, ignored.
"""
from src import agent_loop as al

P = "mcp__builtin_browser__"
_ALL = [f"{P}browser_navigate", f"{P}browser_click", f"{P}browser_snapshot",
        f"{P}browser_console_messages", f"{P}browser_take_screenshot",
        f"{P}browser_network_requests", f"{P}browser_tabs"]


class _Mgr:
    def __init__(self, disabled=()):
        self.disabled = set(disabled)

    def get_all_tools(self):
        return [{"server_id": "builtin_browser", "qualified_name": n,
                 "is_disabled": n in self.disabled} for n in _ALL] + [
            {"server_id": "other", "qualified_name": "mcp__other__thing", "is_disabled": False}]


def test_route_level_browser_intent_still_expands_to_everything():
    got = al._expand_browser_mcp_tools({"builtin_browser", "read_file"}, _Mgr())
    assert set(_ALL) <= got
    assert "read_file" in got
    assert "mcp__other__thing" not in got


def test_a_session_starting_tool_expands():
    got = al._expand_browser_mcp_tools({f"{P}browser_navigate"}, _Mgr())
    assert set(_ALL) <= got


def test_two_browser_tools_are_intent_enough():
    got = al._expand_browser_mcp_tools(
        {f"{P}browser_click", f"{P}browser_console_messages"}, _Mgr())
    assert set(_ALL) <= got


def test_a_lone_peripheral_hit_is_kept_but_never_expanded():
    names = {f"{P}browser_console_messages", "read_file", "edit_file"}
    got = al._expand_browser_mcp_tools(names, _Mgr())
    assert got == names


def test_a_lone_screenshot_hit_is_not_intent_either():
    names = {f"{P}browser_take_screenshot", "bash"}
    assert al._expand_browser_mcp_tools(names, _Mgr()) == names


def test_no_browser_tools_at_all_is_untouched():
    names = {"read_file", "bash"}
    assert al._expand_browser_mcp_tools(names, _Mgr()) == names


def test_disabled_browser_tools_stay_out_of_the_expansion():
    got = al._expand_browser_mcp_tools({"builtin_browser"},
                                       _Mgr(disabled=[f"{P}browser_click"]))
    assert f"{P}browser_click" not in got
    assert f"{P}browser_navigate" in got


def test_without_an_mcp_manager_nothing_changes():
    names = {f"{P}browser_navigate"}
    assert al._expand_browser_mcp_tools(names, None) == names


def test_intent_predicate_directly():
    assert al._browser_intent_is_real({"builtin_browser"})
    assert al._browser_intent_is_real({f"{P}browser_navigate"})
    assert al._browser_intent_is_real({f"{P}browser_click", f"{P}browser_hover"})
    assert not al._browser_intent_is_real({f"{P}browser_console_messages"})
    assert not al._browser_intent_is_real({"read_file"})
    assert not al._browser_intent_is_real(set())
