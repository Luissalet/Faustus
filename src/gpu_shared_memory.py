"""Shared GPU memory (Windows WDDM) — the number `nvidia-smi` cannot see.

`nvidia-smi memory.used` counts the card's own VRAM and nothing else. Windows
additionally lets a GPU page allocations into system RAM over PCIe — "shared
GPU memory" in Task Manager, 102 GB of it on a 128 GB box. For LLM inference
that is never what you want: PCIe 4.0 x16 moves ~25 GB/s against ~500 GB/s of
GDDR6X, and generation re-reads the active weights for every token, so a layer
served from shared memory costs roughly 20x.

llama.cpp / Ollama never use it on purpose. What they do when a model does not
fit is offload whole layers to the CPU, which reads the same RAM without the
PCIe round trip — that is the `45%/55% CPU/GPU` split `ollama ps` reports.
Shared memory filling up therefore means something spilled *behind* Ollama's
back: the CUDA driver's system-memory fallback caught an allocation that did
not fit, and the model now runs at a fraction of its speed while every other
indicator (VRAM used, `ollama ps`, GPU utilisation, temperature) still looks
perfectly healthy. That is the failure this module makes visible.

Windows-only and best-effort: everywhere else it reports ``supported: False``
and the caller carries on. Counters are read through PDH (a few milliseconds,
no subprocess) and cached briefly, because the usage widget polls every 1.5 s
while a model generates.
"""
from __future__ import annotations

import logging
import os
import re
import sys
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Instance names look like: pid_2448_luid_0x00000000_0x0000edb2_phys_0
_INSTANCE_RE = re.compile(
    r"pid_(\d+)_luid_(0x[0-9a-fA-F]+)_(0x[0-9a-fA-F]+)_phys_(\d+)", re.IGNORECASE
)

_SHARED_PATH = r"\GPU Process Memory(*)\Shared Usage"
_DEDICATED_PATH = r"\GPU Process Memory(*)\Dedicated Usage"

# A CUDA process always holds *some* host-visible memory — staging and pinned
# buffers — and Windows counts that as shared. Measured on the reference box
# (RTX 4070 Ti, qwen3.5:9b fully on the GPU, 65 tok/s): a flat 706 MB shared
# against 8461 MB dedicated, 7.7%, unchanged while generating. That is the
# healthy baseline, not a spill. Weights actually paging over PCIe show up as
# gigabytes and a much larger share of the runner's footprint, so we want both
# an absolute floor and a fraction before crying wolf.
DEFAULT_WARN_BYTES = 1024 * 1024 * 1024
WARN_FRACTION = 0.15

_CACHE_TTL = 2.0
_cache: Dict[str, Any] = {"ts": 0.0, "data": None}
_lock = threading.Lock()

PDH_FMT_LARGE = 0x00000400
PDH_MORE_DATA = 0x800007D2


def warn_threshold_bytes() -> int:
    """Shared bytes attributed to Ollama above which we call it spilling."""
    raw = os.getenv("FAUSTUS_GPU_SHARED_WARN_BYTES", "").strip()
    if raw:
        try:
            v = int(raw)
            if v > 0:
                return v
        except ValueError:
            pass
    return DEFAULT_WARN_BYTES


def _read_counters(paths: List[str]) -> Dict[str, Dict[str, int]]:
    """{path: {instance name: value}} for several wildcard counter paths.

    One query for all of them: opening a PDH query and collecting the GPU
    counter set is the expensive part (~150 ms), and the widget polls this
    every 1.5 s while a model generates.
    """
    import ctypes
    from ctypes import wintypes

    pdh = ctypes.WinDLL("pdh.dll")

    def rc_of(value: int) -> int:
        """PDH status codes are unsigned; ctypes hands them back signed."""
        return value & 0xFFFFFFFF

    class PDH_FMT_COUNTERVALUE(ctypes.Structure):
        _fields_ = [("CStatus", ctypes.c_uint32), ("largeValue", ctypes.c_longlong)]

    class PDH_FMT_COUNTERVALUE_ITEM_W(ctypes.Structure):
        _fields_ = [("szName", ctypes.c_wchar_p), ("FmtValue", PDH_FMT_COUNTERVALUE)]

    query = wintypes.LPVOID()
    if rc_of(pdh.PdhOpenQueryW(None, 0, ctypes.byref(query))) != 0:
        raise OSError("PdhOpenQueryW failed")
    try:
        counters = {}
        for path in paths:
            handle = wintypes.LPVOID()
            rc = rc_of(pdh.PdhAddEnglishCounterW(query, path, 0, ctypes.byref(handle)))
            if rc != 0:
                raise OSError(f"PdhAddEnglishCounterW({path}) failed: {rc:#010x}")
            counters[path] = handle
        rc = rc_of(pdh.PdhCollectQueryData(query))
        if rc != 0:
            raise OSError(f"PdhCollectQueryData failed: {rc:#010x}")

        out: Dict[str, Dict[str, int]] = {}
        for path, handle in counters.items():
            values: Dict[str, int] = {}
            out[path] = values
            size = ctypes.c_uint32(0)
            count = ctypes.c_uint32(0)
            rc = rc_of(pdh.PdhGetFormattedCounterArrayW(
                handle, PDH_FMT_LARGE, ctypes.byref(size), ctypes.byref(count), None
            ))
            if rc == 0 or count.value == 0:
                continue
            if rc != PDH_MORE_DATA:
                raise OSError(f"PdhGetFormattedCounterArrayW sizing failed: {rc:#010x}")
            buf = ctypes.create_string_buffer(size.value)
            rc = rc_of(pdh.PdhGetFormattedCounterArrayW(
                handle, PDH_FMT_LARGE, ctypes.byref(size), ctypes.byref(count), buf
            ))
            if rc != 0:
                raise OSError(f"PdhGetFormattedCounterArrayW failed: {rc:#010x}")
            items = ctypes.cast(
                buf, ctypes.POINTER(PDH_FMT_COUNTERVALUE_ITEM_W * count.value)
            ).contents
            for item in items:
                if item.szName:
                    values[item.szName] = int(item.FmtValue.largeValue)
        return out
    finally:
        pdh.PdhCloseQuery(query)


