"""external_worker.py — run an agent Faustus did not write, inside the harness
Faustus does own.

    run_task("claude", "add apply_tax and a test", workspace="D:/proj")

The point is NOT to launch somebody's CLI: the user can type its name. The
point is that a third-party agent can be a WORKER — checkpoint before, diff
after, `claimed_only`, Faustus's own verification, the honest `prove` verdict
— exactly like the built-in sub-agents (src/dispatch.py, FAUSTUS.md §22).

**What this module can and cannot promise, said once and never hidden**

For most runners, still the original sentence:

    Faustus's command guard CANNOT see inside another agent's own shell.

Every command a built-in worker runs goes through the tool gate: the
destructive-command guard, the workspace roots, the approval cards, the
per-worker file locks. An agent Faustus did not write runs its own loop in its
own process; Faustus starts it, bounds it in time, watches its output and
reads the disk afterwards, and for a runner with `gate: "none"` that is all it
can honestly claim. Those runs still carry `external_agent_unguarded` into the
proof's uncertainty list (src/prove.py), and the setting that allows any of
this still ships **off**.

**For a runner whose own interface can be gated, that sentence no longer
holds.** A row with `gate: "hook"` (src/agent_runners.py — today: Claude Code)
is started with a per-run pre-tool hook pointed back at src/agent_gate.py.
Every tool call it makes is judged by Faustus's OWN command guard, workspace
roots and file locks before it runs, and Faustus's refusal is binding — the
hook's deny holds even under the agent's most permissive mode. Such a run's
result carries `unguarded: False` and a `gate` block saying how many calls were
judged, how many refused, and how many the gate did not recognise.

The narrower thing a gated run still cannot promise, and it is in every
result:

* the gate sees TOOL CALLS. A program started by an allowed command has
  children, and they are not tool calls;
* a tool name the gate does not recognise is ALLOWED and counted as unjudged,
  because a gate that walled off every tool a foreign CLI added would break it
  on release day. Those calls are named and reach `prove` as their own
  uncertainty;
* the CLI's own `stream-json` output is reconciled against the gate's ledger
  afterwards, so a call the hook never fired for is *detected* rather than
  assumed judged — but it is detected after the fact, not prevented.

What this module DOES guarantee, gated or not:

* **a hard timeout with the process TREE killed** — the same helpers the Bash
  tool uses (`src.agent_tools.subprocess_tools._kill_tree`), because a bare
  `proc.kill()` leaves the children of a shell running (seen live on Windows);
* **the output is read, not policed** — every chunk goes to `on_output` and
  the tail is classified by `src.output_rules`. An agent that is rate-limited
  or sitting at a prompt is REPORTED, never killed for it (§25.1). Only the
  hard timeout, an explicit cancel or an invalid/oversized output stream kill
  the process; ordinary silence is not a termination condition;
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

import hashlib
import json
import logging
import os
import re
import shlex
import subprocess
import threading
import time
import uuid
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


def task_looks_sensitive(task: Any) -> bool:
    """Does this prompt carry something that must not be published in argv?

    The same rule the log redaction uses (`core.log_safety`): if redacting the
    text changes it, the text contains a value shaped like a credential. Not a
    classifier and not trying to be one — a cheap, explainable test whose
    false positives cost one flag on the call and whose false negatives are no
    worse than what argv did before.
    """
    text = str(task or "")
    if not text:
        return False
    try:
        from core.log_safety import redact_secrets
    except ImportError:  # pragma: no cover - core always imports in the app
        return False
    return redact_secrets(text) != text


def task_descriptor(task: str) -> str:
    """How a task is referred to in `argv_shown`: a digest and a length.

    SEC-1 (B-022). The prompt belongs to the run's own record, which is where
    it can be read deliberately. It does not belong in the command field, which
    is printed in the board, stored with the result and quoted in errors — and
    which, for a runner that has to use argv, is already the least private part
    of the run. The digest is enough to tell two runs apart and to check that
    the text on file is the text that ran.
    """
    raw = str(task or "")
    digest = hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:12]
    return f"<task:sha256={digest} chars={len(raw)}>"


def _shown(argv: List[str], env: Optional[Dict[str, str]], task: str = "") -> str:
    """The command as it ran, safe to print: `NAME=value … prog args`.

    The task text never appears: whether it went in on stdin or in argv, what
    is printed here is its descriptor.
    """
    parts: List[str] = []
    for name, value in sorted(redact_env(env).items()):
        parts.append(f"{name}={shlex.quote(value)}")
    marker = task_descriptor(task) if task else ""
    for token in argv:
        text = str(token)
        if marker and str(task) and str(task) in text:
            text = text.replace(str(task), marker)
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


class _GateSession:
    """The gate that stands in front of ONE run: its token, its listener, and
    the hook script the foreign agent will execute before every tool call.

    Everything here dies with the run. The token is minted at
    :meth:`start` and revoked in :meth:`close`, the listener binds an ephemeral
    loopback port and is shut down with it, and the hook script lives in a
    temporary directory that is removed. Nothing is written into the user's own
    configuration for the agent — that file is not Faustus's to edit, and a
    gate installed there would outlive the run that needed it.
    """

    def __init__(self, runner: Any, *, run_id: str, workspace_roots: List[str],
                 cwd: Optional[str], owner: Optional[str], attended: bool,
                 locks: Any, worker_key: str):
        self.runner = runner
        self.run_id = run_id
        self.workspace_roots = workspace_roots
        self.cwd = cwd or ""
        self.owner = owner or ""
        self.attended = attended
        self.locks = locks
        self.worker_key = worker_key
        self.run: Any = None
        self.server: Any = None
        self.settings = ""
        self._tmpdir = ""
        self._closed = False

    def start(self) -> None:
        """Raises on failure — the caller refuses the run rather than
        downgrading it. A gate that quietly did not start would be worse than
        no gate: the proof packet would say `gated` about a run nobody judged.
        """
        import tempfile
        from src import agent_gate, agent_runners as reg

        self.run = agent_gate.open_run(
            self.run_id, runner=self.runner.key, owner=self.owner,
            workspace_roots=self.workspace_roots, cwd=self.cwd,
            attended=self.attended, locks=self.locks, worker_key=self.worker_key,
        )
        self._tmpdir = tempfile.mkdtemp(prefix="faustus-gate-")
        script = agent_gate.write_hook_script(self._tmpdir)
        self.server = agent_gate.GateServer(self.run).start()
        self.settings = reg.hook_settings(self.runner, command=_hook_command(script))
        if not self.settings:
            raise RuntimeError(f"no gate configuration for runner {self.runner.key!r}")

    def env(self) -> Dict[str, str]:
        from src import agent_gate
        return {agent_gate.URL_ENV: self.server.base_url,
                agent_gate.TOKEN_ENV: self.run.token}

    def close(self) -> Dict[str, Any]:
        """Revoke the token, stop the listener, remove the script; return the
        ledger. Total: a failure while tearing down never loses the ledger."""
        from src import agent_gate
        led: Dict[str, Any] = {}
        if self._closed:
            return led
        self._closed = True
        try:
            led = agent_gate.close_run(self.run_id) or {}
        except Exception as e:  # noqa: BLE001
            logger.debug("external_worker: closing the gate run failed: %s", e)
        seen = set(getattr(self.run, "seen_ids", ()) or ())
        try:
            if self.server is not None:
                self.server.close()
        except Exception as e:  # noqa: BLE001
            logger.debug("external_worker: closing the gate listener failed: %s", e)
        try:
            if self._tmpdir:
                _remove_gate_dir(self._tmpdir)
        except Exception as e:  # noqa: BLE001
            logger.debug("external_worker: removing the hook script failed: %s", e)
        led["judged_ids"] = seen
        return led


def _remove_gate_dir(path: str) -> bool:
    """Delete a gate scratch directory, read-only hook script included.

    `agent_gate.write_hook_script` finishes with `chmod(0o500)` on purpose, so
    that the foreign agent cannot rewrite the hook that is policing it. On
    POSIX that still leaves the *directory* writable and `rmtree` succeeds. On
    Windows the same call clears the read-only-file bit's inverse — the file
    ends up 444 — and `rmtree` raises PermissionError [WinError 5] on it.

    The old code passed `ignore_errors=True`, which turned that failure into
    silence: 264 orphaned `faustus-gate-*` directories had accumulated in the
    user's TEMP since the feature shipped, and the test that checks the script
    is not left behind had been failing for days without anyone reading it as
    a leak.

    Retrying with a backoff would not help — the mode does not change with
    time. Clearing the bit and retrying does, which is what `onerror` does
    here. It still never raises: a scratch directory that cannot be deleted is
    worth a warning, not a lost ledger.
    """
    import shutil as _shutil
    import stat as _stat
    import sys as _sys

    def _force(func, target, _exc):
        try:
            os.chmod(target, _stat.S_IWRITE | _stat.S_IREAD)
            func(target)
        except Exception as inner:  # noqa: BLE001
            logger.warning("external_worker: could not remove %s: %s", target, inner)

    try:
        # `onexc` since 3.12, `onerror` before it; passing both is an error.
        if _sys.version_info >= (3, 12):
            _shutil.rmtree(path, onexc=lambda f, t, e: _force(f, t, e))
        else:
            _shutil.rmtree(path, onerror=lambda f, t, e: _force(f, t, e))
    except Exception as exc:  # noqa: BLE001
        logger.warning("external_worker: gate scratch dir %s survived: %s", path, exc)
    return not os.path.exists(path)


def _hook_command(script: str) -> str:
    """The shell command the foreign agent runs as its pre-tool hook.

    The interpreter is the one Faustus itself is running under, so the hook
    needs nothing installed and cannot pick up a different `python` from the
    agent's own PATH.
    """
    import sys
    if os.name == "nt":
        return f'"{sys.executable}" "{script}"'
    return f"{shlex.quote(sys.executable)} {shlex.quote(script)}"


class _Stream:
    """Reads a runner's ``stream-json`` output into two things: lines a human
    (and src/output_rules.py) can read, and the inventory of tool calls the
    CLI ITSELF reported.

    That second one is the point. The stream is written by the agent's binary,
    not by the model steering it, so a tool call that appears here without a
    matching receipt in the gate's ledger is a call the hook did not fire for.
    Faustus cannot prevent that after the fact — but it can refuse to pretend
    the call was judged.
    """

    def __init__(self) -> None:
        self.tool_calls: List[Dict[str, str]] = []
        self.result: Dict[str, Any] = {}
        #: The run's own id, as the CLI reports it. Taken from the FIRST event
        #: that carries one rather than only from the final `result`, because a
        #: run that timed out never emits a final event and is exactly the one
        #: worth continuing.
        self.session_id = ""
        self.parsed = 0
        self.unparsed = 0
        self.overflow = False
        # The answer is not the diagnostic tail (which includes tool receipts,
        # progress and cost annotations). Retain it separately and bounded.
        self.response_text = ""

    def feed(self, line: str) -> str:
        """One raw stream line → the text to show. Never raises."""
        text = line.strip()
        if not text:
            return ""
        try:
            event = json.loads(text)
        except (ValueError, TypeError):
            self.unparsed += 1
            return line                       # not JSON: show it as it came
        if not isinstance(event, dict):
            self.unparsed += 1
            return line
        self.parsed += 1
        if not self.session_id and not event.get('parent_tool_use_id'):
            ident = str(event.get("session_id") or "")
            if len(ident) > 256:
                self.overflow = True
                return ''
            self.session_id = ident
        try:
            return self._render(event)
        except Exception as e:  # noqa: BLE001 - a stream reader never kills a run
            logger.debug("external_worker: stream event unreadable: %s", e)
            return ""

    def _render(self, event: Dict[str, Any]) -> str:
        kind = str(event.get("type") or "")
        parent = event.get("parent_tool_use_id") or ""
        # A nested agent's work is attributed, not flattened: `parent_tool_use_id`
        # is how the CLI says "this came from a sub-agent I started", and a
        # board that showed it as the top-level agent's own work would be
        # reporting the wrong worker.
        prefix = f"  ↳[{str(parent)[-8:]}] " if parent else ""
        if kind == "result":
            result = {
                "subtype": str(event.get("subtype") or ""),
                "is_error": bool(event.get("is_error")),
                "num_turns": event.get("num_turns"),
                "duration_ms": event.get("duration_ms"),
                "total_cost_usd": event.get("total_cost_usd"),
                "session_id": str(event.get("session_id") or ""),
            }
            if not parent:
                self.result = result
            cost = event.get("total_cost_usd")
            tail = f" (${float(cost):.4f})" if isinstance(cost, (int, float)) else ""
            final = str(event.get("result") or "")
            if not parent:
                self.response_text = final
            return f"{final}\n[{result['subtype'] or 'result'}{tail}]\n" if final \
                else f"[{result['subtype'] or 'result'}{tail}]\n"
        message = event.get("message")
        blocks = (message or {}).get("content") if isinstance(message, dict) else None
        if not isinstance(blocks, list):
            return ""
        out: List[str] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            btype = str(block.get("type") or "")
            if btype == "text":
                body = str(block.get("text") or "").strip()
                if body:
                    out.append(prefix + body + "\n")
            elif btype == "tool_use":
                if len(self.tool_calls) >= 4096:
                    self.overflow = True
                    return ''
                name = str(block.get("name") or "?")
                if any(len(value) > 256 for value in (name, str(block.get('id') or ''), str(parent))):
                    self.overflow = True
                    return ''
                self.tool_calls.append({
                    "id": str(block.get("id") or ""), "name": name,
                    "parent_tool_use_id": str(parent or ""),
                })
                out.append(f"{prefix}→ {name}({_digest(block.get('input'))})\n")
            elif btype == "tool_result":
                body = block.get("content")
                out.append(f"{prefix}← {_digest(body, limit=160)}\n")
        return "".join(out)


class _CodexStream(_Stream):
    """Documented `codex exec --json` events, without claiming a tool gate.

    https://learn.chatgpt.com/docs/non-interactive-mode
    Reasoning payloads stay out of the progress log; tool names and phases are
    enough to explain activity. Only a completed agent message is answer text.
    """

    def _render(self, event: Dict[str, Any]) -> str:
        kind = event.get('type')
        if kind == 'thread.started':
            ident = event.get('thread_id')
            if isinstance(ident, str) and ident and not ident.startswith('-') and len(ident) <= 200:
                self.session_id = self.session_id or ident
            return '[Codex session started]\n'
        if kind == 'turn.started':
            return '[Codex working]\n'
        if kind == 'turn.completed':
            usage = event.get('usage')
            self.result = {'is_error': False, 'subtype': 'success', 'usage': {
                key: value for key, value in (usage.items() if isinstance(usage, dict) else [])
                if key in {'input_tokens', 'cached_input_tokens', 'output_tokens', 'reasoning_output_tokens'}
                and type(value) is int and value >= 0}}
            return '[Codex turn completed]\n'
        if kind in {'turn.failed', 'error'}:
            error = event.get('error')
            message = error.get('message') if isinstance(error, dict) else event.get('message')
            # A standalone error can describe a retry; only turn.failed is final.
            if kind == 'turn.failed':
                self.result = {'is_error': True, 'subtype': 'turn.failed'}
            return '[Codex error] ' + _digest(message, 500) + '\n'
        item = event.get('item')
        if not isinstance(item, dict):
            return ''
        item_type = item.get('type')
        if item_type == 'agent_message' and kind == 'item.completed':
            self.response_text = str(item.get('text') or '')
            return self.response_text + '\n'
        if item_type == 'reasoning':
            return ''
        if kind not in {'item.started', 'item.completed'}:
            return ''
        phase = 'started' if kind == 'item.started' else 'completed'
        detail = _digest(item.get('command') or item.get('tool') or item.get('query') or '', 160)
        return f'[Codex {item_type or "item"} {phase}] {detail}\n'


def _digest(value: Any, limit: int = 100) -> str:
    """A short, single-line rendering of a tool input or result."""
    try:
        if isinstance(value, dict):
            for key in ("command", "file_path", "pattern", "path", "notebook_path", "url"):
                if isinstance(value.get(key), str) and value[key].strip():
                    text = value[key]
                    break
            else:
                text = json.dumps(value, default=str)
        elif isinstance(value, list):
            text = " ".join(str(v.get("text") if isinstance(v, dict) else v) for v in value[:3])
        else:
            text = "" if value is None else str(value)
    except Exception:  # noqa: BLE001
        text = ""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _reconcile(ledger: Dict[str, Any], stream: Optional[_Stream]) -> Dict[str, Any]:
    """Fold what the CLI reported into what the gate judged.

    ``unseen`` is the honest number: tool calls the binary says it made that
    have no receipt in the gate's ledger. It is normally 0; when it is not, the
    tool names go into the ledger and out through `prove` as declared
    uncertainty rather than being averaged away.
    """
    out = dict(ledger or {})
    judged_ids = set(out.pop("judged_ids", ()) or ())
    if stream is None:
        return out
    out["stream_tool_calls"] = len(stream.tool_calls)
    out["subagent_tool_calls"] = sum(1 for c in stream.tool_calls if c.get("parent_tool_use_id"))
    unseen = [c for c in stream.tool_calls if c.get("id") and c["id"] not in judged_ids]
    out["unseen"] = len(unseen)
    if unseen:
        names = sorted({c["name"] for c in unseen if c.get("name")})
        out["unseen_tools"] = names
        out["unjudged_tools"] = sorted(set(out.get("unjudged_tools") or []) | set(names))
    if stream.result:
        out["result"] = dict(stream.result)
    return out


_RUN_COST_LOG_NAME = "_external_worker_costs.jsonl"


def _append_run_cost_event(*, cost_usd: float, runner_key: Any, run_id: Optional[str],
                            owner: Optional[str], seconds: float) -> None:
    """OPS-07: the one write `cleanup_service.remote_cost_report()` was built
    to read and nothing wrote — append-only, best effort, never raises into
    the caller's result. Lands in `DATA_DIR/runs/` (the same replay-log
    directory `src.support_bundle.recent_events` already scans) as its own
    file rather than a chat session's `_RunLog` (src/agent_runs.py opens its
    per-session file with mode "w", which truncates — sharing it here would
    race a live chat turn and could erase its transcript).
    """
    try:
        from src.constants import DATA_DIR
        runs_dir = os.path.join(DATA_DIR, "runs")
        os.makedirs(runs_dir, exist_ok=True)
        event = {
            "event": "external_worker_result",
            "ts": time.time(),
            "total_cost_usd": round(float(cost_usd), 6),
            "runner": str(runner_key or ""),
            "run_id": str(run_id or ""),
            "owner": str(owner or ""),
            "seconds": seconds,
        }
        with open(os.path.join(runs_dir, _RUN_COST_LOG_NAME), "a", encoding="utf-8") as f:
            f.write(json.dumps(event, sort_keys=True) + "\n")
    except Exception as e:  # noqa: BLE001 - a cost line that failed to write
        # must never fail (or even flavor) the run's own result.
        logger.debug("external_worker: could not persist run_cost event: %s", e)


def run_task(runner_key: Any, task: str, *, workspace: Optional[str] = None,
             model: Optional[str] = None, endpoint: Optional[str] = None,
             timeout_s: Optional[float] = None,
             on_output: Optional[Callable[[str], None]] = None,
             should_cancel: Optional[Callable[[], bool]] = None,
             env: Optional[Dict[str, str]] = None,
             run_id: Optional[str] = None,
             workspace_roots: Optional[List[str]] = None,
             owner: Optional[str] = None,
             attended: bool = False,
             locks: Any = None,
             worker_key: Optional[str] = None,
             resume: Optional[str] = None,
             allow_argv_task: bool = False,
             billing_mode: Optional[str] = None) -> Dict[str, Any]:
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

    The gate (src/agent_gate.py) needs four things from the caller, and there
    is deliberately no fifth that turns it off:

    * ``workspace_roots`` — everything this agent may write. Defaults to the
      workspace, which is what a dispatched job has;
    * ``attended`` — whether the caller owns a surface that can put a question
      to a human. The same assertion `src.tool_approvals` calls
      ``allow_continuation``: a CAUTION command becomes an `ask` when someone
      can answer it and a refusal when nobody can. A background job leaves it
      False, which is the truth about a background job;
    * ``locks`` / ``worker_key`` — this delegation's
      :class:`~src.agent_tools.subagent_tools.FileLockRegistry` and this
      worker's key in it, so the foreign agent cannot write the file a
      built-in worker is holding;
    * ``run_id`` — the id the gate's ledger and its receipts are filed under.

    ``resume`` is a prior run of the SAME agent to continue instead of
    starting a fresh one that has to read its way back to the same
    understanding. The id comes from a previous result's ``session_id``, which
    is read out of the runner's own ``stream-json`` output — so it exists only
    for a runner whose row asks for that stream (today: the gated ones), and a
    result without one leaves the caller on its fresh-worker path.

    ``allow_argv_task`` is the human override for SEC-1 (B-022): a runner
    whose row cannot take the prompt on stdin passes it as a command-line
    argument, where the operating system shows it to every process listing on
    the machine. That is accepted for ordinary work and refused when the task
    itself looks like it carries a credential, unless a person says otherwise
    by setting this.

    A gated result carries ``unguarded: False`` and a ``gate`` block; an
    ungated one is exactly what it always was.

    Never raises: every failure — an unknown runner, one that is not
    installed, a binary that will not start, a gate that would not start —
    comes back as a result with ``error`` set.
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
    for field, value in (('model', model), ('resume', resume)):
        if value is not None and (not isinstance(value, str) or value.lstrip().startswith('-')
                                  or any(c in value for c in ('\n', '\r', '\0'))):
            return _fail(key, f'Invalid {field}: an option or control character is not a model/session identifier')
    if not runner.stdin_task and task_looks_sensitive(task) and not allow_argv_task:
        # SEC-1 (B-022). Refuse rather than warn: an argv prompt is readable by
        # every process on the machine, it lands in diagnostics and OS crash
        # reports, and by the time anyone reads the warning the secret has
        # already been published.
        return _fail(key, f"{runner.label} passes the task as a command-line argument (its row has no "
                          f"verified stdin form), and this task looks like it carries a credential — "
                          f"argv is visible to every process on this machine. Remove the secret, use a "
                          f"runner that reads stdin, or re-run with allow_argv_task=True to accept it.")

    cwd: Optional[str] = None
    if runner.cwd_is_workspace:
        folder = str(workspace or "").strip()
        if not folder or not os.path.isdir(folder):
            return _fail(key, f"workspace is not a usable directory for {runner.label}: {workspace!r}")
        cwd = folder

    roots = [str(r) for r in (workspace_roots or ()) if str(r or "").strip()] or (
        [cwd] if cwd else [])
    gate: Optional[_GateSession] = None
    if runner.gate == "hook":
        # Fail CLOSED. A row that says `hook` is a promise the proof packet
        # repeats; if the gate will not start, the honest move is to not run
        # the agent at all rather than run it and file the promise anyway.
        gate = _GateSession(
            runner, run_id=str(run_id or f"ext-{uuid.uuid4().hex[:12]}"),
            workspace_roots=roots, cwd=cwd, owner=owner, attended=bool(attended),
            locks=locks, worker_key=str(worker_key or ""),
        )
        try:
            gate.start()
        except Exception as e:  # noqa: BLE001
            try:
                gate.close()
            except Exception:  # noqa: BLE001
                pass
            return _fail(key, f"{runner.label} is gated by Faustus and its gate could not be "
                              f"started ({type(e).__name__}: {e}); the run was not started rather "
                              f"than started without it"[:300])

    try:
        argv = reg.build_argv(runner, str(task or ""), model=model, cwd=cwd, endpoint=endpoint,
                              settings=(gate.settings if gate is not None else None),
                              session=resume)
        if not argv:
            return _fail(key, f"{runner.label}: the table produced an empty command for this task")
        table_env = reg.table_env(runner, model=model, cwd=cwd, endpoint=endpoint)
        gate_env = gate.env() if gate is not None else {}
        shown = _shown(argv, dict(table_env, **gate_env), task=str(task or ""))

        full_env = dict(reg.build_env(runner, base=env, model=model, cwd=cwd, endpoint=endpoint))
        full_env.update(gate_env)
        billing = None
        if billing_mode is not None:
            from src.runner_billing import prepare, verify
            try:
                argv, full_env = prepare(key, billing_mode, argv, full_env, endpoint=endpoint)
                billing = verify(key, billing_mode, full_env)
                shown = _shown(argv, dict(table_env, **gate_env), task=str(task or ''))
            except Exception as exc:
                logger.debug('Official client authentication could not be verified: %s', type(exc).__name__)
                return _fail(key, str(exc) if isinstance(exc, ValueError) else
                             'Official client authentication check failed; no task was sent')
        if should_cancel is not None and should_cancel():
            result = _fail(key, '', argv_shown=shown)
            result.update(status='cancelled', cancelled=True, outcome='cancelled')
            return result
        result = _spawn(runner, key, task, argv=argv, shown=shown, cwd=cwd, full_env=full_env,
                      timeout_s=timeout_s, on_output=on_output, should_cancel=should_cancel,
                      gate=gate, started=started)
        if billing is not None:
            result['billing'] = billing
        return result
    finally:
        if gate is not None:
            # Idempotent. `_spawn` closes it as soon as the process is gone, so
            # the token dies with the run and not with the bookkeeping after
            # it; this is the backstop for every path that never got there.
            gate.close()


def _spawn(runner: Any, key: str, task: str, *, argv: List[str], shown: str,
           cwd: Optional[str], full_env: Dict[str, str], timeout_s: Optional[float],
           on_output: Optional[Callable[[str], None]],
           should_cancel: Optional[Callable[[], bool]],
           gate: Optional["_GateSession"], started: float) -> Dict[str, Any]:
    """Start the agent, supervise it, and report. Split out of
    :func:`run_task` only so the gate's setup and teardown can bracket it."""
    from src import agent_runners as reg

    exe = None
    try:
        import shutil
        # Look on the child's PATH first, then on ours. SEC-1 (B-008) strips
        # our virtualenv out of the child's PATH — which is the point — but a
        # CLI the user pip-installed INTO that virtualenv would then read as
        # "not installed". Faustus can still find it and hand the child the
        # absolute path; what the child does not get is our PATH.
        exe = shutil.which(argv[0], path=full_env.get("PATH")) or shutil.which(argv[0])
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
        "text": True, "encoding": "utf-8", "errors": "replace", "bufsize": 1,
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

    def _write_input() -> None:
        # Writing a long context can fill the pipe while the CLI is still
        # starting (or printing). Never block the supervisor on stdin.
        try:
            proc.stdin.write(str(task or "") + "\n")           # type: ignore[union-attr]
        except Exception as e:  # noqa: BLE001
            logger.debug("external_worker: %s would not take the task on stdin: %s", key, e)
        finally:
            try:
                proc.stdin.close()                              # type: ignore[union-attr]
            except Exception:  # noqa: BLE001
                pass

    # A gated run reads the CLI's structured stream, so the board and the
    # output rules see prose (not JSONL) and the tool calls the binary reports
    # can be reconciled against the gate's ledger afterwards.
    events = (_CodexStream() if key == 'codex' and '--json' in argv else
              _Stream() if gate is not None and 'stream-json' in argv else None)

    transport_failed = threading.Event()
    max_line_chars = 1024 * 1024

    def _read() -> None:
        stream = proc.stdout
        if stream is None:
            return
        try:
            # A CLI can print an unbounded JSON event (or never a newline).
            # Bound the read itself, not only the tail retained afterwards.
            for line in iter(lambda: stream.readline(max_line_chars + 1), ""):
                if len(line) > max_line_chars:
                    transport_failed.set()
                    break
                shown_line = events.feed(line) if events is not None else line
                if events is not None and events.overflow:
                    transport_failed.set()
                    break
                if not shown_line:
                    continue
                buf.add(shown_line)
                if on_output is not None:
                    try:
                        on_output(shown_line)
                    except Exception as e:  # noqa: BLE001 - a callback never breaks the run
                        logger.debug("external_worker: on_output failed: %s", e)
        except Exception as e:  # noqa: BLE001
            logger.debug("external_worker: reading %s failed: %s", key, e)
            transport_failed.set()
        finally:
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass

    reader = threading.Thread(target=_read, name=f"external-worker-{key}", daemon=True)
    reader.start()
    writer = None
    if runner.stdin_task:
        writer = threading.Thread(target=_write_input, name=f"external-input-{key}", daemon=True)
        writer.start()

    from src.agent_tools.subprocess_tools import _kill_tree

    deadline = time.monotonic() + limit
    timed_out = cancelled = killed = False
    while True:
        if transport_failed.is_set():
            killed = True
            _kill_tree(proc)
            break
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
            # Clock, explicit cancel, or broken/oversized transport. Never a
            # semantic state such as "rate limited" read from the output.
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
    if writer is not None:
        writer.join(timeout=5)

    # Closed here, not in the caller's `finally`: the token stops working the
    # moment the process this gate exists for is gone.
    ledger = _reconcile(gate.close(), events) if gate is not None else {}

    seconds = round(time.time() - started, 3)
    exit_code = proc.returncode
    tail = buf.text()
    read = _classify(tail)
    if cancelled:
        status, error = "cancelled", ""
    elif timed_out:
        status = "timeout"
        error = f"{runner.label} was stopped after {int(limit)}s (its process tree was killed)"
    elif transport_failed.is_set():
        status = 'error'
        error = f'{runner.label} output exceeded transport limits or could not be read; the run was stopped'
    elif events is not None and events.result.get('is_error'):
        status = 'error'
        error = f"{runner.label} reported {events.result.get('subtype') or 'an error'}"
    elif exit_code == 0:
        status, error = "done", ""
    else:
        status = "error"
        error = f"{runner.label} exited with code {exit_code}"
        if read.get("state"):
            error += f" — its output says {read['state']}"
    out = {
        "ok": status == 'done',
        "exit_code": exit_code,
        "outcome": _outcome(status, error=error or None, cancelled=cancelled),
        "output_tail": tail[-RESULT_TAIL_CHARS:],
        "output_chars": buf.total,
        "response_text": events.response_text if events is not None else "",
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
        # Said in every result, not only in the docs: whether the command guard
        # saw this run's tool calls. `False` is only ever written when a gate
        # really ran (src/agent_gate.py) — an ungated runner keeps the original
        # sentence, and so does a gated one whose gate produced no ledger.
        "unguarded": not bool(ledger.get("gated")),
        "guard_note": reg.gate_note(runner) if ledger.get("gated") else reg.GUARD_NOTE,
    }
    if events is not None and events.session_id:
        # Only ever present when the runner's own stream reported one, so a
        # runner that reports nothing leaves the caller on its fresh-worker
        # path instead of being handed a handle nobody can use.
        out["session_id"] = events.session_id
    if events is not None and events.result:
        out['client_result'] = dict(events.result)
    if ledger:
        out["gate"] = ledger
        cost = (ledger.get("result") or {}).get("total_cost_usd")
        if isinstance(cost, (int, float)):
            # What the run actually cost, from the CLI's own final event. A
            # subscription runner's bill is the user's, and a number they did
            # not ask for is one they cannot check.
            out["total_cost_usd"] = float(cost)
            # OPS-07: persist it — remote_cost_report() (src/cleanup_service.py)
            # reads DATA_DIR/runs/*.jsonl for exactly this event and, until
            # this line existed, nothing ever wrote one.
            _append_run_cost_event(
                cost_usd=float(cost), runner_key=key,
                run_id=getattr(gate, "run_id", None),
                owner=getattr(gate, "owner", None), seconds=seconds,
            )
    return out


__all__ = ["OUTPUT_TAIL_CHARS", "REDACTED", "RESULT_TAIL_CHARS", "redact_env", "run_task"]
