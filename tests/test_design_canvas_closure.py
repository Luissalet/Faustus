# -*- coding: utf-8 -*-
"""OBJ-47: the loop closes. A canvas drafted through the tool is compared
with the work at the end of the turn, and the verdict lands in the turn's
summary, the `harness_check` event and the canvas itself.

Fakes throughout: the model that drafts, the model that judges, the concept
store (a real one, in a temp folder).
"""

import json
import re

import numpy as np
import pytest

from src import agent_harness as h
from src import design_canvas, design_canvas_check as dcc
from src import project_concepts as pc
from src.agent_tools.design_canvas_tools import DesignCanvasTool

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


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(dcc, "STORE_DIR", str(tmp_path / "canvas_checks"))
    monkeypatch.setattr(pc, "_embed_texts", lambda texts: np.zeros((len(texts), 8), dtype="float32"))
    monkeypatch.setattr(pc, "_embed_one", lambda text: np.zeros(8, dtype="float32"))
    store = pc.Store("closure-test", path=str(tmp_path / "concepts.db"))
    monkeypatch.setattr(pc, "_store", lambda **kwargs: store)
    return store


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "tests").mkdir()
    (ws / "src" / "stats.py").write_text("def median(v):\n    return 0\n", encoding="utf-8")
    return ws


def _edit(ledger, path):
    ledger.record("edit_file", json.dumps({"path": path, "old_string": "a", "new_string": "b"}),
                  {"output": "ok", "exit_code": 0}, 1)


async def _draft_canvas(monkeypatch, ctx):
    async def _draft(goal, **kwargs):
        return {"canvas": CANVAS, "markdown": design_canvas.render(CANVAS),
                "paths": design_canvas.referenced_paths(CANVAS), "elapsed_ms": 1}

    import src.design_canvas_pass as dcp
    monkeypatch.setattr(dcp, "draft", _draft)
    return await DesignCanvasTool().execute('{"goal": "fix the median of even lists"}', ctx)


def _all_met(messages, schema):
    ids = re.findall(r"^- (\S+) \[", messages[1]["content"], re.MULTILINE)

    async def _go():
        return json.dumps({"items": [
            {"id": i, "verdict": "met", "evidence": f"the diff for src/stats.py covers {i}"} for i in ids]})
    return _go()


@pytest.mark.asyncio
async def test_drafting_a_canvas_arms_the_closure(monkeypatch, workspace):
    ctx = {"session_id": "chat-1", "workspace": str(workspace), "owner": "u", "turn_id": "t1"}
    result = await _draft_canvas(monkeypatch, ctx)
    assert result["exit_code"] == 0
    assert result["closure"] == "armed", result.get("note")
    assert "records a verdict" in result["output"]
    record = dcc.load("chat-1")
    assert result.get("saved") is True, result.get("note")
    assert record["status"] == "open"


@pytest.mark.asyncio
async def test_a_workspace_chat_without_a_project_still_files_its_canvas(monkeypatch, workspace):
    """The tool context has no `workspace` key; the chat's folder is the
    turn's active workspace. Without that fallback nothing was filed, so the
    closure had no canvas to write its verdict into."""
    import src.tool_execution as te
    monkeypatch.setattr(te, "get_active_workspace", lambda: str(workspace))
    result = await _draft_canvas(monkeypatch, {"session_id": "chat-9", "owner": "u"})
    assert result["saved"] is True
    assert dcc.load("chat-9")["concept_id"] == result["concept"]["id"]


@pytest.mark.asyncio
async def test_without_a_session_nothing_is_armed(monkeypatch, workspace):
    result = await _draft_canvas(monkeypatch, {"workspace": str(workspace)})
    assert result["exit_code"] == 0
    assert "closure" not in result


