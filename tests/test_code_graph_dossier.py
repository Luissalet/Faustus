"""Tests for `code_graph.dossier`, `dossier_diff`, `locate` and `context_pack`.

One synthetic repo: a python package with a FastAPI app, a CLI, tests,
a package.json with scripts, an env var, an outbound URL and a dynamic import.
"""
from __future__ import annotations

import os
import subprocess

import pytest

from src import code_graph
from src import tool_execution as te
from src.context_engine import store as ce_store


def _write(root, rel, text):
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path) or root, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def _ln(text, needle):
    return next(i for i, l in enumerate(text.split("\n"), 1) if needle in l)


def _git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)


def _commit(root, msg):
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=T", "-c", "user.email=t@example.com", "commit", "-q", "-m", msg)


APP_PY = '''"""Web app."""
import os
import importlib

import requests
from fastapi import FastAPI
from pydantic import BaseModel

from shop.orders import place_order

app = FastAPI()
API_KEY = os.environ["SHOP_API_KEY"]
REGION = os.getenv("SHOP_REGION", "eu")


class OrderIn(BaseModel):
    sku: str
    qty: int


@app.post("/orders")
def create_order(payload: OrderIn) -> dict:
    """Create an order."""
    requests.get("https://payments.acme-pay.test/v1/ping")
    return place_order(payload.sku, payload.qty)


def load_plugin(name):
    return importlib.import_module("shop.plugins." + name)
'''
ORDERS_PY = '''"""Orders."""
import sqlite3


def place_order(sku, qty):
    conn = sqlite3.connect("orders.db")
    return {"sku": sku, "qty": qty, "ok": check_stock(sku)}


def check_stock(sku):
    return True
'''
CLI_PY = '''"""CLI."""
import argparse

from shop.orders import place_order


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("sku")
    args = parser.parse_args()
    print(place_order(args.sku, 1))


if __name__ == "__main__":
    main()
'''
TEST_PY = '''from shop.orders import place_order, check_stock


def test_place_order():
    result = place_order("a", 2)
    assert result["qty"] == 2
    assert result["ok"] is True


def test_check_stock():
    assert check_stock("a")
'''
PKG_JSON = '''{
  "name": "shop-ui",
  "scripts": {
    "start": "node server.js",
    "test": "jest"
  },
  "dependencies": {"express": "^4.0.0"}
}
'''
README = '''# Shop

```bash
pip install -r requirements.txt
uvicorn shop.app:app --reload
```
'''


@pytest.fixture()
def repo(tmp_path):
    ce_store.use_path(str(tmp_path / "ce.db"))
    root = tmp_path / "repo"
    root.mkdir()
    r = str(root)
    _write(r, "shop/__init__.py", "")
    _write(r, "shop/app.py", APP_PY)
    _write(r, "shop/orders.py", ORDERS_PY)
    _write(r, "shop/cli.py", CLI_PY)
    _write(r, "tests/test_orders.py", TEST_PY)
    _write(r, "package.json", PKG_JSON)
    _write(r, "requirements.txt", "fastapi\nrequests\n")
    _write(r, "README.md", README)
    _write(r, "static/index.html", "<html><head><title>Shop home</title></head></html>\n")
    _write(r, "db/schema.sql", "CREATE TABLE orders (id INTEGER);\n")
    _git(r, "init", "-q")
    _commit(r, "initial")
    tok1 = te._active_workspace.set(r)
    tok2 = te._active_workspace_roots.set((r,))
    try:
        yield r
    finally:
        te._active_workspace.reset(tok1)
        te._active_workspace_roots.reset(tok2)
        ce_store.use_path(None)


def _facts(d, section):
    return d["dossier"]["sections"][section]


def _has(facts, needle, key=None):
    return [f for f in facts if needle in f["fact"] or (key and f["key"].startswith(key))]


