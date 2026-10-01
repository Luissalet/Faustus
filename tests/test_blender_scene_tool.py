"""The `blender_scene` agent tool, its built-in MCP server and its REST route:
registration in every catalogue the guard tests read, and each action's paths.
Blender itself is replaced by a fake here (tests/test_blender_scene_integration.py
runs the real one)."""
import asyncio
import base64
import json
import os

import pytest

from src import blender_scene as bs
from src.agent_tools import blender_scene_tool as tool_module
from src.agent_tools.blender_scene_tool import BlenderSceneTool
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    from src import constants

    monkeypatch.setattr(constants, "BLENDER_SCENES_DIR", str(tmp_path / "scenes"))
    monkeypatch.setattr(constants, "GENERATED_IMAGES_DIR", str(tmp_path / "generated"))

    def no_db(*a, **k):
        raise RuntimeError("no database in this test")

    import src.database as database

    monkeypatch.setattr(database, "SessionLocal", no_db)
    return tmp_path


def run_tool(args, ctx=None):
    return asyncio.run(BlenderSceneTool().execute(json.dumps(args), ctx if ctx is not None else {"session_id": "chat-1"}))


def scene():
    return {"meta": {"name": "demo"}, "ops": [{"op": "new_scene"}, {"op": "add_mesh", "primitive": "cube", "name": "Box"}]}


def fake_result(root, ok=True, with_image=True, ops_failed=0):
    outputs, images = [], []
    if with_image:
        png = os.path.join(root, "out", "a.png")
        os.makedirs(os.path.dirname(png), exist_ok=True)
        with open(png, "wb") as fh:
            fh.write(PNG)
        outputs.append({"path": png, "bytes": len(PNG), "kind": "render"})
        images.append(png)
    ops = [{"index": 0, "op": "new_scene", "status": "ok", "duration_s": 0.1},
           {"index": 1, "op": "render", "status": "ok", "duration_s": 0.2, "info": {"resolution": [320, 240]}}]
    errors = []
    if ops_failed:
        ops[1] = {"index": 1, "op": "render", "status": "error", "duration_s": 0.2, "message": "no active camera"}
        errors = ["ops[1] render: no active camera"]
    return {"ok": ok and not ops_failed, "stage": "done", "root": root, "errors": errors, "warnings": ["w1"],
            "outputs": outputs, "images": images, "blender": {"found": True, "version": "4.3.2", "path": "/b"},
            "report": {"ops": ops, "assertions": [{"label": "n", "passed": True}], "outputs": outputs},
            "assertions": {"total": 1, "failed": 0}, "duration_s": 1.5}


# --------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------

def test_tool_is_registered_everywhere_the_catalogs_look():
    from src.agent_tools import TOOL_HANDLERS, TOOL_TAGS
    from src.tool_authority_catalog import default_exposure
    from src.tool_authority import Exposure
    from src.tool_capabilities import ResultIntegrity, ToolEffect, capabilities_for_tool
    from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
    from src.tool_index_examples import EXAMPLES
    from src.tool_security import NON_ADMIN_BLOCKED_TOOLS, PLAN_MODE_READONLY_TOOLS

    assert "blender_scene" in TOOL_TAGS and "blender_scene" in TOOL_HANDLERS
    assert len(BUILTIN_TOOL_DESCRIPTIONS["blender_scene"]) > 100
    assert len(EXAMPLES["blender_scene"]) >= 3
    caps = capabilities_for_tool("blender_scene")
    assert caps.known
    assert {ToolEffect.WRITE_PRIVATE, ToolEffect.READ_WORKSPACE} <= set(caps.effects)
    assert ToolEffect.NETWORK_EGRESS not in caps.effects and ToolEffect.EXECUTE_CODE not in caps.effects
    assert caps.result_integrity == ResultIntegrity.WORKSPACE_UNTRUSTED
    assert "blender_scene" in NON_ADMIN_BLOCKED_TOOLS
    assert "blender_scene" not in PLAN_MODE_READONLY_TOOLS   # `run` writes files
    assert default_exposure("blender_scene") == Exposure.DEFERRED


def test_tool_is_known_to_the_tool_authority():
    from src.tool_authority import AUTHORITY
    from src.tool_authority_catalog import ensure_catalog

    ensure_catalog()
    assert "blender_scene" in AUTHORITY


