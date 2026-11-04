"""The three surfaces over delivery packages (Hangar E33): chat tool, HTTP
routes and MCP server share src/delivery_package.run; owner scoping holds."""
from __future__ import annotations

import asyncio
import json
import struct
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import constants as constants_mod
from src import delivery_package as dp
from src import tool_index, tool_schemas
from src.agent_tools import TOOL_HANDLERS, TOOL_TAGS


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(constants_mod, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(dp, "_confine", lambda p: Path(str(p)).resolve())
    yield


def _stl(path: Path) -> Path:
    body = bytearray(b"\0" * 80) + struct.pack("<I", 1) + struct.pack("<12f", 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0) + b"\0\0"
    path.write_bytes(bytes(body))
    return path


def _args(tmp_path):
    return {"name": "Pieza", "items": [{"path": str(_stl(tmp_path / "p.stl")), "role": "editable"}],
            "instructions": "Abrir en el laminador.", "limitations": ["Sin prueba de impresión."]}


def test_tool_is_registered_everywhere_the_catalogue_looks():
    schemas = {s["function"]["name"] for s in tool_schemas.FUNCTION_TOOL_SCHEMAS}
    assert "delivery_package" in TOOL_HANDLERS and "delivery_package" in TOOL_TAGS and "delivery_package" in schemas
    assert "delivery_package" in tool_index.BUILTIN_TOOL_DESCRIPTIONS
    from src.tool_index_examples import EXAMPLES
    from src.tool_capabilities import TOOL_CAPABILITIES
    assert EXAMPLES["delivery_package"] and TOOL_CAPABILITIES["delivery_package"].effects


def test_chat_tool_builds_and_lists(tmp_path):
    out = asyncio.run(TOOL_HANDLERS["delivery_package"](json.dumps({"action": "build", **_args(tmp_path)}), {"owner": "alice"}))
    assert out["exit_code"] == 0 and out["result"]["ready"] is True and "READY" in out["output"]
    listed = asyncio.run(TOOL_HANDLERS["delivery_package"](json.dumps({"action": "list"}), {"owner": "alice"}))
    assert listed["result"]["packages"][0]["name"] == "Pieza"
    bad = asyncio.run(TOOL_HANDLERS["delivery_package"]("not json", {"owner": "alice"}))
    assert bad["exit_code"] == 1


@pytest.fixture()
def client(monkeypatch):
    import routes.delivery_routes as dr
    monkeypatch.setattr(dr, "effective_user", lambda request: "alice")
    monkeypatch.setattr(dr, "require_admin", lambda request: None)
    app = FastAPI()
    app.include_router(dr.setup_delivery_routes())
    return TestClient(app)


def test_http_routes(client, tmp_path):
    r = client.post("/api/delivery-packages", json=_args(tmp_path))
    assert r.status_code == 200, r.text
    pkg = r.json()
    assert pkg["ready"] is True and pkg["reimport"]["ok"] is True
    assert client.get("/api/delivery-packages").json()["packages"][0]["id"] == pkg["id"]
    assert client.get(f"/api/delivery-packages/{pkg['id']}").json()["name"] == "Pieza"
    assert client.post(f"/api/delivery-packages/{pkg['id']}/reverify").json()["reverify"]["ok"] is True
    z = client.get(f"/api/delivery-packages/{pkg['id']}/download")
    assert z.status_code == 200 and z.content[:2] == b"PK"
    assert "Lista para entregar" in client.get(f"/api/delivery-packages/{pkg['id']}/document").text
    assert ".3mf" in client.get("/api/delivery-packages/readers").json()["readers"]
    assert client.get("/api/delivery-packages/20260101-000000-abcdef").status_code == 404
    assert client.post("/api/delivery-packages", json={"items": []}).status_code == 400
    secret = tmp_path / ".ssh" / "id_rsa"
    secret.parent.mkdir()
    secret.write_text("key", encoding="utf-8")
    refused = client.post("/api/delivery-packages", json={**_args(tmp_path), "items": [{"path": str(secret), "role": "editable"}]}).json()
    assert refused["items"] == [] and "sensitive" in refused["excluded"][0]["reason"]


def test_mcp_server_lists_the_tool_and_refuses_without_an_owner(monkeypatch, tmp_path):
    pytest.importorskip("mcp")
    import mcp_servers.delivery_server as srv
    tools = asyncio.run(srv.list_tools())
    assert [t.name for t in tools] == ["delivery_package"] and tools[0].inputSchema["type"] == "object"
    monkeypatch.delenv("ODYSSEUS_MCP_DELIVERY_OWNER", raising=False)
    monkeypatch.delenv("ODYSSEUS_DELIVERY_OWNER", raising=False)
    assert "no owner configured" in asyncio.run(srv.call_tool("delivery_package", {"action": "list"}))[0].text
    monkeypatch.setenv("ODYSSEUS_MCP_DELIVERY_OWNER", "alice")
    res = asyncio.run(srv.call_tool("delivery_package", {"action": "build", **_args(tmp_path)}))
    assert json.loads(res[0].text)["ready"] is True
    assert "Unknown tool" in asyncio.run(srv.call_tool("other", {}))[0].text


def test_the_server_is_a_registered_builtin_twin():
    from src import builtin_mcp
    assert "delivery" in builtin_mcp._BUILTIN_SERVERS
    assert "delivery" in builtin_mcp.NATIVE_TWIN_SERVERS