def test_every_fact_has_evidence_and_certainty(repo):
    d = code_graph.dossier(repo)
    assert d["exit_code"] == 0 and d["cached"] is False
    for sec, facts in d["dossier"]["sections"].items():
        for f in facts:
            assert f["certainty"] in ("code", "config", "inferred"), (sec, f)
            # index-level facts (no single line) use "(code graph)" as evidence
            assert f["evidence"] and (":" in f["evidence"] or f["evidence"] == "(code graph)"), (sec, f)


def test_entry_points(repo):
    d = code_graph.dossier(repo)
    ep = _facts(d, "entry_points")
    assert any(f["key"] == "main:shop/cli.py" and f["evidence"] == f"shop/cli.py:{_ln(CLI_PY, 'if __name__')}" and f["certainty"] == "code" for f in ep)
    assert any(f["key"].startswith("web:shop/app.py") for f in ep)
    start = next(f for f in ep if f["key"] == "npm-script:package.json:start")
    assert start["certainty"] == "config" and start["evidence"] == "package.json:4"
    assert any(f["key"].startswith("routes:shop/app.py") for f in ep)


def test_build_run_quotes_files(repo):
    br = _facts(code_graph.dossier(repo), "build_run")
    readme = [f for f in br if f["key"].startswith("readme:")]
    assert {f["quote"] for f in readme} == {"pip install -r requirements.txt", "uvicorn shop.app:app --reload"}
    assert all(f["certainty"] == "config" for f in readme)
    assert next(f for f in readme if "uvicorn" in f["quote"])["evidence"] == "README.md:5"
    reqs = next(f for f in br if f["key"] == "py-reqs:requirements.txt")
    assert reqs["certainty"] == "inferred"


def test_tests_section(repo):
    t = _facts(code_graph.dossier(repo), "test")
    area = next(f for f in t if f["key"] == "tests:tests")
    assert area["value"] == 2 and area["evidence"] == "tests/test_orders.py:4"
    assert any(f["key"] == "npm-test:package.json:test" and f["quote"] == "jest" for f in t)


def test_data_integrations_screens(repo):
    d = code_graph.dossier(repo)
    data = _facts(d, "data")
    assert any(f["key"] == "sql:db/schema.sql" for f in data)
    assert any(f["key"].startswith("db:shop/orders.py") and f["certainty"] == "code" for f in data)
    integ = _facts(d, "integrations")
    host = next(f for f in integ if f["key"] == "host:payments.acme-pay.test")
    assert host["certainty"] == "code" and host["evidence"].startswith("shop/app.py:")
    assert next(f for f in integ if f["key"] == "env:SHOP_API_KEY")["has_default"] is False
    assert next(f for f in integ if f["key"] == "env:SHOP_REGION")["has_default"] is True
    assert any(f["key"] == "sdk:fastapi" for f in integ)
    assert not any(f["key"] == "sdk:shop" for f in integ)
    screens = _facts(d, "screens")
    assert any(f["key"] == "page:static/index.html" and "Shop home" in f["fact"] for f in screens)


def test_structure_and_flows(repo):
    d = code_graph.dossier(repo)
    assert _facts(d, "structure")
    assert any(f["certainty"] == "inferred" for f in _facts(d, "structure"))
    assert any("create_order" in f["key"] for f in _facts(d, "main_flows"))


def test_uncertainties(repo):
    un = _facts(code_graph.dossier(repo), "uncertainties")
    dyn = [f for f in un if f["key"].startswith("dynamic:shop/app.py")]
    assert dyn and "importlib" in dyn[0]["fact"] and dyn[0]["evidence"] == f"shop/app.py:{_ln(APP_PY, 'importlib.import_module')}"
    assert any(f["key"] == "ambiguous-entry" for f in un)
    assert any(f["key"] == "env-required:SHOP_API_KEY" for f in un)
    assert not any(f["key"] == "env-required:SHOP_REGION" for f in un)


def test_cached_per_revision_and_refresh(repo):
    first = code_graph.dossier(repo)
    again = code_graph.dossier(repo)
    assert again["cached"] is True and again["revision"] == first["revision"]
    assert code_graph.dossier(repo, refresh=True)["cached"] is False


