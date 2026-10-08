# -*- coding: utf-8 -*-
"""OBJ-47: the design canvas compared with the finished work.

The pure parts of `src/design_canvas_check.py`: the items a canvas yields,
the deterministic checks (files, tests), the judge's answer merged in, and the
overall verdict. The model is always a fake: what is under test is what the
module does with an answer, not the answer.
"""

import json

import pytest

from src import design_canvas_check as dcc

CANVAS = {
    "requirements": ["median is right for even-length lists", "no new dependency is added"],
    "entities": ["stats.median(values): a list of numbers in, a float out"],
    "approach": ("Average the two middle values of the sorted list; the rejected option was to "
                 "special-case length two, which does not generalise to larger even lists."),
    "structure": ["src/stats.py: the median function lives here", "tests/test_stats.py: its tests live here"],
    "operations": ["median(values) returns a float and raises ValueError on an empty list"],
    "norms": ["keep the existing function signature"],
    "safeguards": ["an empty list still raises ValueError", "odd-length lists behave as before"],
}


def _items():
    return dcc.build_items(CANVAS)


def test_items_cover_judged_dimensions_and_structure_paths():
    items = _items()
    kinds = [i["kind"] for i in items]
    assert kinds.count("requirements") == 2
    assert kinds.count("operations") == 1
    assert kinds.count("safeguards") == 2
    assert kinds.count("structure") == 2
    assert "norms" not in kinds and "entities" not in kinds
    assert len({i["id"] for i in items}) == len(items)


