"""How hard each mode asks the model to think.

The chat has its reasoning chip (src/think_mode.py) and `delegate_agents` its
per-task effort (src/effort_profile.py). Every other mode called the model
through the helper path, which turns thinking off so titles and summaries
stay fast: Deep Research planned, extracted and wrote its report with the
27B not thinking at all, and a council member or the teacher answered the
same way (found 26-09-2026). This module gives each mode a level of its own,
a setting to change it (`mode_effort_<mode>`) and a way for a caller (the
Research screen's selector) to pick one per run.

Levels: auto (the mode's default) | off | low | medium | high | max.
"max" asks each provider for its strongest setting; src/llm_core.py and
src/provider_reasoning.py fit it to what the model accepts (a llama-server
template's top level, Claude's "max"/"xhigh", OpenRouter's "max") and step
down on a model that refuses it.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

LEVELS = ("auto", "off", "low", "medium", "high", "max")

#: Each mode's default. Only modes whose quality is the point think by
#: default. Swarm items and the answer reviews are not here: they answer in
#: a fixed JSON shape, and constrained decoding does not mix with reasoning
#: on every engine, so they keep the fast helper path.
MODE_DEFAULTS: Dict[str, str] = {
    "research": "max",
    # Reading one page of a research run: dozens per run, so it keeps the
    # fast path unless the owner raises it.
    "research_reading": "off",
    "council": "high",
    "teacher": "max",
    # Asking another model a question (`chat_with_model`): a consultation is
    # for its considered answer.
    "consult": "high",
    # Tournament contestants and blind rounds (src/tournament.py): the answers
    # are compared on quality.
    "tournament": "high",
    # bug_hunt's edge-case generation and triage, and the CI failure
    # analysis, run on the small utility model by design (fast, background);
    # they keep its default unless the owner raises them in Settings.
    "bug_hunt": "auto",
    "ci_analysis": "auto",
}

_EFFORT_WORD = {"low": "low", "medium": "medium", "high": "high", "max": "max"}


def normalize(level: Optional[str]) -> str:
    value = str(level or "").strip().lower()
    aliases = {"xhigh": "max", "maximum": "max", "none": "off", "fast": "off",
               "think": "medium", "deep": "max", "": "auto"}
    value = aliases.get(value, value)
    return value if value in LEVELS else "auto"


def _budgets() -> Dict[str, int]:
    out = {"low": 1024, "medium": 4096, "high": 8192, "max": 16384}
    try:
        from src.settings import get_setting
        out["low"] = int(get_setting("think_mode_budget_light", 1024) or 1024)
        out["medium"] = int(get_setting("think_mode_budget_think", 4096) or 4096)
        out["max"] = int(get_setting("think_mode_budget_deep", 16384) or 16384)
        out["high"] = max(out["medium"], min(out["max"], out["medium"] * 2))
    except Exception:  # noqa: BLE001 - settings unavailable: the defaults above
        pass
    return out


def overrides_for_level(level: str) -> Optional[Dict[str, Any]]:
    """`gen_overrides` for a concrete level; None for "auto" (nothing to add)."""
    level = normalize(level)
    if level == "auto":
        return None
    if level == "off":
        return {"think": False}
    return {"think": True, "reasoning_effort": _EFFORT_WORD[level],
            "reasoning_budget": _budgets()[level]}


def level_for(mode: str, requested: Optional[str] = None) -> str:
    """The level a mode runs at: the caller's pick, else the owner's setting,
    else the mode's default."""
    picked = normalize(requested)
    if picked != "auto":
        return picked
    saved = "auto"
    try:
        from src.settings import get_setting
        saved = normalize(get_setting(f"mode_effort_{mode}", "auto"))
    except Exception:  # noqa: BLE001
        pass
    if saved != "auto":
        return saved
    return MODE_DEFAULTS.get(mode, "auto")


def for_mode(mode: str, requested: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """`gen_overrides` for one call of `mode`, or None to leave it as is."""
    return overrides_for_level(level_for(mode, requested))


def from_chat(think_mode: Optional[str] = None, reasoning_effort: Optional[str] = None) -> str:
    """A chat composer's pick, read as a mode level (a chat in research mode
    runs Deep Research with the chip's choice): the model's own level wins,
    then the mode chip; "auto" leaves the mode's default."""
    effort = str(reasoning_effort or "").strip().lower()
    if effort and effort != "auto":
        return normalize({"minimal": "low"}.get(effort, effort))
    chip = str(think_mode or "").strip().lower()
    return {"fast": "off", "think": "high", "deep": "max"}.get(chip, "auto")


def timeout_for(overrides: Optional[Dict[str, Any]], base: float) -> float:
    """A call's timeout with room for the reasoning it asks for (about 20
    tokens a second, the slow end of a local 27B)."""
    if not overrides or not overrides.get("think"):
        return float(base)
    try:
        budget = int(overrides.get("reasoning_budget") or 0)
    except (TypeError, ValueError):
        budget = 0
    return float(base) + budget / 20.0
