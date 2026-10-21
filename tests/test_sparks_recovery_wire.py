"""Real stream producers report the managed base, not the completions URL."""
import asyncio
from copy import deepcopy
import json

import pytest

from src import llm_core, model_server_heal

BASE = "http://10.33.0.9:8012/v1"


def stream_stub(monkeypatch, delta):
    sent, reported = [], []

    class Response:
        status_code = 200

        async def aiter_lines(self):
            yield "data: " + json.dumps({"choices": [{"delta": delta}]})
            yield "data: [DONE]"

        async def aread(self):
            return b""

    class Context:
        async def __aenter__(self):
            return Response()

        async def __aexit__(self, *args):
            return False

    class Client:
        def stream(self, method, url, **kwargs):
            sent.append((url, deepcopy(kwargs["json"])))
            return Context()

    monkeypatch.setattr(llm_core, "_get_http_client", lambda: Client())
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda _: False)
    monkeypatch.setattr(llm_core, "note_model_activity", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "_clear_host_dead", lambda *a, **k: None)
    monkeypatch.setattr(model_server_heal, "schedule", lambda *args: reported.append(args) or True)
    return sent, reported


def collect(url, **kwargs):
    async def run():
        return [chunk async for chunk in llm_core.stream_llm(
            url, "test-model", [{"role": "user", "content": "Explain this project"}],
            max_retries=0, **kwargs)]
    return asyncio.run(run())


@pytest.mark.parametrize("url", [BASE, BASE + "/chat/completions"])
def test_content_loop_reports_managed_base_from_real_stream(monkeypatch, url):
    sent, reported = stream_stub(monkeypatch, {"content": "/" * 200})
    chunks = collect(url)
    assert sent[0][0] == BASE + "/chat/completions"
    assert len(reported) == 1 and reported[0][:2] == (BASE, "test-model")
    assert any('"error_class": "degenerate_output"' in chunk for chunk in chunks)


def test_argument_loop_error_retains_the_managed_base(monkeypatch):
    arguments = json.dumps({"content": "project explanation " * 300})
    sent, reported = stream_stub(monkeypatch, {"tool_calls": [{
        "index": 0, "id": "call-one", "type": "function",
        "function": {"name": "write_file", "arguments": arguments},
    }]})
    # Exercise the distinct accumulated-argument producer; delta-loop detection
    # is independently covered above and in test_degenerate_output.py.
    monkeypatch.setattr(llm_core._DegenerateStreamGuard, "check", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "tool_argument_loop", lambda _: "repeated argument prose")
    chunks = collect(BASE, tools=[{"type": "function", "function": {
        "name": "write_file", "parameters": {"type": "object"},
    }}])
    assert sent[0][0] == BASE + "/chat/completions"
    assert len(reported) == 1 and reported[0][:2] == (BASE, "test-model")
    assert any('"error_class": "degenerate_output"' in chunk for chunk in chunks)
