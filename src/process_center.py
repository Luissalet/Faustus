"""src/process_center.py — what is running on this machine because of
Faustus (or of the assistant driving it), and how to stop it.

The complaint behind it (17-09): the agent, Cursor, ChatGPT, a `node
server/index.js` started from a launch profile, a bg job, an MCP server —
they open ports and keep running in the background, invisible from the
Studio, and there is nowhere to stop them. This module is the inventory
half of the "control center":

* `snapshot()` lists every **listening port** with the process behind it,
  every **background job** the agent launched (`src/bg_jobs.py`), every
  **launch profile** this server started (`src/launch_profiles.py`), the
  MCP server children, and the **watched apps** — process families whose
  executable name is on the watchlist (Cursor, ChatGPT, Code, node, python,
  ollama…) — with pid, memory and how long they have been running.
* Every row carries an **origin**: `faustus` (a descendant of this server —
  the agent's shell, an MCP server, a launched profile), `bg_job`,
  `launch_profile`, `connector` (the app a connector points at), `self`
  (this server and whatever it must not kill), `ollama`, or `other`.
* `stop(pid, created_at)` kills one process tree. The caller is a **person**
  (the route is `require_human`; there is no agent tool for it — an
  assistant that can kill anything on the box is the problem, not the fix)
  who picked the row by name, and `created_at` is the creation time the
  listing showed: `process_ownership.terminate_tree` refuses the kill when
  the pid was recycled since, and prunes descendants that cannot be the
  tree's own. Protected rows (this server, its ancestors — the desktop app
  that owns 7000 —, Ollama, the OS) are refused unless `allow_protected`
  is set explicitly, and the OS itself never.

psutil is required for the process side; without it `snapshot()` still
returns the ports (netstat) and the jobs, with `available: False`.
"""
from __future__ import annotations

import logging
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from src import process_ownership

logger = logging.getLogger(__name__)

#: Executable names (lower-case, without extension) worth showing even when
#: they hold no port: the apps the assistant opens and forgets.
DEFAULT_WATCHLIST: Tuple[str, ...] = (
    "cursor", "chatgpt", "code", "claude", "electron", "node", "python", "pythonw",
    "ollama", "ollama app", "llama-server", "llama.cpp", "lmstudio", "lm studio",
    "chrome", "msedge", "powershell", "pwsh", "cmd", "conhost", "uvicorn", "vite",
    "npm", "npx", "deno", "bun", "java", "dotnet", "docker", "com.docker.backend",
)

#: Never killed from here, whatever the flag says.
OS_PROCESSES: Set[str] = {
    "system", "system idle process", "registry", "smss", "csrss", "wininit", "winlogon",
    "services", "lsass", "svchost", "dwm", "explorer", "fontdrvhost", "sihost", "taskhostw",
    "runtimebroker", "searchhost", "startmenuexperiencehost", "ctfmon", "audiodg",
    "init", "systemd", "launchd", "kernel_task", "windowserver", "loginwindow",
}

OLLAMA_PORT = 11434


@dataclass
class ProcRow:
    pid: int
    name: str
    created_at: Optional[float]
    origin: str = "other"            # self | faustus | bg_job | launch_profile | connector | ollama | other
    label: str = ""                  # human description of the origin (profile name, job id, connector name)
    cmdline: str = ""
    cwd: str = ""
    exe: str = ""
    ports: List[int] = field(default_factory=list)
    rss_mb: Optional[float] = None
    children: int = 0
    protected: bool = False
    protected_reason: str = ""
    uptime_s: Optional[int] = None
    parent_pid: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _psutil():
    try:
        import psutil  # type: ignore
        return psutil
    except Exception:  # noqa: BLE001
        return None


def _base_name(name: str) -> str:
    """`Cursor.exe` → `cursor`, `python3.11` → `python` (version suffixes
    dropped so the watchlist matches every interpreter)."""
    n = (name or "").lower()
    for ext in (".exe", ".app"):
        if n.endswith(ext):
            n = n[: -len(ext)]
    return re.sub(r"[\d.]+$", "", n) or n


def watchlist() -> Tuple[str, ...]:
    """The watchlist, extended by the `process_center_watch` setting
    (comma-separated names) when settings are reachable."""
    extra: List[str] = []
    try:
        from src.settings import get_setting  # type: ignore
        raw = str(get_setting("process_center_watch", "") or "")
        extra = [x.strip().lower() for x in raw.split(",") if x.strip()]
    except Exception:  # noqa: BLE001
        pass
    return tuple(dict.fromkeys(list(DEFAULT_WATCHLIST) + extra))


# ---------------------------------------------------------------------------
# attribution
# ---------------------------------------------------------------------------

