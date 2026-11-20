"""«Pasar a…» to Faustus itself, and CLI agents that live on the person's PC while Faustus runs on a Spark.

The PC side is a small HTTP server that speaks the Hub's ``/api/executors`` routes; the Faustus side is the real client."""
import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from core.models import ChatMessage
from src import hub_link, pc_executors
from src import thread_agents as ta


class FakeManager:
    def __init__(self):
        self.sessions = {}

    def get_session(self, sid):
        return self.sessions.setdefault(sid, SimpleNamespace(history=[]))

    def add_message(self, sid, message):
        self.get_session(sid).history.append(message)


class FakePcHub:
    """The PC Hub's executors routes: one run that ends after ``script`` lines, with the exit code given."""

    def __init__(self, programs=("claude",), lines=("hecho en el PC",), exit_code=0, slow=False):
        self.programs, self.lines, self.exit_code, self.slow = set(programs), list(lines), exit_code, slow
        self.started, self.cancelled, self.reads = [], [], []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, payload, status=200):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path, _, query = self.path.partition("?")
                if path == "/api/executors":
                    names = query.split("names=", 1)[-1].replace("%2C", ",").split(",")
                    return self._send({"ok": True, "host": "workstation", "executors": {
                        n: {"installed": n in outer.programs, "path": "C:/bin/" + n if n in outer.programs else ""}
                        for n in names if n}})
                if path.startswith("/api/executors/runs/"):
                    since = int(query.split("since=", 1)[-1] or 0) if "since=" in query else 0
                    outer.reads.append(since)
                    if outer.cancelled:
                        return self._send({"ok": True, "status": "cancelled", "exit_code": -9, "output": "", "offset": since,
                                           "more": False})
                    if outer.slow:
                        return self._send({"ok": True, "status": "running", "exit_code": None, "output": "", "offset": since,
                                           "more": False})
                    text = "\n".join(outer.lines) + "\n"
                    return self._send({"ok": True, "status": "done" if outer.exit_code == 0 else "error",
                                       "exit_code": outer.exit_code, "output": text[since:], "offset": len(text),
                                       "more": False})
                self._send({"error": "no"}, 404)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if self.path == "/api/executors/runs":
                    if body["argv"][0] not in outer.programs:
                        return self._send({"ok": False, "error": body["argv"][0] + " is not installed on this PC"}, 404)
                    outer.started.append(body)
                    return self._send({"ok": True, "id": "run1", "host": "workstation"})
                if self.path.endswith("/cancel"):
                    outer.cancelled.append(self.path)
                    return self._send({"ok": True})
                self._send({"error": "no"}, 404)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]

    def close(self):
        self.server.shutdown()


@pytest.fixture
def pc(tmp_path, monkeypatch):
    from src import constants
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path / "data"))
    manager = FakeManager()
    monkeypatch.setattr(ta, "_session_manager", lambda: manager)
    token = tmp_path / "token"
    token.write_text("t" * 20, encoding="utf-8")
    link = tmp_path / "link.json"
    hub = FakePcHub()
    link.write_text(json.dumps({"url": f"http://127.0.0.1:{hub.port}", "token_file": str(token)}), encoding="utf-8")
    monkeypatch.setenv(hub_link.ENV, str(link))
    # Nothing is installed on this machine (the Spark); external runners are on.
    from src import agent_runners as reg
    monkeypatch.setattr(reg, "_which", lambda names, which=None: None)
    monkeypatch.setattr(reg, "enabled", lambda: True)
    pc_executors.reset_cache()
    with ta._LOCK:
        ta._JOBS.clear()
    yield SimpleNamespace(hub=hub, manager=manager, tmp=tmp_path, link=link, monkeypatch=monkeypatch)
    hub.close()
    pc_executors.reset_cache()


