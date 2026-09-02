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

:func:`vram_snapshot` at the bottom is the other half and is not Windows-only:
the plain size of the card(s), read from nvidia-smi, for the callers that have
to answer "will this model fit" *before* anything is loaded — the model picker.
With two cards it is the pool (Ollama 0.33 places a model on the card with the
most free memory and splits one that fits no single card), plus one entry per
card so the fit arithmetic can also ask "does it fit ONE card".
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Instance names look like: pid_2448_luid_0x00000000_0x0000edb2_phys_0
_INSTANCE_RE = re.compile(
    r"pid_(\d+)_luid_(0x[0-9a-fA-F]+)_(0x[0-9a-fA-F]+)_phys_(\d+)", re.IGNORECASE
)
# Adapter instances have no pid: luid_0x00000000_0x01b3ff4f_phys_0
_ADAPTER_RE = re.compile(
    r"luid_(0x[0-9a-fA-F]+)_(0x[0-9a-fA-F]+)_phys_(\d+)", re.IGNORECASE
)

_SHARED_PATH = r"\GPU Process Memory(*)\Shared Usage"
_DEDICATED_PATH = r"\GPU Process Memory(*)\Dedicated Usage"
# Per-adapter total, the WDDM twin of nvidia-smi `memory.used` — what maps a
# luid onto a GPU index when no runner pins it down (src/gpu_placement.py).
_ADAPTER_DEDICATED_PATH = r"\GPU Adapter Memory(*)\Dedicated Usage"

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


def parse_process_counters(shared: Dict[str, int], dedicated: Dict[str, int]) -> List[Dict[str, Any]]:
    """{instance name: value} × 2 → one row per (pid, adapter luid).

    ``luid`` is the adapter as WDDM names it (``0x00000000_0x01b3ff4f``): a
    runner holding a model split across two cards has two rows, one per luid.
    """
    by_key: Dict[tuple, Dict[str, Any]] = {}
    for source, values in (("shared", shared), ("dedicated", dedicated)):
        for name, value in (values or {}).items():
            m = _INSTANCE_RE.search(name)
            if not m:
                continue
            pid = int(m.group(1))
            luid = f"{m.group(2)}_{m.group(3)}"
            row = by_key.setdefault(
                (pid, luid), {"pid": pid, "luid": luid, "shared": 0, "dedicated": 0}
            )
            row[source] = int(value)
    return list(by_key.values())


def parse_adapter_counters(dedicated: Dict[str, int]) -> Dict[str, int]:
    """{luid: dedicated bytes} from the per-adapter counter instances."""
    out: Dict[str, int] = {}
    for name, value in (dedicated or {}).items():
        if _INSTANCE_RE.search(name):
            continue  # a process instance, not an adapter one
        m = _ADAPTER_RE.search(name)
        if not m:
            continue
        luid = f"{m.group(1)}_{m.group(2)}"
        out[luid] = out.get(luid, 0) + int(value)
    return out


def _rows() -> List[Dict[str, Any]]:
    """Per (pid, adapter) dedicated/shared bytes, from the WDDM counters."""
    counters = _read_counters([_SHARED_PATH, _DEDICATED_PATH])
    return parse_process_counters(counters.get(_SHARED_PATH, {}), counters.get(_DEDICATED_PATH, {}))


def wddm_rows() -> Dict[str, Any]:
    """Process rows AND adapter totals in one PDH query, for the placement
    module: ``{"processes": [...rows as _rows()...], "adapters": {luid: bytes}}``.
    Windows only; raises where the counters are not available (the caller
    treats that as "no per-card bytes", not as an error)."""
    if not sys.platform.startswith("win"):
        raise OSError("WDDM counters are Windows-only")
    counters = _read_counters([_SHARED_PATH, _DEDICATED_PATH, _ADAPTER_DEDICATED_PATH])
    return {
        "processes": parse_process_counters(
            counters.get(_SHARED_PATH, {}), counters.get(_DEDICATED_PATH, {})
        ),
        "adapters": parse_adapter_counters(counters.get(_ADAPTER_DEDICATED_PATH, {})),
    }


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


# ── The cards' own VRAM, before anything is loaded ──────────────────────────
#
# Everything above answers "is the runner paging over PCIe *right now*" — a
# diagnosis after the fact, and only on Windows. Choosing a model is a decision
# taken *before* anything is loaded, and that needs the plain size of the card.
# nvidia-smi is the only source that answers with no model resident, so it is
# read here, once, cheaply, and reported as unsupported everywhere it is not
# available rather than guessed at.
#
# With several cards the headline numbers are the POOL: Ollama 0.33 schedules
# across every GPU it sees (a model goes to the card with the most free memory;
# one that fits no single card is split across them, verified on the reference
# box — qwen3.8:27b-q4_K_M at 17 GB lands as 8.5 + 10.2 GB across a 12 GB and
# a 16 GB card at 100 % GPU). "Would it fit somewhere" is therefore a question
# about the pool; "would it fit ONE card" is answered from ``gpus``.

