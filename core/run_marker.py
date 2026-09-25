"""Leave a trace when the process ends without shutting down.

A test instance died mid-turn with nothing in its log: no exception, no
shutdown line, no Windows crash event. Two things make the next death
explainable:

* `enable_fault_log(log_dir)` sends Python's fault handler (segfaults, fatal
  errors in native code) to `logs/crash.log`, where a native crash leaves its
  thread stacks.
* `mark_running(path)` writes a marker with this process's pid and start
  time; a clean shutdown removes it (`clear`). If the next start still finds
  it, `previous_run_report(path)` says so: the previous process was killed
  from outside (task manager, another script stopping python, a closed
  console) or crashed before Python could log anything.
"""
from __future__ import annotations

import faulthandler
import json
import os
import time
from typing import IO, Optional

_fault_file: Optional[IO[str]] = None


def enable_fault_log(log_dir: str) -> Optional[str]:
    """Route faulthandler output to `<log_dir>/crash.log`. Returns the path,
    or None when the file cannot be opened."""
    global _fault_file
    try:
        os.makedirs(log_dir, exist_ok=True)
        path = os.path.join(log_dir, "crash.log")
        if _fault_file is None or _fault_file.closed:
            _fault_file = open(path, "a", encoding="utf-8")
            _fault_file.write(f"\n--- pid {os.getpid()} started {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
            _fault_file.flush()
        faulthandler.enable(_fault_file, all_threads=True)
        return path
    except Exception:  # noqa: BLE001 - diagnostics must never block startup
        return None


def previous_run_report(path: str) -> Optional[str]:
    """A sentence about the previous process when it left its marker behind,
    else None."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None
    except Exception:  # noqa: BLE001
        return "the previous process left an unreadable run marker; it did not shut down cleanly"
    pid = data.get("pid")
    started = data.get("started")
    if pid == os.getpid():
        return None
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started)) if isinstance(started, (int, float)) else "?"
    return (f"the previous process (pid {pid}, started {when}) ended without shutting down: "
            "it was killed from outside or crashed in native code (see logs/crash.log)")


def mark_running(path: str) -> None:
    try:
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"pid": os.getpid(), "started": time.time()}, fh)
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001
        pass


def clear(path: str) -> None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            if json.load(fh).get("pid") != os.getpid():
                return
        os.remove(path)
    except Exception:  # noqa: BLE001
        pass