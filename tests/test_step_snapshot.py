"""H06: a late call uses the right contract and current revocation (src/step_snapshot.py)."""
from __future__ import annotations

import json

import pytest

import src.agent_tools  # noqa: F401
from src.step_snapshot import (
    AnnouncedTool, CallAuthorization, StepSnapshot, authorize_call, authorize_for_execution, capture_step,
    live_mcp_definition, mcp_descriptors, step_snapshot_for_answer,
)
from src.tool_authority import AUTHORITY


def _schema(name, props=None, desc="d"):
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props or {"q": {"type": "string"}}}}}


def _snap(tools=None, **kw):
    args = dict(session_id="s1", run_id="r1", round_num=2, candidate_index=0, owner="alice")
    args.update(kw)
    return capture_step(tools if tools is not None else [AUTHORITY.emit("bash"), _schema("mcp__srv__get")], **args)


class _Policy:
    def __init__(self, blocked=()):
        self.blocked = set(blocked)

    def blocks(self, name):
        return name in self.blocked


class _Block:
    def __init__(self, tool_type):
        self.tool_type = tool_type
        self.content = "{}"


# -- capture ----------------------------------------------------------------
def test_capture_freezes_the_announced_contract_and_carries_no_identity():
    schemas = [AUTHORITY.emit("bash")]
    snap = _snap(schemas, owner="alice@example.test")
    schemas[0]["function"]["parameters"]["properties"]["command"]["type"] = "integer"  # caller mutates later
    assert snap.contract_for("bash")["parameters"]["properties"]["command"]["type"] == "string"
    assert "alice" not in json.dumps(snap.receipt()) and "alice" not in snap.owner_ref
    assert snap.announced("bash").origin == "authored"
    assert snap.announced("bash").canonical_id == "shell.bash"
    assert snap.receipt()["announced"] == 1


def test_announced_resolves_aliases_and_ids_but_never_guesses():
    snap = _snap()
    assert snap.announced("terminal").name == "bash"
    assert snap.announced("shell.bash").name == "bash"
    assert snap.announced("python") is None
    assert snap.announced(None) is None
    assert snap.contract_for("python") is None


def test_capture_skips_malformed_entries_and_duplicates():
    snap = capture_step([{"nope": 1}, "x", None, _schema("a"), _schema("a"), {"function": {"name": 3}}],
                        session_id="s", run_id="r", round_num=1, candidate_index=0)
    assert [t.name for t in snap.tools] == ["a"]


def test_policy_denied_at_capture_is_recorded_but_is_not_an_authority():
    snap = _snap(disabled_tools={"bash"}, tool_policy=_Policy({"mcp__srv__get"}))
    assert snap.policy_denied == ("bash", "mcp__srv__get")
    # Policy lifted since capture: the live state decides, not the capture.
    verdict = authorize_call(snap, "bash", session_id="s1", owner="alice", disabled_tools=set(), tool_policy=_Policy())
    assert verdict.allowed and verdict.status == "ok"


def test_catalog_version_changes_when_an_announced_schema_changes():
    a = _snap([_schema("t", {"q": {"type": "string"}})])
    b = _snap([_schema("t", {"q": {"type": "integer"}})])
    c = _snap([_schema("t", {"q": {"type": "string"}})])
    assert a.catalog_version != b.catalog_version
    assert a.catalog_version == c.catalog_version
    assert a.snapshot_id != c.snapshot_id


def test_snapshot_for_answer_never_substitutes_another_candidate_or_round():
    snap = _snap(round_num=3, candidate_index=1)
    states = {1: {"step_snapshot": snap}, 0: {"step_snapshot": _snap(round_num=3, candidate_index=0)}}
    assert step_snapshot_for_answer(states, 1, round_num=3) is snap
    assert step_snapshot_for_answer(states, 1, round_num=4) is None  # old round
    assert step_snapshot_for_answer(states, 2, round_num=3) is None  # missing candidate, not candidate 0
    assert step_snapshot_for_answer(states, None, round_num=3) is None
    assert step_snapshot_for_answer(states, True, round_num=3) is None
    assert step_snapshot_for_answer({1: {"step_snapshot": "x"}}, 1, round_num=3) is None


