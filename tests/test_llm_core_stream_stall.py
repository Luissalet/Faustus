"""A streamed answer that goes silent after it started is closed as degenerate.

Live 17-10-2026: a Hoard copilot turn on GLM 5.3 flash (vLLM on the Sparks)
streamed 1.2k characters of reasoning, then nothing for nine minutes while
the server kept generating at 130 tokens/s: vLLM's tool-call parser held a
broken call back. The first line may take as long as the prompt needs; only
the silence after it counts."""
import asyncio
import json

from src import llm_core


class _SlowResp:
    def __init__(self, script):
        self._script = script
        self.status_code = 200
        self.closed_early = False

    async def aiter_lines(self):
        for pause, line in self._script:
            await asyncio.sleep(pause)
            yield line

    async def aread(self):
        return b""


class _Ctx:
    def __init__(self, resp):
        self.resp = resp

    async def __aenter__(self):
        return self.resp

    async def __aexit__(self, *a):
        return False


class _Client:
    def __init__(self, resp):
        self.resp = resp

    def stream(self, method, url, **kw):
        return _Ctx(self.resp)


def _sse(delta):
    return "data: " + json.dumps({"choices": [{"delta": delta}]})


def _drive(monkeypatch, script, idle=0.2):
    resp = _SlowResp(script)
    monkeypatch.setattr(llm_core, "_get_http_client", lambda: _Client(resp))
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda u: False)
    monkeypatch.setattr(llm_core, "note_model_activity", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "_clear_host_dead", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "_stream_idle_cap", lambda: idle)

    async def run():
        events = []
        async for chunk in llm_core.stream_llm(
            "https://api.example.test/v1/chat/completions", "glm-test", [{"role": "user", "content": "hi"}],
            headers={"Authorization": "Bearer k"},
            tools=[{"type": "function", "function": {"name": "web_search", "parameters": {}}}],
        ):
            for ln in chunk.split("\n"):
                ln = ln.strip()
                if ln.startswith("data: ") and ln[6:] != "[DONE]":
                    try:
                        events.append(json.loads(ln[6:]))
                    except ValueError:
                        pass
        return events

    return asyncio.run(run())


def test_silence_after_the_first_line_is_cut(monkeypatch):
    script = [(0, _sse({"reasoning_content": "Plan: 1. lookup_tools"})), (1.0, _sse({"content": "late"})), (0, "data: [DONE]")]
    events = _drive(monkeypatch, script)
    errors = [e for e in events if e.get("error_class") == llm_core.DEGENERATE_OUTPUT_ERROR_CLASS]
    assert errors, events
    assert "sent nothing" in errors[0]["error"] and errors[0]["stalled_s"] == 0
    assert not any(e.get("delta") == "late" for e in events)


def test_a_slow_first_line_is_prefill_not_a_stall(monkeypatch):
    script = [(0.5, _sse({"content": "hello"})), (0.05, _sse({"content": " there"})), (0, "data: [DONE]")]
    events = _drive(monkeypatch, script)
    assert not any(e.get("error_class") for e in events), events
    assert "".join(e.get("delta", "") for e in events if not e.get("thinking")) == "hello there"


def test_zero_turns_the_cap_off(monkeypatch):
    script = [(0, _sse({"content": "a"})), (0.4, _sse({"content": "b"})), (0, "data: [DONE]")]
    events = _drive(monkeypatch, script, idle=0)
    assert not any(e.get("error_class") for e in events), events


def test_lines_until_stall_yields_the_marker_once():
    async def lines():
        yield "one"
        await asyncio.sleep(0.5)
        yield "two"

    async def collect():
        return [x async for x in llm_core._lines_until_stall(lines(), 0.1)]

    out = asyncio.run(collect())
    assert out == ["one", llm_core._STREAM_STALLED]


def test_setting_default_and_bad_values(monkeypatch):
    from src import settings
    assert settings.DEFAULT_SETTINGS["llm_stream_idle_timeout_s"] == 120
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: "nonsense")
    assert llm_core._stream_idle_cap() == llm_core._STREAM_IDLE_DEFAULT_S
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: -5)
    assert llm_core._stream_idle_cap() == 0.0
