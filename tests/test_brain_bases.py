"""Obsidian Bases over the Cerebro (src/brain/bases.py): the expression language, filters, views,
grouping, sorting and summaries against a real disposable vault, validation, files, and the
chat / HTTP / MCP surfaces."""
from __future__ import annotations

import asyncio
import json
import os

import pytest

from src.brain import bases as B, db as brain_db, notes, vault

OWNER = "ada"


@pytest.fixture(autouse=True)
def isolated(tmp_path):
    brain_db.use_dir(str(tmp_path / "brain"))
    yield
    brain_db.use_dir(None)


def _seed():
    notes.create_note(OWNER, "Dune", folder="Books", content="---\nstatus: reading\npages: 600\nauthor: Herbert\ndue: 2030-01-10\n---\n"
                      "Science fiction. #book #scifi/classic")
    notes.create_note(OWNER, "Emma", folder="Books", content="---\nstatus: done\npages: 400\nauthor: Austen\ndone: true\n---\n#book")
    notes.create_note(OWNER, "Idea", folder="Notes", content="---\nstatus: active\npriority: 1\n---\nSee [[Dune]]. #task")


BASE = """
filters:
  or:
    - file.hasTag("book")
    - file.hasTag("task")
formulas:
  icon: 'if(status == "reading", "R", if(status == "done", "D", "-"))'
  days_left: 'if(due, (date(due) - today()).days > 0, "")'
properties:
  author:
    displayName: Autor
views:
  - type: table
    name: Libros
    filters:
      and:
        - file.hasTag("book")
    order: [file.name, author, pages, formula.icon]
    sort:
      - property: pages
        direction: DESC
    summaries:
      pages: Sum
  - type: cards
    name: Todo
    groupBy:
      property: status
      direction: ASC
    order: [file.name, status]
"""


def _eval(expr, path="Books/Dune.md", formulas=None):
    store = B._Store(OWNER).load()
    return B.evaluate(B.parse(expr), B._Scope(store, path, formulas or {}, "Bases/x.base"))


def test_expressions_over_real_notes():
    _seed()
    cases = {
        'status == "reading"': True, 'file.hasTag("scifi")': True, 'file.hasTag("book") && pages > 500': True,
        'file.inFolder("Books")': True, 'file.basename': "Dune", 'file.ext': "md", 'file.backlinks.length': 1,
        '"a,b".split(",").map(value.upper()).join("-")': "A-B", '[1, 2, 3].filter(value > 1).reduce(acc + value, 0)': 5,
        '(date("2026-01-05") - date("2026-01-01")).days': 4, 'date("2026-01-05").format("dddd D MMMM YYYY")': "Monday 5 January 2026",
        '/^Du/.matches(file.basename)': True, 'note["author"].lower()': "herbert", 'max(1, pages, 3)': 600,
        '!file.hasProperty("missing")': True, 'this.name': "x.base", 'price * quantity': None,
    }
    for expr, want in cases.items():
        got = _eval(expr)
        assert (B.display(got) if isinstance(want, str) and not isinstance(got, str) else got) == want, expr
    assert B.display(_eval('date("2026-01-01") + "1w"')) == "2026-01-08"
    assert _eval('file.hasLink("Dune")', path="Notes/Idea.md") is True
    assert _eval("formula.a", formulas={"a": "formula.b + 1", "b": "2"}) == 3
    with pytest.raises(B.BaseError):
        _eval("formula.a", formulas={"a": "formula.a"})
    for bad in ("a ==", "(1", "@x", '"open'):
        with pytest.raises(B.BaseError):
            B.parse(bad)


def test_query_views_groups_sorting_and_summaries():
    _seed()
    out = B.query(OWNER, yaml_text=BASE)
    assert out["matched"] == 3 and out["errors"] == []
    libros, todo = out["views"]
    assert [r["title"] for r in libros["rows"]] == ["Dune", "Emma"]
    assert [c["label"] for c in libros["columns"]] == ["file.name", "Autor", "pages", "icon"]
    assert libros["rows"][0]["cells"]["formula.icon"] == "R" and libros["summaries"]["pages"]["value"] == "1000"
    assert [g["label"] for g in todo["groups"]] == ["active", "done", "reading"]
    only = B.query(OWNER, yaml_text=BASE, view="Todo")
    assert [v["name"] for v in only["views"]] == ["Todo"]
    with pytest.raises(B.BaseError):
        B.query(OWNER, yaml_text=BASE, view="Nope")
    text = B.render_text(out)
    assert "| Dune.md | Herbert | 600 | R |" in text and "Sum of pages: 1000" in text


