"""src/memory_budget.py — INF-05 §11 A3: memory, desegregated, per physical
GPU — never one opaque "used" number.

Two questions, kept apart on purpose:

  `physical_budgets`     "what is on this card RIGHT NOW" — one
                          `MemoryBudget` per PHYSICAL GPU (keyed by
                          `src.contracts.inference.identity_key`, never an
                          index), built from readings the caller already
                          took (`gpu_topology.snapshot`, `gpu_shared_memory.
                          vram_snapshot`, `gpu_placement.placement`,
                          `/api/ps`, live `LaunchReceipt`s). Two endpoints
                          on one physical card (§11 T14, e.g. Ollama and a
                          Faustus-managed `llama-server`) fold into ONE
                          budget with two consumers, never two budgets.

  `estimate_candidate`   "what would loading THIS model cost" — a forecast,
                          not a reading. Reuses `vram_fit`'s KV-cost
                          arithmetic and its measurement-vs-metadata
                          distinction, but never claims precision the
                          inputs do not support: an unknown architecture
                          with no measured overhead comes back `complete:
                          False, basis: "incomplete"`, not a number.

Everything here is pure arithmetic over dicts/dataclasses the caller already
gathered — no `nvidia-smi`, no HTTP, no model load — except `system_memory()`,
the one function this module's docstring is allowed to call "impure": it
reads `psutil`/`GlobalMemoryStatusEx` and nothing else.

What this module deliberately does NOT do yet (see the CONTRATO_INF05 Lote A
report for the full list): split a resident model's own footprint into
weights vs. KV vs. buffers per GPU — that needs each resident's on-disk file
size (not part of `/api/ps`) paired with a KV rate, which `physical_budgets`'
inputs do not carry. Those three `MemoryComponents` stay `absent`, with a
note, rather than a number nobody actually measured; the per-model TOTAL
bytes are still there, in `consumers`.
"""
from __future__ import annotations

import logging
import math
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from src import vram_fit
from src.contracts.inference import (
    CandidateEstimate, EstimateValidity, GpuInfo, HardwareSnapshot,
    MemoryBudget, MemoryComponent, MemoryComponents, MemoryConsumer,
    identity_key,
)

logger = logging.getLogger(__name__)

DEFAULT_MAX_AGE_S = 30.0


# ── physical_budgets: what is on the card right now ─────────────────────────

def _iso_age_s(observed_at: Optional[str], *, now: float) -> Optional[float]:
    if not observed_at:
        return None
    try:
        dt = datetime.fromisoformat(str(observed_at).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, now - dt.timestamp())
    except (ValueError, OverflowError, OSError):
        return None


