"""Which models are in memory, who loaded each one, how much it holds and on
which cards — plus every other process holding GPU memory.

The header gauge used to name one model (the first external runner, or the
first Ollama model) and nothing about its size, so two resident models looked
like one and 50 GB of VRAM had no owner. This module answers the question
from three sources the usage route already reads:

* the per-process GPU memory: ``nvidia-smi --query-compute-apps`` (bytes on
  Linux, only the card on Windows) and the WDDM ``GPU Process Memory``
  counters (dedicated bytes per process and adapter, Windows), with the
  adapter-to-card mapping of ``src/gpu_placement``;
* who listens on each runner endpoint's port (``src/process_center``), so a
  llama-server row gets its pid;
* the process tree, so "who loaded it" is a name a person recognises: this
  Faustus, a launch profile or engine started by Faustus, Ollama, or the
  program that started it (a script, a terminal) when it was started by hand.

Everything is best-effort and cached for a few seconds; a missing source
leaves a field empty (``bytes`` None means "not measured"), never a guess
presented as a measurement.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_MIB = 1024 * 1024
#: Processes holding less than this are driver contexts, browsers, the desktop
#: compositor... not something a person needs in the list.
OTHER_MIN_BYTES = 256 * _MIB
_TTL_S = 3.0
_lock = threading.Lock()
_cache: Dict[str, Any] = {"at": 0.0, "value": None}

_SHELLS = {"powershell", "pwsh", "cmd", "conhost", "bash", "sh", "zsh", "windowsterminal", "wt",
           "explorer", "python", "pythonw", "node", "uv", "py"}
_SCRIPT_EXT = (".ps1", ".bat", ".cmd", ".sh", ".py")


# ── per-process GPU memory ──────────────────────────────────────────────────

def process_gpu_bytes(gpus: List[Dict[str, Any]]) -> Dict[int, Dict[int, Optional[int]]]:
    """``{pid: {gpu index: bytes or None}}`` for every process on a card.

    nvidia-smi says which card a process uses (and how much, on Linux); the
    WDDM counters fill the bytes on Windows. A process only the counters see
    is still listed, on the card its adapter maps to."""
    from src import gpu_placement as gp

    ngpus = gp._norm_gpus(gpus)
    out: Dict[int, Dict[int, Optional[int]]] = {}
    apps = gp._compute_apps()
    for a in apps:
        idx = gp._gpu_index_for(a, ngpus)
        if idx is not None:
            out.setdefault(int(a["pid"]), {})[idx] = a.get("used_bytes")
    wddm = gp._wddm()
    if wddm:
        procs = wddm.get("processes") or []
        luid_to_idx = gp.map_luids(ngpus, apps, procs, wddm.get("adapters") or {})
        for r in procs:
            idx = luid_to_idx.get(str(r.get("luid")))
            dedicated = int(r.get("dedicated") or 0)
            if idx is None or dedicated <= 0:
                continue
            slot = out.setdefault(int(r["pid"]), {})
            if slot.get(idx) is None:
                slot[idx] = dedicated
    return out


def _total(per: Dict[int, Optional[int]]) -> Optional[int]:
    known = [b for b in per.values() if b is not None]
    return sum(known) if known else None


# ── who started a process ───────────────────────────────────────────────────

def _psutil():
    try:
        import psutil
        return psutil
    except Exception:  # noqa: BLE001
        return None


def _script_of(cmdline: List[str]) -> str:
    for tok in cmdline[1:]:
        low = tok.lower().strip('"')
        if low.endswith(_SCRIPT_EXT):
            return _basename(tok.strip('"'))
    return ""


def _basename(path: str) -> str:
    """Last part of a Windows or POSIX path, whichever this process runs on."""
    return re.split(r"[\\/]", path.rstrip("\\/"))[-1] if path else ""


def _dirname(path: str) -> str:
    parts = re.split(r"[\\/]", path.rstrip("\\/"))
    return parts[-2] if len(parts) >= 2 else ""


def _describe_origin(pid: int, *, self_pid: int, profiles: Dict[int, str]) -> Dict[str, str]:
    """``{"kind", "label"}`` for who started ``pid``.

    kind: ``faustus`` (this server or something it spawned), ``profile`` (a
    launch profile / engine Faustus started), ``ollama``, ``external`` (a
    program outside Faustus; the label names it), ``unknown``."""
    psutil = _psutil()
    if pid in profiles:
        return {"kind": "profile", "label": f"Faustus: {profiles[pid]}"}
    if psutil is None:
        return {"kind": "unknown", "label": ""}
    try:
        proc = psutil.Process(pid)
        chain = proc.parents()
    except Exception:  # noqa: BLE001 - gone or access denied
        return {"kind": "unknown", "label": ""}
    if pid == self_pid or any(p.pid == self_pid for p in chain):
        return {"kind": "faustus", "label": "Faustus"}
    for p in chain:
        try:
            if p.pid in profiles:
                return {"kind": "profile", "label": f"Faustus: {profiles[p.pid]}"}
            name = (p.name() or "").lower()
        except Exception:  # noqa: BLE001
            continue
        if name.startswith("ollama"):
            return {"kind": "ollama", "label": "Ollama"}
    # Started outside Faustus: name the nearest parent that says something
    # (a script beats the shell that ran it; a shell beats nothing).
    for p in chain:
        try:
            name = p.name() or ""
            base = os.path.splitext(name)[0].lower()
            script = _script_of(p.cmdline() or [])
        except Exception:  # noqa: BLE001
            continue
        if script:
            return {"kind": "external", "label": f"{script} ({name})"}
        if base not in _SHELLS and base not in ("services", "svchost", "wininit", "system", "init", "systemd"):
            return {"kind": "external", "label": name}
    if chain:
        try:
            return {"kind": "external", "label": chain[0].name() or ""}
        except Exception:  # noqa: BLE001
            pass
    return {"kind": "external", "label": "started outside Faustus (its parent process has ended)"}


_INTERPRETERS = ("python", "pythonw", "node", "java", "javaw", "ruby", "deno", "bun")
#: Per-card bytes below this are a driver context on a card the process does
#: not really use; they are left out of the card list.
CARD_MIN_BYTES = 64 * _MIB


def _name_of(psutil, pid: int) -> str:
    """The process name, also for processes `Process(pid).name()` refuses
    (protected system processes): the process list still names them."""
    try:
        return psutil.Process(pid).name() or ""
    except Exception:  # noqa: BLE001
        pass
    try:
        for proc in psutil.process_iter(["pid", "name"]):
            if proc.info.get("pid") == pid:
                return proc.info.get("name") or ""
    except Exception:  # noqa: BLE001
        pass
    return ""


def _process_label(pid: int) -> Dict[str, str]:
    """Name and, for an interpreter, what it runs: the script with its folder
    (``ComfyUI/main.py``), so two ``python.exe`` rows can be told apart."""
    psutil = _psutil()
    if psutil is None:
        return {"name": "", "hint": ""}
    name = _name_of(psutil, pid)
    base = os.path.splitext(name)[0].lower()
    if base not in _INTERPRETERS:
        return {"name": name, "hint": ""}
    try:
        proc = psutil.Process(pid)
        cmd = proc.cmdline() or []
    except Exception:  # noqa: BLE001
        return {"name": name, "hint": ""}
    hint = _script_of(cmd)
    folder = ""
    for tok in cmd[1:]:
        if hint and tok.strip('"').endswith(hint):
            folder = _dirname(tok.strip('"'))
            break
    if not folder:
        try:
            folder = _basename(proc.cwd() or "")
        except Exception:  # noqa: BLE001
            folder = ""
    if hint and folder:
        hint = f"{folder}/{hint}"
    elif not hint:
        hint = folder
    return {"name": name, "hint": hint}


# ── the list ────────────────────────────────────────────────────────────────

def _port_of(root: str) -> Optional[int]:
    try:
        u = urlparse(root)
        if u.port:
            return int(u.port)
        return 443 if u.scheme == "https" else 80
    except Exception:  # noqa: BLE001
        return None


def _per_gpu_rows(per: Dict[int, Optional[int]]) -> List[Dict[str, Any]]:
    """Cards the process really uses: unknown bytes are kept (nvidia-smi saw
    it there), a few KB of driver context on another card are not."""
    return [{"index": int(i), "bytes": per[i]} for i in sorted(per)
            if per[i] is None or per[i] >= CARD_MIN_BYTES]


def build(ollama: Dict[str, Any], external_runners: List[Dict[str, Any]], gpus: List[Dict[str, Any]],
          *, per_pid: Optional[Dict[int, Dict[int, Optional[int]]]] = None,
          listening: Optional[Dict[int, int]] = None, self_pid: Optional[int] = None,
          profiles: Optional[Dict[int, str]] = None, origin=None, label=None) -> Dict[str, Any]:
    """``{"models": [...], "others": [...]}``.

    Each model: ``model``, ``engine`` (ollama | llama.cpp), ``loaded_by`` and
    ``loaded_by_kind``, ``endpoint``, ``port``, ``pid``, ``bytes`` (memory it
    holds, measured when ``measured``), ``weights_bytes``, ``gpus`` (per-card
    bytes), ``context_length``, ``generating``, ``expires_at``. Each other
    process: ``pid``, ``name``, ``hint``, ``loaded_by``, ``bytes``, ``gpus``.
    The injectable arguments are for tests; left out, they are read live."""
    per_pid = per_pid if per_pid is not None else (process_gpu_bytes(gpus) if gpus else {})
    self_pid = self_pid if self_pid is not None else os.getpid()
    if profiles is None:
        try:
            from src import process_center
            profiles = process_center._launch_profile_pids()
        except Exception:  # noqa: BLE001
            profiles = {}
    origin = origin or (lambda pid: _describe_origin(pid, self_pid=self_pid, profiles=profiles or {}))
    label = label or _process_label
    claimed: Set[int] = set()
    models: List[Dict[str, Any]] = []

    for m in (ollama or {}).get("models") or []:
        pid = m.get("pid")
        per = {int(p["index"]): p.get("bytes") for p in (m.get("per_gpu") or []) if p.get("index") is not None}
        if pid is not None:
            claimed.add(int(pid))
            for idx, b in (per_pid.get(int(pid)) or {}).items():
                if per.get(idx) is None:
                    per[idx] = b
        vram = int(m.get("size_vram") or 0) or None
        models.append({
            "model": m.get("name") or "",
            "engine": "ollama",
            "loaded_by": "Ollama",
            "loaded_by_kind": "ollama",
            "endpoint": (ollama or {}).get("base") or "",
            "port": _port_of((ollama or {}).get("base") or ""),
            "pid": pid,
            "bytes": vram,
            "measured": vram is not None,
            "weights_bytes": int(m.get("size") or 0) or None,
            "gpus": _per_gpu_rows(per),
            "gpu_pct": m.get("gpu_pct"),
            "context_length": m.get("context_length"),
            "generating": None,
            "expires_at": m.get("expires_at"),
        })

    for r in external_runners or []:
        root = str(r.get("root") or "")
        port = _port_of(root) if root else None
        pid: Optional[int] = None
        if port is not None:
            if listening is not None:
                pid = listening.get(port)
            elif r.get("same_machine", True):
                try:
                    from src import process_center
                    hit = process_center.pid_listening_on(port, ports_by_pid=process_center._ports_by_pid_cached())
                    pid = int(hit["pid"]) if hit else None
                except Exception:  # noqa: BLE001
                    pid = None
        per = dict(per_pid.get(pid) or {}) if pid is not None else {}
        measured = _total(per)
        who = origin(pid) if pid is not None else {"kind": "unknown", "label": ""}
        if pid is not None:
            claimed.add(pid)
        weights = r.get("footprint_bytes")
        models.append({
            "model": r.get("model") or "",
            "engine": r.get("engine") or "llama.cpp",
            "loaded_by": who.get("label") or "",
            "loaded_by_kind": who.get("kind") or "unknown",
            "endpoint": r.get("endpoint_name") or root,
            "port": port,
            "pid": pid,
            "bytes": measured if measured is not None else weights,
            "measured": measured is not None,
            "weights_bytes": weights,
            "gpus": _per_gpu_rows(per),
            "gpu_pct": None,
            "context_length": r.get("context_length"),
            "generating": r.get("generating"),
            "expires_at": None,
        })

    others: List[Dict[str, Any]] = []
    for pid, per in per_pid.items():
        if pid in claimed:
            continue
        total = _total(per)
        if total is None or total < OTHER_MIN_BYTES:
            continue
        info = label(pid)
        who = origin(pid)
        others.append({
            "pid": pid, "name": info.get("name") or "", "hint": info.get("hint") or "",
            "loaded_by": who.get("label") or "", "loaded_by_kind": who.get("kind") or "unknown",
            "bytes": total, "gpus": _per_gpu_rows(per),
        })

    models.sort(key=lambda x: -(x.get("bytes") or 0))
    others.sort(key=lambda x: -(x.get("bytes") or 0))
    return {"models": models, "others": others}


def snapshot(ollama: Dict[str, Any], external_runners: List[Dict[str, Any]],
             gpus: List[Dict[str, Any]]) -> Dict[str, Any]:
    """`build` over the live system, cached for a few seconds; never raises."""
    now = time.monotonic()
    with _lock:
        if _cache["value"] is not None and now - _cache["at"] < _TTL_S:
            return _cache["value"]
    try:
        value = build(ollama, external_runners, gpus)
    except Exception as exc:  # noqa: BLE001 - a gauge never breaks the gauges
        logger.debug("model residency failed: %s", exc)
        value = {"models": [], "others": [], "error": str(exc)[:200]}
    with _lock:
        _cache.update({"at": time.monotonic(), "value": value})
    return value


def reset_cache() -> None:
    with _lock:
        _cache.update({"at": 0.0, "value": None})