def test_schema_is_well_formed():
    fn = next(t["function"] for t in FUNCTION_TOOL_SCHEMAS if t["function"]["name"] == "blender_scene")
    props = fn["parameters"]["properties"]
    assert props["action"]["enum"] == ["probe", "schema", "validate", "run", "smoke"]
    for key in ("scene", "ops", "meta", "folder", "timeout", "format", "op", "engine"):
        assert key in props
    assert props["scene"]["type"] == "object" and props["ops"]["type"] == "array"
    assert fn["parameters"]["required"] == []
    for op in bs.OPS:
        assert op in fn["description"], op


def test_the_runtime_validator_accepts_the_tool_arguments():
    from src.tool_schemas import validate_tool_arguments

    assert validate_tool_arguments("blender_scene", {"action": "run", "scene": scene(), "folder": "box", "timeout": 60}) == []
    assert validate_tool_arguments("blender_scene", {"action": "probe"}) == []
    assert validate_tool_arguments("blender_scene", {"ops": scene()["ops"], "meta": {"name": "x"}}) == []
    bad = validate_tool_arguments("blender_scene", {"action": "dance", "colour": 1})
    assert {e.field for e in bad} >= {"action", "colour"}


def test_settings_defaults_exist():
    from src.settings import DEFAULT_SETTINGS

    assert DEFAULT_SETTINGS["blender_path"] == ""
    assert DEFAULT_SETTINGS["blender_timeout_seconds"] == 300


# --------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------

def test_probe_action(monkeypatch):
    monkeypatch.setattr(bs, "probe", lambda: {"found": True, "path": "/b", "version": "4.3.2", "message": "Blender 4.3.2 at /b"})
    out = run_tool({"action": "probe"})
    assert out["exit_code"] == 0 and out["output"] == "Blender 4.3.2 at /b" and out["probe"]["found"]


def test_probe_action_when_blender_is_missing(monkeypatch):
    monkeypatch.setattr(bs, "probe", lambda: {"found": False, "message": "Blender was not found."})
    out = run_tool({"action": "probe"})
    assert out["exit_code"] == 0 and "not found" in out["output"] and out["probe"]["found"] is False


def test_schema_action_formats():
    summary = run_tool({"action": "schema"})
    assert summary["exit_code"] == 0 and "- add_mesh:" in summary["output"] and "${PROJECT_ROOT}" in summary["output"]
    assert len(summary["output"]) < 10_000
    one = run_tool({"action": "schema", "op": "render"})
    assert one["output"].startswith("- render:") and "add_mesh" not in one["output"]
    full = run_tool({"action": "schema", "format": "json_schema"})
    assert full["schema"]["$schema"] and json.loads(full["output"]) == full["schema"]
    bad = run_tool({"action": "schema", "op": "nope"})
    assert bad["exit_code"] == 1 and "unknown op" in bad["error"]


def test_validate_action_good_and_bad():
    good = run_tool({"action": "validate", "scene": scene()})
    assert good["exit_code"] == 0 and good["output"].startswith("Valid: 2 ops")
    bad = run_tool({"action": "validate", "ops": [{"op": "add_mesh", "primitive": "cube", "radius": 1},
                                                  {"op": "render", "path": "../x.png"}]})
    assert bad["exit_code"] == 1 and bad["output"].startswith("Invalid scene, 2 problem(s):")
    assert "ops[0] (add_mesh).radius" in bad["output"] and "ops[1] (render).path" in bad["output"]
    assert bad["validation"]["errors"][0]["op_index"] == 0
    assert run_tool({"action": "validate"})["exit_code"] == 1


def test_a_bare_scene_without_an_action_runs(monkeypatch):
    seen = {}

    def fake(scn, root, **kw):
        seen["root"] = root
        return fake_result(root)

    monkeypatch.setattr(bs, "run_scene", fake)
    out = run_tool({"scene": scene()})
    assert out["exit_code"] == 0 and seen["root"].endswith(os.path.join("scenes", "chat-1"))
    assert run_tool({})["exit_code"] == 1
    assert "action must be one of" in run_tool({"action": "dance"})["error"]


def test_run_uses_a_per_session_folder_and_a_sanitised_subfolder(monkeypatch, isolated_dirs):
    roots = []
    monkeypatch.setattr(bs, "run_scene", lambda scn, root, **kw: roots.append(root) or fake_result(root, with_image=False))
    run_tool({"action": "run", "scene": scene(), "folder": "chair"}, {"session_id": "chat-9"})
    run_tool({"action": "run", "scene": scene(), "folder": "../../etc"}, {"session_id": "../evil/id"})
    run_tool({"action": "run", "scene": scene()}, {})
    base = str(isolated_dirs / "scenes")
    assert roots[0] == os.path.join(base, "chat-9", "chair")
    for root in roots:
        assert os.path.commonpath([base, root]) == base, root
    assert ".." not in roots[1].replace(base, "")
    assert roots[2] == os.path.join(base, "default")


