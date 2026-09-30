"""Owned Windows JobObject and trusted -I bootstrap; no PID-tree teardown."""
from __future__ import annotations

import ctypes
import json
import os
import runpy
import sys
import uuid
from ctypes import wintypes


class WindowsJobError(RuntimeError):
    def __init__(self, code, winerror=0):
        self.code = code
        self.winerror = winerror
        super().__init__(code)


class _BasicLimits(ctypes.Structure):
    _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
        ("flags", wintypes.DWORD), ("minimum_ws", ctypes.c_size_t),
        ("maximum_ws", ctypes.c_size_t), ("active_processes", wintypes.DWORD),
        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
        ("scheduling", wintypes.DWORD)]


class _IOCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in
        ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("basic", _BasicLimits), ("io", _IOCounters),
        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
        ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]


def _kernel():
    if os.name != "nt":
        raise WindowsJobError("windows_job_unavailable")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    for name, args, result in (
        ("CreateJobObjectW", [ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
        ("OpenJobObjectW", [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
        ("SetInformationJobObject", [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
        ("SetHandleInformation", [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD], wintypes.BOOL),
        ("AssignProcessToJobObject", [wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
        ("GetCurrentProcess", [], wintypes.HANDLE),
        ("CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
    ):
        function = getattr(kernel, name)
        function.argtypes, function.restype = args, result
    return kernel


class OwnedWindowsJob:
    def __init__(self, kernel, name, handle):
        self._kernel, self.name, self.handle = kernel, name, handle

    def close(self):
        if self.handle is not None:
            if not self._kernel.CloseHandle(self.handle):
                raise WindowsJobError("windows_job_close_failed", ctypes.get_last_error())
            self.handle = None


def create_owned_job():
    kernel = _kernel()
    name = "Local\\FaustusCodeMode_" + uuid.uuid4().hex
    handle = kernel.CreateJobObjectW(None, name)
    if not handle:
        raise WindowsJobError("windows_job_create_failed", ctypes.get_last_error())
    job = OwnedWindowsJob(kernel, name, handle)
    try:
        if ctypes.get_last_error() == 183:
            raise WindowsJobError("windows_job_name_collision", 183)
        # Neither bootstrap nor descendants inherit the parent's owning handle.
        if not kernel.SetHandleInformation(handle, 1, 0):
            raise WindowsJobError("windows_job_configure_failed", ctypes.get_last_error())
        limits = _ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise WindowsJobError("windows_job_configure_failed", ctypes.get_last_error())
        return job
    except BaseException:
        try:
            job.close()
        except WindowsJobError:
            pass  # Preserve the creation/configuration failure.
        raise


def _assign_current(job_name):
    kernel = _kernel()
    handle = kernel.OpenJobObjectW(1, False, job_name)  # JOB_OBJECT_ASSIGN_PROCESS
    if not handle:
        raise WindowsJobError("windows_job_open_failed", ctypes.get_last_error())
    try:
        if not kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess()):
            raise WindowsJobError("windows_job_assign_failed", ctypes.get_last_error())
    finally:
        # Parent must remain the sole job-handle owner before user code runs.
        if not kernel.CloseHandle(handle):
            raise WindowsJobError("windows_job_bootstrap_close_failed", ctypes.get_last_error())


def main():
    try:
        job_name, guest_path, user_code_path = sys.argv[1:]
        _assign_current(job_name)
    except WindowsJobError as error:
        print(json.dumps({"type": "containment_error", "error_code": error.code,
                          "winerror": error.winerror}), flush=True)
        return 77
    print(json.dumps({"type": "containment_ready", "protocol": 1,
                      "mechanism": "windows_job_object"}), flush=True)
    sys.argv = [guest_path, user_code_path]
    runpy.run_path(guest_path, run_name="__main__")
    return 0


if __name__ == "__main__":
    sys.exit(main())