def _ancestors(psutil, pid: int) -> List[int]:
    out: List[int] = []
    try:
        p = psutil.Process(pid)
        for parent in p.parents():
            out.append(parent.pid)
    except Exception:  # noqa: BLE001
        pass
    return out


def _bg_job_pids() -> Dict[int, str]:
    try:
        from src import bg_jobs
        jobs = bg_jobs.refresh()
    except Exception:  # noqa: BLE001
        return {}
    out: Dict[int, str] = {}
    for jid, rec in (jobs or {}).items():
        if rec.get("status") == "running" and rec.get("pid"):
            try:
                out[int(rec["pid"])] = str(jid)
            except (TypeError, ValueError):
                continue
    return out


def _launch_profile_pids() -> Dict[int, str]:
    try:
        from src import launch_profiles
        own = getattr(launch_profiles, "_own_launches", {}) or {}
        names = {p["id"]: p.get("name", p["id"]) for p in launch_profiles.list_profiles()}
    except Exception:  # noqa: BLE001
        return {}
    out: Dict[int, str] = {}
    for profile_id, rec in own.items():
        pid = rec.get("pid")
        if pid:
            out[int(pid)] = names.get(profile_id, profile_id)
    return out


def _connector_ports(connectors: Optional[Iterable[Dict[str, Any]]]) -> Dict[int, str]:
    """port -> connector name, from the sidecar entries the route passes in."""
    out: Dict[int, str] = {}
    from urllib.parse import urlsplit
    for entry in connectors or ():
        url = str(entry.get("app_url") or (entry.get("values") or {}).get("APP_URL") or "")
        try:
            port = int(urlsplit(url).port or 0)
        except Exception:  # noqa: BLE001
            port = 0
        if port:
            out[port] = str(entry.get("name") or entry.get("id") or "")
    return out


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------

def _ports_by_pid() -> Dict[int, List[int]]:
    from src.connector_discovery import listening_ports
    out: Dict[int, List[int]] = {}
    for lp in listening_ports():
        if lp.pid:
            out.setdefault(int(lp.pid), []).append(lp.port)
    return out


class _Table:
    """One pass over the process list (`process_iter` with pid/ppid/name):
    the parent map every attribution question is answered from. On Windows
    `Process.children(recursive=True)` and `parents()` each rescan the whole
    table, and a 45-port / 90-app snapshot took 20 s that way (17-09)."""

    def __init__(self, psutil):
        self.ppid: Dict[int, int] = {}
        self.name: Dict[int, str] = {}
        self.kids: Dict[int, List[int]] = {}
        for proc in psutil.process_iter(["pid", "ppid", "name"]):
            info = proc.info
            pid, ppid = info.get("pid"), info.get("ppid")
            if pid is None:
                continue
            self.name[pid] = info.get("name") or ""
            if ppid is not None:
                self.ppid[pid] = ppid
                self.kids.setdefault(ppid, []).append(pid)

    def ancestors(self, pid: int) -> List[int]:
        out: List[int] = []
        seen = {pid}
        while pid in self.ppid and self.ppid[pid] not in seen:
            pid = self.ppid[pid]
            seen.add(pid)
            out.append(pid)
        return out

    def descendants(self, pid: int) -> List[int]:
        out: List[int] = []
        frontier = list(self.kids.get(pid, []))
        seen = {pid}
        while frontier:
            p = frontier.pop()
            if p in seen:
                continue
            seen.add(p)
            out.append(p)
            frontier.extend(self.kids.get(p, []))
        return out


