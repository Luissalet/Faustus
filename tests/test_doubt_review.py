"""Tests for src/doubt_review.py -- the pre-edit "doubt review" gate.

`should_review` gating is exercised with an injected `risk_fn` so it needs
no real git repo / code index; `review()` is exercised with an injected
`model_call` so it needs no network or local model. The edit-tool wiring
(advisory append, block refusal, confirm_risky bypass, disabled no-op) is
exercised through `EditFileTool` with `src.doubt_review.check_edit`
monkeypatched to a deterministic stand-in, matching how `test_edit_file.py`
already drives that tool directly.
"""
from __future__ import annotations

import json
import os
import tempfile

import pytest

from src import doubt_review as dr
from src.agent_tools.filesystem_tools import EditFileTool


# ---------------------------------------------------------------------------
# should_review gating
# ---------------------------------------------------------------------------

def _risk(level: str, score: float = 80.0):
    def _fn(paths, **kw):
        return {"level": level, "score": score, "top_reasons": []}
    return _fn


NONTRIVIAL_DIFF = "--- a/x.py\n+++ b/x.py\n@@\n-old_line\n+new_line_one\n+new_line_two\n+new_line_three\n"
TRIVIAL_DIFF = "--- a/x.py\n+++ b/x.py\n@@\n-old\n+new\n"
EMPTY_DIFF = ""


def test_should_review_true_on_high_tier_nontrivial_diff():
    decision, risk = dr.should_review("pkg/hub.py", "/ws", NONTRIVIAL_DIFF, risk_fn=_risk("high"))
    assert decision is True
    assert risk["level"] == "high"


def test_should_review_false_below_min_tier():
    decision, risk = dr.should_review("pkg/hub.py", "/ws", NONTRIVIAL_DIFF, risk_fn=_risk("medium"))
    assert decision is False
    assert risk["level"] == "medium"


def test_should_review_respects_explicit_min_tier_medium():
    decision, _ = dr.should_review("pkg/hub.py", "/ws", NONTRIVIAL_DIFF, risk_fn=_risk("medium"),
                                    min_tier="medium")
    assert decision is True


def test_should_review_false_on_low_tier():
    decision, _ = dr.should_review("pkg/hub.py", "/ws", NONTRIVIAL_DIFF, risk_fn=_risk("low"))
    assert decision is False


def test_should_review_false_on_trivial_diff_even_high_tier():
    decision, risk = dr.should_review("pkg/hub.py", "/ws", TRIVIAL_DIFF, risk_fn=_risk("high"))
    assert decision is False
    # Trivial-diff gate short-circuits before the risk lookup.
    assert risk == {}


def test_should_review_false_on_empty_diff():
    decision, _ = dr.should_review("pkg/hub.py", "/ws", EMPTY_DIFF, risk_fn=_risk("high"))
    assert decision is False


@pytest.mark.parametrize("path", [
    "tests/test_foo.py",
    "src/foo_test.py",
    "docs/guide.md",
    "src/README.md",
])
def test_should_review_false_for_test_and_docs_paths(path):
    decision, _ = dr.should_review(path, "/ws", NONTRIVIAL_DIFF, risk_fn=_risk("high"))
    assert decision is False


def test_should_review_whitespace_and_comment_only_diff_is_trivial():
    diff = "--- a/x.py\n+++ b/x.py\n@@\n+   \n+# just a comment\n+// also a comment\n"
    assert dr.is_trivial_diff(diff) is True


def test_should_review_per_turn_cap():
    state = dr.DoubtReviewState(max_per_turn=1)
    decision1, _ = dr.should_review("pkg/a.py", "/ws", NONTRIVIAL_DIFF, risk_fn=_risk("high"), state=state)
    assert decision1 is True
    state.note_review_run()
    decision2, _ = dr.should_review("pkg/b.py", "/ws", NONTRIVIAL_DIFF, risk_fn=_risk("high"), state=state)
    assert decision2 is False


def test_get_risk_summary_cached_per_turn_state():
    calls = {"n": 0}

    def _fn(paths, **kw):
        calls["n"] += 1
        return {"level": "high", "score": 90}

    state = dr.DoubtReviewState()
    r1 = dr.get_risk_summary("pkg/a.py", "/ws", state=state, risk_fn=_fn)
    r2 = dr.get_risk_summary("pkg/a.py", "/ws", state=state, risk_fn=_fn)
    assert r1 == r2
    assert calls["n"] == 1


