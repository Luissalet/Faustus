"""`wait_for` and live events as orchestration primitives (src/dispatch.py,
routes/dispatch_routes.py).

From frankenterm: block on a CONDITION, not on a sleep. A coordinator should
be able to say "wake me when the verification starts", "wake me when a worker
hits its rate limit", "wake me when anything changes on disk" — and be woken
the moment it happens, not on the next poll tick. So every test here measures
the ELAPSED time: a condition that resolves by polling would pass a `met`
assertion and fail these.

Also pinned:
  * a timeout is `met: false`, not an exception (the four-value outcomes);
  * `done` and the plain `/wait` are byte-for-byte what they were;
  * the non-streaming `/events` answer is byte-for-byte what it was;
  * an unknown condition is a clear 400, never a 500;
  * with `agent_worker_state_detection` and `agent_dispatch_sse` off,
    everything behaves exactly as it did before either existed;
  * a worker detected rate-limited or waiting for input is REPORTED, never
    killed;
  * a client that disconnects mid-stream leaves no waiter behind on the job.
"""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

import routes.dispatch_routes as dr
from src import dispatch

# Every wait below is given a generous ceiling; the assertions are that it
# came back far sooner than this, because the condition held.
TIMEOUT = 30.0
FAST = 5.0


@pytest.fixture(autouse=True)
def _clean():
    dispatch.reset_for_tests()
    yield
    dispatch.reset_for_tests()


def _job(tmp_path=None, status="running"):
    job = dispatch.DispatchJob("luis", {"tasks": [{"name": "w1", "instruction": "a"},
                                                  {"name": "w2", "instruction": "b"}],
                                        "parallel": True, "timeout_s": 60},
                               str(tmp_path) if tmp_path else None, "", "m", None, "t")
    job.status = status
    job.started = time.time()
    dispatch._jobs[job.id] = job
    return job


async def _soon(fn, delay=0.05):
    """Drive the job like a worker would, just after the wait has started."""
    await asyncio.sleep(delay)
    fn()


def _settings(monkeypatch, **values):
    from src import settings as settings_mod
    monkeypatch.setattr(settings_mod, "get_setting", lambda key, default=None: values.get(key, default))


# ── every condition resolves the moment it holds ────────────────────────────

@pytest.mark.asyncio
async def test_done_resolves_when_the_job_settles(tmp_path):
    job = _job(tmp_path)

    def finish():
        job.status = "partial"
        job.verdict = "1/2 workers done (error)"
        job._notify()

    started = time.monotonic()
    task = asyncio.create_task(_soon(finish))
    answer = await dispatch.wait_for(job, condition="done", timeout_s=TIMEOUT)
    elapsed = time.monotonic() - started
    await task
    assert answer["met"] is True and answer["condition"] == "done"
    assert elapsed < FAST, "a `done` wait must return on the settle, not on a tick"
    assert answer["waited_s"] < FAST
    # the state is the same compact job GET /api/dispatch/{id} answers with
    assert answer["state"]["id"] == job.id and answer["state"]["status"] == "partial"
    assert answer["state"]["verdict"] == "1/2 workers done (error)"


@pytest.mark.asyncio
async def test_phase_resolves_when_the_job_reaches_it(tmp_path):
    job = _job(tmp_path)
    job._event(event="job", message="workers running")

    started = time.monotonic()
    task = asyncio.create_task(_soon(lambda: job._event(event="job", message="running the verification")))
    answer = await dispatch.wait_for(job, condition="phase:verification", timeout_s=TIMEOUT)
    elapsed = time.monotonic() - started
    await task
    assert answer["met"] is True and elapsed < FAST
    assert answer["condition"] == "phase:verification"
    # a phase already reached before the call is met at once
    assert (await dispatch.wait_for(job, condition="phase:workers running", timeout_s=TIMEOUT))["met"] is True


