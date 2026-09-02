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
    """From GGUF metadata. Returns (bytes_per_token, note).

    This is the weak path. Two things routinely make it an over-estimate: a
    model that leaves ``attention.head_count_kv`` out of its metadata (we then
    have to assume full multi-head attention, when almost every modern model
    uses grouped-query attention with far fewer KV heads), and a hybrid model
    where only every n-th block is full attention. We correct for the second
    when the metadata says so, flag the first, and let the caller treat the
    number as a ceiling rather than a measurement.
    """
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

    caveats = []
    if not val("attention.head_count_kv"):
        caveats.append("no head_count_kv in the metadata, assuming full multi-head attention")
    # Hybrid models (Qwen 3.5 and friends) only give every n-th block full
    # attention; the rest are SSM blocks with a fixed-size state.
    attn_layers = layers
    interval = val("full_attention_interval")
    if interval and interval > 1:
        attn_layers = math.ceil(layers / interval)
        caveats.append(f"hybrid attention, 1 full-attention block in {int(interval)}")
    elif any(str(k).startswith(f"{arch}.ssm.") for k in model_info):
        caveats.append("hybrid SSM model")

    # f16 K and V for every attention layer.
    per_token = attn_layers * heads_kv * (key_len + value_len) * 2
    note = "estimated from GGUF metadata"
    if caveats:
        note += " — " + "; ".join(caveats) + "; treat as an upper bound"
    return per_token, note


