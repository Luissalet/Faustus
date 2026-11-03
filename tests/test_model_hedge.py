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

    def __init__(self, mode, text="", delay=0.0):
        self.mode, self.text, self.delay = mode, text, delay
        self.requests = 0
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
            if length:
                await reader.readexactly(length)
            if method != "POST" or not path.endswith("/chat/completions"):
                writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                await writer.drain()
                return
            self.requests += 1
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                         b"Cache-Control: no-cache\r\nConnection: close\r\n\r\n")
            await writer.drain()
            if self.mode == "stall":
                await self._stall(reader, writer)
            else:
                await self._answer(writer)
        except (ConnectionError, asyncio.IncompleteReadError):
            self.disconnected.set()
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _stall(self, reader, writer):
        eof = asyncio.ensure_future(reader.read(1))
        try:
            for _ in range(600):
                if eof.done():
                    self.disconnected.set()
                    return
                writer.write(b": holding\n\n")
                await writer.drain()
                await asyncio.sleep(0.05)
        except ConnectionError:
            self.disconnected.set()
        finally:
            eof.cancel()

    async def _answer(self, writer):
        await asyncio.sleep(self.delay)

        def frame(delta, finish=None):
            body = {"id": "x", "object": "chat.completion.chunk", "model": "m",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            return ("data: " + json.dumps(body) + "\n\n").encode()

        writer.write(frame({"role": "assistant"}))
        for word in self.text.split(" "):
            writer.write(frame({"content": word + " "}))
            await writer.drain()
            await asyncio.sleep(0.01)
        writer.write(frame({}, "stop"))
        writer.write(b"data: [DONE]\n\n")
        await writer.drain()


@pytest.fixture
def hedge_env(monkeypatch, tmp_path):
    from src import llm_core
    monkeypatch.setenv("ODYSSEUS_LOCAL_MODEL_GATE", "false")
    monkeypatch.setattr(model_hedge, "_log_path", lambda: str(tmp_path / "model_hedges.jsonl"))
    monkeypatch.setattr(model_hedge, "_RECENT", model_hedge.deque(maxlen=200))
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda u: False)
    monkeypatch.setattr(llm_core, "is_sparks_url", lambda u: False)
    state = {"cfg": {"enabled": True, "delay_s": 0.3, "target": "auto", "delay_overrides": {}}}
    monkeypatch.setattr(model_hedge, "config", lambda: dict(state["cfg"]))
    return state


def _wire_same_model(monkeypatch, other):
    base = model_hedge._base(other.url)
    monkeypatch.setattr(model_hedge, "endpoint_models", lambda: [
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
    async def scenario():
        fast = await _FakeServer("answer", "primary answer").start()
        other = await _FakeServer("answer", "should not run").start()
        _wire_same_model(monkeypatch, other)
        try:
            chunks = await _collect([(fast.url, "m", {})])
            await asyncio.sleep(0.5)  # past the delay: still nothing is started
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
    monkeypatch.setattr(model_hedge, "endpoint_models", lambda: [])
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
    monkeypatch.setattr(model_hedge, "endpoint_models", lambda: [
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
    monkeypatch.setattr(model_hedge, "endpoint_models", lambda: [
        {"endpoint_id": "ep", "label": "EP", "base": "http://x/v1", "models": ["m"]}])
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