def _physical_gpu_rows(snapshot: Optional[HardwareSnapshot], vram: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One row per physical GPU this reading covers — index, physical
    identity (`None` when neither uuid nor bus_id is known — an index is
    NEVER identity), name, and the totals `vram_shared_memory.vram_snapshot`
    is the only source of (it reads real `memory.used`; `gpu_topology`'s
    reading may be a stale/separate `nvidia-smi` call and is used here only
    for the richer identity, when it has one for this index)."""
    snap_by_index: Dict[int, GpuInfo] = {g.index: g for g in (snapshot.gpus if snapshot is not None else ())}
    vram_gpus = (vram or {}).get("gpus") if isinstance(vram, dict) else None
    rows: List[Dict[str, Any]] = []
    if vram_gpus:
        for vg in vram_gpus:
            try:
                idx = int(vg.get("index") or 0)
            except (TypeError, ValueError):
                continue
            sg = snap_by_index.get(idx)
            if sg is not None:
                key = identity_key(sg)
                name = sg.name or str(vg.get("name") or "")
            else:
                uuid = str(vg.get("uuid") or "") or None
                key = identity_key(GpuInfo(index=idx, name=str(vg.get("name") or ""), uuid=uuid))
                name = str(vg.get("name") or "")
            total = vg.get("total")
            used = vg.get("used")
            rows.append({
                "index": idx, "gpu_key": key, "name": name,
                "total_bytes": int(total) if total is not None else None,
                "used_bytes": int(used) if used is not None else None,
            })
        return rows
    # No `vram_snapshot` at all: fall back to the topology reading alone —
    # identity and a nameplate total, but no `used`/`free` (nvidia-smi's
    # `memory.used` is what vram_snapshot reads; topology's own
    # `memory.used` column is parsed but not carried onto `GpuInfo`).
    for idx, sg in sorted(snap_by_index.items()):
        rows.append({"index": idx, "gpu_key": identity_key(sg), "name": sg.name,
                     "total_bytes": sg.vram_bytes, "used_bytes": None})
    return rows


def _parse_gpu_indices(raw: Any) -> List[int]:
    if raw is None:
        return []
    if isinstance(raw, str):
        out = []
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                out.append(int(part))
            except ValueError:
                continue
        return out
    if isinstance(raw, (list, tuple)):
        out = []
        for item in raw:
            try:
                out.append(int(item))
            except (TypeError, ValueError):
                continue
        return out
    return []


def physical_budgets(
    *,
    snapshot: Optional[HardwareSnapshot],
    vram: Optional[Dict[str, Any]],
    placement: Optional[Dict[str, Dict[str, Any]]] = None,
    residents: Optional[List[Dict[str, Any]]] = None,
    receipts: Optional[Sequence[Any]] = None,
    wddm: Optional[Dict[str, Any]] = None,
    reserve_bytes: int = vram_fit.DEFAULT_RESERVE_BYTES,
    max_age_s: float = DEFAULT_MAX_AGE_S,
    now: Optional[float] = None,
) -> Dict[str, MemoryBudget]:
    """One `MemoryBudget` per PHYSICAL GPU `vram`/`snapshot` cover, keyed by
    `identity_key` (or, for a card neither reading can identify, a
    reading-local `"index:N"` string — never claimed as the budget's own
    `gpu_key`, which stays `None` there, honestly).

    `placement` (`gpu_placement.placement(...)`) attributes Ollama residents
    to a physical GPU by INDEX (the same index `vram`'s rows use — both come
    from the same `nvidia-smi` enumeration); `receipts` (`launch_receipts.
    live_receipts()`) attribute a Faustus-managed serve session by the GPU
    indices its own launch plan recorded (`plan["gpus"]`), with bytes only
    when the plan recorded `weights_bytes` — absent otherwise, never guessed.
    Two consumers landing on the same physical index (§11 T14: Ollama and a
    `llama-server` sharing one card) fold into the SAME budget by
    construction — there is exactly one `MemoryBudget` per row.
    """
    now = now if now is not None else time.time()
    rows = _physical_gpu_rows(snapshot, vram)
    if not rows:
        return {}

    residents_by_name = {
        str(r.get("name") or r.get("model") or ""): r for r in (residents or []) if isinstance(r, dict)
    }
    consumers_by_index: Dict[int, List[MemoryConsumer]] = {}
    for name, info in (placement or {}).items():
        per_gpu = (info or {}).get("per_gpu") or []
        if not per_gpu:
            continue
        pid = (info or {}).get("pid")
        residents_by_name.get(name)  # kept for symmetry/future use; not needed for bytes today
        for entry in per_gpu:
            idx_raw = entry.get("index")
            if idx_raw is None:
                continue
            idx = int(idx_raw)
            b = entry.get("bytes")
            consumers_by_index.setdefault(idx, []).append(MemoryConsumer(
                kind="ollama", label=name, pid=int(pid) if pid is not None else None,
                bytes=int(b) if b is not None else None,
                source="observed" if b is not None else "estimated",
            ))

    for receipt in (receipts or []):
        plan = getattr(receipt, "plan", None) or {}
        indices = _parse_gpu_indices(plan.get("gpus"))
        if not indices:
            continue  # no recorded placement: this session is not attributable to a physical GPU here
        weights_bytes = plan.get("weights_bytes")
        if isinstance(weights_bytes, (int, float)) and not isinstance(weights_bytes, bool) and weights_bytes > 0:
            bytes_each: Optional[int] = int(weights_bytes) // len(indices)
            source = "reported_engine"
        else:
            bytes_each = None
            source = "absent"
        session_id = str(getattr(receipt, "session_id", "") or "")
        label = f"faustus serve · {session_id}" if session_id else "faustus serve"
        for idx in indices:
            consumers_by_index.setdefault(idx, []).append(MemoryConsumer(
                kind="faustus_serve", label=label, pid=None, bytes=bytes_each, source=source,
            ))

    shared_component = MemoryComponent(bytes=None, source="absent", note="")
    if isinstance(wddm, dict) and wddm.get("supported"):
        shared_bytes = (wddm.get("ollama") or {}).get("shared")
        if shared_bytes is not None:
            shared_component = MemoryComponent(
                bytes=int(shared_bytes), source="observed",
                note="WDDM shared system memory attributed to the Ollama runner — "
                     "never VRAM, shown for visibility only (§11)",
            )

    observed_at = snapshot.observed_at if snapshot is not None else None
    age = _iso_age_s(observed_at, now=now)
    stale = bool(age is not None and age > max_age_s)

    out: Dict[str, MemoryBudget] = {}
    for row in rows:
        idx = row["index"]
        gpu_key = row["gpu_key"]
        consumers = consumers_by_index.get(idx, [])
        used_bytes = row["used_bytes"]
        total_bytes = row["total_bytes"]
        known_attributed = sum(c.bytes for c in consumers if c.bytes is not None)
        any_unknown = any(c.bytes is None for c in consumers)

        if used_bytes is None:
            other_processes = MemoryComponent(bytes=None, source="absent", note="")
        elif any_unknown:
            other_processes = MemoryComponent(
                bytes=max(0, used_bytes - known_attributed), source="estimated",
                note="one or more consumers on this GPU have no per-process byte reading "
                     "(Windows reports no per-process bytes); this total may double count them",
            )
        else:
            other_processes = MemoryComponent(bytes=max(0, used_bytes - known_attributed), source="observed", note="")

        free_bytes = max(0, total_bytes - used_bytes) if (total_bytes is not None and used_bytes is not None) else None
        free = MemoryComponent(bytes=free_bytes, source="observed" if free_bytes is not None else "absent", note="")

        margin = MemoryComponent(
            bytes=int(reserve_bytes), source="estimated",
            note="fixed reserve for CUDA context/compute buffers (vram_fit.DEFAULT_RESERVE_BYTES)",
        )
        split_note = ("per-GPU weights/KV split needs each resident's on-disk file size paired "
                     "with a KV rate — not part of this reading; see `consumers` for each "
                     "model/process's TOTAL attributed bytes")
        components = MemoryComponents(
            weights_resident=MemoryComponent(bytes=None, source="absent", note=split_note),
            kv_state=MemoryComponent(bytes=None, source="absent", note=split_note),
            buffers_runtime=MemoryComponent(bytes=None, source="absent", note=""),
            auxiliary_models=MemoryComponent(bytes=None, source="absent", note="not tracked in this reading"),
            other_processes=other_processes,
            system_margin=margin,
            free=free,
        )
        budget = MemoryBudget(
            gpu_key=gpu_key, gpu_index=idx, gpu_name=row["name"], total_bytes=total_bytes,
            observed_at=observed_at, components=components, consumers=tuple(consumers),
            shared_spill=shared_component, stale=stale,
        )
        out[gpu_key or f"index:{idx}"] = budget
    return out


# ── estimate_candidate: what loading a model would cost ────────────────────

def fit_overhead(observations: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Least-squares fixed+variable KV overhead from >= 2 `{per_token, ctx}`
    readings taken at DIFFERENT contexts (each one already a `vram_fit`
    bytes-per-token rate for that ctx). `fixed` is what does not scale with
    context — a hybrid model's constant-size recurrent state, mainly — and
    `per_token` is the variable part. Raises `ValueError` for fewer than 2
    usable points, or points that are all at the same context (nothing to
    fit a slope to): the caller decides what "incomplete" means in that
    case, this function does not silently return a flat line."""
    pts = [
        (float(o["ctx"]), float(o["per_token"]) * float(o["ctx"]))
        for o in observations if o.get("ctx") and o.get("per_token")
    ]
    if len(pts) < 2:
        raise ValueError("fit_overhead needs at least 2 usable observations")
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    n = len(pts)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    var_x = sum((x - mean_x) ** 2 for x in xs)
    if var_x == 0:
        raise ValueError("fit_overhead needs observations at different contexts")
    cov_xy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    slope = cov_xy / var_x
    intercept = mean_y - slope * mean_x
    residual = math.sqrt(sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys)) / n)
    return {"fixed": intercept, "per_token": slope,
           "ctx_min": int(min(xs)), "ctx_max": int(max(xs)), "residual": residual}


