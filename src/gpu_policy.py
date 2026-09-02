"""GPU placement policy — which card Ollama should fill first.

Ollama 0.33 puts a model that fits one card on the card with the MOST free
memory and splits one that fits no single card across all of them. Measured
on the two-card box (RTX 4070 Ti 12 GB + RTX 5060 Ti 16 GB):

* `main_gpu: N` pins a model to card N — and a model too big for that card is
  NOT split: the rest goes to the CPU (66-layer 27B pinned to the 16 GB card:
  54/66 layers on the GPU, 10 tok/s instead of 19–24 split). So a policy may
  only pin what fits.
* `tensor_split` in the request options is ignored: the split ratio of a big
  model is Ollama's (proportional to free memory) and not ours to choose.

The policy is one number, `gpu_placement_prefer`: -1 = Auto (Ollama's own
choice), N = fill card N first — every model whose weights fit card N with
room for its context gets `main_gpu = N` unless its Options pin it elsewhere;
bigger models are left to Ollama (split). Per-model Options always win.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

SETTING_KEY = "gpu_placement_prefer"
RESERVE_BYTES = 800 * 1024 * 1024        # CUDA context etc. (same as the fit advisor)
# KV cache + compute buffers on top of the weights: a fraction of the card
# rather than a per-model estimate (the exact KV size needs /api/show; the
# fit advisor does that, the policy must be cheap — it runs on every request).
HEADROOM_FRACTION = 0.18
_SIZES_TTL = 120.0
_sizes_cache: Dict[str, tuple[float, Dict[str, int]]] = {}
_lock = threading.Lock()


def preferred_index() -> int:
    try:
        from src.settings import get_setting
        return int(get_setting(SETTING_KEY, -1))
    except Exception:
        return -1


def set_preferred_index(index: int) -> int:
    from src.settings import load_settings, save_settings
    idx = int(index)
    if idx < -1 or idx > 15:
        raise ValueError("gpu index must be -1 (auto) or 0..15")
    settings = load_settings()
    settings[SETTING_KEY] = idx
    save_settings(settings)
    return idx


def fits_card(size_bytes: int, card_total_bytes: int, *, reserve: int = RESERVE_BYTES,
              headroom: float = HEADROOM_FRACTION) -> bool:
    """Weights + a CUDA context + room for the KV cache, on this card alone."""
    if size_bytes <= 0 or card_total_bytes <= 0:
        return False
    return size_bytes + reserve <= card_total_bytes * (1.0 - headroom)


def _base_of(url: str) -> str:
    p = urlparse(str(url or "").strip())
    if not p.scheme or not p.netloc:
        return ""
    return f"{p.scheme}://{p.netloc}"


def _is_local_ollama(url: str) -> bool:
    """The policy only makes sense for the Ollama on THIS machine — the one
    nvidia-smi sees. Loopback, or a host the admin declared as Ollama."""
    p = urlparse(str(url or ""))
    host = (p.hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return True
    try:
        from src.model_load_options import is_declared_ollama_host
        return bool(is_declared_ollama_host(url))
    except Exception:
        return False


def model_sizes(base: str, *, timeout: float = 3.0) -> Dict[str, int]:
    """{tag: bytes on disk} from `/api/tags`, cached ~2 min per server."""
    now = time.time()
    with _lock:
        cached = _sizes_cache.get(base)
        if cached and now - cached[0] < _SIZES_TTL:
            return cached[1]
    sizes: Dict[str, int] = {}
    try:
        r = httpx.get(base.rstrip("/") + "/api/tags", timeout=timeout)
        if r.status_code == 200:
            for m in (r.json() or {}).get("models") or []:
                name = str(m.get("name") or m.get("model") or "")
                if name:
                    sizes[name] = int(m.get("size") or 0)
    except Exception as e:  # noqa: BLE001 — never worth failing a request
        logger.debug("gpu policy: /api/tags failed for %s: %s", base, e)
        with _lock:
            if base in _sizes_cache:
                return _sizes_cache[base][1]
    with _lock:
        _sizes_cache[base] = (now, sizes)
    return sizes


def _size_for(sizes: Dict[str, int], model: str) -> int:
    m = str(model or "").strip()
    if not m:
        return 0
    if m in sizes:
        return sizes[m]
    short = m.split("/")[-1]
    for cand in (short, f"{short}:latest", m.split(":")[0] + ":latest"):
        if cand in sizes:
            return sizes[cand]
    return 0


def card_total(index: int) -> int:
    try:
        from src import gpu_shared_memory
        snap = gpu_shared_memory.vram_snapshot()
    except Exception:
        return 0
    if not snap.get("supported"):
        return 0
    for g in snap.get("gpus") or []:
        try:
            if int(g.get("index")) == index:
                return int(g.get("total") or 0)
        except (TypeError, ValueError):
            continue
    return 0


def preferred_main_gpu(url: str, model: str, *, prefer: Optional[int] = None) -> Optional[int]:
    """The `main_gpu` the policy adds to a request for `model` at `url`, or
    None: policy off, not the local Ollama, card unknown, model size unknown,
    or the model would not fit the preferred card (then Ollama splits it)."""
    idx = preferred_index() if prefer is None else int(prefer)
    if idx < 0:
        return None
    if not _is_local_ollama(url):
        return None
    total = card_total(idx)
    if total <= 0:
        return None
    base = _base_of(url)
    if not base:
        return None
    size = _size_for(model_sizes(base), model)
    if size <= 0:
        return None
    return idx if fits_card(size, total) else None


def describe(gpus: Optional[list] = None) -> Dict[str, Any]:
    """For the UI: `{prefer, name, mode}`."""
    idx = preferred_index()
    name = ""
    if idx >= 0 and gpus:
        for g in gpus:
            try:
                if int(g.get("index")) == idx:
                    name = str(g.get("name") or "")
            except (TypeError, ValueError):
                continue
    return {"prefer": idx, "name": name, "mode": "auto" if idx < 0 else "prefer"}


def reset_cache() -> None:
    with _lock:
        _sizes_cache.clear()
