"""src/process_launch.py — F1.4: the generic core of "start a local process
and let it outlive the request", factored out for `src/launch_profiles.py`.

`routes/cookbook_routes.py::_launch_local_detached` is NOT reused directly:
it is a private closure over `setup_cookbook_routes`'s own conventions — a
bash wrapper script written under `TMUX_LOG_DIR`, `bash_lines` templated for
Git-Bash-on-Windows, a `<session>.pid`/`.log` pair meant for the tmux-session
poller. A launch profile starts an arbitrary admin-configured executable
directly (no bash wrapper, no shell involved at all — rule 4 requires
`shell=False` and a structured argv), so its actual launch mechanics — build
argv, ``subprocess.Popen`` with `core.platform_compat.detached_popen_kwargs()`,
tell `src.process_ownership` about it — are the part worth sharing, and they
are exactly what this module holds. See the batch report for the full
reasoning; `routes/cookbook_routes.py` itself is untouched by this lote, and
its own tests are unaffected.

Every function here is synchronous and side-effect-light enough to unit-test
without a real OS process (tests substitute `subprocess.Popen`).
"""
from __future__ import annotations

import logging
import json
import os
import subprocess
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from core.platform_compat import detached_popen_kwargs
from src import process_ownership

logger = logging.getLogger(__name__)

#: Same cap the MCP stderr log uses (src/mcp_manager.py) — a log a launched
#: process can grow without bound is a disk-fill vector, not a debugging aid.
LOG_MAX_BYTES = 256 * 1024


def trim_log(path: str, max_bytes: int = LOG_MAX_BYTES) -> None:
    """Cap `path` to `max_bytes`, dropping from the front at a line boundary —
    the crash is always the last thing printed, never the first."""
    try:
        if not os.path.exists(path) or os.path.getsize(path) <= max_bytes:
            return
        with open(path, "rb") as fh:
            fh.seek(-max_bytes, os.SEEK_END)
            data = fh.read()
        cut = data.find(b"\n")
        if 0 <= cut < len(data) - 1:
            data = data[cut + 1:]
        tmp = path + ".trim"
        with open(tmp, "wb") as fh:
            fh.write(b"[... earlier output dropped ...]\n")
            fh.write(data)
        os.replace(tmp, path)
    except OSError as exc:
        logger.debug("could not trim launch log %s: %s", path, exc)


@dataclass
class LaunchResult:
    pid: int
    log_path: str
    spawned_at: Optional[float]


def spawn_detached(
    argv: List[str],
    *,
    cwd: str,
    env: Optional[Dict[str, str]],
    log_path: str,
    owner: str = "",
) -> LaunchResult:
    """Start `argv` as a fully-detached child, its stdout/stderr appended to
    a bounded log file at `log_path`. `shell=False` always — `argv` is a
    structured list built by the caller, never a string handed to a shell
    (rule 4). Records the spawn with `src.process_ownership.note_started` so
    a later teardown attempt can prove it is still ours to touch (this
    module deliberately never kills anything itself — see the module-level
    launch_profiles docstring: "al parar Faustus no se terminan estos
    procesos").
    """
    trim_log(log_path)
    os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
    log_handle = open(log_path, "ab")
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            shell=False,
            **detached_popen_kwargs(),
        )
    finally:
        log_handle.close()
    process_ownership.note_started(proc, command=" ".join(argv), owner=owner)
    spawned_at = process_ownership.creation_time(proc.pid)
    _note_detached(proc.pid, spawned_at, " ".join(argv))
    return LaunchResult(pid=proc.pid, log_path=log_path, spawned_at=spawned_at)


def detached_ledger_path() -> str:
    """`data/runtime/detached.json`: {pid: {created, command}} of the children
    meant to outlive this server. `server_runtime.serve` reads it on shutdown
    and leaves those (and their descendants) alone — on Windows a detached
    child still lists us as its parent, so a blanket `children(recursive=True)`
    terminate took the WhatsApp bridge and every launch profile down with
    each restart (seen live)."""
    from src.constants import BASE_DIR  # type: ignore
    return os.path.join(BASE_DIR, "data", "runtime", "detached.json")


def _note_detached(pid: int, created: Optional[float], command: str) -> None:
    path = detached_ledger_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            with open(path, encoding="utf-8") as fh:
                ledger = json.load(fh)
            if not isinstance(ledger, dict):
                ledger = {}
        except (OSError, ValueError):
            ledger = {}
        # prune what is gone (or was recycled into another process)
        for old_pid, rec in list(ledger.items()):
            try:
                if abs(process_ownership.creation_time(int(old_pid)) - float(rec.get("created") or 0)) > 0.01:
                    ledger.pop(old_pid, None)
            except Exception:  # noqa: BLE001
                ledger.pop(old_pid, None)
        ledger[str(pid)] = {"created": created, "command": command[:200]}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(ledger, fh)
    except Exception as exc:  # noqa: BLE001 - the ledger is a courtesy, never load-bearing
        logger.debug("detached ledger not updated: %s", exc)


def poll_readiness(check_ready, *, timeout_s: float, interval_s: float = 0.5) -> bool:
    """Call `check_ready()` (a zero-arg callable returning bool) every
    `interval_s` seconds until it returns True or `timeout_s` elapses.
    Never raises: an exception from `check_ready` counts as "not ready yet".
    """
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    while True:
        try:
            if check_ready():
                return True
        except Exception as exc:  # noqa: BLE001 - a probe failure is just "not ready"
            logger.debug("readiness probe raised: %s", exc)
        if time.monotonic() >= deadline:
            return False
        time.sleep(max(0.01, float(interval_s)))