@pytest.mark.asyncio
async def test_worker_state_resolves_when_a_worker_enters_it(tmp_path):
    """The condition the rule packs exist for: a worker that hit its provider's
    rate limit is a fact the coordinator can wait on — and the worker keeps
    running while it does."""
    job = _job(tmp_path)

    def rate_limited():
        job.note_worker_event({"name": "w1", "event": "tool", "phase": "done",
                               "output": "POST /v1/messages\nHTTP 429 Too Many Requests"})

    started = time.monotonic()
    task = asyncio.create_task(_soon(rate_limited))
    answer = await dispatch.wait_for(job, condition="worker:w1:rate_limited", timeout_s=TIMEOUT)
    elapsed = time.monotonic() - started
    await task
    assert answer["met"] is True and elapsed < FAST
    # `*` matches any worker, and a state a worker never entered does not
    assert (await dispatch.wait_for(job, condition="worker:*:rate_limited", timeout_s=TIMEOUT))["met"] is True
    assert (await dispatch.wait_for(job, condition="worker:w2:rate_limited", timeout_s=0.2))["met"] is False
    assert (await dispatch.wait_for(job, condition="worker:w1:disk_full", timeout_s=0.2))["met"] is False
    # …and the job never stopped it: that is the supervisor's call, not this one
    assert job.status == "running" and job.task is None


@pytest.mark.asyncio
async def test_event_substring_resolves_on_the_event_that_carries_it(tmp_path):
    job = _job(tmp_path)

    started = time.monotonic()
    task = asyncio.create_task(_soon(lambda: job.note_worker_event(
        {"name": "w1", "event": "tool", "tool": "bash", "command": "pytest -q tests/test_cart.py"})))
    answer = await dispatch.wait_for(job, condition="event:test_cart", timeout_s=TIMEOUT)
    elapsed = time.monotonic() - started
    await task
    assert answer["met"] is True and elapsed < FAST
    assert (await dispatch.wait_for(job, condition="event:never happened", timeout_s=0.2))["met"] is False


@pytest.mark.asyncio
async def test_changed_resolves_when_the_workspace_really_changes(tmp_path):
    (tmp_path / "cart.py").write_text("x = 1\n", encoding="utf-8")
    job = _job(tmp_path)

    def worker_writes():
        (tmp_path / "cart.py").write_text("x = 2\ndef apply_discount(): ...\n", encoding="utf-8")
        job.note_worker_event({"name": "w1", "event": "tool", "tool": "write_file", "phase": "done"})

    started = time.monotonic()
    task = asyncio.create_task(_soon(worker_writes))
    answer = await dispatch.wait_for(job, condition="changed", timeout_s=TIMEOUT)
    elapsed = time.monotonic() - started
    await task
    assert answer["met"] is True and elapsed < FAST
    # a job with no workspace has nothing to watch: answered at once, not held
    started = time.monotonic()
    answer = await dispatch.wait_for(_job(None), condition="changed", timeout_s=TIMEOUT)
    assert answer["met"] is False and time.monotonic() - started < FAST


@pytest.mark.asyncio
async def test_a_condition_already_true_returns_immediately(tmp_path):
    job = _job(tmp_path, status="done")
    started = time.monotonic()
    assert (await dispatch.wait_for(job, condition="done", timeout_s=TIMEOUT))["met"] is True
    # and one that can no longer become true does NOT hold the caller for its
    # whole timeout: a finished job reaches no new phase
    assert (await dispatch.wait_for(job, condition="phase:verification", timeout_s=TIMEOUT))["met"] is False
    assert (await dispatch.wait_for(job, condition="worker:w1:stuck", timeout_s=TIMEOUT))["met"] is False
    assert time.monotonic() - started < FAST


# ── a timeout is an answer, not an error ────────────────────────────────────

