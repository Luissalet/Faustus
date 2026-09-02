"""Which card(s) a loaded Ollama model lives on — and how many bytes on each.

`ollama ps` says how much of a model is on "the GPU"; with two cards in the
box that is not enough to know where it went. Ollama 0.33 (sched.go) puts a
model that fits one card on the card with the most free memory, pins it when
the request carries ``main_gpu``, and splits one that fits no single card
across all of them. This module reads the placement back, best-effort, from
what the system already exposes:

* ``nvidia-smi --query-compute-apps=gpu_uuid,gpu_bus_id,pid,process_name,
  used_memory`` lists every CUDA process **per GPU it touches** — a split
  model shows the same runner pid under both UUIDs. ``used_memory`` is the
  per-process bytes on Linux and ``[N/A]`` on Windows/WDDM.
* Each loaded model has its own runner (``llama-server --model <blob> …``),
  so the pid's command line names the blob; ``POST /api/show`` for a loaded
  model returns a modelfile whose ``FROM <blob>`` names the same file. When
  the two do not line up (a custom modelfile, an old runner) the fallback is
  size: the loaded model whose ``size_vram`` is closest to what the pid holds.
* On Windows the WDDM counters (src/gpu_shared_memory.py) give the dedicated
  bytes per pid AND per adapter luid. Luids are mapped to GPU indexes by a
  process nvidia-smi lists on exactly one GPU whose counters carry exactly
  one luid (exact), else by matching the adapter's dedicated total to
  nvidia-smi's ``memory.used`` (heuristic; skipped when two cards are within
  5 % of each other).

Everything is pure parsing over strings and dicts (unit-testable with
fixtures) around a thin, cached, never-raising collector. With nothing
loaded there is no subprocess and no network.
"""
from __future__ import annotations

import logging
import re
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from src import gpu_shared_memory as gsm

logger = logging.getLogger(__name__)

_MIB = 1024 * 1024
_CACHE_TTL = 2.0
_SHOW_TTL = 600.0
_SHOW_TIMEOUT = 2.5
_COMPUTE_APPS_FIELDS = ("gpu_uuid", "gpu_bus_id", "pid", "process_name", "used_memory")
# Two adapters whose dedicated totals are this close to a card's memory.used
# cannot be told apart by the heuristic.
_AMBIGUOUS_FRACTION = 0.05
# And a "closest" adapter that is still this far off is not a match at all
# (the two readings are taken a few ms apart; a model loading can move more
# than this, in which case we simply report no per-card bytes this round).
_MATCH_FRACTION = 0.2
_MATCH_SLACK = 256 * _MIB

_FROM_RE = re.compile(r"^\s*FROM\s+(.+?)\s*$", re.IGNORECASE)

_lock = threading.Lock()
_cache: Dict[str, Any] = {"ts": 0.0, "key": None, "data": None}
_show_cache: Dict[Tuple[str, str, str], Tuple[float, Optional[str]]] = {}


# ── pure parsers ────────────────────────────────────────────────────────────

def parse_compute_apps(csv_text: str) -> List[Dict[str, Any]]:
    """Rows of ``--query-compute-apps=gpu_uuid,gpu_bus_id,pid,process_name,
    used_memory`` (csv, noheader, nounits) →
    ``[{uuid, bus_id, pid, process, used_bytes|None}]``.

    ``used_memory`` is ``[N/A]`` on Windows (WDDM owns the accounting) and a
    number in MiB on Linux; a process name containing commas is kept whole.
    """
    rows: List[Dict[str, Any]] = []
    for line in (csv_text or "").splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < len(_COMPUTE_APPS_FIELDS):
            continue
        try:
            pid = int(parts[2])
        except ValueError:
            continue  # a header line, or garbage
        try:
            used_bytes: Optional[int] = int(float(parts[-1])) * _MIB
        except ValueError:
            used_bytes = None
        rows.append({
            "uuid": parts[0],
            "bus_id": parts[1],
            "pid": pid,
            "process": ", ".join(parts[3:-1]),
            "used_bytes": used_bytes,
        })
    return rows


