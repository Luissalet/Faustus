"""The reasoning levels a model's own chat template accepts, for the
composer's effort control.

Qwen3.8's template lists ``xhigh`` / ``medium`` / ``low`` (default ``xhigh``)
and raises on anything else; a generic Low/Medium/High selector would send
"high" and get HTTP 500. The levels come from the running server when it can
say (a local llama-server's ``/props`` template), never from a guess.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh")


def levels_for(endpoint_url: str) -> Optional[Dict[str, Any]]:
    """``{"levels": [...low to high], "default": str|None, "source": str}``,
    or None when the endpoint does not say what it accepts."""
    try:
        from src.chat_helpers import llamacpp_reasoning_efforts
        accepted = llamacpp_reasoning_efforts(endpoint_url or "")
    except Exception:  # noqa: BLE001 - unknown means no control
        return None
    if not accepted:
        return None
    ranked = sorted(accepted, key=lambda v: _ORDER.index(v) if v in _ORDER else len(_ORDER))
    return {"levels": list(ranked), "default": accepted[0], "source": "template"}


def overrides_for(effort: str, *, budgets: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
    """``gen_overrides`` for an explicit level picked in the composer.

    "none" turns thinking off; any other level thinks, with the level passed
    through (``llm_core`` fits it to the template) and a budget that grows
    with it."""
    level = str(effort or "").strip().lower()
    if level in ("", "auto"):
        return {}
    if level == "none":
        return {"think": False}
    b = {"light": 1024, "think": 4096, "deep": 16384}
    b.update(budgets or {})
    if level in ("minimal", "low"):
        budget = b["light"]
    elif level == "medium":
        budget = b["think"]
    else:
        budget = b["deep"]
    return {"think": True, "reasoning_effort": level, "reasoning_budget": budget}