def test_dossier_diff_between_revisions(repo):
    first = code_graph.dossier(repo)
    _write(repo, "package.json", PKG_JSON.replace('"test": "jest"', '"test": "vitest",\n    "build": "tsc"'))
    _write(repo, "shop/jobs.py", 'import os\nTOKEN = os.environ["JOB_TOKEN"]\n')
    _commit(repo, "second")
    second = code_graph.dossier(repo)
    assert second["revision"] != first["revision"]
    diff = code_graph.dossier_diff(repo)
    assert diff["exit_code"] == 0
    assert diff["from_revision"] == first["revision"] and diff["to_revision"] == second["revision"]
    added = {f["key"] for f in diff["changes"]["integrations"]["added"]}
    assert "env:JOB_TOKEN" in added
    assert any(f["key"] == "npm-script:package.json:build" for f in diff["changes"]["entry_points"]["added"])
    changed = diff["changes"]["test"]["changed"]
    assert any(c["after"].endswith("vitest") and c["before"].endswith("jest") for c in changed)
    explicit = code_graph.dossier_diff(repo, from_revision=first["revision"][:8], to_revision=second["revision"])
    assert explicit["total"] == diff["total"]


def test_diff_without_earlier_dossier_is_clean_error(repo):
    res = code_graph.dossier_diff(repo)
    assert res["exit_code"] == 1 and "earlier" in res["error"]


def test_dirty_tree_gets_its_own_revision(repo):
    clean = code_graph.dossier(repo)["revision"]
    _write(repo, "shop/extra.py", "X = 1\n")
    assert code_graph.dossier(repo)["revision"] != clean


def test_locate_returns_labelled_candidates(repo):
    res = code_graph.locate(repo, "where do I change how stock is checked when placing an order")
    assert res["exit_code"] == 0
    names = [c["symbol"] for c in res["candidates"]]
    assert "check_stock" in names and "place_order" in names
    assert all(c["label"] == "candidate" for c in res["candidates"])
    po = next(c for c in res["candidates"] if c["symbol"] == "place_order")
    assert "tests/test_orders.py" in po["tests"]
    assert po["flows"]
    assert "candidates only" in res["note"]


def test_context_pack_contents(repo):
    res = code_graph.context_pack("place_order", repo, budget_lines=200)
    assert res["exit_code"] == 0
    kinds = {i["kind"] for i in res["items"]}
    assert {"source", "contract", "consumer", "test"} <= kinds
    src = next(i for i in res["items"] if i["kind"] == "source")
    assert src["lines"][0]["text"] == "def place_order(sku, qty):"
    consumers = [i for i in res["items"] if i["kind"] == "consumer"]
    texts = {l["text"].strip() for c in consumers for l in c["lines"]}
    assert "return place_order(payload.sku, payload.qty)" in texts
    assert "print(place_order(args.sku, 1))" in texts
    tests = [l["text"].strip() for i in res["items"] if i["kind"] == "test" for l in i["lines"]]
    assert 'assert result["qty"] == 2' in tests
    assert res["left_out"] == [] and res["used_lines"] <= 200


def test_context_pack_contract_with_pydantic_model(repo):
    res = code_graph.context_pack("create_order", repo, budget_lines=200)
    contracts = [i for i in res["items"] if i["kind"] == "contract"]
    joined = "\n".join(l["text"] for c in contracts for l in c["lines"])
    assert '@app.post("/orders")' in joined and "payload: OrderIn" in joined and "Create an order." in joined
    assert "class OrderIn(BaseModel):" in joined and "qty: int" in joined


def test_context_pack_budget_trims_and_lists_left_out(repo):
    res = code_graph.context_pack("place_order", repo, budget_lines=8)
    assert res["used_lines"] <= 8
    assert res["left_out"]
    assert all(x["more"]["tool"].startswith("code_graph_") for x in res["left_out"])
    assert res["items"][0]["kind"] == "source"
    assert "left out" in res["output"]


