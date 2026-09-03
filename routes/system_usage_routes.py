"""System usage API — what `ollama ps` + `nvidia-smi` + a RAM/CPU gauge show,
as one JSON document the chat UI can poll while a local model generates.

GET /api/system/usage
{
  "ts": 1725000000.0,
  "ollama": {"reachable": true, "base": "http://127.0.0.1:11434",
             "models": [{"name", "size", "size_vram", "gpu_pct", "cpu_pct",
                         "context_length", "expires_at", "parameter_size",
                         "quantization",
                         "gpus": [1] | [0, 1] | [],        # the card(s) it sits on
                         "placement": "single|split|cpu|unknown",
                         "per_gpu": [{"index", "bytes"|null}]}]},
  "gpu": [{"index", "name", "util", "mem_used", "mem_total", "temp",
           "power", "power_limit",              # MiB / °C / W, from nvidia-smi
           "uuid", "bus_id", "mem_free",
           "models": [{"name", "bytes"|null}],  # loaded models resident on THIS card
           "runner_pids": [15948]}],
  "orphans": [{"pid", "name", "started", "gpus", "bytes", "blob"}],  # runners no server owns
  "gpu_pool": {"count": 2, "mem_used", "mem_total", "mem_free",   # sums (MiB)
               "util": max, "util_avg", "power": sum, "power_limit": sum,
               "temp": max, "names": [...]},   # {} when no GPU
  "gpu_mem": {"supported": true,               # Windows WDDM counters
              "ollama": {"shared": 0, "dedicated": 7.6e9, "spilling": false}},
  "sysmem_fallback": {"exposed": false, "manual_only": true, "steps": [...]},
  "cpu": {"percent": 12.5, "count": 32},
  "ram": {"used": 40.1e9, "total": 137.0e9, "percent": 29.3},
  "errors": ["nvidia-smi: not found"]        # non-fatal collection problems
}

`gpu` is each card's own VRAM; `gpu_mem` is the part nvidia-smi cannot see —
system RAM the driver paged GPU allocations into over PCIe. Ollama filling
that up is a ~20x slowdown with every other gauge still reading green, so the
widget calls it out. See src/gpu_shared_memory.py for why. With two cards
Ollama 0.33 places each model on the card with the most free memory and
splits one that fits no single card; `gpu_pool` is the sum the fit
arithmetic works against and src/gpu_placement.py says where each model
went.

Everything is best-effort: a missing nvidia-smi or an unreachable Ollama just
leaves that section empty. Results are cached for ~1s so several browser tabs
polling at once do not fork nvidia-smi per request.

``GET /api/system/usage`` also answers in robot mode (``?robot=1`` /
``?format=toon``, src/robot_envelope.py) — the GPU and model rows are exactly
the tabular shape TOON collapses to one header plus one line per row. A call
without those query parameters answers as it always did.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request

from src import gpu_placement, nvidia_drs, vram_fit
from src import gpu_shared_memory
from src import robot_envelope as robot
from core.middleware import require_admin
from src.auth_helpers import require_user

logger = logging.getLogger(__name__)

_CACHE_TTL = 1.0
_cache: Dict[str, Any] = {"ts": 0.0, "data": None}
_cache_lock = asyncio.Lock()

# The driver policy changes when a human opens the Control Panel, not on a
# timer — reading it on every poll would be silly.
_POLICY_TTL = 300.0
_policy_cache: Dict[str, Any] = {"ts": 0.0, "data": None}


def _ollama_base() -> str:
    base = os.getenv("OLLAMA_BASE_URL") or os.getenv("OLLAMA_HOST") or ""
    base = base.strip()
    if not base:
        host = (os.getenv("LLM_HOST") or "127.0.0.1").strip() or "127.0.0.1"
        base = f"http://{host}:11434"
    if not base.startswith("http"):
        base = "http://" + base
    return base.rstrip("/")


async def _collect_ollama(client: httpx.AsyncClient) -> Dict[str, Any]:
    base = _ollama_base()
    out: Dict[str, Any] = {"reachable": False, "base": base, "models": []}
    try:
        r = await client.get(f"{base}/api/ps", timeout=2.5)
        if r.status_code != 200:
            out["error"] = f"HTTP {r.status_code}"
            return out
        data = r.json()
        out["reachable"] = True
        for m in data.get("models") or []:
            size = int(m.get("size") or 0)
            vram = int(m.get("size_vram") or 0)
            gpu_pct = round(100.0 * vram / size) if size else 0
            details = m.get("details") or {}
            out["models"].append({
                "name": m.get("name") or m.get("model"),
                "size": size,
                "size_vram": vram,
                "gpu_pct": gpu_pct,
                "cpu_pct": max(0, 100 - gpu_pct) if size else 0,
                "context_length": m.get("context_length"),
                "expires_at": m.get("expires_at"),
                "parameter_size": details.get("parameter_size"),
                "quantization": details.get("quantization_level"),
                "family": details.get("family"),
            })
    except (httpx.HTTPError, ValueError) as e:
        out["error"] = str(e)[:200]
    return out


_NVSMI_FIELDS = [
    "index", "name", "utilization.gpu", "memory.used", "memory.total",
    "temperature.gpu", "power.draw", "power.limit",
    # Appended, so the columns above keep their positions: the uuid / bus id
    # are what nvidia-smi's compute-apps listing names a card by.
    "uuid", "pci.bus_id", "memory.free",
]


def _num(v: str) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_gpu_query(stdout: str) -> List[Dict[str, Any]]:
    """Rows of ``--query-gpu=<_NVSMI_FIELDS>`` → one dict per card (MiB,
    °C, W), with empty ``models`` / ``runner_pids`` for the placement pass
    to fill in."""
    gpus: List[Dict[str, Any]] = []
    for line in (stdout or "").splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < len(_NVSMI_FIELDS):
            continue
        mem_total = _num(parts[4])
        mem_used = _num(parts[3])
        mem_free = _num(parts[10])
        if mem_free is None and mem_total is not None and mem_used is not None:
            mem_free = max(0.0, mem_total - mem_used)
        gpus.append({
            "index": int(_num(parts[0]) or 0),
            "name": parts[1],
            "util": _num(parts[2]),
            "mem_used": mem_used,
            "mem_total": mem_total,
            "temp": _num(parts[5]),
            "power": _num(parts[6]),
            "power_limit": _num(parts[7]),
            "uuid": parts[8],
            "bus_id": parts[9],
            "mem_free": mem_free,
            "models": [],
            "runner_pids": [],
        })
    return gpus


def _collect_gpu() -> tuple[List[Dict[str, Any]], Optional[str]]:
    exe = shutil.which("nvidia-smi")
    if not exe:
        # Standard Windows install location when PATH does not include it.
        for cand in (
            r"C:\Windows\System32\nvidia-smi.exe",
            r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe",
        ):
            if os.path.exists(cand):
                exe = cand
                break
    if not exe:
        return [], "nvidia-smi: not found"
    try:
        proc = subprocess.run(
            [exe, f"--query-gpu={','.join(_NVSMI_FIELDS)}", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=4,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return [], f"nvidia-smi: {e}"
    if proc.returncode != 0:
        return [], f"nvidia-smi exit {proc.returncode}: {(proc.stderr or '').strip()[:200]}"
    return parse_gpu_query(proc.stdout or ""), None


def gpu_pool(gpus: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The cards as one: memory and power summed, utilisation and temperature
    at their maximum (the pill's busy dot and the hot-card warning care about
    the worst card, not the average — though the average is there too).
    ``{}`` without a GPU."""
    if not gpus:
        return {}

    def _sum(key: str) -> Optional[float]:
        vals = [g.get(key) for g in gpus if g.get(key) is not None]
        return float(sum(vals)) if vals else None

    def _max(key: str) -> Optional[float]:
        vals = [g.get(key) for g in gpus if g.get(key) is not None]
        return float(max(vals)) if vals else None

    utils = [g.get("util") for g in gpus if g.get("util") is not None]
    mem_total = _sum("mem_total")
    mem_used = _sum("mem_used")
    mem_free = _sum("mem_free")
    if mem_free is None and mem_total is not None and mem_used is not None:
        mem_free = max(0.0, mem_total - mem_used)
    return {
        "count": len(gpus),
        "mem_used": mem_used,
        "mem_total": mem_total,
        "mem_free": mem_free,
        "util": _max("util"),
        "util_avg": round(sum(utils) / len(utils), 1) if utils else None,
        "power": _sum("power"),
        "power_limit": _sum("power_limit"),
        "temp": _max("temp"),
        "names": [str(g.get("name") or "") for g in gpus],
        "name": gpu_shared_memory.pool_name([str(g.get("name") or "") for g in gpus]),
    }