@pytest.mark.asyncio
async def test_a_timeout_answers_met_false_and_says_how_long_it_waited(tmp_path):
    job = _job(tmp_path)
    started = time.monotonic()
    answer = await dispatch.wait_for(job, condition="phase:verification", timeout_s=0.3)
    elapsed = time.monotonic() - started
    assert answer["met"] is False and answer["condition"] == "phase:verification"
    assert 0.25 <= answer["waited_s"] < FAST and elapsed >= 0.25
    assert answer["state"]["status"] == "running"
    assert set(answer) == {"met", "condition", "waited_s", "state"}
    # a zero timeout is a poll, not an error
    assert (await dispatch.wait_for(job, condition="done", timeout_s=0))["met"] is False
    # and an unusable timeout falls back to the default instead of raising
    settled = _job(tmp_path, status="done")
    assert (await dispatch.wait_for(settled, condition="done", timeout_s="junk"))["met"] is True
    assert (await dispatch.wait_for(settled.id, condition="done", timeout_s=None))["met"] is True


@pytest.mark.asyncio
async def test_an_unknown_condition_is_refused_with_every_form_named(tmp_path):
    job = _job(tmp_path)
    for bad in ("finished", "phase:", "worker:w1", "worker:w1:sleeping", "worker::stuck", "event:", "  :  "):
        with pytest.raises(ValueError) as exc:
            await dispatch.wait_for(job, condition=bad, timeout_s=TIMEOUT)
        message = str(exc.value)
        assert "unknown wait condition" in message
        for form in ("done", "changed", "phase:<name>", "worker:<label>:<state>", "event:<text>"):
            assert form in message
        assert "rate_limited" in message and "waiting_for_input" in message
    with pytest.raises(ValueError):
        await dispatch.wait_for("no-such-job", condition="done", timeout_s=TIMEOUT)


def test_parse_condition_reads_every_documented_form():
    assert dispatch.parse_condition(None) == {"kind": "done", "raw": "done"}
    assert dispatch.parse_condition("  ")["kind"] == "done"
    assert dispatch.parse_condition("DONE")["kind"] == "done"
    assert dispatch.parse_condition("changed")["kind"] == "changed"
    assert dispatch.parse_condition("phase:Verifying") == {"kind": "phase", "text": "verifying", "raw": "phase:Verifying"}
    assert dispatch.parse_condition("event:npm ERR") == {"kind": "event", "text": "npm err", "raw": "event:npm ERR"}
    assert dispatch.parse_condition("worker:w1:OOM") == {"kind": "worker", "label": "w1", "state": "oom",
                                                        "raw": "worker:w1:OOM"}


# ── `done` and the plain reads are exactly what they were ───────────────────

def _client(monkeypatch, cookie_user="luis"):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()

    @app.middleware("http")
    async def stamp(request, call_next):
        request.state.current_user = cookie_user
        return await call_next(request)

    monkeypatch.setattr(dr, "require_user", lambda request: getattr(request.state, "current_user", None) or "")
    monkeypatch.setattr(dr, "_is_admin", lambda owner: True)
    app.include_router(dr.setup_dispatch_routes())
    return TestClient(app)


def _todays_events_body(job):
    """Exactly what the endpoint returned before any of this existed."""
    from fastapi.responses import JSONResponse
    return JSONResponse({"id": job.id, "status": job.status, "events": list(job.events)}).body


def test_the_non_streaming_events_answer_is_byte_identical(monkeypatch, tmp_path):
    job = _job(tmp_path)
    job._event(event="job", message="checkpointing the workspace")
    job.note_worker_event({"name": "w1", "event": "started", "session_id": "child-1",
                           "output": "HTTP 429 Too Many Requests"})
    client = _client(monkeypatch)
    expected = _todays_events_body(job)

    plain = client.get(f"/api/dispatch/{job.id}/events")
    assert plain.content == expected
    assert set(plain.json()) == {"id", "status", "events"}
    # …and with the stream asked for but switched off, byte for byte the same
    _settings(monkeypatch, agent_dispatch_sse=False, agent_worker_state_detection=True)
    assert client.get(f"/api/dispatch/{job.id}/events", params={"stream": "1"}).content == expected
    assert client.get(f"/api/dispatch/{job.id}/events", params={"stream": "0"}).content == expected


