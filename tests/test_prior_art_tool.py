"""tests/test_prior_art_tool.py — the `prior_art` agent tool executor.

`PriorArtTool.execute(content, ctx)` is exercised directly, the same shape
`src/tool_execution.py` calls every `TOOL_HANDLERS` entry with.
`src.prior_art` itself is monkeypatched here (its own network behaviour is
covered by `tests/test_prior_art.py`), so this file is only about argument
parsing and dispatch.
"""
from __future__ import annotations

import asyncio

import pytest

from src.agent_tools.prior_art_tools import PriorArtTool

tool = PriorArtTool()


def _run(content, ctx=None):
    return asyncio.run(tool.execute(content, ctx or {}))


def test_bare_string_dispatches_to_rubric(monkeypatch):
    captured = {}

    def fake_rubric(idea, **kw):
        captured["idea"] = idea
        captured["kw"] = kw
        return {"idea": idea, "instructions": "do the thing", "slate_shape": {}}

    monkeypatch.setattr("src.prior_art.rubric", fake_rubric)
    out = _run("a markdown to pdf converter")
    assert out["exit_code"] == 0
    assert out["output"] == "do the thing"
    assert captured["idea"] == "a markdown to pdf converter"


def test_rubric_json_form_forwards_all_fields(monkeypatch):
    captured = {}

    def fake_rubric(idea, **kw):
        captured.update(idea=idea, **kw)
        return {"idea": idea, "instructions": "x", "slate_shape": {}}

    monkeypatch.setattr("src.prior_art.rubric", fake_rubric)
    out = _run({"action": "rubric", "idea": "thing", "stack": "python",
               "license": "MIT", "constraints": "offline"})
    assert out["exit_code"] == 0
    assert captured == {"idea": "thing", "stack": "python", "license": "MIT",
                        "constraints": "offline"}


def test_rubric_without_idea_is_an_error():
    out = _run({"action": "rubric"})
    assert out["exit_code"] == 1
    assert "idea" in out["error"]


def test_verify_forwards_slate_and_owner_context(monkeypatch):
    captured = {}

    def fake_verify(slate, **kw):
        captured["slate"] = slate
        captured["kw"] = kw
        return {"verified": True, "table": "| a | b |", "components": [], "exit_code": 0}

    monkeypatch.setattr("src.prior_art.verify", fake_verify)
    slate = {"idea": "x", "components": [{"name": "y", "verdict": "reuse", "repos": ["a/b"]}]}
    out = _run({"action": "verify", "slate": slate, "target_license": "MIT"},
              ctx={"owner": "ada", "project_id": "proj1"})
    assert out["exit_code"] == 0
    assert out["output"] == "| a | b |"
    assert captured["slate"] == slate
    assert captured["kw"]["target_license"] == "MIT"
    assert captured["kw"]["owner"] == "ada"
    assert captured["kw"]["project_id"] == "proj1"


def test_verify_tolerates_top_level_components_without_slate_wrapper(monkeypatch):
    captured = {}

    def fake_verify(slate, **kw):
        captured["slate"] = slate
        return {"verified": True, "table": "", "components": [], "exit_code": 0}

    monkeypatch.setattr("src.prior_art.verify", fake_verify)
    out = _run({"action": "verify", "idea": "x", "components": [{"name": "y", "verdict": "write"}]})
    assert out["exit_code"] == 0
    assert captured["slate"]["idea"] == "x"
    assert captured["slate"]["components"] == [{"name": "y", "verdict": "write"}]


def test_search_requires_query():
    out = _run({"action": "search"})
    assert out["exit_code"] == 1
    assert "query" in out["error"]


def test_search_forwards_args(monkeypatch):
    captured = {}

    def fake_search(query, **kw):
        captured["query"] = query
        captured["kw"] = kw
        return {"verified": True, "results": [{"full_name": "psf/requests"}], "exit_code": 0}

    monkeypatch.setattr("src.prior_art.search", fake_search)
    out = _run({"action": "search", "query": "http client", "language": "python", "limit": 5})
    assert out["exit_code"] == 0
    assert "1 result(s)" in out["output"]
    assert captured["query"] == "http client"
    assert captured["kw"] == {"language": "python", "limit": 5, "include_stale": False}


def test_report_by_id_found_and_missing(monkeypatch):
    def fake_report(report_id):
        if report_id == "PA-000001":
            return {"id": "PA-000001", "results": {"table": "| x |"}}
        return None

    monkeypatch.setattr("src.prior_art.report", fake_report)
    found = _run({"action": "report", "id": "PA-000001"})
    assert found["exit_code"] == 0
    assert found["output"] == "| x |"

    missing = _run({"action": "report", "id": "PA-999999"})
    assert missing["exit_code"] == 1
    assert "no such report" in missing["error"]


def test_report_listing_uses_owner_from_context(monkeypatch):
    captured = {}

    def fake_reports(limit, owner=""):
        captured["limit"] = limit
        captured["owner"] = owner
        return [{"id": "PA-000001"}]

    monkeypatch.setattr("src.prior_art.reports", fake_reports)
    out = _run({"action": "report", "limit": 5}, ctx={"owner": "ada"})
    assert out["exit_code"] == 0
    assert captured == {"limit": 5, "owner": "ada"}
    assert out["reports"] == [{"id": "PA-000001"}]


def test_unknown_action_is_an_error():
    out = _run({"action": "fly"})
    assert out["exit_code"] == 1
    assert "unknown action" in out["error"]


def test_a_tool_exception_never_raises_out_of_execute(monkeypatch):
    def boom(idea, **kw):
        raise RuntimeError("kaboom")

    monkeypatch.setattr("src.prior_art.rubric", boom)
    out = _run({"action": "rubric", "idea": "x"})
    assert out["exit_code"] == 1
    assert "kaboom" in out["error"]
