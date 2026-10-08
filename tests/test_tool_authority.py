"""H05: catalog, schema and handler come from one authority (src/tool_authority*.py)."""
from __future__ import annotations

import json
import os

import pytest

import src.agent_tools  # noqa: F401  (import order: handlers before schemas)
from src.agent_tools.filesystem_tools import GlobTool, ReadFileTool, WriteFileTool
from src.tool_authority import (
    AUTHORITY, Exposure, LimitRef, ParserContract, ToolAuthority, ToolAuthorityError, ToolEffects, ToolLimits,
    ToolResources, descriptor_from_mcp, make_tool,
)
from src.tool_authority_catalog import exposure_map, families, unregistered_builtins
from src.tool_parsing import _TOOL_NAME_MAP
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS, function_call_to_tool_block

def _handlers():
    # Looked up when used: the table is finished only after every handler
    # module has been imported.
    import src.agent_tools
    return src.agent_tools.TOOL_HANDLERS


AUTHORED = ("bash", "python", "powershell", "web_search", "web_fetch", "read_file", "grep", "glob", "ls",
            "get_workspace", "write_file", "edit_file", "apply_patch", "plan_media_transform",
            "transform_media", "inspect_media", "inspect_deliverable", "image_job")


def _native(name):
    for entry in FUNCTION_TOOL_SCHEMAS:
        if entry["function"]["name"] == name:
            return entry
    raise AssertionError(name)


# -- one authority: schemas -------------------------------------------------
def test_every_authored_schema_is_what_the_authority_emits():
    for name in AUTHORED:
        assert _native(name) == AUTHORITY.emit(name), name


def test_pdf_schemas_are_the_contract_the_authority_adopted():
    from src.pdf_tool_contracts import function_definition, tool_names
    for name in tool_names():
        assert AUTHORITY.emit(name)["function"] == function_definition(name)
        assert AUTHORITY.get(name).origin == "contract"


def test_emission_returns_fresh_objects_so_a_caller_cannot_mutate_the_registry():
    first = AUTHORITY.emit("bash")
    first["function"]["parameters"]["properties"]["command"]["description"] = "tampered"
    assert AUTHORITY.emit("bash")["function"]["parameters"]["properties"]["command"]["description"] != "tampered"


def test_every_builtin_tool_is_registered_and_the_parity_report_is_clean():
    assert unregistered_builtins() == []
    assert AUTHORITY.parity_report(_handlers()) == []


# -- parity report catches the divergence it exists for ---------------------
def _authority_with(encode, properties, *, required=("path",)):
    reg = ToolAuthority()
    reg.register(make_tool(
        name="probe", canonical_id="probe.tool", family="probe", description="d",
        parameters={"type": "object", "properties": properties, "required": list(required)},
        parser=ParserContract(encode, required_any=(tuple(required),))))
    return reg


def test_parity_reports_a_property_the_encoder_drops():
    props = {"path": {"type": "string"}, "base_revision": {"type": "string"}}
    reg = _authority_with(lambda a: str(a.get("path", "")), props)
    issues = reg.parity_report()
    assert [(i.tool, i.check) for i in issues] == [("probe", "dropped_property")]
    assert "base_revision" in issues[0].detail


def test_parity_is_clean_when_every_property_reaches_the_handler():
    props = {"path": {"type": "string"}, "flag": {"type": "boolean"}}
    reg = _authority_with(lambda a: json.dumps(dict(a)), props)
    assert reg.parity_report() == []


def test_parity_reports_a_missing_handler_and_a_dangling_limit():
    reg = ToolAuthority()
    reg.register(make_tool(
        name="probe", canonical_id="probe.tool", family="probe", description="d",
        parameters={"type": "object", "properties": {}}, parser=ParserContract(lambda a: ""),
        limits=ToolLimits.of(max_results=LimitRef("src.constants:NO_SUCH_LIMIT"))))
    checks = sorted(i.check for i in reg.parity_report({}))
    assert checks == ["handler", "limits"]


def test_write_file_and_apply_patch_no_longer_drop_their_advertised_arguments():
    """The defect the parity report found: the parser produced `path\\ncontent`
    and the handler never saw base_revision or confirm_risky."""
    block = function_call_to_tool_block("write_file", json.dumps(
        {"path": "a.txt", "content": "x", "base_revision": "sha256:abc"}))
    assert json.loads(block.content) == {"path": "a.txt", "content": "x", "base_revision": "sha256:abc"}
    plain = function_call_to_tool_block("write_file", json.dumps({"path": "a.txt", "content": "x"}))
    assert plain.content == "a.txt\nx"  # legacy shape unchanged
    patch = function_call_to_tool_block("apply_patch", json.dumps({"patch_text": "P", "confirm_risky": True}))
    assert json.loads(patch.content) == {"patch_text": "P", "confirm_risky": True}
    assert function_call_to_tool_block("apply_patch", json.dumps({"patch_text": "P"})).content == "P"


