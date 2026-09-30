"""Pure decisions of the agent loop's closing stage (H07).

When a round ends with text and no tool call the loop has to decide what
happens next: continue a cut-off reply, send a call to a tool that does not
exist back for correction, nudge an empty round, ask the user after the nudges
ran out, correct a reply in the wrong language, or go on to the claim checks
that close the turn. That order used to be spread over consecutive `if`
blocks inside the generator. It is stated here, once, as a function of plain
values, so a test can walk the whole transition table without a model.

The function is pure and dependency-injected: the loop passes `permits`, the
callable that decides whether another extra round of a cause is granted
(`src.extra_round_policy.ExtraRounds.permits`), and everything else is data.
It returns the action only; the effects (messages, events, counters) stay in
the loop, which applies the action it is given.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

#: Actions of `decide_text_round`, in the order they are considered.
CONTINUE_LENGTH = "continue_length"
CORRECT_UNKNOWN_TOOL = "correct_unknown_tool"
NUDGE_EMPTY = "nudge_empty"
ASK_EMPTY_EXHAUSTED = "ask_empty_exhausted"
CORRECT_LANGUAGE = "correct_language"
CHECK_CLAIMS = "check_claims"

ACTIONS = (CONTINUE_LENGTH, CORRECT_UNKNOWN_TOOL, NUDGE_EMPTY, ASK_EMPTY_EXHAUSTED, CORRECT_LANGUAGE, CHECK_CLAIMS)

Permits = Callable[[str, int, int], bool]


@dataclass(frozen=True)
class TextRoundState:
    """What is known when a text-only round ends. Counters are the loop's own."""

    finish_reason: str
    length_continues: int
    length_limit: int
    dropped_tool_calls: bool
    unknown_tool_nudges: int
    unknown_tool_limit: int
    empty_give_up: bool
    empty_nudges: int
    empty_limit: int
    wrong_language: bool
    language_nudges: int
    round_num: int
    max_rounds: int


def decide_text_round(state: TextRoundState, permits: Permits) -> str:
    """The action for a text-only round, first rule that applies.

    1. the reply was cut off by the token limit and continuations remain
    2. a native call named a tool that does not exist and corrections remain
    3. the round was empty where the model was supposed to act: nudge while
       nudges remain, otherwise ask the user (never end in silence)
    4. the reply is in another language than the request, once, and there is
       a round left to redo it in
    otherwise the turn goes on to the claim checks.
    """
    if state.finish_reason == "length" and permits("length", state.length_continues, state.length_limit):
        return CONTINUE_LENGTH
    if state.dropped_tool_calls and permits("hallucinated_tool", state.unknown_tool_nudges, state.unknown_tool_limit):
        return CORRECT_UNKNOWN_TOOL
    if state.empty_give_up:
        if permits("empty_completion", state.empty_nudges, state.empty_limit):
            return NUDGE_EMPTY
        if state.empty_nudges >= state.empty_limit:
            return ASK_EMPTY_EXHAUSTED
    if (state.wrong_language and permits("language_mismatch", state.language_nudges, 1)
            and state.round_num < state.max_rounds):
        return CORRECT_LANGUAGE
    return CHECK_CLAIMS
