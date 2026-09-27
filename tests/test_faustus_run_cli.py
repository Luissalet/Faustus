"""scripts/faustus_run.py: a headless turn against a stand-in server."""
import io
import json
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("faustus_run", Path(__file__).resolve().parents[1] / "scripts" / "faustus_run.py")
fr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fr)


def _sse(events):
    return "".join("data: " + json.dumps(e) + "\n\n" for e in events) + "data: [DONE]\n\n"


class _H(BaseHTTPRequestHandler):
    forms = []
    legs = []
    sessions = []
    gets = {}
    refuse = None

    def do_GET(self):  # noqa: N802
        payload = _H.gets.get(self.path.split("?")[0])
        if payload is None:
            self.send_response(404); self.end_headers(); return
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        if self.path == "/api/chat_stream" and _H.refuse:
            data = json.dumps({"detail": _H.refuse}).encode()
            self.send_response(400); self.send_header("Content-Length", str(len(data))); self.end_headers()
            self.wfile.write(data); return
        if self.path == "/api/session":
            _H.sessions.append(dict(urllib.parse.parse_qsl(body)))
            out, ctype = json.dumps({"id": "s1"}), "application/json"
        else:
            form = dict(urllib.parse.parse_qsl(body))
            _H.forms.append(form)
            out, ctype = _sse(_H.legs.pop(0)), "text/event-stream"
        data = out.encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def _serve(legs, gets=None, refuse=None):
    _H.forms, _H.legs, _H.sessions, _H.gets, _H.refuse = [], list(legs), [], dict(gets or {}), refuse
    srv = HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


CARD = {"type": "ask_user", "data": {"kind": "tool_approval", "approval_id": "A1"}}
LEG_DONE = [{"type": "tool_start", "tool": "edit_file", "round": 2},
            {"delta": "Fixed."},
            {"type": "usage", "data": {"input_tokens": 100, "output_tokens": 20, "cached_tokens": 80}}]


def test_json_run_with_auto_approval(monkeypatch, capsys):
    srv, url = _serve([[{"type": "tool_start", "tool": "read_file", "round": 1}, CARD], LEG_DONE])
    try:
        code = fr.main(["-p", "fix it", "--url", url, "--json", "--approve", "--workspace", "."])
    finally:
        srv.shutdown()
    lines = [json.loads(l) for l in capsys.readouterr().out.splitlines()]
    assert code == 0
    assert lines[0]["type"] == "run_start" and lines[-1]["type"] == "run_summary"
    s = lines[-1]
    assert s["stop"] == "answered" and s["text"] == "Fixed." and s["tools"] == ["read_file", "edit_file"]
    assert s["usage"]["cached_tokens"] == 80 and s["rounds"] == 2
    assert _H.forms[1]["tool_approval_id"] == "A1" and _H.forms[1]["tool_approval_decision"] == "approve_task"


def test_json_run_uses_utf8_even_with_windows_code_page(monkeypatch):
    srv, url = _serve([[{"delta": "A → B"}]])
    data = io.BytesIO()
    out = io.TextIOWrapper(data, encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", out)
    try:
        code = fr.main(["-p", "test", "--url", url, "--json", "--model", "test-model"])
    finally:
        srv.shutdown()
    lines = [json.loads(line) for line in data.getvalue().decode("utf-8").splitlines()]
    assert code == 0
    assert lines[-1]["text"] == "A → B"


def test_stops_on_a_card_without_approve(capsys):
    srv, url = _serve([[CARD]])
    try:
        code = fr.main(["-p", "delete build", "--url", url])
    finally:
        srv.shutdown()
    captured = capsys.readouterr()
    assert code == fr.EXIT_APPROVAL
    assert "approval A1" in captured.err


def test_prompt_from_file(tmp_path):
    p = tmp_path / "m.txt"
    p.write_text("¿Qué hay?", encoding="utf-8")
    assert fr.read_prompt("@" + str(p)) == "¿Qué hay?"


def test_a_run_without_model_opens_the_chat_on_the_default_model(capsys):
    gets = {"/api/auth/settings": {"default_model": "big-27b", "default_endpoint_id": "e2"},
            "/api/models": {"items": [
                {"endpoint_id": "e1", "url": "http://127.0.0.1:9/v1", "models": ["big-27b"], "model_type": "llm"},
                {"endpoint_id": "e2", "url": "http://127.0.0.1:8081/v1", "models": ["big-27b"], "model_type": "llm"}]}}
    srv, url = _serve([LEG_DONE], gets=gets)
    try:
        code = fr.main(["-p", "hola", "--url", url])
    finally:
        srv.shutdown()
    assert code == 0
    assert _H.sessions[0]["model"] == "big-27b"
    assert _H.sessions[0]["endpoint_url"] == "http://127.0.0.1:8081/v1"
    assert _H.forms[0]["model"] == "big-27b"


def test_a_refused_turn_says_why(capsys):
    srv, url = _serve([], refuse="No model selected for this chat.")
    try:
        code = fr.main(["-p", "hola", "--url", url, "--json"])
    finally:
        srv.shutdown()
    summary = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert code == fr.EXIT_ERROR
    assert "No model selected for this chat." in summary["error"]