def estimate_candidate(
    *,
    weights_bytes: Optional[int],
    arch: Optional[Dict[str, Any]],
    ctx: Optional[int],
    slots: int = 1,
    kv_observations: Optional[Sequence[Dict[str, Any]]] = None,
    engine_implementation: str = "unknown",
    reserve_bytes: int = vram_fit.DEFAULT_RESERVE_BYTES,
) -> CandidateEstimate:
    """A forecast for loading one candidate, never a reading. `arch` is the
    compact shape `{"kind", "n_layers"?, "kv_per_token"?, "hybrid"?}` a
    caller derives from `model_architecture`/GGUF metadata — NOT the raw HF
    config; this function does not itself read metadata.

    §11: "una arquitectura desconocida produce una estimación INCOMPLETA, no
    falsa precisión" — with no `kv_observations` and no usable `arch`, the
    result is `complete=False, basis="incomplete"`, and `total_lower` is
    only weights+margin (never a KV guess dressed up as a number).
    """
    kv_observations = list(kv_observations or [])
    notes: List[str] = []
    kv_bytes: Optional[float] = None
    kv_source = "absent"
    kv_extrapolated = False
    kv_note_parts: List[str] = []
    validity: Optional[EstimateValidity] = None
    basis: str

    if len(kv_observations) == 1:
        obs = kv_observations[0]
        per_token = float(obs.get("per_token") or 0)
        obs_ctx = int(obs.get("ctx") or 0)
        target_ctx = int(ctx) if ctx is not None else obs_ctx
        if per_token > 0 and obs_ctx > 0:
            kv_bytes = per_token * target_ctx
            kv_source = "observed"
        kv_note_parts.append(f"observed overhead in this configuration (ctx {obs_ctx})")
        validity = EstimateValidity(ctx_min=obs_ctx, ctx_max=obs_ctx, slots=1)
        if target_ctx != obs_ctx:
            kv_extrapolated = True
            kv_note_parts.append("extrapolated beyond measured range")
        basis = "single_observation"
    elif len(kv_observations) >= 2:
        try:
            fit = fit_overhead(kv_observations)
        except ValueError as e:
            fit = None
            kv_note_parts.append(f"fit unavailable: {e}")
        if fit is not None:
            target_ctx = int(ctx) if ctx is not None else fit["ctx_max"]
            kv_bytes = max(0.0, fit["fixed"] + fit["per_token"] * target_ctx)
            kv_source = "estimated"
            kv_note_parts.append(f"fitted over {fit['ctx_min']}–{fit['ctx_max']} tokens of context")
            validity = EstimateValidity(ctx_min=fit["ctx_min"], ctx_max=fit["ctx_max"], slots=1)
            if target_ctx < fit["ctx_min"] or target_ctx > fit["ctx_max"]:
                kv_extrapolated = True
                kv_note_parts.append("extrapolated beyond measured range")
        basis = "fitted"
    elif arch and str(arch.get("kind") or "unknown") != "unknown" and arch.get("kv_per_token") is not None:
        per_token = float(arch["kv_per_token"])
        if ctx is not None and per_token > 0:
            kv_bytes = per_token * float(ctx)
            kv_source = "estimated"
        kv_note_parts.append("estimated from architecture metadata")
        if arch.get("hybrid"):
            kv_note_parts.append("hybrid attention: a dense per-layer formula is not applied to every block")
        basis = "metadata"
    else:
        basis = "incomplete"
        kv_note_parts.append("no KV observation and no usable architecture metadata")

    extra_upper = 0.0
    if kv_bytes is not None and slots and slots > 1:
        kv_bytes = kv_bytes * slots
        extra_upper = kv_bytes * 0.25
        kv_note_parts.append(f"measured with 1 slot; {slots} slots is an extrapolation, not a guarantee")
        if validity is not None:
            validity = EstimateValidity(ctx_min=validity.ctx_min, ctx_max=validity.ctx_max, slots=slots)

    kv_component = MemoryComponent(
        bytes=int(kv_bytes) if kv_bytes is not None else None, source=kv_source,
        note="; ".join(kv_note_parts),
    )

    weights_component = (
        MemoryComponent(bytes=int(weights_bytes), source="observed", note="")
        if weights_bytes is not None
        else MemoryComponent(bytes=None, source="absent", note="weights size not provided")
    )
    margin_component = MemoryComponent(
        bytes=int(reserve_bytes), source="estimated",
        note="fixed reserve for CUDA context/compute buffers (vram_fit.DEFAULT_RESERVE_BYTES)",
    )
    buffers_component = MemoryComponent(
        bytes=None, source="absent",
        note="compute-buffer overhead is not estimated from architecture metadata alone",
    )

    if str(engine_implementation or "").lower() in ("vllm", "sglang"):
        notes.append(
            f"{engine_implementation}: paged KV — preallocates a fraction of VRAM at launch; "
            "the engine's own gpu-memory-utilization governs, this estimate is a floor",
        )

    have_weights = weights_component.bytes is not None
    if basis == "incomplete" or not have_weights:
        complete = False
        total_lower = (
            weights_component.bytes + margin_component.bytes if have_weights else None
        )
        total_upper = None
    else:
        lower_parts = [weights_component.bytes, margin_component.bytes]
        upper_parts = [weights_component.bytes, margin_component.bytes]
        if buffers_component.bytes is not None:
            lower_parts.append(buffers_component.bytes)
            upper_parts.append(buffers_component.bytes)
        if kv_component.bytes is not None:
            if kv_extrapolated:
                upper_parts.append(kv_component.bytes)
            else:
                lower_parts.append(kv_component.bytes)
                upper_parts.append(kv_component.bytes)
        complete = kv_component.bytes is not None and not kv_extrapolated
        total_lower = int(sum(lower_parts))
        total_upper = int(sum(upper_parts) + extra_upper)

    return CandidateEstimate(
        weights=weights_component, kv_state=kv_component, buffers=buffers_component,
        margin=margin_component, total_lower=total_lower, total_upper=total_upper,
        complete=complete, basis=basis, validity=validity, notes=tuple(notes),
    )


