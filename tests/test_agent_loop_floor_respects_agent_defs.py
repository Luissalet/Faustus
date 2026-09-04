"""The workspace floor stops at the running worker's own definition.

`src/agent_loop.py`'s workspace tool floor restores read_file / ls / edit_file
/ apply_patch past `disabled_tools` for a bound folder, because an agent that
cannot read a file in its own project is not an agent. It already exempts every
denial that is an *authorization* decision rather than a guess — guide-only,
block-all, the non-admin denylist, plan mode, the operator's own
`disabled_tools`.

An agent definition (src/agent_defs.py) is an authorization decision too: a
human wrote the file, `subagent_permissions.derive` turned it into a frozen
`ChildPermissions`, `subagent_tools.worker_disabled_tools` folds its denials
into the turn's denylist, and `subagent_tools.write_block_reason` refuses at
execution time what it denies. The floor did not know about it, so a `reviewer`
defined read-only had `edit_file` put back on the list it was SHOWN and then
refused when it called it — the offered-then-refused trap
`tests/test_agent_loop_offer_execute_coherence.py` exists to make impossible,
and one `worker_disabled_tools`'s own docstring named as a known hole.

WHY THIS IS NOT THE REVERTED CASE
---------------------------------
A preflight rule that pruned the document tools was reverted (df041a6) because
preflight runs once at the start of the turn and a document can come into
existence DURING it — the tool was removed before it could become legal.

Nothing here can change mid-turn. `_PERMS_CTX` is set once in `_run_subagent`,
before the loop is entered, and holds a frozen dataclass; `tool_denied` is a
pure function of a `denied_tools` set and an `allowed_tools` allowlist that
nothing mutates. A name this subtracts could not have become legal later.

The definition's PATH rules are deliberately NOT consulted: the path a call
will write is in its arguments, which is exactly the mid-turn target the revert
was about, so those stay at the point of use in `write_block_reason`. A worker
allowed `edit_file` but fenced to one directory is still offered `edit_file`.

And the invariant under all of it: an ordinary worker with no definition gets a
byte-identical floor.
"""

import pytest

import src.agent_tools  # noqa: F401  - resolves the circular schema imports first
import src.agent_loop as agent_loop  # noqa: F401  - the module under test
from src.agent_defs import AgentDef, Rule
from src.agent_tools import subagent_tools as st
from src.subagent_permissions import derive

from tests.test_agent_loop_workspace_tool_floor import (  # noqa: E402
    SPANISH_REQUEST,
    tools_sent,
    workspace,  # noqa: F401  - the fixture
)

FLOOR = {"read_file", "ls", "edit_file", "apply_patch"}


def _definition(slug, **kw):
    return AgentDef(slug=slug, name=slug, prompt="do the job", **kw)


@pytest.fixture
def as_worker():
    """Drive the turn the way `_run_subagent` drives one: the worker's derived
    permissions set on the real context variable — not a stub, because whether
    a `ContextVar.set` before the loop is visible inside it is half of what is
    being claimed — and the denylist `_run_subagent` really passes,
    `worker_disabled_tools`, which is where a definition's denials enter and
    therefore where the floor used to undo them."""
    tokens = []

    def run(definition, *, ws, message=SPANISH_REQUEST, extra_disabled=(), **kw):
        perms = None if definition is None else derive(None, definition, workspace=ws or "")
        tokens.append(st._PERMS_CTX.set(perms))
        # `extra_disabled` is what the TURN ROUTE folds in on top (its web-intent
        # clamp, the operator's own setting): the floor only ever puts back
        # something that was denied, so a test about the floor has to deny it.
        denied = st.worker_disabled_tools(message, perms) | set(extra_disabled)
        return set(tools_sent(message, ws, disabled_tools=denied, **kw))

    yield run
    for token in reversed(tokens):
        st._PERMS_CTX.reset(token)


# ── the invariant, first ───────────────────────────────────────────────────

def test_a_worker_with_no_definition_gets_an_unchanged_floor(workspace, as_worker):
    """The whole of what keeps every existing delegation on its old path."""
    baseline = set(tools_sent(SPANISH_REQUEST, workspace,
                              disabled_tools=st.worker_disabled_tools(SPANISH_REQUEST, None)))
    assert FLOOR <= baseline, "the floor itself stopped working"
    assert as_worker(None, ws=workspace) == baseline