def plan(
    *,
    vram_total_bytes: int,
    vram_used_by_others_bytes: int = 0,
    file_size_bytes: int,
    n_layers: int,
    kv_bytes_per_token: Optional[float],
    kv_source: str = "unknown",
    kv_reliable: bool = True,
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
        "kv_reliable": kv_reliable,
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
    if not kv_reliable:
        steps.append(
            "The KV cache size here is a ceiling read off the metadata, not a measurement — "
            "load the model once and ask again for exact numbers."
        )
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


# ── The pool: "does it fit somewhere" and "does it fit ONE card" ────────────
#
# Ollama 0.33 with two cards puts a model on the card with the most free
# memory and splits one that fits no single card across all of them. Two
# budgets therefore matter: the pool's (can it be loaded at all, at full GPU
# speed) and the largest single card's (will it be split — which works, at a
# PCIe-bound tokens/s). A CUDA context is paid per card.

def pool_budgets(
    vram: Dict[str, Any],
    *,
    held_by_runner_bytes: int,
    others_bytes: int,
    placements: Optional[Dict[str, Dict[str, Any]]] = None,
    reserve_per_card: int = DEFAULT_RESERVE_BYTES,
) -> Dict[str, Any]:
    """The `vram` block of /api/local-models and /api/models/fit.

    ``vram`` is ``gpu_shared_memory.vram_snapshot()`` (supported). The caller
    decides what counts as somebody else's (``others_bytes``: the pool's used
    minus what the runner holds, because a switch frees that); this works out
    the pool budget, the clean budget (nothing loaded — what Discover fits
    against) and the same two per card, attributing each loaded model's bytes
    to the card(s) src/gpu_placement.py saw it on. A card whose models could
    not be measured reports ``models_bytes: None`` and takes its share of the
    pool's "others" pro rata — the honest "we do not know", not a zero.
    """
    total = int(vram.get("total") or 0)
    used = int(vram.get("used") or 0)
    held = max(0, int(held_by_runner_bytes or 0))
    others = max(0, int(others_bytes or 0))
    cards = [dict(g) for g in (vram.get("gpus") or []) if isinstance(g, dict)]
    if not cards:
        cards = [{"index": 0, "name": vram.get("name", ""), "uuid": "",
                  "total": total, "used": used, "free": max(0, total - used)}]
    count = len(cards)
    reserve = reserve_per_card * count
    budget = max(0, total - reserve - others)
    clean_budget = max(0, total - reserve)
    placements = placements or {}

    def _placed(info: Dict[str, Any]) -> bool:
        return bool(info.get("gpus")) or info.get("placement") == "cpu"

    # Can every VRAM-resident model be put on a card? If not, the cards that
    # show no model may still be holding one, and are unknown rather than empty.
    all_placed = held == 0 or (bool(placements) and all(_placed(i) for i in placements.values()))

    out_cards: List[Dict[str, Any]] = []
    for c in cards:
        idx = int(c.get("index") or 0)
        c_total = int(c.get("total") or 0)
        c_used = int(c.get("used") or 0)
        names = sorted(n for n, i in placements.items() if idx in (i.get("gpus") or []))
        parts: List[Optional[int]] = []
        for n in names:
            for e in placements[n].get("per_gpu") or []:
                if int(e.get("index", -1)) == idx:
                    parts.append(e.get("bytes"))
        if count == 1:
            models_bytes: Optional[int] = held
        elif names:
            models_bytes = sum(int(b) for b in parts) if parts and all(b is not None for b in parts) else None
        else:
            models_bytes = 0 if all_placed else None
        out_cards.append({
            "index": idx,
            "name": str(c.get("name") or ""),
            "uuid": str(c.get("uuid") or ""),
            "total_bytes": c_total,
            "used_bytes": c_used,
            "free_bytes": int(c.get("free") if c.get("free") is not None else max(0, c_total - c_used)),
            "models_bytes": models_bytes,
            "models": names,
        })

    if count == 1:
        out_cards[0]["other_bytes"] = others
    else:
        known_other = 0
        for oc in out_cards:
            if oc["models_bytes"] is not None:
                oc["other_bytes"] = max(0, oc["used_bytes"] - int(oc["models_bytes"]))
                known_other += oc["other_bytes"]
        unknown = [oc for oc in out_cards if oc["models_bytes"] is None]
        remaining = max(0, others - known_other)
        weight = sum(oc["used_bytes"] for oc in unknown)
        for oc in unknown:
            share = remaining * oc["used_bytes"] / weight if weight else 0
            oc["other_bytes"] = int(min(oc["used_bytes"], share))
    for oc in out_cards:
        oc["reserve_bytes"] = reserve_per_card
        oc["budget_bytes"] = (budget if count == 1
                              else max(0, oc["total_bytes"] - reserve_per_card - oc["other_bytes"]))
        oc["clean_budget_bytes"] = (clean_budget if count == 1
                                    else max(0, oc["total_bytes"] - reserve_per_card))

    return {
        "supported": True,
        "name": str(vram.get("name") or ""),
        "count": count,
        "total_bytes": total,
        "used_bytes": used,
        "free_bytes": int(vram.get("free") if vram.get("free") is not None else max(0, total - used)),
        "held_by_runner_bytes": held,
        "other_bytes": others,
        "reserve_bytes": reserve,
        "reserve_per_gpu_bytes": reserve_per_card,
        "budget_bytes": budget,
        # Budget with nothing else resident: what Discover fits against.
        "clean_budget_bytes": clean_budget,
        "gpus": out_cards,
        "largest_single_budget_bytes": max(oc["budget_bytes"] for oc in out_cards),
        "largest_single_clean_budget_bytes": max(oc["clean_budget_bytes"] for oc in out_cards),
    }


def needs_split(size_bytes: int, vram: Dict[str, Any], *, clean: bool = False) -> bool:
    """True when the weights exceed the largest single card's budget but not
    the pool's: Ollama will load it, split across the cards."""
    if int(vram.get("count") or 1) < 2 or size_bytes <= 0:
        return False
    pool = int(vram.get("clean_budget_bytes" if clean else "budget_bytes") or 0)
    single = vram.get("largest_single_clean_budget_bytes" if clean else "largest_single_budget_bytes")
    if single is None:
        return False
    return int(single) < size_bytes <= pool
