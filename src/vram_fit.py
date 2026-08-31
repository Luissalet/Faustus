"""Make a model fit in VRAM instead of spilling — the arithmetic, on its own.

Two things decide whether a local model runs at full speed: the weights and
the KV cache. The weights are fixed by the file; the KV cache grows linearly
with the context window, which is the knob nobody thinks about until a model
that loaded fine at 8k crawls at 32k.

We work out the KV cost per token two ways, and say which one we used:

* **measured** — the model is loaded, so `ollama ps` already tells us the
  truth: its reported ``size`` minus the file on disk is the KV cache plus the
  compute buffers *at the context it was loaded with*. Divide by that context
  and you have bytes per token for this exact model, quantisation and runner.
  No formula can beat that, and it works for hybrid attention models (Qwen 3.5
  interleaves SSM and full-attention blocks, so the textbook 2·layers·heads·dim
  formula is simply wrong for them).
* **estimated** — the model is not loaded, so fall back to the GGUF metadata
  and flag the answer as an estimate.

Everything here is pure arithmetic on numbers the caller gathered, so it can
be unit-tested without a GPU.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

MIB = 1024 * 1024
GIB = 1024 * MIB

# CUDA context, cuBLAS workspace and the graph's compute buffers. Ollama keeps
# its own headroom too; this is what we assume is gone before a single weight
# is loaded.
DEFAULT_RESERVE_BYTES = 800 * MIB

# Contexts worth proposing, largest first.
CTX_LADDER = (131072, 65536, 32768, 16384, 8192, 4096)


def kv_bytes_per_token_measured(
    loaded_size_bytes: int, file_size_bytes: int, loaded_ctx: int
) -> Optional[float]:
    """From `ollama ps`: (total - weights) / context. None if it makes no sense."""
    if not loaded_size_bytes or not file_size_bytes or not loaded_ctx:
        return None
    overhead = loaded_size_bytes - file_size_bytes
    if overhead <= 0:
        return None
    return overhead / float(loaded_ctx)


def kv_bytes_per_token_estimated(model_info: Dict[str, Any]) -> Tuple[Optional[float], str]:
    """From GGUF metadata. Returns (bytes_per_token, note)."""
    if not model_info:
        return None, "no model metadata"
    arch = str(model_info.get("general.architecture") or "").strip()
    if not arch:
        return None, "no architecture in metadata"

    def val(key: str) -> Optional[float]:
        v = model_info.get(f"{arch}.{key}")
        if v in (None, ""):
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    layers = val("block_count")
    if not layers:
        return None, "no block_count in metadata"
    heads = val("attention.head_count")
    heads_kv = val("attention.head_count_kv") or heads
    key_len = val("attention.key_length")
    value_len = val("attention.value_length") or key_len
    if key_len is None:
        emb = val("embedding_length")
        if emb and heads:
            key_len = value_len = emb / heads
    if not heads_kv or not key_len or not value_len:
        return None, "not enough attention metadata"
    # f16 K and V for every layer.
    per_token = layers * heads_kv * (key_len + value_len) * 2
    note = "estimated from GGUF metadata"
    if model_info.get(f"{arch}.full_attention_interval") or any(
        str(k).startswith(f"{arch}.ssm.") for k in model_info
    ):
        note = "estimated from GGUF metadata — hybrid attention model, treat as an upper bound"
    return per_token, note


def plan(
    *,
    vram_total_bytes: int,
    vram_used_by_others_bytes: int = 0,
    file_size_bytes: int,
    n_layers: int,
    kv_bytes_per_token: Optional[float],
    kv_source: str = "unknown",
    current_ctx: Optional[int] = None,
    target_ctx: Optional[int] = None,
    max_ctx: Optional[int] = None,
    reserve_bytes: int = DEFAULT_RESERVE_BYTES,
) -> Dict[str, Any]:
    """Largest context that still keeps every layer on the GPU — or, failing
    that, how many layers do fit.

    The order of preference is deliberate: shrink the context first, then
    quantise the KV cache to q8_0 (half the bytes, a rounding error in output
    quality), and only then start pushing layers onto the CPU. Layers on the
    CPU are honest and predictable; what we are avoiding at all costs is the
    driver quietly paging weights into shared memory over PCIe.

    ``num_ctx`` is never raised above what the caller asked for (or 32k when
    it asked for nothing) — spare room is reported as ``max_ctx_that_fits``
    instead of silently making every load bigger.
    """
    budget = vram_total_bytes - reserve_bytes - max(0, vram_used_by_others_bytes)
    steps: List[str] = []
    target = target_ctx or max(current_ctx or 0, 32768)
    if max_ctx:
        target = min(target, max_ctx)
    ladder = sorted({c for c in CTX_LADDER + (target,) if c >= 2048 and (not max_ctx or c <= max_ctx)},
                    reverse=True) or [2048]

    base = {
        "budget_bytes": budget,
        "vram_total_bytes": vram_total_bytes,
        "vram_used_by_others_bytes": max(0, vram_used_by_others_bytes),
        "reserve_bytes": reserve_bytes,
        "file_size_bytes": file_size_bytes,
        "kv_bytes_per_token": kv_bytes_per_token,
        "kv_source": kv_source,
        "current_ctx": current_ctx,
        "target_ctx": target,
        "n_layers": n_layers,
    }

    if budget <= 0:
        return {
            **base, "fits": False, "num_ctx": min(4096, target), "num_gpu": 0,
            "kv_cache_type": None, "gpu_overhead_bytes": 0,
            "estimated_vram_bytes": 0, "max_ctx_that_fits": None,
            "steps": ["Something else is using the whole card — free that first."],
        }

    if kv_bytes_per_token:
        for kv_type, factor in (("f16", 1.0), ("q8_0", 0.5)):
            best = None
            for ctx in ladder:
                if file_size_bytes + kv_bytes_per_token * ctx * factor <= budget:
                    best = ctx
                    break
            if best is None:
                continue
            chosen = min(best, target)
            kv = kv_bytes_per_token * chosen * factor
            if kv_type == "q8_0":
                steps.append("KV cache to q8_0 — half the bytes, no visible quality cost.")
            if current_ctx and chosen < current_ctx:
                steps.append(f"Context {current_ctx} → {chosen}: the KV cache is what does not fit.")
            if best > chosen:
                steps.append(f"There is room for up to {best} tokens of context if you want it.")
            if not steps:
                steps.append("Everything already fits on the GPU.")
            return {
                **base, "fits": True, "num_ctx": chosen, "num_gpu": None,
                "kv_cache_type": None if kv_type == "f16" else "q8_0",
                "gpu_overhead_bytes": _overhead(vram_used_by_others_bytes),
                "estimated_vram_bytes": int(file_size_bytes + kv),
                "max_ctx_that_fits": best,
                "steps": steps,
            }

    # Nothing fits whole: put as many layers as we can on the GPU and be
    # explicit that the rest runs on the CPU (which is fine — it is not PCIe).
    ctx = min(target, 8192)
    kv = (kv_bytes_per_token or 0) * ctx * 0.5
    total_layers = max(1, n_layers) + 1
    per_layer = file_size_bytes / float(total_layers)
    num_gpu = int(max(0, math.floor((budget - kv) / per_layer))) if per_layer else 0
    num_gpu = min(num_gpu, total_layers)
    steps.append(
        f"The model does not fit whole: {num_gpu} of {total_layers} layers on the GPU at "
        f"{ctx} tokens of context, the rest on the CPU. Slower, but honest — CPU layers read "
        "system RAM directly instead of dragging it back over PCIe."
    )
    if kv_bytes_per_token:
        steps.append("KV cache to q8_0 to buy back some of the room.")
    return {
        **base, "fits": False, "num_ctx": ctx, "num_gpu": num_gpu,
        "kv_cache_type": "q8_0" if kv_bytes_per_token else None,
        "gpu_overhead_bytes": _overhead(vram_used_by_others_bytes),
        "estimated_vram_bytes": int(num_gpu * per_layer + kv),
        "max_ctx_that_fits": None,
        "steps": steps,
    }


def _overhead(used_by_others: int) -> int:
    """`OLLAMA_GPU_OVERHEAD`: keep Ollama from filling the card to the brim.

    Only worth suggesting when something else is already on the GPU, because
    that something else can grow after Ollama has sized its layers — which is
    exactly when the driver starts paging.
    """
    return 512 * MIB if used_by_others > 256 * MIB else 0
