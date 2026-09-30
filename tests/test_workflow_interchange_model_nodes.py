"""The model-driven node types through import and export.

Pinned here: the canonical envelope round-trips a definition that uses every new
node type (and its declared inputs) without changing its fingerprint; a foreign
graph's Agent / LLM / Evaluator / Loop become real nodes only when the payload
carries what the node needs and stay `design_only` (with the reason) when it does
not; a loop that cannot be translated drags its body with it instead of leaving
the body to run once; nodes that wait on a loop's body wait on the loop; and the
types that still have no node stay `design_only`.
"""
from __future__ import annotations

import pytest

from src.contracts import WorkflowDefinition
from src.workflows.interchange import export_canonical, import_external

SCHEMA = {"type": "object", "required": ["n"], "properties": {"n": {"type": "integer"}}}

FULL = {
    "id": "model.flow", "version": "1.2.0", "title": "Model flow", "description": "Every model node.",
    "inputs": {"type": "object", "required": ["text"], "properties": {"text": {"type": "string"}}},
    "nodes": [
        {"id": "start", "type": "manual"},
        {"id": "pick", "type": "classify", "needs": ["start"],
         "config": {"text": "{{ inputs.text }}", "labels": ["a", "b"], "threshold": 0.6}},
        {"id": "pull", "type": "extract", "needs": ["start"], "config": {"text": "{{ inputs.text }}", "schema": SCHEMA}},
        {"id": "think", "type": "agent", "needs": ["pick"], "branch": {"pick": "a"},
         "config": {"prompt": "Answer {{ inputs.text }}", "tools": ["read_file"], "output_schema": SCHEMA, "max_rounds": 4}},
        {"id": "safe", "type": "guard", "needs": ["think"],
         "config": {"text": "{{ results.think.text }}", "checks": ["secrets", {"type": "urls", "deny": ["bad.example"]}]}},
        {"id": "lp", "type": "loop", "needs": ["start"],
         "config": {"body": ["draft", "check"], "budget": {"max_iterations": 3, "on_exhausted": "fail"},
                    "until": {"left": {"path": "loop.iteration"}, "op": "gte", "right": 2}}},
        {"id": "draft", "type": "agent", "needs": ["start"], "config": {"prompt": "Draft {{ loop.iteration }}"}},
        {"id": "check", "type": "guard", "needs": ["draft"],
         "config": {"text": "{{ results.draft.text }}", "checks": ["pii"]}},
        {"id": "done", "type": "artifact_store", "needs": ["lp", "safe", "pull"], "config": {"kind": "report"}},
    ],
}


def test_the_canonical_envelope_round_trips_every_new_node_type():
    wf = WorkflowDefinition.parse(FULL)
    envelope = export_canonical(wf, layout={"think": {"x": 1, "y": 2}})
    back = import_external(envelope)
    assert back["executable"] is True and back["design_only"] == [] and back["rejected"] == []
    again = WorkflowDefinition.parse(back["definition"])
    assert again.fingerprint() == wf.fingerprint() and again.to_dict() == wf.to_dict()
    assert {n.type for n in again.nodes} >= {"agent", "classify", "extract", "guard", "loop"}
    assert again.inputs == wf.inputs, "declared inputs survive the trip"
    assert again.node("pick").config["labels"] == ["a", "b"] and again.node("think").branch == {"pick": ("a",)}


def test_a_definition_without_inputs_exports_without_the_key():
    wf = WorkflowDefinition.parse({"id": "plain", "version": "1.0.0", "title": "P", "nodes": [{"id": "s", "type": "manual"}]})
    assert "inputs" not in export_canonical(wf)["definition"]


def graph(nodes, edges=()):
    return {"schemaVersion": 1, "nodes": nodes, "edges": [{"source": a, "target": b} for a, b in edges]}