def test_wait_without_a_condition_is_the_long_poll_it_always_was(monkeypatch, tmp_path):
    from fastapi.responses import JSONResponse
    job = _job(tmp_path, status="done")
    job.verdict = "1/1 workers done"
    client = _client(monkeypatch)
    answer = client.get(f"/api/dispatch/{job.id}/wait", params={"timeout": 1})
    assert answer.content == JSONResponse(dispatch.compact(job)).body
    assert "met" not in answer.json() and answer.json()["status"] == "done"


def test_the_wait_route_takes_a_condition_and_refuses_an_unknown_one(monkeypatch, tmp_path):
    job = _job(tmp_path, status="done")
    client = _client(monkeypatch)
    answer = client.get(f"/api/dispatch/{job.id}/wait", params={"timeout": 5, "condition": "done"})
    assert answer.status_code == 200
    body = answer.json()
    assert body["met"] is True and body["condition"] == "done" and body["state"]["id"] == job.id
    bad = client.get(f"/api/dispatch/{job.id}/wait", params={"timeout": 5, "condition": "whenever"})
    assert bad.status_code == 400 and "unknown wait condition" in bad.json()["detail"]


def test_the_states_block_is_opt_in(monkeypatch, tmp_path):
    job = _job(tmp_path)
    job.note_worker_event({"name": "w1", "event": "tool", "output": "429 Too Many Requests"})
    client = _client(monkeypatch)
    assert "states" not in client.get(f"/api/dispatch/{job.id}/events").json()
    states = client.get(f"/api/dispatch/{job.id}/events", params={"states": "1"}).json()["states"]
    assert states["w1"]["state"] == "rate_limited"
    # the literal the rule actually matched, and the line it sits on
    assert states["w1"]["matched"] == "Too Many Requests"
    assert states["w1"]["why"] == "429 Too Many Requests" and states["w1"]["seen"] == ["rate_limited"]
    assert states["w1"]["confidence"] == 0.8


# ── live events (SSE) ───────────────────────────────────────────────────────

def _request(disconnected=False):
    async def _is_disconnected():
        return disconnected
    return SimpleNamespace(is_disconnected=_is_disconnected)


def _parse(chunks):
    """(events in order, the `end` payload, heartbeats) out of an SSE stream."""
    events, end, beats = [], None, 0
    for chunk in chunks:
        if chunk.startswith(": heartbeat"):
            beats += 1
        elif chunk.startswith("event: end\ndata: "):
            end = json.loads(chunk[len("event: end\ndata: "):].strip())
        elif chunk.startswith("data: "):
            events.append(json.loads(chunk[len("data: "):].strip()))
    return events, end, beats


@pytest.mark.asyncio
async def test_a_streaming_client_receives_the_events_in_order_and_a_final_end(tmp_path):
    job = _job(tmp_path)
    job._event(event="job", message="checkpointing the workspace")
    chunks = []
    stream = dr.event_stream(_request(), job)

    async def drive():
        await asyncio.sleep(0.05)
        job.note_worker_event({"name": "w1", "event": "started", "session_id": "child-1"})
        await asyncio.sleep(0.05)
        job.note_worker_event({"name": "w1", "event": "round", "round": 2})
        await asyncio.sleep(0.05)
        job.status = "partial"
        job.verdict = "1/2 workers done (error)"
        job._notify()

    task = asyncio.create_task(drive())
    started = time.monotonic()
    async for chunk in stream:
        chunks.append(chunk)
    elapsed = time.monotonic() - started
    await task
    events, end, beats = _parse(chunks)
    assert [e.get("event") for e in events] == ["job", "started", "round"]
    assert [e.get("name") for e in events] == ["job", "w1", "w1"]
    assert end == {"id": job.id, "status": "partial", "verdict": "1/2 workers done (error)", "error": ""}
    assert beats == 0, "events arrived as they happened; no heartbeat was due"
    # the whole exchange took the three 50 ms steps, not a poll interval
    assert elapsed < FAST
    assert job._updates == [], "the stream unsubscribed when it ended"


