"""src/token_calibration.py — per-model calibration of ``estimate_tokens()``.

``src.model_context.estimate_tokens`` is a fixed ``chars * 0.3`` heuristic —
it has no idea which real BPE tokenizer a given model actually uses, so it is
systematically wrong (in either direction) for any one model, forever, by a
fixed but unknown amount.

The real prompt size is not actually unknown: every provider reports it back
after the fact (OpenAI-style ``usage.prompt_tokens``, Anthropic
``usage.input_tokens``, Ollama's native ``prompt_eval_count``), and
``src/llm_trace.py`` already sees that usage for every call this app makes
(streaming and non-streaming alike). This module closes the loop: it tracks,
per model, a running ratio ``actual_prompt_tokens / estimate_tokens(request)``
as an exponential moving average (EMA, alpha=0.2), and exposes that as a
correction factor a caller can multiply its own ``estimate_tokens()`` result
by — so compaction/trim/budget decisions made for e.g. a Qwen model converge
on Qwen's real tokenizer instead of staying at the generic chars*0.3 guess
that was tuned by eyeballing a handful of other tokenizers.

Design constraints (same spirit as ``src/llm_trace.py``):
* ``observe()`` must NEVER break or slow down a real LLM call — every
  public function here is wrapped so an internal failure only logs a
  debug line and the caller never sees it.
* Persisted to a small JSON file under ``DATA_DIR`` (``token_calibration.json``),
  loaded lazily (once per process) and written on a background thread,
  throttled so a chatty session cannot turn this into disk-write spam.
* Samples are filtered for sanity before they can move the EMA at all (see
  ``_extract_actual_prompt_tokens`` and the constants below) — a single
  bad sample (e.g. an Ollama KV-cache hit that only reports the *new*
  tokens) must not visibly skew a model's calibration.
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Iterable, List, Optional

from src.model_context import estimate_tokens

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# tuning constants
# ---------------------------------------------------------------------------

ALPHA = 0.2                 # EMA smoothing factor for the ratio
MIN_SAMPLES_FOR_FACTOR = 3  # factor() stays 1.0 (no-op) below this many samples
FACTOR_MIN = 0.6            # clamp: never correct the estimate down more than this
FACTOR_MAX = 1.8            # clamp: never correct the estimate up more than this

MIN_ESTIMATE_TOKENS = 200   # ignore samples whose estimate is too small to be stable
RATIO_MIN = 0.3             # sanity band: a ratio outside this is a bad sample,
RATIO_MAX = 3.0             # not a real signal about the model's tokenizer

# Ollama's native `prompt_eval_count` only reports the tokens actually
# evaluated THIS call. On a KV-cache hit (a prefix already resident from the
# previous turn) that is far smaller than the true prompt length, which would
# otherwise look like "our estimate is way too high" and drag the EMA down
# for a reason that has nothing to do with the tokenizer. Only trust it when
# it is at least this fraction of our own estimate.
OLLAMA_CACHE_HIT_FLOOR = 0.5

# Bumped whenever ``estimate_tokens`` changes what it counts: a ratio measured
# against the old estimator describes that estimator, not the tokenizer, so
# entries stamped with another version are dropped on load and relearned.
# 2: reasoning_content on assistant turns is counted (26-09-2026; a Qwen3
#    model had drifted to a x2.2 ratio from reasoning the estimate missed).
ESTIMATOR_VERSION = 2

# Same chars*0.3 rule model_context.estimate_tokens uses for text, applied to
# the JSON-serialized tool schemas so the "estimate" side of the ratio also
# accounts for the tools the request actually carried (they count toward the
# provider's real prompt_tokens, so leaving them out would make every
# tool-using model look like it needs a much bigger correction than it does).
_CHARS_PER_TOKEN = 0.3

# ---------------------------------------------------------------------------
# persisted state
# ---------------------------------------------------------------------------

_STATE_LOCK = threading.RLock()
_STATE: Optional[Dict[str, Dict[str, Any]]] = None  # lazy-loaded, keyed by normalized model

_WRITE_LOCK = threading.Lock()
_DIRTY = False
_LAST_WRITE_TS = 0.0
_MIN_WRITE_INTERVAL_S = 2.0

# Single background worker: writes are small, infrequent (throttled) and
# order does not matter beyond "last write wins", so one thread is plenty.
_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="token-calib")


def _file_path() -> str:
    try:
        from src.constants import DATA_DIR
    except Exception:  # pragma: no cover
        DATA_DIR = os.path.join(os.getcwd(), "data")
    return os.path.join(DATA_DIR, "token_calibration.json")


def _normalize_model_key(model: Optional[str]) -> str:
    return str(model or "").strip().lower()


def _load_locked() -> Dict[str, Dict[str, Any]]:
    """Populate ``_STATE`` from disk on first use. Caller holds ``_STATE_LOCK``."""
    global _STATE
    if _STATE is not None:
        return _STATE
    path = _file_path()
    data: Dict[str, Dict[str, Any]] = {}
    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                for key, entry in raw.items():
                    if not isinstance(entry, dict):
                        continue
                    try:
                        samples = int(entry.get("samples", 0))
                        ratio = float(entry.get("ratio_ema", 1.0))
                    except (TypeError, ValueError):
                        continue
                    if samples < 0 or not math.isfinite(ratio):
                        continue
                    if entry.get("estimator") != ESTIMATOR_VERSION:
                        continue  # measured against another estimator
                    data[str(key)] = {
                        "ratio_ema": ratio,
                        "samples": samples,
                        "last_update": entry.get("last_update"),
                        "estimator": ESTIMATOR_VERSION,
                    }
    except Exception:
        logger.debug("[token_calibration] load failed", exc_info=True)
        data = {}
    _STATE = data
    return _STATE


def _atomic_write(path: str, data: Dict[str, Any]) -> None:
    d = os.path.dirname(path) or "."
    tmp_path = None
    try:
        os.makedirs(d, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=".token_calibration-", dir=d)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp_path, path)
        tmp_path = None
    except Exception:
        logger.debug("[token_calibration] persist failed", exc_info=True)
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _persist_throttled(force: bool = False) -> None:
    """Write current state to disk, at most once per ``_MIN_WRITE_INTERVAL_S``.

    Runs on the background executor thread, never on the caller's hot path.
    A write skipped by the throttle is not lost: the in-memory state already
    has it, and the next ``observe()`` (or ``flush_for_tests``) will persist
    the up-to-date snapshot.
    """
    global _DIRTY, _LAST_WRITE_TS
    now = time.time()
    with _WRITE_LOCK:
        if not force and (now - _LAST_WRITE_TS) < _MIN_WRITE_INTERVAL_S:
            return
        with _STATE_LOCK:
            state = _load_locked()
            snapshot = {k: dict(v) for k, v in state.items()}
        _atomic_write(_file_path(), snapshot)
        _LAST_WRITE_TS = now
        _DIRTY = False


def flush_for_tests(timeout: float = 5.0) -> None:
    """Block until any pending write has completed. Test helper only."""
    _EXECUTOR.submit(lambda: _persist_throttled(force=True)).result(timeout=timeout)


def _reset_for_tests() -> None:
    """Drop the in-memory cache so the next call re-reads ``DATA_DIR``.

    Test helper only — production code loads once per process.
    """
    global _STATE, _DIRTY, _LAST_WRITE_TS
    with _STATE_LOCK:
        _STATE = None
    with _WRITE_LOCK:
        _DIRTY = False
        _LAST_WRITE_TS = 0.0


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------

def calibration_enabled() -> bool:
    try:
        from src.settings import get_setting
        return bool(get_setting("token_calibration_enabled", True))
    except Exception:
        return True


# ---------------------------------------------------------------------------
# public: factor / calibrated estimate
# ---------------------------------------------------------------------------

def factor(model: Optional[str]) -> float:
    """Multiplicative correction for ``estimate_tokens()`` for this model.

    ``1.0`` (a no-op) until at least ``MIN_SAMPLES_FOR_FACTOR`` real samples
    have been observed for this model; clamped to
    ``[FACTOR_MIN, FACTOR_MAX]`` afterwards so one wild sample (that still
    passed the sanity filters) cannot send a decision wildly astray.
    """
    key = _normalize_model_key(model)
    if not key:
        return 1.0
    try:
        with _STATE_LOCK:
            entry = _load_locked().get(key)
    except Exception:
        return 1.0
    if not entry or int(entry.get("samples", 0)) < MIN_SAMPLES_FOR_FACTOR:
        return 1.0
    ratio = float(entry.get("ratio_ema", 1.0))
    if not math.isfinite(ratio):
        return 1.0
    return max(FACTOR_MIN, min(FACTOR_MAX, ratio))


def estimate_tokens_for(messages: List[Dict], model: Optional[str]) -> int:
    """``estimate_tokens(messages)`` corrected by this model's calibration factor."""
    try:
        base = estimate_tokens(messages)
    except Exception:
        base = 0
    try:
        return int(round(base * factor(model)))
    except Exception:
        return base