def test_an_agent_with_a_prompt_becomes_an_agent_node_and_an_llm_a_tool_less_one():
    out = import_external(graph(
        [{"id": "s", "type": "Start"},
         {"id": "a", "type": "Agent", "data": {"label": "Research", "prompt": "Find {{ inputs.topic }}", "agent": "researcher",
                                               "tools": ["web_search"], "output_schema": SCHEMA, "max_rounds": 5}},
         {"id": "l", "type": "LLM", "data": {"prompt": "Summarise", "tools": ["bash"]}}],
        [("s", "a"), ("a", "l")]))
    assert out["executable"] is True and out["design_only"] == []
    nodes = {n["id"]: n for n in out["definition"]["nodes"]}
    assert nodes["a"]["type"] == "agent" and nodes["a"]["config"] == {
        "prompt": "Find {{ inputs.topic }}", "agent": "researcher", "tools": ["web_search"],
        "output_schema": SCHEMA, "max_rounds": 5}
    assert nodes["l"]["config"] == {"prompt": "Summarise", "tools": []}, "an LLM step never gets tools"
    assert nodes["l"]["needs"] == ["a"]
    WorkflowDefinition.parse(out["definition"])


@pytest.mark.parametrize("node,needle", [
    ({"id": "a", "type": "Agent", "data": {"label": "no prompt"}}, "no `prompt`"),
    ({"id": "a", "type": "LLM", "data": {"prompt": "   "}}, "no `prompt`"),
    ({"id": "a", "type": "Evaluator", "data": {"text": "x"}}, "no `checks`"),
    ({"id": "a", "type": "Evaluator", "data": {"text": "x", "checks": ["vibes"]}}, "no `checks`"),
    ({"id": "a", "type": "Evaluator", "data": {"checks": ["secrets"]}}, "no `text`"),
    ({"id": "a", "type": "Loop", "data": {"body": ["x"]}}, "positive `max_iterations`"),
    ({"id": "a", "type": "Loop", "data": {"max_iterations": 3}}, "no `body`"),
    ({"id": "a", "type": "Loop", "data": {"body": ["x"], "max_iterations": 0}}, "positive `max_iterations`"),
    ({"id": "a", "type": "Loop", "data": {"body": ["x"], "max_iterations": 2, "until": {"op": "x"}}}, "single left/op/right"),
])
def test_a_node_missing_what_it_needs_stays_design_only_with_the_reason(node, needle):
    out = import_external(graph([{"id": "s", "type": "Start"}, node], [("s", "a")]))
    entry = next(r for r in out["design_only"] if r["id"] == "a")
    assert needle in entry["reason"]
    assert [n["id"] for n in out["definition"]["nodes"]] == ["s"]


def test_an_evaluator_with_checks_becomes_a_guard():
    out = import_external(graph([{"id": "s", "type": "Start"},
                                 {"id": "g", "type": "Evaluator", "data": {"text": "{{ inputs.t }}", "checks": ["secrets", "pii"]}}],
                                [("s", "g")]))
    node = next(n for n in out["definition"]["nodes"] if n["id"] == "g")
    assert node["type"] == "guard" and node["config"] == {"text": "{{ inputs.t }}", "checks": ["secrets", "pii"]}


def loop_graph(**loop_data):
    return graph(
        [{"id": "s", "type": "Start"},
         {"id": "lp", "type": "Loop", "data": {"body": ["w", "c"], "max_iterations": 3,
                                               "until": {"left": {"path": "loop.iteration"}, "op": "gte", "right": 2},
                                               **loop_data}},
         {"id": "w", "type": "Agent", "data": {"prompt": "Write"}},
         {"id": "c", "type": "Tool", "data": {"tool": "lint"}},
         {"id": "o", "type": "Output", "data": {"label": "Report"}}],
        [("s", "lp"), ("lp", "w"), ("w", "c"), ("c", "o")])


def test_a_loop_becomes_a_loop_node_whose_body_waits_on_what_the_loop_waits_for():
    out = import_external(loop_graph())
    assert out["executable"] is True and out["design_only"] == []
    nodes = {n["id"]: n for n in out["definition"]["nodes"]}
    assert nodes["lp"]["type"] == "loop" and nodes["lp"]["needs"] == ["s"]
    assert nodes["lp"]["config"]["body"] == ["w", "c"] and nodes["lp"]["config"]["budget"] == {"max_iterations": 3}
    assert nodes["lp"]["config"]["until"]["op"] == "gte"
    assert nodes["w"]["needs"] == ["s"] and nodes["c"]["needs"] == ["s", "w"]
    assert nodes["o"]["needs"] == ["lp"], "what waited on the body waits on the loop"
    WorkflowDefinition.parse(out["definition"])