# ── system_memory: the one impure reading in this module ───────────────────

SYSTEM_MEMORY_SOURCES = ("psutil", "windows_api", "absent")


def system_memory() -> Dict[str, Any]:
    """RAM (everywhere `psutil` is available) plus Windows commit
    accounting (`GlobalMemoryStatusEx`, ctypes — no subprocess). Never
    invents `commit_*` on a platform that has no such concept, and never
    counts GPU shared memory here — that is `shared_spill` on a
    `MemoryBudget`, a different axis entirely."""
    out: Dict[str, Any] = {
        "ram_total": None, "ram_available": None,
        "commit_total": None, "commit_limit": None, "commit_available": None,
        "source": "absent",
    }
    try:
        import psutil
        vm = psutil.virtual_memory()
        out["ram_total"] = int(vm.total)
        out["ram_available"] = int(vm.available)
        out["source"] = "psutil"
    except Exception as e:  # noqa: BLE001
        logger.debug("memory_budget: psutil unavailable: %s", e)

    if sys.platform.startswith("win"):
        try:
            import ctypes

            class _MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = _MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):  # type: ignore[attr-defined]
                if out["ram_total"] is None:
                    out["ram_total"] = int(stat.ullTotalPhys)
                    out["ram_available"] = int(stat.ullAvailPhys)
                # The page file's current total/available IS the commit
                # limit/available Windows enforces process-wide — there is
                # no separate "limit" reading distinct from the page file
                # accounting itself.
                out["commit_total"] = int(stat.ullTotalPageFile)
                out["commit_limit"] = int(stat.ullTotalPageFile)
                out["commit_available"] = int(stat.ullAvailPageFile)
                out["source"] = "windows_api" if out["source"] == "absent" else out["source"]
        except Exception as e:  # noqa: BLE001
            logger.debug("memory_budget: GlobalMemoryStatusEx unavailable: %s", e)

    return out