# -- revocation beats what was announced ----------------------------------------
def test_a_tool_disabled_after_the_step_is_refused_even_though_it_was_announced():
    snap = _snap()
    assert authorize_call(snap, "bash", session_id="s1", owner="alice").allowed
    verdict = authorize_call(snap, "bash", session_id="s1", owner="alice", disabled_tools={"bash"})
    assert not verdict.allowed and verdict.status == "revoked"


@pytest.mark.parametrize("spelling", ["bash", "shell", "terminal", "shell.bash", "command", "execute", "run"])
def test_revocation_holds_under_every_spelling_of_the_tool(spelling):
    snap = _snap()
    assert not authorize_call(snap, spelling, session_id="s1", owner="alice", disabled_tools={"bash"}).allowed
    assert not authorize_call(snap, "bash", session_id="s1", owner="alice", disabled_tools={spelling}).allowed
    assert not authorize_call(snap, spelling, session_id="s1", owner="alice", tool_policy=_Policy({"bash"})).allowed


def test_revocation_is_not_softened_by_shadow_mode_or_a_permissive_unannounced_setting():
    snap = _snap()
    verdict = authorize_call(snap, "bash", session_id="s1", owner="alice", disabled_tools={"bash"},
                             mode="shadow", unannounced="allow")
    assert not verdict.allowed and verdict.status == "revoked"
    assert not authorize_call(_snap([]), "bash", disabled_tools={"bash"}).allowed  # unannounced and revoked


def test_a_policy_that_cannot_be_read_denies():
    class Broken:
        def blocks(self, name):
            raise RuntimeError("store unreadable")
    assert not authorize_call(_snap(), "bash", session_id="s1", owner="alice", tool_policy=Broken()).allowed


def test_mode_off_and_missing_snapshot_judge_nothing():
    assert authorize_call(_snap(), "bash", mode="off", disabled_tools={"bash"}).status == "off"
    assert authorize_call(None, "bash", disabled_tools={"bash"}).status == "no_snapshot"


def test_a_snapshot_of_another_session_or_owner_is_ignored_not_applied():
    snap = _snap()
    other_session = authorize_call(snap, "python", session_id="s2", owner="alice")
    assert other_session.allowed and other_session.status == "snapshot_ignored"
    other_owner = authorize_call(snap, "python", session_id="s1", owner="bob", unannounced="refuse")
    assert other_owner.allowed and other_owner.status == "snapshot_ignored"


# -- announced or not ----------------------------------------------------------
def test_an_unannounced_tool_follows_the_setting():
    snap = _snap()
    shadow = authorize_call(snap, "python", session_id="s1", owner="alice", unannounced="shadow")
    assert shadow.allowed and shadow.status == "not_announced"
    assert authorize_call(snap, "python", unannounced="allow").allowed
    refused = authorize_call(snap, "python", unannounced="refuse")
    assert not refused.allowed and refused.status == "not_announced"
    # A shadow run never refuses on this ground.
    assert authorize_call(snap, "python", unannounced="refuse", mode="shadow").allowed


# -- an MCP update does not change the contract of a call already made ---------
def _live(schema):
    return lambda name: {"name": name, "description": "x", "parameters": schema}


def test_an_mcp_schema_change_after_the_step_is_reported_and_refused_when_enforced():
    snap = _snap()
    same = authorize_call(snap, "mcp__srv__get", session_id="s1", owner="alice",
                          live_definition=_live({"type": "object", "properties": {"q": {"type": "string"}}}))
    assert same.allowed and same.status == "ok"
    changed = authorize_call(snap, "mcp__srv__get", session_id="s1", owner="alice",
                             live_definition=_live({"type": "object", "properties": {"q": {"type": "integer"}}}))
    assert not changed.allowed and changed.status == "contract_changed"
    assert changed.contract_sha256 and changed.live_contract_sha256 != changed.contract_sha256
    assert snap.contract_for("mcp__srv__get")["parameters"]["properties"]["q"]["type"] == "string"


