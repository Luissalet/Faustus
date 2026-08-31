"""A scripted OpenAI-compatible model server for the end-to-end tests.

Serves POST /v1/chat/completions (streaming SSE, the shape Faustus's
llm_core consumes) and answers each call of a chat with the next scripted
response. Tool calls are emitted as fenced blocks in the text (the format the
agent loop parses for local models), e.g.

    ```edit_file
    {"path": "a.py", "old_string": "1", "new_string": "2"}
    ```

Control endpoints (used by the tests):
    POST /_script   {"responses": [...], "reset": true}   set the script
    GET  /_calls     {"count": N, "last_messages": [...]}  what it received
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List


class _State:
    def __init__(self) -> None:
        self.responses: List[Any] = []
        self.calls = 0
        self.last_messages: List[Dict[str, Any]] = []
        self.delay = 0.0          # seconds between deltas (to keep runs "in flight")
        self.lock = threading.Lock()


STATE = _State()


def _sse(obj: Dict[str, Any]) -> bytes:
    return ("data: " + json.dumps(obj) + "\n\n").encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # silence
        pass

    def _json(self, code: int, obj: Any) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/v1/models"):
            return self._json(200, {"object": "list", "data": [{"id": "fake-coder", "object": "model"}]})
        if self.path.startswith("/_calls"):
            with STATE.lock:
                return self._json(200, {"count": STATE.calls, "last_messages": STATE.last_messages})
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            body = {}
        if self.path.startswith("/_script"):
            with STATE.lock:
                if body.get("reset", True):
                    STATE.calls = 0
                STATE.responses = list(body.get("responses") or [])
                STATE.delay = float(body.get("delay") or 0.0)
            return self._json(200, {"ok": True})
        if not self.path.startswith("/v1/chat/completions"):
            return self._json(404, {"error": "not found"})
        with STATE.lock:
            idx = STATE.calls
            STATE.calls += 1
            STATE.last_messages = [
                {"role": m.get("role"), "content": (m.get("content") if isinstance(m.get("content"), str) else json.dumps(m.get("content")))[:4000]}
                for m in (body.get("messages") or [])
            ]
            responses = list(STATE.responses)
            delay = STATE.delay
        text = responses[min(idx, len(responses) - 1)] if responses else "Done."
        if isinstance(text, dict):
            delay = float(text.get("delay", delay))
            text = str(text.get("text", ""))
        model = body.get("model") or "fake-coder"
        if not body.get("stream"):
            return self._json(200, {"id": "cmpl-1", "object": "chat.completion", "model": model,
                                    "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                                    "usage": {"prompt_tokens": 10, "completion_tokens": max(1, len(text) // 4), "total_tokens": 10 + max(1, len(text) // 4)}})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(_sse({"id": "cmpl-1", "object": "chat.completion.chunk", "model": model,
                               "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}))
        chunk = 48
        for i in range(0, len(text), chunk):
            self.wfile.write(_sse({"id": "cmpl-1", "object": "chat.completion.chunk", "model": model,
                                   "choices": [{"index": 0, "delta": {"content": text[i:i + chunk]}, "finish_reason": None}]}))
            self.wfile.flush()
            if delay:
                time.sleep(delay)
        self.wfile.write(_sse({"id": "cmpl-1", "object": "chat.completion.chunk", "model": model,
                               "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                               "usage": {"prompt_tokens": 10, "completion_tokens": max(1, len(text) // 4)}}))
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def serve(port: int) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


if __name__ == "__main__":
    import sys
    p = int(sys.argv[1]) if len(sys.argv) > 1 else 7891
    serve(p)
    print(f"fake llm on http://127.0.0.1:{p}/v1", flush=True)
    while True:
        time.sleep(3600)
