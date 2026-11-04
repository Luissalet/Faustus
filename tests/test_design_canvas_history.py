"""Darwin #989: design canvas versions, semantic diff and decision log.

Run: python -m pytest tests/test_design_canvas_history.py -q -p no:cacheprovider
"""
from __future__ import annotations

import asyncio
import copy
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import design_canvas_history as history

APPROACH = ("Keep one JSON file per canvas next to the other stores and append a version on every filing; "
            "a database table was rejected because nothing else in the module needs queries.")
BASE = {
    "requirements": ["Every filing of a canvas becomes a numbered version",
                     "Two versions can be compared element by element"],
    "entities": ["CanvasVersion with number, author, goal and the canvas itself"],
    "approach": APPROACH,
    "structure": ["src/design_canvas_history.py holds versions and the diff",
                  "routes/design_canvas_routes.py serves the history over REST"],
    "operations": ["record_version(canvas_id, canvas) returns the new version"],
    "norms": ["Stores live under the data folder like the other JSON stores"],
    "safeguards": ["A corrupt file reads as missing instead of failing the filing",
                   "An identical filing does not create an empty version"],
}


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("FAUSTUS_DESIGN_CANVAS_DIR", str(tmp_path / "canvases"))


def _v2():
    c = copy.deepcopy(BASE)
    c["requirements"].append("Decisions record who decided what and why on a version")      # added
    c["safeguards"].remove("An identical filing does not create an empty version")           # removed
    c["entities"][0] = "CanvasVersion with its number, author, goal and the whole canvas"    # changed (reworded)
    moved = c["norms"].pop(0)                                                                 # moved norms -> safeguards
    c["norms"].append("Routes are registered in app.py next to their neighbours")
    c["safeguards"].append(moved)
    c["structure"].reverse()                                                                  # reordered
    c["approach"] = APPROACH + " The diff is computed when asked, never stored."
    return c


def test_semantic_diff_names_each_kind_of_change():
    d = history.semantic_diff(BASE, _v2())
    kinds = {(c["kind"], c.get("dimension") or c.get("to_dimension")) for c in d["changes"]}
    assert ("added", "requirements") in kinds
    assert ("removed", "safeguards") in kinds
    assert ("changed", "entities") in kinds
    assert ("moved", "safeguards") in kinds
    assert ("reordered", "structure") in kinds
    approach = next(c for c in d["changes"] if c.get("dimension") == "approach")
    assert approach["sentences_added"] == ["The diff is computed when asked, never stored."]
    moved = next(c for c in d["changes"] if c["kind"] == "moved")
    assert moved["from_dimension"] == "norms"
    assert d["counts"]["moved"] == 1 and not d["identical"]
    assert history.semantic_diff(BASE, copy.deepcopy(BASE))["identical"]


def test_versions_decisions_and_diff_with_the_why():
    v1 = history.record_version("c1", BASE, goal="history", name="Canvas history", author="agent")
    assert v1["version"] == 1
    assert history.record_version("c1", copy.deepcopy(BASE))["unchanged"] is True
    with pytest.raises(history.CanvasHistoryError):
        history.diff("c1")
    v2 = history.record_version("c1", _v2(), author="luis", source="user", note="tightened")
    assert v2["version"] == 2
    entry = history.add_decision("c1", "Drop the empty-version guard from safeguards", why="record_version enforces it")
    assert entry["version"] == 2 and entry["author"] == "user"
    d = history.diff("c1")
    assert d["from"]["version"] == 1 and d["to"]["version"] == 2
    assert [x["decision"] for x in d["decisions"]] == ["Drop the empty-version guard from safeguards"]
    text = history.render_diff(d)
    assert "v1 -> v2" in text and "moved Norms -> Safeguards" in text and "why: record_version enforces it" in text
    rows = history.list_canvases()
    assert rows[0]["id"] == "c1" and rows[0]["latest"] == 2 and rows[0]["decisions"] == 1
    h = history.history("c1")
    assert [v["version"] for v in h["versions"]] == [1, 2] and "canvas" not in h["versions"][0]
    with pytest.raises(history.CanvasHistoryError):
        history.add_decision("c1", "  ")
    with pytest.raises(history.CanvasHistoryError):
        history.get("../escape")


def test_filing_a_canvas_records_a_version(monkeypatch):
    from src import design_canvas_pass
    import src.project_concepts as pc

    class Store:
        def upsert_concept(self, **kw):
            return {"id": kw.get("concept_id") or "concept-7", "name": kw["name"]}
    monkeypatch.setattr(pc, "_store", lambda **kw: Store())
    first = design_canvas_pass.file_as_concept(BASE, "history", name="Canvas history", project_id="p1")
    again = design_canvas_pass.file_as_concept(_v2(), "history", name="Canvas history", project_id="p1",
                                               concept_id="concept-7", author="qwen")
    assert first["canvas_version"] == 1 and again["canvas_version"] == 2
    assert history.history("concept-7")["versions"][1]["author"] == "qwen"
    assert history.list_canvases(project_id="p1")[0]["id"] == "concept-7"