def test_a_description_only_mcp_update_does_not_count_as_a_contract_change():
    snap = _snap()
    verdict = authorize_call(snap, "mcp__srv__get", live_definition=lambda n: {
        "name": n, "description": "reworded", "parameters": {"type": "object", "properties": {"q": {"type": "string"}}}})
    assert verdict.allowed and verdict.status == "ok"


def test_shadow_mode_reports_a_changed_contract_without_refusing():
    snap = _snap()
    verdict = authorize_call(snap, "mcp__srv__get", mode="shadow",
                             live_definition=_live({"type": "object", "properties": {"z": {}}}))
    assert verdict.allowed and verdict.status == "contract_changed"


def test_an_mcp_tool_that_vanished_is_revoked():
    snap = _snap()
    verdict = authorize_call(snap, "mcp__srv__get", live_definition=lambda n: None)
    assert not verdict.allowed and verdict.status == "revoked"


def test_live_mcp_definition_reads_the_manager_and_skips_disabled_tools():
    class Manager:
        def __init__(self, rows):
            self.rows = rows

        def get_all_tools(self):
            return self.rows

    row = {"qualified_name": "mcp__srv__get", "description": "D", "input_schema": {"type": "object"}, "is_disabled": False}
    assert live_mcp_definition("mcp__srv__get", Manager([row]))["parameters"] == {"type": "object"}
    assert live_mcp_definition("mcp__srv__get", Manager([{**row, "is_disabled": True}])) is None
    assert live_mcp_definition("mcp__other__x", Manager([row])) is None

    class Broken:
        def get_all_tools(self):
            raise RuntimeError("down")
    assert live_mcp_definition("mcp__srv__get", Broken()) is None
    assert live_mcp_definition("mcp__srv__get", object()) is None


def test_authorize_for_execution_uses_the_live_manager_for_mcp_tools_only():
    class Manager:
        def get_all_tools(self):
            return [{"qualified_name": "mcp__srv__get", "description": "", "is_disabled": False,
                     "input_schema": {"type": "object", "properties": {"q": {"type": "integer"}}}}]

    snap = _snap()
    mcp = authorize_for_execution(snap, _Block("mcp__srv__get"), session_id="s1", owner="alice",
                                  mode="enforce", unannounced="shadow", mcp_manager=Manager())
    assert mcp.status == "contract_changed" and not mcp.allowed
    builtin = authorize_for_execution(snap, _Block("bash"), session_id="s1", owner="alice",
                                      mode="enforce", unannounced="shadow", mcp_manager=Manager())
    assert builtin.allowed and builtin.status == "ok"
    assert authorize_for_execution(None, _Block("bash")).status == "no_snapshot"


def test_mcp_descriptors_are_built_per_discovered_tool_and_skip_malformed_ones():
    rows = [{"server_id": "a", "name": "one", "input_schema": {"type": "object", "properties": {}}},
            {"server_id": "a"}, {"name": "orphan"}, "junk"]
    built = mcp_descriptors(rows, readonly_of=lambda t: True)
    assert list(built) == ["mcp__a__one"]
    assert built["mcp__a__one"].origin == "mcp"


# -- wired into execute_tool_block -----------------------------------------------
@pytest.mark.asyncio
async def test_execute_tool_block_refuses_a_call_revoked_after_its_step():
    from src.tool_execution import NO_TOOL_SECURITY_CONTEXT, execute_tool_block
    from src.tool_parsing import ToolBlock

    snap = _snap([AUTHORITY.emit("bash")])
    desc, result = await execute_tool_block(
        ToolBlock("bash", "echo hi"), session_id="s1", owner="alice", disabled_tools={"bash"},
        security_context=NO_TOOL_SECURITY_CONTEXT, step_snapshot=snap)
    assert result["exit_code"] == 1 and result["blocked"] is True
    assert result["policy"] == "step_snapshot"
    assert result["step_snapshot"]["status"] == "revoked"
    assert result["step_snapshot"]["snapshot"]["snapshot_id"] == snap.snapshot_id