def _row_for(psutil, proc, *, table: _Table, self_pid: int, self_ancestors: Set[int],
             ports: Dict[int, List[int]], jobs: Dict[int, str], profiles: Dict[int, str],
             conn_ports: Dict[int, str], now: float, detail: bool = True) -> Optional[ProcRow]:
    try:
        with proc.oneshot():
            name = proc.name()
            created = float(proc.create_time())
            row = ProcRow(pid=proc.pid, name=name, created_at=created)
            base = _base_name(name)
            system = base in OS_PROCESSES or proc.pid <= 4
            if detail and not system:
                try:
                    row.cmdline = " ".join(proc.cmdline() or [])[:400]
                except Exception:  # noqa: BLE001
                    pass
                try:
                    row.cwd = proc.cwd() or ""
                except Exception:  # noqa: BLE001
                    pass
                try:
                    row.exe = proc.exe() or ""
                except Exception:  # noqa: BLE001
                    pass
            try:
                row.rss_mb = round(proc.memory_info().rss / (1024 * 1024), 1)
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001 - gone / access denied
        return None
    row.parent_pid = table.ppid.get(proc.pid)
    row.children = len(table.descendants(proc.pid))
    row.uptime_s = int(max(0.0, now - created))
    row.ports = sorted(ports.get(proc.pid, []))
    base = _base_name(name)

    if proc.pid == self_pid or proc.pid in self_ancestors:
        row.origin = "self"
        row.label = "This Faustus server" if proc.pid == self_pid else "Owner of this server (desktop app / launcher)"
        row.protected, row.protected_reason = True, "stopping it would stop Faustus itself"
    elif base in OS_PROCESSES or proc.pid <= 4:
        row.origin = "other"
        row.protected, row.protected_reason = True, "operating system process"
    elif proc.pid in jobs:
        row.origin, row.label = "bg_job", f"background job {jobs[proc.pid]}"
    elif proc.pid in profiles:
        row.origin, row.label = "launch_profile", f"launch profile: {profiles[proc.pid]}"
    elif any(p in conn_ports for p in row.ports):
        row.origin = "connector"
        row.label = "app of connector: " + ", ".join(conn_ports[p] for p in row.ports if p in conn_ports)
    elif base.startswith("ollama") or OLLAMA_PORT in row.ports:
        row.origin = "ollama"
        row.label = "Ollama (local models)"
        row.protected, row.protected_reason = True, "the local model runtime; stop it only on purpose"
    elif self_pid in table.ancestors(proc.pid):
        row.origin = "faustus"
        row.label = "started by Faustus (agent shell, MCP server or tool)"
    return row


def snapshot(*, connectors: Optional[Iterable[Dict[str, Any]]] = None,
             include_watched: bool = True) -> Dict[str, Any]:
    """Everything worth seeing, in one object:

    ``{"available", "ports": [...rows with ports...], "faustus": [...children
    of this server...], "watched": [...watchlist families...], "jobs": [...bg
    job records...], "generated_at"}``.

    Rows are `ProcRow.to_dict()`; a pid appears in one list only (ports first,
    then faustus, then watched). psutil missing → `available: False`, ports
    from netstat without process detail, jobs still listed."""
    now = time.time()
    psutil = _psutil()
    jobs_map = _bg_job_pids()
    profiles = _launch_profile_pids()
    conn_ports = _connector_ports(connectors)
    out: Dict[str, Any] = {"available": psutil is not None, "ports": [], "faustus": [], "watched": [],
                           "jobs": _job_records(), "generated_at": now}
    if psutil is None:
        from src.connector_discovery import listening_ports
        out["ports"] = [ProcRow(pid=lp.pid or 0, name=lp.process, created_at=None, ports=[lp.port],
                                cmdline=lp.cmdline, cwd=lp.cwd).to_dict() for lp in listening_ports()]
        return out

    self_pid = os.getpid()
    table = _Table(psutil)
    self_anc = set(table.ancestors(self_pid))
    ports = _ports_by_pid()
    seen: Set[int] = set()
    kw = dict(table=table, self_pid=self_pid, self_ancestors=self_anc, ports=ports, jobs=jobs_map,
              profiles=profiles, conn_ports=conn_ports, now=now)

    # 1. whoever holds a listening port
    for pid in sorted(ports):
        try:
            row = _row_for(psutil, psutil.Process(pid), **kw)
        except Exception:  # noqa: BLE001
            row = None
        if row is None:
            row = ProcRow(pid=pid, name="?", created_at=None, ports=sorted(ports[pid]),
                          protected=True, protected_reason="not visible to this user")
        out["ports"].append(row.to_dict())
        seen.add(pid)

    # 2. this server's own descendants (agent shells, MCP servers, tools)
    try:
        for child_pid in table.descendants(self_pid):
            if child_pid in seen:
                continue
            try:
                row = _row_for(psutil, psutil.Process(child_pid), **kw)
            except Exception:  # noqa: BLE001
                row = None
            if row is None:
                continue
            if row.origin == "other":
                row.origin, row.label = "faustus", "started by Faustus (agent shell, MCP server or tool)"
            out["faustus"].append(row.to_dict())
            seen.add(child_pid)
    except Exception:  # noqa: BLE001
        pass

    # 3. watched app families: the top-most process of each name
    if include_watched:
        names = set(watchlist())
        candidates: Dict[int, str] = {}
        for pid, name in table.name.items():
            base = _base_name(name)
            if base in names and pid not in seen and pid != self_pid:
                candidates[pid] = base
        for pid, base in candidates.items():
            if pid in seen:
                continue
            ppid = table.ppid.get(pid)
            # family root: parent is not the same app (Electron/Chrome spawn many)
            if ppid in candidates and candidates[ppid] == base:
                continue
            try:
                row = _row_for(psutil, psutil.Process(pid), **kw)
            except Exception:  # noqa: BLE001
                row = None
            if row is None:
                continue
            # a family's memory is the sum of its members, so Cursor reads
            # as one app and not as its 14 renderers
            fam = [p for p in table.descendants(pid) if candidates.get(p) == base]
            for p in fam:
                seen.add(p)
                try:
                    row.rss_mb = round((row.rss_mb or 0) + psutil.Process(p).memory_info().rss / (1024 * 1024), 1)
                except Exception:  # noqa: BLE001
                    pass
            out["watched"].append(row.to_dict())
            seen.add(pid)
        out["watched"].sort(key=lambda r: (r["origin"] != "faustus", r["name"].lower(), r["pid"]))
    return out


