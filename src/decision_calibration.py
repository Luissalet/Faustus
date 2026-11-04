"""Calibrated acceptance thresholds for typed decisions, with a statistical guarantee.

A typed decision (src/typed_decision.py) answers a closed question and reports a confidence. Accepting
every answer above one global confidence says nothing about how often the accepted answers are wrong.
This module picks, PER QUESTION (the field name), the lowest confidence threshold whose accepted answers
are wrong less often than a chosen rate, with a Clopper-Pearson upper confidence bound on the error rate
measured on labelled examples:

    threshold = lowest t such that  upper_bound(errors among samples with confidence >= t,
                                                 count of such samples, delta) <= max_error

``delta`` is the chance that the true error rate is above the bound. A sample that is too small never
passes: with 0 errors in n samples the bound is 1 - delta**(1/n), so reaching 5 % at delta 0.05 takes 59
clean samples. Answers below the threshold are not trusted and fall to the slower path (the model with
reasoning, or the user); ``audit_rate`` is the share of ACCEPTED answers a caller should still send to
the slow path so the guarantee can be re-measured.

Pure stdlib. Nothing here raises on bad input. The calibration of each question is stored in
``data/typed_decision_calibration.json`` and read by ``typed_decision.decide`` when the caller did not
pass its own ``min_confidence``.
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    from src.constants import DATA_DIR as _DEFAULT_DATA_DIR
except Exception:  # noqa: BLE001 - standalone use (tests, tooling)
    _DEFAULT_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")

DATA_DIR = _DEFAULT_DATA_DIR  # module-level so tests can point the store somewhere disposable
FILE_NAME = "typed_decision_calibration.json"
DEFAULT_MAX_ERROR = 0.05
DEFAULT_DELTA = 0.05
DEFAULT_AUDIT_RATE = 0.02
MAX_SAMPLES = 20000
_LOCK = threading.Lock()


def _log_binom_cdf(k: int, n: int, p: float) -> float:
    """log P(X <= k) for X ~ Binomial(n, p), summed in log space."""
    if k >= n:
        return 0.0
    if p <= 0.0:
        return 0.0
    if p >= 1.0:
        return float("-inf")
    lp, lq = math.log(p), math.log1p(-p)
    terms = [math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1) + i * lp + (n - i) * lq
             for i in range(0, k + 1)]
    top = max(terms)
    return top + math.log(sum(math.exp(t - top) for t in terms))


def clopper_pearson_upper(errors: int, n: int, delta: float = DEFAULT_DELTA) -> float:
    """One-sided upper confidence bound (level 1 - delta) on an error rate seen as ``errors`` in ``n``."""
    try:
        errors, n, delta = int(errors), int(n), float(delta)
    except (TypeError, ValueError):
        return 1.0
    if n <= 0 or errors < 0 or errors > n or not (0.0 < delta < 1.0):
        return 1.0
    if errors >= n:
        return 1.0
    target = math.log(delta)
    lo, hi = errors / n, 1.0
    for _ in range(80):  # the CDF falls as p grows; find p with CDF(errors) = delta
        mid = (lo + hi) / 2
        if _log_binom_cdf(errors, n, mid) > target:
            lo = mid
        else:
            hi = mid
    return round(hi, 6)


def _clean_samples(samples: Iterable[Any]) -> List[Tuple[float, bool]]:
    out: List[Tuple[float, bool]] = []
    for s in list(samples or [])[:MAX_SAMPLES]:
        try:
            if isinstance(s, dict):
                conf, ok = s.get("confidence"), s.get("correct")
            else:
                conf, ok = s[0], s[1]
            conf = float(conf)
        except (TypeError, ValueError, IndexError, KeyError):
            continue
        if not (0.0 <= conf <= 1.0) or not isinstance(ok, (bool, int)):
            continue
        out.append((conf, bool(ok)))
    return out


def calibrate(samples: Iterable[Any], *, max_error: float = DEFAULT_MAX_ERROR,
              delta: float = DEFAULT_DELTA, audit_rate: float = DEFAULT_AUDIT_RATE) -> Dict[str, Any]:
    """Lowest confidence threshold whose accepted answers keep the error bound under ``max_error``.

    ``samples``: ``[{"confidence": 0.93, "correct": true}, ...]`` (or ``[confidence, correct]`` pairs), one
    per labelled example the decision answered. Returns ``{ok, threshold, accepted, errors, upper_bound,
    coverage, samples, max_error, delta, audit_rate, reason}``. ``ok`` False (``threshold`` None) when no
    threshold gives the guarantee, usually because there are too few examples or the model is not accurate
    enough; ``reason`` says which."""
    try:
        max_error = float(max_error)
        delta = float(delta)
        audit_rate = min(1.0, max(0.0, float(audit_rate)))
    except (TypeError, ValueError):
        max_error, delta, audit_rate = DEFAULT_MAX_ERROR, DEFAULT_DELTA, DEFAULT_AUDIT_RATE
    base: Dict[str, Any] = {"ok": False, "threshold": None, "accepted": 0, "errors": 0, "upper_bound": None,
                            "coverage": 0.0, "samples": 0, "max_error": max_error, "delta": delta,
                            "audit_rate": audit_rate, "reason": ""}
    if not (0.0 < max_error < 1.0) or not (0.0 < delta < 1.0):
        return {**base, "reason": "max_error and delta must be between 0 and 1"}
    data = sorted(_clean_samples(samples), key=lambda s: -s[0])
    base["samples"] = len(data)
    if not data:
        return {**base, "reason": "no usable samples"}
    # Walk the threshold down through the distinct confidences; the accepted set only grows. Keep the
    # LOWEST threshold that still holds the bound (more coverage), not merely the first.
    best: Optional[Dict[str, Any]] = None
    errors = accepted = 0
    i = 0
    while i < len(data):
        conf = data[i][0]
        while i < len(data) and data[i][0] == conf:
            accepted += 1
            errors += 0 if data[i][1] else 1
            i += 1
        upper = clopper_pearson_upper(errors, accepted, delta)
        if upper <= max_error:
            best = {"threshold": round(conf, 6), "accepted": accepted, "errors": errors, "upper_bound": upper}
        elif best is not None:
            break  # the first threshold that fails ends the search: a lower one is not trusted on luck
    if best is None:
        floor = clopper_pearson_upper(0, len(data), delta)
        why = ("too few examples: even with no error the bound over all %d samples is %.3f" % (len(data), floor)
               if floor > max_error else "no threshold keeps the error bound under %.3f" % max_error)
        return {**base, "reason": why}
    return {**base, **best, "ok": True, "coverage": round(best["accepted"] / len(data), 4)}


# -- the stored calibration of each question -------------------------------------------------------

def _path() -> str:
    return os.path.join(DATA_DIR, FILE_NAME)


def _read() -> Dict[str, Any]:
    try:
        with open(_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(data: Dict[str, Any]) -> bool:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = _path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, _path())
        return True
    except OSError:
        return False


def save(field: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """Store a successful calibration for ``field``; the decision then uses its threshold by default."""
    field = str(field or "").strip()[:120]
    if not field:
        return {"ok": False, "reason": "a question (field) name is needed"}
    if not result.get("ok") or result.get("threshold") is None:
        return {"ok": False, "reason": result.get("reason") or "nothing to save"}
    entry = {k: result.get(k) for k in ("threshold", "accepted", "errors", "upper_bound", "coverage", "samples",
                                        "max_error", "delta", "audit_rate")}
    entry["calibrated_at"] = time.time()
    with _LOCK:
        data = _read()
        data[field] = entry
        if not _write(data):
            return {"ok": False, "reason": "could not write the calibration file"}
    return {"ok": True, "field": field, **entry}


def forget(field: str) -> bool:
    with _LOCK:
        data = _read()
        if str(field or "").strip() not in data:
            return False
        del data[str(field).strip()]
        return _write(data)


def all_calibrations() -> Dict[str, Any]:
    return _read()


def threshold_for(field: str) -> Optional[float]:
    """The calibrated threshold of a question, or None when it has none."""
    entry = _read().get(str(field or "").strip())
    try:
        value = float(entry["threshold"]) if isinstance(entry, dict) else None
    except (TypeError, ValueError, KeyError):
        return None
    return value if value is not None and 0.0 <= value <= 1.0 else None


def should_audit(field: str, *, roll: float) -> bool:
    """True when this ACCEPTED answer should also go to the slow path. ``roll`` is a uniform [0,1) draw."""
    entry = _read().get(str(field or "").strip())
    try:
        rate = float(entry.get("audit_rate", DEFAULT_AUDIT_RATE)) if isinstance(entry, dict) else 0.0
    except (TypeError, ValueError):
        rate = 0.0
    return roll < rate
