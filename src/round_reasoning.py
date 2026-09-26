"""Per-round reasoning budget for agent turns.

Measured on the exam run of 26-09-2026 (local 27B, llama-server): every
round spent its whole 4096-token reasoning budget, at ~8.5 tokens/s, so a
round cost ~9 minutes and the prompt prefill only 25-50 s of that. Most of
those rounds were execution steps after the plan was already set ("fetch the
remaining coordinates, then compute"), where the model re-walked reasoning it
had written the round before (the newest round's reasoning stays in the
prompt).

``agent_followup_reasoning_budget`` caps the budget of those follow-up
rounds. The first round keeps the full budget, and so does any round that
follows a tool failure, a refusal or a harness note, because that is where
the model has something new to think about. 0 (the default) leaves every
round as it was; the value is meant to be set after an A/B on the eval
harness, not guessed.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Mapping, Optional, Tuple

SETTING_KEY = "agent_followup_reasoning_budget"


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def tool_result_is_trouble(result: Any) -> bool:
    """A tool result the next round has to think about: an error, a non-zero
    exit, a refusal or an argument problem."""
    if not isinstance(result, Mapping):
        return False
    if result.get("blocked") or result.get("argument_errors"):
        return True
    if result.get("error"):
        return True
    if result.get("success") is False:
        return True
    code = result.get("exit_code")
    return isinstance(code, int) and not isinstance(code, bool) and code != 0


def followup_overrides(
    gen_overrides: Optional[Mapping[str, Any]],
    *,
    round_num: int,
    trouble: bool,
    pinned: bool,
    get_setting: Callable[[str, Any], Any],
) -> Tuple[Optional[Dict[str, Any]], Optional[int]]:
    """The overrides to send for this round and the budget applied (None when
    unchanged). Never mutates ``gen_overrides``; never raises a budget."""
    if not isinstance(gen_overrides, Mapping) or not gen_overrides:
        return (dict(gen_overrides) if isinstance(gen_overrides, Mapping) else gen_overrides), None
    cap = _int(get_setting(SETTING_KEY, 0))
    if cap <= 0 or pinned or trouble or round_num <= 1:
        return dict(gen_overrides), None
    if gen_overrides.get("think") is not True:
        return dict(gen_overrides), None
    current = _int(gen_overrides.get("reasoning_budget"))
    if current and current <= cap:
        return dict(gen_overrides), None
    out = dict(gen_overrides)
    out["reasoning_budget"] = cap
    return out, cap


def harness_note_pending(messages: Any) -> bool:
    """True when the harness added a note since the model's last message (a
    nudge, a verification result, a cut-off carry): that round has something
    new to weigh, so it keeps the full budget."""
    if not isinstance(messages, list):
        return False
    for msg in reversed(messages):
        if not isinstance(msg, Mapping):
            continue
        if msg.get("role") == "assistant":
            return False
        if msg.get("_harness_note"):
            return True
    return False
