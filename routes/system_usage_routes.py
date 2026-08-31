"""System usage API — what `ollama ps` + `nvidia-smi` + a RAM/CPU gauge show,
as one JSON document the chat UI can poll while a local model generates.

GET /api/system/usage
{
  "ts": 1725000000.0,
  "ollama": {"reachable": true, "base": "http://127.0.0.1:11434",
             "models": [{"name", "size", "size_vram", "gpu_pct", "cpu_pct",
                         "context_length", "expires_at", "parameter_size",
                         "quantization"}]},
  "gpu": [{"index", "name", "util", "mem_used", "mem_total", "temp",
           "power", "power_limit"}],           # MiB / °C / W, from nvidia-smi
  "gpu_mem": {"supported": true,               # Windows WDDM counters
              "ollama": {"shared": 0, "dedicated": 7.6e9, "spilling": false}},
  "sysmem_fallback": {"exposed": false, "manual_only": true, "steps": [...]},
  "cpu": {"percent": 12.5, "count": 32},
  "ram": {"used": 40.1e9, "total": 137.0e9, "percent": 29.3},
  "errors": ["nvidia-smi: not found"]        # non-fatal collection problems
}

`gpu` is the card's own VRAM; `gpu_mem` is the part nvidia-smi cannot see —
system RAM the driver paged GPU allocations into over PCIe. Ollama filling
that up is a ~20x slowdown with every other gauge still reading green, so the
widget calls it out. See src/gpu_shared_memory.py for why.

Everything is best-effort: a missing nvidia-smi or an unreachable Ollama just
leaves that section empty. Results are cached for ~1s so several browser tabs
polling at once do not fork nvidia-smi per request.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request

from src import gpu_shared_memory, nvidia_drs, vram_fit
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
]


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
    gpus: List[Dict[str, Any]] = []
    for line in (proc.stdout or "").splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < len(_NVSMI_FIELDS):
            continue

        def num(v: str) -> Optional[float]:
            try:
                return float(v)
            except ValueError:
                return None

        gpus.append({
            "index": int(num(parts[0]) or 0),
            "name": parts[1],
            "util": num(parts[2]),
            "mem_used": num(parts[3]),
            "mem_total": num(parts[4]),
            "temp": num(parts[5]),
            "power": num(parts[6]),
            "power_limit": num(parts[7]),
        })
    return gpus, None


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
    """Size of the model on disk — the weights, without the KV cache."""
    try:
        r = await client.get(f"{_ollama_base()}/api/tags", timeout=6)
        if r.status_code != 200:
            return 0
        short = model.split("/")[-1]
        for m in r.json().get("models") or []:
            name = m.get("name") or ""
            if name in (model, short) or name.split(":")[0] == short.split(":")[0]:
                return int(m.get("size") or 0)
    except (httpx.HTTPError, ValueError):
        pass
    return 0


async def collect_fit(model: str, target_ctx: Optional[int] = None) -> Dict[str, Any]:
    """Everything `vram_fit.plan` needs, gathered from the live system."""
    usage = await collect_usage()
    gpu = (usage.get("gpu") or [None])[0]
    if not gpu or not gpu.get("mem_total"):
        raise HTTPException(503, "no NVIDIA GPU visible to nvidia-smi")
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

    total_bytes = int((gpu.get("mem_total") or 0) * 1024 * 1024)
    used_bytes = int((gpu.get("mem_used") or 0) * 1024 * 1024)
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
        current_ctx=int(loaded.get("context_length")) if loaded and loaded.get("context_length") else None,
        target_ctx=target_ctx,
        max_ctx=max_ctx,
    )
    result["model"] = model
    result["kv_note"] = kv_note
    result["loaded"] = bool(loaded)
    result["gpu_name"] = gpu.get("name")
    return result


def setup_system_usage_routes() -> APIRouter:
    router = APIRouter(prefix="/api/system", tags=["system"])

    @router.get("/usage")
    async def usage(request: Request):
        require_user(request)
        try:
            return await collect_usage()
        except Exception as e:
            logger.warning("usage collection failed: %s", e)
            raise HTTPException(500, "usage collection failed")

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