def wait(sid, job_id, seconds=10):
    for _ in range(int(seconds / 0.05)):
        row = ta.job(sid, job_id)
        if row["status"] != "running":
            return row
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_the_agent_list_says_where_each_runner_lives(pc):
    rows = {r["key"]: r for r in ta.agents()}
    assert rows["faustus"]["where"] == "faustus"
    assert rows["claude"]["installed"] is True and rows["claude"]["where"] == "pc"
    assert rows["claude"]["where_label"] == "en el PC (workstation)"
    assert rows["codex"]["installed"] is False and rows["codex"]["where"] is None
    assert rows["codex"]["where_label"] == "no instalado ni aquí ni en el PC"


def test_without_a_link_the_list_is_what_this_machine_has(pc):
    pc.monkeypatch.delenv(hub_link.ENV)
    rows = {r["key"]: r for r in ta.agents()}
    assert rows["claude"]["installed"] is False and rows["claude"]["where_label"] == "no instalado"


def test_a_dead_hub_is_named_in_the_list(pc):
    pc.hub.close()
    pc_executors.reset_cache()
    rows = {r["key"]: r for r in ta.agents()}
    assert rows["claude"]["where"] is None and rows["claude"]["where_label"].endswith("el PC no responde")


def test_handoff_to_a_pc_runner_runs_there_with_the_task_and_returns_the_answer(pc):
    pc.manager.add_message("t1", ChatMessage("user", "estamos con el parser"))
    job = wait("t1", ta.start_handoff("t1", "claude", "revisa el parser", model="opus")["id"])
    assert job["status"] == "done" and job["where"] == "pc"
    sent = pc.hub.started[0]
    assert sent["argv"][0] == "claude" and "opus" in sent["argv"] and sent["cwd"] is None
    assert "estamos con el parser" in (sent["stdin"] if sent["stdin"] is not None else " ".join(sent["argv"]))
    last = pc.manager.get_session("t1").history[-1]
    assert last.content == "hecho en el PC" and last.metadata["kind"] == "handoff"
    assert ta.state("t1")["agent"] == "faustus"


def test_turns_with_a_pc_runner_start_it_again_each_time_and_do_not_check_the_folder_here(pc):
    state = ta.set_agent("t2", "claude", workspace="C:/Users/example-user/proyecto")
    assert state["workspace"] == "C:/Users/example-user/proyecto"
    job = wait("t2", ta.start_turn("t2", "hola")["id"])
    assert job["status"] == "done" and job["resumed"] is False
    assert pc.hub.started[0]["cwd"] == "C:/Users/example-user/proyecto"
    wait("t2", ta.start_turn("t2", "otra vez")["id"])
    assert len(pc.hub.started) == 2 and "resume" not in " ".join(pc.hub.started[1]["argv"])


def test_a_failing_pc_run_is_written_as_a_failure(pc):
    pc.hub.exit_code, pc.hub.lines = 2, ["se rompió"]
    job = wait("t3", ta.start_handoff("t3", "claude", "haz algo")["id"])
    assert job["status"] == "failed" and "code 2" in job["error"]
    assert "no terminó" in pc.manager.get_session("t3").history[-1].content


def test_cancelling_a_pc_run_tells_the_hub_to_stop_it(pc):
    pc.hub.slow = True
    started = ta.start_handoff("t4", "claude", "tarea larga")
    for _ in range(100):
        if pc.hub.started:
            break
        time.sleep(0.05)
    ta.cancel("t4", started["id"])
    assert wait("t4", started["id"])["status"] == "cancelled"
    assert pc.hub.cancelled == ["/api/executors/runs/run1/cancel"]


def test_a_runner_missing_everywhere_keeps_its_own_error(pc):
    job = wait("t5", ta.start_handoff("t5", "codex", "algo")["id"])
    assert job["status"] == "failed" and job["where"] == "local"


def test_pc_executor_refuses_without_link_and_with_runners_off(pc, monkeypatch):
    from src import agent_runners as reg
    monkeypatch.setattr(reg, "enabled", lambda: False)
    assert "off" in pc_executors.run_task("claude", "x")["error"]
    monkeypatch.setattr(reg, "enabled", lambda: True)
    monkeypatch.delenv(hub_link.ENV)
    assert "not linked" in pc_executors.run_task("claude", "x")["error"]