def _job_records() -> List[Dict[str, Any]]:
    try:
        from src import bg_jobs
        jobs = bg_jobs.refresh()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for jid, rec in (jobs or {}).items():
        if rec.get("status") != "running":
            continue
        out.append({"id": jid, "pid": rec.get("pid"), "command": str(rec.get("command") or "")[:200],
                    "cwd": rec.get("cwd") or "", "session_id": rec.get("session_id") or "",
                    "started_at": rec.get("started_at") or rec.get("created_at")})
    return out


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------

def stop(pid: int, created_at: Optional[float], *, allow_protected: bool = False) -> Dict[str, Any]:
    """Kill `pid` and its verifiable descendants. `created_at` must be the
    creation time the listing showed for that pid (proof it is the same
    process). Returns ``{"ok", "code", "reason", "signalled", "refused"}``."""
    psutil = _psutil()
    if psutil is None:
        return {"ok": False, "code": "unavailable", "reason": "psutil is not installed"}
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return {"ok": False, "code": "bad_pid", "reason": "no pid"}
    try:
        proc = psutil.Process(pid)
        name = proc.name()
        actual_created = float(proc.create_time())
    except Exception:  # noqa: BLE001
        return {"ok": True, "code": "gone", "reason": "already gone", "signalled": [], "refused": []}
    base = _base_name(name)
    self_pid = os.getpid()
    if pid == self_pid or pid in _ancestors(psutil, self_pid):
        return {"ok": False, "code": "self", "reason": "that is Faustus itself (or the process that owns it); use the server controls instead"}
    if base in OS_PROCESSES or pid <= 4:
        return {"ok": False, "code": "os", "reason": f"{name} is an operating-system process"}
    if created_at is None:
        return {"ok": False, "code": "no_proof", "reason": "the listing's creation time is required to prove it is the same process"}
    if abs(actual_created - float(created_at)) > 1.0:
        return {"ok": False, "code": "recycled", "reason": "the pid now belongs to a different process; refresh the list"}
    if (base.startswith("ollama") or OLLAMA_PORT in _ports_by_pid().get(pid, [])) and not allow_protected:
        return {"ok": False, "code": "protected", "reason": "Ollama is protected; confirm to stop it anyway"}
    # bg jobs: go through their own bookkeeping so the record is marked killed
    for jid, jpid in _bg_job_pids().items():
        if jpid == pid:
            try:
                from src import bg_jobs
                rec = bg_jobs.kill(jid) or {}
                refused = rec.get("kill_refused")
                return {"ok": not refused, "code": "" if not refused else "refused", "reason": refused or "",
                        "signalled": [pid], "refused": [], "bg_job": jid}
            except Exception as exc:  # noqa: BLE001
                logger.warning("[process-center] bg job kill failed: %s", exc)
    outcome = process_ownership.terminate_tree(pid, spawned_at=float(created_at))
    result = {"ok": outcome.owned, "code": outcome.code, "reason": outcome.reason,
              "signalled": list(outcome.signalled), "refused": [list(r) for r in outcome.rejected], "name": name}
    if outcome.owned:
        try:
            from src import launch_profiles
            own = getattr(launch_profiles, "_own_launches", {}) or {}
            for prof_id, rec in list(own.items()):
                if rec.get("pid") == pid:
                    own.pop(prof_id, None)
        except Exception:  # noqa: BLE001
            pass
        logger.info("[process-center] stopped %s (pid %s), %d descendant(s)", name, pid,
                    max(0, len(outcome.signalled) - 1))
    return result


def stop_port(port: int, *, allow_protected: bool = False) -> Dict[str, Any]:
    """Stop whatever listens on `port` (looked up now, with its creation time)."""
    psutil = _psutil()
    for pid, ports in _ports_by_pid().items():
        if port in ports:
            created = None
            if psutil is not None:
                try:
                    created = float(psutil.Process(pid).create_time())
                except Exception:  # noqa: BLE001
                    created = None
            out = stop(pid, created, allow_protected=allow_protected)
            out["port"] = port
            return out
    return {"ok": True, "code": "gone", "reason": f"nothing is listening on {port}", "port": port}
