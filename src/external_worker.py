"""external_worker.py — run an agent Faustus did not write, inside the harness
Faustus does own.

    run_task("claude", "add apply_tax and a test", workspace="D:/proj")

The point is NOT to launch somebody's CLI: the user can type its name. The
point is that a third-party agent can be a WORKER — checkpoint before, diff
after, `claimed_only`, Faustus's own verification, the honest `prove` verdict
— exactly like the built-in sub-agents (src/dispatch.py, FAUSTUS.md §22).

**What this module cannot promise, said once and never hidden**

    Faustus's command guard CANNOT see inside another agent's own shell.

Every command a built-in worker runs goes through the tool gate: the
destructive-command guard, the workspace roots, the approval cards, the
per-worker file locks. An external agent runs its own loop in its own process:
Faustus starts it, bounds it in time, watches its output and reads the disk
afterwards — and that is all it can honestly claim. Nothing here inspects,
approves or blocks the commands that agent decides to run. That is why the
dispatch path adds an explicit `external_agent_unguarded` entry to the proof's
uncertainty list (src/prove.py), and why the setting that allows any of this
ships **off**.

What this module DOES guarantee:

* **a hard timeout with the process TREE killed** — the same helpers the Bash
  tool uses (`src.agent_tools.subprocess_tools._kill_tree`), because a bare
  `proc.kill()` leaves the children of a shell running (seen live on Windows);
* **the output is read, not policed** — every chunk goes to `on_output` and
  the tail is classified by `src.output_rules`. An agent that is rate-limited
  or sitting at a prompt is REPORTED, never killed for it (§25.1). Only the
  hard timeout and an explicit cancel ever kill anything;
* **four-value outcomes** — `src.tool_outcome`: a run the user cancelled is
  `cancelled`, not a failure, and a timeout is an `expected_error`, not a
  panic;
* **`argv_shown` is safe to display** — the command as it ran, with any
  secret-looking environment value replaced by `***`.

The setting gate (`agent_external_runners`, default off) is checked here as
well as by the caller: a module that starts third-party binaries does not
assume somebody else remembered.

Stdlib only. `run_task` is synchronous and returns a plain dict; the async
caller runs it in a thread.
"""
from __future__ import annotations

import logging
import os
import re
import shlex
import subprocess
import threading
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

#: How much of the agent's output is kept (characters). The rules only read
#: their own tail; this bounds what one run holds in memory.
OUTPUT_TAIL_CHARS = 16384
#: How much of it is returned in `output_tail`.
RESULT_TAIL_CHARS = 4000
#: Polling interval of the supervision loop (the reader is a thread; this loop
#: only watches the clock and the cancel flag).
POLL_S = 0.05
#: Longest token printed in `argv_shown`, and the longest `argv_shown`.
_TOKEN_CHARS = 300
_SHOWN_CHARS = 2000

#: Environment names whose VALUE is never shown.
_SECRET_RE = re.compile(r"(?:^|_)(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|PWD|AUTH|CREDENTIALS?|SESSION|COOKIE)S?$",
                        re.IGNORECASE)
REDACTED = "***"


def _outcome(name: str, *, error: Any = None, cancelled: bool = False) -> str:
    """The four-value outcome as a plain string (src/tool_outcome.py)."""
    try:
        from src import tool_outcome
        return tool_outcome.classify_status(name, error=error, cancelled=cancelled).value
    except Exception:  # noqa: BLE001 - never raise out of a worker result
        return "cancelled" if cancelled else ("success" if name == "done" else "expected_error")


def _classify(text: str) -> Dict[str, Any]:
    """What the agent's own output says about it. Reported, never acted on."""
    try:
        from src import output_rules
        verdict = output_rules.classify_output(text)
        states = list(verdict.get("states") or [])
        matches = verdict.get("matches") or []
        return {
            "state": states[0] if states else "",
            "states": states,
            "why": output_rules.why(verdict, states[0]) if states else "",
            "matched": str((matches[0] or {}).get("literal") or "") if matches else "",
            "confidence": verdict.get("confidence"),
        }
    except Exception as e:  # noqa: BLE001 - the rules never break a run
        logger.debug("external_worker: output rules unavailable: %s", e)
        return {"state": "", "states": [], "why": "", "matched": "", "confidence": None}


def redact_env(env: Optional[Dict[str, str]]) -> Dict[str, str]:
    """The environment this table adds, with secret-looking values replaced."""
    out: Dict[str, str] = {}
    for name, value in (env or {}).items():
        key = str(name)
        out[key] = REDACTED if _SECRET_RE.search(key) else str(value)
    return out