def test_pc_executor_reads_a_long_output_in_pieces(pc):
    chunks = []
    result = pc_executors.run_task("claude", "x", on_output=chunks.append, poll_s=0.01)
    assert result["ok"] and result["where"] == "pc" and result["host"] == "workstation"
    assert "".join(chunks).strip() == "hecho en el PC" and pc.hub.reads[0] == 0


# ── Faustus itself ───────────────────────────────────────────────────────────

@pytest.fixture
def fau(tmp_path, monkeypatch):
    from src import constants
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path / "data"))
    manager = FakeManager()
    monkeypatch.setattr(ta, "_session_manager", lambda: manager)
    monkeypatch.delenv(hub_link.ENV, raising=False)
    seen = {}

    from src.agent_tools import subagent_tools
    from src.swarm import runner as swarm

    def route(spec, owner):
        seen["spec"] = spec
        return "http://model.local/v1", spec.get("model") or "modelo-del-hilo", None, "session"
    monkeypatch.setattr(swarm, "resolve_route", route)

    async def worker(run, **kw):
        seen["kw"], seen["run"] = kw, run
        if seen.get("hang"):
            await asyncio.sleep(30)
        run.text = "resultado del subagente"
    monkeypatch.setattr(subagent_tools, "_run_subagent", worker)
    with ta._LOCK:
        ta._JOBS.clear()
    return SimpleNamespace(manager=manager, seen=seen)


def test_handoff_to_faustus_runs_a_subagent_on_the_thread_model(fau):
    fau.manager.add_message("t1", ChatMessage("user", "revisemos el contrato"))
    job = wait("t1", ta.start_handoff("t1", "faustus", "resume el contrato")["id"])
    assert job["status"] == "done" and job["where"] == "faustus" and job["agent"] == "faustus"
    assert fau.seen["spec"]["session_id"] == "t1" and fau.seen["spec"]["model"] == ""
    assert fau.seen["kw"]["model"] == "modelo-del-hilo" and fau.seen["kw"]["parent_session_id"] == "t1"
    prompt = fau.seen["run"].instruction
    assert "revisemos el contrato" in prompt and "Tarea:\nresume el contrato" in prompt
    history = fau.manager.get_session("t1").history
    assert history[-2].content.startswith("Pasar a Faustus") and history[-1].content == "resultado del subagente"
    assert history[-1].metadata["kind"] == "handoff" and ta.state("t1")["agent"] == "faustus"


def test_handoff_to_faustus_can_name_another_model_or_route(fau):
    wait("t1", ta.start_handoff("t1", "faustus", "tarea", model="otro-modelo", endpoint_id="ep-2")["id"])
    assert fau.seen["spec"] == {"session_id": "t1", "model": "otro-modelo", "endpoint_id": "ep-2"}
    assert fau.seen["kw"]["model"] == "otro-modelo"


def test_faustus_turns_still_use_the_normal_chat(fau):
    with pytest.raises(ValueError):
        ta.start_turn("t1", "hola")


def test_a_faustus_handoff_can_be_cancelled(fau):
    fau.seen["hang"] = True
    started = ta.start_handoff("t1", "faustus", "tarea larga")
    time.sleep(0.5)
    ta.cancel("t1", started["id"])
    row = wait("t1", started["id"])
    assert row["status"] == "cancelled"


def test_a_route_that_cannot_be_resolved_is_the_jobs_error(fau, monkeypatch):
    from src.swarm import runner as swarm

    def broken(spec, owner):
        raise swarm.ItemError("endpoint 'x' could not be resolved")
    monkeypatch.setattr(swarm, "resolve_route", broken)
    job = wait("t1", ta.start_handoff("t1", "faustus", "tarea")["id"])
    assert job["status"] == "failed" and "could not be resolved" in job["error"]