def test_run_passes_the_timeout_and_describes_the_result(monkeypatch):
    seen = {}
    monkeypatch.setattr(bs, "run_scene", lambda scn, root, **kw: seen.update(kw) or fake_result(root))
    out = run_tool({"action": "run", "scene": scene(), "timeout": 45})
    assert seen["timeout"] == 45
    text = out["output"]
    assert text.startswith("OK. Blender 4.3.2 ran 2 ops") and "Assertions: 1/1 passed" in text
    assert "${PROJECT_ROOT}/out/a.png" in text and "Warnings:" in text and "rendered image(s) attached" in text
    assert out["run"]["ops"][1]["op"] == "render" and "report" not in out["run"]
    assert out["project_root"].endswith("chat-1") and "error" not in out


def test_a_render_is_returned_as_an_image_and_registered_with_the_gallery_folder(monkeypatch, isolated_dirs):
    monkeypatch.setattr(bs, "run_scene", lambda scn, root, **kw: fake_result(root))
    out = run_tool({"action": "run", "scene": scene()})
    assert out["images"] == [{"data": base64.b64encode(PNG).decode("ascii"), "mimeType": "image/png"}]
    from src.tool_images import normalize_result_images, screenshot_data_url

    assert normalize_result_images(out)[0]["mimeType"] == "image/png"
    assert screenshot_data_url(out).startswith("data:image/png;base64,")
    url = out["image_url"]
    assert url.startswith("/api/generated-image/") and url.endswith(".png")
    # the name must be one the generated-image route serves, or the chat shows a broken image
    from src.generated_images import GENERATED_IMAGE_RE
    assert GENERATED_IMAGE_RE.fullmatch(url.rsplit("/", 1)[1])
    assert out["image_size"] == "320x240" and out["image_model"] == "blender 4.3.2" and out["image_prompt"] == "demo"
    copied = isolated_dirs / "generated" / url.rsplit("/", 1)[1]
    assert copied.read_bytes() == PNG
    assert "image_id" not in out   # the database was unavailable; the run still succeeded


def test_the_gallery_row_is_written_when_the_database_works(monkeypatch):
    rows = []

    class FakeDb:
        def add(self, row): rows.append(row)
        def commit(self): pass
        def close(self): pass

    import src.database as database

    monkeypatch.setattr(database, "SessionLocal", lambda: FakeDb())
    monkeypatch.setattr(bs, "run_scene", lambda scn, root, **kw: fake_result(root))
    out = run_tool({"action": "run", "scene": scene()}, {"session_id": "s1", "owner": "ada"})
    assert len(rows) == 1 and rows[0].owner == "ada" and rows[0].session_id == "s1"
    assert out["image_id"] == rows[0].id and rows[0].filename in out["image_url"]


def test_a_run_whose_ops_failed_keeps_its_report_and_images_without_an_error_key(monkeypatch):
    monkeypatch.setattr(bs, "run_scene", lambda scn, root, **kw: fake_result(root, ops_failed=1))
    out = run_tool({"action": "run", "scene": scene()})
    assert out["exit_code"] == 1 and "error" not in out and out["images"]
    assert out["output"].startswith("FAILED. ") and "ops[1] render: no active camera" in out["output"]


def test_a_run_that_could_not_start_is_an_error(monkeypatch):
    def failed(scn, root, **kw):
        return {"ok": False, "stage": "discovery", "errors": ["Blender was not found."], "warnings": [], "outputs": [],
                "images": []}

    monkeypatch.setattr(bs, "run_scene", failed)
    out = run_tool({"action": "run", "scene": scene()})
    assert out["exit_code"] == 1 and "FAILED at stage 'discovery'" in out["error"] and "images" not in out


def test_an_invalid_scene_is_rejected_before_blender_with_precise_errors(monkeypatch):
    monkeypatch.setattr(bs, "probe", lambda **k: pytest.fail("Blender must not be probed for an invalid scene"))
    out = run_tool({"action": "run", "scene": {"ops": [{"op": "add_mesh", "primitive": "cube", "location": [1, 2]}]}})
    assert out["exit_code"] == 1 and "stage 'validate'" in out["error"]
    assert "ops[0] (add_mesh).location: expected 3 numbers" in out["error"]