def _shown(argv: List[str], env: Optional[Dict[str, str]]) -> str:
    """The command as it ran, safe to print: `NAME=value … prog args`."""
    parts: List[str] = []
    for name, value in sorted(redact_env(env).items()):
        parts.append(f"{name}={shlex.quote(value)}")
    for token in argv:
        text = str(token)
        if len(text) > _TOKEN_CHARS:
            text = text[: _TOKEN_CHARS - 1] + "…"
        parts.append(shlex.quote(text))
    line = " ".join(parts)
    return line if len(line) <= _SHOWN_CHARS else line[: _SHOWN_CHARS - 1] + "…"


def _fail(runner_key: str, reason: str, *, argv_shown: str = "") -> Dict[str, Any]:
    """A refusal, in the shape of a result — never an exception."""
    return {
        "ok": False, "exit_code": None, "outcome": "expected_error", "output_tail": "",
        "state": "", "states": [], "why": "", "matched": "", "seconds": 0.0,
        "argv_shown": argv_shown, "runner": str(runner_key or ""), "error": reason,
        "timed_out": False, "cancelled": False, "killed": False, "unguarded": True,
    }


class _Buffer:
    """The tail of the agent's output, bounded, appended from a reader thread."""

    def __init__(self, limit: int = OUTPUT_TAIL_CHARS):
        self.limit = int(limit)
        self._lock = threading.Lock()
        self._text = ""
        self.total = 0

    def add(self, chunk: str) -> None:
        with self._lock:
            self.total += len(chunk)
            text = self._text + chunk
            self._text = text[-self.limit:] if len(text) > self.limit else text

    def text(self) -> str:
        with self._lock:
            return self._text