def _merge_placement(ollama: Dict[str, Any], gpus: List[Dict[str, Any]], report: Dict[str, Any]) -> None:
    """Write src/gpu_placement's answer onto the ollama rows and the cards."""
    models = (report or {}).get("models") or {}
    for m in ollama.get("models") or []:
        info = models.get(m.get("name")) or {}
        m["gpus"] = list(info.get("gpus") or [])
        m["placement"] = info.get("placement") or ("cpu" if not int(m.get("size_vram") or 0) else "unknown")
        m["per_gpu"] = [dict(p) for p in (info.get("per_gpu") or [])]
    per_gpu = (report or {}).get("gpus") or {}
    for g in gpus:
        slot = per_gpu.get(g.get("index")) or {}
        g["models"] = [dict(x) for x in (slot.get("models") or [])]
        g["runner_pids"] = list(slot.get("runner_pids") or [])


def _collect_host() -> Dict[str, Any]:
    out: Dict[str, Any] = {"cpu": {}, "ram": {}}
    try:
        import psutil
        out["cpu"] = {"percent": psutil.cpu_percent(interval=None), "count": psutil.cpu_count()}
        vm = psutil.virtual_memory()
        out["ram"] = {"used": int(vm.used), "total": int(vm.total), "percent": vm.percent}
    except Exception as e:  # psutil missing or failing
        out["error"] = f"psutil: {e}"
    return out