def test_oversized_images_are_not_attached(monkeypatch):
    monkeypatch.setattr(tool_module, "MAX_IMAGE_BYTES", 10)
    monkeypatch.setattr(bs, "run_scene", lambda scn, root, **kw: fake_result(root))
    out = run_tool({"action": "run", "scene": scene()})
    assert "images" not in out


def test_smoke_runs_the_scene_and_then_checks_the_reloaded_blend(monkeypatch):
    calls = []

    def fake(scn, root, **kw):
        calls.append((scn["meta"]["name"], os.path.basename(root)))
        return fake_result(root, with_image=len(calls) == 1)

    monkeypatch.setattr(bs, "run_scene", fake)
    monkeypatch.setattr(bs, "probe", lambda **k: {"found": True, "path": "/b", "version": "4.3.2"})
    out = run_tool({"action": "smoke"})
    assert [c[0] for c in calls] == ["smoke scene", "smoke scene reload"] and {c[1] for c in calls} == {"smoke"}
    assert "Reload check: 1/1 assertions passed" in out["output"] and out["reload"]["ok"] and out["exit_code"] == 0
    assert out["images"]
    assert run_tool({"action": "smoke", "engine": "unreal"})["exit_code"] == 1


def test_smoke_stops_after_a_failed_first_run(monkeypatch):
    calls = []
    monkeypatch.setattr(bs, "probe", lambda **k: {"found": True, "path": "/b", "version": "4.3.2"})
    monkeypatch.setattr(bs, "run_scene", lambda scn, root, **kw: calls.append(1) or fake_result(root, ops_failed=1))
    out = run_tool({"action": "smoke"})
    assert out["exit_code"] == 1 and len(calls) == 1


def test_non_json_content_is_an_error_not_a_crash():
    out = asyncio.run(BlenderSceneTool().execute("just words", {}))
    assert out["exit_code"] == 1


def test_an_unexpected_exception_becomes_an_error(monkeypatch):
    def boom(scn, root, **kw):
        raise RuntimeError("kaput")

    monkeypatch.setattr(bs, "run_scene", boom)
    out = run_tool({"action": "run", "scene": scene()})
    assert out["exit_code"] == 1 and "RuntimeError: kaput" in out["error"]


# --------------------------------------------------------------------------
# MCP server
# --------------------------------------------------------------------------

mcp = pytest.importorskip("mcp")
import mcp_servers.blender_scene_server as srv  # noqa: E402


def call(name, arguments):
    return asyncio.run(srv.call_tool(name, arguments))


def payload(out):
    return json.loads(out[0].text)


def test_server_identity_and_tool_surface():
    assert srv.server.name == "blender_scene"
    tools = asyncio.run(srv.list_tools())
    assert [t.name for t in tools] == ["blender_probe", "blender_scene_schema", "blender_scene_validate",
                                       "blender_scene_run", "blender_smoke"]
    for tool in tools:
        assert tool.inputSchema["type"] == "object" and len(tool.description) > 60, tool.name
        for required in tool.inputSchema.get("required", []):
            assert required in tool.inputSchema["properties"]
    run_schema = next(t for t in tools if t.name == "blender_scene_run").inputSchema
    assert {"scene", "ops", "meta", "folder", "timeout"} <= set(run_schema["properties"])


def test_server_probe_schema_and_validate(monkeypatch):
    monkeypatch.setattr(bs, "probe", lambda: {"found": False, "message": "Blender was not found."})
    got = payload(call("blender_probe", {}))
    assert got["ok"] is True and got["probe"]["found"] is False
    summary = payload(call("blender_scene_schema", {}))
    assert "- add_mesh:" in summary["output"]
    full = payload(call("blender_scene_schema", {"format": "json_schema"}))
    assert full["schema"]["$schema"] and "output" not in full
    ok = payload(call("blender_scene_validate", {"scene": scene()}))
    assert ok["ok"] is True and ok["validation"]["ok"]
    bad = payload(call("blender_scene_validate", {"ops": [{"op": "nope"}]}))
    assert bad["ok"] is False and "unknown op" in bad["validation"]["error_lines"][0]


