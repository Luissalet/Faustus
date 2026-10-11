"""Thinking inertia: does a "fast" (no reasoning) turn really not think?

A model told not to think often keeps thinking (a chat template that opens a
``<think>`` block anyway, an engine that ignores the switch). Fast mode is
only worth anything if the engine really stops reasoning, so this module:

* measures the reasoning that still came out of a fast turn (``measure``:
  the engine's own ``reasoning_tokens`` when it reports them, else an
  estimate from the reasoning text), stored with the turn's calibration
  record as ``thinking_leak_tokens`` / ``thinking_leak_chars``;
* keeps a small per-model memory of the latest fast turns (``record_fast_turn``)
  and, when a model leaked beyond ``think_leak_threshold_tokens`` in any of its
  last few fast turns, hands the next fast turn a reasoning cap
  (``cap_for``), sent to the engine as a reasoning budget;
* aggregates the leak per model for the calibration report (``summarize``).

It never blocks or rewrites an answer: it only measures and, on the next fast
turn of a leaky model, bounds the reasoning. Everything is best effort and
never raises into a turn.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any, Dict, Iterable, List, Mapping, Optional

__all__ = [
    "DEFAULT_THRESHOLD_TOKENS", "DEFAULT_CAP_TOKENS", "RECENT_WINDOW", "estimate_tokens", "measure",
    "record_fast_turn", "cap_for", "caps", "reset", "summarize", "leak_stamp",
]

#: A fast turn that reasoned more than this many tokens leaked.
DEFAULT_THRESHOLD_TOKENS = 64
#: Reasoning budget handed to the next fast turn of a leaky model.
DEFAULT_CAP_TOKENS = 128
#: How many of the latest fast turns of a model decide whether it is capped.
RECENT_WINDOW = 3
_MIN_CAP = 16
_MAX_KEPT = 20

_lock = threading.Lock()
_path_override: Optional[str] = None


def _store_path() -> str:
    if _path_override:
        return _path_override
    try:
        from src.constants import DATA_DIR
    except Exception:  # noqa: BLE001
        DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
    return os.path.join(DATA_DIR, "thinking_inertia.json")


def _setting(key: str, default: int) -> int:
    try:
        from src.settings import get_setting
        value = int(get_setting(key, default) or 0)
    except Exception:  # noqa: BLE001
        return default
    return value if value > 0 else default


def threshold_tokens() -> int:
    return _setting("think_leak_threshold_tokens", DEFAULT_THRESHOLD_TOKENS)


def cap_tokens() -> int:
    return max(_MIN_CAP, _setting("think_leak_cap_tokens", DEFAULT_CAP_TOKENS))


def estimate_tokens(text: Any) -> int:
    """Rough token count of a reasoning text (about four characters a token)."""
    n = len(str(text or "").strip())
    return (n + 3) // 4 if n else 0


def measure(thinking: Any, metrics: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """The reasoning that came out of a turn: ``{"tokens", "chars", "source"}``.

    The engine's ``reasoning_tokens`` (``last_metrics``) wins when present;
    otherwise the count is estimated from the reasoning text.
    """
    text = str(thinking or "").strip()
    chars = len(text)
    reported = None
    if isinstance(metrics, Mapping):
        for key in ("reasoning_tokens", "thinking_tokens"):
            try:
                value = int(metrics[key]) if metrics.get(key) is not None else None
            except (TypeError, ValueError):
                value = None
            if value is not None and value >= 0:
                reported = value
                break
    if reported is not None:
        return {"tokens": reported, "chars": chars, "source": "engine"}
    return {"tokens": estimate_tokens(text), "chars": chars, "source": "estimate"}


def _load() -> Dict[str, Any]:
    try:
        with open(_store_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: Dict[str, Any]) -> None:
    path = _store_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass


def _key(model: Any) -> str:
    return str(model or "").strip().lower()


def _capped(recent: List[int], threshold: int) -> bool:
    return any(int(t) > threshold for t in recent[-RECENT_WINDOW:])


def record_fast_turn(model: Any, tokens: int, *, chars: int = 0) -> Dict[str, Any]:
    """Remember how much a fast turn reasoned; returns the model's state
    (``recent``, ``capped``, ``cap``). Never raises."""
    key = _key(model)
    if not key:
        return {}
    try:
        threshold = threshold_tokens()
        with _lock:
            data = _load()
            entry = data.get(key) if isinstance(data.get(key), dict) else {}
            recent = [int(t) for t in entry.get("recent") or [] if isinstance(t, (int, float))]
            recent.append(max(0, int(tokens)))
            recent = recent[-_MAX_KEPT:]
            entry.update({
                "recent": recent,
                "fast_turns": int(entry.get("fast_turns") or 0) + 1,
                "leaked_turns": int(entry.get("leaked_turns") or 0) + (1 if int(tokens) > threshold else 0),
                "leak_tokens": int(entry.get("leak_tokens") or 0) + max(0, int(tokens)),
                "max_leak_tokens": max(int(entry.get("max_leak_tokens") or 0), int(tokens)),
            })
            data[key] = entry
            _save(data)
        capped = _capped(recent, threshold)
        return {"recent": recent, "capped": capped, "cap": cap_tokens() if capped else None}
    except Exception:  # noqa: BLE001
        return {}


def cap_for(model: Any) -> Optional[int]:
    """Reasoning cap (tokens) for the next fast turn of ``model``, or None.

    Active while any of the model's last few fast turns reasoned beyond the
    threshold; a model that then stays quiet drops out of the window.
    """
    key = _key(model)
    if not key:
        return None
    try:
        with _lock:
            entry = _load().get(key)
        recent = [int(t) for t in (entry or {}).get("recent") or [] if isinstance(t, (int, float))]
        return cap_tokens() if _capped(recent, threshold_tokens()) else None
    except Exception:  # noqa: BLE001
        return None


def caps() -> Dict[str, Dict[str, Any]]:
    """Every model currently capped: ``{model: {"cap", "recent"}}``."""
    out: Dict[str, Dict[str, Any]] = {}
    try:
        threshold, cap = threshold_tokens(), cap_tokens()
        with _lock:
            data = _load()
        for key, entry in data.items():
            recent = [int(t) for t in (entry or {}).get("recent") or [] if isinstance(t, (int, float))]
            if _capped(recent, threshold):
                out[key] = {"cap": cap, "recent": recent[-RECENT_WINDOW:]}
    except Exception:  # noqa: BLE001
        pass
    return out


def reset(model: Any = None) -> None:
    """Forget one model (or all): used by tests and after a model is replaced."""
    with _lock:
        data = _load()
        if model is None:
            data = {}
        else:
            data.pop(_key(model), None)
        _save(data)


def leak_stamp(turn_mode: Optional[Mapping[str, Any]], thinking: Any,
               metrics: Optional[Mapping[str, Any]] = None, model: Any = None) -> Dict[str, Any]:
    """Fields to add to a turn's saved ``think_mode`` record.

    Only a fast turn is measured. Returns ``{}`` for any other mode. Updates
    the per-model memory as a side effect.
    """
    if not isinstance(turn_mode, Mapping) or turn_mode.get("mode") != "fast":
        return {}
    m = measure(thinking, metrics)
    out: Dict[str, Any] = {"thinking_leak_tokens": m["tokens"], "thinking_leak_chars": m["chars"]}
    if m["source"] == "engine":
        out["thinking_leak_source"] = "engine"
    state = record_fast_turn(model, m["tokens"], chars=m["chars"])
    if state.get("capped"):
        out["thinking_leaky"] = True
    return out


def summarize(records: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    """Leak rate per model from calibration records.

    Each record carries ``mode``, ``model``, ``thinking_leak_tokens`` (None
    when the turn was not measured) and ``thinking_cap`` (the cap the turn
    ran with). Only fast turns count.
    """
    threshold = threshold_tokens()
    by_model: Dict[str, Dict[str, Any]] = {}
    total = {"fast_turns": 0, "measured": 0, "leaked": 0, "leak_tokens": 0}
    for rec in records:
        if rec.get("mode") != "fast":
            continue
        model = str(rec.get("model") or "?")
        b = by_model.setdefault(model, {"fast_turns": 0, "measured": 0, "leaked": 0, "leak_tokens": 0,
                                        "max_leak_tokens": 0, "capped_turns": 0})
        for target in (b, total):
            target["fast_turns"] += 1
        if rec.get("thinking_cap"):
            b["capped_turns"] += 1
        leak = rec.get("thinking_leak_tokens")
        if isinstance(leak, (int, float)):
            for target in (b, total):
                target["measured"] += 1
                target["leak_tokens"] += int(leak)
                target["leaked"] += 1 if leak > threshold else 0
            b["max_leak_tokens"] = max(b["max_leak_tokens"], int(leak))
    for b in [*by_model.values(), total]:
        b["leak_rate"] = round(b["leaked"] / b["measured"], 3) if b["measured"] else None
        b["avg_leak_tokens"] = round(b["leak_tokens"] / b["measured"], 1) if b["measured"] else None
    active = caps()
    flags = []
    for model, b in by_model.items():
        if b["leaked"]:
            cap = active.get(_key(model))
            flags.append({
                "kind": "thinking_leak", "key": model, "leaked": b["leaked"], "measured": b["measured"],
                "leak_rate": b["leak_rate"], "max_leak_tokens": b["max_leak_tokens"],
                "capped_now": bool(cap), "cap": cap["cap"] if cap else None,
                "hint": (f"{model} reasoned more than {threshold} tokens in {b['leaked']} of {b['measured']} fast "
                         f"turns (max {b['max_leak_tokens']}); "
                         + (f"its next fast turn is capped at {cap['cap']} reasoning tokens." if cap
                            else "it is not capped now (its latest fast turns stayed quiet).")),
            })
    flags.sort(key=lambda f: -f["leaked"])
    return {"threshold_tokens": threshold, "cap_tokens": cap_tokens(), "total": total,
            "by_model": dict(sorted(by_model.items(), key=lambda kv: -kv[1]["fast_turns"])),
            "capped_models": active, "flags": flags}