@pytest.mark.asyncio
async def test_native_write_file_with_a_stale_base_revision_is_refused_end_to_end(tmp_path):
    from src.agent_tools.filesystem_tools import sha256_revision
    target = tmp_path / "a.txt"
    target.write_text("one\n")
    stale = sha256_revision(target.read_bytes())
    target.write_text("changed elsewhere\n")
    block = function_call_to_tool_block("write_file", json.dumps(
        {"path": str(target), "content": "mine\n", "base_revision": stale}))
    result = await WriteFileTool().execute(block.content, {})
    assert result["error_code"] == "BASE_REVISION_MISMATCH"
    assert target.read_text() == "changed elsewhere\n"


# -- limits: declared == applied --------------------------------------------
def test_limit_references_resolve_to_the_constants_the_handlers_use():
    from src import constants
    from src.agent_tools import filesystem_tools, subprocess_tools
    assert AUTHORITY.limit("bash", "max_output_chars") == constants.MAX_OUTPUT_CHARS
    assert AUTHORITY.limit("read_file", "max_output_chars") == constants.MAX_READ_CHARS
    assert AUTHORITY.limit("bash", "hard_timeout_s") == subprocess_tools.DEFAULT_BASH_TIMEOUT
    assert AUTHORITY.limit("powershell", "hard_timeout_s") == subprocess_tools.DEFAULT_POWERSHELL_TIMEOUT
    assert AUTHORITY.limit("write_file", "max_diff_lines") == constants.MAX_DIFF_LINES
    for name in ("grep", "glob", "ls"):
        assert AUTHORITY.limit(name, "max_results") == filesystem_tools._CODENAV_MAX_HITS
    assert AUTHORITY.limit("grep", "max_line_chars") == filesystem_tools._CODENAV_MAX_LINE


@pytest.mark.asyncio
async def test_codenav_tools_really_stop_at_the_declared_hit_limit(tmp_path):
    limit = AUTHORITY.limit("glob", "max_results")
    for i in range(limit + 25):
        (tmp_path / f"f{i:04d}.txt").write_text("x")
    block = function_call_to_tool_block("glob", json.dumps({"pattern": "*.txt", "path": str(tmp_path)}))
    result = await GlobTool().execute(block.content, {})
    listed = [line for line in str(result.get("output", "")).splitlines() if line.strip().endswith(".txt")]
    assert 0 < len(listed) <= limit


@pytest.mark.asyncio
async def test_read_file_truncates_at_its_declared_output_limit(tmp_path):
    big = tmp_path / "big.txt"
    big.write_text("a" * (AUTHORITY.limit("read_file", "max_output_chars") + 5_000))
    block = function_call_to_tool_block("read_file", json.dumps({"path": str(big)}))
    result = await ReadFileTool().execute(block.content, {})
    limit = AUTHORITY.limit("read_file", "max_output_chars")
    assert f"truncated at {limit} chars" in str(result.get("output", ""))


def test_catalogue_descriptors_report_the_declared_limits():
    from src.tool_registry import by_name, snapshot
    rows = snapshot()
    assert by_name(rows, "read_file").max_output_bytes == AUTHORITY.limit("read_file", "max_output_chars")
    assert by_name(rows, "bash").max_output_bytes == AUTHORITY.limit("bash", "max_output_chars")


# -- aliases and revocation -------------------------------------------------
def test_aliases_are_current_spellings_and_resolve_to_one_tool():
    for name in AUTHORED:
        tool = AUTHORITY.get(name)
        for alias in tool.aliases:
            assert _TOOL_NAME_MAP.get(alias, name) == name, (name, alias)
            assert AUTHORITY.resolve(alias).canonical == name
    assert AUTHORITY.resolve("shell.bash").via == "canonical_id"
    assert AUTHORITY.resolve("bash").via == "name"
    assert AUTHORITY.resolve("terminal").via == "alias"
    assert AUTHORITY.resolve("no_such_tool") is None
    assert AUTHORITY.resolve(None) is None


def test_aliases_never_dispatch_only_the_wire_name_does():
    assert AUTHORITY.handler("bash", _handlers()) is _handlers()["bash"]
    assert AUTHORITY.handler("terminal", _handlers()) is None
    assert AUTHORITY.handler("shell.bash", _handlers()) is None
    assert AUTHORITY.handler("unknown", _handlers()) is None


