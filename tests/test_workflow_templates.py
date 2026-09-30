"""Workflow templates: starting points with parameters.

Pinned here: every template fills in to a definition the contract accepts and
every tool reads; bad parameters are refused together and by name; the watch
template refuses a folder pair that would feed itself; the PDF template is wired
to the real `file_change` source, the `pdf_ops` tool and the artifact node; the
same templates dry-run end to end; and the routes fill, save and refuse.
"""
from __future__ import annotations

import pytest

from src.workflows import templates
from src.workflows.dry_run import dry_run
from src.workflows.handlers import EVENT_SOURCES

PDF = {"watch_dir": "/srv/inbox", "output_dir": "/srv/done"}


def test_the_list_describes_every_template_without_building_anything():
    listed = {t["id"]: t for t in templates.list_templates()}
    assert set(listed) == {"pdf-folder-batch", "triage-and-reply", "refine-until-good"}
    pdf = listed["pdf-folder-batch"]
    assert pdf["category"] == "data" and [p["name"] for p in pdf["parameters"]][:2] == ["watch_dir", "output_dir"]
    assert [n["type"] for n in pdf["nodes"]] == ["manual", "wait_for_event", "agent", "artifact_store"]
    assert pdf["inputs"]["required"] == ["operations"]


@pytest.mark.parametrize("template_id,params", [
    ("pdf-folder-batch", PDF), ("triage-and-reply", {}), ("refine-until-good", {}),
    ("refine-until-good", {"max_iterations": 7})])
def test_every_template_fills_in_to_a_valid_definition(template_id, params):
    wf = templates.instantiate(template_id, params)
    assert wf.nodes and "@@" not in repr(wf.to_dict())


def test_the_pdf_template_is_wired_to_real_parts():
    wf = templates.instantiate("pdf-folder-batch", {**PDF, "settle_seconds": 5, "timeout_minutes": 30})
    watch, convert, report = wf.node("watch"), wf.node("convert"), wf.node("report")
    assert watch.type == "wait_for_event" and watch.config["source"] == "file_change" and watch.config["source"] in EVENT_SOURCES
    assert watch.config["path"] == "/srv/inbox" and watch.config["pattern"] == "*.pdf"
    assert watch.config["settle_ms"] == 5000 and watch.config["timeout_ms"] == 1_800_000, "numbers stay numbers"
    assert convert.type == "agent" and convert.config["tools"] == ["pdf_ops"], "the only tool the turn gets"
    assert "/srv/done" in convert.config["prompt"] and "{{ results.watch.events | json }}" in convert.config["prompt"]
    assert report.type == "artifact_store" and report.needs == ("convert",) and watch.needs == ("start",)
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS  # the tool the agent is allowed exists
    assert any(t.get("function", t).get("name") == "pdf_ops" for t in FUNCTION_TOOL_SCHEMAS)


def test_the_pdf_template_dry_runs_with_the_tool_free_placeholders():
    wf = templates.instantiate("pdf-folder-batch", PDF)
    out = dry_run(wf, {"operations": "compress"})
    assert out["status"] == "completed" and out["nodes"]["convert"]["result"]["data"]["files"] != []
    assert out["outputs"]["report"]["simulated"] is True


def test_triage_routes_by_label_and_holds_a_failed_draft():
    wf = templates.instantiate("triage-and-reply", {"category_a": "sales", "category_b": "support"})
    assert wf.node("route").config["labels"] == ["sales", "support"] and wf.node("route").config["threshold"] == 0.7
    a = dry_run(wf, {"message": "price?"})
    assert a["nodes"]["draft_a"]["status"] == "completed" and a["nodes"]["draft_b"]["status"] == "skipped"
    assert a["nodes"]["keep_a"]["status"] == "completed" and a["nodes"]["hold_a"]["status"] == "skipped"
    b = dry_run(wf, {"message": "broken"}, mocks={"route": {"label": "support"}, "check_b": {"passed": False}})
    assert b["nodes"]["keep_b"]["status"] == "skipped" and b["nodes"]["hold_b"]["status"] == "completed"
    assert b["human_waits"] == ["hold_b"]


def test_refine_loops_until_the_guard_passes_and_keeps_the_last_draft():
    wf = templates.instantiate("refine-until-good", {"max_iterations": 3})
    out = dry_run(wf, {"brief": "a haiku"})
    assert out["status"] == "completed" and out["nodes"]["refine"]["result"]["iterations"] == 1
    assert out["nodes"]["keep"]["status"] == "completed"
    stuck = dry_run(wf, {"brief": "x"}, mocks={"verify": {"passed": False}})
    assert stuck["status"] == "paused" and stuck["nodes"]["keep"]["status"] == "skipped"
    assert stuck["nodes"]["refine"]["result"]["iterations"] == 3