def get_info(model: Optional[str]) -> Dict[str, Any]:
    """Small summary for one model — used by the context ledger."""
    key = _normalize_model_key(model)
    if not key:
        return {"factor": 1.0, "samples": 0}
    try:
        with _STATE_LOCK:
            entry = _load_locked().get(key)
    except Exception:
        entry = None
    if not entry:
        return {"factor": 1.0, "samples": 0}
    return {"factor": round(factor(model), 4), "samples": int(entry.get("samples", 0))}


def stats() -> Dict[str, Dict[str, Any]]:
    """Per-model factor/samples/ratio — used by ``GET /api/token-calibration``."""
    out: Dict[str, Dict[str, Any]] = {}
    try:
        with _STATE_LOCK:
            state = _load_locked()
            items = list(state.items())
    except Exception:
        return out
    for key, entry in items:
        samples = int(entry.get("samples", 0))
        ratio = float(entry.get("ratio_ema", 1.0))
        fac = 1.0 if samples < MIN_SAMPLES_FOR_FACTOR else max(FACTOR_MIN, min(FACTOR_MAX, ratio))
        out[key] = {
            "factor": round(fac, 4),
            "samples": samples,
            "ratio_ema": round(ratio, 4),
            "last_update": entry.get("last_update"),
        }
    return out


