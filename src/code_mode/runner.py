"""Code Mode process runner (T6, A11): launches a separate host subprocess,
services the guest's tool-call requests through ``bridge.py``, and enforces
wall time / call count / output size / CPU / memory quotas. When a quota is
hit the process tree is killed and the result carries a diagnostic receipt::

    {"terminated_by": "timeout"|"max_calls"|"output"|"cpu"|"memory",
     "calls_made": int, "elapsed_ms": float, "output_bytes": int,
     "last_call": str | None}
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
import uuid
from typing import Any, Optional

from core.platform_compat import IS_WINDOWS
from src.code_mode import bridge
from src.code_mode.outcomes import CallOutcomes

logger = logging.getLogger(__name__)

_GUEST_PATH = os.path.join(os.path.dirname(__file__), "guest.py")
_WINDOWS_BOOTSTRAP_PATH = os.path.join(os.path.dirname(__file__), "windows_job_bootstrap.py")

# Python -I isolates interpreter configuration, not filesystem or network.
HOST_RUNTIME_GUARANTEES = {
    "mode": "host_process",
    "filesystem_isolated": False,
    "network_isolated": False,
    "tool_policy_scope": "tools.call_only",
}

DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_MAX_CALLS = 50
DEFAULT_MAX_OUTPUT_BYTES = 200_000
# RLIMIT_AS ceiling (POSIX only) -- generous enough for ordinary tool-composing
# scripts, tight enough that a runaway allocation dies well before it can
# threaten the host.
DEFAULT_MAX_MEMORY_BYTES = 512 * 1024 * 1024
PROTOCOL_FRAME_OVERHEAD = 64 * 1024


class _ProtocolFrameLimit(Exception):
    pass


def _settings():
    try:
        from src.settings import get_setting
    except Exception:  # pragma: no cover - settings module always present in prod
        return None
    return get_setting


def _limits() -> dict:
    get_setting = _settings()
    if get_setting is None:
        return {
            "timeout_seconds": DEFAULT_TIMEOUT_SECONDS,
            "max_calls": DEFAULT_MAX_CALLS,
            "max_output_bytes": DEFAULT_MAX_OUTPUT_BYTES,
            "max_memory_bytes": DEFAULT_MAX_MEMORY_BYTES,
        }
    return {
        "timeout_seconds": int(get_setting("agent_code_mode_timeout_seconds", DEFAULT_TIMEOUT_SECONDS) or DEFAULT_TIMEOUT_SECONDS),
        "max_calls": int(get_setting("agent_code_mode_max_calls", DEFAULT_MAX_CALLS) or DEFAULT_MAX_CALLS),
        "max_output_bytes": int(get_setting("agent_code_mode_max_output_bytes", DEFAULT_MAX_OUTPUT_BYTES) or DEFAULT_MAX_OUTPUT_BYTES),
        "max_memory_bytes": DEFAULT_MAX_MEMORY_BYTES,
    }


def _minimal_env() -> dict:
    """No network credentials, no MCP/model keys, nothing of this process's
    own environment -- only what a bare interpreter needs to start."""
    env = {}
    for key in ("PATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "TEMP", "TMP"):
        if key in os.environ:
            env[key] = os.environ[key]
    # -I already ignores PYTHON* env vars and skips the user site dir; TEMP/TMP
    # above only matter for tempfile lookups the guest itself never does (its
    # cwd is the temp workdir runner.py creates), left in for Windows tools
    # that assume they exist.
    return env


class _WallClock:
    """A run's own timeout deadline, mutable so a paused approval question
    (bridge._pause_for_approval) can push it out for exactly as long as the
    script was blocked on a human -- the SCRIPT's wall-time budget must not
    be spent doing nothing but waiting for someone to click a button."""

    __slots__ = ("deadline",)

    def __init__(self, timeout_seconds: float):
        self.deadline = time.monotonic() + timeout_seconds

    def extend(self, seconds: float) -> None:
        self.deadline += seconds

    def remaining(self) -> float:
        return self.deadline - time.monotonic()


def _preexec_fn(max_memory_bytes: Optional[int], cpu_seconds: Optional[int]):
    """POSIX only: RLIMIT_AS / RLIMIT_CPU applied in the child right after
    fork, before exec -- the kernel enforces these, not the guest script, so
    they hold even against code that tries to defeat its own bootstrap."""
    def _apply():
        try:
            import resource
            if max_memory_bytes:
                resource.setrlimit(resource.RLIMIT_AS, (max_memory_bytes, max_memory_bytes))
            if cpu_seconds:
                resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        except Exception:
            pass
        # Isolate the child into its own process group so a whole tree
        # (a subprocess the guest itself started) can be killed together.
        try:
            os.setsid()
        except Exception:
            pass
    return _apply


async def _read_line(stream: asyncio.StreamReader) -> Optional[bytes]:
    try:
        line = await stream.readline()
    except (ValueError, asyncio.LimitOverrunError) as exc:
        raise _ProtocolFrameLimit("Code Mode protocol frame exceeded its bound") from exc
    except Exception:
        return None
    return line or None


class _WindowsProcessScope:
    def __init__(self, job):
        self.job = job
        self.proc = None
        self.spawn_task = None
        self.pump_task = None
        self.assigned = False

    def close_job(self):
        self.job.close()

    async def cleanup(self):
        # Close the native handle first: descendants are killed without walking
        # recorded parent PIDs, including when cancellation precedes spawn ACK.
        self.close_job()
        if self.spawn_task is not None:
            await asyncio.wait({self.spawn_task}, timeout=5)
            if self.spawn_task.done():
                try:
                    self.proc = self.spawn_task.result()
                except (Exception, asyncio.CancelledError):
                    pass
        if self.proc is None:
            return
        if self.pump_task is not None and not self.pump_task.done():
            self.pump_task.cancel()
            await asyncio.wait({self.pump_task}, timeout=5)
        if self.pump_task is not None and self.pump_task.done():
            await asyncio.gather(self.pump_task, return_exceptions=True)
        if self.proc.returncode is None:
            try:
                self.proc.kill()  # Owned direct handle, possibly not assigned yet.
            except ProcessLookupError:
                pass
        if self.proc.stdin is not None:
            self.proc.stdin.close()

        async def discard(reader):
            while await reader.read(8192):
                pass

        # Resume paused pipe readers without retaining output; a killed process
        # can otherwise wait forever for its transport to report EOF.
        drains = [asyncio.create_task(discard(reader)) for reader in
                  (self.proc.stdout, self.proc.stderr) if reader is not None]
        done, pending = await asyncio.wait(drains, timeout=5) if drains else (set(), set())
        for task in pending:
            task.cancel()
        await asyncio.gather(*done, *pending, return_exceptions=True)
        try:
            await asyncio.wait_for(self.proc.wait(), timeout=5)
            if self.proc.stdin is not None:
                await asyncio.wait_for(self.proc.stdin.wait_closed(), timeout=1)
        except (Exception, asyncio.CancelledError):
            pass


async def run_code_mode(
    code: str,
    *,
    session_id: Optional[str] = None,
    owner: Optional[str] = None,
    disabled_tools: Optional[set] = None,
    workspace: Optional[str] = None,
    workspace_roots: Optional[list] = None,
    tool_policy: Any = None,
    security_context: Any = None,
) -> dict:
    kwargs = dict(session_id=session_id, owner=owner, disabled_tools=disabled_tools,
                  workspace=workspace, workspace_roots=workspace_roots,
                  tool_policy=tool_policy, security_context=security_context)
    if not IS_WINDOWS:
        return await _run_code_mode_impl(code, **kwargs)
    from src.code_mode.windows_job_bootstrap import create_owned_job, WindowsJobError
    try:
        scope = _WindowsProcessScope(create_owned_job())
    except WindowsJobError as error:
        return {"error": "Code Mode containment could not start", "error_code": error.code,
                "exit_code": 1, "runtime_guarantees": dict(HOST_RUNTIME_GUARANTEES)}
    try:
        result = await _run_code_mode_impl(code, _process_scope=scope, **kwargs)
        if scope.assigned:
            result["process_containment"] = {"mechanism": "windows_job_object", "assigned": True}
        return result
    finally:
        await scope.cleanup()


async def _run_code_mode_impl(
    code: str,
    *,
    session_id: Optional[str] = None,
    owner: Optional[str] = None,
    disabled_tools: Optional[set] = None,
    workspace: Optional[str] = None,
    workspace_roots: Optional[list] = None,
    tool_policy: Any = None,
    security_context: Any = None,
    _process_scope=None,
) -> dict:
    limits = _limits()
    timeout_s = max(1, limits["timeout_seconds"])
    max_calls = max(1, limits["max_calls"])
    max_output_bytes = max(1, limits["max_output_bytes"])
    max_memory_bytes = limits["max_memory_bytes"]
    # JSON may escape each output byte as six ASCII bytes (e.g. \u0000).
    # The extra space covers framing fields; this is not a larger output quota.
    frame_limit = 6 * max_output_bytes + PROTOCOL_FRAME_OVERHEAD

    workdir = tempfile.mkdtemp(prefix="faustus_code_mode_")
    user_code_path = os.path.join(workdir, "user_code.py")
    with open(user_code_path, "w", encoding="utf-8") as f:
        f.write(code)

    popen_kwargs: dict[str, Any] = dict(
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=workdir,
        env=_minimal_env(),
        limit=frame_limit,
    )
    if not IS_WINDOWS:
        popen_kwargs["preexec_fn"] = _preexec_fn(max_memory_bytes, timeout_s + 5)
    else:
        popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    t0 = time.monotonic()
    try:
        command = [sys.executable, "-I", _GUEST_PATH, user_code_path]
        if _process_scope is not None:
            command = [sys.executable, "-I", _WINDOWS_BOOTSTRAP_PATH, _process_scope.job.name,
                       _GUEST_PATH, user_code_path]
            _process_scope.spawn_task = asyncio.create_task(
                asyncio.create_subprocess_exec(*command, **popen_kwargs))
            proc = await asyncio.shield(_process_scope.spawn_task)
            _process_scope.proc = proc
        else:
            proc = await asyncio.create_subprocess_exec(*command, **popen_kwargs)
    except Exception as e:  # noqa: BLE001
        logger.exception("code_mode: failed to spawn guest process")
        return {"error": f"Code Mode failed to start: {e}", "exit_code": 1,
                "runtime_guarantees": dict(HOST_RUNTIME_GUARANTEES)}

    calls_made = 0
    last_call: Optional[str] = None
    terminated_by: Optional[str] = None
    final_payload: Optional[dict] = None
    stderr_tail = b""
    wall_clock = _WallClock(timeout_s)
    approvals_log: list = []
    outcomes = CallOutcomes()

    if _process_scope is not None:
        # No configuration is released to the guest until assignment is ACKed.
        try:
            raw = await asyncio.wait_for(_read_line(proc.stdout), timeout=min(timeout_s, 5))
            ack = json.loads(raw.decode("utf-8")) if raw is not None else {}
        except Exception:
            ack = {}
        if (not isinstance(ack, dict) or type(ack.get("protocol")) is not int
                or ack != {"type": "containment_ready", "protocol": 1,
                           "mechanism": "windows_job_object"}):
            code = ack.get("error_code") if isinstance(ack, dict) else None
            if code not in {"windows_job_open_failed", "windows_job_assign_failed",
                            "windows_job_bootstrap_close_failed", "windows_job_unavailable"}:
                code = "windows_job_protocol_failed"
            return {"error": "Code Mode containment could not start", "error_code": code,
                    "exit_code": 1, "runtime_guarantees": dict(HOST_RUNTIME_GUARANTEES)}
        _process_scope.assigned = True

    proc.stdin.write((json.dumps({
        "max_calls": max_calls,
        "max_output_bytes": max_output_bytes,
    }) + "\n").encode("utf-8"))
    try:
        await proc.stdin.drain()
    except Exception:
        pass

    async def _pump():
        nonlocal calls_made, last_call, terminated_by, final_payload
        while True:
            try:
                raw = await _read_line(proc.stdout)
            except _ProtocolFrameLimit:
                terminated_by = "protocol_frame_limit"
                return
            if raw is None:
                return
            try:
                msg = json.loads(raw.decode("utf-8", "replace"))
            except Exception:
                continue
            msg_type = msg.get("type")
            if msg_type == "call":
                if calls_made >= max_calls:
                    terminated_by = "max_calls"
                    return
                calls_made += 1
                last_call = str(msg.get("tool") or "")
                call_id = str(msg.get("call_id") or "")
                dispatch_id = f"code_mode:{call_id}:{uuid.uuid4().hex[:8]}"
                outcome_record = outcomes.begin(dispatch_id, last_call)
                result = await bridge.dispatch_call(
                    msg.get("tool"),
                    msg.get("args"),
                    session_id=session_id,
                    owner=owner,
                    workspace=workspace,
                    workspace_roots=workspace_roots,
                    disabled_tools=disabled_tools,
                    call_id=dispatch_id,
                    tool_policy=tool_policy,
                    security_context=security_context,
                    wall_clock=wall_clock,
                    approvals_log=approvals_log,
                )
                outcomes.finish(outcome_record, result)
                # "ok" transport-wise means "the tool ran" (even a functional
                # error, e.g. a bad path or a policy rejection, is `ok=True`
                # with the error carried inside `result` -- exactly how
                # execute_tool_block reports it to every other caller); the
                # guest surfaces `result["error"]`/`result["blocked"]` itself.
                response = {"call_id": call_id, "ok": True, "result": result}
                try:
                    proc.stdin.write((json.dumps(response, default=str) + "\n").encode("utf-8"))
                    await proc.stdin.drain()
                except Exception:
                    return
            elif msg_type == "list":
                call_id = str(msg.get("call_id") or "")
                rows = bridge.list_tools(disabled_tools, detail=str(msg.get("detail") or "catalog"))
                response = {"call_id": call_id, "ok": True, "result": rows}
                try:
                    proc.stdin.write((json.dumps(response, default=str) + "\n").encode("utf-8"))
                    await proc.stdin.drain()
                except Exception:
                    return
            elif msg_type == "final":
                final_payload = msg
                if _process_scope is not None:
                    _process_scope.close_job()
                return

    async def _drain_stderr():
        nonlocal stderr_tail
        try:
            while True:
                data = await proc.stderr.read(8192)
                if not data:
                    break
                stderr_tail = (stderr_tail + data)[-8192:]
        except Exception:
            pass

    async def _wait_pump():
        await asyncio.gather(_pump(), _drain_stderr())

    # A plain `asyncio.wait_for(_wait_pump(), timeout=timeout_s)` cannot be
    # extended once started -- and it must be extendable, because a paused
    # approval question (bridge._pause_for_approval) lives INSIDE this same
    # awaited chain (dispatch_call, called from _pump) and can legitimately
    # take up to `agent_code_mode_approval_wait_seconds` to resolve. Poll a
    # mutable deadline (`wall_clock`) in short slices instead: the pause
    # pre-extends it before blocking and gives back the unused slack after,
    # so this loop's own timeout check simply never fires while a human is
    # being asked, and still fires promptly for a script that is just slow.
    pump_task = asyncio.ensure_future(_wait_pump())
    if _process_scope is not None:
        _process_scope.pump_task = pump_task
    try:
        while not pump_task.done():
            remaining = wall_clock.remaining()
            if terminated_by is not None or remaining <= 0:
                terminated_by = terminated_by or "timeout"
                if _process_scope is not None:
                    _process_scope.close_job()
                pump_task.cancel()
                try:
                    await pump_task
                except (asyncio.CancelledError, Exception):
                    pass
                break
            try:
                await asyncio.wait_for(
                    asyncio.shield(pump_task), timeout=min(remaining, 0.5)
                )
            except asyncio.TimeoutError:
                continue
    except asyncio.CancelledError:
        if _process_scope is not None:
            _process_scope.close_job()
        # Cancellation must not bypass the reap below or leave the shielded
        # protocol/stderr pump running. Only terminate our direct child;
        # descendant containment is a separate runtime guarantee.
        pump_task.cancel()
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        # Do not let a bridge that ignores cancellation delay child teardown.
        await asyncio.wait({pump_task}, timeout=5)
        if pump_task.done():
            await asyncio.gather(pump_task, return_exceptions=True)
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except Exception:
            # Preserve the caller's cancellation even if reaping fails.
            pass
        raise
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass

    elapsed_ms = round((time.monotonic() - t0) * 1000.0, 1)

    if terminated_by is not None or final_payload is None:
        if terminated_by is None:
            # The guest closed the pipe without sending a final message
            # (crashed, or the parent's own max_calls kill above fired) --
            # treat it as its own diagnostic reason when known, else "error".
            terminated_by = terminated_by or "error"
        from src.agent_tools.subprocess_tools import _kill_tree_async
        if _process_scope is not None:
            _process_scope.close_job()
        else:
            await _kill_tree_async(proc)
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except Exception:
            pass
        receipt = {
            "terminated_by": terminated_by,
            "calls_made": calls_made,
            "elapsed_ms": elapsed_ms,
            "output_bytes": 0,
            "last_call": last_call,
        }
        return {
            "error": f"Code Mode terminated: {terminated_by}",
            "runtime_guarantees": dict(HOST_RUNTIME_GUARANTEES),
            "exit_code": 1,
            "terminated": True,
            "receipt": receipt,
            "stderr": stderr_tail.decode("utf-8", "replace")[-2000:] if stderr_tail else "",
            "approvals": approvals_log,
            **outcomes.fields(),
        }

    # A cooperative finish: the guest itself hit (and reported) a quota, or
    # finished normally. Windows has closed the job after its final frame;
    # on POSIX the cooperative child exits on its own. Reap the direct child.
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except Exception:
        from src.agent_tools.subprocess_tools import _kill_tree_async
        if _process_scope is not None:
            _process_scope.close_job()
        else:
            await _kill_tree_async(proc)

    status = final_payload.get("status") or "ok"
    if status != "ok":
        receipt = {
            "terminated_by": status,
            "calls_made": int(final_payload.get("calls_made") or calls_made),
            "elapsed_ms": elapsed_ms,
            "output_bytes": int(final_payload.get("output_bytes") or 0),
            "last_call": final_payload.get("last_call") or last_call,
        }
        return {
            "error": final_payload.get("error") or f"Code Mode terminated: {status}",
            "runtime_guarantees": dict(HOST_RUNTIME_GUARANTEES),
            "exit_code": 1,
            "terminated": True,
            "receipt": receipt,
            "output": final_payload.get("output") or "",
            "approvals": approvals_log,
            **outcomes.fields(),
        }

    _output = final_payload.get("output") or ""
    return {
        "output": _output,
        "runtime_guarantees": dict(HOST_RUNTIME_GUARANTEES),
        "exit_code": 0,
        "calls_made": int(final_payload.get("calls_made") or calls_made),
        "elapsed_ms": elapsed_ms,
        "result_chars": len(_output),
        "approvals": approvals_log,
        **outcomes.fields(),
    }
