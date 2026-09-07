"""Pure decision for whether the cost of branching is justified."""
from __future__ import annotations

from typing import Any, Dict, Mapping


def decide(signals: Mapping[str, Any] | None = None) -> Dict[str, Any]:
    data = dict(signals or {})
    if data.get("requested") is True:
        return {"activate": True, "reason": "explicitly_requested", "score": 1.0}
    if data.get("trivial") is True:
        return {"activate": False, "reason": "trivial_task", "score": 0.0}
    components = {
        key: max(0.0, min(1.0, float(data.get(key, 0.0) or 0.0)))
        for key in ("uncertainty", "risk", "value", "strategy_diversity")
    }
    score = (components["uncertainty"] * 0.30 + components["risk"] * 0.25
             + components["value"] * 0.25 + components["strategy_diversity"] * 0.20)
    return {"activate": score >= 0.55, "reason": "threshold_met" if score >= 0.55 else "single_path_is_enough",
            "score": round(score, 4), "signals": components}
