"""crash_recovery.py — what was running when the machine went down.

A power cut does not close files, write a status line or run a `finally`. What
it leaves behind is a set of records that all **stopped being written at the
same instant**, because the processes holding them died together. That is the
only signal there is, and it is the one this module reads.

Two rules from the method this follows, and they matter more than the code:

* **group by mtime only.** Not by owner, not by status, not by folder. The
  thing that identifies a crash is simultaneity.
* **group first, filter after.** Filtering the records down to "the ones that
  look interrupted" and *then* grouping them displaces the real cluster: the
  neighbour that proves the simultaneity is often a record that finished
  cleanly a second earlier, and dropping it first splits one tight pocket into
  two loose ones (there is a test that fails if this order is reversed).

What it reads: the dispatch mirrors under ``DATA_DIR/dispatch/`` and the
detached-run logs under ``DATA_DIR/runs/`` (src/agent_runs.py) left in a live
state — ``running``, ``verifying``, ``queued`` — by a process that is gone.

What it produces: a **plan**, never an action. `resume_plan` re-pins the model
and the parameters the job actually had, read from its own record; it never
re-derives them from today's defaults, because "resumed with the current
default model" is a different job wearing the old one's id. The stale jobs are
marked `interrupted` with the reason — a state src/dispatch.py already has —
and that is all that happens by itself.

`verify_resumed` is the last rule: **probe the process table before declaring
success**. A call that returned is not a process that is running.

Boot time comes from ``/proc/stat btime`` (Linux), ``GetTickCount64``
(Windows) or ``kern.boottime`` (macOS). When it cannot be determined the whole
feature reports "unknown boot time" and does nothing — it never guesses, because
a guessed boot time silently re-times the window and turns unrelated files into
a "crash".

Stdlib only, and every entry point is total: a boot-time scan must never delay
or break startup.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

#: Record states that mean "this was still going when the writing stopped".
LIVE_STATES = frozenset({"running", "verifying", "queued", "cancelling"})

#: Records whose mtimes are within this of each other belong to one pocket
#: (single linkage: the group grows while the next record is close enough).
DEFAULT_GAP_S = 120.0
#: A pocket this tight is "the same instant" for the confidence rule.
TIGHT_S = 60.0
#: How many records in a tight pre-boot pocket earn `high`.
HIGH_MEMBERS = 3

DEFAULT_LOOKBACK_S = 3600.0
DEFAULT_SLACK_S = 300.0

_MAX_MIRROR_BYTES = 4 * 1024 * 1024
_TAIL_BYTES = 64 * 1024

Cluster = Dict[str, Any]


def enabled() -> bool:
    """Setting ``agent_crash_recovery``. Off = nothing scans at startup and
    nothing is marked, exactly as before this module existed."""
    try:
        from src.settings import get_setting
        return bool(get_setting("agent_crash_recovery", True))
    except Exception:  # noqa: BLE001 - never raise into startup
        return True


# ── boot time: measured or unknown, never guessed ───────────────────────────

def _linux_boot_time() -> Optional[float]:
    try:
        with open("/proc/stat", "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("btime"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return float(parts[1])
    except (OSError, ValueError):
        return None
    return None


def _windows_boot_time() -> Optional[float]:
    try:
        import ctypes
        ticks = ctypes.windll.kernel32.GetTickCount64()   # type: ignore[attr-defined]
        ms = float(ticks)
        if ms <= 0:
            return None
        return time.time() - ms / 1000.0
    except Exception:  # noqa: BLE001 - not Windows, or the call is unavailable
        return None


def _darwin_boot_time() -> Optional[float]:
    try:
        import ctypes
        import ctypes.util

        class _Timeval(ctypes.Structure):
            _fields_ = [("tv_sec", ctypes.c_long), ("tv_usec", ctypes.c_long)]

        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.dylib", use_errno=True)
        tv = _Timeval()
        size = ctypes.c_size_t(ctypes.sizeof(tv))
        if libc.sysctlbyname(b"kern.boottime", ctypes.byref(tv), ctypes.byref(size), None, 0) != 0:
            return None
        return float(tv.tv_sec) or None
    except Exception:  # noqa: BLE001
        return None


def boot_time() -> Optional[float]:
    """When this machine last booted, as a Unix timestamp — or None when the
    platform will not say. None means the feature does nothing."""
    try:
        if sys.platform.startswith("linux"):
            return _linux_boot_time()
        if os.name == "nt":
            return _windows_boot_time()
        if sys.platform == "darwin":
            return _darwin_boot_time()
        # Anything else: try the readable ones rather than inventing a number.
        return _linux_boot_time()
    except Exception as e:  # noqa: BLE001
        logger.debug("crash_recovery: boot time unavailable: %s", e)
        return None


# ── the process table ───────────────────────────────────────────────────────

def pid_alive(pid: Any) -> Optional[bool]:
    """True / False / None ("cannot tell"). POSIX signal 0, Windows
    OpenProcess. Nothing here ever claims a process is alive because a call
    returned successfully somewhere else."""
    try:
        n = int(pid)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    if os.name == "nt":
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32           # type: ignore[attr-defined]
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, n)
            if not handle:
                # 5 = access denied: it exists, it is simply not ours.
                return True if ctypes.get_last_error() == 5 else False
            try:
                code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return None
                return code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001
            return None
    try:
        os.kill(n, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                                     # exists, owned by somebody else
    except OSError:
        return None


# ── the records on disk ─────────────────────────────────────────────────────

def _mtime(path: str) -> Optional[float]:
    try:
        return float(os.path.getmtime(path))
    except OSError:
        return None


def _read_dispatch_mirror(path: str) -> Optional[Dict[str, Any]]:
    try:
        if os.path.getsize(path) > _MAX_MIRROR_BYTES:
            return None
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict):
        return None
    status = str(d.get("status") or "").strip().lower()
    return {
        "kind": "dispatch",
        "path": path,
        "id": str(d.get("id") or os.path.basename(path)[:-5]),
        "status": status,
        "live": status in LIVE_STATES,
        "workspace": d.get("workspace"),
        "model": d.get("model") or None,
        "session_id": d.get("session_id"),
        "title": d.get("title") or "",
        "pid": d.get("pid"),
        "params": {
            "tasks": d.get("tasks") or [],
            "parallel": d.get("parallel"),
            "reviewer": d.get("reviewer"),
            "max_rounds": d.get("max_rounds"),
            "timeout_s": d.get("timeout_s"),
            "verify": d.get("verify"),
            "verify_scope": d.get("verify_scope"),
            "fix_rounds": d.get("fix_rounds"),
        },
    }


def _read_run_log(path: str) -> Optional[Dict[str, Any]]:
    """The head line (the run's own metadata) and the LAST status line, without
    reading a long replay log end to end."""
    meta: Dict[str, Any] = {}
    status = ""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            head = fh.readline(_TAIL_BYTES)
            try:
                obj = json.loads(head.decode("utf-8", "replace"))
                if isinstance(obj, dict):
                    meta = obj
                    status = str(obj.get("status") or "")
            except ValueError:
                meta = {}
            if size > _TAIL_BYTES:
                fh.seek(size - _TAIL_BYTES)
                tail = fh.read(_TAIL_BYTES)
                lines = tail.split(b"\n")[1:]           # the first one may be a fragment
            else:
                fh.seek(0)
                lines = fh.read().split(b"\n")
        for raw in reversed(lines):
            if b'"status"' not in raw:
                continue
            try:
                obj = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            if isinstance(obj, dict) and obj.get("status"):
                status = str(obj["status"])
                break
    except (OSError, ValueError):
        return None
    status = status.strip().lower()
    sid = str(meta.get("session_id") or os.path.basename(path)[:-6])
    return {
        "kind": "run",
        "path": path,
        "id": sid,
        "status": status,
        "live": status in LIVE_STATES,
        "workspace": None,
        # A replay log does not record the model. It is left None on purpose:
        # a resume that filled it from today's default would be a different
        # job wearing this one's id.
        "model": None,
        "session_id": sid,
        "title": str(meta.get("label") or ""),
        "pid": meta.get("pid"),
        "params": {"run_id": meta.get("run_id"), "lane": meta.get("lane"), "label": meta.get("label"),
                   "started_ts": meta.get("ts")},
    }


def scan_records(data_dir: Any) -> List[Dict[str, Any]]:
    """Every record this module knows how to read, with its mtime. NOTHING is
    filtered here — the grouping needs the neighbours that finished cleanly."""
    rows: List[Dict[str, Any]] = []
    root = str(data_dir or "")
    if not root:
        return rows
    for sub, suffix, reader in (("dispatch", ".json", _read_dispatch_mirror),
                                ("runs", ".jsonl", _read_run_log)):
        folder = os.path.join(root, sub)
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            continue
        for name in names:
            if not name.endswith(suffix) or name.startswith("."):
                continue
            path = os.path.join(folder, name)
            ts = _mtime(path)
            if ts is None:
                continue
            try:
                rec = reader(path)
            except Exception as e:  # noqa: BLE001 - one bad file never stops the scan
                logger.debug("crash_recovery: could not read %s: %s", path, e)
                rec = None
            if not rec:
                continue
            rec["mtime"] = ts
            rows.append(rec)
    return rows


# ── grouping: by mtime, before anything is filtered ─────────────────────────

def group_by_mtime(records: List[Dict[str, Any]], *, gap_s: float = DEFAULT_GAP_S) -> List[List[Dict[str, Any]]]:
    """Single-linkage grouping on mtime alone. Records that stopped being
    written within `gap_s` of each other are one pocket."""
    rows = sorted((r for r in records if isinstance(r.get("mtime"), (int, float))),
                  key=lambda r: float(r["mtime"]))
    groups: List[List[Dict[str, Any]]] = []
    for rec in rows:
        if groups and float(rec["mtime"]) - float(groups[-1][-1]["mtime"]) <= gap_s:
            groups[-1].append(rec)
        else:
            groups.append([rec])
    return groups


def _confidence(members: List[Dict[str, Any]], interrupted: List[Dict[str, Any]],
                bt: float) -> tuple[str, str]:
    span = float(members[-1]["mtime"]) - float(members[0]["mtime"])
    newest = float(members[-1]["mtime"])
    pre_boot = newest <= bt
    n = len(members)
    when = f"{n} record(s) in a {span:.0f} s pocket"
    if not pre_boot:
        return "low", (f"{when} but the newest one was written {newest - bt:.0f} s AFTER this boot — "
                       f"that is this process, not the one that died")
    if n >= HIGH_MEMBERS and span <= TIGHT_S:
        return "high", (f"{when} ending {bt - newest:.0f} s before boot: {n} records stopped being written "
                        f"at the same instant, which is what a power cut looks like")
    if n >= 2:
        return "medium", (f"{when} ending {bt - newest:.0f} s before boot — simultaneous, but "
                          f"{'too few records' if n < HIGH_MEMBERS else 'too spread out'} to be sure it was a crash")
    return "low", (f"a single record ({interrupted[0]['id'] if interrupted else members[0]['id']}) stopped "
                   f"{bt - newest:.0f} s before boot — one file proves nothing about how it stopped")


def _current_boot_time() -> Optional[float]:
    """`boot_time()` through the module namespace, so the keyword argument of
    `find_interrupted` can shadow the name without hiding the function (and so
    a test can replace it)."""
    return boot_time()


def find_interrupted(data_dir: Any, *, boot_time: Optional[float] = None,
                     lookback_s: float = DEFAULT_LOOKBACK_S, slack_s: float = DEFAULT_SLACK_S,
                     gap_s: float = DEFAULT_GAP_S) -> List[Cluster]:
    """Clusters of records that stopped being written together around the last
    boot, each carrying the ones left in a live state.

    The window is ``[boot - lookback_s, boot + slack_s]``. Without a boot time
    the answer is empty: this module does not guess when the machine came up.
    """
    try:
        bt = boot_time if boot_time is not None else _current_boot_time()
    except Exception:  # noqa: BLE001
        bt = None
    if bt is None:
        logger.debug("crash_recovery: unknown boot time — nothing is scanned")
        return []
    try:
        bt = float(bt)
    except (TypeError, ValueError):
        return []
    lo, hi = bt - float(lookback_s), bt + float(slack_s)
    try:
        records = scan_records(data_dir)
    except Exception as e:  # noqa: BLE001
        logger.debug("crash_recovery: scan failed: %s", e)
        return []
    # GROUP FIRST — every record, whatever its state — then filter.
    clusters: List[Cluster] = []
    for members in group_by_mtime(records, gap_s=gap_s):
        interrupted = [r for r in members if r.get("live") and lo <= float(r["mtime"]) <= hi]
        # A record whose recorded process is STILL ALIVE was not interrupted.
        alive = [r for r in interrupted if r.get("pid") is not None and pid_alive(r.get("pid")) is True]
        if alive:
            still = {id(r) for r in alive}
            interrupted = [r for r in interrupted if id(r) not in still]
        if not interrupted:
            continue
        confidence, reason = _confidence(members, interrupted, bt)
        clusters.append({
            "at": float(members[-1]["mtime"]),
            "span_s": round(float(members[-1]["mtime"]) - float(members[0]["mtime"]), 3),
            "boot_time": bt,
            "window": [lo, hi],
            "members": members,
            "interrupted": interrupted,
            "confidence": confidence,
            "reason": reason,
            "still_running": [r["id"] for r in alive],
        })
    clusters.sort(key=lambda c: c["at"], reverse=True)
    return clusters


# ── the plan (never an action) ──────────────────────────────────────────────

def resume_plan(cluster: Any) -> List[Dict[str, Any]]:
    """One entry per interrupted record: the SAME model and the SAME parameters
    the job had, read from its own record. Nothing here is filled in from the
    current defaults — a job resumed on today's default model is a different
    job, and saying otherwise is the lie this whole module exists to avoid."""
    out: List[Dict[str, Any]] = []
    try:
        rows = (cluster or {}).get("interrupted") or []
        confidence = str((cluster or {}).get("confidence") or "")
        at = (cluster or {}).get("at")
    except Exception:  # noqa: BLE001
        return out
    for rec in rows:
        if not isinstance(rec, dict):
            continue
        model = rec.get("model")
        why = [f"{rec.get('kind', 'record')} {rec.get('id')} was `{rec.get('status') or 'live'}` when writing "
               f"stopped" + (f" at {float(at):.0f}" if isinstance(at, (int, float)) else "")
               + (f" (cluster confidence {confidence})" if confidence else "")]
        if model:
            why.append(f"re-pin the model it ran on ({model}) and the parameters it was given, not the current defaults")
        else:
            why.append("its record does not name the model it ran on: it must be re-pinned by hand — "
                       "nothing here substitutes today's default")
        if rec.get("kind") == "run":
            why.append("a detached run cannot be continued from its log (the model state is gone); what it "
                       "produced is kept as a partial message by src/agent_runs.py")
        out.append({
            "job_id": rec.get("id"),
            "kind": rec.get("kind"),
            "workspace": rec.get("workspace"),
            "model": model,
            "params": dict(rec.get("params") or {}),
            "why": " · ".join(why),
        })
    return out


def verify_resumed(plan: Any, *, probe: Optional[Callable[[Any], Optional[bool]]] = None,
                   now: Optional[float] = None) -> Dict[str, Any]:
    """Did the resume actually start anything? The rule: **probe the process
    table**. An entry with no pid to probe is `not_probed`, never `resumed` —
    a call that returned is not a running process."""
    check = probe or pid_alive
    entries: List[Dict[str, Any]] = []
    rows = plan if isinstance(plan, list) else []
    for item in rows:
        if not isinstance(item, dict):
            continue
        pid = item.get("pid")
        if pid is None:
            entries.append({"job_id": item.get("job_id"), "pid": None, "alive": None, "verdict": "not_probed",
                            "why": "no process id was recorded for this entry — nothing was probed, so nothing "
                                   "is claimed"})
            continue
        try:
            alive = check(pid)
        except Exception as e:  # noqa: BLE001 - a probe that throws proves nothing
            entries.append({"job_id": item.get("job_id"), "pid": pid, "alive": None, "verdict": "not_probed",
                            "why": f"the process-table probe failed: {type(e).__name__}: {e}"[:200]})
            continue
        if alive is True:
            entries.append({"job_id": item.get("job_id"), "pid": pid, "alive": True, "verdict": "running",
                            "why": f"pid {pid} is in the process table"})
        elif alive is False:
            entries.append({"job_id": item.get("job_id"), "pid": pid, "alive": False, "verdict": "gone",
                            "why": f"pid {pid} is not in the process table — the resume did not survive"})
        else:
            entries.append({"job_id": item.get("job_id"), "pid": pid, "alive": None, "verdict": "not_probed",
                            "why": f"the process table could not be read for pid {pid}"})
    running = [e for e in entries if e["verdict"] == "running"]
    unverified = [e for e in entries if e["verdict"] != "running"]
    return {
        "ok": bool(entries) and not unverified,
        "checked": len(entries),
        "probed": len([e for e in entries if e["alive"] is not None]),
        "running": [e["job_id"] for e in running],
        "unverified": [e["job_id"] for e in unverified],
        "entries": entries,
        "at": time.time() if now is None else now,
        "summary": (f"{len(running)}/{len(entries)} resumed job(s) answer to the process table"
                    if entries else "nothing was resumed, so nothing is claimed"),
    }


# ── marking, and the boot-time scan ─────────────────────────────────────────

def mark_interrupted(cluster: Any, *, reason: str = "") -> List[str]:
    """Write `interrupted` (and the reason) into the dispatch mirrors of a
    cluster. `interrupted` is a state src/dispatch.py already has; a mirror it
    reads back in that state is reported, not resumed. Run logs are left alone
    — src/agent_runs.py owns those."""
    marked: List[str] = []
    try:
        rows = (cluster or {}).get("interrupted") or []
        why = reason or str((cluster or {}).get("reason") or "")
    except Exception:  # noqa: BLE001
        return marked
    for rec in rows:
        if not isinstance(rec, dict) or rec.get("kind") != "dispatch":
            continue
        path = str(rec.get("path") or "")
        if not path:
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                d = json.load(fh)
            if not isinstance(d, dict) or str(d.get("status") or "").lower() not in LIVE_STATES:
                continue                                  # somebody finished it in the meantime
            d["status"] = "interrupted"
            d["interrupted_reason"] = why[:400]
            if not d.get("verdict"):
                d["verdict"] = ("interrupted by a power cut or a hard stop — re-dispatch the remaining work "
                                "(nothing was resumed automatically)")
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(d, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
            marked.append(str(rec.get("id") or ""))
        except (OSError, ValueError) as e:
            logger.debug("crash_recovery: could not mark %s: %s", path, e)
    return marked


def _data_dir() -> str:
    try:
        from src.constants import DATA_DIR
        return str(DATA_DIR)
    except Exception:  # pragma: no cover
        return os.path.join(os.getcwd(), "data")


def boot_scan(data_dir: Any = None, *, mark: bool = True) -> Dict[str, Any]:
    """The once-at-startup pass. Reports what stopped together around the last
    boot, marks the stale dispatch jobs `interrupted` with the reason, and
    returns the plan it did NOT carry out.

    Total by contract: startup calls this inside its own guard, and this
    function additionally swallows everything — a boot scan is never a reason
    for Faustus not to come up.
    """
    report: Dict[str, Any] = {"ok": False, "reason": "", "boot_time": None, "clusters": [], "plan": [], "marked": []}
    try:
        if not enabled():
            report["reason"] = "disabled (agent_crash_recovery)"
            return report
        bt = boot_time()
        if bt is None:
            report["reason"] = "unknown boot time — this platform will not say when it came up, so nothing is scanned"
            return report
        report["boot_time"] = bt
        clusters = find_interrupted(data_dir if data_dir is not None else _data_dir(), boot_time=bt)
        report["ok"] = True
        if not clusters:
            report["reason"] = "nothing was left in a live state around the last boot"
            return report
        for c in clusters:
            plan = resume_plan(c)
            report["plan"].extend(plan)
            if mark:
                report["marked"].extend(mark_interrupted(c))
            report["clusters"].append({
                "at": c["at"], "span_s": c["span_s"], "confidence": c["confidence"], "reason": c["reason"],
                "members": len(c["members"]),
                "interrupted": [{"kind": r.get("kind"), "id": r.get("id"), "status": r.get("status")}
                                for r in c["interrupted"]],
            })
        report["reason"] = (f"{len(report['plan'])} job(s) in {len(clusters)} cluster(s) stopped around the last "
                            f"boot; a plan is ready and nothing was resumed")
        logger.warning("[crash-recovery] %s", report["reason"])
        return report
    except Exception as e:  # noqa: BLE001 - startup, always
        logger.debug("crash_recovery: boot scan failed: %s", e)
        report["reason"] = f"boot scan failed: {type(e).__name__}: {e}"[:200]
        return report
