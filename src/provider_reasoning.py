"""Reasoning controls in each provider's own words.

Faustus asks for reasoning in one vocabulary (`gen_overrides`: ``think``,
``reasoning_effort`` from none to max, ``reasoning_budget``). A local
llama-server reads ``chat_template_kwargs.enable_thinking`` and a budget;
Ollama reads ``think``; OpenAI reads ``reasoning_effort``. Two routes had no
reasoning at all until 26-09: the Anthropic API (no ``thinking`` or
``output_config.effort`` was ever sent, so Claude never thought through
Faustus) and OpenRouter (no ``reasoning`` object). This module fills those
in, so whichever model wears the harness gets its own strongest setting when
a mode asks for it.

API facts it relies on (checked 26-09-2026 against the providers' docs):

* Anthropic: ``thinking: {"type": "adaptive"}`` on Claude 4.6 and later
  (``"enabled"`` with ``budget_tokens`` is rejected from 4.7 on); older
  models take ``{"type": "enabled", "budget_tokens": n}`` with n >= 1024 and
  below ``max_tokens``. ``output_config.effort`` accepts low / medium / high
  on models that support effort, ``max`` on Opus/Sonnet 4.6+ and the 5.x
  line, ``xhigh`` on Opus 4.7+, Sonnet 5 and later. Effort works with or
  without thinking.
* OpenRouter: ``reasoning: {"effort": ...}`` with none / minimal / low /
  medium / high / xhigh / max; it maps the value to each vendor.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple

#: Faustus's effort words, low to high.
EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

#: Cap for Claude's max_tokens once thinking room is added.
_ANTHROPIC_MAX_TOKENS_CAP = 64000

_CLAUDE_RE = re.compile(
    r"(?<![a-z])(opus|sonnet|haiku|fable|mythos)-(\d{1,2})(?:[-.](\d{1,2})(?!\d))?(?!\d)",
    re.IGNORECASE,
)


def claude_version(model: str) -> Optional[Tuple[str, int, int]]:
    """``(family, major, minor)`` of a Claude model id, or None.

    ``claude-opus-4-7-20260201`` -> ("opus", 4, 7); ``claude-sonnet-5`` ->
    ("sonnet", 5, 0); a legacy ``claude-3-7-sonnet`` -> ("sonnet", 3, 7)."""
    name = str(model or "").lower()
    if "claude" not in name and not any(f in name for f in ("fable", "mythos")):
        return None
    hit = _CLAUDE_RE.search(name)
    if hit:
        return hit.group(1), int(hit.group(2)), int(hit.group(3) or 0)
    legacy = re.search(r"claude-(\d)-(\d)-(opus|sonnet|haiku)", name)
    if legacy:
        return legacy.group(3), int(legacy.group(1)), int(legacy.group(2))
    legacy = re.search(r"claude-(\d)-(opus|sonnet|haiku)", name)
    if legacy:
        return legacy.group(2), int(legacy.group(1)), 0
    return None


def anthropic_capabilities(model: str) -> Dict[str, Any]:
    """What a Claude model accepts: adaptive thinking, and which efforts."""
    ver = claude_version(model)
    if ver is None:
        name = str(model or "").lower()
        if any(f in name for f in ("fable", "mythos")):
            ver = ("fable" if "fable" in name else "mythos", 5, 0)
        else:
            return {"adaptive": False, "thinking": False, "efforts": ()}
    family, major, minor = ver
    v = (major, minor)
    newest = family in ("fable", "mythos") or major >= 5
    efforts = []
    if family != "haiku" and (newest or v >= (4, 5)):
        efforts = ["low", "medium", "high"]
        if newest or v >= (4, 6):
            efforts.append("max")
        if (family in ("fable", "mythos") or (family == "opus" and v >= (4, 7))
                or (family == "sonnet" and major >= 5)):
            efforts.append("xhigh")
    return {
        "adaptive": newest or v >= (4, 6),
        "thinking": newest or v >= (3, 7),
        "efforts": tuple(efforts),
    }


def fit_effort(value: Optional[str], accepted) -> Optional[str]:
    """The nearest level in `accepted` to `value`, never above what exists;
    "xhigh" and "max" fall back to each other before stepping down."""
    value = str(value or "").strip().lower()
    accepted = tuple(accepted or ())
    if not value or not accepted:
        return None
    if value in accepted:
        return value
    if value == "minimal" and "low" in accepted:
        return "low"
    if value in ("xhigh", "max"):
        other = "max" if value == "xhigh" else "xhigh"
        if other in accepted:
            return other
    ranked = [v for v in EFFORT_ORDER if v in accepted]
    if value not in EFFORT_ORDER:
        return None
    want = EFFORT_ORDER.index(value)
    below = [v for v in ranked if EFFORT_ORDER.index(v) <= want]
    return below[-1] if below else ranked[0]


def _requested(overrides: Optional[Dict[str, Any]]) -> Tuple[Optional[bool], Optional[str], Optional[int]]:
    ov = overrides if isinstance(overrides, dict) else {}
    think = ov.get("think")
    think = None if think is None else bool(think)
    effort = str(ov.get("reasoning_effort") or "").strip().lower() or None
    try:
        budget = int(ov["reasoning_budget"]) if ov.get("reasoning_budget") is not None else None
    except (TypeError, ValueError):
        budget = None
    return think, effort, budget


def apply_anthropic(payload: Dict[str, Any], model: str, overrides: Optional[Dict[str, Any]],
                    *, allow_thinking: bool = True) -> bool:
    """Put the asked-for reasoning on an Anthropic Messages payload.

    `allow_thinking` False sends only the effort (a tool loop whose history
    does not carry thinking blocks back must not turn thinking on). Returns
    True when anything was added."""
    think, effort, budget = _requested(overrides)
    if think is None and effort is None:
        return False
    if think is False and effort in (None, "none"):
        return False
    caps = anthropic_capabilities(model)
    changed = False
    if effort and effort != "none" and caps["efforts"]:
        fitted = fit_effort(effort, caps["efforts"])
        if fitted:
            payload.setdefault("output_config", {})["effort"] = fitted
            changed = True
    if think and allow_thinking and caps["thinking"]:
        room = max(1024, int(budget or 4096))
        if caps["adaptive"]:
            payload["thinking"] = {"type": "adaptive"}
        else:
            payload["thinking"] = {"type": "enabled", "budget_tokens": room}
        base = int(payload.get("max_tokens") or 4096)
        payload["max_tokens"] = min(_ANTHROPIC_MAX_TOKENS_CAP, base + room)
        if payload["thinking"].get("budget_tokens", 0) >= payload["max_tokens"]:
            payload["thinking"]["budget_tokens"] = max(1024, payload["max_tokens"] - 1024)
        # Thinking does not take sampler changes.
        for key in ("temperature", "top_p", "top_k"):
            payload.pop(key, None)
        changed = True
    return changed


def apply_openrouter(payload: Dict[str, Any], overrides: Optional[Dict[str, Any]]) -> bool:
    """OpenRouter's `reasoning` object; it maps the effort to each vendor."""
    think, effort, budget = _requested(overrides)
    if think is None and effort is None:
        return False
    if think is False:
        return False
    level = effort if effort in EFFORT_ORDER and effort != "none" else "high"
    payload["reasoning"] = {"effort": level}
    return True


#: Payload keys that carry reasoning, per provider. A 400 that names
#: reasoning drops these and retries once without them.
REASONING_KEYS = ("reasoning_effort", "thinking", "output_config", "reasoning",
                  "reasoning_budget", "thinking_budget_tokens")


def looks_like_reasoning_error(status: int, text: str) -> bool:
    if status != 400:
        return False
    low = str(text or "").lower()
    return any(w in low for w in ("reasoning", "effort", "thinking", "budget_tokens", "output_config"))


def strip_reasoning(payload: Dict[str, Any]) -> bool:
    """Remove every reasoning field; True when something was removed."""
    removed = False
    for key in REASONING_KEYS:
        if key in payload:
            payload.pop(key, None)
            removed = True
    ctk = payload.get("chat_template_kwargs")
    if isinstance(ctk, dict) and ctk.get("enable_thinking") is True:
        ctk["enable_thinking"] = False
        removed = True
    return removed
