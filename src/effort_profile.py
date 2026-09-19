"""Per-task delegation "effort" profiles.

`delegate_agents` (src/agent_tools/subagent_tools.py) lets a task say how hard
its worker should think: `effort: "low" | "medium" | "high"`. `resolve()` maps
that word to the request options a child run should use — the same
`gen_overrides` vocabulary src/llm_core.py already understands (`think`,
`reasoning_effort`, `reasoning_budget`), plus a short system hint appended to
the worker's preamble.

This module makes no network call and probes no backend: it only decides what
to ASK for. src/llm_core.py already decides what a given backend can actually
carry — an Ollama /v1 target ignores `think` unless routed to `/api/chat`
(`_route_for_gen_overrides`), a thinking-incapable model gets no thinking
field at all (`_resolve_think_decision`), and a plain remote chat provider
that has no notion of `reasoning_effort` just receives (and typically ignores
or 400s on) an extra JSON field the same way any other saved gen_overrides
key would. So for a backend with no thinking controls, the `gen_overrides`
this returns are inert and only the `hint` actually changes behaviour — which
is the documented fallback.

`effort` omitted, unknown, or explicitly "medium" resolves to
`{"gen_overrides": None, "hint": ""}` — today's behaviour, unchanged, so a
caller that merges `None` over its existing overrides changes nothing.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

EFFORT_LEVELS = ("low", "medium", "high")

#: Mirrors `local_openai_reasoning_budget_default`'s own fallback
#: (src/settings.py) — the budget a self-hosted thinking-capable model gets
#: when nothing overrides it.
DEFAULT_REASONING_BUDGET = 4096
#: What "high" asks for instead: twice the ordinary default. A future caller
#: that knows a specific model's real reasoning-token ceiling can still pass
#: a tighter `reasoning_budget` of its own — this is only the level's default.
HIGH_REASONING_BUDGET = 8192

_HINTS: Dict[str, str] = {
    "low": "Be brief; do the task directly.",
    "high": "Think carefully and verify before answering.",
}


def normalize(effort: Optional[str]) -> str:
    """Canonical effort level, or "" when it should be treated as "inherit
    current behaviour" — missing, blank, unrecognised, or explicitly
    "medium"."""
    value = str(effort or "").strip().lower()
    return value if value in EFFORT_LEVELS else ""


def resolve(effort: Optional[str], model: str = "", endpoint: str = "") -> Dict[str, Any]:
    """Map an `effort` level to `{"gen_overrides": dict | None, "hint": str}`.

    `gen_overrides`, when not None, is meant to be merged OVER whatever
    request options the caller already had (a saved per-model default still
    applies to any key this does not set) — never to replace them outright.

    `model`/`endpoint` are accepted so a future caller can gate on backend
    capability before merging (e.g. skip the thinking keys for a provider it
    already knows has none); this function itself does not need to probe
    either to answer correctly, since llm_core.py already drops any knob a
    given backend does not understand.
    """
    level = normalize(effort)
    if level in ("", "medium"):
        return {"gen_overrides": None, "hint": ""}
    if level == "low":
        return {
            "gen_overrides": {
                "think": False,
                "reasoning_effort": "low",
                "reasoning_budget": 0,
            },
            "hint": _HINTS["low"],
        }
    # "high"
    return {
        "gen_overrides": {
            "think": True,
            "reasoning_effort": "high",
            "reasoning_budget": HIGH_REASONING_BUDGET,
        },
        "hint": _HINTS["high"],
    }


def merge_gen_overrides(base: Optional[Dict[str, Any]], effort_overrides: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """`effort_overrides` (from `resolve()`) applied OVER `base`. Returns
    `base` unchanged (same object, so a caller can compare `is`) when there
    is nothing to merge — this is the "default path unchanged" case."""
    if not effort_overrides:
        return base
    merged = dict(base) if isinstance(base, dict) else {}
    merged.update(effort_overrides)
    return merged
