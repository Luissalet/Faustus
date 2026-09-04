"""Derived permissions for a delegated worker (src/subagent_permissions.py).

The one sentence this module exists for, and the one these tests are written
to break: **a restriction a human placed on the parent cannot be laundered by
delegating.** Everything else here is the mechanism that has to hold for that
sentence to be true —

* denies flow down and allows do not, which is an ORDERING fact: the parent's
  denies are appended after the child's own rules so "last match wins" cannot
  be talked out of them;
* a parent's allowlist flows down as the deny of its complement, because a
  human who pinned the parent to `read_file` took `bash` away from the work,
  not from one process;
* a child cannot delegate unless its own definition asks to, the parent may,
  and the depth ceiling has room;
* the depth refusal names the limit and the setting, because "too deep" alone
  sends its reader source-diving.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import agent_defs as defs                      # noqa: E402
from src import subagent_permissions as perms           # noqa: E402


def _def(slug="child", *, mode="worker", tools=(), deny=(), rules=()):
    return defs.AgentDef(slug=slug, name=slug, mode=mode, tools=tuple(tools), deny=tuple(deny),
                         permission=tuple(defs.parse_rule(r) for r in rules))


@pytest.fixture()
def ceiling_one(monkeypatch):
    """The shipped ceiling, pinned so a settings file cannot move these tests."""
    monkeypatch.setattr(perms, "max_depth", lambda: 1)


# ── the case the whole contract exists for ────────────────────────────────

def test_a_parent_deny_cannot_be_laundered_by_a_child_that_allows_it(ceiling_one):
    parent = perms.coordinator_permissions([defs.parse_rule("deny write src/**")], workspace="/w")
    child = perms.derive(parent, _def(rules=("allow write **", "allow write src/**")),
                         parent_depth=0, workspace="/w")
    assert child.path_denied("write", "src/app.py") is True
    assert "deny write src/**" in child.why_path_denied("write", "src/app.py")
    # And only there: the child's own allow still governs everything the
    # parent said nothing about.
    assert child.path_denied("write", "tests/test_app.py") is False


def test_a_parent_deny_survives_into_the_child(ceiling_one):
    parent = perms.coordinator_permissions([defs.parse_rule("deny read secrets/**")])
    child = perms.derive(parent, _def(), parent_depth=0)
    assert child.path_denied("read", "secrets/key.pem") is True
    assert any(r.as_text() == "deny read secrets/**" for r in child.rules)


def test_a_parent_allow_does_not_widen_a_child_that_denies(ceiling_one):
    parent = perms.coordinator_permissions([defs.parse_rule("allow write **")])
    child = perms.derive(parent, _def(rules=("deny write src/**",)), parent_depth=0)
    assert child.path_denied("write", "src/app.py") is True


def test_a_parent_tool_allowlist_flows_down_as_the_deny_of_its_complement(ceiling_one):
    parent = perms.ChildPermissions(allowed_tools=frozenset({"read_file"}), may_delegate=True)
    child = perms.derive(parent, _def(tools=("read_file", "bash")), parent_depth=0,
                         vocabulary=("read_file", "bash", "write_file"))
    assert child.tool_denied("bash") is True
    assert child.tool_denied("read_file") is False
    assert "deny list" in child.why_tool_denied("bash")


def test_a_parent_tool_deny_flows_down_even_when_the_child_allows_it(ceiling_one):
    parent = perms.ChildPermissions(denied_tools=frozenset({"bash"}), may_delegate=True)
    child = perms.derive(parent, _def(tools=("bash", "read_file")), parent_depth=0)
    assert child.tool_denied("bash") is True


# ── delegation and depth ──────────────────────────────────────────────────

def test_a_child_cannot_delegate_unless_its_own_definition_grants_it(monkeypatch):
    monkeypatch.setattr(perms, "max_depth", lambda: 2)
    plain = perms.derive(None, _def(), parent_depth=0)
    assert plain.may_delegate is False and perms.DELEGATE_TOOL in plain.denied_tools
    granted = perms.derive(None, _def(mode="coordinator"), parent_depth=0)
    assert granted.may_delegate is True and perms.DELEGATE_TOOL not in granted.denied_tools


def test_a_parent_that_may_not_delegate_does_not_hand_the_right_on(monkeypatch):
    monkeypatch.setattr(perms, "max_depth", lambda: 3)
    parent = perms.ChildPermissions(may_delegate=False, depth=1)
    child = perms.derive(parent, _def(mode="coordinator"), parent_depth=1)
    assert child.may_delegate is False
    assert any("may not delegate either" in c for c in child.caveats)


def test_depth_one_refuses_a_grandchild_and_the_error_names_the_limit(ceiling_one):
    parent = perms.derive(None, _def(mode="coordinator"), parent_depth=0)
    assert parent.depth == 1
    with pytest.raises(perms.DepthExceeded) as excinfo:
        perms.derive(parent, _def(), parent_depth=1)
    message = str(excinfo.value)
    assert "agent_subagent_depth=1" in message and "ceiling is 1" in message


def test_at_the_ceiling_a_coordinator_says_why_it_cannot_delegate(ceiling_one):
    child = perms.derive(None, _def(mode="coordinator"), parent_depth=0)
    assert child.may_delegate is False
    assert any("agent_subagent_depth is 1" in c for c in child.caveats)


def test_a_deny_delegate_rule_beats_the_coordinator_mode(ceiling_one):
    definition = _def(mode="coordinator", rules=("deny delegate *",))
    assert definition.may_delegate() is False
    assert perms.derive(None, definition, parent_depth=0).may_delegate is False


def test_the_ceiling_is_a_setting_and_ships_at_one(monkeypatch):
    monkeypatch.setattr(perms, "max_depth", perms.max_depth)
    values = {}
    monkeypatch.setitem(sys.modules, "src.settings",
                        type("M", (), {"get_setting": staticmethod(lambda k, d=None: values.get(k, d))}))
    assert perms.max_depth() == perms.DEFAULT_MAX_DEPTH == 1
    values[perms.DEPTH_SETTING] = 3
    assert perms.max_depth() == 3
    values[perms.DEPTH_SETTING] = "not a number"
    assert perms.max_depth() == 1               # an unreadable value never removes the ceiling


# ── the workspace roots ───────────────────────────────────────────────────

def test_the_child_gets_the_parents_roots_and_cannot_name_another(ceiling_one):
    parent = perms.coordinator_permissions(workspace_roots=("/w",), workspace="/w")
    child = perms.derive(parent, _def(), parent_depth=0)
    assert child.workspace_roots == ("/w",)


# ── the pattern language ──────────────────────────────────────────────────

@pytest.mark.parametrize("pattern,path,expected", [
    ("src/**", "src/app.py", True),
    ("src/**", "src/deep/nested/app.py", True),
    ("src/**", "tests/app.py", False),
    # `*` does not cross a separator; `**` does. A rule meant to fence ONE
    # directory must not silently fence its whole tree.
    ("src/*", "src/app.py", True),
    ("src/*", "src/deep/app.py", False),
    ("**", "anything/at/all.py", True),
    ("*", "anything/at/all.py", True),
    ("**/*.py", "src/a/b.py", True),
    ("**/*.py", "src/a/b.txt", False),
    ("*.md", "README.md", True),
    ("*.md", "docs/README.md", False),
])
def test_path_patterns(pattern, path, expected):
    assert perms.path_matches(pattern, path) is expected


def test_paths_are_judged_in_one_spelling(tmp_path):
    root = str(tmp_path)
    inside = os.path.join(root, "src", "app.py")
    # An absolute path, a relative one and a walk through `..` are the same
    # file; a rule that only catches one of the three is a rule with a door in
    # it.
    assert perms.normalise_path(inside, root) == "src/app.py"
    assert perms.normalise_path("src/app.py", root) == "src/app.py"
    assert perms.normalise_path(os.path.join(root, "src", "..", "src", "app.py"), root) == "src/app.py"


def test_last_match_wins_and_the_default_is_allow():
    rules = [defs.parse_rule("deny write **"), defs.parse_rule("allow write docs/**")]
    assert perms.decide(rules, "write", "src/a.py") == "deny"
    assert perms.decide(rules, "write", "docs/a.md") == "allow"
    # Nothing said about reading: a definition that restricts writing has not
    # thereby forbidden reading, and pretending otherwise would stop every
    # worker that exists today.
    assert perms.decide(rules, "read", "src/a.py") == "allow"
    assert perms.decide([], "write", "src/a.py") == "allow"


def test_restricts_action_is_what_the_fail_closed_branch_asks():
    unrestricted = perms.ChildPermissions()
    assert unrestricted.restricts_action("write") is False
    restricted = perms.ChildPermissions(rules=(defs.parse_rule("deny write src/**"),))
    assert restricted.restricts_action("write") is True
    assert restricted.restricts_action("read") is False
