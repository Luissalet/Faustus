"""Hedged first token (src/model_hedge.py): the race, its wiring into
stream_llm_with_fallback against real HTTP servers, settings and REST."""
import asyncio
import json
import time

import pytest

from src import model_hedge


def _delta(text, *, thinking=False):
    event = {"delta": text}
    if thinking:
        event["thinking"] = True
    return "data: " + json.dumps(event) + "\n\n"


def _meta(kind="model_actual"):
    return "data: " + json.dumps({"type": kind, "model": "m"}) + "\n\n"


class _Stream:
    """A scripted stream that records whether it was closed early."""

    def __init__(self, script):
        self.script = script
        self.closed = False
        self.finished = False

    async def gen(self):
        try:
            for pause, chunk in self.script:
                await asyncio.sleep(pause)
                yield chunk
            self.finished = True
        finally:
            self.closed = True


def _race(primary, hedge, delay, notices=None):
    started = []

    async def start_hedge():
        started.append(time.monotonic())
        return hedge.gen() if hedge is not None else None

    def on_decided(info):
        if notices is not None:
            notices.append(info)
        return "data: " + json.dumps({"type": "hedge", **info}) + "\n\n"

    async def run():
        return [c async for c in model_hedge.race_first_token(
            primary.gen(), start_hedge, delay, on_decided=on_decided)]

    return asyncio.run(run()), started


def _texts(chunks):
    out = []
    for chunk in chunks:
        data = model_hedge._event(chunk)
        if "delta" in data:
            out.append(data["delta"])
    return "".join(out)


def test_a_primary_that_answers_in_time_never_starts_the_hedge():
    primary = _Stream([(0.01, _meta()), (0.01, _delta("hi")), (0.0, _delta(" there"))])
    hedge = _Stream([(0.0, _delta("hedge"))])
    chunks, started = _race(primary, hedge, 0.5)
    assert started == []
    assert _texts(chunks) == "hi there"
    assert not any('"type": "hedge"' in c for c in chunks)
    assert chunks[0] == _meta()


def test_a_stalled_primary_loses_to_the_hedge_and_is_closed():
    primary = _Stream([(0.0, _meta()), (30.0, _delta("late primary"))])
    hedge = _Stream([(0.05, _delta("from hedge")), (0.0, _delta("!"))])
    notices = []
    t0 = time.monotonic()
    chunks, started = _race(primary, hedge, 0.2, notices)
    assert time.monotonic() - t0 < 5
    assert len(started) == 1
    assert _texts(chunks) == "from hedge!"
    assert primary.closed and not primary.finished
    # the primary's held metadata never reaches the caller
    assert _meta() not in chunks
    assert notices and notices[0]["winner"] == "hedge" and notices[0]["outcome"] == "first_token"
    assert notices[0]["loser_cancelled"] is True
    assert 0.15 <= notices[0]["hedge_started_s"] < 2
    assert '"type": "hedge"' in chunks[0]


def test_the_primary_can_still_win_after_the_hedge_started():
    primary = _Stream([(0.3, _delta("primary first")), (0.0, _delta("."))])
    hedge = _Stream([(10.0, _delta("hedge"))])
    notices = []
    chunks, started = _race(primary, hedge, 0.1, notices)
    assert len(started) == 1
    assert _texts(chunks) == "primary first."
    assert hedge.closed and not hedge.finished
    assert notices[0]["winner"] == "primary" and notices[0]["loser_cancelled"] is True


def test_no_hedge_once_the_first_token_was_streamed():
    primary = _Stream([(0.0, _delta("first")), (0.5, _delta(" second"))])
    hedge = _Stream([(0.0, _delta("hedge"))])
    chunks, started = _race(primary, hedge, 0.1)
    assert started == []
    assert _texts(chunks) == "first second"


def test_reasoning_and_tool_call_deltas_count_as_a_first_token():
    tool_delta = "data: " + json.dumps({"type": "tool_call_delta", "name": "web_search"}) + "\n\n"
    for first in (_delta("thinking", thinking=True), tool_delta):
        primary = _Stream([(0.0, first), (0.4, _delta("x"))])
        chunks, started = _race(primary, _Stream([(0.0, _delta("h"))]), 0.1)
        assert started == [] and chunks[0] == first


def test_an_early_primary_error_passes_through_without_a_hedge():
    error = 'event: error\ndata: {"status": 502, "error": "bad gateway"}\n\n'
    primary = _Stream([(0.0, error)])
    chunks, started = _race(primary, _Stream([(0.0, _delta("h"))]), 0.3)
    assert started == [] and chunks == [error]


def test_when_both_fail_the_primary_error_is_reported():
    error = 'event: error\ndata: {"status": 504, "error": "primary"}\n\n'
    other = 'event: error\ndata: {"status": 500, "error": "hedge"}\n\n'
    primary = _Stream([(0.3, error)])
    hedge = _Stream([(0.05, other)])
    notices = []
    chunks, started = _race(primary, hedge, 0.1, notices)
    assert error in chunks and other not in chunks
    assert notices[0]["winner"] == "primary" and notices[0]["outcome"] == "error"


def test_a_primary_error_after_the_hedge_started_lets_the_hedge_answer():
    error = 'event: error\ndata: {"status": 504, "error": "primary"}\n\n'
    primary = _Stream([(0.2, error)])
    hedge = _Stream([(0.3, _delta("saved"))])
    chunks, _ = _race(primary, hedge, 0.1)
    assert _texts(chunks) == "saved" and error not in chunks


# ---------------------------------------------------------------------------
# real HTTP: a vLLM-like server that stalls and one that answers
# ---------------------------------------------------------------------------