def test_agent_tool_actions():
    from src.agent_tools.design_canvas_tools import DesignCanvasTool
    history.record_version("c2", BASE)
    history.record_version("c2", _v2())
    run = lambda payload: asyncio.run(DesignCanvasTool().execute(json.dumps(payload), {"project_id": ""}))
    assert "c2" in run({"goal": "x", "action": "list"})["output"]
    out = run({"goal": "x", "action": "diff", "concept_id": "c2"})
    assert out["exit_code"] == 0 and out["diff"]["counts"]["added"] >= 1
    logged = run({"goal": "Keep JSON files", "action": "decide", "concept_id": "c2", "why": "no queries", "version": 1})
    assert logged["decision"]["version"] == 1 and logged["decision"]["decision"] == "Keep JSON files"
    hist = run({"goal": "x", "action": "history", "concept_id": "c2"})
    assert "decision v1" in hist["output"] and "v2" in hist["output"]
    assert run({"goal": "x", "action": "diff"})["exit_code"] == 1
    assert run({"goal": "x", "action": "nope", "concept_id": "c2"})["exit_code"] == 1
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    schema = next(t for t in FUNCTION_TOOL_SCHEMAS if t["function"]["name"] == "design_canvas")
    props = schema["function"]["parameters"]["properties"]
    assert props["action"]["enum"] == ["draft", "list", "history", "diff", "decide"]
    assert {"from_version", "to_version", "decision", "why", "version"} <= set(props)


def test_rest_routes(monkeypatch):
    import routes.design_canvas_routes as routes
    monkeypatch.setattr(routes, "require_user", lambda request: "")
    app = FastAPI()
    app.include_router(routes.setup_design_canvas_routes())
    c = TestClient(app)
    history.record_version("c3", BASE, name="REST canvas")
    assert c.get("/api/design-canvas").json()["canvases"][0]["id"] == "c3"
    assert c.post("/api/design-canvas/c3/versions", json={"canvas": _v2(), "note": "edited"}).json()["version"] == 2
    assert c.post("/api/design-canvas/c3/versions", json={"canvas": {"requirements": []}}).status_code == 400
    diff = c.get("/api/design-canvas/c3/diff?from=1&to=2").json()
    assert diff["counts"]["reordered"] == 1 and "v1 -> v2" in diff["text"]
    assert c.post("/api/design-canvas/c3/decisions", json={"decision": "Ship it", "why": "tests pass", "author": "luis"}).json()["version"] == 2
    assert c.get("/api/design-canvas/c3/decisions?version=2").json()["decisions"][0]["author"] == "luis"
    assert c.get("/api/design-canvas/c3").json()["versions"][1]["note"] == "edited"
    assert "## Requirements" in c.get("/api/design-canvas/c3/versions/1").json()["markdown"]
    assert c.get("/api/design-canvas/missing").status_code == 404
    assert c.get("/api/design-canvas/c3/diff?from=x").status_code == 400


def test_mcp_tools(monkeypatch):
    from tests.test_dispatch import _load_workers_server
    ws = _load_workers_server(monkeypatch)
    assert {"design_canvas_history", "design_canvas_diff", "design_canvas_decide"} <= {t.name for t in ws.TOOLS}
    seen = []

    def fake(method, path, body=None, *a, **kw):
        seen.append((method, path, body))
        if path.endswith("/diff?from=1&to=2"):
            return {"text": "Canvas: v1 -> v2 · 1 added"}
        if path.endswith("/decisions"):
            return {"version": 2, "decision": body["decision"], "why": body.get("why", "")}
        return {"canvases": [{"id": "c9", "name": "N", "latest": 2, "decisions": 1}]}
    monkeypatch.setattr(ws, "_request", fake)
    assert "c9 · N · v2" in asyncio.run(ws.call_tool("design_canvas_history", {}))[0].text
    assert "1 added" in asyncio.run(ws.call_tool("design_canvas_diff", {"canvas_id": "c9", "from_version": 1, "to_version": 2}))[0].text
    out = asyncio.run(ws.call_tool("design_canvas_decide", {"canvas_id": "c9", "decision": "Ship", "why": "ok"}))[0].text
    assert out == "logged on v2: Ship — why: ok" and seen[-1][2]["author"] == "agent"