@pytest.mark.asyncio
async def test_the_end_of_the_turn_records_a_verdict_everywhere(monkeypatch, workspace, _isolated):
    ctx = {"session_id": "chat-1", "workspace": str(workspace), "owner": "u", "turn_id": "t1"}
    drafted = await _draft_canvas(monkeypatch, ctx)

    ledger = h.TurnLedger(str(workspace), "arregla la mediana de las listas pares")
    (workspace / "tests" / "test_stats.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    _edit(ledger, "src/stats.py")
    _edit(ledger, "tests/test_stats.py")
    ledger.tests = {"ran": True, "ok": True, "summary": "1 passed"}

    out = await dcc.close_turn(ledger, session_id="chat-1", turn_id="t2",
                               model_call=_all_met, diff_text="+ def median")
    assert out["overall"] == "met"
    assert out["label"].startswith("Canvas de dise")          # the turn is in Spanish
    # 1) the turn's summary
    assert ledger.summary()["canvas_check"]["overall"] == "met"
    # 2) the record on disk
    rec = dcc.load("chat-1")
    assert rec["status"] == "checked" and rec["checks"] == 1
    assert set(rec["touched"]) == {"src/stats.py", "tests/test_stats.py"}
    # 3) the canvas itself: the concept now carries the closure section
    concept = _isolated.get_concept(drafted["concept"]["id"])
    assert "Comprobación de cierre" in concept["details"]
    assert "Requirements" in concept["details"]               # the design is still there
    assert concept["refs"] == drafted["concept"]["refs"]


@pytest.mark.asyncio
async def test_an_unmet_canvas_is_reported_loudly_and_never_blocks(monkeypatch, workspace):
    ctx = {"session_id": "chat-1", "workspace": str(workspace), "owner": "u"}
    await _draft_canvas(monkeypatch, ctx)

    ledger = h.TurnLedger(str(workspace), "fix the median")
    _edit(ledger, "src/stats.py")               # but tests/test_stats.py is never written
    ledger.tests = {"ran": True, "ok": False, "summary": "1 failed"}

    async def judge(messages, schema):
        ids = re.findall(r"^- (\S+) \[", messages[1]["content"], re.MULTILINE)
        return json.dumps({"items": [{"id": i, "verdict": "not_met", "evidence": "the diff does not touch it"}
                                     for i in ids]})

    out = await dcc.close_turn(ledger, session_id="chat-1", model_call=judge, diff_text="")
    assert out["overall"] == "partial"           # src/stats.py is there, everything else is not
    verdicts = {i["id"]: i["verdict"] for i in out["items"]}
    assert verdicts["structure.1"] == "met"
    assert verdicts["structure.2"] == "not_met"
    assert verdicts["tests.1"] == "not_met"
    assert any(n.startswith("canvas_partial") for n in ledger.notes)
    assert ledger.summary()["stop_reason"] == "complete"      # it informs; it does not gate


@pytest.mark.asyncio
async def test_no_canvas_no_check(workspace):
    ledger = h.TurnLedger(str(workspace), "fix the median")
    _edit(ledger, "src/stats.py")
    assert await dcc.close_turn(ledger, session_id="chat-without-canvas", model_call=_all_met) is None
    assert ledger.summary()["canvas_check"] is None


@pytest.mark.asyncio
async def test_a_turn_that_only_designed_has_nothing_to_compare(monkeypatch, workspace):
    await _draft_canvas(monkeypatch, {"session_id": "chat-1", "workspace": str(workspace)})
    ledger = h.TurnLedger(str(workspace), "design it first")
    assert await dcc.close_turn(ledger, session_id="chat-1", model_call=_all_met) is None
    assert dcc.load("chat-1")["status"] == "open"


@pytest.mark.asyncio
async def test_a_later_turn_adds_to_what_was_touched(monkeypatch, workspace):
    await _draft_canvas(monkeypatch, {"session_id": "chat-1", "workspace": str(workspace)})
    (workspace / "tests" / "test_stats.py").write_text("x = 1\n", encoding="utf-8")

    first = h.TurnLedger(str(workspace), "go")
    _edit(first, "src/stats.py")
    one = await dcc.close_turn(first, session_id="chat-1", model_call=_all_met, diff_text="d")
    assert one["overall"] == "partial"           # tests/test_stats.py exists but was not touched yet

    second = h.TurnLedger(str(workspace), "and the test")
    _edit(second, "tests/test_stats.py")
    two = await dcc.close_turn(second, session_id="chat-1", model_call=_all_met, diff_text="d")
    assert two["overall"] == "met"
    assert dcc.load("chat-1")["checks"] == 2


@pytest.mark.asyncio
async def test_a_dead_judge_still_closes_the_loop_with_unverified_items(monkeypatch, workspace):
    await _draft_canvas(monkeypatch, {"session_id": "chat-1", "workspace": str(workspace)})
    ledger = h.TurnLedger(str(workspace), "fix the median")
    _edit(ledger, "src/stats.py")

    async def dead(messages, schema):
        raise ConnectionError("model server is down")

    out = await dcc.close_turn(ledger, session_id="chat-1", model_call=dead, diff_text="")
    assert out is not None
    assert "ConnectionError" in out["judge_error"]
    assert {i["verdict"] for i in out["items"] if i["kind"] == "requirements"} == {"unverified"}


@pytest.mark.asyncio
async def test_the_setting_turns_it_off(monkeypatch, workspace):
    await _draft_canvas(monkeypatch, {"session_id": "chat-1", "workspace": str(workspace)})
    monkeypatch.setattr(dcc, "_setting", lambda key, default=None: False)
    ledger = h.TurnLedger(str(workspace), "fix the median")
    _edit(ledger, "src/stats.py")
    assert await dcc.close_turn(ledger, session_id="chat-1", model_call=_all_met) is None