def run_task(runner_key: Any, task: str, *, workspace: Optional[str] = None,
             model: Optional[str] = None, endpoint: Optional[str] = None,
             timeout_s: Optional[float] = None,
             on_output: Optional[Callable[[str], None]] = None,
             should_cancel: Optional[Callable[[], bool]] = None,
             env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Run ONE task with one external agent and report what happened.

    ``runner_key`` is a key or alias from src/agent_runners.py — or a
    :class:`~src.agent_runners.Runner` itself, so a caller can run a row that
    is not in the shipped table.

    Returns::

        {"ok", "exit_code", "outcome", "output_tail", "state", "why",
         "seconds", "argv_shown", …}

    ``outcome`` is a `src.tool_outcome.Outcome` value: a cancelled run is
    `cancelled` (not a failure), a timeout is `expected_error`, a non-zero exit
    is read from its own output. ``state``/``why`` are what the agent's output
    said about itself — a `rate_limited` agent is reported here and was NOT
    killed for it.

    Never raises: every failure — an unknown runner, one that is not
    installed, a binary that will not start — comes back as a result with
    ``error`` set.
    """
    started = time.time()
    from src import agent_runners as reg

    runner = runner_key if isinstance(runner_key, reg.Runner) else reg.get(runner_key)
    key = runner.key if runner is not None else str(runner_key or "")
    if runner is None:
        return _fail(key, f"unknown agent runner: {runner_key!r} — see GET /api/agent-runners "
                          f"for the ones this machine knows")
    if not reg.enabled():
        return _fail(key, "external agent runners are off: turn on Settings → Agent & automation → "
                          "`agent_external_runners` (it ships off because it runs third-party binaries "
                          "on this machine)")
    if runner.kind != "cli":
        return _fail(key, f"{runner.label} is a GUI application, not a worker: it has no one-task, "
                          f"one-exit invocation")
    if not runner.argv:
        return _fail(key, f"{runner.label} is {reg.NOT_RUNNABLE_NOTE}: Faustus has no row saying how to "
                          f"run one task with it (src/agent_runners.py)")

    cwd: Optional[str] = None
    if runner.cwd_is_workspace:
        folder = str(workspace or "").strip()
        if not folder or not os.path.isdir(folder):
            return _fail(key, f"workspace is not a usable directory for {runner.label}: {workspace!r}")
        cwd = folder

    argv = reg.build_argv(runner, str(task or ""), model=model, cwd=cwd, endpoint=endpoint)
    if not argv:
        return _fail(key, f"{runner.label}: the table produced an empty command for this task")
    table_env = reg.table_env(runner, model=model, cwd=cwd, endpoint=endpoint)
    shown = _shown(argv, table_env)

    full_env = reg.build_env(runner, base=env, model=model, cwd=cwd, endpoint=endpoint)
    exe = None
    try:
        import shutil
        exe = shutil.which(argv[0], path=full_env.get("PATH"))
    except Exception:  # noqa: BLE001
        exe = None
    if not exe:
        return _fail(key, f"{runner.label} is not installed on this machine ({argv[0]} is not on PATH). "
                          f"Install it with: {runner.install or ('ollama launch ' + runner.key)}",
                     argv_shown=shown)

    limit = float(timeout_s if timeout_s is not None else reg.timeout_s())
    limit = max(1.0, limit)
    buf = _Buffer()

    popen_kwargs: Dict[str, Any] = {
        "cwd": cwd, "env": full_env, "stdout": subprocess.PIPE, "stderr": subprocess.STDOUT,
        "stdin": subprocess.PIPE if runner.stdin_task else subprocess.DEVNULL,
        "text": True, "bufsize": 1,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        # Its own session, so the whole tree can be killed (see _kill_tree).
        popen_kwargs["start_new_session"] = True
    try:
        # The resolved absolute path, so the child is the binary this module
        # checked for and not whatever a different PATH would have found.
        proc = subprocess.Popen([exe] + list(argv[1:]), **popen_kwargs)   # noqa: S603 - argv from the table
    except Exception as e:  # noqa: BLE001
        return _fail(key, f"{runner.label} could not be started: {type(e).__name__}: {e}"[:300],
                     argv_shown=shown)

    if runner.stdin_task:
        try:
            proc.stdin.write(str(task or "") + "\n")           # type: ignore[union-attr]
            proc.stdin.close()                                  # type: ignore[union-attr]
        except Exception as e:  # noqa: BLE001
            logger.debug("external_worker: %s would not take the task on stdin: %s", key, e)

    def _read() -> None:
        stream = proc.stdout
        if stream is None:
            return
        try:
            for line in iter(stream.readline, ""):
                buf.add(line)
                if on_output is not None:
                    try:
                        on_output(line)
                    except Exception as e:  # noqa: BLE001 - a callback never breaks the run
                        logger.debug("external_worker: on_output failed: %s", e)
        except Exception as e:  # noqa: BLE001
            logger.debug("external_worker: reading %s failed: %s", key, e)
        finally:
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass

    reader = threading.Thread(target=_read, name=f"external-worker-{key}", daemon=True)
    reader.start()

    from src.agent_tools.subprocess_tools import _kill_tree

    deadline = time.monotonic() + limit
    timed_out = cancelled = killed = False
    while True:
        if proc.poll() is not None:
            break
        if should_cancel is not None:
            try:
                stop = bool(should_cancel())
            except Exception:  # noqa: BLE001
                stop = False
            if stop:
                cancelled = killed = True
                _kill_tree(proc)
                break
        if time.monotonic() >= deadline:
            # The ONLY two reasons anything here kills a process: the clock,
            # and an explicit cancel. Never a state read off the output.
            timed_out = killed = True
            _kill_tree(proc)
            break
        time.sleep(POLL_S)

    try:
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001
        try:
            _kill_tree(proc)
        except Exception:  # noqa: BLE001
            pass
    reader.join(timeout=5)

    seconds = round(time.time() - started, 3)
    exit_code = proc.returncode
    tail = buf.text()
    read = _classify(tail)
    if cancelled:
        status, error = "cancelled", ""
    elif timed_out:
        status = "timeout"
        error = f"{runner.label} was stopped after {int(limit)}s (its process tree was killed)"
    elif exit_code == 0:
        status, error = "done", ""
    else:
        status = "error"
        error = f"{runner.label} exited with code {exit_code}"
        if read.get("state"):
            error += f" — its output says {read['state']}"
    return {
        "ok": bool(exit_code == 0 and not timed_out and not cancelled),
        "exit_code": exit_code,
        "outcome": _outcome(status, error=error or None, cancelled=cancelled),
        "output_tail": tail[-RESULT_TAIL_CHARS:],
        "output_chars": buf.total,
        "state": read.get("state") or "",
        "states": read.get("states") or [],
        "why": read.get("why") or "",
        "matched": read.get("matched") or "",
        "seconds": seconds,
        "argv_shown": shown,
        "runner": key,
        "label": runner.label,
        "status": status,
        "error": error,
        "timed_out": timed_out,
        "cancelled": cancelled,
        "killed": killed,
        # Said in every result, not only in the docs: this run was not seen by
        # the command guard.
        "unguarded": True,
        "guard_note": reg.GUARD_NOTE,
    }


__all__ = ["OUTPUT_TAIL_CHARS", "REDACTED", "RESULT_TAIL_CHARS", "redact_env", "run_task"]
