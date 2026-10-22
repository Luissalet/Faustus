"""A new system is decided with the user before the first file is written.

10-09-2026, live on the 7001 instance with qwen3.8:27b-q4_K_M: asked to
"implement a per-user preferences system" in a workspace, the model read
the five files and wrote preferences.py (JSON on disk, a CLI) without a
word - the tool description alone did not make it ask. The rule now sits
in the effective "Base rules" (both the fenced and the native variant) and
in the workspace coding block, which is where "implement X in this
project" lands. Note: src/agent_loop.py defines _AGENT_RULES twice; the
second definition is the one the model sees, so these tests read the
module attributes, never the file.

15-10-2026 (d3475e1c): the rule was turned round on purpose. Asking first
stalled unattended and benchmark runs; the agent now builds the design it
would recommend and says in one line which one it picked, so the user can
redirect. `ask_user` stays for what it cannot find out or infer. These
tests pin the current rule.
"""
from src import agent_loop


def _rule_in(rules: str, marker: str) -> str:
    start = rules.index(marker)
    return rules[start:rules.index("\n", start)]


def test_both_effective_rule_sets_carry_the_rule_once():
    for rules in (agent_loop._AGENT_RULES, agent_loop._API_AGENT_RULES):
        assert rules.startswith("## Base rules"), "the second definition is the live one"
        assert rules.count("DO THE WORK, DO NOT STOP TO ASK") == 1
        assert "A NEW SYSTEM IS DECIDED WITH THE USER FIRST" not in rules
        rule = _rule_in(rules, "DO THE WORK, DO NOT STOP TO ASK")
        assert "pick the design you would recommend, build it" in rule
        assert "say in one line which one you picked" in rule
        # ask_user stays for what the agent cannot find out itself.
        assert "`ask_user` is only for what you cannot find out or infer yourself" in rule


def test_the_workspace_coding_block_says_it_too():
    block = agent_loop._workspace_coding_rules("D:/some/project")
    rule = _rule_in(block, "Do the work, do not stop to ask")
    assert "pick the one you would recommend, build it" in rule and "`ask_user`" in rule
    assert block.index("Start by orienting") < block.index("Do the work, do not stop to ask")
    assert agent_loop._workspace_coding_rules(None) == ""


def test_ask_user_is_always_offered_to_the_agent(monkeypatch):
    """The rule above is worthless if `ask_user` is not on the turn's list.

    This used to be checked by looking for one line of agent_loop.py in the
    file. It went red the day a third primitive joined the set -- the rule
    was MORE true, and the test said less true -- so it asks the prompt
    builder instead: whatever retrieval selected, the loop primitives are
    there and are never deferred to the catalog.
    """
    seen = {}

    def fake_assemble(tool_names, disabled, **kwargs):
        seen["tools"] = set(tool_names)
        seen["deferred"] = set(kwargs.get("deferred_tools") or ())
        return "prompt"

    monkeypatch.setattr(agent_loop, "_assemble_prompt", fake_assemble)
    agent_loop._build_base_prompt(
        disabled_tools=set(),
        mcp_mgr=None,
        needs_admin=False,
        relevant_tools={"bash"},
        deferred_tools={"ask_user", "update_plan", "lookup_tools", "python"},
    )

    assert {"ask_user", "update_plan", "lookup_tools"} <= seen["tools"]
    assert not ({"ask_user", "update_plan", "lookup_tools"} & seen["deferred"]), (
        "a loop primitive must never be a catalog one-liner the model has to look up")
