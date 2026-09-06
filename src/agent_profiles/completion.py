"""
agent_profiles/completion.py — how far a run may go, and why it thinks so.

Faustus already answers "who works" in `src/agent_defs.py`.  This module
answers the orthogonal question the plan keeps deliberately separate (§1):
*how far does this particular mission push before it stops*.  A completion
mode is depth, never authority (§3.3), so the most important fact about this
file is what it cannot express.  There is no tool list here, no path, no
effect, no work root and no permission rule — and no field in which one could
be written.  `maximalist` handed to a read-only reviewer is a reviewer that
looks harder and still cannot write a byte, and that holds because this module
has no vocabulary for saying otherwise, not because someone remembered.

Two more things are load-bearing.

**The precedence is data.**  §6 orders six places a mode can come from.  A
chain of `if`s would answer "greedy" without being able to say why, and "why
did it explore for forty minutes?" is precisely the question this layer exists
to answer.  `MODE_PRECEDENCE` is a tuple, `resolve_mode` walks it, and the
source that won travels with the answer inside `CompletionChoice.reason`.

**Reading a mode out of a sentence is conservative.**  `from_instruction`
matches a small fixed vocabulary in Spanish and English, and returns `""` the
moment two readings survive.  It never calls a model.  The two errors are not
symmetric: a wrong `literal` costs one follow-up message, while a wrong
`maximalist` spends an afternoon of GPU the user never offered.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

from .contracts import COMPLETION_MODES, CompletionChoice, ProfileError

logger = logging.getLogger(__name__)

__all__ = [
    "CompletionPolicy",
    "POLICIES",
    "MODE_ORDER",
    "MODE_PRECEDENCE",
    "policy_for",
    "resolve_mode",
    "from_instruction",
    "transition",
    "narrows",
]


# ── what a mode is ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CompletionPolicy:
    """The versioned meaning of one completion mode.

    Every field answers a question about *depth*: does the run look for work
    beyond the literal ask, how much of the budget may that work take, may it
    stop as soon as the core is proved, how many finishing layers it may add,
    and whether verification is owed.

    Deliberately absent, and it must stay that way: tools, denies, paths, work
    roots, effects, permission rules, network, secrets.  §3.3 says a mode
    grants nothing; the cheapest way to keep that true after twenty commits is
    to give the type no field in which a grant could be written.  A test in
    `tests/test_completion_modes.py` reads these field names back and fails if
    one that smells of authority ever appears.
    """

    mode: str
    policy_version: str
    description: str
    explore_frontier: bool
    bonus_budget_share: float
    stop_on_core_proved: bool
    max_extra_layers: int
    requires_verification: bool


#: Narrow to wide.  This is the strictness axis `narrows()` reads, and the
#: reason it lives here rather than being inferred from `COMPLETION_MODES` is
#: that a tuple's order in another module is not a contract we should lean on
#: for a semantic ordering.  A test asserts the two stay the same set.
MODE_ORDER: Tuple[str, ...] = ("literal", "professional", "greedy", "maximalist")

POLICIES: Dict[str, CompletionPolicy] = {
    "literal": CompletionPolicy(
        mode="literal",
        policy_version="literal_v1",
        description=(
            "Do the asked thing and nothing adjacent to it. Minimum diff, "
            "strict scope envelope, stop the moment the core is proved."
        ),
        explore_frontier=False,
        bonus_budget_share=0.0,
        stop_on_core_proved=True,
        max_extra_layers=0,
        requires_verification=True,
    ),
    "professional": CompletionPolicy(
        mode="professional",
        policy_version="professional_v1",
        description=(
            "Deliver the thing finished: tests, error handling, the "
            "documentation the change makes stale. No frontier search."
        ),
        explore_frontier=False,
        bonus_budget_share=0.15,
        stop_on_core_proved=False,
        max_extra_layers=1,
        requires_verification=True,
    ),
    "greedy": CompletionPolicy(
        mode="greedy",
        policy_version="greedy_v1",
        description=(
            "Professional finish plus the near-equivalent cases and the "
            "high-return neighbours the work exposes. Bounded frontier."
        ),
        explore_frontier=True,
        bonus_budget_share=0.35,
        stop_on_core_proved=False,
        max_extra_layers=2,
        requires_verification=True,
    ),
    "maximalist": CompletionPolicy(
        mode="maximalist",
        policy_version="maximalist_v1",
        description=(
            "Explore alternatives and produce variants worth comparing. The "
            "widest depth Faustus offers; still not one extra permission."
        ),
        explore_frontier=True,
        bonus_budget_share=0.60,
        stop_on_core_proved=False,
        max_extra_layers=4,
        requires_verification=True,
    ),
}

# `requires_verification` is True in all four on purpose, and the field earns
# its place by saying so out loud: depth is negotiable, evidence is not.  A
# future mode that wanted to skip `prove` would have to write the False and
# defend it in review, which is exactly the friction we want.

# The shared contract owns the list of legal modes; this file owns their
# order and their meaning.  If the two ever disagree the failure is silent and
# nasty — a mode nobody can resolve, or an order that ranks it wrongly — so it
# is caught here, at import, rather than by whoever runs next.
_missing = tuple(m for m in COMPLETION_MODES if m not in POLICIES)
_extra = tuple(m for m in MODE_ORDER if m not in COMPLETION_MODES)
if _missing or _extra:  # pragma: no cover - a programming error, not input
    raise ProfileError(
        "completion.POLICIES",
        "policies and contracts.COMPLETION_MODES disagree: "
        "missing {}, unknown {}".format(_missing or "-", _extra or "-"),
    )
del _missing, _extra


# ── precedence, as data ────────────────────────────────────────────────────

#: §6, highest authority first.  These are the six places a *mode* may come
#: from.  The three levels above them in the global precedence of §1.6 — hard
#: system policy, owner policy, project restrictions — are absent on purpose:
#: they gate permissions and effects, and a mode is neither, so they have
#: nothing to say here.  Keeping them out means nobody can later mistake this
#: tuple for the permission ladder and "resolve" an authority question with it.
MODE_PRECEDENCE: Tuple[str, ...] = (
    "activity",
    "task",
    "run",
    "agent_default",
    "project_default",
    "global_default",
)


def policy_for(mode: str) -> CompletionPolicy:
    """The versioned policy behind a mode name.

    Rejects anything else by name.  Callers pass user input and frontmatter
    through here, and a silent fallback to `greedy` would answer a typo by
    spending an afternoon of GPU on it.
    """
    policy = POLICIES.get(str(mode or "").strip())
    if policy is None:
        raise ProfileError(
            "completion_mode",
            "unknown completion mode; known modes are " + ", ".join(MODE_ORDER),
            got=mode,
        )
    return policy


def _explain(source: str, mode: str, silent: List[str], outranked: Tuple[str, ...]) -> str:
    """Say which level won, which ones said nothing, and which ones lost."""
    parts = ["{} set '{}'".format(source, mode)]
    if silent:
        parts.append("no value at " + ", ".join(silent))
    if outranked:
        parts.append("outranks " + ", ".join(outranked))
    return "; ".join(parts)


def resolve_mode(
    *,
    activity: str = "",
    task: str = "",
    run: str = "",
    agent_default: str = "",
    project_default: str = "",
    global_default: str = "greedy",
) -> CompletionChoice:
    """Walk §6's precedence and return the first level that said something.

    The walk is the whole point.  The returned `CompletionChoice` carries the
    winning `source` and a `reason` that names the levels which stayed silent
    and the ones that were outranked, so a run receipt can answer "why greedy?"
    six months later without re-deriving anything.

    An empty string means "this level has no opinion", which is why the
    parameters are strings and not `Optional`: a level that offers `None` and a
    level that offers `""` are the same thing, and having one spelling avoids a
    class of bug where a missing key resolves differently from a blank field.
    """
    offered: Dict[str, str] = {
        "activity": activity,
        "task": task,
        "run": run,
        "agent_default": agent_default,
        "project_default": project_default,
        "global_default": global_default,
    }
    silent: List[str] = []
    for index, source in enumerate(MODE_PRECEDENCE):
        raw = str(offered.get(source) or "").strip()
        if not raw:
            silent.append(source)
            continue
        if raw not in POLICIES:
            raise ProfileError(
                "completion_mode." + source,
                "unknown completion mode; known modes are " + ", ".join(MODE_ORDER),
                got=raw,
            )
        policy = POLICIES[raw]
        choice = CompletionChoice(
            mode=raw,
            policy_version=policy.policy_version,
            source=source,
            reason=_explain(source, raw, silent, MODE_PRECEDENCE[index + 1:]),
        )
        logger.debug("completion mode %r resolved from %r", raw, source)
        return choice
    raise ProfileError(
        "completion_mode",
        "no level offered a completion mode; at least global_default must be "
        "set, levels are " + ", ".join(MODE_PRECEDENCE),
    )


# ── reading a mode out of a sentence ───────────────────────────────────────

#: Words that turn a cue into its opposite.  "no solo cambies eso" is not a
#: request for `literal`; without this guard it would read as one.
_NEGATIONS = frozenset({
    "no", "not", "never", "nunca", "sin", "dont", "doesnt", "didnt", "isnt",
})

#: Cue -> mode, in Spanish and English, matched on whole words only.  The list
#: is short on purpose: every entry is a phrase whose meaning does not depend
#: on the rest of the sentence.  "mejóralo" is not here, because it could mean
#: any of the four modes and guessing costs somebody's evening.
#:
#: "hazlo completo" / "make it complete" are read as `professional`, not
#: `greedy`.  §6 allows either; when a phrase admits two readings we take the
#: narrower one, because raising the mode spends budget nobody asked for while
#: lowering it costs one sentence to correct.
_CUES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("literal", (
        "solo cambia", "solo cambies", "solo cambiar", "solo modifica",
        "solo toca", "cambia solo", "cambia unicamente", "unicamente cambia",
        "nada mas", "cambio minimo", "minimo cambio",
        "only change", "only fix", "only touch", "change only", "just change",
        "just fix", "nothing else", "minimal change", "smallest change",
    )),
    ("professional", (
        "hazlo completo", "hazlo bien", "trabajo completo", "entrega completa",
        "acabado profesional", "make it complete", "do it properly",
        "do it right", "production ready", "finish the job",
    )),
    ("greedy", (
        "de paso", "ya que estas", "aprovecha y", "y de camino",
        "while you are at it", "while youre at it",
    )),
    ("maximalist", (
        "dame opciones", "dame muchas opciones", "dame varias opciones",
        "muchas variantes", "todas las alternativas", "explora todo",
        "explora todas las alternativas", "give me options",
        "give me many options", "as many options", "as many variants",
        "explore every", "explore all",
    )),
)


def _normalize(text: str) -> str:
    """Fold a sentence into ` word word ` form: lowercase, accents stripped,
    apostrophes removed, everything else a single space, padded at both ends.

    The padding is what makes a plain substring search safe here — ` de paso `
    cannot match inside "desde pasos" — and stripping accents is what lets one
    cue serve "solo", "sólo" and "SÓLO" without three entries in the table.
    """
    folded = unicodedata.normalize("NFKD", str(text or "")).lower()
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    folded = folded.replace("’", "").replace("'", "")
    folded = re.sub(r"[^a-z0-9]+", " ", folded).strip()
    return " " + folded + " " if folded else ""


def _cue_present(padded: str, cue: str) -> bool:
    """True when the cue appears and is not negated by the word before it."""
    needle = " " + cue + " "
    at = padded.find(needle)
    while at != -1:
        preceding = padded[: at + 1].split()
        if not preceding or preceding[-1] not in _NEGATIONS:
            return True
        at = padded.find(needle, at + 1)
    return False


def from_instruction(text: str) -> str:
    """The completion mode an explicit instruction asks for, or `""`.

    Deterministic, offline, and conservative by construction: if the sentence
    carries cues for two different modes — "solo cambia esto pero dame
    opciones" — the answer is `""` and the caller falls back to the precedence
    in `resolve_mode`, which at least knows where its answer came from.  A
    guess here would look like a decision in the audit trail.
    """
    padded = _normalize(text)
    if not padded:
        return ""
    hits = [mode for mode, cues in _CUES
            if any(_cue_present(padded, cue) for cue in cues)]
    if len(hits) != 1:
        if hits:
            logger.debug("ambiguous completion cues %s; deferring to precedence", hits)
        return ""
    return hits[0]


# ── comparing and changing modes ───────────────────────────────────────────

def narrows(a: str, b: str) -> bool:
    """Is `b` narrower than `a`?

    Narrower means less initiative: `literal` < `professional` < `greedy` <
    `maximalist`.  Equal modes narrow nothing, so this is strict.
    """
    policy_for(a)
    policy_for(b)
    return MODE_ORDER.index(b.strip()) < MODE_ORDER.index(a.strip())


def _spent(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProfileError("transition.spent_budget", "expected a number", got=value)
    spent = float(value)
    if spent != spent or spent < 0.0:  # NaN or negative
        raise ProfileError("transition.spent_budget", "must be zero or more", got=value)
    return spent


def transition(current: str, target: str, *, spent_budget: float) -> Dict[str, Any]:
    """What changes when a running mission changes mode mid-flight (§6).

    The four rules, in the order the plan states them:

    * `greedy -> literal` starts no new bonus work, but the effects already
      begun are finished. Abandoning a half-applied change leaves the system in
      a state neither mode wanted, so `finish_started_effects` is True in every
      direction, not only when narrowing.
    * `literal -> greedy` generates a frontier **from the current state**; the
      core that is already proved is not redone (`audit["repeat_core"]`).
    * Any change keeps the budget already consumed. A mode change is not a
      refund, and it is not a licence to spend the same tokens twice.
    * No change touches tools or permissions. This function cannot express one:
      see the module docstring, and `audit` says so explicitly so a reader of
      the run receipt does not have to take it on faith.
    """
    now = policy_for(current)
    then = policy_for(target)
    spent = _spent(spent_budget)

    narrowing = narrows(current, target)
    widening = narrows(target, current)
    direction = "narrower" if narrowing else "wider" if widening else "unchanged"

    # The target's own policy decides whether new bonus work may start, which
    # is the general form of "greedy -> literal starts no new bonus": literal
    # has no bonus share, greedy has one, and narrowing maximalist -> greedy
    # correctly keeps greedy's.
    start_new_bonus = then.bonus_budget_share > 0.0
    regenerate_frontier = widening and then.explore_frontier

    if narrowing:
        reason = (
            "narrowed to {}: no new bonus work starts; effects already begun "
            "are finished so nothing is left half-applied".format(target)
        )
    elif widening:
        reason = (
            "widened to {}: a frontier is generated from the current state, "
            "the proved core is not redone".format(target)
        )
    else:
        reason = "mode unchanged: nothing to start, nothing to stop"

    result: Dict[str, Any] = {
        "from": now.mode,
        "to": then.mode,
        # The budget already burnt, carried across untouched. A number rather
        # than a flag because the next stop decision needs the figure, and
        # "keep" with nothing to keep is not a useful audit line.
        "keep_spent": spent,
        "start_new_bonus": start_new_bonus,
        "finish_started_effects": True,
        "regenerate_frontier": regenerate_frontier,
        "audit": {
            "direction": direction,
            "from_policy_version": now.policy_version,
            "to_policy_version": then.policy_version,
            "spent_budget": spent,
            "repeat_core": False,
            "tools_changed": False,
            "permissions_changed": False,
            "reason": reason,
        },
    }
    logger.info("completion mode %s -> %s (%s), %.4g budget kept",
                now.mode, then.mode, direction, spent)
    return result