# ── admissible: does the estimate fit the budget ─────────────────────────────

ADMISSIBLE_VERDICTS = ("fits", "does_not_fit", "unknown")


def admissible(budget: MemoryBudget, estimate: CandidateEstimate, *, now: Optional[float] = None) -> Dict[str, Any]:
    """§11 acceptance cases: a stale reading, or an incomplete estimate,
    NEVER authorizes `"fits"` — both resolve to `"unknown"` with a reason,
    regardless of what the raw numbers happen to say."""
    if budget.stale:
        now = now if now is not None else time.time()
        age = _iso_age_s(budget.observed_at, now=now)
        age_txt = f"{age:.0f}" if age is not None else "?"
        return {"verdict": "unknown", "reason": f"reading is {age_txt} s old; refresh before loading",
               "shortfall_bytes": None}
    if not estimate.complete:
        return {"verdict": "unknown",
               "reason": f"the memory estimate is incomplete (basis: {estimate.basis})",
               "shortfall_bytes": None}
    free = budget.components.free.bytes
    if free is None:
        return {"verdict": "unknown", "reason": "free VRAM on this GPU was not observed",
               "shortfall_bytes": None}
    need = estimate.total_upper if estimate.total_upper is not None else estimate.total_lower
    if need is None:
        return {"verdict": "unknown", "reason": "the candidate estimate has no total to compare",
               "shortfall_bytes": None}
    if free >= need:
        return {"verdict": "fits", "reason": "", "shortfall_bytes": None}
    return {"verdict": "does_not_fit", "reason": f"short by {need - free} bytes",
           "shortfall_bytes": int(need - free)}
