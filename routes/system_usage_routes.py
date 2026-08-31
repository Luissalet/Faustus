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
  "cpu": {"percent": 12.5, "count": 32},
  "ram": {"used": 40.1e9, "total": 137.0e9, "percent": 29.3},
  "errors": ["nvidia-smi: not found"]        # non-fatal collection problems
}

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

from src.auth_helpers import require_user

logger = logging.getLogger(__name__)

_CACHE_TTL = 1.0
_cache: Dict[str, Any] = {"ts": 0.0, "data": None}
_cache_lock = asyncio.Lock()


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
            ollama, (gpus, gpu_err), host = await asyncio.gather(ollama_task, gpu_task, host_task)
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
            "cpu": host.get("cpu", {}),
            "ram": host.get("ram", {}),
            "errors": errors,
        }
        _cache["ts"] = now
        _cache["data"] = data
        return data


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
