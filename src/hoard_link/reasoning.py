"""How hard a chat call asks the model to reason.

A Hoard calls ``Link.chat`` for work of every size: a one-line title and a
graded exam answer went out the same way, with whatever the server's chat
template does by default. On a reasoning model that default is either
"think without a budget" (a short ``max_tokens`` then ends mid-thought with
no answer) or "never think" (the answer that needed reasoning gets none).
Faustus found the same gap in its own modes on 26-09-2026; this module is the
family's copy of the fix, in the words each local server reads.

Levels: ``off`` | ``low`` | ``medium`` | ``high`` | ``max`` (``xhigh``,
``deep`` and ``maximum`` read as ``max``; ``none`` and ``fast`` as ``off``).
``None`` (or ``auto``) sends nothing and leaves the server's default.

Per server:

* OpenAI-compatible (llama-server, vLLM, LM Studio):
  ``chat_template_kwargs.enable_thinking``, plus the budget under both names
  llama-server builds read (``thinking_budget_tokens`` is the one the current
  build honours; ``reasoning_budget`` the older one) and ``reasoning_effort``.
* Ollama: ``think`` (true/false).

When the model is asked to think and the caller capped the output, the cap
grows by the budget (reasoning comes out of the same tokens as the answer),
and the HTTP timeout grows with it. A server that answers 400 naming a
reasoning field gets the call again without those fields.
"""

from __future__ import annotations

import re
from typing import Any, Optional

LEVELS = ("off", "low", "medium", "high", "max")

#: Reasoning tokens per level.
BUDGETS = {"low": 1024, "medium": 4096, "high": 8192, "max": 16384}

_ALIASES = {
    "xhigh": "max", "maximum": "max", "deep": "max", "strongest": "max",
    "none": "off", "fast": "off", "false": "off", "0": "off",
    "minimal": "low", "light": "low", "think": "medium", "true": "medium",
}

#: Seconds of extra timeout per reasoning token (a 27B on one GPU reasons at
#: roughly 10-20 tokens/s; this leaves room for the slow end).
_SECONDS_PER_TOKEN = 0.1

#: Fields this module may add; a 400 that names one drops them all.
REASONING_KEYS = ("reasoning_effort", "reasoning_budget", "thinking_budget_tokens", "think")


def normalize(level: Any) -> Optional[str]:
    """A level from `LEVELS`, or None for "leave the server's default"."""
    if level is None or level is True:
        return None if level is None else "medium"
    if level is False:
        return "off"
    value = str(level).strip().lower()
    if not value or value == "auto":
        return None
    value = _ALIASES.get(value, value)
    return value if value in LEVELS else None


def budget_for(level: Optional[str]) -> int:
    return BUDGETS.get(level or "", 0)


def apply_openai(payload: dict[str, Any], level: Optional[str]) -> None:
    """Reasoning fields for an OpenAI-compatible chat payload."""
    if level is None:
        return
    ctk = payload.setdefault("chat_template_kwargs", {})
    if level == "off":
        ctk["enable_thinking"] = False
        return
    budget = BUDGETS[level]
    ctk["enable_thinking"] = True
    payload["thinking_budget_tokens"] = budget
    payload["reasoning_budget"] = budget
    payload["reasoning_effort"] = "high" if level == "max" else level
    _widen(payload, "max_tokens", budget)


def apply_ollama(payload: dict[str, Any], level: Optional[str]) -> None:
    """Reasoning fields for an Ollama /api/chat payload."""
    if level is None:
        return
    payload["think"] = level != "off"
    if level != "off":
        options = payload.get("options")
        if isinstance(options, dict):
            _widen(options, "num_predict", BUDGETS[level])


def timeout_for(base: float, level: Optional[str]) -> float:
    """The HTTP timeout for a call that reasons at `level`."""
    return float(base) + budget_for(level) * _SECONDS_PER_TOKEN


def looks_like_reasoning_error(status: int, text: str) -> bool:
    """A refusal caused by the reasoning fields: a 400/422 naming one, or a
    500 raised by the chat template itself while reading them (llama-server
    reports a template ``raise_exception`` as HTTP 500)."""
    low = str(text or "").lower()
    named = any(w in low for w in ("reasoning", "thinking", "think", "enable_thinking",
                                   "chat_template_kwargs", "effort", "budget"))
    if status in (400, 422):
        return named
    if status == 500:
        template = any(w in low for w in ("raise_exception", "while executing", "template"))
        return template and (named or "unexpected" in low)
    return False


#: Effort names in increasing strength, as chat templates spell them.
_EFFORT_ORDER = ("minimal", "low", "medium", "high", "xhigh", "max")


def supported_efforts(text: str) -> list[str]:
    """The effort names a template error lists as supported
    ("Supported types are xhigh (default), medium, and low"), in order."""
    m = re.search(r"supported (?:types|values|efforts?)(?: are|:)\s*([^.\n\"]+)", str(text or ""), re.I)
    if not m:
        return []
    found = re.findall(r"[a-z]+", m.group(1).lower())
    return [w for w in found if w in _EFFORT_ORDER]


def remap_effort(payload: dict[str, Any], supported: list[str]) -> bool:
    """Replace ``reasoning_effort`` by the nearest supported name; between two
    equally near names the lighter one, so the call still fits the budget and
    timeout computed for the level that was asked. True when it changed."""
    current = payload.get("reasoning_effort")
    if not supported or not isinstance(current, str) or current in supported:
        return False
    rank = {name: i for i, name in enumerate(_EFFORT_ORDER)}
    want = rank.get(current)
    if want is None:
        return False
    best = min(supported, key=lambda s: (abs(rank[s] - want), rank[s]))
    payload["reasoning_effort"] = best
    return True


def strip(payload: dict[str, Any]) -> bool:
    """Remove what `apply_*` added; True when something was removed."""
    removed = False
    for key in REASONING_KEYS:
        if key in payload:
            payload.pop(key, None)
            removed = True
    ctk = payload.get("chat_template_kwargs")
    if isinstance(ctk, dict) and "enable_thinking" in ctk:
        ctk.pop("enable_thinking", None)
        if not ctk:
            payload.pop("chat_template_kwargs", None)
        removed = True
    return removed


def _widen(target: dict[str, Any], key: str, budget: int) -> None:
    try:
        current = int(target.get(key) or 0)
    except (TypeError, ValueError):
        return
    if current > 0:
        target[key] = current + budget