def test_server_run_returns_the_report_and_the_image(monkeypatch):
    seen = {}

    def fake(scn, root, **kw):
        seen["root"] = root
        return fake_result(root)

    monkeypatch.setattr(bs, "run_scene", fake)
    out = call("blender_scene_run", {"scene": scene(), "folder": "box"})
    got = payload(out)
    assert got["ok"] is True and got["run"]["ok"] and got["run"]["outputs"][0]["kind"] == "render"
    assert seen["root"].endswith(os.path.join("scenes", "mcp", "box"))
    assert len(out) == 2 and out[1].type == "image" and out[1].mimeType == "image/png"
    assert "image_url" not in got   # the MCP server does not touch the gallery


def test_server_smoke(monkeypatch):
    monkeypatch.setattr(bs, "probe", lambda **k: {"found": True, "path": "/b", "version": "4.3.2"})
    monkeypatch.setattr(bs, "run_scene", lambda scn, root, **kw: fake_result(root))
    got = payload(call("blender_smoke", {}))
    assert got["ok"] is True and got["reload"]["ok"] is True


def test_server_errors_are_messages_not_exceptions(monkeypatch):
    assert "Unknown tool" in call("nope", {})[0].text
    assert "provide `scene`" in call("blender_scene_validate", {})[0].text
    assert "provide `scene`" in asyncio.run(srv.call_tool("blender_scene_run", None))[0].text
    monkeypatch.setattr(bs, "run_scene", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("kaput")))
    assert "kaput" in call("blender_scene_run", {"scene": scene()})[0].text


def test_registered_as_a_builtin_native_twin_server():
    from pathlib import Path

    from src import builtin_mcp
    from src.effect_tools import _BUILTIN_MCP_SERVERS

    script, name = builtin_mcp._BUILTIN_SERVERS["blender_scene"]
    assert script == "mcp_servers/blender_scene_server.py"
    assert (Path(__file__).parent.parent / script).is_file()
    assert "blender_scene" in builtin_mcp.NATIVE_TWIN_SERVERS
    assert "blender_scene" in _BUILTIN_MCP_SERVERS


# --------------------------------------------------------------------------
# REST route
# --------------------------------------------------------------------------

def _client(monkeypatch, allow=True):
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient

    import routes.blender_scene_routes as route_module

    def gate(request):
        if not allow:
            raise HTTPException(status_code=403, detail="admin only")

    monkeypatch.setattr(route_module, "require_admin", gate)
    app = FastAPI()
    app.include_router(route_module.setup_blender_scene_routes())
    return TestClient(app)


def test_route_probe_schema_validate_run(monkeypatch):
    client = _client(monkeypatch)
    monkeypatch.setattr(bs, "probe", lambda: {"found": True, "version": "4.3.2", "path": "/b"})
    assert client.get("/api/blender/probe").json()["version"] == "4.3.2"
    assert "- add_mesh:" in client.get("/api/blender/schema").json()["summary"]
    assert client.get("/api/blender/schema?format=json_schema").json()["$schema"]
    assert client.get("/api/blender/schema?op=nope").status_code == 400
    assert client.get("/api/blender/schema?format=xml").status_code == 400

    good = client.post("/api/blender/validate", json={"scene": scene()})
    assert good.status_code == 200 and good.json()["ok"] is True
    bad = client.post("/api/blender/validate", json={"ops": [{"op": "render", "path": "/etc/x.png"}]}).json()
    assert bad["ok"] is False and "outside the project folder" in bad["error_lines"][0]
    assert client.post("/api/blender/validate", json={}).status_code == 400

    seen = {}
    monkeypatch.setattr(bs, "run_scene", lambda scn, root, **kw: seen.update(root=root, kw=kw) or {"ok": True, "stage": "done"})
    res = client.post("/api/blender/run", json={"ops": scene()["ops"], "meta": {"name": "x"}, "folder": "chair", "timeout": 30})
    assert res.status_code == 200 and res.json()["ok"] is True
    assert seen["root"].endswith(os.path.join("scenes", "api", "chair")) and seen["kw"] == {"timeout": 30}


def test_route_requires_admin_on_every_endpoint(monkeypatch):
    client = _client(monkeypatch, allow=False)
    for method, url, body in (("get", "/api/blender/probe", None), ("get", "/api/blender/schema", None),
                              ("post", "/api/blender/validate", {"scene": scene()}),
                              ("post", "/api/blender/run", {"scene": scene()})):
        resp = client.get(url) if method == "get" else client.post(url, json=body)
        assert resp.status_code == 403, url