def test_a_definition_that_denies_no_tool_changes_nothing(workspace, as_worker):
    baseline = set(tools_sent(SPANISH_REQUEST, workspace,
                              disabled_tools=st.worker_disabled_tools(SPANISH_REQUEST, None)))
    # A definition with no `tools` allowlist and no `deny` denies nothing but
    # `delegate_agents`, which is not floored.
    assert as_worker(_definition("helper"), ws=workspace) - {"delegate_agents"} == \
        baseline - {"delegate_agents"}


def test_a_chat_turn_outside_any_worker_is_untouched(workspace):
    """`_PERMS_CTX` is None everywhere except inside a definition-driven
    worker, so an ordinary chat never reaches the new subtraction at all."""
    assert st._PERMS_CTX.get() is None
    names = set(tools_sent(SPANISH_REQUEST, workspace,
                           disabled_tools=st.worker_disabled_tools(SPANISH_REQUEST, None)))
    assert FLOOR <= names


# ── the trap this closes ───────────────────────────────────────────────────

def test_a_read_only_reviewer_is_never_offered_the_edit_path(workspace, as_worker):
    names = as_worker(_definition("reviewer", tools=("read_file", "ls", "grep", "glob")),
                      ws=workspace)
    assert {"read_file", "ls"} <= names, (
        f"a reviewer that cannot read its own project is not a reviewer: {sorted(names)}")
    assert not names & {"edit_file", "apply_patch"}, (
        f"the floor handed a read-only reviewer the edit path: {sorted(names)}")


def test_a_named_deny_is_taken_out_of_the_floor(workspace, as_worker):
    names = as_worker(_definition("careful", deny=("apply_patch",)), ws=workspace)
    assert "apply_patch" not in names
    assert {"read_file", "ls", "edit_file"} <= names, "only the named one goes"


def test_what_the_floor_removes_is_exactly_what_the_guard_would_refuse(workspace, as_worker):
    """The two halves have to agree by construction rather than by two lists
    happening to match: `write_block_reason` is the execution side of the same
    answer, and it is where the offered-then-refused pair used to appear."""
    definition = _definition("reviewer", tools=("read_file", "ls"))
    perms = derive(None, definition, workspace=workspace)
    names = as_worker(definition, ws=workspace)

    token = st._PERMS_CTX.set(perms)
    try:
        for tool in sorted(FLOOR):
            refused = st.write_block_reason(tool, "{}") is not None
            assert refused == (tool not in names), (
                f"{tool}: offered={tool in names} but the guard "
                f"{'refuses' if refused else 'allows'} it")
    finally:
        st._PERMS_CTX.reset(token)


def test_a_path_rule_alone_never_removes_a_tool(workspace, as_worker):
    """Paths are checked per call, because the path is in the arguments and is
    not knowable when the floor is resolved. A worker fenced to one directory
    must still be able to express an edit — this is the whole distinction from
    the reverted document rule."""
    definition = _definition("fenced", permission=(Rule("write", "src/**", "deny"),))
    names = as_worker(definition, ws=workspace)
    assert FLOOR <= names, f"a path rule pruned a tool name: {sorted(FLOOR - names)}"


def test_a_worker_allowed_no_file_tool_at_all_gets_no_floor(workspace, as_worker):
    names = as_worker(_definition("mute", tools=("grep",)), ws=workspace)
    assert not names & FLOOR, f"a worker allowed none of them was handed some: {sorted(names)}"


# ── the floor still does its own job ───────────────────────────────────────

def test_subtracting_only_ever_removes(workspace, as_worker):
    """A definition that allows everything does not put back what the operator
    switched off: the floor's other exemptions are unaffected."""
    definition = _definition("wide", tools=("read_file", "ls", "edit_file", "apply_patch",
                                            "bash", "write_file", "grep", "glob"))
    names = as_worker(definition, ws=workspace, extra_disabled={"edit_file"},
                      settings={"disabled_tools": ["edit_file"]})
    assert "edit_file" not in names
    assert {"read_file", "ls", "apply_patch"} <= names


def test_no_workspace_still_means_no_floor(as_worker):
    """Without a bound folder there is no floor at all, so the new exemption
    cannot accidentally create one."""
    names = as_worker(_definition("helper"), ws=None, extra_disabled=FLOOR)
    assert not names & FLOOR, f"floor fired without a workspace: {sorted(names)}"


def test_plan_mode_and_a_definition_both_narrow_it(workspace, as_worker):
    """Two authorization sources, both subtracted, neither overruling the
    other: plan mode drops the edit half and the definition drops `ls`."""
    names = as_worker(_definition("odd", deny=("ls",)), ws=workspace, plan_mode=True)
    assert "ls" not in names
    assert not names & {"edit_file", "apply_patch"}
    assert "read_file" in names