@pytest.mark.asyncio
async def test_a_finished_job_streams_its_backlog_then_ends_at_once(tmp_path):
    job = _job(tmp_path, status="done")
    job._event(event="job", message="workers running")
    job.verdict = "1/1 workers done"
    chunks = [c async for c in dr.event_stream(_request(), job)]
    events, end, _ = _parse(chunks)
    assert [e["message"] for e in events] == ["workers running"]
    assert end["status"] == "done" and end["verdict"] == "1/1 workers done"


@pytest.mark.asyncio
async def test_a_heartbeat_keeps_an_idle_stream_open(tmp_path, monkeypatch):
    monkeypatch.setattr(dispatch, "STREAM_HEARTBEAT_S", 0.05)
    job = _job(tmp_path)
    stream = dr.event_stream(_request(), job)
    first = await stream.__anext__()
    assert first == ": heartbeat\n\n"
    job.status = "done"
    job._notify()
    assert (await stream.__anext__()).startswith("event: end\ndata: ")
    await stream.aclose()
    assert job._updates == []


@pytest.mark.asyncio
async def test_a_disconnected_client_leaks_nothing(tmp_path):
    job = _job(tmp_path)
    job._event(event="job", message="workers running")
    # the client is already gone: the stream sends the backlog and stops
    chunks = [c async for c in dr.event_stream(_request(disconnected=True), job)]
    assert len(chunks) == 1 and chunks[0].startswith("data: ")
    assert job._updates == []
    # and one that goes away mid-stream (the server closes the generator)
    stream = dr.event_stream(_request(), job)
    await stream.__anext__()
    assert job._updates != []
    await stream.aclose()
    assert job._updates == [], "a client that walked away left a waiter behind"


@pytest.mark.asyncio
async def test_the_stream_is_capped_at_the_jobs_own_ceiling(tmp_path, monkeypatch):
    monkeypatch.setattr(dispatch, "STREAM_MARGIN_S", 0.0)
    job = _job(tmp_path)
    job.ceiling_s = lambda: 0                                   # "its time is up"
    chunks = [c async for c in dr.event_stream(_request(), job)]
    _events, end, _ = _parse(chunks)
    assert end["reason"] == "the stream reached the job's ceiling"
    assert job.status == "running", "the cap ends the STREAM, never the job"
    assert job._updates == []


def test_the_stream_endpoint_answers_as_an_event_stream(monkeypatch, tmp_path):
    job = _job(tmp_path, status="done")
    job._event(event="job", message="workers running")
    client = _client(monkeypatch)
    with client.stream("GET", f"/api/dispatch/{job.id}/events", params={"stream": "1"}) as answer:
        assert answer.status_code == 200
        assert answer.headers["content-type"].startswith("text/event-stream")
        assert answer.headers["cache-control"] == "no-cache" and answer.headers["x-accel-buffering"] == "no"
        body = "".join(answer.iter_text())
    assert '"message": "workers running"' in body or '"message":"workers running"' in body
    assert body.rstrip().endswith("}") and "event: end" in body


def test_new_events_never_replays_and_survives_the_deque_rotating(tmp_path):
    job = _job(tmp_path)
    assert dispatch.new_events(job, 0) == ([], 0)
    job._event(event="job", message="one")
    rows, sent = dispatch.new_events(job, 0)
    assert [r["message"] for r in rows] == ["one"] and sent == 1
    assert dispatch.new_events(job, sent) == ([], 1)
    for i in range(dispatch.EVENTS_KEPT + 10):
        job._event(event="job", message=f"m{i}")
    rows, sent = dispatch.new_events(job, 1)
    # the oldest fell out of the deque; the client is handed the newest it
    # can still be given rather than nothing
    assert len(rows) == dispatch.EVENTS_KEPT and sent == job.events_produced
    assert rows[-1]["message"] == f"m{dispatch.EVENTS_KEPT + 9}"
    assert dispatch.new_events(job, sent + 5) == ([], sent)