_MIB = 1024 * 1024
_VRAM_TTL = 8.0
_vram_cache: Dict[str, Any] = {"ts": 0.0, "data": None}
_vram_lock = threading.Lock()

_VRAM_FIELDS = ("index", "name", "uuid", "memory.total", "memory.used")
_VENDOR_PREFIXES = ("NVIDIA GeForce ", "NVIDIA ")


def short_gpu_name(name: str) -> str:
    """'NVIDIA GeForce RTX 4070 Ti' → 'RTX 4070 Ti' (the pool label)."""
    text = str(name or "").strip()
    for prefix in _VENDOR_PREFIXES:
        if text.startswith(prefix) and len(text) > len(prefix):
            return text[len(prefix):]
    return text


def pool_name(names: List[str]) -> str:
    """One card keeps its full name; a pool is 'RTX 4070 Ti + RTX 5060 Ti'."""
    names = [str(n or "") for n in names if str(n or "").strip()]
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return " + ".join(short_gpu_name(n) for n in names)


def parse_vram_query(stdout: str) -> List[Dict[str, Any]]:
    """Rows of ``--query-gpu=index,name,uuid,memory.total,memory.used``
    (csv, noheader, nounits) → ``[{index, name, uuid, total, used, free}]``
    in bytes. Rows that do not parse, or report no memory, are skipped."""
    gpus: List[Dict[str, Any]] = []
    for line in (stdout or "").splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < len(_VRAM_FIELDS):
            continue
        # A name with a comma in it would shift the columns; take the numeric
        # fields from the right so the name absorbs any surplus.
        try:
            index = int(float(parts[0]))
            total = int(float(parts[-2])) * _MIB
            used = int(float(parts[-1])) * _MIB
        except ValueError:
            continue
        if total <= 0:
            continue
        gpus.append({
            "index": index,
            "name": ", ".join(parts[1:-3]),
            "uuid": parts[-3],
            "total": total,
            "used": max(0, used),
            "free": max(0, total - used),
        })
    return gpus


def snapshot_from_gpus(gpus: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The pool view over per-card rows (see :func:`vram_snapshot`)."""
    if not gpus:
        return {"supported": False, "reason": "nvidia-smi reported no usable GPU"}
    total = sum(int(g.get("total") or 0) for g in gpus)
    used = sum(int(g.get("used") or 0) for g in gpus)
    return {
        "supported": True,
        "name": pool_name([g.get("name", "") for g in gpus]),
        "total": total,
        "used": used,
        "free": max(0, total - used),
        "count": len(gpus),
        "gpus": [dict(g) for g in gpus],
    }


def _nvidia_smi_path() -> Optional[str]:
    exe = shutil.which("nvidia-smi")
    if exe:
        return exe
    # Standard Windows install locations when PATH does not include it.
    for cand in (
        r"C:\Windows\System32\nvidia-smi.exe",
        r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe",
    ):
        if os.path.exists(cand):
            return cand
    return None


def _vram_uncached() -> Dict[str, Any]:
    exe = _nvidia_smi_path()
    if not exe:
        return {"supported": False, "reason": "nvidia-smi: not found"}
    try:
        proc = subprocess.run(
            [exe, f"--query-gpu={','.join(_VRAM_FIELDS)}",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=4,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return {"supported": False, "reason": f"nvidia-smi: {e}"}
    if proc.returncode != 0:
        return {"supported": False,
                "reason": f"nvidia-smi exit {proc.returncode}"}
    return snapshot_from_gpus(parse_vram_query(proc.stdout))


def vram_snapshot() -> Dict[str, Any]:
    """Total/used/free VRAM in bytes, plus the cards. Never raises.

    ``{"supported": True, "name", "total", "used", "free", "count",
    "gpus": [{"index", "name", "uuid", "total", "used", "free"}]}``. With one
    card ``name/total/used/free`` are that card's; with several they are the
    POOL sums and ``name`` reads ``"RTX 4070 Ti + RTX 5060 Ti"``.

    ``{"supported": False, "reason": ...}`` when there is no NVIDIA card, no
    nvidia-smi, or the output could not be parsed — callers are expected to
    show nothing at all in that case rather than invent a number.
    """
    with _vram_lock:
        cached = _vram_cache.get("data")
        if cached is not None and time.time() - _vram_cache["ts"] < _VRAM_TTL:
            return cached
    try:
        data = _vram_uncached()
    except Exception as e:  # pragma: no cover - defensive, subprocess is guarded
        logger.debug("vram snapshot failed: %s", e)
        data = {"supported": False, "reason": str(e)[:200]}
    with _vram_lock:
        _vram_cache["ts"] = time.time()
        _vram_cache["data"] = data
    return data


def reset_vram_cache() -> None:
    with _vram_lock:
        _vram_cache["ts"] = 0.0
        _vram_cache["data"] = None


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
