"""Real process replacement on an isolated port; no live model is stopped."""
import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from types import SimpleNamespace

import httpx
import psutil
import pytest

from src import engine_swap, model_server_heal as heal


def test_a_shell_or_one_shot_script_is_not_a_restart_supervisor(tmp_path):
    assert not heal._launcher_has_restart_loop(["powershell.exe"], str(tmp_path))
    script = tmp_path / "start.ps1"
    script.write_text("Start-Process llama-server.exe", encoding="utf-8")
    assert not heal._launcher_has_restart_loop(["powershell.exe", "-File", str(script)], str(tmp_path))
    script.write_text("while ($true) { Start-Process llama-server.exe -Wait }", encoding="utf-8")
    assert heal._launcher_has_restart_loop(["powershell.exe", "-File", str(script)], str(tmp_path))
    assert heal._launcher_has_restart_loop(["bash", "-c", "while true; do llama-server; done"], str(tmp_path))


def test_runner_snapshot_requires_matching_executable_port_and_model(monkeypatch):
    argv = ["llama-server", "-m", "current.gguf", "--port=8081", "--alias", "q8", "-c", "131072"]
    process = SimpleNamespace(exe=lambda: "/models/llama-server", cmdline=lambda: argv,
        create_time=lambda: 123.0, cwd=lambda: "/models", environ=lambda: {"CUDA_VISIBLE_DEVICES": "0,1,2,3"})
    monkeypatch.setattr(heal, "_psutil", lambda: SimpleNamespace(Process=lambda pid: process))
    spec = heal._llama_snapshot(42, 8081, "q8")
    assert spec["argv"] == ["/models/llama-server", *argv[1:]]
    assert spec["env"] == {"CUDA_VISIBLE_DEVICES": "0,1,2,3"}
    assert spec["cwd"] == "/models" and spec["created"] == 123.0
    assert heal._llama_snapshot(42, 8082, "q8") is None
    assert heal._llama_snapshot(42, 8081, "q4") is None
    process.exe = lambda: "/models/other-server"
    assert heal._llama_snapshot(42, 8081, "q8") is None


def test_reused_pid_is_never_terminated(monkeypatch):
    process = SimpleNamespace(create_time=lambda: 999.0)
    monkeypatch.setattr(heal, "_psutil", lambda: SimpleNamespace(Process=lambda pid: process))
    async def no_stop(*args):
        pytest.fail("reused PID")
    monkeypatch.setattr(heal, "_end_process", no_stop)
    assert asyncio.run(heal._restart_live_llama({"pid": 42, "created": 123.0}, 8081)) is False


@pytest.mark.parametrize("body,expected", [
    ([{"is_processing": False}], True),
    ([{"is_processing": True}], False),
    ([{"is_processing": False}, {"is_processing": True}], False),
    ([], None), ({"status": "ok"}, None), ([{}], None),
])
def test_only_valid_idle_slots_allow_restart(monkeypatch, body, expected):
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)), **kw))
    assert asyncio.run(heal._server_idle("http://127.0.0.1:12345/v1")) is expected


def test_confirmed_corruption_replaces_one_real_process_and_verifies_generation(monkeypatch, tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    script = tmp_path / "runner.py"
    marker = tmp_path / "loaded-once"
    script.write_text('''
import json, os, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
marker = Path(sys.argv[2])
broken = not marker.exists()
marker.write_text("loaded")
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def send(self, body):
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def do_GET(self):
        self.send([{"is_processing": False}] if self.path == "/slots" else {"status": "ok"})
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        prompt = body["messages"][0]["content"]
        answer = "kowaVCVCVCVCVCVC" if broken else ("12" if "7 + 5" in prompt else "7")
        self.send({"choices": [{"message": {"content": answer}}]})
HTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
''', encoding="utf-8")
    env = {**os.environ, "HEAL_TEST_MARKER": "preserved"}
    process = subprocess.Popen([sys.executable, str(script), str(port), str(marker)],
        cwd=tmp_path, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    replacement_pid = None
    url = f"http://127.0.0.1:{port}/v1"
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not asyncio.run(heal._health_ok(url)):
            time.sleep(.05)
        assert asyncio.run(engine_swap.generates_sanely(url, "fake-fixture")) is False
        original_pid = heal.listener_pid(port)  # Windows venv Python may forward to a child.
        assert original_pid
        monkeypatch.setattr(heal, "_settings", lambda: {"enabled": True, "commands": [], "timeout_s": 15})
        monkeypatch.setattr(heal.tempfile, "gettempdir", lambda: str(tmp_path))
        monkeypatch.setattr(engine_swap, "restartable_engine_for_url", lambda url: None)
        monkeypatch.setattr(heal, "supervisor_of", lambda pid: None)
        # The fixture is Python, not llama-server. Replace only identity discovery;
        # HTTP confirmation, idle checks, stop/spawn and post-reload checks are real.
        def snapshot(pid, observed_port, model):
            assert pid == original_pid and observed_port == port
            proc = psutil.Process(pid)
            return {"pid": pid, "created": proc.create_time(), "argv": [proc.exe(), *proc.cmdline()[1:]],
                    "cwd": proc.cwd(), "env": proc.environ()}
        monkeypatch.setattr(heal, "_llama_snapshot", snapshot)
        result = asyncio.run(heal.heal(url, "fake-fixture"))
        replacement_pid = heal.listener_pid(port)
        assert result["action"] == "restarted", result
        assert replacement_pid and replacement_pid != original_pid
        assert not psutil.pid_exists(original_pid)
        assert psutil.Process(replacement_pid).environ()["HEAL_TEST_MARKER"] == "preserved"
        assert asyncio.run(engine_swap.generates_sanely(url, "fake-fixture")) is True
        assert not os.path.exists(heal._lock_path(port))
    finally:
        for pid in {process.pid, replacement_pid or heal.listener_pid(port)} - {None}:
            try:
                proc = psutil.Process(pid)
                proc.terminate()
                proc.wait(timeout=5)
            except psutil.NoSuchProcess:
                pass
        heal._LAST_RESTART.pop(str(port), None)
        heal._INFLIGHT.pop(str(port), None)