@pytest.mark.asyncio
async def test_execute_tool_block_runs_an_announced_call_and_attaches_the_receipt(monkeypatch):
    from src.tool_execution import NO_TOOL_SECURITY_CONTEXT, execute_tool_block
    from src.tool_parsing import ToolBlock

    snap = _snap([AUTHORITY.emit("get_workspace")])
    desc, result = await execute_tool_block(
        ToolBlock("get_workspace", ""), session_id="s1", owner="alice",
        security_context=NO_TOOL_SECURITY_CONTEXT, step_snapshot=snap)
    assert result["step_snapshot"]["status"] == "ok" and result["step_snapshot"]["allowed"] is True


@pytest.mark.asyncio
async def test_execute_tool_block_without_a_snapshot_is_unchanged():
    from src.tool_execution import NO_TOOL_SECURITY_CONTEXT, execute_tool_block
    from src.tool_parsing import ToolBlock

    desc, result = await execute_tool_block(
        ToolBlock("get_workspace", ""), session_id="s1", owner="alice", security_context=NO_TOOL_SECURITY_CONTEXT)
    assert "step_snapshot" not in result


# -- a revoked workspace grant beats the older answer --------------------------------
def test_workspace_grant_revoked_mid_run_withdraws_the_bypass(tmp_path, monkeypatch):
    from src import tool_approval_grants as grants
    from src.tool_capabilities import ToolRunSecurityContext

    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path))
    workspace = tmp_path / "proj"
    workspace.mkdir()
    grants.grant("alice", str(workspace))
    ctx = ToolRunSecurityContext(external_untrusted_context_seen=True, approval_gate_bypassed=True,
                                 workspace_grant_bypass=True, workspace=str(workspace), owner="alice")
    assert ctx.decision_for("write_file", json.dumps({"path": "x", "content": "y"})).allowed
    assert grants.revoke("alice", str(workspace))
    decision = ctx.decision_for("write_file", json.dumps({"path": "x", "content": "y"}))
    assert not decision.allowed
    assert ctx.approval_gate_bypassed is False and ctx.workspace_grant_bypass is False


def test_a_bypass_from_another_answer_is_not_withdrawn_by_the_workspace_store(tmp_path, monkeypatch):
    from src.tool_capabilities import ToolRunSecurityContext

    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path))
    task_scope = ToolRunSecurityContext(external_untrusted_context_seen=True, approval_gate_bypassed=True,
                                        workspace=str(tmp_path), owner="alice")
    assert task_scope.revalidate_workspace_grant() is False and task_scope.approval_gate_bypassed is True
    from src.tool_capabilities import CHAT_SESSION_APPROVAL_CONTEXT_MARKER
    marker = {"role": "user", "content": "x", "metadata": {CHAT_SESSION_APPROVAL_CONTEXT_MARKER: True}}
    both = ToolRunSecurityContext(external_untrusted_context_seen=True, approval_gate_bypassed=True,
                                  workspace_grant_bypass=True, workspace=str(tmp_path), owner="alice")
    both.observe_messages([marker])
    assert both.workspace_grant_bypass is False
    assert both.revalidate_workspace_grant() is False and both.approval_gate_bypassed is True


def test_an_unreadable_grant_store_withdraws_the_workspace_bypass(monkeypatch, tmp_path):
    from src import tool_approval_grants as grants
    from src.tool_capabilities import ToolRunSecurityContext

    def boom(*a, **k):
        raise OSError("disk")
    monkeypatch.setattr(grants, "is_granted", boom)
    ctx = ToolRunSecurityContext(approval_gate_bypassed=True, workspace_grant_bypass=True,
                                 workspace=str(tmp_path), owner="alice")
    assert ctx.revalidate_workspace_grant() is True and ctx.approval_gate_bypassed is False
