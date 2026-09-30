"""One lifecycle for a process, its stdin and its output (H11).

A process started through this manager is addressed by an opaque *handle*. The
handle carries its owner, the environment policy it was started under, the
initial command, a state, a bounded output buffer read through a cursor, and a
termination reason. Nothing about it is tied to the request, chat stream or
browser tab that started it: closing the UI does not stop a run; only an
explicit ``stop`` (or the owner's ``cancel_for_session``) does, and a stop only
ever reaches the process tree this manager spawned.

States
------
``starting`` ``running`` -> ``exited`` (exit code known) | ``stopped`` (asked to
stop) | ``timed_out`` (max runtime) | ``failed_to_start``. After a server
restart a record whose process this interpreter does not hold becomes
``orphaned`` (the OS process is still alive and is ours to stop, but its pipes
are gone so output and stdin are unreachable) or ``lost`` (no such process; the
exit status is unknown). A remote process whose runner disconnected is
``uncertain``. A record is never re-spawned: a restart that could not keep the
process reports that it could not.

What the handle API guarantees
------------------------------
* ``read`` is cursor based and never blocks the process: the buffer keeps the
  last ``MAX_BUFFER_BYTES`` and reports how many bytes were dropped before the
  cursor. A process that prints nothing stays valid until it exits, is stopped
  or exceeds its max runtime; there is no idle kill here.
* ``write_stdin`` verifies the handle's owner and session against the caller
  and re-checks the *current* permission (manager switched off, tool disabled
  for the caller, confinement now required while the handle runs on the host).
* ``stop`` takes a handle, never a name or a pid, and signals only what
  ``process_ownership.terminate_tree`` can prove was spawned by this manager
  (pid + creation time). An application the user started is unreachable from
  here.
* ``bg_jobs`` detached jobs are visible and stoppable through the same
  handles (``bg:<job id>``) without a second copy of their state.

The manager runs managed processes on the host. It therefore refuses to start
one while command confinement is ``required`` (or ``strict`` on POSIX): there
is no container path for it, and the refusal is explicit rather than a
fallback.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.platform_compat import IS_WINDOWS, detached_popen_kwargs, find_bash
from src import process_ownership

logger = logging.getLogger(__name__)

SETTING_ENABLED = "agent_process_manager"
SETTING_MAX_HANDLES = "agent_process_max_handles"
SETTING_MAX_RUNTIME = "agent_process_max_runtime_seconds"
SETTING_BUFFER = "agent_process_buffer_bytes"

DEFAULT_MAX_HANDLES = 16
DEFAULT_MAX_RUNTIME_S = 4 * 3600
DEFAULT_BUFFER_BYTES = 256 * 1024
MAX_BUFFER_BYTES = 4 * 1024 * 1024
MAX_READ_BYTES = 64 * 1024
DEFAULT_READ_BYTES = 16 * 1024
MAX_STDIN_BYTES = 64 * 1024
TAIL_PERSIST_BYTES = 8 * 1024
TAIL_PERSIST_INTERVAL_S = 2.0
FINISHED_RETENTION_S = 3600.0
MAX_WAIT_MS = 30_000

LIVE_STATES = ("starting", "running")
FINISHED_STATES = ("exited", "stopped", "timed_out", "failed_to_start", "lost", "uncertain")
BG_PREFIX = "bg:"


# ── settings / paths ───────────────────────────────────────────────────────

def _setting(key: str, default: Any) -> Any:
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:  # pragma: no cover
        return default


def enabled() -> bool:
    return _setting(SETTING_ENABLED, True) is not False


def _int_setting(key: str, default: int, lo: int, hi: int) -> int:
    raw = _setting(key, default)
    if isinstance(raw, bool) or not isinstance(raw, int):
        return default
    return max(lo, min(hi, raw))


def _store_path() -> str:
    override = os.environ.get("FAUSTUS_PROCESS_STORE")
    if override:
        return override
    from src import constants
    return os.path.join(constants.DATA_DIR, "process_handles.json")


# ── the bounded, cursor-addressed buffer ───────────────────────────────────

class OutputBuffer:
    """Last ``cap`` bytes of a stream, addressed by absolute byte offsets."""

    def __init__(self, cap: int):
        self.cap = max(1024, int(cap))
        self._buf = bytearray()
        self.base = 0          # absolute offset of _buf[0]

    @property
    def total(self) -> int:
        return self.base + len(self._buf)

    def append(self, data: bytes) -> None:
        if not data:
            return
        self._buf += data
        overflow = len(self._buf) - self.cap
        if overflow > 0:
            del self._buf[:overflow]
            self.base += overflow

    def read(self, cursor: Optional[int], max_bytes: int, *, final: bool) -> Tuple[bytes, int, int]:
        """``(data, next_cursor, dropped_bytes)``.

        ``cursor=None`` starts at the oldest retained byte. A cursor older than
        the buffer is moved forward and the gap is reported as dropped. A cut
        never splits a UTF-8 sequence unless the stream is final.
        """
        start = self.base if cursor is None else int(cursor)
        dropped = max(0, self.base - start)
        start = max(start, self.base)
        start = min(start, self.total)
        end = min(self.total, start + max(1, int(max_bytes)))
        chunk = bytes(self._buf[start - self.base:end - self.base])
        if not final or end < self.total:
            chunk = chunk[:len(chunk) - _incomplete_tail(chunk)]
        return chunk, start + len(chunk), dropped

    def tail(self, n: int) -> bytes:
        return bytes(self._buf[-n:]) if n > 0 else b""


def _incomplete_tail(chunk: bytes) -> int:
    """Number of trailing bytes that begin a UTF-8 sequence not yet complete."""
    for back in range(1, min(4, len(chunk)) + 1):
        byte = chunk[-back]
        if byte & 0xC0 == 0x80:        # continuation byte: keep looking for its start
            continue
        if byte < 0x80:
            return 0
        need = 2 if byte >= 0xC0 and byte < 0xE0 else 3 if byte < 0xF0 else 4
        return back if back < need else 0
    return 0


# ── caller and permission ──────────────────────────────────────────────────

@dataclass(frozen=True)
class Caller:
    owner: str = ""
    session_id: str = ""
    disabled_tools: frozenset = field(default_factory=frozenset)

    @classmethod
    def from_ctx(cls, ctx: Optional[dict]) -> "Caller":
        ctx = ctx or {}
        return cls(owner=str(ctx.get("owner") or ""), session_id=str(ctx.get("session_id") or ""),
                   disabled_tools=frozenset(str(t) for t in (ctx.get("disabled_tools") or ())))


#: Extra permission check, ``(handle_record, caller, action) -> (ok, reason)``.
#: Replaceable so a deployment (or a test) can veto actions the built-in checks
#: allow; it can only make the decision stricter.
permission_hook: Optional[Callable[[Dict[str, Any], Caller, str], Tuple[bool, str]]] = None


def _route_refusal(tool: str) -> Optional[Dict[str, Any]]:
    """Explicit refusal instead of a host run when confinement is required."""
    try:
        from src import sandbox_exec
    except Exception:  # pragma: no cover
        return None
    if not sandbox_exec.enabled():
        return None
    mode = sandbox_exec.mode()
    if mode == "required" or (mode == "strict" and not sandbox_exec._host_is_windows()):
        reason = (f"{mode} confinement: {tool} runs on the host and has no container backend")
        return {**sandbox_exec._refusal(tool, reason),
                "requested_policy": f"sandbox_{mode}", "effective_policy": "not_executed"}
    return None


def _host_policy(tool: str) -> Dict[str, Any]:
    try:
        from src import sandbox_exec
        if sandbox_exec.enabled():
            return sandbox_exec.host_policy_metadata(
                f"{tool} has no container backend and runs on the host "
                f"(sandbox mode `{sandbox_exec.mode()}`)")
    except Exception:  # pragma: no cover
        pass
    return {"requested_policy": "host", "effective_policy": "host"}


# ── the live runtime behind a handle ───────────────────────────────────────

class _Live:
    def __init__(self, proc: subprocess.Popen, buffer: OutputBuffer):
        self.proc = proc
        self.buffer = buffer
        self.lock = threading.Lock()
        self.changed = threading.Condition(self.lock)
        self.readers: List[threading.Thread] = []
        self.waiter: Optional[threading.Thread] = None
        self.timer: Optional[threading.Timer] = None
        self.job: Any = None
        self.last_persist = 0.0


class ProcessManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._records: Dict[str, Dict[str, Any]] = {}
        self._live: Dict[str, _Live] = {}
        self._loaded = False

    # ── persistence ─────────────────────────────────────────────────────

    def _load(self) -> None:
        with self._lock:
            if self._loaded:
                return
            self._loaded = True
            try:
                with open(_store_path(), "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except Exception:
                data = {}
            if isinstance(data, dict):
                for hid, rec in data.items():
                    if isinstance(rec, dict) and isinstance(hid, str):
                        self._records[hid] = rec
            self._recover_locked()

    def _save_locked(self) -> None:
        try:
            from core.atomic_io import atomic_write_json
            os.makedirs(os.path.dirname(_store_path()), exist_ok=True)
            atomic_write_json(_store_path(), self._records, indent=1)
        except Exception:
            logger.debug("process handle store write failed", exc_info=True)

    def _recover_locked(self) -> None:
        """Records this interpreter does not hold cannot be live handles."""
        changed = False
        for hid, rec in self._records.items():
            if rec.get("state") not in LIVE_STATES or hid in self._live or rec.get("kind") != "managed":
                continue
            pid, created = rec.get("pid"), rec.get("pid_created_at")
            alive = bool(pid) and created is not None and _same_process(pid, created)
            if alive:
                rec["state"] = "orphaned"
                rec["termination_reason"] = ("server_restarted_process_still_running: its output and "
                                             "stdin pipes were lost with the previous server process")
            else:
                rec["state"] = "lost"
                rec["termination_reason"] = ("server_restarted_process_not_found: the exit status "
                                             "is unknown")
                rec["ended_at"] = rec.get("ended_at") or time.time()
                rec["outcome_unknown"] = True
            changed = True
        if changed:
            self._save_locked()

    def recover(self) -> None:
        """Re-read the store and reclassify records this process does not hold
        (called at start-up; safe to call again)."""
        with self._lock:
            self._loaded = False
            self._records = {k: v for k, v in self._records.items() if k in self._live}
        self._load()

    # ── helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _public(rec: Dict[str, Any]) -> Dict[str, Any]:
        keep = ("handle", "kind", "state", "command", "shell", "cwd", "owner", "session_id",
                "env_policy", "route", "started_at", "ended_at", "exit_code", "termination_reason",
                "stdin_open", "output_total_bytes", "max_runtime_s", "containment",
                "outcome_unknown", "remote_id", "requested_policy", "effective_policy")
        return {k: rec[k] for k in keep if k in rec}

    def _authorize(self, rec: Optional[Dict[str, Any]], caller: Caller, action: str
                   ) -> Optional[Dict[str, Any]]:
        if rec is None:
            return {"ok": False, "error": "no such handle", "code": "unknown_handle"}
        if not enabled():
            return {"ok": False, "code": "disabled",
                    "error": f"the process manager is switched off (`{SETTING_ENABLED}`)"}
        owner, session = str(rec.get("owner") or ""), str(rec.get("session_id") or "")
        if (owner and caller.owner and owner != caller.owner) or (session and session != caller.session_id):
            return {"ok": False, "code": "not_owner",
                    "error": "this handle belongs to another session or owner"}
        if f"process_{action}" in caller.disabled_tools:
            return {"ok": False, "code": "tool_disabled",
                    "error": f"process_{action} is disabled for this caller"}
        if action in ("write_stdin",) and rec.get("route") == "host":
            blocked = _route_refusal(f"process_{action}")
            if blocked is not None:
                return {"ok": False, "code": "confinement_required", "error": blocked["error"]}
        if permission_hook is not None:
            ok, why = permission_hook(dict(rec), caller, action)
            if not ok:
                return {"ok": False, "code": "permission", "error": why or "not permitted"}
        return None

    def _set_state_locked(self, rec: Dict[str, Any], state: str, reason: str = "",
                          exit_code: Optional[int] = None) -> None:
        rec["state"] = state
        if reason and not rec.get("termination_reason"):
            rec["termination_reason"] = reason
        if exit_code is not None:
            rec["exit_code"] = exit_code
        if state in FINISHED_STATES:
            rec["ended_at"] = rec.get("ended_at") or time.time()
            rec["stdin_open"] = False

    # ── start ───────────────────────────────────────────────────────────

    def start(self, command: Optional[str] = None, caller: Optional[Caller] = None, *,
              argv: Optional[List[str]] = None, cwd: Optional[str] = None, shell: str = "bash",
              yield_ms: int = 1500, max_runtime_s: Optional[int] = None,
              env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        self._load()
        caller = caller or Caller()
        if not enabled():
            return {"ok": False, "code": "disabled", "exit_code": 1,
                    "error": f"process_start: the process manager is switched off (`{SETTING_ENABLED}`)"}
        if "process_start" in caller.disabled_tools:
            return {"ok": False, "code": "tool_disabled", "exit_code": 1,
                    "error": "process_start is disabled for this caller"}
        blocked = _route_refusal("process_start")
        if blocked is not None:
            return {"ok": False, "code": "confinement_required", **blocked}
        if argv is None and not (isinstance(command, str) and command.strip()):
            return {"ok": False, "code": "bad_request", "exit_code": 1,
                    "error": "process_start: provide a command (or an argv list)"}
        limit = _int_setting(SETTING_MAX_HANDLES, DEFAULT_MAX_HANDLES, 1, 256)
        with self._lock:
            running = [r for r in self._records.values()
                       if r.get("state") in LIVE_STATES and r.get("owner") == caller.owner
                       and r.get("session_id") == caller.session_id]
            if len(running) >= limit:
                return {"ok": False, "code": "too_many", "exit_code": 1,
                        "error": f"process_start: {len(running)} processes are already running for "
                                 f"this session (limit {limit}); stop one first"}
        try:
            full_argv = _build_argv(command, argv, shell)
        except ValueError as exc:
            return {"ok": False, "code": "bad_request", "exit_code": 1, "error": f"process_start: {exc}"}

        from src.native_env import native_host_environment
        child_env = dict(native_host_environment())
        child_env.setdefault("PYTHONUNBUFFERED", "1")
        for key, value in (env or {}).items():
            if isinstance(key, str) and isinstance(value, str) and key and "=" not in key:
                child_env[key] = value
        handle = "ph_" + uuid.uuid4().hex[:16]
        rec: Dict[str, Any] = {
            "handle": handle, "kind": "managed", "state": "starting",
            "command": command if command is not None else " ".join(map(shlex.quote, full_argv)),
            "argv0": full_argv[0], "shell": shell, "cwd": cwd or "",
            "owner": caller.owner, "session_id": caller.session_id,
            "env_policy": "native_host_environment+PYTHONUNBUFFERED" + ("+explicit_env" if env else ""),
            "route": "host", **_host_policy("process_start"),
            "started_at": time.time(), "ended_at": None, "exit_code": None,
            "termination_reason": "", "stdin_open": True, "output_total_bytes": 0,
            "max_runtime_s": int(max_runtime_s) if max_runtime_s else _int_setting(
                SETTING_MAX_RUNTIME, DEFAULT_MAX_RUNTIME_S, 0, 7 * 24 * 3600),
            "tail": "",
        }
        try:
            proc = subprocess.Popen(
                full_argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                cwd=cwd or None, env=child_env, **detached_popen_kwargs())
        except (OSError, ValueError) as exc:
            rec.update(state="failed_to_start", termination_reason=f"launch_failed: {exc}",
                       ended_at=time.time(), stdin_open=False, exit_code=None)
            with self._lock:
                self._records[handle] = rec
                self._save_locked()
            return {"ok": False, "code": "launch_failed", "exit_code": 1, "handle": handle,
                    "state": "failed_to_start", "error": f"process_start: could not start it: {exc}"}

        live = _Live(proc, OutputBuffer(_int_setting(SETTING_BUFFER, DEFAULT_BUFFER_BYTES, 1024,
                                                     MAX_BUFFER_BYTES)))
        rec.update(state="running", pid=proc.pid,
                   pid_created_at=process_ownership.creation_time(proc.pid),
                   pgid=process_ownership.process_group_id(proc.pid))
        rec["containment"] = self._contain(live, proc)
        process_ownership.note_started(proc, rec["command"])
        with self._lock:
            self._records[handle] = rec
            self._live[handle] = live
            self._save_locked()
        self._spawn_threads(handle, live)
        if rec["max_runtime_s"]:
            live.timer = threading.Timer(rec["max_runtime_s"], self._on_max_runtime, args=(handle,))
            live.timer.daemon = True
            live.timer.start()

        first = self.read(handle, caller, wait_ms=max(0, int(yield_ms)), until_exit=True)
        first.pop("ok", None)
        return {"ok": True, "exit_code": 0, **self._public(rec), **first,
                "completed": rec["state"] in FINISHED_STATES}

    def _contain(self, live: _Live, proc: subprocess.Popen) -> str:
        """Windows: put the child in a KILL_ON_JOB_CLOSE job so every descendant
        dies with it. POSIX: its own session/process group (see stop)."""
        if not IS_WINDOWS:
            return "process_group"
        try:
            from src.code_mode import windows_job_bootstrap as wjb
            job = wjb.create_owned_job()
            kernel = wjb._kernel()
            if not kernel.AssignProcessToJobObject(job.handle, int(proc._handle)):  # type: ignore[attr-defined]
                job.close()
                return "process_tree_walk"
            live.job = job
            return "job_object"
        except Exception as exc:  # noqa: BLE001
            logger.debug("job object for managed process unavailable: %s", exc)
            return "process_tree_walk"

    def _spawn_threads(self, handle: str, live: _Live) -> None:
        def pump() -> None:
            stream = live.proc.stdout
            try:
                while True:
                    data = os.read(stream.fileno(), 65536)
                    if not data:
                        break
                    with live.changed:
                        live.buffer.append(data)
                        live.changed.notify_all()
                    now = time.monotonic()
                    if now - live.last_persist > TAIL_PERSIST_INTERVAL_S:
                        live.last_persist = now
                        self._persist_tail(handle, live)
            except Exception:
                pass

        def wait() -> None:
            code = live.proc.wait()
            for t in live.readers:
                t.join(timeout=5)
            self._finish(handle, live, code)

        reader = threading.Thread(target=pump, name=f"{handle}-out", daemon=True)
        live.readers.append(reader)
        reader.start()
        live.waiter = threading.Thread(target=wait, name=f"{handle}-wait", daemon=True)
        live.waiter.start()

    def _persist_tail(self, handle: str, live: _Live) -> None:
        with self._lock:
            rec = self._records.get(handle)
            if rec is None:
                return
            rec["tail"] = live.buffer.tail(TAIL_PERSIST_BYTES).decode("utf-8", "replace")
            rec["output_total_bytes"] = live.buffer.total
            self._save_locked()

    def _finish(self, handle: str, live: _Live, code: int) -> None:
        if live.timer is not None:
            live.timer.cancel()
        if live.job is not None:
            try:
                live.job.close()
            except Exception:
                pass
        with self._lock:
            rec = self._records.get(handle)
            if rec is None:
                return
            rec["tail"] = live.buffer.tail(TAIL_PERSIST_BYTES).decode("utf-8", "replace")
            rec["output_total_bytes"] = live.buffer.total
            if rec.get("state") in LIVE_STATES:
                self._set_state_locked(rec, "exited", "exited", exit_code=code)
            else:
                rec["exit_code"] = code
            rec["ended_at"] = rec.get("ended_at") or time.time()
            rec["stdin_open"] = False
            self._save_locked()
        with live.changed:
            live.changed.notify_all()

    def _on_max_runtime(self, handle: str) -> None:
        with self._lock:
            rec = self._records.get(handle)
            live = self._live.get(handle)
            if rec is None or live is None or rec.get("state") not in LIVE_STATES:
                return
            rec["state"] = "timed_out"
            rec["termination_reason"] = f"max_runtime_exceeded: {rec.get('max_runtime_s')}s"
            self._save_locked()
        self._kill(rec, live)

    # ── read ────────────────────────────────────────────────────────────

    def read(self, handle: str, caller: Optional[Caller] = None, *, cursor: Optional[int] = None,
             max_bytes: int = DEFAULT_READ_BYTES, wait_ms: int = 0, until_exit: bool = False
             ) -> Dict[str, Any]:
        self._load()
        caller = caller or Caller()
        if handle.startswith(BG_PREFIX):
            return _bg_read(handle[len(BG_PREFIX):], caller, cursor, max_bytes, self)
        with self._lock:
            rec = self._records.get(handle)
        denied = self._authorize(rec, caller, "read")
        if denied:
            return denied
        max_bytes = max(1, min(int(max_bytes or DEFAULT_READ_BYTES), MAX_READ_BYTES))
        wait_s = max(0, min(int(wait_ms or 0), MAX_WAIT_MS)) / 1000.0
        live = self._live.get(handle)
        if live is not None:
            deadline = time.monotonic() + wait_s
            with live.changed:
                while True:
                    start = live.buffer.base if cursor is None else cursor
                    has_new = live.buffer.total > max(start, live.buffer.base)
                    with self._lock:        # _finish sets the state and persists under this lock
                        finished = rec["state"] in FINISHED_STATES
                    if finished or (has_new and not until_exit) or time.monotonic() >= deadline:
                        break
                    live.changed.wait(timeout=min(0.05, max(0.0, deadline - time.monotonic())))
                data, nxt, dropped = live.buffer.read(cursor, max_bytes, final=rec["state"] in FINISHED_STATES)
                total = live.buffer.total
                base = live.buffer.base
            return self._read_result(rec, data, nxt, dropped, total, base)
        # No live buffer: a finished or recovered record keeps only its tail.
        tail = (rec.get("tail") or "").encode("utf-8")
        total = int(rec.get("output_total_bytes") or len(tail))
        base = max(0, total - len(tail))
        buf = OutputBuffer(max(1024, len(tail)))
        buf._buf, buf.base = bytearray(tail), base
        data, nxt, dropped = buf.read(cursor, max_bytes, final=True)
        result = self._read_result(rec, data, nxt, dropped, total, base)
        result["buffer_source"] = "persisted_tail"
        return result

    def _read_result(self, rec: Dict[str, Any], data: bytes, nxt: int, dropped: int,
                     total: int, base: int) -> Dict[str, Any]:
        out = {"ok": True, "handle": rec["handle"], "state": rec["state"],
               "output": data.decode("utf-8", "replace"), "cursor": nxt, "next_cursor": nxt,
               "dropped_bytes": dropped, "output_total_bytes": total, "buffer_start": base,
               "more": nxt < total, "exit_code": rec.get("exit_code"),
               "termination_reason": rec.get("termination_reason") or "",
               "stdin_open": bool(rec.get("stdin_open"))}
        if rec.get("outcome_unknown"):
            out["outcome_unknown"] = True
            out["uncertainty"] = {"reason": rec.get("termination_reason") or "outcome unknown",
                                  "reconcile_action": "inspect_the_effects_before_retrying"}
        return out

    # ── stdin ───────────────────────────────────────────────────────────

    def write_stdin(self, handle: str, caller: Optional[Caller] = None, *, data: str = "",
                    close_stdin: bool = False) -> Dict[str, Any]:
        self._load()
        caller = caller or Caller()
        if handle.startswith(BG_PREFIX):
            return {"ok": False, "code": "no_stdin", "error":
                    "a detached background job has no stdin (it was started with its input closed); "
                    "start the program with process_start to drive it interactively"}
        with self._lock:
            rec = self._records.get(handle)
        denied = self._authorize(rec, caller, "write_stdin")
        if denied:
            return denied
        live = self._live.get(handle)
        if rec["state"] not in LIVE_STATES or live is None:
            return {"ok": False, "code": "not_running", "state": rec["state"],
                    "error": f"the process is {rec['state']}; stdin is unreachable",
                    **({"outcome_unknown": True} if rec.get("outcome_unknown") else {})}
        payload = (data or "").encode("utf-8")
        if len(payload) > MAX_STDIN_BYTES:
            return {"ok": False, "code": "too_large", "error": f"stdin write is limited to {MAX_STDIN_BYTES} bytes"}
        try:
            if payload:
                live.proc.stdin.write(payload)
                live.proc.stdin.flush()
            if close_stdin:
                live.proc.stdin.close()
                with self._lock:
                    rec["stdin_open"] = False
                    self._save_locked()
        except (BrokenPipeError, OSError, ValueError) as exc:
            return {"ok": False, "code": "stdin_closed", "error": f"stdin is closed: {exc}"}
        return {"ok": True, "handle": handle, "written": len(payload), "stdin_closed": bool(close_stdin)}

    # ── stop ────────────────────────────────────────────────────────────

    def _kill(self, rec: Dict[str, Any], live: Optional[_Live]) -> process_ownership.TreeKill:
        """Kill the tree this manager spawned, nothing else. Holding the
        unreaped process object is the proof for a live handle; a recovered
        record must prove (pid, creation time)."""
        if live is not None and live.job is not None:
            try:
                live.job.close()
            except Exception:
                pass
        unverified = live is not None and live.proc.poll() is None
        return process_ownership.terminate_tree(
            rec.get("pid"), spawned_at=rec.get("pid_created_at"), pgid=rec.get("pgid"),
            unverified_tree_ok=unverified)

    def stop(self, handle: str, caller: Optional[Caller] = None, *, reason: str = "") -> Dict[str, Any]:
        self._load()
        caller = caller or Caller()
        if handle.startswith(BG_PREFIX):
            return _bg_stop(handle[len(BG_PREFIX):], caller)
        with self._lock:
            rec = self._records.get(handle)
        denied = self._authorize(rec, caller, "stop")
        if denied:
            return denied
        live = self._live.get(handle)
        if rec["state"] in FINISHED_STATES:
            return {"ok": True, "handle": handle, "state": rec["state"], "signalled": False,
                    "note": "already finished; nothing was signalled"}
        if rec["state"] == "orphaned":
            outcome = self._kill(rec, None)
            with self._lock:
                if outcome.signalled:
                    self._set_state_locked(rec, "stopped", "stopped_by_request")
                self._save_locked()
            return {"ok": bool(outcome.signalled), "handle": handle, "state": rec["state"],
                    "signalled": bool(outcome.signalled), "note": outcome.reason or "stopped the verified tree"}
        with self._lock:
            rec["state"] = "stopped"
            rec["termination_reason"] = "stopped_by_request" + (f": {reason}" if reason else "")
            self._save_locked()
        outcome = self._kill(rec, live)
        if live is not None:
            try:
                live.proc.wait(timeout=5)
            except Exception:
                pass
        return {"ok": True, "handle": handle, "state": "stopped",
                "signalled": bool(outcome.signalled), "descendants_signalled": max(0, len(outcome.signalled) - 1),
                "refused": [{"pid": p, "why": w} for p, w in outcome.rejected]}

    # ── list / housekeeping ─────────────────────────────────────────────

    def list(self, caller: Optional[Caller] = None, *, include_finished: bool = True) -> Dict[str, Any]:
        self._load()
        caller = caller or Caller()
        self._prune()
        rows: List[Dict[str, Any]] = []
        with self._lock:
            for rec in self._records.values():
                if rec.get("session_id") != caller.session_id:
                    continue
                if rec.get("owner") and caller.owner and rec["owner"] != caller.owner:
                    continue
                if not include_finished and rec.get("state") in FINISHED_STATES:
                    continue
                rows.append(self._public(rec))
        rows.extend(_bg_rows(caller, include_finished))
        rows.sort(key=lambda r: r.get("started_at") or 0)
        return {"ok": True, "handles": rows, "count": len(rows)}

    def _prune(self) -> None:
        now = time.time()
        with self._lock:
            stale = [h for h, r in self._records.items()
                     if r.get("state") in FINISHED_STATES and r.get("ended_at")
                     and now - r["ended_at"] > FINISHED_RETENTION_S and h not in self._live]
            stale = list(dict.fromkeys(stale))
            stale += [h for h, r in self._records.items() if r.get("state") in FINISHED_STATES
                      and h in self._live and r.get("ended_at") and now - r["ended_at"] > FINISHED_RETENTION_S
                      and h not in stale]
            for h in stale:
                self._records.pop(h, None)
                self._live.pop(h, None)
            if stale:
                self._save_locked()

    def cancel_for_session(self, session_id: str) -> List[Dict[str, Any]]:
        """The owner's explicit cancel: stop every live managed handle of the
        session. Each is stopped through the same verified path as ``stop``."""
        self._load()
        stopped = []
        with self._lock:
            targets = [(h, dict(r)) for h, r in self._records.items()
                       if r.get("session_id") == session_id and r.get("state") in LIVE_STATES + ("orphaned",)]
        for handle, rec in targets:
            result = self.stop(handle, Caller(owner=str(rec.get("owner") or ""), session_id=session_id),
                               reason="session cancelled")
            stopped.append({"handle": handle, **{k: result.get(k) for k in ("state", "signalled")}})
        return stopped

    # ── remote handles ──────────────────────────────────────────────────

    def register_remote(self, caller: Caller, command: str, remote_id: str) -> Dict[str, Any]:
        """Record a process running on a remote runner. The manager cannot see
        or kill it; it only tracks the handle so a disconnect is representable."""
        self._load()
        handle = "ph_" + uuid.uuid4().hex[:16]
        rec = {"handle": handle, "kind": "remote", "state": "running", "command": command,
               "owner": caller.owner, "session_id": caller.session_id, "remote_id": remote_id,
               "route": "remote", "started_at": time.time(), "ended_at": None, "exit_code": None,
               "termination_reason": "", "stdin_open": False, "tail": "", "output_total_bytes": 0}
        with self._lock:
            self._records[handle] = rec
            self._save_locked()
        return self._public(rec)

    def remote_finished(self, handle: str, exit_code: int, output: str = "") -> None:
        with self._lock:
            rec = self._records.get(handle)
            if rec is None or rec.get("kind") != "remote":
                return
            rec["tail"] = output[-TAIL_PERSIST_BYTES:]
            rec["output_total_bytes"] = len(output.encode("utf-8"))
            self._set_state_locked(rec, "exited", "exited", exit_code=exit_code)
            self._save_locked()

    def remote_disconnected(self, remote_id: str, reason: str = "runner disconnected") -> List[str]:
        """The runner went away: its running handles become ``uncertain``. The
        process may have finished, been killed or still be running; none of
        that is known, and nothing here pretends otherwise."""
        changed: List[str] = []
        with self._lock:
            for handle, rec in self._records.items():
                if rec.get("kind") == "remote" and rec.get("remote_id") == remote_id \
                        and rec.get("state") in LIVE_STATES:
                    rec["outcome_unknown"] = True
                    self._set_state_locked(rec, "uncertain", f"remote_disconnected: {reason}")
                    changed.append(handle)
            if changed:
                self._save_locked()
        return changed


# ── argv construction ──────────────────────────────────────────────────────

def _build_argv(command: Optional[str], argv: Optional[List[str]], shell: str) -> List[str]:
    if argv is not None:
        if not argv or not all(isinstance(a, str) and a for a in argv):
            raise ValueError("argv must be a non-empty list of non-empty strings")
        return list(argv)
    kind = (shell or "bash").strip().lower()
    if kind == "powershell":
        from src.bg_jobs import find_powershell
        ps = find_powershell()
        if not ps:
            raise ValueError("no PowerShell on this host")
        return [ps, "-NoProfile", "-NonInteractive", "-NoLogo", "-Command", command or ""]
    if kind in ("bash", "sh"):
        bash = find_bash() if kind == "bash" else None
        if bash:
            return [bash, "-c", command or ""]
        if IS_WINDOWS:
            raise ValueError("no bash on this host; use shell=powershell or pass argv")
        return ["/bin/sh", "-c", command or ""]
    raise ValueError(f"unknown shell {shell!r} (bash, sh, powershell, or pass argv)")


def _same_process(pid: Any, created: Any) -> bool:
    now = process_ownership.creation_time(int(pid))
    return now is not None and abs(now - float(created)) <= 1.0


# ── bg_jobs as handles ─────────────────────────────────────────────────────

_BG_STATE = {"queued": "starting", "running": "running", "done": "exited", "failed": "exited"}


def _bg_public(rec: Dict[str, Any]) -> Dict[str, Any]:
    state = _BG_STATE.get(rec.get("status"), "exited")
    reason = ""
    if rec.get("killed"):
        state, reason = "stopped", "stopped_by_request"
    elif rec.get("timed_out"):
        state, reason = "timed_out", f"max_runtime_exceeded: {rec.get('max_runtime_s')}s"
    elif rec.get("died"):
        state, reason = "lost", "process vanished without writing an exit code"
    elif state == "exited":
        reason = "exited"
    out = {"handle": BG_PREFIX + str(rec.get("id")), "kind": "bg_job", "state": state,
           "command": rec.get("command"), "shell": rec.get("shell"), "cwd": rec.get("cwd") or "",
           "session_id": rec.get("session_id"), "owner": "", "env_policy": "native_host_environment",
           "route": "host", "started_at": rec.get("started_at"), "ended_at": rec.get("ended_at"),
           "exit_code": rec.get("exit_code"), "termination_reason": reason, "stdin_open": False,
           "max_runtime_s": rec.get("max_runtime_s")}
    if state == "lost":
        out["outcome_unknown"] = True
    return out


def _bg_rows(caller: Caller, include_finished: bool) -> List[Dict[str, Any]]:
    try:
        from src import bg_jobs
        recs = bg_jobs.list_for_session(caller.session_id) if caller.session_id else []
    except Exception:
        return []
    rows = [_bg_public(r) for r in recs]
    return [r for r in rows if include_finished or r["state"] not in FINISHED_STATES]


def _bg_record(job_id: str, caller: Caller) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    from src import bg_jobs
    rec = bg_jobs.get(job_id)
    if rec is None:
        return None, {"ok": False, "code": "unknown_handle", "error": "no such handle"}
    if not caller.session_id or rec.get("session_id") != caller.session_id:
        return None, {"ok": False, "code": "not_owner", "error": "this handle belongs to another session"}
    return rec, None


def _bg_read(job_id: str, caller: Caller, cursor: Optional[int], max_bytes: int,
             manager: "ProcessManager") -> Dict[str, Any]:
    if not enabled():
        return {"ok": False, "code": "disabled", "error": f"the process manager is switched off (`{SETTING_ENABLED}`)"}
    rec, err = _bg_record(job_id, caller)
    if err:
        return err
    max_bytes = max(1, min(int(max_bytes or DEFAULT_READ_BYTES), MAX_READ_BYTES))
    view = _bg_public(rec)
    path = rec.get("log_path")
    data, nxt, dropped, total = b"", int(cursor or 0), 0, 0
    try:
        with open(path, "rb") as fh:
            total = os.fstat(fh.fileno()).st_size
            head = fh.read(2)
            if head[:2] in (b"\xff\xfe", b"\xfe\xff"):
                # UTF-16 log (Windows PowerShell 5): no byte cursor; hand back
                # the bounded text the bg_jobs reader already produces.
                from src import bg_jobs
                text = bg_jobs._read_output(rec)
                return {"ok": True, **{k: view[k] for k in ("handle", "state", "exit_code", "termination_reason")},
                        "output": text, "cursor": None, "next_cursor": None, "dropped_bytes": 0,
                        "cursor_supported": False, "more": False, "stdin_open": False}
            start = int(cursor) if cursor is not None else max(0, total - max_bytes)
            if cursor is None and total > max_bytes:
                dropped = total - max_bytes
            if start > total:
                start = total
            fh.seek(start)
            data = fh.read(max_bytes)
            nxt = start + len(data)
            if nxt < total or view["state"] not in FINISHED_STATES:
                data = data[:len(data) - _incomplete_tail(data)]
                nxt = start + len(data)
    except OSError:
        pass
    out = {"ok": True, **{k: view[k] for k in ("handle", "state", "exit_code", "termination_reason")},
           "output": data.decode("utf-8", "replace"), "cursor": nxt, "next_cursor": nxt,
           "dropped_bytes": dropped, "output_total_bytes": total, "more": nxt < total,
           "stdin_open": False, "buffer_source": "job_log_file"}
    if view.get("outcome_unknown"):
        out["outcome_unknown"] = True
    return out


def _bg_stop(job_id: str, caller: Caller) -> Dict[str, Any]:
    if not enabled():
        return {"ok": False, "code": "disabled", "error": f"the process manager is switched off (`{SETTING_ENABLED}`)"}
    if "process_stop" in caller.disabled_tools:
        return {"ok": False, "code": "tool_disabled", "error": "process_stop is disabled for this caller"}
    rec, err = _bg_record(job_id, caller)
    if err:
        return err
    from src import bg_jobs
    updated = bg_jobs.kill(job_id) or rec
    refused = updated.get("kill_refused")
    return {"ok": not refused or bool(updated.get("killed")), "handle": BG_PREFIX + job_id,
            "state": _bg_public(updated)["state"], "signalled": bool(updated.get("killed")),
            **({"note": refused} if refused else {})}


# ── the process-wide manager ───────────────────────────────────────────────

_MANAGER: Optional[ProcessManager] = None
_MANAGER_LOCK = threading.Lock()


def manager() -> ProcessManager:
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is None:
            _MANAGER = ProcessManager()
        return _MANAGER


def reset_for_tests() -> None:
    """Forget the singleton (tests only; live processes are not touched)."""
    global _MANAGER
    with _MANAGER_LOCK:
        _MANAGER = None