def _collect_policy() -> Dict[str, Any]:
    now = time.time()
    if _policy_cache["data"] is not None and now - _policy_cache["ts"] < _POLICY_TTL:
        return _policy_cache["data"]
    data = nvidia_drs.status()
    _policy_cache["ts"] = now
    _policy_cache["data"] = data
    return data


async def collect_usage() -> Dict[str, Any]:
    async with _cache_lock:
        now = time.time()
        if _cache["data"] is not None and now - _cache["ts"] < _CACHE_TTL:
            return _cache["data"]
        errors: List[str] = []
        async with httpx.AsyncClient() as client:
            ollama_task = _collect_ollama(client)
            gpu_task = asyncio.to_thread(_collect_gpu)
            host_task = asyncio.to_thread(_collect_host)
            shared_task = asyncio.to_thread(gpu_shared_memory.collect)
            policy_task = asyncio.to_thread(_collect_policy)
            ollama, (gpus, gpu_err), host, gpu_mem, policy = await asyncio.gather(
                ollama_task, gpu_task, host_task, shared_task, policy_task
            )
        # Placement needs both answers (the loaded models and the cards), so
        # it runs after the gather; it is its own 2 s cache and never raises.
        report: Dict[str, Any] = {}
        if ollama.get("models") and gpus:
            report = await asyncio.to_thread(gpu_placement.report, ollama.get("base", ""), ollama["models"], gpus)
        _merge_placement(ollama, gpus, report)
        # Runners no Ollama server owns any more (a restart leaves them
        # behind) — they hold VRAM every other gauge files under "other".
        orphans: List[Dict[str, Any]] = []
        if gpus:
            orphans = await asyncio.to_thread(gpu_placement.orphan_runners, gpus)
        if gpu_err:
            errors.append(gpu_err)
        if ollama.get("error"):
            errors.append(f"ollama: {ollama['error']}")
        if host.get("error"):
            errors.append(host["error"])
        data = {
            "ts": now,
            "ollama": ollama,
            "gpu": gpus,
            "gpu_pool": gpu_pool(gpus),
            "orphans": orphans,
            "gpu_mem": gpu_mem,
            "sysmem_fallback": policy,
            "cpu": host.get("cpu", {}),
            "ram": host.get("ram", {}),
            "errors": errors,
        }
        _cache["ts"] = now
        _cache["data"] = data
        return data