# ---------------------------------------------------------------------------
# public: feed a real observation
# ---------------------------------------------------------------------------

def _tools_size_tokens(tools: Optional[Iterable[Any]]) -> int:
    if not tools:
        return 0
    try:
        raw = json.dumps(list(tools), ensure_ascii=False, default=str)
    except Exception:
        try:
            raw = str(tools)
        except Exception:
            return 0
    return int(len(raw) * _CHARS_PER_TOKEN)


def _extract_actual_prompt_tokens(
    usage: Dict[str, Any], provider_kind: Optional[str], estimate: int,
) -> Optional[float]:
    """The real, TOTAL prompt-token count from an already-normalized usage dict.

    ``usage`` here is the shape ``src/llm_core.py`` normalizes every
    provider's payload into before it ever reaches ``llm_trace`` — a single
    ``input_tokens`` key, not the provider's own wire field name. What that
    key was built from differs by provider, though:

    * OpenAI-compatible (openai, openrouter, groq, mistral, llama.cpp's
      OpenAI surface, ...): normalized from ``prompt_tokens``, a TOTAL that
      already includes any cached prefix — always trustworthy.
    * anthropic: normalized from Anthropic's own ``input_tokens`` — also a
      total.
    * ollama (native ``/api/chat``): normalized from ``prompt_eval_count``,
      which on a KV-cache hit reports ONLY the newly evaluated tokens, not
      the full prompt. An undercount here must never calibrate the ratio
      down, so it is only trusted when it is at least
      ``OLLAMA_CACHE_HIT_FLOOR`` of our own estimate.
    """
    value = usage.get("input_tokens")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        return None
    if provider_kind == "ollama" and value < OLLAMA_CACHE_HIT_FLOOR * estimate:
        return None
    return value


def observe(
    *,
    model: Optional[str],
    request_messages: Optional[List[Dict]],
    tools: Optional[Iterable[Any]] = None,
    usage: Optional[Dict[str, Any]] = None,
    provider_kind: Optional[str] = None,
) -> None:
    """Feed one real call's actual prompt-token count into this model's EMA.

    Best-effort and silent on any failure — this must never break or slow
    down the LLM call it is observing. No-op when calibration is disabled,
    the model is unknown, usage carries no usable prompt-token count, or the
    sample fails a sanity filter (tiny estimate, wild ratio, unreliable
    Ollama cache-hit count).
    """
    try:
        if not calibration_enabled():
            return
        key = _normalize_model_key(model)
        if not key or not isinstance(usage, dict) or not usage:
            return
        messages = list(request_messages or [])
        estimate = estimate_tokens(messages) + _tools_size_tokens(tools)
        if estimate < MIN_ESTIMATE_TOKENS:
            return
        actual = _extract_actual_prompt_tokens(usage, provider_kind, estimate)
        if actual is None:
            return
        ratio = actual / estimate
        if not math.isfinite(ratio) or ratio < RATIO_MIN or ratio > RATIO_MAX:
            return

        with _STATE_LOCK:
            state = _load_locked()
            entry = state.get(key)
            if entry is None:
                state[key] = {"ratio_ema": ratio, "samples": 1, "last_update": time.time(),
                              "estimator": ESTIMATOR_VERSION}
            else:
                old_ratio = float(entry.get("ratio_ema", ratio))
                entry["ratio_ema"] = old_ratio * (1 - ALPHA) + ratio * ALPHA
                entry["samples"] = int(entry.get("samples", 0)) + 1
                entry["last_update"] = time.time()
                entry["estimator"] = ESTIMATOR_VERSION

        global _DIRTY
        with _WRITE_LOCK:
            _DIRTY = True
        try:
            _EXECUTOR.submit(_persist_throttled)
        except Exception:
            pass
    except Exception:
        logger.debug("[token_calibration] observe failed", exc_info=True)
