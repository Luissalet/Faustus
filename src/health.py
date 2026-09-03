"""health.py — a health score that cannot lie by staying silent.

Two design decisions, and both of them are about refusing a comfortable
default:

* **A machine nothing has been collected from scores near ZERO, not "healthy
  by default".** Absence of signal is not absence of problem. A dashboard that
  paints green because no collector ran is worse than no dashboard: it is a
  claim, made with no evidence, that the caller will believe.
* **A component with no data source says so.** It contributes **0** to the
  score, it is listed in `missing`, and its state is `no_data` — never a
  plausible zero that reads like a measurement.

    score(signals) -> {"score": 0..100, "grade", "collected": bool,
                       "components": [{"name", "value", "weight", "state", "why"}],
                       "missing": [name, ...]}

`signals` maps a component name to a READING the caller actually took:

    {"ollama": {"state": "ok", "value": True, "why": "reachable at …"},
     "gpu":    None}                       # nothing collected this

This module owns the weights, the arithmetic and the grade; it never collects
anything and it never invents a reading. That separation is the point: the
call site can only report what it really measured, and a component nobody
measured is visibly missing rather than quietly average.

Pure, stdlib, total — `score` returns a document for any input.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

SCHEMA_VERSION = 1

#: The states a reading may carry. `no_data` is not a bad reading — it is the
#: absence of one, and it scores the same as the worst case on purpose.
STATES = ("ok", "warn", "bad", "no_data")

#: What each state is worth, as a fraction of the component's weight.
STATE_FACTOR: Dict[str, float] = {"ok": 1.0, "warn": 0.5, "bad": 0.0, "no_data": 0.0}

#: The default component set: what /api/system/usage can really source.
#: (name, weight, label) — the weights sum to 100.
DEFAULT_COMPONENTS: Tuple[Tuple[str, int, str], ...] = (
    ("ollama", 20, "Ollama reachable"),
    ("gpu", 15, "GPU visible to nvidia-smi"),
    ("vram", 15, "VRAM headroom"),
    ("host", 15, "RAM headroom"),
    ("disk", 15, "Disk headroom"),
    ("runners", 10, "No orphaned runners"),
    ("dispatch", 10, "Recent dispatched jobs"),
)

_GRADES: Tuple[Tuple[int, str], ...] = ((90, "A"), (75, "B"), (60, "C"), (40, "D"), (0, "F"))

_NO_SOURCE = "no data source yet — nothing has reported this, which is not the same as nothing being wrong"


def enabled() -> bool:
    """Setting ``agent_health_score``. Off = the endpoints answer exactly what
    they answered before this module existed (no `health` block at all)."""
    try:
        from src.settings import get_setting
        return bool(get_setting("agent_health_score", True))
    except Exception:  # noqa: BLE001 - never raise into a collector
        return True


def grade_for(score: Any) -> str:
    try:
        n = float(score)
    except (TypeError, ValueError):
        return "F"
    for floor, letter in _GRADES:
        if n >= floor:
            return letter
    return "F"


def reading(state: str, why: str, value: Any = None) -> Dict[str, Any]:
    """One component's reading, for a caller assembling `signals`."""
    st = str(state or "").strip().lower()
    return {"state": st if st in STATES else "no_data", "why": str(why or ""), "value": value}


def _norm(raw: Any) -> Optional[Dict[str, Any]]:
    """A reading, or None when the caller reported nothing for it. A bare
    "ok"/"warn"/"bad" string and a bare bool are accepted as shorthand."""
    if raw is None:
        return None
    if isinstance(raw, dict):
        state = str(raw.get("state") or "").strip().lower()
        if state not in STATES:
            state = "no_data"
        return {"state": state, "why": str(raw.get("why") or ""), "value": raw.get("value")}
    if isinstance(raw, bool):
        return {"state": "ok" if raw else "bad", "why": "", "value": raw}
    text = str(raw).strip().lower()
    if text in STATES:
        return {"state": text, "why": "", "value": raw}
    return {"state": "no_data", "why": "", "value": raw}


def score(signals: Any, components: Optional[Sequence[Tuple[str, int, str]]] = None) -> Dict[str, Any]:
    """The health document. See the module docstring: a component with no
    reading contributes 0 and is listed in `missing`, and `collected` is False
    when not one component reported anything at all."""
    comps = tuple(components or DEFAULT_COMPONENTS)
    try:
        given = signals if isinstance(signals, dict) else {}
    except Exception:  # noqa: BLE001
        given = {}
    rows: List[Dict[str, Any]] = []
    missing: List[str] = []
    total_weight = 0
    earned = 0.0
    collected = False
    for entry in comps:
        try:
            name, weight, label = str(entry[0]), int(entry[1]), str(entry[2])
        except Exception:  # noqa: BLE001 - a malformed registry entry is skipped
            continue
        r = _norm(given.get(name))
        if r is None:
            r = {"state": "no_data", "why": _NO_SOURCE, "value": None}
            missing.append(name)
        elif r["state"] == "no_data":
            if not r["why"]:
                r["why"] = _NO_SOURCE
            missing.append(name)
        else:
            collected = True
        total_weight += weight
        earned += weight * STATE_FACTOR.get(r["state"], 0.0)
        rows.append({"name": name, "label": label, "value": r["value"], "weight": weight,
                     "state": r["state"], "why": r["why"]})
    pct = (100.0 * earned / total_weight) if total_weight else 0.0
    out = {
        "score": int(round(pct)),
        "grade": grade_for(pct),
        "collected": collected,
        "components": rows,
        "missing": missing,
        "reporting": len(rows) - len(missing),
        "of": len(rows),
        "schema_version": SCHEMA_VERSION,
    }
    if not collected:
        out["why"] = ("nothing has been collected from this machine yet — this is a score of zero because "
                      "absence of signal is not absence of problem, not because anything was measured as bad")
    return out


def summary(doc: Any) -> str:
    """One line: ``42/100 (D) · 3 of 7 signals reporting``."""
    try:
        d = doc or {}
        return (f"{d.get('score')}/100 ({d.get('grade')}) · "
                f"{d.get('reporting')} of {d.get('of')} signals reporting"
                + ("" if d.get("collected") else " · nothing collected"))
    except Exception:  # noqa: BLE001
        return ""


def names(components: Optional[Iterable[Tuple[str, int, str]]] = None) -> List[str]:
    return [str(c[0]) for c in (components or DEFAULT_COMPONENTS)]
