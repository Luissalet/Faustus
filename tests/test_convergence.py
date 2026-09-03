"""Convergence detector (src/convergence.py) and its use in a dispatched
job's fix loop (src/dispatch.py).

The fix loop used to run a FIXED number of rounds: with `fix_rounds: 2` it
spent two fixer workers even when the first one changed nothing and the second
was going to change nothing either. The detector reads the artifacts the rounds
leave behind — the verification verdict, its failures, its output tail and the
files that changed on disk — and says whether the rounds are still producing
change:

    score = 0.35*size_trend + 0.35*change_velocity + 0.30*similarity_trend

`fix_rounds` becomes a MAXIMUM: the loop stops as soon as the score reaches the
`high` band, records `stopped_by: "convergence"` and the score, and says so in
the Workers chat verdict line. With `agent_fix_round_convergence` off, the loop
is the fixed counter it always was (and the request cap is 2 again).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import convergence, dispatch


# ── the detector itself ─────────────────────────────────────────────────────

def test_fewer_than_two_rounds_is_early_and_never_converged():
    for rounds in ([], ["only one round"], None):
        v = convergence.assess(rounds)
        assert v["converged"] is False and v["confidence"] == "early" and v["score"] == 0.0
        assert set(v["components"]) == {"size_trend", "change_velocity", "similarity_trend"}
        assert "at least 2" in v["reason"]


def test_identical_rounds_converge_with_high_confidence():
    same = "FAILED tests/test_cart.py::test_total — assert 0 == 3\nchanged: cart.py"
    v = convergence.assess([same, same])
    assert v["converged"] is True and v["confidence"] == "high" and v["score"] >= 0.75
    assert v["components"] == {"size_trend": 1.0, "change_velocity": 1.0, "similarity_trend": 1.0}
    assert v["rounds"] == 2


def test_rounds_that_keep_changing_everything_do_not_converge():
    v = convergence.assess([
        "FAILED test_total — assert 0 == 3",
        "ImportError: cannot import name apply_tax from cart",
        "SyntaxError: invalid syntax at line 41 of parser.py, plus 88 other lines of pytest output " * 3,
    ])
    assert v["converged"] is False and v["confidence"] == "early" and v["score"] < 0.5
    assert v["components"]["change_velocity"] < 0.3


def test_a_shrinking_failure_list_is_progress_not_convergence():
    """Rounds that really fix things stay BELOW the high band: the artifacts
    keep changing size and content."""
    v = convergence.assess([
        "3 failed: test_a, test_b, test_c\n" + "trace " * 200,
        "2 failed: test_a, test_b\n" + "trace " * 120,
        "1 failed: test_a\n" + "trace " * 40,
    ])
    assert v["converged"] is False
    assert v["components"]["size_trend"] < 0.8


def test_similarity_trend_rewards_rising_similarity():
    """Two runs of three rounds with the same mean similarity: the one whose
    similarity RISES scores higher than the one whose similarity falls."""
    rising = convergence.assess(["alpha beta gamma delta", "alpha beta gamma epsilon", "alpha beta gamma epsilon"])
    falling = convergence.assess(["alpha beta gamma epsilon", "alpha beta gamma epsilon", "alpha beta gamma delta"])
    assert rising["components"]["similarity_trend"] > falling["components"]["similarity_trend"]
    assert rising["score"] > falling["score"]


def test_only_the_last_three_pairs_count():
    """An old, wildly different round must not keep a settled loop from
    converging — the trend is read from the newest pairs."""
    steady = "same verification output every time"
    v = convergence.assess(["completely unrelated first round " * 20, steady, steady, steady, steady])
    assert v["converged"] is True


def test_the_score_is_the_weighted_sum_of_its_components():
    v = convergence.assess(["a b c d", "a b c e", "a b c f"])
    c = v["components"]
    expected = 0.35 * c["size_trend"] + 0.35 * c["change_velocity"] + 0.30 * c["similarity_trend"]
    assert abs(v["score"] - expected) < 0.002
    assert convergence.W_SIZE + convergence.W_VELOCITY + convergence.W_SIMILARITY == pytest.approx(1.0)


def test_two_empty_artifacts_are_identical_one_empty_is_not():
    assert convergence.similarity("", "") == 1.0
    assert convergence.similarity("", "something") == 0.0
    assert convergence.assess(["", ""])["converged"] is True


def test_assess_never_raises_on_junk():
    for junk in (42, [None, None], [{"a": 1}, {"a": 1}], ["x", 3.5], object()):
        v = convergence.assess(junk)
        assert isinstance(v["score"], float) and v["converged"] in (True, False)


def test_the_bands_are_high_moderate_early():
    assert convergence._confidence(0.75) == "high"
    assert convergence._confidence(0.74) == "moderate"
    assert convergence._confidence(0.50) == "moderate"
    assert convergence._confidence(0.49) == "early"


# ── wired into the dispatch fix loop ────────────────────────────────────────

class _SM:
    def __init__(self):
        self.sessions = {}
        self.messages = []

    def create_session(self, session_id, name, endpoint_url, model, rag=False, owner=None):
        s = SimpleNamespace(id=session_id, name=name, endpoint_url=endpoint_url, model=model, owner=owner,
                            headers=None, messages=[])
        self.sessions[session_id] = s
        return s

    def get_session(self, sid):
        return self.sessions.get(sid)

    def add_message(self, sid, msg):
        self.messages.append((sid, msg))

    def save_sessions(self):
        pass


def _worker_report(**over):
    base = {
        "id": "sa1-abc", "name": "w1", "session_id": "child-1", "status": "done", "stop_reason": "complete",
        "error": None, "tool_calls": 3, "failed_calls": 0, "mutations": [], "rejections": 0, "rounds": 2,
        "static_checks": [], "git": None, "duration_s": 5.0, "final_text": "done", "role": "worker", "files": [],
        "model": None, "instruction": "x", "input_tokens": 10, "output_tokens": 5, "started_at": 1.0,
        "ended_at": 6.0, "steered": 0, "supervisor": [],
    }
    base.update(over)
    return base


@pytest.fixture
def job_runner(tmp_path, monkeypatch):
    """A dispatched job with a fake delegate tool and a verification that keeps
    failing the same way — the case the detector is for."""
    import src.ai_interaction as ai
    import src.settings as settings_mod
    from src.agent_tools import subagent_tools as st

    sm = _SM()
    monkeypatch.setattr(ai, "get_session_manager", lambda: sm)
    monkeypatch.setattr(dispatch, "_data_dir", lambda: str(tmp_path / "dispatch"))
    monkeypatch.setattr(dispatch, "resolve_route",
                        lambda owner, model=None: ("http://127.0.0.1:11434/v1", model or "qwen3.5:9b", None))
    monkeypatch.setattr(dispatch, "_checkpoint", lambda ws, label: None)
    monkeypatch.setattr(dispatch, "_changes_since", lambda ws, sha: None)

    values = {}
    real_get = settings_mod.get_setting
    monkeypatch.setattr(settings_mod, "get_setting",
                        lambda key, default=None: values[key] if key in values else real_get(key, default))

    state = {"calls": [], "sm": sm, "settings": values, "verifications": [], "ws": str(tmp_path / "ws")}
    Path(state["ws"]).mkdir()

    class FakeTool:
        async def execute(self, content, ctx):
            args = json.loads(content)
            state["calls"].append(args)
            return {"output": "r", "exit_code": 0, "lock_conflicts": [], "dropped_tasks": 0,
                    "subagents": [_worker_report(name=args["tasks"][0]["name"])]}

    monkeypatch.setattr(st, "DelegateAgentsTool", FakeTool)

    def fake_verification(workspace, verify, changed, **kw):
        state["verifications"].append(len(state["verifications"]))
        return dict(state["verify_result"])

    state["verify_result"] = {
        "mode": "command", "ran": True, "ok": False, "inconclusive": False, "kind": "pytest",
        "command": "pytest -q", "scope": "all", "exit_code": 1, "timed_out": False, "duration_s": 1.0,
        "summary": "1 failed", "failures": ["tests/test_cart.py::test_total — assert 0 == 3"],
        "output_tail": "E   assert 0 == 3",
    }
    monkeypatch.setattr(dispatch, "run_verification", fake_verification)
    dispatch.reset_for_tests()
    yield state
    dispatch.reset_for_tests()


async def test_the_fix_loop_stops_when_the_rounds_stop_changing_anything(job_runner):
    """Two fix rounds that leave the identical verification behind: the third
    is not spent, the job says why, and the score rides in the answer."""
    job_runner["settings"]["agent_fix_round_convergence"] = True
    job = await dispatch.start("luis", {"tasks": ["fix total()"], "workspace": job_runner["ws"],
                                        "fix_rounds": 4, "verify": "pytest -q"})
    assert await dispatch.wait(job, 60)
    # the workers + fix rounds 1 and 2; round 3 and 4 were never dispatched
    assert len(job_runner["calls"]) == 3, [c["tasks"][0]["name"] for c in job_runner["calls"]]
    assert [c["tasks"][0]["name"] for c in job_runner["calls"][1:]] == ["fixer-1", "fixer-2"]
    res = dispatch.compact(job)["result"]
    assert res["stopped_by"] == "convergence"
    assert res["convergence"]["converged"] is True and res["convergence"]["score"] >= 0.75
    assert res["convergence"]["confidence"] == "high" and res["convergence"]["rounds"] == 2
    assert "fix rounds converged" in job.verdict and str(res["convergence"]["score"]) in job.verdict
    # the Workers chat carries the same verdict line
    last = [m for sid, m in job_runner["sm"].messages if sid == job.session_id][-1]
    assert "fix rounds converged" in last.content
    assert job.status == "partial"          # the verification still failed


async def test_with_the_setting_off_the_fixed_counter_runs_every_round(job_runner):
    """The invariant: off = today's behaviour, byte for byte. Same job, same
    unchanging verification — every requested round is spent, nothing about
    convergence appears anywhere."""
    job_runner["settings"]["agent_fix_round_convergence"] = False
    job = await dispatch.start("luis", {"tasks": ["fix total()"], "workspace": job_runner["ws"],
                                        "fix_rounds": 4, "verify": "pytest -q"})
    assert await dispatch.wait(job, 60)
    # 4 was clamped to the old maximum of 2, and both rounds ran
    assert job.fix_rounds == dispatch._MAX_FIX_ROUNDS == 2
    assert len(job_runner["calls"]) == 3
    res = dispatch.compact(job)["result"]
    assert "convergence" not in res and "stopped_by" not in res
    assert job.convergence is None and job.stopped_by is None
    assert "converged" not in (job.verdict or "")


async def test_a_loop_that_is_still_making_progress_spends_its_rounds(job_runner, monkeypatch):
    """Rounds that change things must NOT be cut short: the artifact differs
    every round, so the detector never reaches the high band."""
    job_runner["settings"]["agent_fix_round_convergence"] = True
    outputs = [
        {"summary": "3 failed", "failures": ["a", "b", "c"], "output_tail": "E assert 0 == 3 " * 40},
        {"summary": "2 failed", "failures": ["a", "b"], "output_tail": "E import error in parser " * 20},
        {"summary": "1 failed", "failures": ["a"], "output_tail": "E name apply_tax is not defined"},
        {"summary": "1 failed", "failures": ["z"], "output_tail": "E totally different message here " * 9},
    ]
    base = dict(job_runner["verify_result"])
    seq = iter(outputs)

    def verification(workspace, verify, changed, **kw):
        try:
            over = next(seq)
        except StopIteration:
            over = outputs[-1]
        return {**base, **over}

    monkeypatch.setattr(dispatch, "run_verification", verification)
    job = await dispatch.start("luis", {"tasks": ["fix it"], "workspace": job_runner["ws"],
                                        "fix_rounds": 3, "verify": "pytest -q"})
    assert await dispatch.wait(job, 60)
    assert len(job_runner["calls"]) == 4                     # the workers + all 3 fix rounds
    res = dispatch.compact(job)["result"]
    assert res.get("stopped_by") is None
    assert res["convergence"]["converged"] is False


async def test_convergence_raises_the_fix_round_cap_only_while_it_is_on(job_runner):
    """`fix_rounds` is a maximum, so a caller may ask for more of them — but
    only while something ends the loop for them."""
    job_runner["settings"]["agent_fix_round_convergence"] = True
    assert dispatch._verify_options({"fix_rounds": 9})[2] == dispatch._MAX_FIX_ROUNDS_CONVERGENCE == 4
    job_runner["settings"]["agent_fix_round_convergence"] = False
    assert dispatch._verify_options({"fix_rounds": 9})[2] == 2
    assert dispatch._verify_options({})[2] == dispatch._DEFAULT_FIX_ROUNDS == 1


def test_a_broken_detector_never_breaks_the_job(monkeypatch):
    """Nothing in the fix loop may raise: a convergence module that blows up
    leaves the loop running on its counter."""
    import src.convergence as conv
    monkeypatch.setattr(conv, "assess", lambda rounds: (_ for _ in ()).throw(RuntimeError("boom")))
    assert dispatch._assess_convergence(["a", "b"]) is None


def test_the_round_artifact_carries_the_verdict_the_failures_and_the_files():
    job = dispatch.DispatchJob("luis", {"tasks": []}, "/ws", "", "m", None, "t")
    job.changes = {"added": ["b.py"], "modified": ["a.py"], "deleted": []}
    art = dispatch._round_artifact(job, {"summary": "1 failed", "failures": ["test_total"],
                                         "output_tail": "assert 0 == 3"})
    assert "1 failed" in art and "test_total" in art and "assert 0 == 3" in art
    assert "changed: a.py, b.py" in art