# ── the settings' off state is exactly today ────────────────────────────────

def test_state_detection_off_leaves_progress_exactly_as_it_was(monkeypatch, tmp_path):
    job = _job(tmp_path)
    _settings(monkeypatch, agent_worker_state_detection=False)
    job.note_worker_event({"name": "w1", "event": "tick", "round": 2, "elapsed_s": 5,
                           "output": "HTTP 429 Too Many Requests"})
    assert job.worker_states == {} and dispatch.worker_states(job) == {}
    progress = dispatch.compact(job)["progress"]
    assert progress["w1"] == {"last_event": "tick", "round": 2, "elapsed_s": 5}
    assert progress["w2"] == {"last_event": "queued"}
    # the verdict says nothing about states either
    dispatch._settle(job)
    assert "reported (not killed)" not in (job.verdict or "")


@pytest.mark.asyncio
async def test_state_detection_off_means_a_worker_state_wait_simply_never_holds(monkeypatch, tmp_path):
    job = _job(tmp_path)
    _settings(monkeypatch, agent_worker_state_detection=False)
    job.note_worker_event({"name": "w1", "event": "tool", "output": "429 Too Many Requests"})
    answer = await dispatch.wait_for(job, condition="worker:w1:rate_limited", timeout_s=0.2)
    assert answer["met"] is False
    # every other condition still works with the detector off
    job._event(event="job", message="running the verification")
    assert (await dispatch.wait_for(job, condition="phase:verification", timeout_s=1))["met"] is True


def test_both_settings_default_on_and_are_described_for_the_settings_page():
    from src.agent_settings_schema import schema_keys, schema_problems
    from src.settings import DEFAULT_SETTINGS
    assert DEFAULT_SETTINGS["agent_worker_state_detection"] is True
    assert DEFAULT_SETTINGS["agent_dispatch_sse"] is True
    assert dispatch.state_detection_on() is True and dispatch.sse_on() is True
    assert {"agent_worker_state_detection", "agent_dispatch_sse"} <= set(schema_keys())
    assert schema_problems() == []


def test_a_settings_read_that_explodes_does_not_reach_the_job(monkeypatch, tmp_path):
    from src import settings as settings_mod

    def boom(key, default=None):
        raise RuntimeError("settings on fire")

    monkeypatch.setattr(settings_mod, "get_setting", boom)
    job = _job(tmp_path)
    job.note_worker_event({"name": "w1", "event": "tool", "output": "429 Too Many Requests"})
    assert dispatch.state_detection_on() is True          # the default stands
    assert dispatch.compact(job)["progress"]["w1"]["last_event"] == "tool"


# ── detected, reported, never killed ────────────────────────────────────────

def test_a_rate_limited_or_prompting_worker_is_reported_and_left_alone(tmp_path):
    job = _job(tmp_path)
    job.note_worker_event({"name": "w1", "event": "tool", "phase": "progress",
                           "tail": "POST /v1/messages\nHTTP 429 Too Many Requests"})
    job.note_worker_event({"name": "w2", "event": "tool", "phase": "progress",
                           "tail": "rm -rf build/\nOverwrite existing file? [y/N] "})
    progress = dispatch.compact(job)["progress"]
    assert progress["w1"]["state"] == "rate_limited" and "429" in progress["w1"]["why"]
    assert progress["w2"]["state"] == "waiting_for_input" and "[y/N]" in progress["w2"]["why"]
    # nothing was cancelled, nothing was stopped, the job runs on
    assert job.status == "running" and job.task is None
    job.status = "partial"
    dispatch._settle(job)
    assert "reported (not killed): w1 rate_limited, w2 waiting_for_input" in job.verdict


