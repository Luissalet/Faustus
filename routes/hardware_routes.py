"""routes/hardware_routes.py — INF-05 §11/§14 A5: physical GPU topology,
per-GPU memory budgets, manual transport annotations, and the three
separately-tracked context limits. Registered in `app.py` next to
`benchmark_routes` (CONTRATO_INF05 Lote A).

Four endpoints, all read-only except the annotation one, none of which
loads a model, starts a benchmark, or restarts a process:

  GET  /api/hardware/topology            — physical GPU inventory +
                                            reconciliation against the most
                                            recently captured hardware
                                            profile (§11 T15: an index is
                                            never identity).
  GET  /api/hardware/budget              — the desegregated per-GPU memory
                                            picture (`src.memory_budget.
                                            physical_budgets`), plus a
                                            candidate-load estimate when
                                            enough is known to build one.
  POST /api/hardware/topology/annotate   — a person's manual transport call
                                            for one physical GPU, stored on
                                            a hardware profile.
  GET  /api/hardware/context-limits      — native (model metadata) /
                                            configured (the running engine)
                                            / evaluated (a benchmark run) —
                                            never collapsed into one number.

Errors are flat `{"error", "error_class"}` bodies (`hardware.*`), same
convention `routes/inference_routes.py` already uses. Owner-scoped like
Cookbook (`require_admin`) — this reads/annotates process-launch and
hardware state, not the caller's own data.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core.middleware import require_admin
from src import gpu_placement, gpu_shared_memory, gpu_topology, hardware_profiles
from src import launch_receipts, memory_budget, model_architecture, model_load_options
from src import vram_admission, vram_fit
from src.contracts.base import ContractError
from src.contracts.inference import (
    ConfiguredContextLimit, ContextLimits, EvaluatedContextLimit, NativeContextLimit,
    TRANSPORT_KINDS,
)

logger = logging.getLogger(__name__)


def _error(status: int, message: str, error_class: str, **extra: Any) -> JSONResponse:
    body: Dict[str, Any] = {"error": message, "error_class": error_class}
    body.update(extra)
    return JSONResponse(status_code=status, content=body)


class AnnotateRequest(BaseModel):
    gpu_key: str
    kind: str
    note: str = ""
    profile_id: Optional[str] = None


def _most_recent_profile() -> Optional[Dict[str, Any]]:
    profiles = hardware_profiles.list_profiles()
    return profiles[0] if profiles else None


def _residents_and_placement(endpoint: str):
    """`/api/ps` residents + `gpu_placement.placement(...)` for `endpoint`,
    when it names an Ollama this machine can gate — `([], {})` for anything
    else (a llama.cpp/vLLM/remote endpoint, or none given at all). Never
    raises: an unreachable Ollama just means no residents to attribute."""
    root = vram_admission.ollama_root(endpoint) if endpoint else None
    if not root and not endpoint:
        # Servers › Physical GPUs asks with no endpoint at all: attribute the
        # residents of every Ollama declared for THIS machine (seen live
        # 12-09-2026: with no endpoint the resident 27B landed in "other
        # processes (observed)", which is both wrong and mislabelled).
        try:
            from routes.local_models_routes import list_ollama_endpoints
            roots = [ep["root"] for ep in list_ollama_endpoints() if ep.get("same_machine")]
        except Exception as e:  # noqa: BLE001
            logger.debug("hardware_routes: endpoint discovery failed: %s", e)
            roots = []
        residents_all: list = []
        placement_all: dict = {}
        first_root = None
        for r in roots:
            res, pl, _ = _residents_and_placement(r + "/v1")
            if res:
                first_root = first_root or r
                residents_all.extend(res)
                placement_all.update(pl)
        return residents_all, placement_all, first_root
    if not root:
        return [], {}, root
    try:
        residents = (vram_admission._get(root, "/api/ps", 2.5) or {}).get("models") or []
    except Exception as e:  # noqa: BLE001
        logger.debug("hardware_routes: /api/ps for %s failed: %s", root, e)
        return [], {}, root
    vram = gpu_shared_memory.vram_snapshot()
    gpus = vram.get("gpus") if vram.get("supported") else None
    try:
        placement = gpu_placement.placement(root, residents, gpus)
    except Exception as e:  # noqa: BLE001
        logger.debug("hardware_routes: placement for %s failed: %s", root, e)
        placement = {}
    return residents, placement, root


def setup_hardware_routes() -> APIRouter:
    router = APIRouter(tags=["hardware"])

    @router.get("/api/hardware/topology")
    async def get_topology(request: Request, host: str = "", ssh_port: str = ""):
        require_admin(request)
        snap = gpu_topology.snapshot(host=host, ssh_port=ssh_port)
        profile = _most_recent_profile()
        if profile is not None:
            snap = gpu_topology.apply_annotations(snap, profile)
        reconciliation = (
            hardware_profiles.reconciliation_against(profile, snap) if profile is not None else []
        )
        return {
            "snapshot": snap.to_dict(),
            "reconciliation": reconciliation,
            "profile_id": profile.get("id") if profile is not None else None,
        }

    @router.post("/api/hardware/topology/annotate")
    async def annotate_topology(request: Request, req: AnnotateRequest):
        require_admin(request)
        if req.kind not in TRANSPORT_KINDS:
            return _error(400, f"kind must be one of {list(TRANSPORT_KINDS)}", "hardware.invalid_annotation")
        by = str(getattr(request.state, "current_user", "") or "")
        try:
            profile = gpu_topology.annotate_transport(
                req.profile_id or "", req.gpu_key, req.kind, req.note, by,
            )
        except ContractError as e:
            return _error(400, str(e), "hardware.invalid_annotation")
        except ValueError as e:
            return _error(400, str(e), "hardware.invalid_annotation")
        return {"profile": profile}

    @router.get("/api/hardware/budget")
    async def get_budget(
        request: Request, endpoint: str = "", model: str = "",
        ctx: Optional[int] = None, slots: int = 1, weights_bytes: Optional[int] = None,
    ):
        require_admin(request)
        snap = gpu_topology.snapshot()
        vram = gpu_shared_memory.vram_snapshot()
        residents, placement, root = _residents_and_placement(endpoint)
        receipts = launch_receipts.live_receipts()
        wddm = gpu_shared_memory.collect()

        budgets = memory_budget.physical_budgets(
            snapshot=snap, vram=vram, placement=placement, residents=residents,
            receipts=receipts, wddm=wddm,
        )

        estimate = None
        verdict: Dict[str, Any] = {"verdict": "unknown", "reason": "no candidate estimate requested",
                                   "shortfall_bytes": None}
        if weights_bytes is not None or model:
            arch: Optional[Dict[str, Any]] = None
            kv_observations = []
            if model:
                try:
                    ma = model_architecture.get_model_architecture(model)
                    arch = {"kind": ma.get("kind") or "unknown"}
                except Exception as e:  # noqa: BLE001
                    logger.debug("hardware_routes: architecture lookup for %s failed: %s", model, e)
                key = model
                if root:
                    for tag in vram_admission._get(root, "/api/tags", 3.0).get("models", []) if root else []:
                        if str(tag.get("name") or "") == model and tag.get("digest"):
                            key = str(tag["digest"])
                            break
                rate = vram_fit.KV_RATES.get(key)
                if rate:
                    kv_observations = [{"per_token": rate.get("per_token"), "ctx": rate.get("ctx")}]
            estimate = memory_budget.estimate_candidate(
                weights_bytes=weights_bytes, arch=arch, ctx=ctx, slots=max(1, int(slots or 1)),
                kv_observations=kv_observations,
                engine_implementation="ollama" if root else "unknown",
            )
            if budgets:
                best_key = max(
                    budgets, key=lambda k: (budgets[k].components.free.bytes
                                            if budgets[k].components.free.bytes is not None else -1),
                )
                verdict = memory_budget.admissible(budgets[best_key], estimate)
            else:
                verdict = {"verdict": "unknown", "reason": "no physical GPU reading available",
                          "shortfall_bytes": None}

        return {
            "budgets": {key: b.to_dict() for key, b in budgets.items()},
            "estimate": estimate.to_dict() if estimate is not None else None,
            "verdict": verdict,
            "system_memory": memory_budget.system_memory(),
        }

    @router.get("/api/hardware/context-limits")
    async def get_context_limits(request: Request, endpoint: str = "", model: str = "", profile_id: str = ""):
        require_admin(request)

        native = NativeContextLimit()
        if model:
            try:
                ma = model_architecture.get_model_architecture(model)
                if ma.get("native_context") is not None:
                    native = NativeContextLimit(
                        value=int(ma["native_context"]),
                        source=ma.get("source") if ma.get("source") in ("hf_config", "ollama_show") else "absent",
                        note=str(ma.get("context_note") or ""),
                    )
            except Exception as e:  # noqa: BLE001
                logger.debug("hardware_routes: native context lookup for %s failed: %s", model, e)

        configured = ConfiguredContextLimit()
        try:
            host = port = None
            if endpoint:
                parsed = urlparse(endpoint)
                host, port = parsed.hostname, parsed.port
            receipt = launch_receipts.find_by_endpoint(host, port) if host and port else None
        except Exception as e:  # noqa: BLE001
            logger.debug("hardware_routes: receipt lookup for %s failed: %s", endpoint, e)
            receipt = None
        if receipt is not None:
            observed_ctx = (receipt.observed or {}).get("ctx")
            if observed_ctx is None:
                observed_ctx = (receipt.observed or {}).get("num_ctx")
            if observed_ctx is not None:
                try:
                    configured = ConfiguredContextLimit(value=int(observed_ctx), source="receipt", note="")
                except (TypeError, ValueError):
                    pass
        if configured.value is None and endpoint and model:
            try:
                saved = model_load_options.resolve_for_request(endpoint, model)
            except Exception as e:  # noqa: BLE001
                logger.debug("hardware_routes: load-options lookup failed: %s", e)
                saved = {}
            if saved.get("num_ctx") is not None:
                configured = ConfiguredContextLimit(
                    value=int(saved["num_ctx"]), source="load_options", note="",
                )
        if configured.value is None:
            configured = ConfiguredContextLimit(value=None, source="absent", note="engine default not observed")

        evaluated = EvaluatedContextLimit()
        try:
            from src.bench import runner as bench_runner
            max_prompt: Optional[int] = None
            n_runs = 0
            for run in bench_runner.list_runs(limit=200):
                eng = run.profile.engine
                mdl = run.profile.model
                if model and mdl and mdl.artifact_id != model:
                    continue
                if endpoint:
                    parsed = urlparse(endpoint)
                    if parsed.hostname and eng.host and eng.host != parsed.hostname:
                        continue
                    if parsed.port and eng.port and eng.port != parsed.port:
                        continue
                run_has_sample = False
                for sample in run.samples:
                    if sample.metrics is None:
                        continue
                    value = sample.metrics.tokens.prompt.value
                    if value is None:
                        continue
                    run_has_sample = True
                    max_prompt = int(value) if max_prompt is None else max(max_prompt, int(value))
                if run_has_sample:
                    n_runs += 1
            if max_prompt is not None:
                evaluated = EvaluatedContextLimit(
                    min=max_prompt, max=max_prompt, source="bench_runs",
                    note=f"largest observed prompt across {n_runs} run(s)",
                )
        except Exception as e:  # noqa: BLE001
            logger.debug("hardware_routes: evaluated context lookup failed: %s", e)

        limits = ContextLimits(native=native, configured=configured, evaluated=evaluated)
        return {"limits": limits.to_dict()}

    return router
