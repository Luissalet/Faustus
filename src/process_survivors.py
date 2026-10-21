"""Processes that outlive the agent command that started them (#1105, from #1083).

A model-written test may start a server with ``Popen`` and fail before stopping it:
the bash/python tool then returns while that child keeps running, unseen by the model
and by the process manager. Here every host command (Windows) starts inside its own
Job Object from its first instruction. It is created suspended, assigned to the job,
then resumed, so no descendant can be born outside it. When the command ends (success,
failure, timeout or cancel), the members still alive are handed to the process manager
as one handle of the session, and the model is told about them.

Nothing is killed automatically: a server the command meant to leave running stays
running and becomes manageable (`process_list`, `process_stop`). The job has no
KILL_ON_JOB_CLOSE, so Faustus closing its handle (or exiting) does not end the members.
It allows breakaway, so a program that explicitly breaks away still can; that program
is then not tracked. POSIX is not covered yet: the scope's mechanism is "none" there,
and the tools behave exactly as before.
"""
from __future__ import annotations

import ctypes
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

CREATE_SUSPENDED = 0x00000004
_JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
_JOB_EXTENDED_LIMIT_INFORMATION = 9
_JOB_BASIC_PROCESS_ID_LIST = 3
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_SUSPEND_RESUME = 0x0800
_MAX_IDS = 4096
#: Console hosts belong to the console programs they serve; alone they are not a survivor.
_NOT_SURVIVORS = frozenset({"conhost.exe"})
SETTLE_S = 1.0

_API: Any = None