@pytest.mark.parametrize("params,needle", [
    ({}, "watch_dir: is required"),
    ({"watch_dir": "inbox", "output_dir": "/o"}, "absolute folder path"),
    ({"watch_dir": "/a/../b", "output_dir": "/o"}, "'..'"),
    ({"watch_dir": "/a", "output_dir": "/a"}, "must not be the folder being watched"),
    ({"watch_dir": "/a", "output_dir": "/a/out"}, "must not contain each other"),
    ({"watch_dir": "C:\\In", "output_dir": "c:/in/Out"}, "must not contain each other"),
    ({**PDF, "pattern": "*.pdf;rm"}, "may only use"),
    ({**PDF, "settle_seconds": 0}, "between 1 and 3600"),
    ({**PDF, "settle_seconds": "soon"}, "must be a number"),
    ({**PDF, "settle_seconds": 1.5}, "whole number"),
    ({**PDF, "timeout_minutes": True}, "must be a number"),
    ({**PDF, "extra": 1}, "not a parameter"),
    ({"watch_dir": "/a@@b", "output_dir": "/o"}, "'@@'"),
])
def test_bad_parameters_are_refused_by_name(params, needle):
    with pytest.raises(templates.TemplateError) as err:
        templates.instantiate("pdf-folder-batch", params)
    assert needle in str(err.value) or needle in " ".join(err.value.problems)


def test_every_problem_is_reported_at_once_and_windows_paths_are_accepted():
    with pytest.raises(templates.TemplateError) as err:
        templates.instantiate("pdf-folder-batch", {"pattern": "*.pdf;x", "settle_seconds": 0})
    assert len(err.value.problems) >= 4
    wf = templates.instantiate("pdf-folder-batch", {"watch_dir": "D:\\Scans\\In", "output_dir": "D:\\Scans\\Out"})
    assert wf.node("watch").config["path"] == "D:\\Scans\\In"


def test_triage_categories_must_differ_and_be_plain():
    with pytest.raises(templates.TemplateError, match="must differ"):
        templates.instantiate("triage-and-reply", {"category_a": "Sales", "category_b": "sales"})
    with pytest.raises(templates.TemplateError, match="may only use"):
        templates.instantiate("triage-and-reply", {"category_a": "a;b"})
    with pytest.raises(templates.TemplateError, match="between 0 and 1"):
        templates.instantiate("triage-and-reply", {"threshold": 1.5})
    with pytest.raises(templates.TemplateError, match="no template named"):
        templates.instantiate("ghost")


def test_a_filled_in_template_can_be_saved_published_and_read_as_a_tool(store):
    from src.workflows import published
    from src.workflows.library import WorkflowLibrary
    wf = templates.instantiate("pdf-folder-batch", PDF)
    saved = WorkflowLibrary().save("ana", wf, enabled=True)
    assert saved["name"] == "pdf_folder_batch" and saved["enabled"] is True
    spec = published.tool_specs("ana")[0]
    assert spec["name"] == "wf_pdf_folder_batch" and spec["inputSchema"]["required"] == ["operations"]
    assert set(spec["inputSchema"]["properties"]["overrides"]["properties"]) == {"convert"}


from tests.test_workflow_published import client, store  # noqa: E402,F401  (fixtures)


def test_the_routes_list_fill_save_and_refuse(client):
    listed = client.get("/api/workflows/templates").json()["templates"]
    assert {t["id"] for t in listed} >= {"pdf-folder-batch"}
    body = {"parameters": PDF}
    filled = client.post("/api/workflows/templates/pdf-folder-batch/instantiate", json=body)
    assert filled.status_code == 200 and filled.json()["definition"]["id"] == "pdf.folder-batch"
    assert client.get("/api/workflows/library").json()["workflows"] == [], "filling in saves nothing"
    saved = client.post("/api/workflows/templates/pdf-folder-batch/instantiate", json={**body, "save": True, "name": "pdfs"})
    assert saved.json()["saved"]["name"] == "pdfs" and saved.json()["saved"]["enabled"] is False
    bad = client.post("/api/workflows/templates/pdf-folder-batch/instantiate", json={"parameters": {"watch_dir": "x"}})
    assert bad.status_code == 400 and bad.json()["detail"]["problems"]
    assert client.post("/api/workflows/templates/ghost/instantiate", json={}).status_code == 404
    assert client.post("/api/workflows/templates/pdf-folder-batch/instantiate", json={"parameters": []}).status_code == 400
    assert client.post("/api/workflows/templates/pdf-folder-batch/instantiate",
                       json={**body, "save": True, "name": "Bad Name"}).status_code == 400
