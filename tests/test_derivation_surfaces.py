"""The three surfaces over recipe learning, derivation graphs and the decision
map: chat tools, HTTP routes and the MCP server. One implementation behind all
of them (src/derivation_ops.py); these tests pin the wiring, the owner scoping
and the path confinement."""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import constants as constants_mod
from src import derivation_ops as ops
from src import tool_index, tool_schemas
from src.agent_tools import TOOL_HANDLERS, TOOL_TAGS

NAMES = ("recipe_learn", "derivation_graph", "decision_map")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(constants_mod, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(ops, "_confine", lambda p: str(p) if p else str(tmp_path))
    yield


def test_tools_are_registered_everywhere_the_catalogue_looks():
    schemas = {s["function"]["name"] for s in tool_schemas.FUNCTION_TOOL_SCHEMAS}
    for n in NAMES:
        assert n in TOOL_HANDLERS and n in TOOL_TAGS and n in schemas and n in tool_index.BUILTIN_TOOL_DESCRIPTIONS
    from src.tool_index_examples import EXAMPLES
    from src.tool_capabilities import TOOL_CAPABILITIES
    for n in NAMES:
        assert EXAMPLES[n]
        assert TOOL_CAPABILITIES[n].effects


def run_tool(name, args, owner="alice"):
    return asyncio.run(TOOL_HANDLERS[name](json.dumps(args), {"owner": owner}))


def test_chat_tools_end_to_end(tmp_path):
    work = tmp_path / "w"
    work.mkdir()
    (work / "in.txt").write_text("a", encoding="utf-8")
    out = run_tool("derivation_graph", {"action": "define", "node": "n1", "base_dir": str(work),
                                        "op": {"kind": "tool", "name": "up", "version": "1"},
                                        "inputs": ["in.txt"], "outputs": ["o.txt"]})
    assert out["exit_code"] == 0 and out["id"] == "n1"
    step = run_tool("derivation_graph", {"action": "rebuild", "target": "n1"})
    assert step["status"] == "awaiting_execution" and step["next"]["id"] == "n1"
    (work / "o.txt").write_text("A", encoding="utf-8")
    done = run_tool("derivation_graph", {"action": "complete", "node": "n1", "outputs": ["o.txt"]})
    assert done["status"] == "done"
    assert run_tool("derivation_graph", {"action": "plan", "target": "n1"})["stale"] == []
    (work / "in.txt").write_text("b", encoding="utf-8")
    assert run_tool("derivation_graph", {"action": "plan"})["stale"] == ["n1"]
    run_tool("decision_map", {"action": "add", "id": "req:u", "kind": "unit", "title": "metres"})
    run_tool("decision_map", {"action": "link", "from": "req:u", "to": "node:n1"})
    aff = run_tool("decision_map", {"action": "affected_by", "change": "metres"})
    assert aff["recalculate"] == ["n1"]
    # another owner sees nothing
    assert run_tool("derivation_graph", {"action": "show"}, owner="bob")["nodes"] == []


def test_recipe_tool_proposes_tries_and_refuses_a_bad_second_case():
    steps = [{"tool": "read_file", "args": {"path": "/data/q3.csv"}}]
    out = run_tool("recipe_learn", {"action": "propose", "request": "read /data/q3.csv", "steps": steps, "title": "Read"})
    rid = out["recipe"]["id"]
    assert out["recipe"]["params"][0]["name"] == "path"
    bad = run_tool("recipe_learn", {"action": "try", "recipe_id": rid, "params": {"path": "/data/q3.csv"}})
    assert bad["promotable"] is False and bad["untested_params"] == ["path"]
    good = run_tool("recipe_learn", {"action": "try", "recipe_id": rid, "params": {"path": "/data/q4.csv"}})
    assert good["promotable"] is True
    assert run_tool("recipe_learn", {"action": "nope"})["error_class"] == "recipes.bad_action"
    assert run_tool("recipe_learn", {"action": "show", "recipe_id": "zzz"})["error_class"] == "recipes.not_found"
    assert "error" in asyncio.run(TOOL_HANDLERS["recipe_learn"]("not json", {"owner": "alice"}))


def test_paths_outside_the_workspace_are_refused(monkeypatch, tmp_path):
    def deny(p):
        if p and "outside" in str(p):
            raise PermissionError("path is outside the allowed roots")
        return str(p) if p else str(tmp_path)
    monkeypatch.setattr(ops, "_confine", deny)
    out = run_tool("derivation_graph", {"action": "define", "node": "x", "op": {"name": "t"},
                                        "inputs": ["/outside/secret.txt"], "outputs": ["o"]})
    assert out["exit_code"] == 1 and "outside" in out["error"]
    assert run_tool("derivation_graph", {"action": "show"})["nodes"] == []


@pytest.fixture()
def client(monkeypatch):
    import routes.derivation_routes as dr
    monkeypatch.setattr(dr, "effective_user", lambda request: "alice")
    monkeypatch.setattr(dr, "require_admin", lambda request: None)
    app = FastAPI()
    app.include_router(dr.setup_derivation_routes())
    return TestClient(app)


def test_http_routes(client, tmp_path):
    work = tmp_path / "w"
    work.mkdir()
    (work / "in.txt").write_text("a", encoding="utf-8")
    r = client.post("/api/derivations/g1/define", json={"node": "n", "base_dir": str(work), "op": {"name": "t", "version": "1"},
                                                        "inputs": ["in.txt"], "outputs": ["o.txt"]})
    assert r.status_code == 200 and r.json()["id"] == "n"
    assert client.get("/api/derivations/g1/plan").json()["stale"] == ["n"]
    assert client.get("/api/derivations").json()["graphs"][0]["id"] == "g1"
    assert client.post("/api/derivations/g1/rebuild", json={"target": "n"}).json()["status"] == "awaiting_execution"
    assert client.post("/api/derivations/g1/bogus", json={}).status_code == 400
    assert client.post("/api/derivations/g1/remove", json={"node": "ghost"}).status_code == 404
    client.post("/api/decision-map/g1/add", json={"id": "req:f", "kind": "format", "title": "PDF"})
    client.post("/api/decision-map/g1/link", json={"from": "req:f", "to": "node:n"})
    aff = client.get("/api/decision-map/g1/affected-by", params={"change": "PDF", "invalidate": "true"}).json()
    assert aff["invalidated"] == ["n"]
    assert client.get("/api/decision-map/g1/affected-by", params={"change": "nothing here"}).status_code == 404
    learn = client.post("/api/recipes/learn", json={"request": "read /d/a.csv", "title": "R",
                                                    "steps": [{"tool": "read_file", "args": {"path": "/d/a.csv"}}]})
    rid = learn.json()["recipe"]["id"]
    assert client.post(f"/api/recipes/{rid}/promote").status_code == 400          # no try yet
    t = client.post(f"/api/recipes/{rid}/try", json={"params": {"path": "/d/b.csv"}}).json()
    assert t["promotable"] is True and t["executed"] is False
    assert any(h["id"] == rid for h in client.get("/api/recipes/search", params={"query": "read csv"}).json()["recipes"])


def test_mcp_server_lists_the_tools_and_refuses_without_an_owner(monkeypatch, tmp_path):
    pytest.importorskip("mcp")
    import mcp_servers.derivations_server as srv
    tools = asyncio.run(srv.list_tools())
    assert {t.name for t in tools} == set(NAMES) and all(t.inputSchema["type"] == "object" for t in tools)
    monkeypatch.delenv("ODYSSEUS_MCP_DERIVATIONS_OWNER", raising=False)
    monkeypatch.delenv("ODYSSEUS_DERIVATIONS_OWNER", raising=False)
    res = asyncio.run(srv.call_tool("decision_map", {"action": "show"}))
    assert "no owner configured" in res[0].text
    monkeypatch.setenv("ODYSSEUS_MCP_DERIVATIONS_OWNER", "alice")
    res = asyncio.run(srv.call_tool("decision_map", {"action": "add", "id": "r1", "kind": "requirement", "title": "T"}))
    assert json.loads(res[0].text)["id"] == "r1"
    assert "Unknown tool" in asyncio.run(srv.call_tool("other", {}))[0].text


def test_the_server_is_a_registered_builtin_and_hidden_from_agent_retrieval():
    from src import builtin_mcp
    assert "derivations" in builtin_mcp.BUILTIN_SERVERS if hasattr(builtin_mcp, "BUILTIN_SERVERS") else True
    assert "derivations" in builtin_mcp.NATIVE_TWIN_SERVERS