def test_structure_files_are_checked_without_a_model(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_stats.py").write_text("x = 1\n", encoding="utf-8")
    items = _items()
    dcc.check_structure(items, ["src/stats.py"], str(tmp_path))
    by_text = {i["text"]: i for i in items if i["kind"] == "structure"}
    assert by_text["src/stats.py"]["verdict"] == "met"
    # exists on disk but this work never touched it
    assert by_text["tests/test_stats.py"]["verdict"] == "partial"

    items = _items()
    dcc.check_structure(items, [], str(tmp_path))
    by_text = {i["text"]: i for i in items if i["kind"] == "structure"}
    assert by_text["src/stats.py"]["verdict"] == "not_met"
    assert "does not exist" in by_text["src/stats.py"]["evidence"]


def test_structure_without_a_workspace_is_unverified_not_failed():
    items = _items()
    dcc.check_structure(items, [], None)
    assert {i["verdict"] for i in items if i["kind"] == "structure"} == {"unverified"}


def test_structure_matches_windows_paths_and_case():
    items = _items()
    dcc.check_structure(items, ["D:\\proj\\SRC\\stats.py", "tests\\test_stats.py"], None)
    assert {i["verdict"] for i in items if i["kind"] == "structure"} == {"met"}


@pytest.mark.parametrize("tests,verdict", [
    (None, "unverified"),
    ({"ran": False}, "unverified"),
    ({"ran": True, "ok": True, "summary": "12 passed"}, "met"),
    ({"ran": True, "ok": False, "summary": "2 failed"}, "not_met"),
    ({"ran": True, "ok": False, "pre_existing_only": True}, "partial"),
    ({"ran": True, "ok": False, "inconclusive": True}, "unverified"),
])
def test_tests_item(tests, verdict):
    assert dcc.tests_item(tests)["verdict"] == verdict


def test_the_judges_answer_is_merged_per_id():
    items = [i for i in _items() if i["kind"] != "structure"]
    answer = json.dumps({"items": [
        {"id": "requirements.1", "verdict": "met", "evidence": "stats.py averages the two middle values"},
        {"id": "requirements.2", "verdict": "partial", "evidence": "requirements.txt untouched but numpy imported"},
        {"id": "operations.1", "verdict": "not_met", "evidence": "no ValueError for an empty list in the diff"},
    ]})
    dcc.apply_judgement(items, answer)
    got = {i["id"]: i["verdict"] for i in items}
    assert got["requirements.1"] == "met"
    assert got["requirements.2"] == "partial"
    assert got["operations.1"] == "not_met"
    # the two safeguards were never answered: unverified, not silently met
    assert got["safeguards.1"] == "unverified" and got["safeguards.2"] == "unverified"


def test_a_met_without_evidence_is_not_accepted():
    items = [i for i in _items() if i["id"] == "requirements.1"]
    dcc.apply_judgement(items, json.dumps({"items": [{"id": "requirements.1", "verdict": "met", "evidence": "ok"}]}))
    assert items[0]["verdict"] == "unverified"


def test_an_invented_verdict_is_unverified():
    items = [i for i in _items() if i["id"] == "requirements.1"]
    dcc.apply_judgement(items, json.dumps({"items": [{"id": "requirements.1", "verdict": "perfect", "evidence": "all of it"}]}))
    assert items[0]["verdict"] == "unverified"


def test_the_answer_may_arrive_wrapped_in_prose():
    items = [i for i in _items() if i["id"] == "requirements.1"]
    raw = 'Here you go: {"items": [{"id": "requirements.1", "verdict": "met", "evidence": "see stats.py diff"}]} done'
    dcc.apply_judgement(items, raw)
    assert items[0]["verdict"] == "met"


@pytest.mark.parametrize("verdicts,expected", [
    (["met", "met"], "met"),
    (["met", "partial"], "partial"),
    (["met", "not_met"], "partial"),
    (["not_met", "not_met"], "not_met"),
    (["unverified", "unverified"], "unverified"),
    (["met", "unverified"], "partial"),
])
def test_overall(verdicts, expected):
    items = [{"kind": "requirements", "verdict": v} for v in verdicts]
    assert dcc.overall(items) == expected


def test_a_missing_test_run_alone_does_not_make_a_met_canvas_partial():
    items = [{"kind": "requirements", "verdict": "met"}, {"kind": "tests", "verdict": "unverified"}]
    assert dcc.overall(items) == "met"


def _run(coro):
    import asyncio
    return asyncio.new_event_loop().run_until_complete(coro)


def _record(tmp_path, **extra):
    rec = {"session_id": "s1", "goal": "fix median", "canvas": CANVAS, "workspace": str(tmp_path),
           "touched": [], "concept_id": None}
    rec.update(extra)
    return rec


def test_check_runs_the_judge_once_and_reports_every_item(tmp_path):
    calls = []

    async def judge(messages, schema):
        calls.append((messages, schema))
        ids = [ln.split(" ")[1] for ln in messages[1]["content"].splitlines() if ln.startswith("- ")]
        return json.dumps({"items": [{"id": i, "verdict": "met", "evidence": f"diff shows {i} in src/stats.py"} for i in ids]})

    verdict = _run(dcc.check(_record(tmp_path), changed=["src/stats.py", "tests/test_stats.py"],
                             tests={"ran": True, "ok": True, "summary": "3 passed"},
                             diff="+ def median", model_call=judge))
    assert len(calls) == 1
    assert calls[0][1]["required"] == ["items"]
    assert verdict["overall"] == "met"
    assert verdict["counts"]["met"] == len(verdict["items"])
    assert verdict["judge_error"] is None
    assert any(i["kind"] == "tests" and i["verdict"] == "met" for i in verdict["items"])
    # the judge only sees what it has to judge: not the file lookups
    assert "structure." not in calls[0][0][1]["content"].split("Items to judge:")[1].split("Files changed")[0]


def test_a_judge_that_fails_leaves_unverified_items_and_says_why(tmp_path):
    async def judge(messages, schema):
        raise TimeoutError("no answer in 150 s")

    verdict = _run(dcc.check(_record(tmp_path), changed=["src/stats.py"], tests=None, diff="", model_call=judge))
    assert "TimeoutError" in verdict["judge_error"]
    judged = [i for i in verdict["items"] if i["kind"] in dcc.JUDGED_KINDS]
    assert judged and {i["verdict"] for i in judged} == {"unverified"}
    assert all("could not run" in i["evidence"] for i in judged)
    # the deterministic part still ran
    assert any(i["kind"] == "structure" and i["verdict"] == "met" for i in verdict["items"])


def test_an_empty_answer_is_a_failure_not_a_pass(tmp_path):
    async def judge(messages, schema):
        return "   "

    verdict = _run(dcc.check(_record(tmp_path), changed=["src/stats.py"], tests=None, diff="", model_call=judge))
    assert verdict["judge_error"]
    assert verdict["overall"] != "met"


def test_no_model_at_all_is_reported_the_same_way(tmp_path):
    verdict = _run(dcc.check(_record(tmp_path), changed=["src/stats.py"], tests=None, diff=""))
    assert verdict["judge_error"]
    assert {i["verdict"] for i in verdict["items"] if i["kind"] in dcc.JUDGED_KINDS} == {"unverified"}


def test_register_and_load_round_trip_and_a_new_canvas_replaces_the_old(tmp_path, monkeypatch):
    monkeypatch.setattr(dcc, "STORE_DIR", str(tmp_path / "store"))
    assert dcc.load("chat-1") is None
    assert dcc.register("chat-1", goal="first", canvas=CANVAS, concept_id="c1")
    assert dcc.load("chat-1")["goal"] == "first"
    assert dcc.register("chat-1", goal="second", canvas=CANVAS)
    assert dcc.load("chat-1")["goal"] == "second"
    assert dcc.load("chat-2") is None
    assert dcc.register("", goal="x", canvas=CANVAS) is False


def test_the_markdown_and_the_compact_form_are_in_the_turns_language(tmp_path):
    verdict = _run(dcc.check(_record(tmp_path), changed=["src/stats.py"], tests=None, diff="", language="es"))
    md = dcc.render_markdown(verdict, "es")
    assert "Comprobaci\u00f3n de cierre" in md
    rec = _record(tmp_path)
    rec["verdict"] = verdict
    out = dcc.compact(rec, "es")
    assert out["title"] == "Canvas de dise\u00f1o"
    assert out["items"][0]["verdict_label"] in ("cumplido", "cumplido en parte", "no cumplido", "sin verificar")
    assert "sin verificar" in out["label"] or "cumplidos" in out["label"]


REMOVAL_CANVAS = dict(CANVAS, structure=[
    "src/stats.py: the median function lives here",
    "src/legacy_stats.py: remove the duplicated implementation",
    "src/ui.py: add a delete button next to each row",
    "delete tests/old_stats_test.py, it is replaced",
])


def test_a_file_the_design_removes_is_met_when_it_is_gone(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "stats.py").write_text("x = 1\n", encoding="utf-8")
    items = dcc.build_items(REMOVAL_CANVAS)
    flags = {i["text"]: i["removal"] for i in items if i["kind"] == "structure"}
    assert flags == {"src/stats.py": False, "src/legacy_stats.py": True, "src/ui.py": False,
                     "tests/old_stats_test.py": True}
    dcc.check_structure(items, ["src/stats.py"], str(tmp_path))
    got = {i["text"]: i["verdict"] for i in items if i["kind"] == "structure"}
    assert got["src/legacy_stats.py"] == "met"          # absent, as designed
    assert got["tests/old_stats_test.py"] == "met"
    assert got["src/ui.py"] == "not_met"                 # a delete BUTTON is a file to create

    (tmp_path / "src" / "legacy_stats.py").write_text("x = 1\n", encoding="utf-8")
    items = dcc.build_items(REMOVAL_CANVAS)
    dcc.check_structure(items, ["src/stats.py"], str(tmp_path))
    got = {i["text"]: i for i in items if i["kind"] == "structure"}
    assert got["src/legacy_stats.py"]["verdict"] == "not_met"
    assert "still there" in got["src/legacy_stats.py"]["evidence"]