def test_should_review_reuses_cached_risk_summary_via_state():
    calls = {"n": 0}

    def _fn(paths, **kw):
        calls["n"] += 1
        return {"level": "high", "score": 90}

    state = dr.DoubtReviewState(max_per_turn=5)
    dr.should_review("pkg/a.py", "/ws", NONTRIVIAL_DIFF, risk_fn=_fn, state=state)
    dr.should_review("pkg/a.py", "/ws", NONTRIVIAL_DIFF, risk_fn=_fn, state=state)
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# review() -- JSON parsing, good and garbled
# ---------------------------------------------------------------------------

async def _call_with(raw_text):
    async def _model_call(task, path, risk_summary, diff_text, **kw):
        return raw_text
    return _model_call


@pytest.mark.asyncio
async def test_review_parses_clean_ok_json():
    call = await _call_with('{"verdict": "ok", "concerns": [], "confidence": 0.9}')
    result = await dr.review("do X", "pkg/a.py", {"level": "high"}, NONTRIVIAL_DIFF, model_call=call)
    assert result["verdict"] == "ok"
    assert result["concerns"] == []
    assert result["confidence"] == 0.9
    assert "error" not in result


@pytest.mark.asyncio
async def test_review_parses_clean_concerns_json():
    call = await _call_with(
        '{"verdict": "concerns", "concerns": ["breaks caller foo()", "drops null check"], "confidence": 0.4}'
    )
    result = await dr.review("do X", "pkg/a.py", {"level": "high"}, NONTRIVIAL_DIFF, model_call=call)
    assert result["verdict"] == "concerns"
    assert len(result["concerns"]) == 2


@pytest.mark.asyncio
async def test_review_parses_fenced_json_with_prose_around_it():
    raw = "Sure, here it is:\n```json\n{\"verdict\": \"ok\", \"concerns\": []}\n```\nHope that helps."
    call = await _call_with(raw)
    result = await dr.review("do X", "pkg/a.py", {"level": "high"}, NONTRIVIAL_DIFF, model_call=call)
    assert result["verdict"] == "ok"


@pytest.mark.asyncio
async def test_review_parses_json_with_trailing_comma():
    raw = '{"verdict": "concerns", "concerns": ["thing one",], "confidence": 0.5,}'
    call = await _call_with(raw)
    result = await dr.review("do X", "pkg/a.py", {"level": "high"}, NONTRIVIAL_DIFF, model_call=call)
    assert result["verdict"] == "concerns"
    assert result["concerns"] == ["thing one"]


@pytest.mark.asyncio
async def test_review_garbled_non_json_fails_open_to_ok():
    call = await _call_with("I think this change looks fine, no notes.")
    result = await dr.review("do X", "pkg/a.py", {"level": "high"}, NONTRIVIAL_DIFF, model_call=call)
    assert result["verdict"] == "ok"
    assert result.get("unparsed") is True


@pytest.mark.asyncio
async def test_review_empty_answer_is_error_but_fails_open():
    call = await _call_with("")
    result = await dr.review("do X", "pkg/a.py", {"level": "high"}, NONTRIVIAL_DIFF, model_call=call)
    assert result["verdict"] == "ok"
    assert "error" in result


@pytest.mark.asyncio
async def test_review_model_call_exception_fails_open():
    async def _boom(*a, **kw):
        raise RuntimeError("endpoint down")

    result = await dr.review("do X", "pkg/a.py", {"level": "high"}, NONTRIVIAL_DIFF, model_call=_boom)
    assert result["verdict"] == "ok"
    assert "endpoint down" in result.get("error", "")


@pytest.mark.asyncio
async def test_review_empty_diff_is_noop():
    async def _never_called(*a, **kw):
        raise AssertionError("model_call should not run for an empty diff")

    result = await dr.review("do X", "pkg/a.py", {"level": "high"}, "", model_call=_never_called)
    assert result["verdict"] == "ok"
    assert "error" in result


# ---------------------------------------------------------------------------
# format_section / block_message
# ---------------------------------------------------------------------------

def test_format_section_ok_verdict():
    text = dr.format_section({"verdict": "ok", "concerns": []}, {"level": "high", "score": 80})
    assert "Second look" in text
    assert "no concerns" in text


def test_format_section_concerns_lists_them():
    text = dr.format_section({"verdict": "concerns", "concerns": ["a", "b"]}, {"level": "high", "score": 80})
    assert "- a" in text and "- b" in text


