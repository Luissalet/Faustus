"""Running an agent Faustus did not write, as a worker (src/external_worker.py).

Every test here drives a REAL process: a tiny python script the test writes and
a registry row that runs it. The four things being pinned are the four things
that make this shippable at all:

  * a hard timeout kills the process TREE — a bare kill leaves a shell's
    children running;
  * a rate-limited agent is REPORTED, never killed (§25.1's policy, applied to
    somebody else's binary);
  * outcomes are the four-value ones: a cancelled run is `cancelled`, not a
    failure, and a timeout is not a panic;
  * `argv_shown` never prints a secret.

And the one this module exists to say out loud: Faustus's command guard cannot
see inside another agent's own shell, so every result says `unguarded`.
"""
from __future__ import annotations

import os
import sys
import time

import pytest

from src import agent_runners as reg
from src import external_worker


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    """The feature ships off; these tests are about what it does when on."""
    monkeypatch.setattr(reg, "enabled", lambda: True)
    monkeypatch.setattr(reg, "timeout_s", lambda: 30)


def _agent(tmp_path, name: str, body: str) -> reg.Runner:
    """A fake agent: a python script, plus the registry row that runs it."""
    script = tmp_path / f"{name}.py"
    script.write_text(body, encoding="utf-8")
    return reg.Runner(
        key=name, label=f"Fake {name}", kind="cli", licence="open",
        install=f"ollama launch {name}",
        argv=(sys.executable, str(script), "{task}"),
        detect=(sys.executable,), notes="a test double",
    )


GOOD = """
import pathlib, sys
task = sys.argv[1]
print("thinking about:", task)
pathlib.Path("written_by_the_agent.txt").write_text(task, encoding="utf-8")
print("done")
"""

BAD = """
import sys
print("Traceback (most recent call last):", file=sys.stderr)
print("boom", file=sys.stderr)
sys.exit(3)
"""

SLOW = """
import os, sys, time
# A child that outlives a naive kill of the parent: the tree must go.
if os.name != "nt":
    if os.fork() == 0:
        time.sleep(120)
        os._exit(0)
print("started", flush=True)
time.sleep(120)
"""

RATE_LIMITED = """
import sys, time
print("HTTP 429 Too Many Requests", flush=True)
print("rate limit exceeded, retrying in 5s", flush=True)
time.sleep(0.6)
print("recovered; carrying on", flush=True)
print("done", flush=True)
"""


# ── the happy path ──────────────────────────────────────────────────────────

