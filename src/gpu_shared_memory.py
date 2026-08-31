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

# Ollama's runner allocates in bursts; a couple of hundred MB of shared memory
# is the driver keeping command buffers around, not weights paging over PCIe.
DEFAULT_WARN_BYTES = 256 * 1024 * 1024

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


def _read_counter(path: str) -> Dict[str, int]:
    """{instance name: value} for one wildcard counter path, via PDH."""
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
        counter = wintypes.LPVOID()
        rc = rc_of(pdh.PdhAddEnglishCounterW(query, path, 0, ctypes.byref(counter)))
        if rc != 0:
            raise OSError(f"PdhAddEnglishCounterW({path}) failed: {rc:#010x}")
        rc = rc_of(pdh.PdhCollectQueryData(query))
        if rc != 0:
            raise OSError(f"PdhCollectQueryData failed: {rc:#010x}")
        size = ctypes.c_uint32(0)
        count = ctypes.c_uint32(0)
        rc = rc_of(pdh.PdhGetFormattedCounterArrayW(
            counter, PDH_FMT_LARGE, ctypes.byref(size), ctypes.byref(count), None
        ))
        if rc == 0 or count.value == 0:
            return {}
        if rc != PDH_MORE_DATA:
            raise OSError(f"PdhGetFormattedCounterArrayW sizing failed: {rc:#010x}")
        buf = ctypes.create_string_buffer(size.value)
        rc = rc_of(pdh.PdhGetFormattedCounterArrayW(
            counter, PDH_FMT_LARGE, ctypes.byref(size), ctypes.byref(count), buf
        ))
        if rc != 0:
            raise OSError(f"PdhGetFormattedCounterArrayW failed: {rc:#010x}")
        items = ctypes.cast(
            buf, ctypes.POINTER(PDH_FMT_COUNTERVALUE_ITEM_W * count.value)
        ).contents
        out: Dict[str, int] = {}
        for item in items:
            if item.szName:
                out[item.szName] = int(item.FmtValue.largeValue)
        return out
    finally:
        pdh.PdhCloseQuery(query)


def _rows() -> List[Dict[str, Any]]:
    """Per (pid, adapter) dedicated/shared bytes, from the WDDM counters."""
    shared = _read_counter(_SHARED_PATH)
    dedicated = _read_counter(_DEDICATED_PATH)
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


def ollama_pids() -> List[int]:
    """PIDs of the Ollama server and its model runners."""
    try:
        import psutil
    except Exception:  # pragma: no cover - psutil is a hard dep in practice
        return []
    pids: List[int] = []
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            name = (proc.info.get("name") or "").lower()
        except Exception:
            continue
        if name.startswith("ollama"):
            pids.append(int(proc.info["pid"]))
    return pids


def _collect_uncached() -> Dict[str, Any]:
    if not sys.platform.startswith("win"):
        return {"supported": False, "reason": "shared GPU memory is a Windows/WDDM concept"}
    try:
        rows = _rows()
    except Exception as e:
        logger.debug("GPU process memory counters unavailable: %s", e)
        return {"supported": False, "reason": f"PDH: {e}"}
    pids = set(ollama_pids())
    ollama_shared = sum(r["shared"] for r in rows if r["pid"] in pids)
    ollama_dedicated = sum(r["dedicated"] for r in rows if r["pid"] in pids)
    threshold = warn_threshold_bytes()
    return {
        "supported": True,
        "threshold": threshold,
        "total_shared": sum(r["shared"] for r in rows),
        "ollama": {
            "pids": sorted(pids),
            "shared": ollama_shared,
            "dedicated": ollama_dedicated,
            # The whole point of the module: weights paging over PCIe while
            # every other gauge still looks fine.
            "spilling": ollama_shared > threshold,
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
    if o.get("spilling"):
        return f"Ollama is using {mb:.0f} MB of shared GPU memory — weights are paging over PCIe"
    return f"Ollama shared GPU memory: {mb:.0f} MB (no spill)"
