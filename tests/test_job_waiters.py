"""Waiting for the jobs that tools start (src/job_waiters.py) during a real
recipe execution: image requests, fan-out runs, research podcasts and deep
research are polled through their own status tool or record until they finish,
fail or the timeout passes; a tool the registry does not know stays `queued`.
No model, GPU or network: the status tools are stood in for by a scripted
executor, the deep-research record is a real file in a temporary folder."""
from __future__ import annotations

import json

import pytest

from src import constants as constants_mod
from src import job_waiters as jw
from src import recipe_execution as rex
from src import recipe_learning as rl

OWNER = "admin"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(constants_mod, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setattr(jw, "POLL_INTERVAL", 0.01)
    yield


@pytest.fixture()
def ws(tmp_path):
    w = tmp_path / "ws"
    w.mkdir()
    return w


class Script:
    """An executor: the start tool answers once, the status tool answers from a list."""

    def __init__(self, start, polls):
        self.start, self.polls, self.calls = start, list(polls), []

    def __call__(self, tool, args, call_id):
        self.calls.append((tool, dict(args)))
        if tool in jw.WAITERS and not any(c[0] == tool for c in self.calls[:-1]):
            return self.start
        return self.polls.pop(0) if len(self.polls) > 1 else self.polls[0]


def _run(ws, tool, args, executor, **kw):
    recipe = rl.propose(OWNER, request="lanza un trabajo", steps=[{"tool": tool, "args": args}], title="Trabajo")
    params = {p["name"]: p["example"] for p in recipe.params}
    return rex.execute(recipe.id, OWNER, params, executor=executor, workspace=str(ws), **kw)


# -- the judges ------------------------------------------------------------------------------

def test_judges_say_running_done_or_failed():
    assert jw._judge_fanout({"status": "running", "task_alive": True, "candidates": []})[0] == jw.RUNNING
    assert jw._judge_fanout({"status": "done", "candidates": [{"state": "done"}, {"state": "error"}]})[0] == jw.DONE
    assert jw._judge_fanout({"status": "done", "candidates": [{"state": "error"}]})[0] == jw.FAILED
    assert jw._judge_fanout({"status": "running", "task_alive": False, "candidates": []})[0] == jw.FAILED
    assert jw._judge_fanout({"error": "nope"})[0] == jw.FAILED
    assert jw._judge_image({"state": "pending", "error": "still pending"})[0] == jw.RUNNING
    assert jw._judge_image({"image_url": "/x.png", "image_id": "1"})[0] == jw.DONE
    assert jw._judge_image({"state": "failed", "error": "x"})[0] == jw.FAILED
    assert jw._judge_image({"state": "unknown", "status": "outcome_unknown", "error": "x"})[0] == jw.FAILED
    assert jw._judge_podcast({"status": "running"})[0] == jw.RUNNING
    assert jw._judge_podcast({"status": "done"})[0] == jw.DONE
    assert jw._judge_podcast({"status": "failed", "error": "boom"})[0] == jw.FAILED
    assert jw._judge_research({"status": "done"})[0] == jw.DONE
    assert jw._judge_research({"status": "interrupted"})[0] == jw.FAILED
    assert jw._judge_research({"status": "running"})[0] == jw.RUNNING


def test_detect_only_recognises_unfinished_jobs_of_registered_tools():
    assert jw.find("fanout_run", {"run_id": "r1", "exit_code": 0})[1] == "r1"
    assert jw.find("fanout_run", {"error": "x", "exit_code": 1}) is None
    assert jw.find("generate_image", {"state": "pending", "request_id": "q1", "error": "pending"})[1] == "q1"
    assert jw.find("generate_image", {"image_url": "/a.png", "request_id": "q1"}) is None
    assert jw.find("research_podcast", {"status": "done", "session_id": "s"}) is None
    assert jw.find("some_other_tool", {"job_id": "j", "status": "queued"}) is None


def test_wait_reports_done_failure_timeout_and_a_raising_poll():
    waiter = jw.WAITERS["fanout_run"]
    seq = [{"status": "running", "task_alive": True}] * 2 + [{"status": "done", "candidates": [{"state": "done"}]}]
    state, _, _ = jw.wait(waiter, "r", lambda t, a, c: seq.pop(0), "c", OWNER, 5, sleep=lambda s: None)
    assert state == jw.DONE and not seq
    t = [0.0]

    def clock():
        t[0] += 1.0
        return t[0]
    state, detail, _ = jw.wait(waiter, "r", lambda *a: {"status": "running", "task_alive": True}, "c", OWNER, 3,
                               sleep=lambda s: None, clock=clock)
    assert state == "timeout" and "r" in detail

    def boom(*a):
        raise RuntimeError("down")
    state, detail, _ = jw.wait(waiter, "r", boom, "c", OWNER, 3, sleep=lambda s: None)
    assert state == jw.FAILED and "down" in detail


# -- inside a real execution -----------------------------------------------------------------

def test_a_fanout_run_is_waited_on_until_it_is_done(ws):
    ex = Script({"run_id": "fo-1", "exit_code": 0},
                [{"status": "running", "task_alive": True, "candidates": []},
                 {"status": "done", "candidates": [{"state": "done"}]}])
    out = _run(ws, "fanout_run", {"prompt": "compara los modelos"}, ex)
    step = out["steps"][0]
    assert step["status"] == "ok" and step["waited_for"] == "fanout_run:fo-1" and step["ids"]["run_id"] == "fo-1"
    assert [c[0] for c in ex.calls].count("fanout_status") == 2
    assert out["status"] == "executed_unverified" and out["verified"] is False


def test_a_fanout_run_that_never_finishes_is_incomplete_not_ok(ws):
    ex = Script({"run_id": "fo-2", "exit_code": 0}, [{"status": "running", "task_alive": True, "candidates": []}])
    out = _run(ws, "fanout_run", {"prompt": "compara"}, ex, job_timeout=1)
    assert out["status"] == "incomplete" and out["steps"][0]["status"] == "queued"
    assert "not counted as finished" in out["steps"][0]["detail"]


def test_a_failed_job_fails_the_step_and_stops_the_chain(ws):
    ex = Script({"run_id": "fo-3", "exit_code": 0}, [{"status": "done", "candidates": [{"state": "error"}]}])
    out = _run(ws, "fanout_run", {"prompt": "compara"}, ex)
    assert out["status"] == "step_failed" and out["steps"][0]["status"] == "error"


def test_a_pending_image_request_is_resolved_through_image_job(ws):
    ex = Script({"error": "Prospero image is still pending", "state": "pending", "request_id": "img-9", "exit_code": 1},
                [{"state": "pending", "error": "still pending", "request_id": "img-9"},
                 {"image_url": "/api/generated-image/a.png", "image_id": "7", "request_id": "img-9"}])
    out = _run(ws, "generate_image", {"prompt": "un faro"}, ex)
    assert out["steps"][0]["status"] == "ok"
    assert ("image_job", {"request_id": "img-9", "action": "status"}) in ex.calls


def test_an_unregistered_tool_with_a_job_id_stays_queued(ws):
    ex = lambda tool, args, call_id: {"job_id": "j1", "status": "queued", "exit_code": 0}  # noqa: E731
    out = _run(ws, "write_file", {"path": "a.txt", "content": "x"}, ex)
    assert out["steps"][0]["status"] == "queued" and "no waiter" in out["steps"][0]["detail"]
    assert out["status"] == "incomplete"


def test_deep_research_is_polled_through_its_record_on_disk(tmp_path, monkeypatch):
    from src import research_handler as rh
    root = tmp_path / "research"
    root.mkdir()
    monkeypatch.setattr(rh, "RESEARCH_DATA_DIR", root)
    waiter = jw.WAITERS["trigger_research"]
    start = {"research_session_id": "abc123", "session_id": "abc123", "exit_code": 0}
    assert jw.find("trigger_research", start)[1] == "abc123"
    judge = lambda: waiter.judge(waiter.poll(None, "abc123", "c", OWNER))  # noqa: E731
    assert judge()[0] == jw.RUNNING                                   # no record yet
    (root / "abc123.json").write_text(json.dumps({"marker": True, "status": "running", "owner": OWNER}))
    assert judge()[0] == jw.RUNNING
    (root / "abc123.json").write_text(json.dumps({"status": "done", "owner": OWNER, "result": "x"}))
    assert judge()[0] == jw.DONE
    (root / "abc123.json").write_text(json.dumps({"status": "error", "owner": OWNER, "error": "sin red"}))
    state, detail = judge()
    assert state == jw.FAILED and "sin red" in detail
    (root / "abc123.json").write_text(json.dumps({"status": "done", "owner": "otro"}))
    assert judge()[0] == jw.FAILED


def test_every_registered_waiter_uses_tools_that_exist():
    from src import tool_index
    names = set(tool_index.BUILTIN_TOOL_DESCRIPTIONS)
    for tool in jw.WAITERS:
        assert tool in names, tool
    for status_tool in ("image_job", "fanout_status", "research_podcast"):
        assert status_tool in names, status_tool