async def _model_show(client: httpx.AsyncClient, model: str) -> Dict[str, Any]:
    r = await client.post(f"{_ollama_base()}/api/show", json={"model": model}, timeout=8)
    if r.status_code != 200:
        raise HTTPException(r.status_code, f"ollama /api/show: HTTP {r.status_code}")
    return r.json()


async def _file_size(client: httpx.AsyncClient, model: str) -> int:
    """Size of the model on disk — the weights, without the KV cache.

    Exact tag match only. Matching on the base name would happily hand back
    `qwen3.8:27b-q8_0`'s 28 GB when asked about `qwen3.8:27b-q4_K_M`, and the
    whole fit calculation is built on this number.
    """
    try:
        r = await client.get(f"{_ollama_base()}/api/tags", timeout=6)
        if r.status_code != 200:
            return 0
        short = model.split("/")[-1]
        wanted = {model, short}
        if ":" not in short:
            wanted.add(f"{short}:latest")
        for m in r.json().get("models") or []:
            if (m.get("name") or "") in wanted:
                return int(m.get("size") or 0)
    except (httpx.HTTPError, ValueError):
        pass
    return 0


async def collect_fit(model: str, target_ctx: Optional[int] = None) -> Dict[str, Any]:
    """Everything `vram_fit.plan` needs, gathered from the live system.

    Against the POOL: Ollama splits a model across the cards when it fits no
    single one, so the budget is every card's memory, less a CUDA context
    per card."""
    usage = await collect_usage()
    pool = usage.get("gpu_pool") or gpu_pool(usage.get("gpu") or [])
    if not pool or not pool.get("mem_total"):
        raise HTTPException(503, "no NVIDIA GPU visible to nvidia-smi")
    count = int(pool.get("count") or 1)
    async with httpx.AsyncClient() as client:
        show = await _model_show(client, model)
        file_size = await _file_size(client, model)
    info = show.get("model_info") or {}
    arch = str(info.get("general.architecture") or "")
    n_layers = int(info.get(f"{arch}.block_count") or 0)
    max_ctx = int(info.get(f"{arch}.context_length") or 0) or None

    loaded = None
    for m in (usage.get("ollama") or {}).get("models") or []:
        if m.get("name") in (model, model.split("/")[-1]):
            loaded = m
            break

    kv_per_token = None
    kv_source = "unknown"
    kv_note = ""
    if loaded and loaded.get("context_length"):
        kv_per_token = vram_fit.kv_bytes_per_token_measured(
            int(loaded.get("size") or 0), file_size, int(loaded["context_length"])
        )
        if kv_per_token:
            kv_source = "measured"
            kv_note = "measured from the loaded model (ollama ps minus the file on disk)"
    if kv_per_token is None:
        kv_per_token, kv_note = vram_fit.kv_bytes_per_token_estimated(info)
        kv_source = "estimated" if kv_per_token else "unknown"
    kv_reliable = kv_source == "measured" or (
        kv_source == "estimated" and "upper bound" not in kv_note
    )

    total_bytes = int((pool.get("mem_total") or 0) * 1024 * 1024)
    used_bytes = int((pool.get("mem_used") or 0) * 1024 * 1024)
    # What Ollama itself holds does not count as "someone else's". Take that
    # from `ollama ps` rather than the WDDM counter: the counter reports a
    # commitment that can outlive what is actually resident on the card.
    ours = sum(int(m.get("size_vram") or 0) for m in (usage.get("ollama") or {}).get("models") or [])
    others = max(0, used_bytes - ours)

    result = vram_fit.plan(
        vram_total_bytes=total_bytes,
        vram_used_by_others_bytes=others,
        file_size_bytes=file_size,
        n_layers=n_layers,
        kv_bytes_per_token=kv_per_token,
        kv_source=kv_source,
        kv_reliable=kv_reliable,
        current_ctx=int(loaded.get("context_length")) if loaded and loaded.get("context_length") else None,
        target_ctx=target_ctx,
        max_ctx=max_ctx,
        reserve_bytes=vram_fit.DEFAULT_RESERVE_BYTES * count,
    )
    result["model"] = model
    result["kv_note"] = kv_note
    result["loaded"] = bool(loaded)
    result["gpu_name"] = pool.get("name") or gpu_shared_memory.pool_name(pool.get("names") or [])
    result["gpu_count"] = count
    return result