def test_a_registered_tool_without_a_handler_does_not_become_executable():
    reg = ToolAuthority()
    reg.register(make_tool(name="ghost", canonical_id="probe.ghost", family="probe", description="d",
                           parameters={"type": "object", "properties": {}}, parser=ParserContract(lambda a: "")))
    assert reg.handler("ghost", {"other": object()}) is None


def test_revoking_a_tool_covers_its_canonical_id_and_aliases():
    from src.tool_security import email_tool_policy_names
    names = email_tool_policy_names("shell")
    assert {"bash", "shell", "shell.bash", "terminal", "command", "execute", "run"} <= set(names)
    assert AUTHORITY.any_spelling_in("terminal", {"bash"})
    assert not AUTHORITY.any_spelling_in("python", {"bash"})


@pytest.mark.asyncio
async def test_a_disabled_tool_is_refused_under_its_alias_too():
    from src.tool_execution import NO_TOOL_SECURITY_CONTEXT, execute_tool_block
    from src.tool_parsing import ToolBlock
    for spelling in ("bash", "shell", "terminal"):
        desc, result = await execute_tool_block(
            ToolBlock(spelling, "echo hi"), disabled_tools={"bash"}, security_context=NO_TOOL_SECURITY_CONTEXT)
        assert result.get("exit_code") == 1, spelling
        assert "disabled" in json.dumps(result).lower() or result.get("blocked"), spelling


# -- effects, including argument-dependent ones -----------------------------
def test_effects_are_read_through_the_single_capability_table():
    from src.tool_capabilities import capabilities_for_tool
    for name in ("bash", "read_file", "write_file", "web_fetch", "image_job"):
        assert AUTHORITY.get(name).effects.static(name) == frozenset(
            e.value for e in capabilities_for_tool(name).effects)
        assert AUTHORITY.get(name).effects.known(name)


def test_a_multi_action_tool_has_different_effects_per_call():
    read = AUTHORITY.effects_for_call("manage_documents", {"action": "list"})
    write = AUTHORITY.effects_for_call("manage_documents", {"action": "delete", "id": "x"})
    assert read != write
    assert write - read, "a delete must carry an effect a list does not"
    by_action = ToolEffects(action_field="action").by_action("manage_documents", ("list", "delete"))
    assert by_action["list"] == read and by_action["delete"] == write


def test_effects_for_an_alias_are_those_of_the_tool():
    assert AUTHORITY.effects_for_call("terminal", "echo hi") == AUTHORITY.effects_for_call("bash", "echo hi")


def test_resource_claims_name_the_paths_and_fail_to_the_wide_claim():
    assert AUTHORITY.claims_for_call("write_file", {"path": "a.txt"}) == ("fs:write:a.txt",)
    assert AUTHORITY.claims_for_call("write_file", {}) == ("fs:write:*",)
    assert AUTHORITY.claims_for_call("read_file", {"path": "a"}) == ("fs:read:a",)
    assert AUTHORITY.claims_for_call("web_fetch", {"url": "u"}) == ("net",)
    assert AUTHORITY.claims_for_call("bash", {"command": "x"}) == ("proc",)
    assert AUTHORITY.claims_for_call("nope", {}) == ()


# -- registration validation -------------------------------------------------
def _ok(**over):
    base = dict(name="probe", canonical_id="probe.tool", family="probe", description="d",
                parameters={"type": "object", "properties": {"a": {"type": "string"}}},
                parser=ParserContract(lambda a: ""))
    base.update(over)
    return make_tool(**base)


def test_registration_rejects_ambiguous_or_malformed_descriptors():
    with pytest.raises(ToolAuthorityError):
        _ok(name="bad name")
    with pytest.raises(ToolAuthorityError):
        _ok(canonical_id="NotDotted")
    with pytest.raises(ToolAuthorityError):
        _ok(parameters={"type": "array"})
    with pytest.raises(ToolAuthorityError):
        _ok(parameters={"type": "object", "properties": {}, "required": ["missing"]})
    with pytest.raises(ToolAuthorityError):
        _ok(parser=ParserContract(lambda a: "", required_any=(("nowhere",),)))
    with pytest.raises(ToolAuthorityError):
        _ok(origin="invented")
    with pytest.raises(ToolAuthorityError):
        _ok(aliases=["bad alias"])


def test_duplicate_names_and_colliding_spellings_are_refused():
    reg = ToolAuthority()
    reg.register(_ok(aliases=["shared"]))
    with pytest.raises(ToolAuthorityError):
        reg.register(_ok())
    with pytest.raises(ToolAuthorityError):
        reg.register(_ok(name="other", canonical_id="probe.other", aliases=["shared"]))
    reg.unregister("probe")
    reg.register(_ok(name="other", canonical_id="probe.other", aliases=["shared"]))
    assert reg.resolve("shared").canonical == "other"