def test_a_runner_that_succeeds_reports_what_it_did(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    lines = []
    out = external_worker.run_task(_agent(tmp_path, "good", GOOD), "add apply_tax",
                                   workspace=str(ws), on_output=lines.append)
    assert out["ok"] is True and out["exit_code"] == 0
    assert out["outcome"] == "success" and out["status"] == "done"
    assert "thinking about: add apply_tax" in out["output_tail"]
    assert out["seconds"] >= 0 and out["timed_out"] is False and out["cancelled"] is False
    # it really ran in the workspace: the harness's diff is what will see this
    assert (ws / "written_by_the_agent.txt").read_text(encoding="utf-8") == "add apply_tax"
    # the output was streamed, not only returned at the end
    assert any("thinking about" in line for line in lines)
    # and every result says what could not be checked
    assert out["unguarded"] is True and "command guard" in out["guard_note"]


def test_a_runner_that_fails_is_an_error_not_a_crash_of_faustus(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    out = external_worker.run_task(_agent(tmp_path, "bad", BAD), "break it", workspace=str(ws))
    assert out["ok"] is False and out["exit_code"] == 3 and out["status"] == "error"
    assert "exited with code 3" in out["error"]
    assert "boom" in out["output_tail"]
    # its own traceback is the AGENT's, so the outcome is a panic of the agent,
    # never an exception out of run_task
    assert out["outcome"] in ("panic", "expected_error")


# ── the timeout: the tree, not just the process ────────────────────────────

@pytest.mark.skipif(os.name == "nt", reason="the fork-based child is POSIX")
def test_a_timeout_kills_the_process_tree_and_is_not_an_error(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    started = time.time()
    out = external_worker.run_task(_agent(tmp_path, "slow", SLOW), "hang", workspace=str(ws),
                                   timeout_s=1.0)
    assert time.time() - started < 30, "the hard timeout must bound the run"
    assert out["ok"] is False and out["timed_out"] is True and out["killed"] is True
    assert out["status"] == "timeout" and "process tree was killed" in out["error"]
    # A timeout is a bounded, expected end — not a panic and not a failure of
    # the machinery (src/tool_outcome.py's four values).
    assert out["outcome"] == "expected_error"
    assert out["outcome"] != "panic"
    # nothing of the tree is left running
    time.sleep(0.3)
    ours = os.popen("ps -eo pid,command").read()
    assert "slow.py" not in ours, ours


def test_a_cancelled_run_is_cancelled_not_a_failure(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    stop = {"at": time.time() + 0.3}
    out = external_worker.run_task(_agent(tmp_path, "slow2", SLOW), "hang", workspace=str(ws),
                                   timeout_s=60.0, should_cancel=lambda: time.time() > stop["at"])
    assert out["cancelled"] is True and out["killed"] is True and out["timed_out"] is False
    assert out["outcome"] == "cancelled", "somebody stopped it: that is not a failure"
    assert out["status"] == "cancelled"


# ── a blocked agent is reported, never killed ──────────────────────────────

def test_a_rate_limited_agent_is_reported_and_left_alone(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    out = external_worker.run_task(_agent(tmp_path, "limited", RATE_LIMITED), "work",
                                   workspace=str(ws), timeout_s=30.0)
    # It was NOT killed for saying it was rate limited: it ran to its own end.
    assert out["killed"] is False and out["timed_out"] is False
    assert out["ok"] is True and out["exit_code"] == 0
    assert "recovered; carrying on" in out["output_tail"]
    # …and the state is reported, with the literal that proves it
    assert out["state"] == "rate_limited" and "rate_limited" in out["states"]
    assert out["why"] and out["matched"]


# ── argv_shown is safe to print ────────────────────────────────────────────

def test_argv_shown_redacts_a_secret_looking_env_value(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    runner = reg.Runner(
        key="secretive", label="Secretive", kind="cli", licence="unknown",
        argv=(sys.executable, "-c", "print('hi')"),
        env={"AGENT_API_KEY": "{model}", "AGENT_BASE_URL": "{endpoint}"},
        detect=(sys.executable,),
    )
    out = external_worker.run_task(runner, "x", workspace=str(ws), model="sk-live-abc123",
                                   endpoint="http://127.0.0.1:11434", timeout_s=30.0)
    assert out["ok"] is True
    assert "sk-live-abc123" not in out["argv_shown"]
    assert "AGENT_API_KEY=" in out["argv_shown"] and external_worker.REDACTED in out["argv_shown"]
    # a value that is not secret-looking is shown, so the command is readable
    assert "http://127.0.0.1:11434" in out["argv_shown"]


def test_redact_env_only_touches_secret_looking_names():
    got = external_worker.redact_env({"OPENAI_API_KEY": "sk-1", "ANTHROPIC_BASE_URL": "http://x",
                                      "TOKEN": "t", "MY_PASSWORD": "p", "PATH": "/bin",
                                      "SOMETHING_TOKENS": "many"})
    assert got == {"OPENAI_API_KEY": "***", "ANTHROPIC_BASE_URL": "http://x", "TOKEN": "***",
                   "MY_PASSWORD": "***", "PATH": "/bin", "SOMETHING_TOKENS": "***"}


# ── refusals: a result, never an exception ─────────────────────────────────

def test_an_unknown_runner_is_a_result_with_a_reason(tmp_path):
    out = external_worker.run_task("no-such-agent", "x", workspace=str(tmp_path))
    assert out["ok"] is False and "unknown agent runner" in out["error"]
    assert out["outcome"] == "expected_error" and out["exit_code"] is None


def test_the_setting_off_refuses_before_starting_anything(tmp_path, monkeypatch):
    monkeypatch.setattr(reg, "enabled", lambda: False)
    out = external_worker.run_task(_agent(tmp_path, "good2", GOOD), "x", workspace=str(tmp_path))
    assert out["ok"] is False and "agent_external_runners" in out["error"]
    assert "third-party binaries" in out["error"]


def test_a_gui_is_never_a_worker(tmp_path):
    gui = reg.Runner(key="vscode", label="VS Code", kind="app", licence="unknown", detect=("code",))
    out = external_worker.run_task(gui, "x", workspace=str(tmp_path))
    assert out["ok"] is False and "GUI application" in out["error"]


def test_an_agent_with_no_recorded_invocation_says_so(tmp_path):
    row = reg.Runner(key="cline", label="Cline", kind="cli", licence="open", argv=(), detect=("cline",))
    out = external_worker.run_task(row, "x", workspace=str(tmp_path))
    assert out["ok"] is False and reg.NOT_RUNNABLE_NOTE in out["error"]


def test_a_missing_binary_names_the_install_command(tmp_path):
    row = reg.Runner(key="ghost", label="Ghost", kind="cli", licence="open",
                     install="ollama launch ghost",
                     argv=("definitely-not-a-real-binary-xyz", "{task}"), detect=("ghost",))
    out = external_worker.run_task(row, "x", workspace=str(tmp_path))
    assert out["ok"] is False and "not installed" in out["error"]
    assert "ollama launch ghost" in out["error"]


def test_a_workspace_that_is_not_a_directory_is_refused(tmp_path):
    out = external_worker.run_task(_agent(tmp_path, "good3", GOOD), "x",
                                   workspace=str(tmp_path / "nope"))
    assert out["ok"] is False and "not a usable directory" in out["error"]


def test_an_output_callback_that_explodes_does_not_break_the_run(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()

    def boom(_line):
        raise RuntimeError("the board is on fire")

    out = external_worker.run_task(_agent(tmp_path, "good4", GOOD), "x", workspace=str(ws),
                                   on_output=boom)
    assert out["ok"] is True and "done" in out["output_tail"]


def test_the_output_kept_is_bounded(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    loud = _agent(tmp_path, "loud", "for i in range(20000):\n    print('x' * 60)\n")
    out = external_worker.run_task(loud, "x", workspace=str(ws), timeout_s=60.0)
    assert out["ok"] is True
    assert len(out["output_tail"]) <= external_worker.RESULT_TAIL_CHARS
    assert out["output_chars"] > external_worker.OUTPUT_TAIL_CHARS