def parse_modelfile_blob(modelfile_text: str) -> Optional[str]:
    """The path (or digest) after the first real ``FROM`` line of a modelfile.

    `ollama show` prefixes the modelfile with commented lines — one of them
    reads ``# FROM qwen3.5:9b`` — so comments are skipped, not matched.
    """
    for line in (modelfile_text or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = _FROM_RE.match(stripped)
        if m:
            return m.group(1).strip().strip('"').strip("'")
    return None


def blob_key(path: Optional[str]) -> str:
    """What two references to one blob have in common: the file name
    (``sha256-dec52…``), case-folded, whatever the directory or slash style.
    A ``sha256:<hex>`` digest folds to the same key as the file."""
    if not path:
        return ""
    name = re.split(r"[\\/]", str(path).strip())[-1].strip().lower()
    return name.replace("sha256:", "sha256-")


def runner_blob_from_cmdline(argv: List[str]) -> Optional[str]:
    """``--model <blob>`` (or ``--model=<blob>``) out of a runner's argv."""
    for i, arg in enumerate(argv or []):
        text = str(arg)
        if text == "--model" and i + 1 < len(argv):
            return str(argv[i + 1])
        if text.startswith("--model="):
            return text[len("--model="):]
    return None


def _norm_gpus(gpus: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Accept the usage route's rows (mem_used in MiB), the snapshot's rows
    (used in bytes) or anything with an index → one shape."""
    out: List[Dict[str, Any]] = []
    for g in gpus or []:
        if not isinstance(g, dict) or g.get("index") is None:
            continue
        try:
            index = int(g["index"])
        except (TypeError, ValueError):
            continue
        used: Optional[int]
        if g.get("used_bytes") is not None:
            used = int(g["used_bytes"])
        elif g.get("used") is not None:
            used = int(g["used"])
        elif g.get("mem_used") is not None:
            used = int(float(g["mem_used"]) * _MIB)
        else:
            used = None
        out.append({
            "index": index,
            "uuid": str(g.get("uuid") or ""),
            "bus_id": str(g.get("bus_id") or ""),
            "name": str(g.get("name") or ""),
            "used_bytes": used,
        })
    return out


def _gpu_index_for(app: Dict[str, Any], gpus: List[Dict[str, Any]]) -> Optional[int]:
    uuid = str(app.get("uuid") or "")
    bus = str(app.get("bus_id") or "").lower()
    for g in gpus:
        if uuid and g["uuid"] and g["uuid"] == uuid:
            return g["index"]
    for g in gpus:
        if bus and g["bus_id"] and g["bus_id"].lower() == bus:
            return g["index"]
    return None


def map_luids(gpus: List[Dict[str, Any]], apps: List[Dict[str, Any]],
              proc_rows: List[Dict[str, Any]], adapters: Dict[str, int]) -> Dict[str, int]:
    """{WDDM adapter luid: gpu index}.

    (1) exact: a pid nvidia-smi lists on exactly one GPU whose counters carry
    exactly one luid (with dedicated bytes) ties that luid to that GPU;
    (2) heuristic for what is left: the adapter whose dedicated total is
    closest to the card's ``memory.used`` — skipped when the runner-up is
    within 5 %, or when even the closest is nowhere near.
    """
    by_index = _map_luids_by_index(gpus, apps, proc_rows, adapters)
    return {luid: idx for idx, luid in by_index.items()}


def _map_luids_by_index(gpus: List[Dict[str, Any]], apps: List[Dict[str, Any]],
                        proc_rows: List[Dict[str, Any]], adapters: Dict[str, int]) -> Dict[int, str]:
    gpus = _norm_gpus(gpus)
    mapping: Dict[int, str] = {}
    taken_luids: set = set()

    pid_gpus: Dict[int, set] = {}
    for a in apps or []:
        idx = _gpu_index_for(a, gpus)
        if idx is not None:
            pid_gpus.setdefault(int(a["pid"]), set()).add(idx)
    pid_luids: Dict[int, set] = {}
    for r in proc_rows or []:
        if int(r.get("dedicated") or 0) > 0:
            pid_luids.setdefault(int(r["pid"]), set()).add(str(r["luid"]))
    for pid, idxs in pid_gpus.items():
        luids = pid_luids.get(pid) or set()
        if len(idxs) == 1 and len(luids) == 1:
            idx, luid = next(iter(idxs)), next(iter(luids))
            if idx in mapping and mapping[idx] != luid:
                continue  # two witnesses disagree: leave it to the heuristic
            if luid in taken_luids and mapping.get(idx) != luid:
                continue
            mapping[idx] = luid
            taken_luids.add(luid)

    free_gpus = [g for g in gpus if g["index"] not in mapping and g["used_bytes"] is not None]
    free_luids = {luid: int(b) for luid, b in (adapters or {}).items() if luid not in taken_luids}
    pairs = sorted(
        (abs(int(g["used_bytes"]) - bytes_), g["index"], luid, bytes_)
        for g in free_gpus for luid, bytes_ in free_luids.items()
    )
    done_gpus: set = set()
    done_luids: set = set()
    for dist, idx, luid, bytes_ in pairs:
        if idx in done_gpus or luid in done_luids:
            continue
        used = next(g["used_bytes"] for g in free_gpus if g["index"] == idx)
        scale = max(int(used), bytes_, 1)
        if dist > _MATCH_FRACTION * scale + _MATCH_SLACK:
            continue
        ambiguous = any(
            (i2 == idx or l2 == luid) and (i2, l2) != (idx, luid)
            and i2 not in done_gpus and l2 not in done_luids
            and d2 - dist <= _AMBIGUOUS_FRACTION * scale
            for d2, i2, l2, _ in pairs
        )
        if ambiguous:
            continue
        mapping[idx] = luid
        done_gpus.add(idx)
        done_luids.add(luid)
    return mapping


def match_models_to_pids(models: List[Dict[str, Any]], pid_blobs: Dict[int, str],
                         model_blobs: Dict[str, Optional[str]], runner_pids: List[int],
                         pid_bytes: Dict[int, Optional[int]]) -> Dict[str, int]:
    """{model name: runner pid}. Blob match first, then size, then the
    only-one-of-each case."""
    assigned: Dict[str, int] = {}
    used: set = set()
    for m in models:
        key = blob_key(model_blobs.get(m["name"]))
        if not key:
            continue
        for pid, blob in pid_blobs.items():
            if pid not in used and blob_key(blob) == key:
                assigned[m["name"]] = pid
                used.add(pid)
                break
    left_models = [m for m in models if m["name"] not in assigned and int(m.get("size_vram") or 0) > 0]
    left_pids = [p for p in runner_pids if p not in used]
    if len(left_models) == 1 and len(left_pids) == 1:
        assigned[left_models[0]["name"]] = left_pids[0]
        return assigned
    for m in sorted(left_models, key=lambda x: -int(x.get("size_vram") or 0)):
        candidates = [(abs(int(pid_bytes[p]) - int(m.get("size_vram") or 0)), p)
                      for p in left_pids if pid_bytes.get(p) is not None]
        if not candidates:
            break
        _, pid = min(candidates)
        assigned[m["name"]] = pid
        left_pids.remove(pid)
    return assigned


def describe(models: List[Dict[str, Any]], assigned: Dict[str, int],
             pid_gpus: Dict[int, Dict[int, Optional[int]]],
             gpus: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """The per-model answer: ``{name: {gpus, per_gpu, placement, pid}}``."""
    gpus = _norm_gpus(gpus)
    out: Dict[str, Dict[str, Any]] = {}
    for m in models:
        name = m["name"]
        size_vram = int(m.get("size_vram") or 0)
        pid = assigned.get(name)
        held = dict(pid_gpus.get(pid, {})) if pid is not None else {}
        idxs = sorted(held)
        if size_vram <= 0:
            out[name] = {"gpus": [], "per_gpu": [], "placement": "cpu", "pid": pid}
            continue
        if not idxs and len(gpus) == 1:
            # One card in the box: the only place VRAM-resident layers can be.
            idxs = [gpus[0]["index"]]
            held = {idxs[0]: None}
        if len(idxs) >= 2:
            place = "split"
        elif len(idxs) == 1:
            place = "single"
            if held.get(idxs[0]) is None:
                # Whole model on one card: `ollama ps` already measured it.
                held[idxs[0]] = size_vram
        else:
            place = "unknown"
        out[name] = {
            "gpus": idxs,
            "per_gpu": [{"index": i, "bytes": held.get(i)} for i in idxs],
            "placement": place,
            "pid": pid,
        }
    return out


def per_gpu(models: Dict[str, Dict[str, Any]], pid_gpus: Dict[int, Dict[int, Optional[int]]],
            runner_pids: List[int], gpus: List[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
    """Turn the per-model answer around: ``{gpu index: {models, runner_pids}}``
    for every card, including the empty ones."""
    out: Dict[int, Dict[str, Any]] = {g["index"]: {"models": [], "runner_pids": []} for g in _norm_gpus(gpus)}
    for name, info in models.items():
        for entry in info.get("per_gpu") or []:
            slot = out.setdefault(int(entry["index"]), {"models": [], "runner_pids": []})
            slot["models"].append({"name": name, "bytes": entry.get("bytes")})
    for pid in runner_pids:
        for idx in pid_gpus.get(pid, {}):
            slot = out.setdefault(int(idx), {"models": [], "runner_pids": []})
            if pid not in slot["runner_pids"]:
                slot["runner_pids"].append(pid)
    for slot in out.values():
        slot["runner_pids"].sort()
        slot["models"].sort(key=lambda m: m["name"])
    return out


# ── collectors (subprocess / psutil / http), each best-effort ───────────────

def _compute_apps() -> List[Dict[str, Any]]:
    exe = gsm._nvidia_smi_path()
    if not exe:
        return []
    try:
        proc = subprocess.run(
            [exe, f"--query-compute-apps={','.join(_COMPUTE_APPS_FIELDS)}",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=4,
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.debug("nvidia-smi compute-apps failed: %s", e)
        return []
    if proc.returncode != 0:
        return []
    return parse_compute_apps(proc.stdout or "")


def _runner_blobs(pids: Dict[int, str]) -> Dict[int, str]:
    """{pid: blob path} for every Ollama-spawned process started with
    ``--model``: the runners, one per loaded model."""
    try:
        import psutil
    except Exception:  # pragma: no cover - hard dep in practice
        return {}
    out: Dict[int, str] = {}
    for pid in pids:
        try:
            blob = runner_blob_from_cmdline(psutil.Process(pid).cmdline())
        except Exception:
            continue
        if blob:
            out[int(pid)] = blob
    return out


def _show_blob(base: str, name: str, digest: str = "") -> Optional[str]:
    key = (base, name, digest)
    now = time.time()
    with _lock:
        hit = _show_cache.get(key)
    if hit and now - hit[0] < _SHOW_TTL:
        return hit[1]
    blob: Optional[str] = None
    try:
        r = httpx.post(f"{base}/api/show", json={"model": name}, timeout=_SHOW_TIMEOUT)
        if r.status_code == 200:
            blob = parse_modelfile_blob(str((r.json() or {}).get("modelfile") or ""))
    except Exception as e:  # noqa: BLE001 — the row renders without it
        logger.debug("placement: /api/show failed for %s: %s", name, e)
    with _lock:
        if len(_show_cache) > 200:
            _show_cache.clear()
        _show_cache[key] = (now, blob)
    return blob


def _wddm() -> Optional[Dict[str, Any]]:
    if not sys.platform.startswith("win"):
        return None
    try:
        return gsm.wddm_rows()
    except Exception as e:  # noqa: BLE001
        logger.debug("placement: WDDM counters unavailable: %s", e)
        return None


def _analyse(base: str, models: List[Dict[str, Any]], gpus: List[Dict[str, Any]]) -> Dict[str, Any]:
    apps = _compute_apps()
    procs = gsm.runner_pids()
    pid_blobs = _runner_blobs(procs)
    runner_pids = sorted(set(pid_blobs) | {int(a["pid"]) for a in apps if int(a["pid"]) in procs})

    pid_gpus: Dict[int, Dict[int, Optional[int]]] = {}
    for a in apps:
        idx = _gpu_index_for(a, gpus)
        if idx is not None:
            pid_gpus.setdefault(int(a["pid"]), {})[idx] = a.get("used_bytes")

    wddm = _wddm()
    if wddm:
        luid_to_idx = map_luids(gpus, apps, wddm.get("processes") or [], wddm.get("adapters") or {})
        for r in wddm.get("processes") or []:
            idx = luid_to_idx.get(str(r["luid"]))
            pid = int(r["pid"])
            if idx is None:
                continue
            if pid in pid_gpus:
                if idx in pid_gpus[pid] and pid_gpus[pid][idx] is None:
                    pid_gpus[pid][idx] = int(r.get("dedicated") or 0)
            elif pid in runner_pids and int(r.get("dedicated") or 0) > 0:
                # nvidia-smi did not list it (no compute-apps support) but
                # the counters did: still a runner on that card.
                pid_gpus.setdefault(pid, {})[idx] = int(r.get("dedicated") or 0)

    model_blobs: Dict[str, Optional[str]] = {}
    if pid_blobs:
        for m in models:
            model_blobs[m["name"]] = _show_blob(base, m["name"], m.get("digest") or "")
    pid_bytes: Dict[int, Optional[int]] = {}
    for pid in runner_pids:
        known = [b for b in pid_gpus.get(pid, {}).values() if b is not None]
        pid_bytes[pid] = sum(known) if known else None
    assigned = match_models_to_pids(models, pid_blobs, model_blobs, runner_pids, pid_bytes)
    described = describe(models, assigned, pid_gpus, gpus)
    return {"models": described, "gpus": per_gpu(described, pid_gpus, runner_pids, gpus)}


def _norm_models(ps_models: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for m in ps_models or []:
        if not isinstance(m, dict):
            continue
        name = str(m.get("name") or m.get("model") or "")
        if not name:
            continue
        out.append({
            "name": name,
            "size": int(m.get("size") or 0),
            "size_vram": int(m.get("size_vram") or 0),
            "digest": str(m.get("digest") or ""),
        })
    return out


def report(ollama_base: str, ps_models: Optional[List[Dict[str, Any]]],
           gpus: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """``{"models": {name: {gpus, per_gpu, placement, pid}}, "gpus": {index:
    {models, runner_pids}}}``. Cached ~2 s, never raises; empty with nothing
    loaded (and then nothing is run or fetched)."""
    models = _norm_models(ps_models)
    if gpus is None:
        snap = gsm.vram_snapshot()
        gpus = snap.get("gpus") or [] if snap.get("supported") else []
    norm_gpus = _norm_gpus(gpus)
    empty = {"models": {}, "gpus": {g["index"]: {"models": [], "runner_pids": []} for g in norm_gpus}}
    if not models:
        return empty
    base = str(ollama_base or "").rstrip("/")
    key = (base, tuple(sorted((m["name"], m["size_vram"], m["digest"]) for m in models)),
           tuple(g["index"] for g in norm_gpus))
    now = time.time()
    with _lock:
        if _cache["data"] is not None and _cache["key"] == key and now - _cache["ts"] < _CACHE_TTL:
            return _cache["data"]
    try:
        data = _analyse(base, models, norm_gpus)
    except Exception as e:  # noqa: BLE001 — must never break /api/system/usage
        logger.debug("gpu placement failed: %s", e)
        data = empty
    with _lock:
        _cache["ts"] = time.time()
        _cache["key"] = key
        _cache["data"] = data
    return data


def placement(ollama_base: str, ps_models: Optional[List[Dict[str, Any]]],
              gpus: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Dict[str, Any]]:
    """{model name: {"gpus": [idx], "per_gpu": [{"index", "bytes"|None}],
    "placement": "single|split|cpu|unknown", "pid": int|None}}."""
    return report(ollama_base, ps_models, gpus).get("models") or {}


def gpus_runners(ollama_base: str, ps_models: Optional[List[Dict[str, Any]]],
                 gpus: Optional[List[Dict[str, Any]]] = None) -> Dict[int, Dict[str, Any]]:
    """{gpu index: {"models": [{"name", "bytes"|None}], "runner_pids": [pid]}}."""
    return report(ollama_base, ps_models, gpus).get("gpus") or {}


# ── orphaned runners ────────────────────────────────────────────────────────
#
# Seen live (ronda 6, two-card box): restarting Ollama (Stop-Process on
# ollama*) leaves its `llama-server.exe` children alive — two of them held
# 13 GB on the 5060 Ti with `ollama ps` empty, and every gauge just read
# "other 13 GB". Nothing but a process list can tell those from a browser.

_ORPHAN_TTL = 2.0
_orphan_cache: Dict[str, Any] = {"ts": 0.0, "data": None}


def is_orphan_runner(name: str, parent_name: Optional[str], parent_alive: bool) -> bool:
    """A runner process (llama-server / ollama_llama_server / ollama-runner)
    whose parent is gone, or is not an Ollama process (a recycled pid)."""
    n = (name or "").lower()
    if not any(n.startswith(r) for r in gsm._RUNNER_NAMES):
        return False
    if not parent_alive:
        return True
    return not (parent_name or "").lower().startswith("ollama")


def _orphan_processes() -> List[Dict[str, Any]]:
    try:
        import psutil
    except Exception:  # pragma: no cover
        return []
    out: List[Dict[str, Any]] = []
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            name = (proc.info.get("name") or "")
            if not any(name.lower().startswith(r) for r in gsm._RUNNER_NAMES):
                continue
            parent = proc.parent()
            parent_name = None
            parent_alive = False
            if parent is not None:
                try:
                    parent_alive = parent.is_running() and parent.status() != psutil.STATUS_ZOMBIE
                    parent_name = parent.name() if parent_alive else None
                except Exception:
                    parent_alive = False
            if not is_orphan_runner(name, parent_name, parent_alive):
                continue
            try:
                started = float(proc.create_time())
            except Exception:
                started = None
            try:
                blob = runner_blob_from_cmdline(proc.cmdline())
            except Exception:
                blob = None
            out.append({"pid": int(proc.pid), "name": name, "started": started,
                        "blob": blob_key(blob) if blob else ""})
        except Exception:
            continue
    return out


def orphan_runners(gpus: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """``[{pid, name, started, gpus: [idx], bytes|None, blob}]`` — runner
    processes no Ollama server owns any more, with the card(s) and bytes
    they still hold (nvidia-smi compute-apps + the WDDM counters, like the
    placement). Cached ~2 s; never raises; runs even with nothing loaded —
    that is exactly when orphans matter."""
    now = time.time()
    with _lock:
        if _orphan_cache["data"] is not None and now - _orphan_cache["ts"] < _ORPHAN_TTL:
            return list(_orphan_cache["data"])
    try:
        data = _orphans_uncached(gpus)
    except Exception as e:  # noqa: BLE001
        logger.debug("orphan runner scan failed: %s", e)
        data = []
    with _lock:
        _orphan_cache["ts"] = time.time()
        _orphan_cache["data"] = data
    return list(data)


def _orphans_uncached(gpus: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    procs = _orphan_processes()
    if not procs:
        return []
    if gpus is None:
        snap = gsm.vram_snapshot()
        gpus = snap.get("gpus") or [] if snap.get("supported") else []
    norm_gpus = _norm_gpus(gpus)
    apps = _compute_apps()
    pids = {p["pid"] for p in procs}
    pid_gpus: Dict[int, Dict[int, Optional[int]]] = {}
    for a in apps:
        pid = int(a["pid"])
        if pid not in pids:
            continue
        idx = _gpu_index_for(a, norm_gpus)
        if idx is not None:
            pid_gpus.setdefault(pid, {})[idx] = a.get("used_bytes")
    wddm = _wddm()
    if wddm:
        luid_to_idx = map_luids(norm_gpus, apps, wddm.get("processes") or [], wddm.get("adapters") or {})
        for r in wddm.get("processes") or []:
            pid = int(r["pid"])
            if pid not in pids:
                continue
            idx = luid_to_idx.get(str(r["luid"]))
            if idx is None:
                continue
            dedicated = int(r.get("dedicated") or 0)
            slot = pid_gpus.setdefault(pid, {})
            if slot.get(idx) is None and dedicated > 0:
                slot[idx] = dedicated
    for p in procs:
        cards = pid_gpus.get(p["pid"], {})
        p["gpus"] = sorted(cards)
        known = [b for b in cards.values() if b is not None]
        p["bytes"] = sum(known) if known else None
    return procs


def release_orphan(pid: int, timeout: float = 3.0) -> Dict[str, Any]:
    """Terminate ONE runner that is an orphan RIGHT NOW (re-checked, uncached):
    never an arbitrary pid, never a runner an Ollama server still owns.
    Returns ``{"ok", "pid", "killed": bool, "reason"}``."""
    pid = int(pid)
    current = {p["pid"]: p for p in _orphans_uncached(None)}
    if pid not in current:
        return {"ok": False, "pid": pid, "killed": False,
                "reason": "not an orphaned runner right now (gone already, or still owned by an Ollama server)"}
    try:
        import psutil
        proc = psutil.Process(pid)
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except psutil.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=timeout)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "pid": pid, "killed": False, "reason": str(e)[:200]}
    with _lock:
        _orphan_cache["ts"] = 0.0
        _orphan_cache["data"] = None
    gsm.reset_vram_cache()
    return {"ok": True, "pid": pid, "killed": True, "reason": "",
            "bytes": current[pid].get("bytes"), "gpus": current[pid].get("gpus") or []}


def reset_cache() -> None:
    with _lock:
        _cache["ts"] = 0.0
        _cache["key"] = None
        _orphan_cache["ts"] = 0.0
        _orphan_cache["data"] = None
        _cache["data"] = None
        _show_cache.clear()