def setup_system_usage_routes() -> APIRouter:
    router = APIRouter(prefix="/api/system", tags=["system"])

    @router.get("/usage")
    async def usage(request: Request):
        async def payload():
            require_user(request)
            try:
                return await collect_usage()
            except Exception as e:
                logger.warning("usage collection failed: %s", e)
                raise HTTPException(500, "usage collection failed")
        # Robot mode (?robot=1 / ?format=toon): the same numbers in the standard
        # envelope, for a coordinator deciding whether this machine has room.
        if robot.wants(request):
            return await robot.reply(request, payload)
        return await payload()

    @router.post("/gpu/orphans/release")
    async def release_orphan_runner(request: Request):
        """Kill ONE orphaned model runner (`{"pid": N}`) — a llama-server the
        Ollama server no longer owns, still holding VRAM. The pid is re-checked
        against the live orphan list at call time: never an arbitrary process,
        never a runner Ollama still owns. Admin only; off-limits to app_api."""
        require_admin(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            pid = int((body or {}).get("pid"))
        except (TypeError, ValueError):
            raise HTTPException(422, "pid is required")
        result = await asyncio.to_thread(gpu_placement.release_orphan, pid)
        if not result.get("ok"):
            raise HTTPException(409, result.get("reason") or "could not release the runner")
        _cache["ts"] = 0.0
        _cache["data"] = None
        return result

    @router.get("/gpu/policy")
    async def gpu_policy(request: Request):
        """CUDA sysmem fallback: what the driver exposes, and how to change it."""
        require_user(request)
        return await asyncio.to_thread(nvidia_drs.status)

    @router.post("/gpu/policy/open")
    async def gpu_policy_open(request: Request):
        """Open the NVIDIA Control Panel on the machine running Faustus."""
        require_user(request)
        return await asyncio.to_thread(nvidia_drs.open_control_panel)

    @router.get("/vram-fit")
    async def vram_fit_route(request: Request, model: str, target_ctx: int = 0):
        """How to make `model` fit on the card instead of spilling."""
        require_user(request)
        if not model.strip():
            raise HTTPException(422, "model is required")
        try:
            return await collect_fit(model.strip(), target_ctx or None)
        except HTTPException:
            raise
        except httpx.HTTPError as e:
            raise HTTPException(502, f"ollama unreachable: {e}")
        except Exception as e:
            logger.warning("vram fit failed: %s", e)
            raise HTTPException(500, "vram fit failed")

    @router.get("/ollama/model/{model_name:path}")
    async def ollama_model_info(request: Request, model_name: str):
        """Model card from Ollama (`/api/show`): parameters, context window,
        capabilities (tools / thinking) — feeds the model-controls popover."""
        require_user(request)
        base = _ollama_base()
        try:
            async with httpx.AsyncClient() as client:
                r = await client.post(f"{base}/api/show", json={"model": model_name}, timeout=6)
            if r.status_code != 200:
                raise HTTPException(r.status_code, f"ollama /api/show: HTTP {r.status_code}")
            data = r.json()
        except httpx.HTTPError as e:
            raise HTTPException(502, f"ollama unreachable: {e}")
        info = data.get("model_info") or {}
        ctx = None
        for k, v in info.items():
            if k.endswith(".context_length"):
                ctx = v
                break
        params_raw = data.get("parameters") or ""
        params: Dict[str, str] = {}
        for line in params_raw.splitlines():
            bits = line.split(None, 1)
            if len(bits) == 2:
                params.setdefault(bits[0], bits[1].strip().strip('"'))
        details = data.get("details") or {}
        return {
            "name": model_name,
            "capabilities": data.get("capabilities") or [],
            "context_length": ctx,
            "parameters": params,
            "parameter_size": details.get("parameter_size"),
            "quantization": details.get("quantization_level"),
            "family": details.get("family"),
        }

    return router
