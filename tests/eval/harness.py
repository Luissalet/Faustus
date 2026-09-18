"""tests/eval/harness.py — EVAL-01: drive one real turn end to end and read
back the same signals the reliability harness already defines, instead of
trusting the model's own prose.

"Real flow" here means exactly what it says in the lot: a real Faustus
server process (subprocess `uvicorn app:app`, the same recipe
`tests/e2e/conftest.py::app_server` already uses and proves works) talking
to a real `agent_loop` turn through the real `/api/chat_stream` route, with
the MODEL replaced by a scripted, recorded HTTP endpoint
(`tests/e2e/fake_llm.py` — reused, not duplicated: a second copy of that
server would be the "segunda autoridad" hard rule 4 forbids). No mocks
inside the application itself: every tool call the fixture's script names
actually runs against a real temp workspace on disk.

What one call to `send_turn` measures, using only public wire events the
route already emits (`src/agent_loop.py::stream_agent_loop`'s own docstring
names them: `tool_start`, `tool_output`, `agent_step`, `metrics`, `ask_user`,
`[DONE]` — nothing here reaches into agent_loop internals):

  * **rounds**       — the highest `round` any event carried, +1.
  * **tools used**    — one entry per `tool_start` event; a tool offered but
    never called is not in this list, by construction (the approval branch
    of agent_loop never yields `tool_start` for a tool it blocked — see the
    lot report).
  * **tools offered**  — best-effort: the tool names named in the system
    prompt of the FIRST model call (`fake_llm`'s own record of what it
    received). Approximate on purpose and documented as such in the lot
    report: a long system prompt is not reproduced by the fake server, only
    the first 4000 characters of each message are kept.
  * **tokens**        — the final `metrics` event's `data`, verbatim.
  * **verified**      — left to the caller: `run_task` below calls
    `src.verification.run_verifier` for the fixtures that touch code, and a
    fixture-specific content check for the ones that do not (see
    `tests/eval/tasks.py`).

A turn that hits the external-untrusted-context approval gate (any turn that
reads a file before writing one, in this application's real security model —
see `src/tool_capabilities.py`) is resumed automatically for "this task"
scope, exactly as clicking "Allow for this task" does in
`tests/e2e/test_agent_flows.py`; this module is the non-browser way to do the
same thing. Bounded by `max_approvals` so a fixture that goes wrong pauses
forever, not the test.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO = Path(__file__).resolve().parents[2]

#: `approve_task` — src/tool_approval_scopes.py::TASK_APPROVAL_DECISION.
#: Not imported from there: that module lives in the PARENT process's Python
#: path only after `sys.path` is set up by the app, and this harness talks to
#: a *subprocess* over HTTP — the string is the wire contract, same as a
#: browser clicking "Allow for this task" never imports the constant either.
TASK_APPROVAL_DECISION = "approve_task"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_http(url: str, timeout: float = 90.0) -> None:
    t0 = time.time()
    last: Optional[BaseException] = None
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                if r.status == 200:
                    return
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(0.5)
    raise RuntimeError(f"{url} did not come up: {last}")


def _post_form(url: str, data: Dict[str, Any]) -> Dict[str, Any]:
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


@dataclass
class TurnResult:
    session_id: str
    rounds: int = 0
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    tools_offered: Tuple[str, ...] = ()
    approvals_resolved: int = 0
    text: str = ""
    metrics: Dict[str, Any] = field(default_factory=dict)
    events: List[Dict[str, Any]] = field(default_factory=list)
    finished: bool = False

    def tools_used(self) -> Tuple[str, ...]:
        seen: List[str] = []
        for call in self.tool_calls:
            name = str(call.get("tool") or "")
            if name and name not in seen:
                seen.append(name)
        return tuple(seen)


class EvalApp:
    """One subprocess Faustus server + one scripted model, for the life of a
    test session. Mirrors `tests/e2e/conftest.py::app_server`/`fake_llm`
    exactly — those fixtures are opt-in (Playwright-gated) and this suite
    must run without a browser, so the pieces are reassembled here rather
    than imported as fixtures, while the SERVER (`tests.e2e.fake_llm`) is
    reused verbatim."""

    def __init__(self) -> None:
        self.data_dir = tempfile.mkdtemp(prefix="odysseus-eval-data-")
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self._fake_llm_port = _free_port()
        self._fake_llm_base = f"http://127.0.0.1:{self._fake_llm_port}"
        self._fake_srv = None
        self._proc: Optional[subprocess.Popen] = None
        self.endpoint_id = ""

    def start(self, timeout: float = 90.0) -> None:
        from tests.e2e import fake_llm as _fake_llm_mod
        self._fake_srv = _fake_llm_mod.serve(self._fake_llm_port)

        env = dict(os.environ)
        env.update({
            "ODYSSEUS_DATA_DIR": self.data_dir,
            "DATABASE_URL": "sqlite:///" + self.data_dir.replace("\\", "/") + "/app.db",
            "APP_PORT": str(self.port),
            "LOCALHOST_BYPASS": "true",
            "AUTH_ENABLED": "false",
            "ODYSSEUS_INPROCESS_POLLERS": "0",
            "ODYSSEUS_INPROCESS_TASKS": "0",
            "ODYSSEUS_STARTUP_WARMUPS": "0",
            "PYTHONUNBUFFERED": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        })
        log_path = os.path.join(self.data_dir, "server.log")
        self._log = open(log_path, "w", encoding="utf-8")
        self._proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(self.port)],
            cwd=str(REPO), env=env, stdout=self._log, stderr=subprocess.STDOUT,
        )
        try:
            _wait_http(self.base + "/api/chat/activity", timeout=timeout)
        except Exception:
            self.stop()
            raise
        ep = _post_form(self.base + "/api/model-endpoints", {
            "name": "eval-fake", "base_url": self._fake_llm_base + "/v1",
            "skip_probe": "true", "endpoint_kind": "local",
        })
        self.endpoint_id = ep.get("id") or (ep.get("endpoint") or {}).get("id") or ""

    def stop(self) -> None:
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        if self._fake_srv is not None:
            self._fake_srv.shutdown()
        try:
            self._log.close()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(self.data_dir, ignore_errors=True)

    # -- the model's script ------------------------------------------------

    def script(self, responses: List[str]) -> None:
        """Record the canned answers `fake_llm` hands back, one per model
        call this turn makes — the "fixtures que graban las respuestas del
        modelo" EVAL-01 asks for."""
        body = json.dumps({"responses": responses, "reset": True}).encode("utf-8")
        req = urllib.request.Request(self._fake_llm_base + "/_script", data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read()

    def _fake_calls(self) -> Dict[str, Any]:
        with urllib.request.urlopen(self._fake_llm_base + "/_calls", timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))

    # -- sessions & turns ----------------------------------------------------

    def new_session(self, name: str = "eval") -> str:
        r = _post_form(self.base + "/api/session", {
            "name": name, "endpoint_id": self.endpoint_id, "endpoint_url": self._fake_llm_base + "/v1",
            "model": "fake-coder", "skip_validation": "true",
        })
        return r.get("id") or r.get("session_id")

    def _post_chat_stream(self, form: Dict[str, Any], timeout: float) -> List[Dict[str, Any]]:
        """One POST to /api/chat_stream, read to completion (either [DONE]
        or the stream just ending, which is what a paused-for-approval turn
        does), decoded into the JSON payload of every `data:` line."""
        body = urllib.parse.urlencode({k: v for k, v in form.items() if v is not None}).encode("utf-8")
        req = urllib.request.Request(self.base + "/api/chat_stream", data=body, method="POST")
        events: List[Dict[str, Any]] = []
        with urllib.request.urlopen(req, timeout=timeout) as r:
            for raw in r:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[len("data:"):].strip()
                if payload == "[DONE]":
                    break
                try:
                    events.append(json.loads(payload))
                except ValueError:
                    continue
        return events

    def send_turn(self, session_id: str, message: str, *, workspace: Optional[str] = None,
                 max_approvals: int = 5, timeout: float = 60.0) -> TurnResult:
        result = TurnResult(session_id=session_id)
        offered_captured = False
        form: Dict[str, Any] = {"session": session_id, "message": message, "mode": "agent"}
        if workspace:
            form["workspace"] = workspace
        for _leg in range(max_approvals + 1):
            events = self._post_chat_stream(form, timeout)
            if not offered_captured:
                calls = self._fake_calls()
                result.tools_offered = _tool_names_from_messages(calls.get("last_messages") or [])
                offered_captured = True
            approval_id = None
            for ev in events:
                result.events.append(ev)
                etype = ev.get("type")
                if "delta" in ev and isinstance(ev.get("delta"), str):
                    result.text += ev["delta"]
                if etype in ("tool_start", "agent_step"):
                    try:
                        result.rounds = max(result.rounds, int(ev.get("round") or 0) + 1)
                    except (TypeError, ValueError):
                        pass
                if etype == "tool_start":
                    result.tool_calls.append({"tool": ev.get("tool"), "round": ev.get("round")})
                if etype == "metrics" and isinstance(ev.get("data"), dict):
                    result.metrics = ev["data"]
                if etype == "ask_user" and isinstance(ev.get("data"), dict) and ev["data"].get("kind") == "tool_approval":
                    approval_id = ev["data"].get("approval_id")
            if approval_id is None:
                result.finished = True
                break
            result.approvals_resolved += 1
            form = {
                "session": session_id, "mode": "agent",
                "tool_approval_id": approval_id, "tool_approval_decision": TASK_APPROVAL_DECISION,
            }
            if workspace:
                form["workspace"] = workspace
        result.rounds = max(result.rounds, 1)
        self._drain(session_id)
        return result

    def _drain(self, session_id: str, timeout: float = 5.0) -> None:
        """Wait out the turn's own fire-and-forget background jobs (auto-name,
        memory/skill extraction — `routes/chat_helpers.py::run_post_response_tasks`,
        queued sequentially and NOT part of the SSE stream this harness just
        finished reading) before the next task resets `fake_llm`'s script.

        Without this, a background extraction call from task N can still be
        in flight when task N+1 calls `script()`, and it eats the FIRST
        scripted response meant for task N+1's real turn — observed live: a
        4-task run left the 5th task's tool call unexecuted because a
        straggler skill-extraction request (fired only when a turn made >= 2
        tool calls) consumed its script entry. `/api/chat/activity` does not
        track these — they are not a chat run — so this also sleeps a fixed
        grace period; both are heuristic and documented here rather than
        silently relied on."""
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                with urllib.request.urlopen(self.base + "/api/chat/activity", timeout=5) as r:
                    activity = json.loads(r.read().decode("utf-8"))
            except Exception:  # noqa: BLE001 - best effort
                break
            if session_id not in (activity.get("running") or []):
                break
            time.sleep(0.2)
        time.sleep(1.0)


def _tool_names_from_messages(messages: List[Dict[str, Any]]) -> Tuple[str, ...]:
    """Best-effort tool names offered this turn, read out of whichever part
    of the system prompt `fake_llm` kept (it truncates every message to 4000
    chars — see the lot report's Limitaciones). Every tool line the prompt
    builder emits starts `- \\`name\\`` (src/agent_loop.py::_compact_tool_line's
    own fallback, and the shape every other branch of that function falls
    back to as well), so that prefix is all this looks for."""
    import re
    names: List[str] = []
    pattern = re.compile(r"^-\s+`([a-zA-Z_][a-zA-Z0-9_]{1,60})`", re.MULTILINE)
    for msg in messages:
        if str(msg.get("role") or "") != "system":
            continue
        content = str(msg.get("content") or "")
        for m in pattern.finditer(content):
            name = m.group(1)
            if name not in names:
                names.append(name)
    return tuple(names)