def test_context_pack_known_hashes(repo):
    first = code_graph.context_pack("place_order", repo, budget_lines=200)
    assert "shop/orders.py" in first["hashes"]
    second = code_graph.context_pack("place_order", repo, budget_lines=200, known_hashes=first["hashes"])
    assert second["items"] == [] or all(i["path"] not in first["hashes"] for i in second["items"])
    assert any(u["path"] == "shop/orders.py" and u["status"].startswith("unchanged since ") for u in second["unchanged"])
    assert second["used_lines"] < first["used_lines"]
    _write(repo, "shop/orders.py", ORDERS_PY + "\n# edited\n")
    third = code_graph.context_pack("place_order", repo, budget_lines=200, known_hashes=first["hashes"])
    assert any(i["kind"] == "source" for i in third["items"])


def test_context_pack_unknown_symbol(repo):
    res = code_graph.context_pack("definitely_not_here_xyz", repo)
    assert res["exit_code"] == 1


def test_context_pack_from_paths(repo):
    res = code_graph.context_pack(["shop/orders.py"], repo, budget_lines=300)
    assert {t["symbol"] for t in res["targets"]} >= {"place_order", "check_stock"}


def test_http_routes(repo, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core import middleware
    from routes.code_graph_routes import setup_code_graph_routes
    monkeypatch.setattr(middleware, "auth_disabled", lambda: True)
    app = FastAPI()
    app.include_router(setup_code_graph_routes())
    c = TestClient(app)
    d = c.get("/api/code-graph/dossier", params={"root": repo}).json()
    assert d["exit_code"] == 0 and d["dossier"]["sections"]["entry_points"]
    assert c.get("/api/code-graph/dossier/diff", params={"root": repo}).json()["exit_code"] == 1
    loc = c.get("/api/code-graph/locate", params={"root": repo, "question": "check stock"}).json()
    assert any(x["symbol"] == "check_stock" for x in loc["candidates"])
    pack = c.post("/api/code-graph/context-pack",
                  json={"symbols": ["place_order"], "root": repo, "budget_lines": 40}).json()
    assert pack["exit_code"] == 0 and pack["hashes"]
    again = c.post("/api/code-graph/context-pack",
                   json={"symbols": ["place_order"], "root": repo, "budget_lines": 40,
                         "known_hashes": pack["hashes"]}).json()
    assert again["unchanged"]


def test_chat_tool_executors(repo):
    import asyncio
    import json
    from src.agent_tools.code_graph_tools import (CodeGraphContextPackTool, CodeGraphDossierTool,
                                                  CodeGraphLocateTool)
    d = asyncio.run(CodeGraphDossierTool().execute(json.dumps({"root": repo}), {}))
    assert d["exit_code"] == 0 and d["revision"]
    loc = asyncio.run(CodeGraphLocateTool().execute("check stock", {}))
    assert loc["exit_code"] == 0
    pk = asyncio.run(CodeGraphContextPackTool().execute(json.dumps({"symbols": ["place_order"], "root": repo}), {}))
    assert pk["exit_code"] == 0 and pk["items"]


def test_mcp_tools(repo):
    pytest.importorskip("mcp")
    import asyncio
    import mcp_servers.code_graph_server as cgs
    cgs._initialized, cgs._engine = False, {}
    out = asyncio.run(cgs.call_tool("code_graph_dossier", {"root": repo}))
    assert '"revision"' in out[0].text and '"uncertainties"' in out[0].text
    out = asyncio.run(cgs.call_tool("code_graph_locate", {"root": repo, "question": "check stock"}))
    assert "check_stock" in out[0].text
    out = asyncio.run(cgs.call_tool("code_graph_context_pack",
                                    {"root": repo, "symbols": ["place_order"], "budget_lines": 30}))
    assert '"hashes"' in out[0].text


def test_context_pack_strips_windows_line_endings(tmp_path):
    (tmp_path / "crlf.py").write_bytes(b"def crlf_target(x):\r\n    return x + 1\r\n\r\n\r\ndef caller():\r\n    return crlf_target(2)\r\n")
    res = code_graph.context_pack("crlf_target", str(tmp_path), budget_lines=50)
    texts = [l["text"] for i in res["items"] for l in i.get("lines", [])]
    assert texts and not any(t.endswith("\r") for t in texts)
