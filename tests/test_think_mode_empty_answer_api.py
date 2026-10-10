# -*- coding: utf-8 -*-
"""Auto must see a turn that ended with reasoning and no final text (OBJ-58,
Agora #227 r3), through the real API.

A real Faustus server (temp data dir, localhost bypass, no auth) plus the
scripted fake model endpoint of the end-to-end tests. The provider streams ONLY
`reasoning_content` and ends on `finish_reason: "length"`: the thinking budget
went on thinking and there is no final text. Before the fix nothing durable was
saved for such a turn, so GET history showed only the user message and the next
Auto turn chose deep again. These tests follow it as a client would: POST
/api/chat_stream, GET /api/history, POST /api/chat_stream again.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
MODEL = "qwen3-fake"  # a name the think-mode resolver knows to be a thinking model
ASK = "Demuestra con todo el rigor que hay infinitos primos de la forma 4k+3"
ASK_AGAIN = ASK + ", paso a paso"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


def _post_json(url: str, data: dict, headers: dict | None = None) -> dict:
    req = urllib.request.Request(url, data=json.dumps(data).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


def _post_form(url: str, data: dict) -> dict:
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode("utf-8"), method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


def _reasoning(steps: int = 40) -> str:
    # Distinct sentences: llm_core's degenerate-stream guard aborts a repeated phrase.
    return " ".join(f"Paso {i}: considero el caso {i * 7} modulo 4 y descarto la rama {i * 3} por una razon distinta."
                    for i in range(1, steps))


class Server:
    def __init__(self, base: str, fake: str, endpoint_id: str):
        self.base, self.fake, self.endpoint_id = base, fake, endpoint_id
        self.run_id: str | None = None
        self.thinking_seen = threading.Event()  # set once a reasoning chunk has streamed

    def session(self, name: str = "empty-answer") -> str:
        r = _post_form(self.base + "/api/session", {
            "name": name, "endpoint_id": self.endpoint_id, "endpoint_url": self.fake + "/v1",
            "model": MODEL, "skip_validation": "true"})
        return r.get("id") or r.get("session_id")

    def script(self, responses: list, delay: float = 0.0) -> None:
        _post_json(self.fake + "/_script", {"responses": responses, "delay": delay, "reset": True})

    def turn(self, sid: str, message: str, *, mode: str = "chat", think_mode: str | None = None,
             incognito: bool = False) -> list[dict]:
        data = {"session": sid, "message": message, "mode": mode, "model": MODEL}
        if think_mode:
            data["think_mode"] = think_mode
        if incognito:
            data["incognito"] = "true"
        events: list[dict] = []
        self.run_id = None
        self.thinking_seen.clear()
        req = urllib.request.Request(self.base + "/api/chat_stream",
                                     data=urllib.parse.urlencode(data).encode("utf-8"), method="POST")
        with urllib.request.urlopen(req, timeout=180) as r:
            self.run_id = r.headers.get("X-Odysseus-Run-Id")
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                try:
                    event = json.loads(line[6:])
                except ValueError:
                    continue
                events.append(event)
                if event.get("thinking") and "delta" in event:
                    self.thinking_seen.set()
        time.sleep(0.5)  # the post-turn work after [DONE] (saves, receipts)
        return events

    def history(self, sid: str) -> list[dict]:
        return _get(self.base + f"/api/history/{sid}")["history"]

    def stop(self, sid: str) -> dict:
        return _post_json(self.base + f"/api/chat/stop/{sid}", {}, {"X-Odysseus-Run-Id": self.run_id or ""})

    def sent_to_model(self) -> list[dict]:
        return _get(self.fake + "/_calls")["last_messages"]


def _think(events: list[dict]) -> dict:
    return next(e for e in events if e.get("type") == "think_mode")["data"]


@pytest.fixture(scope="module")
def server():
    from tests.e2e import fake_llm
    fake_port, port = _free_port(), _free_port()
    fake = fake_llm.serve(fake_port)
    data_dir = tempfile.mkdtemp(prefix="faustus-empty-answer-")
    env = dict(os.environ)
    env.update({
        "ODYSSEUS_DATA_DIR": data_dir, "FAUSTUS_DATA_DIR": data_dir,
        "DATABASE_URL": "sqlite:///" + data_dir.replace("\\", "/") + "/app.db",
        "APP_PORT": str(port), "LOCALHOST_BYPASS": "true", "AUTH_ENABLED": "false",
        "ODYSSEUS_INPROCESS_POLLERS": "0", "ODYSSEUS_INPROCESS_TASKS": "0", "ODYSSEUS_STARTUP_WARMUPS": "0",
        "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
    })
    log = open(os.path.join(data_dir, "server.log"), "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(port)],
                            cwd=str(REPO), env=env, stdout=log, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 150
        while True:
            try:
                _get(base + "/api/chat/activity")
                break
            except Exception:  # noqa: BLE001
                if proc.poll() is not None or time.time() > deadline:
                    raise RuntimeError("the test server did not come up")
                time.sleep(1)
        endpoint = _post_form(base + "/api/model-endpoints", {
            "name": "fake", "base_url": f"http://127.0.0.1:{fake_port}/v1", "skip_probe": "true",
            "endpoint_kind": "local"})
        yield Server(base, f"http://127.0.0.1:{fake_port}", endpoint.get("id") or (endpoint.get("endpoint") or {}).get("id"))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        fake.shutdown()
        shutil.rmtree(data_dir, ignore_errors=True)


REASONING_ONLY_LENGTH = {"reasoning": _reasoning(), "text": "", "finish_reason": "length"}


@pytest.mark.parametrize("mode", ["chat", "agent"])
def test_a_deep_turn_that_only_reasoned_is_kept_and_auto_does_not_pick_deep_again(server, mode):
    sid = server.session(f"empty-{mode}")
    server.script([REASONING_ONLY_LENGTH])
    first = server.turn(sid, ASK, mode=mode, think_mode="deep")
    assert _think(first)["mode"] == "deep" and _think(first)["source"] == "explicit"
    saved = [e for e in first if e.get("type") == "empty_answer"]
    assert saved and saved[0]["data"] == {"reason": "length", "finish_reason": "length"}
    assert any(e.get("type") == "message_saved" for e in first)

    # The durable record: an assistant row with no text and the outcome in its metadata.
    rows = server.history(sid)
    assert [r["role"] for r in rows] == ["user", "assistant"], rows
    assert rows[1]["content"] == "", "no answer text is invented"
    md = rows[1]["metadata"]
    assert md["empty_answer"] is True and md["empty_answer_reason"] == "length"
    assert md["finish_reason"] == "length"
    assert md["think_mode"]["mode"] == "deep" and md["think_mode"]["source"] == "explicit"
    assert "Paso 1:" in md["thinking"], "the reasoning is kept"

    # The next Auto turn reads it.
    server.script([{"text": "Hecho."}])
    second = server.turn(sid, ASK_AGAIN, mode=mode, think_mode="auto")
    why = _think(second)
    assert why["mode"] != "deep" and "deep_spent_without_answer" in why["reasons"], why
    assert why["requested"] == "auto"
    # ...and the model never reads the empty reply as part of the conversation.
    assert not [m for m in server.sent_to_model() if m["role"] == "assistant" and not (m["content"] or "").strip()]
    final = server.history(sid)
    assert [r["role"] for r in final] == ["user", "assistant", "user", "assistant"]
    assert final[3]["content"] == "Hecho."


def test_an_explicit_deep_still_wins_after_a_spent_deep_pass(server):
    sid = server.session("empty-explicit")
    server.script([REASONING_ONLY_LENGTH])
    server.turn(sid, ASK, think_mode="deep")
    server.script([{"text": "Otra vez, a fondo."}])
    again = server.turn(sid, ASK_AGAIN, think_mode="deep")
    assert _think(again)["mode"] == "deep" and _think(again)["source"] == "explicit"


def test_a_turn_that_answered_leaves_no_empty_record_and_auto_may_pick_deep(server):
    sid = server.session("answered")
    server.script([{"reasoning": _reasoning(6), "text": "La respuesta es 42.", "finish_reason": "stop"}])
    first = server.turn(sid, ASK, think_mode="deep")
    assert not [e for e in first if e.get("type") == "empty_answer"]
    rows = server.history(sid)
    assert [r["content"] for r in rows if r["role"] == "assistant"] == ["La respuesta es 42."]
    assert not rows[1]["metadata"].get("empty_answer")
    server.script([{"text": "ok"}])
    second = server.turn(sid, ASK_AGAIN, think_mode="auto")
    assert "deep_spent_without_answer" not in _think(second)["reasons"]


def test_an_incognito_turn_persists_nothing(server):
    sid = server.session("incognito")
    server.script([REASONING_ONLY_LENGTH])
    events = server.turn(sid, ASK, think_mode="deep", incognito=True)
    assert not [e for e in events if e.get("type") in ("empty_answer", "message_saved")]
    assert server.history(sid) == []


@pytest.mark.parametrize("mode", ["chat", "agent"])
def test_stopping_the_turn_while_it_reasons_is_recorded_as_stopped(server, mode):
    sid = server.session(f"stopped-{mode}")
    server.script([{"reasoning": _reasoning(120), "text": "", "finish_reason": "length"}], delay=0.1)
    result: dict = {}
    worker = threading.Thread(target=lambda: result.update(events=server.turn(sid, ASK, mode=mode, think_mode="deep")))
    worker.start()
    assert server.thinking_seen.wait(90), "the model never started reasoning"
    time.sleep(1.0)  # a few reasoning chunks in
    assert server.stop(sid).get("stopped") is True
    worker.join(60)
    time.sleep(1.0)
    rows = server.history(sid)
    assert [r["role"] for r in rows] == ["user", "assistant"], rows
    md = rows[1]["metadata"]
    assert rows[1]["content"] == "" and md["empty_answer"] is True
    assert md["empty_answer_reason"] == "stopped" and md["stopped"] is True
    assert md["think_mode"]["mode"] == "deep"
    server.script([{"text": "ok"}])
    nxt = server.turn(sid, ASK_AGAIN, mode=mode, think_mode="auto")
    assert _think(nxt)["mode"] != "deep" and "deep_spent_without_answer" in _think(nxt)["reasons"]