def test_fingerprint_changes_with_any_contract_field_and_not_without():
    a = _ok()
    assert a.fingerprint() == _ok().fingerprint()
    assert a.fingerprint() != _ok(description="e").fingerprint()
    assert a.fingerprint() != _ok(exposure=Exposure.DEFERRED).fingerprint()
    assert a.fingerprint() != _ok(limits=ToolLimits.of(max_results=3)).fingerprint()


def test_exposure_is_a_descriptor_field_and_heavy_families_are_deferred():
    modes = exposure_map()
    assert modes["bash"] is Exposure.DIRECT and modes["read_file"] is Exposure.DIRECT
    assert modes["code_graph_context"] is Exposure.DEFERRED if "code_graph_context" in modes else True
    deferred = [n for n, m in modes.items() if m is Exposure.DEFERRED]
    assert len(deferred) >= 20
    assert "swarm_spawn" not in modes or modes["swarm_spawn"] is Exposure.DEFERRED
    assert set(families()) >= {"core_exec", "core_fs", "core_web", "media", "pdf"}


# -- MCP descriptors built at discovery -------------------------------------
def test_mcp_descriptor_carries_the_schema_announced_at_discovery():
    tool = {"server_id": "hoard-1", "name": "Fetch-Item.v2", "qualified_name": "mcp__hoard-1__Fetch-Item.v2",
            "description": "Fetch a record", "input_schema": {"type": "object", "properties": {"id": {"type": "string"}}}}
    before = descriptor_from_mcp(tool, readonly=True)
    assert before.name == "mcp__hoard-1__Fetch-Item.v2"
    assert before.origin == "mcp" and before.family == "mcp:hoard-1"
    assert before.canonical_id == "mcp.hoard_1.fetch_item_v2"
    tool["input_schema"]["properties"]["id"] = {"type": "integer"}  # the server changes its mind later
    assert before.parameters["properties"]["id"] == {"type": "string"}
    assert descriptor_from_mcp(tool).fingerprint() != before.fingerprint()


def test_mcp_descriptor_tolerates_loose_server_schemas_but_not_nameless_tools():
    loose = descriptor_from_mcp({"server_id": "s", "name": "t", "input_schema": {"required": ["x"]}})
    assert loose.parameters["type"] == "object"
    assert descriptor_from_mcp({"server_id": "s", "name": "t"}).parameters == {"type": "object", "properties": {}}
    with pytest.raises(ToolAuthorityError):
        descriptor_from_mcp({"server_id": "s"})
    with pytest.raises(ToolAuthorityError):
        descriptor_from_mcp({"name": "t"})
    with pytest.raises(ToolAuthorityError):
        descriptor_from_mcp("not a mapping")


def test_mcp_descriptor_is_not_added_to_the_process_registry():
    descriptor_from_mcp({"server_id": "s", "name": "t"})
    assert AUTHORITY.resolve("mcp__s__t") is None
    with pytest.raises(ToolAuthorityError):
        descriptor_from_mcp({"server_id": "s", "name": "t"}).parser.encode({})


# -- admin routes --------------------------------------------------------------------------
def _route_client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import routes.tool_registry_routes as route_module
    monkeypatch.setattr(route_module, "require_admin", lambda request: None)
    app = FastAPI()
    app.include_router(route_module.setup_tool_registry_routes())
    return TestClient(app)


def test_the_authority_route_lists_descriptors_with_exposure_and_limits(monkeypatch):
    body = _route_client(monkeypatch).get("/api/tools/authority").json()
    by_name = {t["name"]: t for t in body["tools"]}
    assert body["count"] == len(by_name) > 50
    assert by_name["read_file"]["exposure"] == "direct" and by_name["board_list"]["exposure"] == "deferred"
    assert sum(body["by_exposure"].values()) == body["count"]
    assert "parameters" not in by_name["read_file"]


def test_the_authority_route_filters_and_resolves_aliases(monkeypatch):
    client = _route_client(monkeypatch)
    deferred = client.get("/api/tools/authority", params={"exposure": "deferred"}).json()
    assert deferred["count"] > 0 and {t["exposure"] for t in deferred["tools"]} == {"deferred"}
    entry = client.get("/api/tools/authority/shell.bash").json()
    assert entry["resolved_via"] == "canonical_id" and entry["tool"]["name"] == "bash"
    assert client.get("/api/tools/authority/not_a_tool").status_code == 404


def test_the_authority_parity_route_reports_no_issue_on_the_shipped_registry(monkeypatch):
    body = _route_client(monkeypatch).get("/api/tools/authority/parity").json()
    assert body["ok"] is True and body["issues"] == []