def _win():
    """(kernel32, ntdll) with the signatures used here; Windows only."""
    global _API
    if _API is None:
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        ntdll = ctypes.WinDLL("ntdll")
        for name, args, result in (
            ("CreateJobObjectW", [ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            ("SetInformationJobObject", [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD],
             wintypes.BOOL),
            ("AssignProcessToJobObject", [wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            ("QueryInformationJobObject", [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                           ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            ("TerminateJobObject", [wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            ("OpenProcess", [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            ("TerminateProcess", [wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            ("CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
        ):
            function = getattr(kernel, name)
            function.argtypes, function.restype = args, result
        ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
        ntdll.NtResumeProcess.restype = ctypes.c_long
        _API = (kernel, ntdll)
    return _API


class _ProcessIdList(ctypes.Structure):
    _fields_ = [("assigned", ctypes.c_uint32), ("listed", ctypes.c_uint32),
                ("ids", ctypes.c_size_t * _MAX_IDS)]


def _process_handle(proc: Any) -> Optional[int]:
    """The handle the launcher already holds (asyncio or subprocess Popen)."""
    popen = proc
    transport = getattr(proc, "_transport", None)
    if transport is not None:
        try:
            popen = transport.get_extra_info("subprocess") or proc
        except Exception:  # noqa: BLE001
            popen = proc
    handle = getattr(popen, "_handle", None)
    return int(handle) if handle else None


class CommandScope:
    """One agent command's process scope. ``mechanism`` is "job_object" or "none"."""

    def __init__(self, job: Optional[int] = None):
        self._job = job
        self._lock = threading.Lock()
        self.mechanism = "job_object" if job else "none"
        self.root_pid: Optional[int] = None

    @classmethod
    def create(cls) -> "CommandScope":
        if os.name != "nt":
            return cls()
        try:
            kernel, _ = _win()
            job = kernel.CreateJobObjectW(None, None)
            if not job:
                return cls()
            from src.code_mode.windows_job_bootstrap import _ExtendedLimits
            limits = _ExtendedLimits()
            limits.basic.flags = _JOB_OBJECT_LIMIT_BREAKAWAY_OK
            if not kernel.SetInformationJobObject(job, _JOB_EXTENDED_LIMIT_INFORMATION,
                                                  ctypes.byref(limits), ctypes.sizeof(limits)):
                kernel.CloseHandle(job)
                return cls()
            return cls(job)
        except Exception as exc:  # noqa: BLE001 - no scope: the command runs as before
            logger.debug("command scope unavailable: %s", exc)
            return cls()

    def spawn_kwargs(self) -> Dict[str, Any]:
        """Extra launch arguments: start suspended only when a job will take it."""
        return {"creationflags": CREATE_SUSPENDED} if self._job else {}

    def attach_and_resume(self, proc: Any) -> None:
        """Put the suspended process in the job and resume it.

        Never returns with the process still suspended: if it cannot be resumed it
        is terminated and RuntimeError says so. An assignment failure only drops
        tracking (mechanism "none"); the command still runs.
        """
        self.root_pid = getattr(proc, "pid", None)
        if not self._job:
            return
        import asyncio
        import subprocess
        if not isinstance(proc, (asyncio.subprocess.Process, subprocess.Popen)):
            # Not a process this module can hold (a launcher stand-in): no scope.
            self.mechanism = "none"
            self.close()
            return
        kernel, ntdll = _win()
        handle = _process_handle(proc)
        opened = None
        if handle is None and self.root_pid:
            opened = kernel.OpenProcess(_PROCESS_TERMINATE | _PROCESS_SET_QUOTA | _PROCESS_SUSPEND_RESUME,
                                        False, int(self.root_pid))
            handle = opened or None
        if handle is None:
            self.close()
            try:  # cannot resume what cannot be reached: do not leave it suspended forever
                import psutil
                psutil.Process(int(self.root_pid)).kill()
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError(f"started process {self.root_pid} could not be reached to resume it; "
                               "it was terminated")
        try:
            if not kernel.AssignProcessToJobObject(self._job, handle):
                logger.debug("job assignment failed (winerror %s); command runs untracked",
                             ctypes.get_last_error())
                self.mechanism = "none"
                self.close()
            if ntdll.NtResumeProcess(handle) != 0:
                kernel.TerminateProcess(handle, 1)
                self.close()
                raise RuntimeError(f"started process {self.root_pid} could not be resumed; it was terminated")
        finally:
            if opened:
                kernel.CloseHandle(opened)

    def members(self) -> List[int]:
        with self._lock:
            if not self._job:
                return []
            kernel, _ = _win()
            ids = _ProcessIdList()
            if not kernel.QueryInformationJobObject(self._job, _JOB_BASIC_PROCESS_ID_LIST,
                                                    ctypes.byref(ids), ctypes.sizeof(ids), None):
                return []
            return [int(ids.ids[i]) for i in range(min(int(ids.listed), _MAX_IDS))]

    def survivors(self) -> List[Dict[str, Any]]:
        """Members still running, with the identity needed to act on them safely."""
        import psutil
        out: List[Dict[str, Any]] = []
        for pid in self.members():
            if pid == self.root_pid:
                continue
            try:
                process = psutil.Process(pid)
                if process.status() == psutil.STATUS_ZOMBIE:
                    continue
                name = process.name()
                if name.lower() in _NOT_SURVIVORS:
                    continue
                try:
                    cmdline = " ".join(process.cmdline())
                except psutil.Error:
                    cmdline = name
                out.append({"pid": pid, "created": process.create_time(), "name": name,
                            "cmdline": cmdline[:300]})
            except psutil.Error:
                continue
        out.sort(key=lambda s: s["created"])
        return out

    def settle(self, grace_s: float = SETTLE_S) -> List[Dict[str, Any]]:
        """Survivors once the command's own short-lived helpers had time to exit."""
        deadline = time.monotonic() + max(0.0, grace_s)
        current = self.survivors()
        while current and time.monotonic() < deadline:
            time.sleep(0.1)
            current = self.survivors()
        return current

    def terminate(self) -> bool:
        with self._lock:
            if not self._job:
                return False
            kernel, _ = _win()
            return bool(kernel.TerminateJobObject(self._job, 1))

    def close(self) -> None:
        """Release Faustus's handle on the job. Members keep running."""
        with self._lock:
            if self._job:
                try:
                    _win()[0].CloseHandle(self._job)
                except Exception:  # noqa: BLE001
                    pass
                self._job = None


def keep_survivors(scope: CommandScope, command: str, caller: Any, *, grace_s: float = SETTLE_S
                   ) -> List[Dict[str, Any]]:
    """After the command ended: hand still-running members to the process manager.

    Returns one entry per survivor ({pid, name, cmdline, handle}). The scope is closed
    when nothing survived; otherwise the manager owns it from here.
    """
    if scope.mechanism != "job_object":
        scope.close()
        return []
    try:
        alive = scope.settle(grace_s)
    except Exception as exc:  # noqa: BLE001 - never turn a finished command into a tool error
        logger.debug("survivor check failed: %s", exc)
        alive = []
    if not alive:
        scope.close()
        return []
    handle = ""
    try:
        from src.process_manager import enabled, manager
        if enabled():
            handle = manager().adopt(caller, command, scope, alive).get("handle", "")
    except Exception as exc:  # noqa: BLE001
        logger.debug("survivor adoption failed: %s", exc)
    if not handle:
        scope.close()
    return [{"pid": s["pid"], "name": s["name"], "cmdline": s["cmdline"], "handle": handle} for s in alive]


def survivors_note(kept: List[Dict[str, Any]]) -> str:
    """The text the model reads after the command's own output."""
    if not kept:
        return ""
    handle = kept[0].get("handle") or ""
    shown = ", ".join(f"pid {k['pid']} `{(k.get('cmdline') or k.get('name') or '')[:120]}`" for k in kept[:5])
    more = f" and {len(kept) - 5} more" if len(kept) > 5 else ""
    count = f"{len(kept)} process{'es' if len(kept) != 1 else ''}"
    if handle:
        how = (f"They are tracked as handle {handle} and were NOT stopped. If they were temporary "
               f"(a test server), stop them with process_stop {{\"handle\": \"{handle}\"}}; if you meant to "
               "leave them running, they stay listed in process_list.")
    else:
        how = "The process manager is off, so they are not tracked; they were NOT stopped."
    return f"[Still running after this command: {count} it started: {shown}{more}. {how}]"