def test_a_state_is_read_from_the_newest_output_and_ages_out(tmp_path):
    job = _job(tmp_path)
    job.note_worker_event({"name": "w1", "event": "tool", "output": "429 Too Many Requests"})
    assert dispatch.compact(job)["progress"]["w1"]["state"] == "rate_limited"
    # the worker got going again: the tail no longer says rate limited, so
    # neither does the board — but the job remembers it happened
    job.note_worker_event({"name": "w1", "event": "tool", "output": "x" * 9000})
    assert "state" not in dispatch.compact(job)["progress"]["w1"]
    assert job.worker_states["w1"]["seen"] == ["rate_limited"]


def test_the_job_events_themselves_are_never_rewritten(tmp_path):
    """Detection reads the events; it must not add a key to them — the events
    answer is the same record it always was."""
    job = _job(tmp_path)
    raw = {"name": "w1", "event": "tool", "output": "429 Too Many Requests"}
    job.note_worker_event(dict(raw))
    assert list(job.events) == [raw]


def test_a_broken_rule_pack_cannot_break_a_running_job(monkeypatch, tmp_path):
    from src import output_rules

    def boom(*a, **kw):
        raise RuntimeError("rules exploded")

    monkeypatch.setattr(output_rules, "classify_output", boom)
    job = _job(tmp_path)
    job.note_worker_event({"name": "w1", "event": "tick", "round": 3, "output": "anything"})
    assert dispatch.compact(job)["progress"]["w1"]["round"] == 3
    assert job.worker_states.get("w1", {}).get("state") is None


# ── the MCP tool for a coordinator ──────────────────────────────────────────

def test_the_mcp_wait_for_tool_exists_and_reads_in_one_glance(monkeypatch):
    from tests.test_dispatch import _load_workers_server
    ws = _load_workers_server(monkeypatch)
    tool = [t for t in ws.TOOLS if t.name == "workers_wait_for"][0]
    assert set(tool.inputSchema["properties"]) == {"job_id", "condition", "timeout_s"}
    for form in ("phase:<name>", "worker:<label>:<state>", "event:<text>", "changed"):
        assert form in tool.description
    assert "never killed" in tool.description

    met = ws.render_wait_for({"met": True, "condition": "worker:w1:rate_limited", "waited_s": 12.5,
                              "state": {"id": "j1", "status": "running", "title": "t", "wait_again": True,
                                        "progress": {"w1": {"last_event": "tick", "state": "rate_limited",
                                                            "why": "429 Too Many Requests"}}}})
    assert "condition 'worker:w1:rate_limited': MET after 12.5 s" in met
    assert "RATE_LIMITED (429 Too Many Requests) — reported, not killed" in met
    missed = ws.render_wait_for({"met": False, "condition": "done", "waited_s": 300.0,
                                 "state": {"id": "j1", "status": "running", "title": "t"}})
    assert "not met after 300.0 s" in missed and "a timeout is not an error" in missed
    # an older Faustus answers the plain job: it still renders
    assert "job j1" in ws.render_wait_for({"id": "j1", "status": "done", "title": "t"})


def test_the_mcp_events_tool_names_the_detected_states(monkeypatch):
    from tests.test_dispatch import _load_workers_server
    ws = _load_workers_server(monkeypatch)
    lines = ws.render_states({"w1": {"state": "stuck", "why": "the same line 4 times at the tail: retrying",
                                     "matched": "retrying", "seen": ["stuck"]},
                              "w2": {"state": "finished_ok", "matched": "exit code 0", "seen": ["finished_ok"]},
                              "w3": {"state": None}})
    assert len(lines) == 2
    assert "state: w1 is stuck" in lines[0] and "matched 'retrying'" in lines[0]
    assert "reported, NOT killed" in lines[0] and "do not re-dispatch" in lines[0]
    assert "reported, NOT killed" not in lines[1]
    assert ws.render_states(None) == [] and ws.render_states("junk") == []
