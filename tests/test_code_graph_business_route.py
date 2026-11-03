"""Tests for `code_graph.business_route`: a button in a static page, its handler,
the request it sends, the FastAPI route that serves it, the service that runs an
INSERT, and the test that hits the route -- plus a dynamic URL (unresolved) and a
function with a similar name (never a link)."""
from __future__ import annotations

import os

import pytest

from src import code_graph
from src import tool_execution as te
from src.context_engine import store as ce_store


def _write(root, rel, text):
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path) or root, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


INDEX_HTML = '''<!doctype html>
<html><head><title>Presupuestos</title></head>
<body>
  <h1>Presupuestos</h1>
  <button id="gen" onclick="generarPresupuesto()">Generar presupuesto</button>
  <button id="reload">Recargar lista</button>
  <script src="app.js"></script>
</body></html>
'''
APP_JS = '''const API = "/api";

async function generarPresupuesto() {
  const body = JSON.stringify({ customer: "x", total: 10 });
  const res = await fetch(`${API}/quotes`, { method: "POST", headers: { "Content-Type": "application/json" }, body });
  await loadQuotes();
  return res.json();
}

async function loadQuotes(base, path) {
  const r = await fetch(base + path);
  return r.json();
}

document.getElementById("reload").addEventListener("click", loadQuotes);
'''
API_PY = '''"""HTTP API."""
from fastapi import FastAPI

from shop.quote_service import save_quote

app = FastAPI()


@app.post("/api/quotes")
def create_quote(payload: dict) -> dict:
    """Create a quote."""
    return save_quote(payload)


@app.get("/api/quotes/{quote_id}")
def read_quote(quote_id: int) -> dict:
    return {"id": quote_id}
'''
SERVICE_PY = '''"""Quote service."""
import sqlite3


def save_quote(data):
    conn = sqlite3.connect("quotes.db")
    conn.execute("INSERT INTO quotes (customer, total) VALUES (?, ?)", (data["customer"], data["total"]))
    conn.commit()
    return {"ok": True}


def load_quotes_cache():
    return []
'''
TEST_PY = '''from fastapi.testclient import TestClient

from shop.api import app


def test_create_quote():
    client = TestClient(app)
    r = client.post("/api/quotes", json={"customer": "x", "total": 1})
    assert r.status_code == 200
'''


@pytest.fixture()
def repo(tmp_path):
    ce_store.use_path(str(tmp_path / "ce.db"))
    root = tmp_path / "repo"
    root.mkdir()
    r = str(root)
    _write(r, "static/index.html", INDEX_HTML)
    _write(r, "static/app.js", APP_JS)
    _write(r, "shop/__init__.py", "")
    _write(r, "shop/api.py", API_PY)
    _write(r, "shop/quote_service.py", SERVICE_PY)
    _write(r, "tests/test_quotes.py", TEST_PY)
    tok1 = te._active_workspace.set(r)
    tok2 = te._active_workspace_roots.set((r,))
    try:
        yield r
    finally:
        te._active_workspace.reset(tok1)
        te._active_workspace_roots.reset(tok2)
        ce_store.use_path(None)


def _kinds(res):
    return [(h["kind"], h["certainty"]) for h in res["hops"]]


def test_button_to_insert_hops(repo):
    res = code_graph.business_route(repo, "Generar presupuesto", "saved")
    assert res["exit_code"] == 0, res
    kinds = [h["kind"] for h in res["hops"]]
    assert kinds[:4] == ["ui_event", "handler", "http_call", "route"]
    ui, handler, call, route = res["hops"][:4]
    assert ui["evidence"] == "static/index.html:5"
    assert handler["label"] == "generarPresupuesto()" and handler["evidence"].startswith("static/index.html:5")
    assert call["label"] == "POST /api/quotes" and call["evidence"] == "static/app.js:5"
    assert route["evidence"] == "shop/api.py:10" and "create_quote" in route["label"]
    calls = [h for h in res["hops"] if h["kind"] == "call" and h["layer"] == "backend"]
    assert any(h["symbol"].endswith("save_quote") for h in calls)
    insert = next(h for h in res["hops"] if h["kind"] == "db_write")
    assert insert["label"] == "INSERT INTO quotes" and insert["evidence"] == "shop/quote_service.py:7"
    assert insert.get("is_end") and res["reached_end"] is True
    assert all(h["certainty"] == "resolved" for h in res["hops"])
    assert res["runtime_trace"]["provided"] is False
    assert "INSERT INTO quotes" in res["summary"]


def test_tests_attached_to_the_route_and_below(repo):
    res = code_graph.business_route(repo, "Generar presupuesto")
    route = next(h for h in res["hops"] if h["kind"] == "route")
    assert [t["path"] for t in route["tests"]] == ["tests/test_quotes.py"]
    assert route["tests"][0]["evidence"] == "tests/test_quotes.py:8"
    insert = next(h for h in res["hops"] if h["kind"] == "db_write")
    assert any(t["path"] == "tests/test_quotes.py" for t in insert["tests"])