class _FakeServer:
    """Minimal OpenAI-compatible streaming server on 127.0.0.1.

    mode="stall": accepts the request and sends only SSE comments (like a
    server holding a tool call) until the client goes away.
    mode="answer": streams a short answer after `delay` seconds."""

    def __init__(self, mode, text="", delay=0.0, *, waiting=None, stall_s=30.0, pause_s=0.0):
        # mode "stall_first": the first request stalls (for stall_s, then
        # answers), every later one answers. waiting: the value its GET
        # /metrics reports as vLLM num_requests_waiting (None: no /metrics).
        # pause_s: pause after the first answered token.
        self.mode, self.text, self.delay = mode, text, delay
        self.waiting, self.stall_s, self.pause_s = waiting, stall_s, pause_s
        self.metrics_reads = 0
        self.requests = 0
        self.bodies = []  # the JSON bodies received, in order
        self.disconnected = asyncio.Event()
        self.server = None
        self.port = None

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/v1/chat/completions"

    async def start(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()

    async def _handle(self, reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            method, path = lines[0].split(" ")[:2]
            length = 0
            for line in lines[1:]:
                if line.lower().startswith("content-length:"):
                    length = int(line.split(":", 1)[1])
            body = await reader.readexactly(length) if length else b""
            if method == "GET" and path == "/metrics" and self.waiting is not None:
                self.metrics_reads += 1
                text = ("# TYPE vllm:num_requests_waiting gauge\n"
                        f'vllm:num_requests_waiting{{model_name="m"}} {float(self.waiting)}\n'
                        f'vllm:num_requests_running{{model_name="m"}} {float(self.requests)}\n').encode()
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: "
                             + str(len(text)).encode() + b"\r\nConnection: close\r\n\r\n" + text)
                await writer.drain()
                return
            if method != "POST" or not path.endswith("/chat/completions"):
                writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                await writer.drain()
                return
            self.requests += 1
            try:
                self.bodies.append(json.loads(body.decode("utf-8") or "{}"))
            except ValueError:
                self.bodies.append({})
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                         b"Cache-Control: no-cache\r\nConnection: close\r\n\r\n")
            await writer.drain()
            if self.mode == "stall":
                await self._stall(reader, writer)
            elif self.mode == "stall_first" and self.requests == 1:
                if await self._stall(reader, writer, seconds=self.stall_s):
                    await self._answer(writer, self.bodies[-1].get("model") or "m")
            else:
                await self._answer(writer, self.bodies[-1].get("model") or "m")
        except (ConnectionError, asyncio.IncompleteReadError):
            self.disconnected.set()
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _stall(self, reader, writer, seconds=30.0):
        """Hold the request; True when the client is still there afterwards."""
        eof = asyncio.ensure_future(reader.read(1))
        try:
            for _ in range(int(seconds / 0.05)):
                if eof.done():
                    self.disconnected.set()
                    return False
                writer.write(b": holding\n\n")
                await writer.drain()
                await asyncio.sleep(0.05)
            return True
        except ConnectionError:
            self.disconnected.set()
            return False
        finally:
            eof.cancel()

    async def _answer(self, writer, model="m"):
        await asyncio.sleep(self.delay)

        def frame(delta, finish=None):
            # the server reports the model it was asked for, as vLLM does
            body = {"id": "x", "object": "chat.completion.chunk", "model": model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            return ("data: " + json.dumps(body) + "\n\n").encode()

        writer.write(frame({"role": "assistant"}))
        for n, word in enumerate(self.text.split(" ")):
            writer.write(frame({"content": word + " "}))
            await writer.drain()
            await asyncio.sleep(self.pause_s if n == 0 and self.pause_s else 0.01)
        writer.write(frame({}, "stop"))
        writer.write(b"data: [DONE]\n\n")
        await writer.drain()


@pytest.fixture
def hedge_env(monkeypatch, tmp_path):
    from src import llm_core
    monkeypatch.setenv("ODYSSEUS_LOCAL_MODEL_GATE", "false")
    monkeypatch.setattr(model_hedge, "_log_path", lambda: str(tmp_path / "model_hedges.jsonl"))
    monkeypatch.setattr(model_hedge, "_RECENT", model_hedge.deque(maxlen=200))
    monkeypatch.setattr(model_hedge, "_REISSUES", {})
    # the queue is read from the fake server's own /metrics, never Prometheus
    monkeypatch.setattr(model_hedge, "_serving_capacity", lambda url: None)
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda u: False)
    monkeypatch.setattr(llm_core, "is_sparks_url", lambda u: False)
    state = {"cfg": {"enabled": True, "delay_s": 0.3, "target": "auto", "delay_overrides": {}},
             "service": {"endpoint_ids": set(), "servers": set()}}
    monkeypatch.setattr(model_hedge, "config", lambda: dict(state["cfg"]))
    monkeypatch.setattr(model_hedge, "global_service", lambda: state["service"])
    return state


def _wire_same_model(monkeypatch, other):
    base = model_hedge._base(other.url)
    monkeypatch.setattr(model_hedge, "global_service", lambda: {"endpoint_ids": {"ep-b"}, "servers": set()})
    monkeypatch.setattr(model_hedge, "endpoint_models", lambda *a, **k: [
        {"endpoint_id": "ep-b", "label": "Spark B", "base": base, "models": ["m"]}])
    monkeypatch.setattr(model_hedge, "_resolve", lambda ep, model: (
        (other.url, model, {}), {"endpoint_id": "ep-b", "endpoint_label": "Spark B"}))


def _collect(candidates, **kw):
    from src import llm_core

    async def run():
        return [c async for c in llm_core.stream_llm_with_fallback(
            candidates, [{"role": "user", "content": "hi"}], max_retries=0, **kw)]
    return run()


def _events(chunks):
    out = []
    for chunk in chunks:
        for line in chunk.splitlines():
            if line.startswith("data: ") and line[6:] != "[DONE]":
                try:
                    out.append(json.loads(line[6:]))
                except ValueError:
                    pass
    return out


def test_http_stalled_primary_is_answered_by_the_same_model_elsewhere(hedge_env, monkeypatch):
    from src import llm_trace

    async def scenario():
        stall = await _FakeServer("stall").start()
        answer = await _FakeServer("answer", "answered by the hedge").start()
        _wire_same_model(monkeypatch, answer)
        token = llm_trace.set_current_run_id("run-hedge-1")
        try:
            t0 = time.monotonic()
            chunks = await _collect([(stall.url, "m", {})])
            elapsed = time.monotonic() - t0
            await asyncio.wait_for(stall.disconnected.wait(), timeout=5)
        finally:
            llm_trace.reset_current_run_id(token)
            await stall.stop()
            await answer.stop()
        return chunks, elapsed, stall, answer

    chunks, elapsed, stall, answer = asyncio.run(scenario())
    events = _events(chunks)
    text = "".join(e.get("delta", "") for e in events if "delta" in e and not e.get("thinking"))
    assert text.strip() == "answered by the hedge"
    assert elapsed < 10
    assert stall.requests == 1 and answer.requests == 1
    assert stall.disconnected.is_set()  # the loser's HTTP stream was closed
    hedge = [e for e in events if e.get("type") == "hedge"]
    assert len(hedge) == 1
    assert hedge[0]["winner"] == "hedge" and hedge[0]["outcome"] == "first_token"
    assert hedge[0]["hedge_endpoint_label"] == "Spark B" and hedge[0]["answered_by"] == "m"
    assert hedge[0]["hedge_started_s"] >= 0.25
    # the notice comes before the first answered token
    first_delta = next(i for i, e in enumerate(events) if "delta" in e)
    assert events.index(hedge[0]) < first_delta
    runs = model_hedge.for_run("run-hedge-1")
    assert len(runs) == 1 and runs[0]["winner"] == "hedge"
    logged = model_hedge._load_log(10)
    assert logged and logged[-1]["hedge_endpoint_id"] == "ep-b"