def _rows() -> List[Dict[str, Any]]:
    """Per (pid, adapter) dedicated/shared bytes, from the WDDM counters."""
    counters = _read_counters([_SHARED_PATH, _DEDICATED_PATH])
    shared = counters.get(_SHARED_PATH, {})
    dedicated = counters.get(_DEDICATED_PATH, {})
    by_key: Dict[tuple, Dict[str, Any]] = {}
    for source, values in (("shared", shared), ("dedicated", dedicated)):
        for name, value in values.items():
            m = _INSTANCE_RE.search(name)
            if not m:
                continue
            pid = int(m.group(1))
            luid = f"{m.group(2)}_{m.group(3)}"
            row = by_key.setdefault(
                (pid, luid), {"pid": pid, "luid": luid, "shared": 0, "dedicated": 0}
            )
            row[source] = value
    return list(by_key.values())


# Ollama does not do the inference itself: it spawns a runner, and on Windows
# 0.33 that runner is `llama-server.exe`. Filtering on "ollama*" finds the
# server and the tray app — the two processes holding no GPU memory at all —
# and misses the only one that matters, so walk the children too.
_RUNNER_NAMES = ("llama-server", "ollama_llama_server", "ollama-runner")


def runner_pids() -> Dict[int, str]:
    """{pid: process name} for Ollama and whatever it spawned to hold the model."""
    try:
        import psutil
    except Exception:  # pragma: no cover - psutil is a hard dep in practice
        return {}
    found: Dict[int, str] = {}
    roots = []
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            name = (proc.info.get("name") or "").lower()
        except Exception:
            continue
        if name.startswith("ollama"):
            found[int(proc.info["pid"])] = name
            roots.append(proc)
        elif any(name.startswith(r) for r in _RUNNER_NAMES):
            # Reachable even if the parent link is gone (restarted server).
            found[int(proc.info["pid"])] = name
    for proc in roots:
        try:
            for child in proc.children(recursive=True):
                found[int(child.pid)] = (child.name() or "").lower()
        except Exception:
            continue
    return found


def _collect_uncached() -> Dict[str, Any]:
    if not sys.platform.startswith("win"):
        return {"supported": False, "reason": "shared GPU memory is a Windows/WDDM concept"}
    try:
        rows = _rows()
    except Exception as e:
        logger.debug("GPU process memory counters unavailable: %s", e)
        return {"supported": False, "reason": f"PDH: {e}"}
    procs = runner_pids()
    shared_bytes = sum(r["shared"] for r in rows if r["pid"] in procs)
    dedicated_bytes = sum(r["dedicated"] for r in rows if r["pid"] in procs)
    footprint = shared_bytes + dedicated_bytes
    fraction = (shared_bytes / footprint) if footprint else 0.0
    threshold = warn_threshold_bytes()
    return {
        "supported": True,
        "threshold": threshold,
        "warn_fraction": WARN_FRACTION,
        "total_shared": sum(r["shared"] for r in rows),
        "ollama": {
            "pids": sorted(procs),
            "processes": sorted(set(procs.values())),
            "shared": shared_bytes,
            # WDDM commitment, not what nvidia-smi calls "used": Windows can
            # evict a committed allocation and still count it here. Good enough
            # to see that the runner holds the card, not precise enough to do
            # arithmetic with — the fit advisor uses `ollama ps` for that.
            "dedicated": dedicated_bytes,
            "shared_fraction": round(fraction, 4),
            # The whole point of the module: weights paging over PCIe while
            # every other gauge still looks fine. Both tests have to fire, so
            # the CUDA baseline does not raise a false alarm.
            "spilling": shared_bytes > threshold and fraction > WARN_FRACTION,
        },
    }


def collect() -> Dict[str, Any]:
    """Cached snapshot for the usage endpoint. Never raises."""
    with _lock:
        now = time.time()
        cached = _cache.get("data")
        if cached is not None and now - _cache["ts"] < _CACHE_TTL:
            return cached
    try:
        data = _collect_uncached()
    except Exception as e:  # belt and braces: this must never break /api/system/usage
        logger.debug("gpu shared memory collection failed: %s", e)
        data = {"supported": False, "reason": str(e)[:200]}
    with _lock:
        _cache["ts"] = time.time()
        _cache["data"] = data
    return data


def reset_cache() -> None:
    with _lock:
        _cache["ts"] = 0.0
        _cache["data"] = None


def describe(snapshot: Optional[Dict[str, Any]] = None) -> str:
    """One line for logs and for `/usage` in the chat."""
    d = snapshot if snapshot is not None else collect()
    if not d.get("supported"):
        return f"shared GPU memory: unavailable ({d.get('reason', 'unknown')})"
    o = d.get("ollama") or {}
    mb = (o.get("shared") or 0) / (1024 * 1024)
    pct = 100.0 * (o.get("shared_fraction") or 0.0)
    if o.get("spilling"):
        return (f"the model runner is using {mb:.0f} MB of shared GPU memory "
                f"({pct:.0f}% of its footprint) — weights are paging over PCIe")
    return f"runner shared GPU memory: {mb:.0f} MB ({pct:.0f}%) — no spill"