def test_dynamic_url_is_unresolved_gap(repo):
    res = code_graph.business_route(repo, "Generar presupuesto")
    dyn = [g for g in res["gaps"] if g["kind"] == "dynamic_url"]
    assert dyn and dyn[0]["certainty"] == "unresolved" and dyn[0]["evidence"] == "static/app.js:11"
    # it never became a hop
    assert not any(h["evidence"] == "static/app.js:11" for h in res["hops"])


def test_similar_name_is_listed_as_gap_never_linked(repo):
    res = code_graph.business_route(repo, "Generar presupuesto")
    names = [g for g in res["gaps"] if g["kind"] == "name-only"]
    assert any("load_quotes_cache" in g["label"] and g["certainty"] == "name-only" for g in names)
    assert not any("load_quotes_cache" in h["label"] for h in res["hops"])
    assert not any(h["certainty"] == "name-only" for h in res["hops"])


def test_runtime_trace_confirms_and_completes(repo):
    trace = [
        {"kind": "http", "method": "POST", "path": "/api/quotes", "function": "create_quote", "file": "shop/api.py", "line": 10},
        {"kind": "call", "function": "save_quote", "file": "shop/quote_service.py", "line": 5},
        {"kind": "sql", "function": "save_quote", "file": "shop/quote_service.py", "line": 6, "sql": "INSERT INTO quotes"},
        {"kind": "call", "function": "audit_log", "file": "shop/audit.py", "line": 3},
    ]
    res = code_graph.business_route(repo, "Generar presupuesto", "saved", trace)
    by_kind = {h["kind"]: h for h in res["hops"]}
    assert by_kind["http_call"]["certainty"] == "observed" and by_kind["route"]["certainty"] == "observed"
    assert by_kind["db_write"]["certainty"] == "observed"
    save = next(h for h in res["hops"] if h["kind"] == "call" and h["symbol"].endswith("save_quote"))
    assert save["certainty"] == "observed"
    assert by_kind["ui_event"]["certainty"] == "resolved" and not by_kind["ui_event"]["confirmed"]
    info = res["runtime_trace"]
    assert info["provided"] and len(info["confirmed_hops"]) >= 4
    extra = [h for h in res["hops"] if h.get("from_trace")]
    assert [h["label"] for h in extra] == ["audit_log()"] and extra[0]["certainty"] == "observed"
    assert len(info["unmatched_events"]) == 1


def test_trace_completes_dynamic_call(repo):
    trace = [{"kind": "http", "method": "GET", "path": "/api/quotes/7"}]
    res = code_graph.business_route(repo, "Generar presupuesto", None, trace)
    done = [h for h in res["hops"] if h.get("from_trace") and h["kind"] == "http_call"]
    assert done and done[0]["certainty"] == "observed" and done[0]["evidence"] == "static/app.js:11"
    assert any(g.get("completed_by_trace") for g in res["gaps"] if g["kind"] == "dynamic_url")
    assert any(h["kind"] == "route" and "read_quote" in h["label"] for h in res["hops"])


def test_path_params_match_as_patterns(repo):
    res = code_graph.business_route(repo, "GET /api/quotes/42")
    assert res["start_kind"] == "route"
    assert res["hops"][0]["kind"] == "route" and "read_quote" in res["hops"][0]["label"]


def test_unknown_start_is_a_gap(repo):
    res = code_graph.business_route(repo, "Botón que no existe")
    assert res["hops"] == [] and res["gaps"][0]["kind"] == "start"


def test_end_not_reached_is_reported(repo):
    res = code_graph.business_route(repo, "Generar presupuesto", "table invoices")
    assert res["reached_end"] is False and any(g["kind"] == "end_not_reached" for g in res["gaps"])


def test_jsx_axios_express_and_orm(tmp_path):
    ce_store.use_path(str(tmp_path / "ce2.db"))
    root = str(tmp_path / "repo2")
    _write(root, "web/Form.jsx", '''import axios from "axios";

export function Form({ id }) {
  async function onSave() {
    await axios.put(`/items/${id}`, { name: "x" });
  }
  return (
    <div>
      <button className="primary" onClick={onSave}>Guardar ficha</button>
    </div>
  );
}
''')
    _write(root, "server/index.js", '''const express = require("express");
const fs = require("fs");
const app = express();

function persist(item) {
  fs.writeFileSync("items.json", JSON.stringify(item));
}

app.put("/items/:id", (req, res) => {
  persist(req.body);
  res.json({ ok: true });
});
''')
    tok1 = te._active_workspace.set(root)
    tok2 = te._active_workspace_roots.set((root,))
    try:
        res = code_graph.business_route(root, "Guardar ficha", "saved")
    finally:
        te._active_workspace.reset(tok1)
        te._active_workspace_roots.reset(tok2)
        ce_store.use_path(None)
    kinds = [h["kind"] for h in res["hops"]]
    assert kinds[:4] == ["ui_event", "handler", "http_call", "route"], res["output"]
    assert res["hops"][2]["label"] == "PUT /items/{id}"
    assert any(h["kind"] == "file_write" for h in res["hops"]) and res["reached_end"] is True