def test_http_primary_that_answers_in_time_sends_nothing_to_the_hedge(hedge_env, monkeypatch):
    # The premise is a primary that answers before the delay: a cold first
    # request to a local server can take a few hundred ms on a busy machine,
    # so the delay here leaves room for it (the stalled cases use 0.3 s).
    hedge_env["cfg"]["delay_s"] = 1.5

    async def scenario():
        fast = await _FakeServer("answer", "primary answer").start()
        other = await _FakeServer("answer", "should not run").start()
        _wire_same_model(monkeypatch, other)
        try:
            t0 = time.monotonic()
            chunks = await _collect([(fast.url, "m", {})])
            assert time.monotonic() - t0 < 1.5  # it did answer in time
            await asyncio.sleep(1.7)  # past the delay: still nothing is started
        finally:
            await fast.stop()
            await other.stop()
        return chunks, other

    chunks, other = asyncio.run(scenario())
    events = _events(chunks)
    assert "".join(e.get("delta", "") for e in events if "delta" in e).strip() == "primary answer"
    assert other.requests == 0
    assert not [e for e in events if e.get("type") == "hedge"]


def test_background_work_is_never_hedged(hedge_env, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("plan must not be consulted for background work")
    monkeypatch.setattr(model_hedge, "plan", boom)

    async def scenario():
        fast = await _FakeServer("answer", "ok").start()
        try:
            return await _collect([(fast.url, "m", {})], workload="background")
        finally:
            await fast.stop()

    events = _events(asyncio.run(scenario()))
    assert "".join(e.get("delta", "") for e in events if "delta" in e).strip() == "ok"


# ---------------------------------------------------------------------------
# targets, delays, settings, REST
# ---------------------------------------------------------------------------

def _cfg(**kw):
    return {"enabled": True, "delay_s": 20.0, "target": "auto", "delay_overrides": {}, **kw}


def test_target_resolution(monkeypatch):
    monkeypatch.setattr(model_hedge, "_host_dead", lambda u: False)
    # every route of this test is on the global service (the mixed case has its own test)
    monkeypatch.setattr(model_hedge, "global_service", lambda: {
        "endpoint_ids": {"a", "b", "c", "self"}, "servers": {"http://b:9000", "http://c:8000"}})
    monkeypatch.setattr(model_hedge, "endpoint_models", lambda *a, **k: [])
    primary = ("http://a:8000/v1/chat/completions", "m", {})
    fallback = ("http://b:9000/v1/chat/completions", "other", {"Authorization": "x"})
    descs = [{"endpoint_id": "a"}, {"endpoint_id": "b", "endpoint_label": "B"}]
    # nothing known: auto falls back to the first fallback of the chain
    found = model_hedge.plan(*primary, candidates=[primary, fallback], descriptors=descs, cfg=_cfg())
    assert found["kind"] == "fallback" and found["candidate_index"] == 1 and found["model"] == "other"
    assert model_hedge.plan(*primary, candidates=[primary], cfg=_cfg()) is None
    assert model_hedge.plan(*primary, candidates=[primary, fallback], cfg=_cfg(target="same_model")) is None
    assert model_hedge.plan(*primary, candidates=[primary, fallback], cfg=_cfg(target="off")) is None
    assert model_hedge.plan(*primary, candidates=[primary, fallback], cfg=_cfg(enabled=False)) is None
    # the same model on another endpoint wins over the fallback
    monkeypatch.setattr(model_hedge, "endpoint_models", lambda *a, **k: [
        {"endpoint_id": "self", "label": "A", "base": "http://a:8000/v1", "models": ["m"]},
        {"endpoint_id": "c", "label": "C", "base": "http://c:8000/v1", "models": ["M"]}])
    monkeypatch.setattr(model_hedge, "_resolve", lambda ep, model: (
        (primary[0] if ep == "self" else f"http://{ep}:8000/v1/chat/completions", model, {}),
        {"endpoint_id": ep, "endpoint_label": ep.upper()}))
    found = model_hedge.plan(*primary, candidates=[primary, fallback], descriptors=descs, cfg=_cfg())
    assert found["kind"] == "same_model" and found["endpoint_id"] == "c" and found["candidate_index"] is None
    # a named model
    found = model_hedge.plan(*primary, cfg=_cfg(target="c::big"))
    assert found["kind"] == "named" and found["model"] == "big"
    # the primary itself is never its own hedge
    assert model_hedge.plan(*primary, cfg=_cfg(target="self")) is None
    # an official-client route is not hedged
    assert model_hedge.plan("faustus-cli://x", "m", cfg=_cfg()) is None


def test_delay_per_model_and_bounds():
    cfg = _cfg(delay_overrides=model_hedge.parse_overrides('{"DeepSeek-V4.1-Flash": 12, "bad": "x", "low": 0}'))
    assert model_hedge.delay_for("deepseek-v4.1-flash", cfg) == 12
    assert model_hedge.delay_for("other", cfg) == 20.0
    assert model_hedge.delay_for("low", cfg) == model_hedge.MIN_DELAY_S
    assert model_hedge.parse_overrides("not json") == {}


def test_settings_are_declared_on_the_agent_screen():
    from src.agent_settings_schema import build_schema, schema_problems
    from src.settings import DEFAULT_SETTINGS
    assert schema_problems() == []
    fields = {f["key"]: f for g in build_schema()["groups"] for f in g["fields"]}
    for key in model_hedge.SETTING_KEYS:
        assert key in DEFAULT_SETTINGS and key in fields
    assert DEFAULT_SETTINGS["agent_hedge_enabled"] is True
    assert DEFAULT_SETTINGS["agent_hedge_delay_s"] == 20.0
    assert DEFAULT_SETTINGS["agent_hedge_target"] == "auto"


def test_update_validates_and_writes(monkeypatch):
    written = {}
    monkeypatch.setattr("src.settings.update_settings", lambda patch: written.update(patch))
    monkeypatch.setattr(model_hedge, "status", lambda limit=20: {"ok": True})
    monkeypatch.setattr(model_hedge, "endpoint_models", lambda *a, **k: [
        {"endpoint_id": "ep", "label": "EP", "base": "http://x/v1", "models": ["m"]}])
    monkeypatch.setattr(model_hedge, "_endpoint_rows", lambda enabled_only=True: [])
    model_hedge.update({"enabled": False, "delay_s": 15, "target": "ep::m",
                        "delay_overrides": {"m": 30}})
    assert written == {"agent_hedge_enabled": False, "agent_hedge_delay_s": 15.0,
                       "agent_hedge_target": "ep::m", "agent_hedge_delay_overrides": '{"m": 30.0}'}
    for bad in ({"delay_s": 0}, {"target": "nope::m"}, {"target": "ep::zzz"}, {}):
        with pytest.raises(ValueError):
            model_hedge.update(bad)


def test_rest_and_agent_tool(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes import model_hedge_routes
    from src.agent_tools import TOOL_HANDLERS
    monkeypatch.setattr(model_hedge_routes, "require_admin", lambda request: None)
    state = {"enabled": True, "delay_s": 20.0, "target": "auto", "delay_overrides": {},
             "current": None, "targets": [], "recent": [], "stats": {"races": 0, "hedge_wins": 0}}
    monkeypatch.setattr(model_hedge, "status", lambda limit=20: dict(state))

    def fake_update(patch):
        if "delay_s" in patch and not 1 <= float(patch["delay_s"]) <= 600:
            raise ValueError("delay_s must be between 1 and 600 seconds")
        state.update(patch)
        return dict(state)
    monkeypatch.setattr(model_hedge, "update", fake_update)
    app = FastAPI()
    app.include_router(model_hedge_routes.setup_model_hedge_routes())
    client = TestClient(app)
    assert client.get("/api/model-hedge").json()["target"] == "auto"
    assert client.patch("/api/model-hedge", json={"delay_s": 12, "target": "off"}).json()["delay_s"] == 12
    assert client.post("/api/model-hedge", json={"delay_s": 0}).status_code == 400
    result = asyncio.run(TOOL_HANDLERS["model_hedge"](json.dumps({"action": "set", "enabled": False}), {}))
    assert result["exit_code"] == 0 and state["enabled"] is False
    result = asyncio.run(TOOL_HANDLERS["model_hedge"]("status", {}))
    assert "Hedged first token: off" in result["output"]


def test_the_turn_metrics_carry_the_hedges_of_their_run(monkeypatch, tmp_path):
    from src import llm_trace
    from src.agent_loop import _compute_final_metrics
    monkeypatch.setattr(model_hedge, "_log_path", lambda: str(tmp_path / "h.jsonl"))
    monkeypatch.setattr(model_hedge, "_RECENT", model_hedge.deque(maxlen=200))
    token = llm_trace.set_current_run_id("run-metrics")
    try:
        model_hedge.record({"winner": "hedge", "outcome": "first_token", "after_s": 23.1, "delay_s": 20,
                            "hedge_started_s": 20.0, "loser_cancelled": True},
                           {"kind": "same_model", "model": "m", "endpoint_id": "b", "endpoint_label": "Spark B"},
                           primary_url="http://a/v1/chat/completions", primary_model="m")
        metrics = _compute_final_metrics([{"role": "user", "content": "hi"}], "answer", 25.0, 23.1, 8192,
                                         10, 5, True, [], ["answer"], model="m")
    finally:
        llm_trace.reset_current_run_id(token)
    assert metrics["hedges"][0]["answered_by_endpoint_label"] == "Spark B"
    assert metrics["hedges"][0]["after_s"] == 23.1


# ---------------------------------------------------------------------------
# review 2037: preparing the hedge never stalls the primary
# ---------------------------------------------------------------------------

def test_a_slow_hedge_preparation_never_delays_a_primary_that_answers():
    """The hedge is prepared in its own task: the primary answering during a
    slow preparation is delivered at once and the preparation is cancelled."""
    marks = {}
    prep = {"cancelled": False, "finished": False}
    notices = []

    async def primary():
        await asyncio.sleep(0.15)
        marks["primary_ready"] = time.monotonic()
        yield _delta("primary")

    async def start_hedge():
        try:
            await asyncio.sleep(2.0)  # e.g. context preparation for the hedge route
        except asyncio.CancelledError:
            prep["cancelled"] = True
            raise
        prep["finished"] = True
        return _Stream([(0.0, _delta("hedge"))]).gen()

    async def run():
        chunks = []
        async for chunk in model_hedge.race_first_token(
                primary(), start_hedge, 0.05, on_decided=lambda info: notices.append(info)):
            if "first" not in marks and "delta" in model_hedge._event(chunk):
                marks["first"] = time.monotonic()
            chunks.append(chunk)
        return chunks

    t0 = time.monotonic()
    chunks = asyncio.run(run())
    assert _texts(chunks) == "primary"
    # delivered as soon as the primary had it, not after the 2 s preparation
    assert marks["first"] - marks["primary_ready"] < 0.05
    assert time.monotonic() - t0 < 1.0
    assert prep["cancelled"] is True and prep["finished"] is False
    assert notices and notices[0]["winner"] == "primary"
    assert notices[0]["hedge_ready_s"] is None and notices[0]["loser_cancelled"] is True


def test_cancelling_the_caller_cancels_a_hedge_still_being_prepared():
    prep = {"cancelled": False}
    primary = _Stream([(30.0, _delta("never"))])

    async def start_hedge():
        try:
            await asyncio.sleep(30.0)
        except asyncio.CancelledError:
            prep["cancelled"] = True
            raise
        return None

    async def consume():
        async for _ in model_hedge.race_first_token(primary.gen(), start_hedge, 0.05):
            pass

    async def run():
        task = asyncio.create_task(consume())
        await asyncio.sleep(0.3)  # past the delay: the preparation is running
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert prep["cancelled"] is True
    assert primary.closed and not primary.finished


def test_a_failed_hedge_preparation_leaves_the_primary_and_records_nothing():
    notices = []
    primary = _Stream([(0.3, _delta("primary"))])

    async def start_hedge():
        await asyncio.sleep(0.01)
        raise RuntimeError("no route state")

    async def run():
        return [c async for c in model_hedge.race_first_token(
            primary.gen(), start_hedge, 0.05, on_decided=lambda info: notices.append(info))]

    assert _texts(asyncio.run(run())) == "primary"
    assert notices == []


# ---------------------------------------------------------------------------
# review 2037: a named hedge route outside the chain is prepared and adopted
# ---------------------------------------------------------------------------

def test_http_named_model_outside_the_chain_gets_its_own_request_and_is_adopted(hedge_env, monkeypatch):
    """endpoint::model hedge that is not in the chain: the candidate factory
    prepares its own request (its own max_tokens and messages) under the next
    index, and when it answers a fallback event names that route and model even
    though the server reports exactly the model it was asked for."""
    hedge_env["service"] = {"endpoint_ids": {"ep-named"}, "servers": set()}
    hedge_env["cfg"]["target"] = "ep-named::named-model"
    prepared = []

    async def factory(index, url, model, headers):
        prepared.append((index, model, dict(headers)))
        own = 123 if model == "primary-model" else 7
        return {"messages": [{"role": "user", "content": f"prepared for {model}"}],
                "kwargs": {"max_tokens": own}}

    async def scenario():
        stall = await _FakeServer("stall").start()
        answer = await _FakeServer("answer", "named route answered").start()
        monkeypatch.setattr(model_hedge, "_resolve", lambda ep, model: (
            (answer.url, model, {"Authorization": "Bearer named"}),
            {"endpoint_id": "ep-named", "endpoint_label": "Spark N", "connection_revision": "rev-7",
             "endpoint_cost_tracked": False}))
        try:
            chunks = await _collect(
                [(stall.url, "primary-model", {})], candidate_request_factory=factory,
                candidate_route_descriptors=[{"endpoint_id": "ep-primary", "endpoint_label": "Primary"}])
            await asyncio.wait_for(stall.disconnected.wait(), timeout=5)
        finally:
            await stall.stop()
            await answer.stop()
        return chunks, stall, answer

    chunks, stall, answer = asyncio.run(scenario())
    events = _events(chunks)
    # each route was prepared for itself: the hedge under the next free index
    assert prepared == [(0, "primary-model", {}), (1, "named-model", {"Authorization": "Bearer named"})]
    assert stall.bodies[0]["model"] == "primary-model" and stall.bodies[0]["max_tokens"] == 123
    assert answer.bodies[0]["model"] == "named-model" and answer.bodies[0]["max_tokens"] == 7
    assert answer.bodies[0]["messages"][-1]["content"] == "prepared for named-model"
    text = "".join(e.get("delta", "") for e in events if "delta" in e and not e.get("thinking"))
    assert text.strip() == "named route answered"
    # the provider reported the model it was asked for: no model_actual of its own
    assert not [e for e in events if e.get("type") == "model_actual"]
    hedge = [e for e in events if e.get("type") == "hedge"]
    fallback = [e for e in events if e.get("type") == "fallback"]
    assert len(hedge) == 1 and hedge[0]["winner"] == "hedge" and hedge[0]["answered_by"] == "named-model"
    assert len(fallback) == 1
    assert fallback[0]["hedge"] is True and fallback[0]["candidate_index"] == 1
    assert fallback[0]["selected_model"] == "primary-model" and fallback[0]["answered_by"] == "named-model"
    assert fallback[0]["answered_by_endpoint_id"] == "ep-named"
    assert fallback[0]["answered_by_endpoint_label"] == "Spark N"
    assert fallback[0]["answered_by_connection_revision"] == "rev-7"
    assert fallback[0]["answered_by_endpoint_cost_tracked"] is False
    # announcements come before the first answered token
    first_delta = next(i for i, e in enumerate(events) if "delta" in e)
    assert events.index(hedge[0]) < events.index(fallback[0]) < first_delta


def test_agent_run_adopts_a_hedge_route_that_was_not_in_the_chain(monkeypatch):
    """The agent loop prepares the hedge route (index len(chain)) through its
    own candidate factory and, when the fallback event names that index, pins
    that route and model for the rest of the run like any fallback."""
    import src.agent_loop as agent_loop

    primary = ("https://selected.example/v1", "selected-model", {})
    hedge_route = ("https://spark.example/v1", "hedge-model", {"Authorization": "spark"})
    seen_candidates, route_modes = [], []
    round_number = 0

    monkeypatch.setattr(agent_loop, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda *args, **kwargs: 10)
    monkeypatch.setattr(agent_loop, "blocked_tools_for_owner", lambda owner: set())
    monkeypatch.setattr(
        agent_loop, "_agent_route_tool_mode",
        lambda url, model, owner=None, headers=None: route_modes.append((url, model, headers))
        or (model == "selected-model", False, False))

    async def fake_stream(candidates, messages, **kwargs):
        nonlocal round_number
        round_number += 1
        seen_candidates.append(list(candidates))
        factory = kwargs["candidate_request_factory"]
        if round_number == 1:
            await factory(0, *candidates[0])
            # the hedge route is not in the chain: prepared under the next index
            request = await factory(len(candidates), *hedge_route)
            assert request["messages"]
            yield "data: " + json.dumps({
                "type": "hedge", "winner": "hedge", "outcome": "first_token", "after_s": 20.4,
                "hedge_endpoint_id": "ep-spark", "hedge_endpoint_label": "Spark 4",
                "answered_by": "hedge-model", "answered_by_endpoint_label": "Spark 4"}) + "\n\n"
            yield "data: " + json.dumps({
                "type": "fallback", "hedge": True, "selected_model": "selected-model",
                "answered_by": "hedge-model", "candidate_index": len(candidates),
                "answered_by_endpoint_id": "ep-spark", "answered_by_endpoint_label": "Spark 4",
                "answered_by_endpoint_cost_tracked": False,
                "answered_by_connection_revision": "rev-1", "failures": []}) + "\n\n"
            call = {"name": "bash", "arguments": json.dumps({"command": "printf ok"})}
            yield "data: " + json.dumps({"type": "tool_calls", "calls": [call]}) + "\n\n"
        else:
            await factory(0, *candidates[0])
            yield "data: " + json.dumps({"delta": "done"}) + "\n\n"
        yield "data: [DONE]\n\n"

    async def fake_execute(block, *args, **kwargs):
        return "bash", {"output": "ok", "exit_code": 0}

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", fake_stream)
    monkeypatch.setattr(agent_loop, "execute_tool_block", fake_execute)

    async def run():
        return [c async for c in agent_loop.stream_agent_loop(
            primary[0], primary[1], [{"role": "user", "content": "Run one tool and report back."}],
            max_rounds=3, relevant_tools={"bash"}, headers={}, fallbacks=[], _is_teacher_run=True)]

    chunks = asyncio.run(run())
    # round 2 goes to the route that answered, not back to the stalled primary
    assert seen_candidates == [[primary], [hedge_route]]
    # the hedge route got its own route state (tool transport) before it was sent
    assert ("https://spark.example/v1", "hedge-model", {"Authorization": "spark"}) in route_modes
    fallback = json.loads(next(c for c in chunks if '"type": "fallback"' in c)[6:])
    assert fallback["pinned_for_run"] is True and fallback["round"] == 1
    metrics = json.loads(next(c for c in chunks if '"type": "metrics"' in c)[6:])["data"]
    assert metrics["requested_model"] == "selected-model"
    assert metrics["model"] == "hedge-model"
    assert metrics["round_models"] == ["hedge-model", "hedge-model"]
    assert metrics["round_endpoint_ids"][0] == "ep-spark"
    assert metrics["tool_events"][0]["model"] == "hedge-model"


# ---------------------------------------------------------------------------
# review 2037: hedges stay on the global LLM service (the Sparks)
# ---------------------------------------------------------------------------

_MIXED_ROWS = [
    # published by Sparks recipes
    {"endpoint_id": "sparks-a", "label": "Sparks A", "base_url": "http://10.0.0.5:8888/v1",
     "enabled": True, "models": ["m"]},
    {"endpoint_id": "sparks-b", "label": "Sparks B (recipe, stopped)", "base_url": "http://10.0.0.9:30001/v1",
     "enabled": False, "models": []},
    {"endpoint_id": "sparks-c", "label": "Sparks C", "base_url": "http://10.0.0.7:8000/v1",
     "enabled": True, "models": ["m"]},
    # a hand-made endpoint pointing at the server of sparks-b: compatible
    {"endpoint_id": "spark4", "label": "Spark 4", "base_url": "http://10.0.0.9:30001/v1",
     "enabled": True, "models": ["m2"]},
    # a local text model serving the same model name: never a hedge
    {"endpoint_id": "loc", "label": "llama.cpp (local)", "base_url": "http://127.0.0.1:8081/v1",
     "enabled": True, "models": ["m", "m2"]},
]


def _mixed_inventory(monkeypatch, rows=None):
    rows = list(_MIXED_ROWS if rows is None else rows)
    monkeypatch.setattr(model_hedge, "_load_settings", lambda: {
        "sparks_enabled": True,
        "sparks_endpoints": {"rec-a": "sparks-a", "rec-b": "sparks-b", "rec-c": "sparks-c"}})
    monkeypatch.setattr(model_hedge, "_endpoint_rows", lambda enabled_only=True: [
        r for r in rows if r["enabled"] or not enabled_only])
    monkeypatch.setattr(model_hedge, "_host_dead", lambda u: False)
    by_id = {r["endpoint_id"]: r for r in rows}

    def resolve(endpoint_id, model):
        row = by_id.get(endpoint_id)
        if not row or not row["enabled"] or model not in row["models"]:
            return None
        return ((row["base_url"] + "/chat/completions", model, {}),
                {"endpoint_id": endpoint_id, "endpoint_label": row["label"], "connection_revision": ""})
    monkeypatch.setattr(model_hedge, "_resolve", resolve)


def test_mixed_inventory_auto_picks_only_global_endpoints(monkeypatch):
    _mixed_inventory(monkeypatch)
    service = model_hedge.global_service()
    assert service["endpoint_ids"] == {"sparks-a", "sparks-b", "sparks-c"}
    # the stopped recipe's server still marks the hand-made Spark endpoint as global
    assert "http://10.0.0.9:30001" in service["servers"]
    assert "http://127.0.0.1:8081" not in service["servers"]
    listed = {ep["endpoint_id"] for ep in model_hedge.endpoint_models()}
    assert listed == {"sparks-a", "sparks-c", "spark4"}
    primary = ("http://10.0.0.5:8888/v1/chat/completions", "m", {})
    found = model_hedge.plan(*primary, cfg=_cfg())
    assert found["endpoint_id"] == "sparks-c" and found["kind"] == "same_model"
    assert not found["url"].startswith("http://127.0.0.1")


def test_mixed_inventory_never_falls_back_to_a_local_text_model(monkeypatch):
    # without sparks-c, the only other server listing "m" is the local one
    _mixed_inventory(monkeypatch, [r for r in _MIXED_ROWS if r["endpoint_id"] != "sparks-c"])
    primary = ("http://10.0.0.5:8888/v1/chat/completions", "m", {})
    local = ("http://127.0.0.1:8081/v1/chat/completions", "m", {})
    spark4 = ("http://10.0.0.9:30001/v1/chat/completions", "m2", {})
    # auto ends in a re-send to the primary's own (global) server, never the local one
    found = model_hedge.plan(*primary, cfg=_cfg())
    assert found["kind"] == "reissue" and found["url"] == primary[0]
    assert model_hedge.plan(*primary, cfg=_cfg(target="same_model")) is None
    assert model_hedge.plan(*primary, cfg=_cfg(target="auto", reissue_max=0)) is None
    # a local fallback of the chain is skipped; a global one after it is used
    descs = [{"endpoint_id": "sparks-a"}, {"endpoint_id": "loc"}, {"endpoint_id": "spark4"}]
    found = model_hedge.plan(*primary, candidates=[primary, local], descriptors=descs[:2], cfg=_cfg())
    assert found["kind"] == "reissue" and found["endpoint_id"] == "sparks-a"
    found = model_hedge.plan(*primary, candidates=[primary, local, spark4], descriptors=descs,
                             cfg=_cfg(target="fallback"))
    assert found["candidate_index"] == 2 and found["endpoint_id"] == "spark4"


def test_mixed_inventory_named_targets_must_be_global(monkeypatch):
    _mixed_inventory(monkeypatch)
    primary = ("http://10.0.0.5:8888/v1/chat/completions", "m", {})
    assert model_hedge.plan(*primary, cfg=_cfg(target="loc::m")) is None
    assert model_hedge.plan(*primary, cfg=_cfg(target="loc")) is None
    found = model_hedge.plan(*primary, cfg=_cfg(target="spark4::m2"))
    assert found["endpoint_id"] == "spark4" and found["kind"] == "named"
    with pytest.raises(ValueError, match="global LLM service"):
        model_hedge.validate_target("loc::m")
    assert model_hedge.validate_target("spark4::m2") == "spark4::m2"
    values = {o["value"] for o in model_hedge.target_options()}
    assert "spark4::m2" in values and "sparks-c::m" in values
    assert not any(v.startswith("loc::") for v in values)


def test_no_global_service_means_no_hedge(monkeypatch):
    _mixed_inventory(monkeypatch)
    monkeypatch.setattr(model_hedge, "_load_settings", lambda: {"sparks_enabled": False,
                                                                "sparks_endpoints": {"rec-a": "sparks-a"}})
    primary = ("http://10.0.0.5:8888/v1/chat/completions", "m", {})
    assert model_hedge.endpoint_models() == []
    assert model_hedge.plan(*primary, cfg=_cfg()) is None
    assert model_hedge.plan(*primary, cfg=_cfg(target="spark4::m2")) is None


# ---------------------------------------------------------------------------
# reissue: the same request re-sent to the same server
# ---------------------------------------------------------------------------

def _wire_reissue(hedge_env, server):
    hedge_env["cfg"]["target"] = "reissue"
    hedge_env["service"] = {"endpoint_ids": set(), "servers": {model_hedge._server(server.url)}}


def test_http_reissue_answers_a_held_request_and_closes_it(hedge_env, monkeypatch):
    from src import llm_trace

    async def scenario():
        server = await _FakeServer("stall_first", "answered by the re-send", waiting=0).start()
        _wire_reissue(hedge_env, server)
        token = llm_trace.set_current_run_id("run-reissue-1")
        try:
            t0 = time.monotonic()
            chunks = await _collect([(server.url, "m", {})], max_tokens=321)
            elapsed = time.monotonic() - t0
            await asyncio.wait_for(server.disconnected.wait(), timeout=5)
        finally:
            llm_trace.reset_current_run_id(token)
            await server.stop()
        return chunks, elapsed, server

    chunks, elapsed, server = asyncio.run(scenario())
    events = _events(chunks)
    text = "".join(e.get("delta", "") for e in events if "delta" in e and not e.get("thinking"))
    assert text.strip() == "answered by the re-send"
    assert elapsed < 10
    # two HTTP requests to the same server, identical bodies (messages, tools, limits)
    assert server.requests == 2
    assert server.bodies[0] == server.bodies[1]
    assert server.bodies[0]["messages"] and "321" in json.dumps(server.bodies[0])
    assert server.metrics_reads == 1  # the queue was checked before re-sending
    # the held request's HTTP connection was closed, so the server aborts it
    assert server.disconnected.is_set()
    notices = [e for e in events if e.get("type") == "hedge"]
    assert len(notices) == 1
    assert notices[0]["winner"] == "hedge" and notices[0]["outcome"] == "first_token"
    assert notices[0]["kind"] == "reissue" and notices[0]["hedge_kind"] == "reissue"
    assert notices[0]["loser_cancelled"] is True
    # same route and model: no fallback event to adopt another route
    assert not [e for e in events if e.get("type") == "fallback"]
    runs = model_hedge.for_run("run-reissue-1")
    assert len(runs) == 1 and runs[0]["hedge_kind"] == "reissue"


def test_http_reissue_is_skipped_when_the_server_has_a_queue(hedge_env, monkeypatch):
    from src import llm_trace

    async def scenario():
        # the first request is held for 1 s and then answers; the server
        # reports two requests waiting, so the copy would only queue
        server = await _FakeServer("stall_first", "primary after all", waiting=2, stall_s=1.0).start()
        _wire_reissue(hedge_env, server)
        token = llm_trace.set_current_run_id("run-reissue-2")
        try:
            chunks = await _collect([(server.url, "m", {})])
        finally:
            llm_trace.reset_current_run_id(token)
            await server.stop()
        return chunks, server

    chunks, server = asyncio.run(scenario())
    events = _events(chunks)
    assert "".join(e.get("delta", "") for e in events if "delta" in e).strip() == "primary after all"
    assert server.requests == 1 and server.metrics_reads == 1
    assert not [e for e in events if e.get("type") == "hedge"]
    runs = model_hedge.for_run("run-reissue-2")
    assert len(runs) == 1
    skip = runs[0]
    assert skip["outcome"] == "skipped" and skip["skip_reason"] == "saturated"
    assert skip["server_waiting"] == 2 and skip["kind"] == "reissue"
    assert 0.25 <= skip["after_s"] < 1.0
    stats = model_hedge.stats(runs)
    assert stats["races"] == 0 and stats["reissue_skips"] == 1 and stats["reissue_skips_saturated"] == 1


def test_http_no_reissue_after_the_first_token(hedge_env, monkeypatch):
    async def scenario():
        # first token at once, then a pause longer than the delay
        # The delay must outlast the first (cold) request on a loaded box, or
        # the premise "the first token came before the delay" does not hold.
        hedge_env["cfg"]["delay_s"] = 1.5
        server = await _FakeServer("answer", "first then the rest", waiting=0, pause_s=2.5).start()
        _wire_reissue(hedge_env, server)
        try:
            chunks = await _collect([(server.url, "m", {})])
        finally:
            await server.stop()
        return chunks, server

    chunks, server = asyncio.run(scenario())
    events = _events(chunks)
    assert "".join(e.get("delta", "") for e in events if "delta" in e).strip() == "first then the rest"
    assert server.requests == 1 and server.metrics_reads == 0
    assert not [e for e in events if e.get("type") == "hedge"]
    assert model_hedge.recent(10) == []


def test_reissue_gate_budget_per_run_and_per_model(monkeypatch):
    from src import llm_trace
    monkeypatch.setattr(model_hedge, "_REISSUES", {})
    monkeypatch.setattr(model_hedge, "server_capacity", lambda url: {
        "known": True, "waiting": 0.0, "running": 1.0, "saturated": False, "source": "test"})
    url = "http://10.0.0.5:8888/v1/chat/completions"
    cfg = _cfg(reissue_max=1, reissue_overrides={"Big": 2, "never": 0})
    token = llm_trace.set_current_run_id("run-a")
    try:
        assert model_hedge.reissue_gate(url, "m", cfg=cfg)["ok"] is True
        second = model_hedge.reissue_gate(url, "m", cfg=cfg)
        assert second["ok"] is False and second["reason"] == "limit" and second["used"] == 1
        assert model_hedge.reissue_gate(url, "big", cfg=cfg)["ok"] is True  # its own budget
        assert model_hedge.reissue_gate(url, "never", cfg=cfg)["reason"] == "limit"
    finally:
        llm_trace.reset_current_run_id(token)
    token = llm_trace.set_current_run_id("run-b")  # a new turn starts afresh
    try:
        assert model_hedge.reissue_gate(url, "m", cfg=cfg)["ok"] is True
    finally:
        llm_trace.reset_current_run_id(token)
    monkeypatch.setattr(model_hedge, "server_capacity", lambda url: {
        "known": True, "waiting": 3.0, "running": 16.0, "saturated": True, "source": "test"})
    token = llm_trace.set_current_run_id("run-c")
    try:
        gate = model_hedge.reissue_gate(url, "m", cfg=cfg)
        assert gate["ok"] is False and gate["reason"] == "saturated"
        assert model_hedge._REISSUES.get("run:run-c", 0) == 0  # a skip spends nothing
    finally:
        llm_trace.reset_current_run_id(token)


def test_reissue_plan_needs_a_global_primary(monkeypatch):
    monkeypatch.setattr(model_hedge, "_host_dead", lambda u: False)
    monkeypatch.setattr(model_hedge, "endpoint_models", lambda *a, **k: [])
    monkeypatch.setattr(model_hedge, "global_service", lambda: {
        "endpoint_ids": {"sparks-ds"}, "servers": {"http://10.0.0.5:8888"}})
    primary = ("http://10.0.0.5:8888/v1/chat/completions", "deepseek", {"X": "1"})
    descs = [{"endpoint_id": "sparks-ds", "endpoint_label": "Sparks DS"}]
    for target in ("auto", "reissue", "REISSUE"):
        found = model_hedge.plan(*primary, descriptors=descs, cfg=_cfg(target=target))
        assert found["kind"] == "reissue" and found["url"] == primary[0] and found["model"] == "deepseek"
        assert found["endpoint_id"] == "sparks-ds" and found["delay_s"] == 20.0
    assert model_hedge.plan(*primary, descriptors=descs,
                            cfg=_cfg(target="reissue", reissue_overrides={"deepseek": 0})) is None
    local = ("http://127.0.0.1:8081/v1/chat/completions", "deepseek", {})
    assert model_hedge.plan(*local, descriptors=[{"endpoint_id": "loc"}], cfg=_cfg(target="reissue")) is None
    assert model_hedge.validate_target("reissue") == "reissue"
    assert any(o["value"] == "reissue" for o in model_hedge.target_options())


def test_server_capacity_reads_vllm_sglang_and_prometheus(monkeypatch):
    url = "http://10.0.0.5:8888/v1/chat/completions"
    sglang = ('# HELP sglang:num_queue_reqs The number of requests in the waiting queue.\n'
              'sglang:num_running_reqs{model_name="ds",tp_rank="0"} 3.0\n'
              'sglang:num_queue_reqs{model_name="ds",tp_rank="0"} 2.0\n'
              'sglang:num_grammar_queue_reqs{model_name="ds"} 9.0\n')
    seen = []
    monkeypatch.setattr(model_hedge, "_get_text", lambda u, timeout: seen.append(u) or sglang)
    cap = model_hedge.server_capacity(url)
    assert seen == ["http://10.0.0.5:8888/metrics"]
    assert cap["saturated"] is True and cap["waiting"] == 2.0 and cap["running"] == 3.0
    vllm = 'vllm:num_requests_waiting{model_name="m"} 0.0\nvllm:num_requests_running{model_name="m"} 1.0\n'
    monkeypatch.setattr(model_hedge, "_get_text", lambda u, timeout: vllm)
    cap = model_hedge.server_capacity(url)
    assert cap["known"] is True and cap["saturated"] is False and cap["running"] == 1.0
    # no metrics on the server: Prometheus's /api/serving for the same server
    monkeypatch.setattr(model_hedge, "_get_text", lambda u, timeout: None)
    from src import sparks
    monkeypatch.setattr(sparks, "_request", lambda method, path, body=None, **kw: (200, {"endpoints": [
        {"base_url": "http://10.0.0.9:8000/v1", "now": {"waiting": 5}},
        {"base_url": "http://10.0.0.5:8888/v1", "now": {"waiting": 1, "running": 4}}]}))
    cap = model_hedge.server_capacity(url)
    assert cap["saturated"] is True and cap["source"] == "prometheus /api/serving"
    # nothing known: not saturated (the re-send goes)
    monkeypatch.setattr(sparks, "_request", lambda method, path, body=None, **kw: (None, None))
    cap = model_hedge.server_capacity(url)
    assert cap["known"] is False and cap["saturated"] is False


def test_reissue_settings_update_and_surfaces(monkeypatch):
    written = {}
    monkeypatch.setattr("src.settings.update_settings", lambda patch: written.update(patch))
    monkeypatch.setattr(model_hedge, "status", lambda limit=20: {"ok": True})
    model_hedge.update({"target": "reissue", "reissue_max": 2, "reissue_overrides": '{"m": 0, "x": 5}'})
    assert written == {"agent_hedge_target": "reissue", "agent_hedge_reissue_max": 2,
                       "agent_hedge_reissue_overrides": '{"m": 0, "x": 5}'}
    for bad in ({"reissue_max": 6}, {"reissue_max": -1}, {"reissue_max": 1.5},
                {"reissue_max": "x"}, {"reissue_overrides": "nope"}, {"reissue_overrides": [1]},
                # a bad entry is refused, never silently dropped (or clamped)
                {"reissue_overrides": {"m": "a"}}, {"reissue_overrides": {"m": 9}},
                {"reissue_overrides": {"m": 1.5}}, {"reissue_overrides": {"m": True}},
                {"reissue_overrides": {"m": 1, "n": None}}, {"reissue_overrides": {"": 1}}):
        written.clear()
        with pytest.raises(ValueError, match="reissue_"):
            model_hedge.update(bad)
        assert written == {}
    from src.settings import DEFAULT_SETTINGS
    assert DEFAULT_SETTINGS["agent_hedge_reissue_max"] == 1
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    schema = next(t for t in FUNCTION_TOOL_SCHEMAS if t["function"]["name"] == "model_hedge")
    assert {"reissue_max", "reissue_overrides"} <= set(schema["function"]["parameters"]["properties"])
    assert "reissue" in schema["function"]["parameters"]["properties"]["target"]["description"]
