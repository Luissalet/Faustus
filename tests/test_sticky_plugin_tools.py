"""A plugin the conversation is already using keeps its tools on the next
turn, whatever the latest message looks like to the intent classifier.

Seen live: the assistant asked the first flashcard, the user answered
"creo que era el fichero plugin.json", "fichero" selected the files domain,
the grading tool was gone and the 27B spent ninety seconds finding it
again through `lookup_tools`.
"""
from src import agent_loop as al

HYPATIA = ["mcp__4f9230b5__cards_due", "mcp__4f9230b5__card_review", "mcp__4f9230b5__cards_add"]
BORGES = ["mcp__02e5e776__library_search", "mcp__02e5e776__library_read"]
EMAIL = ["mcp__email__list_emails"]


def _assistant(*tools):
    return {"role": "assistant", "content": "…", "metadata": {"tool_events": [{"tool": t} for t in tools]}}


def test_servers_called_in_the_last_turns_keep_their_tools():
    msgs = [
        {"role": "user", "content": "examíname"},
        _assistant("lookup_tools", "mcp__4f9230b5__cards_due"),
        {"role": "user", "content": "creo que era el fichero plugin.json"},
    ]
    assert al._mcp_servers_used_recently(msgs) == {"mcp__4f9230b5__"}
    assert al._sticky_mcp_tool_names(msgs, HYPATIA + BORGES + EMAIL) == set(HYPATIA)


def test_the_legacy_mcp_event_shape_resolves_through_its_description():
    msgs = [_assistant(), {"role": "user", "content": "siguiente"}]
    msgs[0]["metadata"]["tool_events"] = [{"tool": "mcp", "desc": "mcp: mcp__02e5e776__library_search"}]
    assert al._sticky_mcp_tool_names(msgs, HYPATIA + BORGES) == set(BORGES)


def test_only_the_recent_turns_count_and_builtins_never_stick():
    msgs = [_assistant("mcp__4f9230b5__cards_due")] + [_assistant("read_file") for _ in range(3)]
    assert al._mcp_servers_used_recently(msgs, turns=3) == set()
    assert al._sticky_mcp_tool_names(msgs, HYPATIA) == set()
    assert al._sticky_mcp_tool_names([], HYPATIA) == set()


def test_the_cap_bounds_what_a_chatty_server_can_pin():
    msgs = [_assistant("mcp__big__t0")]
    names = [f"mcp__big__t{i}" for i in range(80)]
    assert len(al._sticky_mcp_tool_names(msgs, names, cap=30)) == 30
