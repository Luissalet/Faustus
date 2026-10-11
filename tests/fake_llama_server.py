"""A small stand-in for llama-server, for the slot-cache tests and live checks.

It keeps one list of "tokens" per slot (the words of the prompt and of the
reply), reuses the common prefix of the slot when `cache_prompt` is true,
reports llama.cpp-style `timings` (`prompt_n`, `cache_n`, `prompt_ms`), and
implements `/slots/{id}?action=save|restore|erase` the way llama-server does:
HTTP 501 unless it was started with a slot save path. Every request is
recorded in `.requests` (and, with `log_path`, appended there as JSON lines).

Run it as a process for a live check:

    python tests/fake_llama_server.py --port 8391 --slot-save-path D:/tmp/slots --log D:/tmp/fake.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

REPLY = "The document says what it says. This is the fake server's reply."


def _tokens(text: str) -> List[str]:
    """Pieces of at most four characters of each word: about what a real
    tokenizer makes of English, so token counts are comparable with Faustus's
    own estimate."""
    return re.findall(r"\S{1,4}", text)


def _message_text(message: Dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, list):
        content = " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
    return f"<{message.get('role')}> {content or ''}"


class FakeLlamaServer:
    def __init__(self, port: int = 0, slot_save_path: Optional[str] = None, n_slots: int = 4,
                 ms_per_prompt_token: float = 1.0, log_path: Optional[str] = None,
                 model_path: str = "D:/models/fake-model.gguf", chat_template: str = "fake-template-1",
                 n_ctx: int = 16384, build_info: str = "fake"):
        self.slot_save_path = slot_save_path
        self.n_slots = n_slots
        self.ms_per_prompt_token = ms_per_prompt_token
        self.log_path = log_path
        self.model_path = model_path
        self.chat_template = chat_template
        self.n_ctx = n_ctx
        self.build_info = build_info
        #: Seconds a slot action sleeps before it answers (a slow disk), per action.
        self.action_delay: Dict[str, float] = {}
        self.slots: List[List[str]] = [[] for _ in range(n_slots)]
        self.requests: List[Dict[str, Any]] = []
        self.fail_actions: Dict[str, int] = {}
        self.lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # silence
                pass

            def _send(self, status: int, body: Any) -> None:
                raw = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _body(self) -> Dict[str, Any]:
                n = int(self.headers.get("Content-Length") or 0)
                if not n:
                    return {}
                try:
                    return json.loads(self.rfile.read(n).decode("utf-8"))
                except ValueError:
                    return {}

            def do_GET(self):  # noqa: N802
                outer._record(self, "GET", {})
                path = urlparse(self.path).path
                if path == "/health":
                    return self._send(200, {"status": "ok"})
                if path == "/props":
                    return self._send(200, {"model_path": outer.model_path, "chat_template": outer.chat_template,
                                            "total_slots": outer.n_slots, "build_info": outer.build_info,
                                            "default_generation_settings": {"n_ctx": outer.n_ctx}})
                if path == "/slots":
                    return self._send(200, [{"id": i, "n_ctx": outer.n_ctx, "is_processing": False}
                                            for i in range(outer.n_slots)])
                if path in ("/v1/models", "/models"):
                    return self._send(200, {"object": "list", "data": [{"id": "fake-model", "object": "model"}]})
                self._send(404, {"error": {"message": "not found", "code": 404}})

            def do_POST(self):  # noqa: N802
                body = self._body()
                outer._record(self, "POST", body)
                parsed = urlparse(self.path)
                parts = [p for p in parsed.path.split("/") if p]
                if len(parts) == 2 and parts[0] == "slots":
                    return outer._slot_action(self, int(parts[1]), (parse_qs(parsed.query).get("action") or [""])[0], body)
                if parsed.path.endswith("/chat/completions"):
                    return outer._chat(self, body)
                self._send(404, {"error": {"message": "not found", "code": 404}})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.port = self.httpd.server_address[1]
        self.thread: Optional[threading.Thread] = None

    # ── plumbing ────────────────────────────────────────────────────────
    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "FakeLlamaServer":
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def __enter__(self) -> "FakeLlamaServer":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    def _record(self, handler: BaseHTTPRequestHandler, method: str, body: Dict[str, Any]) -> None:
        parsed = urlparse(handler.path)
        row = {"t": round(time.time(), 3), "method": method, "path": parsed.path,
               "action": (parse_qs(parsed.query).get("action") or [""])[0], "body": body}
        with self.lock:
            self.requests.append(row)
            if self.log_path:
                slim = dict(row)
                if "messages" in body:
                    slim["body"] = {k: v for k, v in body.items() if k != "messages"}
                    slim["body"]["messages"] = len(body["messages"])
                with open(self.log_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(slim) + "\n")

    def calls(self, action: Optional[str] = None) -> List[Dict[str, Any]]:
        with self.lock:
            return [r for r in self.requests
                    if r["path"].startswith("/slots/") and (action is None or r["action"] == action)]

    def chats(self) -> List[Dict[str, Any]]:
        with self.lock:
            return [r for r in self.requests if r["path"].endswith("/chat/completions")]

    # ── slot actions ───────────────────────────────────────────────────
    def _slot_action(self, handler, slot: int, action: str, body: Dict[str, Any]) -> None:
        if self.action_delay.get(action):
            time.sleep(self.action_delay[action])
        if self.fail_actions.get(action):
            return handler._send(self.fail_actions[action], {"error": {"code": self.fail_actions[action],
                                                                       "message": "forced failure"}})
        if not self.slot_save_path:
            return handler._send(501, {"error": {"code": 501, "type": "not_supported_error",
                                                 "message": "This server does not support slots action. "
                                                            "Start it with `--slot-save-path`"}})
        if slot < 0 or slot >= self.n_slots:
            return handler._send(400, {"error": {"code": 400, "message": "Invalid slot ID"}})
        filename = str(body.get("filename") or "")
        if action == "erase":
            n = len(self.slots[slot])
            self.slots[slot] = []
            return handler._send(200, {"id_slot": slot, "n_erased": n})
        if not filename or "/" in filename or "\\" in filename or ".." in filename:
            return handler._send(400, {"error": {"code": 400, "message": "Invalid filename"}})
        path = os.path.join(self.slot_save_path, filename)
        if action == "save":
            tokens = self.slots[slot]
            os.makedirs(self.slot_save_path, exist_ok=True)
            started = time.time()
            data = json.dumps(tokens).encode("utf-8") + b"\0" * (len(tokens) * 256)  # a KV-sized file
            with open(path, "wb") as fh:
                fh.write(data)
            return handler._send(200, {"id_slot": slot, "filename": filename, "n_saved": len(tokens),
                                       "n_written": len(data),
                                       "timings": {"save_ms": round((time.time() - started) * 1000, 2)}})
        if action == "restore":
            try:
                with open(path, "rb") as fh:
                    raw = fh.read().rstrip(b"\0")
                tokens = json.loads(raw.decode("utf-8"))
            except (OSError, ValueError):
                return handler._send(400, {"error": {"code": 400, "message":
                                                     "Unable to restore slot, no available space in KV cache "
                                                     "or invalid slot save file"}})
            self.slots[slot] = list(tokens)
            return handler._send(200, {"id_slot": slot, "filename": filename, "n_restored": len(tokens),
                                       "n_read": os.path.getsize(path), "timings": {"restore_ms": 1.0}})
        handler._send(400, {"error": {"code": 400, "message": "Invalid action"}})

    # ── completions ────────────────────────────────────────────────────
    def _chat(self, handler, body: Dict[str, Any]) -> None:
        slot = body.get("id_slot")
        slot = slot if isinstance(slot, int) and 0 <= slot < self.n_slots else 0
        prompt: List[str] = []
        for message in body.get("messages") or []:
            prompt.extend(_tokens(_message_text(message)))
        held = self.slots[slot]
        cache_n = 0
        if body.get("cache_prompt", True):
            for a, b in zip(held, prompt):
                if a != b:
                    break
                cache_n += 1
        prompt_n = len(prompt) - cache_n
        prompt_ms = round(prompt_n * self.ms_per_prompt_token, 3)
        time.sleep(min(prompt_ms, 2000.0) / 1000.0)
        reply = _tokens(REPLY)
        self.slots[slot] = prompt + reply
        timings = {"cache_n": cache_n, "prompt_n": prompt_n, "prompt_ms": prompt_ms,
                   "prompt_per_second": round(prompt_n / prompt_ms * 1000, 2) if prompt_ms else 0.0,
                   "predicted_n": len(reply), "predicted_ms": float(len(reply)),
                   "predicted_per_second": 1000.0}
        usage = {"prompt_tokens": len(prompt), "completion_tokens": len(reply), "total_tokens": len(prompt) + len(reply)}
        if not body.get("stream"):
            return handler._send(200, {"id": "fake", "object": "chat.completion", "model": "fake-model",
                                       "choices": [{"index": 0, "finish_reason": "stop",
                                                    "message": {"role": "assistant", "content": REPLY}}],
                                       "usage": usage, "timings": timings})
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Connection", "close")
        handler.end_headers()

        def emit(obj: Any) -> None:
            handler.wfile.write(b"data: " + (obj if isinstance(obj, str) else json.dumps(obj)).encode("utf-8") + b"\n\n")
            handler.wfile.flush()

        emit({"id": "fake", "model": "fake-model", "choices": [{"index": 0, "delta": {"role": "assistant"}}]})
        words = REPLY.split(" ")
        for i, word in enumerate(words):
            emit({"id": "fake", "model": "fake-model",
                  "choices": [{"index": 0, "delta": {"content": word + (" " if i < len(words) - 1 else "")}}]})
        emit({"id": "fake", "model": "fake-model", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        emit({"id": "fake", "model": "fake-model", "choices": [], "usage": usage, "timings": timings})
        emit("[DONE]")
        handler.close_connection = True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--slot-save-path", default=None)
    parser.add_argument("--slots", type=int, default=4)
    parser.add_argument("--ms-per-token", type=float, default=1.0)
    parser.add_argument("--log", default=None)
    parser.add_argument("--n-ctx", type=int, default=16384)
    parser.add_argument("--save-delay", type=float, default=0.0, help="seconds a slot save takes to answer")
    parser.add_argument("--fail-save", type=int, default=0, help="answer slot saves with this HTTP status")
    args = parser.parse_args()
    server = FakeLlamaServer(port=args.port, slot_save_path=args.slot_save_path, n_slots=args.slots,
                             ms_per_prompt_token=args.ms_per_token, log_path=args.log, n_ctx=args.n_ctx)
    if args.save_delay:
        server.action_delay["save"] = args.save_delay
    if args.fail_save:
        server.fail_actions["save"] = args.fail_save
    print(f"fake llama-server on {server.base}", flush=True)
    server.httpd.serve_forever()


if __name__ == "__main__":
    main()