def test_a_loop_that_cannot_be_translated_takes_its_body_and_what_follows_with_it():
    out = import_external(loop_graph(max_iterations=0))
    ids = {r["id"]: r for r in out["design_only"]}
    assert set(ids) == {"lp", "w", "c", "o"}
    assert "belongs to the loop lp" in ids["w"]["reason"] and "belongs to the loop lp" in ids["c"]["reason"]
    assert "positive `max_iterations`" in ids["lp"]["reason"]
    assert [n["id"] for n in out["definition"]["nodes"]] == ["s"]


def test_a_loop_body_node_of_a_type_a_loop_cannot_hold_demotes_the_loop():
    g = loop_graph()
    g["nodes"].append({"id": "h", "type": "Human Approval", "data": {}})
    g["nodes"][1]["data"]["body"] = ["w", "h"]
    out = import_external(g)
    assert "human_approval" in next(r for r in out["design_only"] if r["id"] == "lp")["reason"]
    assert "w" in {r["id"] for r in out["design_only"]}, "no body node is left to run once"


def test_a_loop_naming_a_missing_or_shared_body_node_is_refused():
    out = import_external(loop_graph(body=["w", "ghost"]))
    assert "ghost" in next(r for r in out["design_only"] if r["id"] == "lp")["reason"]
    g = loop_graph()
    g["nodes"].append({"id": "lp2", "type": "Loop", "data": {"body": ["w"], "max_iterations": 2}})
    out = import_external(g)
    assert any("already belongs to another loop" in r["reason"] for r in out["design_only"])


def test_the_types_that_still_have_no_node_stay_design_only():
    out = import_external(graph([{"id": "s", "type": "Start"}] + [
        {"id": t.lower(), "type": t, "data": {"prompt": "x", "body": ["s"], "max_iterations": 2}}
        for t in ("Code", "RAG", "Memory", "Parallel", "Merge", "Retry")], []))
    assert {r["type"] for r in out["design_only"]} == {"Code", "RAG", "Memory", "Parallel", "Merge", "Retry"}
    assert all("no Faustus node type" in r["reason"] for r in out["design_only"])


def test_an_imported_model_graph_exports_and_imports_again_unchanged():
    first = import_external(loop_graph())["definition"]
    wf = WorkflowDefinition.parse(first)
    second = import_external(export_canonical(wf))
    assert WorkflowDefinition.parse(second["definition"]).fingerprint() == wf.fingerprint()


def test_every_definition_tool_reads_every_new_node_type(tmp_path, monkeypatch):
    """validate, plan, simulate, mermaid, estimate, preflight and the dry run all
    take a definition that uses agent, classify, extract, guard and loop, and
    none of them runs anything."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core import middleware
    import routes.workflows_routes as wr
    monkeypatch.setattr(middleware, "auth_disabled", lambda: True)
    app = FastAPI()
    app.include_router(wr.setup_workflows_routes())
    client = TestClient(app)
    body = {"definition": FULL}
    valid = client.post("/api/workflows/validate", json=body).json()
    assert valid["ok"] is True and valid["nodes"] == len(FULL["nodes"])
    plan = client.post("/api/workflows/plan", json=body).json()
    assert plan["ok"] is True and "start" in plan["starts_with"]
    sim = client.post("/api/workflows/simulate", json={**body, "choices": {"pick": "a", "safe": "pass"}}).json()
    assert sim["ok"] is True and "think" in sim["simulation"]["activated"] and "lp" in sim["simulation"]["loops"]
    mer = client.post("/api/workflows/mermaid", json=body).json()["mermaid"]
    assert "think" in mer and "lp" in mer
    assert client.post("/api/workflows/estimate", json=body).status_code == 200
    assert client.post("/api/workflows/preflight", json=body).status_code == 200
    dry = client.post("/api/workflows/dry-run", json={**body, "inputs": {"text": "hello"}}).json()["result"]
    assert dry["status"] == "completed" and dry["nodes"]["lp"]["result"]["iterations"] == 2
    assert dry["nodes"]["done"]["status"] == "completed" and dry["outputs"]["done"]["simulated"] is True
    exported = client.post("/api/workflows/export", json=body).json()["export"]
    imported = client.post("/api/workflows/import", json=exported).json()
    assert imported["ok"] is True and imported["executable"] is True and imported["design_only"] == []