def test_validation_reports_every_kind_of_mistake():
    bad = "filters:\n  and: ['a ==']\n  or: []\nviews:\n  - type: grid\n    order: [formula.zz]\n    summaries: {x: Mean}\n"
    v = B.validate(bad)
    joined = " | ".join(v["errors"])
    assert not v["ok"] and "exactly one key" in joined and "grid" in joined and "formula.zz" in joined and "Mean" in joined
    assert not B.validate("views: [")["ok"]
    assert B.validate(B.yaml_from_spec({"tags": ["book"], "columns": ["file.name", "author"], "group_by": "status"}))["ok"]


def test_save_list_read_and_delete_files():
    _seed()
    saved = B.save_base(OWNER, "Libros", spec={"tags": ["book"], "folder": "Books", "columns": ["file.name", "author", "status"],
                                                "sort_by": "pages", "sort_direction": "desc", "summaries": {"pages": "Sum"}})
    assert saved["path"] == "Bases/Libros.base" and saved["created"]
    assert os.path.isfile(os.path.join(vault.vault_root(OWNER), "Bases", "Libros.base"))
    listed = B.list_bases(OWNER)
    assert listed[0]["path"] == "Bases/Libros.base" and listed[0]["ok"] and listed[0]["views"] == ["Tabla"]
    rows = B.query(OWNER, path="Libros")["views"][0]["rows"]
    assert [r["title"] for r in rows] == ["Dune", "Emma"]
    with pytest.raises(B.BaseError):
        B.save_base(OWNER, "Broken", yaml_text="views:\n  - type: nope\n")
    with pytest.raises(B.BaseError):
        B.save_base(OWNER, "Libros", yaml_text=BASE, overwrite=False)
    with pytest.raises(vault.VaultPathError):
        B.save_base(OWNER, "../escape", yaml_text=BASE)
    assert B.delete_base(OWNER, "Bases/Libros.base") == {"deleted": "Bases/Libros.base"}
    assert B.run(OWNER, {"action": "get", "path": "Libros"})["error_class"] == "brain_base.not_found"


def test_chat_tool_routes_and_mcp(monkeypatch):
    _seed()
    from src.agent_tools import TOOL_HANDLERS, TOOL_TAGS
    from src import tool_schemas, tool_index
    from src.tool_capabilities import TOOL_CAPABILITIES
    assert "brain_base" in TOOL_HANDLERS and "brain_base" in TOOL_TAGS and TOOL_CAPABILITIES["brain_base"].effects
    assert "brain_base" in {s["function"]["name"] for s in tool_schemas.FUNCTION_TOOL_SCHEMAS}
    assert tool_index.BUILTIN_TOOL_DESCRIPTIONS["brain_base"]
    ctx = {"owner": OWNER}
    out = asyncio.run(TOOL_HANDLERS["brain_base"](json.dumps({"action": "save", "path": "Tareas", "yaml": BASE}), ctx))
    assert out["exit_code"] == 0
    q = asyncio.run(TOOL_HANDLERS["brain_base"](json.dumps({"action": "query", "path": "Tareas", "view": "Libros"}), ctx))
    assert "Herbert" in q["output"]
    bad = asyncio.run(TOOL_HANDLERS["brain_base"](json.dumps({"action": "query"}), ctx))
    assert bad["exit_code"] == 1

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import routes.brain_routes as br
    monkeypatch.setattr(br, "effective_user", lambda request: OWNER)
    app = FastAPI()
    app.include_router(br.setup_brain_routes())
    app.dependency_overrides[br.require_user] = lambda: OWNER
    client = TestClient(app)
    assert client.get("/api/brain/bases").json()["bases"][0]["path"] == "Bases/Tareas.base"
    assert client.post("/api/brain/bases/query", json={"path": "Bases/Tareas.base"}).json()["matched"] == 3
    assert client.get("/api/brain/bases/file", params={"path": "Bases/Tareas.base"}).json()["validation"]["ok"]
    assert client.post("/api/brain/bases/validate", json={"yaml": "views: ["}).json()["ok"] is False
    assert client.put("/api/brain/bases/file", json={"path": "Otra", "spec": {"tags": ["task"]}}).json()["path"] == "Bases/Otra.base"
    assert client.put("/api/brain/bases/file", json={"path": "Mal", "yaml": "views: [{type: x}]"}).status_code == 400
    assert client.delete("/api/brain/bases/file", params={"path": "Bases/Otra.base"}).json()["deleted"] == "Bases/Otra.base"
    assert client.get("/api/brain/bases/file", params={"path": "Bases/Otra.base"}).status_code == 404

    pytest.importorskip("mcp")
    import mcp_servers.brain_server as srv
    assert "brain_base" in [t.name for t in asyncio.run(srv.list_tools())]
    monkeypatch.setattr(srv, "_initialized", srv._initialized)
    monkeypatch.setattr(srv, "_engine", dict(srv._engine))
    monkeypatch.setattr(srv, "_configured_owner", lambda: OWNER)
    res = asyncio.run(srv.call_tool("brain_base", {"action": "list"}))
    assert "Bases/Tareas.base" in res[0].text
