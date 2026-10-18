"""A managed prefill budget reaches the real HTTP reader (no model needed)."""
import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
import time

import httpx
import pytest

from src import llm_core
from src.first_token_budget import _OVERRIDE


@pytest.mark.parametrize("managed,budget,expected_read,complete", [
    (True, .9, .9, True),
    (True, .3, .3, False),
    (False, None, .3, False),
])
def test_421_second_prefill_scaled_with_300_or_900_budget(
    monkeypatch, managed, budget, expected_read, complete,
):
    # 1:1000 scaling, including early empty frames: 421 ms of genuine
    # silence exceeds 300 ms but fits 900 ms. No aggregate engine metrics.
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            try:
                self.wfile.write(b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n: alive\n\n')
                self.wfile.flush()
                time.sleep(.421)
                self.wfile.write(b'data: {"choices":[{"delta":{"content":"answer"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    url = f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
    monkeypatch.setattr(llm_core, "is_sparks_url", lambda u: managed)
    monkeypatch.setattr(llm_core, "_sparks_initial_wait", lambda u: budget)
    monkeypatch.setattr(llm_core, "_stream_idle_cap", lambda: .12)
    monkeypatch.setattr(llm_core, "note_model_activity", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda u: False)
    monkeypatch.setattr(llm_core, "_clear_host_dead", lambda *a, **k: None)
    read_timeouts = []
    async def observed(request):
        read_timeouts.append(request.extensions["timeout"]["read"])

    async def run():
        async with httpx.AsyncClient(trust_env=False, event_hooks={"request": [observed]}) as client:
            monkeypatch.setattr(llm_core, "_get_http_client", lambda: client)
            return [chunk async for chunk in llm_core.stream_llm(
                url, "fixture-model", [{"role": "user", "content": "synthetic long prefill"}],
                headers={"Authorization": "Bearer fixture"}, timeout=.3, max_retries=1,
            )]

    try:
        chunks = asyncio.run(run())
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
    assert len(requests) == 1, "A partial or timed-out stream must not replay the request"
    assert read_timeouts == [expected_read]
    assert any('"delta": "answer"' in chunk for chunk in chunks) is complete, chunks
    assert any('data: [DONE]' in chunk for chunk in chunks) is complete, chunks
    assert not any('degenerate_output' in chunk for chunk in chunks)


def test_managed_saved_and_request_budget_selection(monkeypatch):
    from src import settings
    from src.sparks import first_token_timeout
    from src.first_token_budget import effective
    monkeypatch.setattr(settings, "load_settings", lambda: {
        "sparks_endpoints": {"fixture-recipe": "fixture-endpoint"},
        "sparks_first_token_timeout_s": 900,
    })
    monkeypatch.setattr(llm_core, "_endpoint_id_for_url", lambda u: "fixture-endpoint")
    assert first_token_timeout({}) == 300  # no global default increase
    assert llm_core._sparks_initial_wait("http://fixture.invalid/v1") == 900
    token = _OVERRIDE.set(300)
    try:
        assert effective(900) == 300
        assert llm_core._sparks_initial_wait("http://fixture.invalid/v1") == 300
    finally:
        _OVERRIDE.reset(token)