def test_block_message_mentions_confirm_risky():
    text = dr.block_message({"verdict": "concerns", "concerns": ["a"]}, {"level": "high"}, "pkg/a.py")
    assert "confirm_risky" in text
    assert "pkg/a.py" in text


# ---------------------------------------------------------------------------
# Edit-tool wiring: advisory append / block refusal / confirm_risky / off
# ---------------------------------------------------------------------------

def _tmp_file(text="def f():\n    return 1\n"):
    fd, path = tempfile.mkstemp(suffix=".py")
    os.close(fd)
    with open(path, "w") as fh:
        fh.write(text)
    return path


@pytest.mark.asyncio
async def test_doubt_review_disabled_is_a_no_op(monkeypatch):
    monkeypatch.setattr(dr, "enabled", lambda: False)

    async def _should_not_run(*a, **kw):
        raise AssertionError("check_edit should not be reached when disabled")

    monkeypatch.setattr(dr, "check_edit", _should_not_run)
    p = _tmp_file()
    try:
        res = await EditFileTool().execute(
            json.dumps({"path": p, "old_string": "return 1", "new_string": "return 2"}), {})
        assert res["exit_code"] == 0
        assert "Second look" not in res["output"]
        assert open(p).read() == "def f():\n    return 2\n"
    finally:
        os.unlink(p)


@pytest.mark.asyncio
async def test_doubt_review_advisory_appends_section_and_applies_edit(monkeypatch):
    monkeypatch.setattr(dr, "enabled", lambda: True)
    monkeypatch.setattr(dr, "block_mode_enabled", lambda: False)

    async def _fake_check_edit(ctx, path, raw_path, diff, *, confirm_risky=False):
        return {"proceed": True, "section": "Second look (high-risk file, risk=HIGH 88/100): "
                                             "looks right, no concerns raised.",
                "review": {"verdict": "ok", "concerns": []}}

    monkeypatch.setattr(dr, "check_edit", _fake_check_edit)
    p = _tmp_file()
    try:
        res = await EditFileTool().execute(
            json.dumps({"path": p, "old_string": "return 1", "new_string": "return 2"}), {})
        assert res["exit_code"] == 0
        assert "Second look" in res["output"]
        assert res["doubt_review"]["verdict"] == "ok"
        # The edit was actually applied.
        assert open(p).read() == "def f():\n    return 2\n"
    finally:
        os.unlink(p)


@pytest.mark.asyncio
async def test_doubt_review_block_mode_refuses_the_edit(monkeypatch):
    monkeypatch.setattr(dr, "enabled", lambda: True)
    monkeypatch.setattr(dr, "block_mode_enabled", lambda: True)

    async def _fake_check_edit(ctx, path, raw_path, diff, *, confirm_risky=False):
        if confirm_risky:
            return {"proceed": True, "section": "Second look: looks right now.", "review": {"verdict": "ok"}}
        return {"proceed": False, "result": {
            "error": "doubt_review: concerns raised", "exit_code": 1,
            "status": "doubt_review_blocked", "doubt_review": {"verdict": "concerns", "concerns": ["bad"]},
        }}

    monkeypatch.setattr(dr, "check_edit", _fake_check_edit)
    p = _tmp_file()
    original = open(p).read()
    try:
        res = await EditFileTool().execute(
            json.dumps({"path": p, "old_string": "return 1", "new_string": "return 2"}), {})
        assert res["exit_code"] == 1
        assert res.get("status") == "doubt_review_blocked"
        # Nothing was written.
        assert open(p).read() == original
    finally:
        os.unlink(p)


@pytest.mark.asyncio
async def test_doubt_review_confirm_risky_bypasses_block(monkeypatch):
    monkeypatch.setattr(dr, "enabled", lambda: True)
    monkeypatch.setattr(dr, "block_mode_enabled", lambda: True)

    calls = []

    async def _fake_check_edit(ctx, path, raw_path, diff, *, confirm_risky=False):
        calls.append(confirm_risky)
        return {"proceed": True, "section": "Second look: applied with confirm_risky.",
                "review": {"verdict": "concerns", "concerns": ["bad"]}}

    monkeypatch.setattr(dr, "check_edit", _fake_check_edit)
    p = _tmp_file()
    try:
        res = await EditFileTool().execute(
            json.dumps({"path": p, "old_string": "return 1", "new_string": "return 2",
                       "confirm_risky": True}), {})
        assert res["exit_code"] == 0
        assert open(p).read() == "def f():\n    return 2\n"
        assert True in calls
    finally:
        os.unlink(p)
