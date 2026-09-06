"""
agent_profiles/selection.py — which agent, and the record of why not the others.

Automatic selection is the part of §14 that quietly goes wrong, and it goes
wrong in four specific ways this module is built to make impossible.

**It picks the agent with the nicest name.** A ranker that reads a slug, a
display name or a prose description ends up preferring `super-builder` over
`builder` for reasons nobody wrote down. Nothing here reads `slug`, `name` or
`description` for scoring; the only text that counts is the vocabulary a
definition DECLARED — `capabilities`, `specialties`, `tags`, `preferred_tasks`,
`avoid_tasks` — and the slug is used for one thing only: breaking a numeric tie
alphabetically, which is said out loud in the trace.

**It believes what an agent says about itself.** `capabilities` and
`specialties` are for FILTERING AND MATCHING. They are never evidence of
success, and a definition cannot raise its own score by claiming to be good.
The single success term in the ranking comes from :func:`record_outcome`, which
is fed by observed results — a run that finished, a verification that passed or
failed — and by nothing else (§14).

**It collapses reliability into one number.** An agent that is excellent at
refactoring and hopeless with images does not have "a success rate". Outcomes
are stored per `(slug, task_intent)` and read back per task; ask for an agent's
reliability without naming a task and you get the per-task breakdown and
`success_rate: None`, because the single number does not exist.

**It invents health.** The Immune System that §1.5 assigns health and
quarantine to does not exist yet. So availability is taken from what is
observable — is the runner registered, is the model listed, did the caller pass
a health map — and what is not observable is reported as degraded in the trace
rather than defaulted to `healthy` (§1.3).

Two structural choices follow from §14 and are worth stating:

*Hard filters run before ranking, and every rejection carries a reason.*
`SelectionTrace.alternatives_rejected` is a list of `(slug, reason)` pairs
because a candidate rejected for a reason nobody recorded is a candidate the
selector proposes again next turn, and the human reading the trace cannot tell
"wrong capability" from "quarantined".

*A requested agent wins, and a requested agent that fails a hard filter is not
quietly swapped.* When the caller names an agent, the answer is that agent or
an explanation — never a different agent under the same request. Quarantine and
health are the one pair of filters a human request overrides, because §1.8 says
a quarantined asset is out of AUTOMATIC selection; the override then leaves a
caveat in the trace.

Nothing here raises on the selection path. `select` returns a `SelectionTrace`
with an empty `chosen` and a reason when nothing fits, the outcome store is
best-effort on both read and write, and a corrupt store degrades to "no
observed outcomes" rather than taking a dispatch down with it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from src.agent_profiles import catalog
from src.agent_profiles.builtin import effective_tools
from src.agent_profiles.contracts import CAPABILITIES, SelectionTrace

logger = logging.getLogger(__name__)

__all__ = [
    "TaskSpec",
    "Candidate",
    "WEIGHTS",
    "OUTCOMES_FILENAME",
    "OUTCOMES_SCHEMA_VERSION",
    "DATA_DIR",
    "outcomes_path",
    "hard_filters",
    "rank",
    "select",
    "record_outcome",
    "reliability",
]

try:  # pragma: no cover - constants always import in the app
    from src.constants import DATA_DIR as _DEFAULT_DATA_DIR
except Exception:  # noqa: BLE001 - standalone use (tests, tooling)
    _DEFAULT_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")

#: Module-level so a test can point the store somewhere disposable, exactly as
#: `agent_defs.DATA_DIR` does it.
DATA_DIR = _DEFAULT_DATA_DIR

#: The observed-outcome store. This belongs in `src/constants.py` with every
#: other persisted filename — that file is the single source of truth for what
#: lives under DATA_DIR — and is declared here only because this change may not
#: touch it. Moving it up is a one-line change and a note in the review.
OUTCOMES_FILENAME = "agent_selection_outcomes.json"
OUTCOMES_SCHEMA_VERSION = 1

#: Ceilings on the store, so a machine that runs for a year does not grow a
#: selection file it has to parse on every dispatch.
_MAX_AGENTS, _MAX_INTENTS, _RECENT_KEEP = 500, 200, 10
_MAX_STORE_BYTES = 2_000_000

#: Field limits `contracts.SelectionTrace` enforces. Clamped here rather than
#: discovered there: a trace that raises because a reason ran long would turn
#: an explanation into a failed dispatch.
_REASON_MAX, _REJECTION_MAX, _SLUG_MAX = 400, 300, 80
#: How many losing candidates reach the trace. Enough to see the shape of the
#: decision, not so many that a hundred-agent catalogue writes a wall.
_MAX_REJECTIONS = 20


# ── what is being asked for ────────────────────────────────────────────────

@dataclass(frozen=True)
class TaskSpec:
    """What a task needs, in terms a definition can be measured against.

    Every field is something the CALLER states. None of it is read out of an
    agent, and none of it is inferred from a model: `intent` is the short,
    stable name of the kind of work ("refactor", "image_generation"), which is
    also the key reliability is recorded under, and `description` is free text
    that only ever contributes matched words.
    """

    intent: str = ""
    description: str = ""
    required_capabilities: Tuple[str, ...] = ()
    required_tools: Tuple[str, ...] = ()
    #: The slot this agent fills: coordinator, worker or reviewer.
    mode: str = ""
    specialties: Tuple[str, ...] = ()
    output_contract: str = ""
    #: A human fixing the agent. It wins, and it is never silently replaced.
    requested_agent: str = ""


@dataclass(frozen=True)
class Candidate:
    """One definition's standing: its total, the terms behind it, and — for a
    candidate that never reached the ranking — why it was refused.

    `parts` carries the RAW term values, not the weighted ones, so a reader can
    see both what was measured and (against :data:`WEIGHTS`) what it was worth.
    A `rejected` candidate has a score of 0 and empty parts: it was never
    scored, and a zero that looks like a measurement would be a lie about how
    close it came.
    """

    slug: str
    score: float
    parts: Mapping[str, float] = field(default_factory=lambda: MappingProxyType({}))
    rejected: str = ""


# ── the ranking terms (§14) ────────────────────────────────────────────────
#
# The list of terms is §14's, in §14's order, and the weights are DATA so that
# "why did it pick that one" is answered by reading a dict rather than by
# re-deriving a scoring function. Every term is measured in [0, 1]; the sign
# lives in the weight.

WEIGHTS: Mapping[str, float] = MappingProxyType({
    # Declared vocabulary against the words of the task.
    "task_fit": 0.30,
    "specialty_fit": 0.20,
    # The ONLY success term, and it comes from observed outcomes.
    "verified_success_rate": 0.25,
    "output_contract_fit": 0.05,
    "availability": 0.10,
    # Computed and reported, deliberately weightless: §14 asks for cost/latency
    # fit and `TaskSpec` carries no deadline and no budget, so there is nothing
    # to fit AGAINST. Preferring the cheaper lane anyway would be this module
    # inventing a preference the caller never stated. It is reported at zero so
    # the missing input is visible in the trace instead of forgotten.
    "cost_latency_fit": 0.0,
    "recent_failures": -0.20,
    "avoided_task": -0.30,
    "degraded_dependency": -0.10,
})

#: Two declared hits is a full mark. A definition is not rewarded for listing
#: every word in the dictionary under `tags`, which is what a
#: "fraction of my vocabulary matched" measure would pay for.
_FULL_MARK_HITS = 2.0

#: Words that match everything and therefore mean nothing.
_STOPWORDS = frozenset({
    "the", "and", "for", "with", "that", "this", "from", "into", "onto", "over",
    "una", "unos", "unas", "los", "las", "del", "por", "para", "con", "que",
    "como", "sobre", "hacer", "haz", "add", "make", "new", "please", "por_favor",
})
_WORD_RE = re.compile(r"[a-z0-9_]+")


# ── small readers, none of which raise ─────────────────────────────────────

def _clip(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _words(*values: Any) -> Tuple[str, ...]:
    """Free text and word lists to a deduplicated bag of matchable words.

    `_` is a word character here because the vocabulary fields of a definition
    are normalised to `snake_case` by `agent_defs._words`, so `threat_model`
    must survive as one word AND as its two halves — a task that says "threat
    model" and a definition that says `threat_model` are talking about the
    same thing.
    """
    out: List[str] = []
    for value in values:
        items = value if isinstance(value, (list, tuple)) else [value]
        for item in items:
            raw = str(item or "").lower()
            for word in _WORD_RE.findall(raw.replace("_", " ")) + _WORD_RE.findall(raw):
                if len(word) < 3 or word in _STOPWORDS or word in out:
                    continue
                out.append(word)
    return tuple(out)


def _norm(value: Any) -> str:
    """A slug or an intent, in one spelling: lowercase, `_`-joined."""
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


def _intent_key(value: Any) -> str:
    """The key an outcome is filed under. Blank becomes `_any`, which is a real
    key and not a wildcard: work nobody named is still a kind of work, and
    lumping it in with everything else would be the global score §14 refuses."""
    return _norm(value)[:60] or "_any"


def _as_defs(defs: Any) -> Sequence[Any]:
    """A catalogue in whatever shape the caller keeps it, as a list of definitions.

    A `{slug: definition}` mapping is the shape the resolver and the loader
    both hold, and iterating one yields its KEYS — strings, with no `slug`
    attribute, which every filter here would then discard in silence. Rather
    than a selector that mysteriously returns nothing, the shape is normalised
    once, at the door.
    """
    if defs is None:
        return ()
    if isinstance(defs, Mapping):
        return list(defs.values())
    if hasattr(defs, "by_slug"):                      # an agent_defs.LoadResult
        return list(defs.by_slug().values())
    return list(defs)


def _index(defs: Any) -> Dict[str, Any]:
    return {str(getattr(d, "slug", "") or ""): d
            for d in _as_defs(defs) if getattr(d, "slug", "")}


def _declared(d: Any, name: str) -> Tuple[str, ...]:
    value = getattr(d, name, ()) or ()
    return tuple(str(v) for v in value)


def _reaches(d: Any, tool: str) -> bool:
    """Whether this definition may use a tool.

    An empty `tools` means the definition stated no allowlist and the session
    floor decides, which is `agent_defs`' reading and must stay this module's
    too: treating "said nothing" as "allows nothing" would filter out every
    definition written before allowlists were common.
    """
    name = str(tool or "").strip()
    if not name:
        return False
    if not _declared(d, "tools"):
        return name not in _declared(d, "deny")
    return name in effective_tools(d)


def _find(index: Mapping[str, Any], requested: str) -> Optional[Any]:
    """The definition a caller asked for, by whatever spelling they used.

    Three tries, in order of how sure they are: the slug exactly; the slug
    after this module's normalisation, lowercase and `_`-joined (so
    `Greedy Builder` and `greedy_builder` reach `greedy-builder`); and finally
    the plan's own name for a shipped built-in, because §8 calls one of them
    `auditor` and a person who read the plan will type that. A name that
    reaches none of the three is reported as unknown, never guessed at.
    """
    direct = index.get(requested)
    if direct is not None:
        return direct
    wanted = _norm(requested)
    for slug, definition in index.items():
        if _norm(slug) == wanted:
            return definition
    try:
        from src.agent_profiles.builtin import resolve_slug
        shipped = resolve_slug(requested)
    except Exception as exc:  # noqa: BLE001 - the alias table is a convenience
        logger.debug("selection: built-in alias table unavailable: %s", exc)
        return None
    return index.get(shipped) if shipped else None


def _availability_of(value: Any) -> Optional[float]:
    """A health signal to [0, 1], or None when it says nothing usable.

    None is not 0.5 and not 1.0. A caller that hands us a word we do not know
    has told us nothing, and the trace says so rather than picking a number
    that would read as a measurement.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return max(0.0, min(1.0, float(value)))
    word = str(value or "").strip().lower()
    return {"healthy": 1.0, "ready": 1.0, "available": 1.0, "up": 1.0,
            "degraded": 0.5, "busy": 0.5, "warming": 0.5,
            "unhealthy": 0.0, "down": 0.0, "offline": 0.0, "quarantined": 0.0}.get(word)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ── hard filters (§14) ─────────────────────────────────────────────────────

def hard_filters(defs: Any, task: TaskSpec, *,
                 available_models: Sequence[str] = (),
                 available_runners: Sequence[str] = (),
                 unhealthy: Sequence[str] = (),
                 quarantined: Sequence[str] = ()) -> Tuple[List[Any], List[Candidate]]:
    """Split a catalogue into what may be considered and what may not, WITH REASONS.

    Filtering before ranking is not an optimisation. A quarantined agent that
    merely scores badly is an agent that gets picked the moment the good one is
    busy; a filter says no and keeps saying no. And every rejection carries the
    sentence a human needs — "does not declare capability `image`", not a slug
    in a list of losers — because an unexplained rejection is re-proposed next
    turn by whoever reads the trace.

    What is checked, in §14's order: mode slot, capabilities, tools, output
    contract, model and runner availability, health and quarantine.

    Availability is only ever a filter when the caller told us what exists.
    Empty `available_models`/`available_runners` mean "no registry was handed
    to this call", which is not the same as "nothing is available" — the Immune
    System of §1.5 does not exist yet — so nothing is rejected for it and
    :func:`select` reports the gap as degraded (§1.3).

    Returns `(survivors, rejected)`; `rejected` is a list of :class:`Candidate`
    carrying only a slug and a reason.
    """
    models = {str(m) for m in (available_models or ()) if str(m or "").strip()}
    runners = {str(r) for r in (available_runners or ()) if str(r or "").strip()}
    sick = {_norm(s) for s in (unhealthy or ())}
    held = {_norm(s) for s in (quarantined or ())}
    wanted_caps = tuple(_norm(c) for c in (task.required_capabilities or ()))

    survivors: List[Any] = []
    rejected: List[Candidate] = []

    for d in _as_defs(defs):
        slug = str(getattr(d, "slug", "") or "")
        if not slug:
            continue
        reason = ""

        if task.mode and str(getattr(d, "mode", "")) != task.mode:
            reason = (f"is a `{getattr(d, 'mode', '?')}` and the task needs the "
                      f"`{task.mode}` slot")
        if not reason:
            declared = set(_declared(d, "capabilities"))
            missing = [c for c in wanted_caps if c not in declared]
            if missing:
                unknown = [c for c in missing if c not in CAPABILITIES]
                reason = ("does not declare " + ", ".join("`{}`".format(c) for c in missing)
                          + (" (and no agent can: not a known capability)" if unknown else ""))
        if not reason:
            blocked = [t for t in (task.required_tools or ()) if not _reaches(d, t)]
            if blocked:
                reason = "cannot use " + ", ".join("`{}`".format(t) for t in blocked)
        if not reason and task.output_contract:
            contract = str(getattr(d, "output_contract", "") or "")
            if contract and contract != task.output_contract:
                reason = (f"hands back `{contract}` and the task needs "
                          f"`{task.output_contract}`")
        if not reason and models:
            model = str(getattr(d, "model", "") or "")
            if model and model not in models:
                reason = f"needs model `{model}`, which is not among the ones loaded"
        if not reason and runners:
            runner = str(getattr(d, "runner", "") or "")
            if runner and runner not in runners:
                reason = f"needs runner `{runner}`, which this machine does not have"
        if not reason and _norm(slug) in held:
            reason = ("is quarantined, so automatic selection excludes it; a person can "
                      "still ask for it by name")
        if not reason and _norm(slug) in sick:
            reason = "is reported unhealthy, so automatic selection excludes it"

        if reason:
            rejected.append(Candidate(slug=_clip(slug, _SLUG_MAX), score=0.0,
                                      rejected=_clip(reason, _REJECTION_MAX)))
        else:
            survivors.append(d)

    return survivors, rejected


# ── ranking (§14) ──────────────────────────────────────────────────────────

def _task_fit(d: Any, task_words: Tuple[str, ...]) -> float:
    """How much of the task's language this definition DECLARED it works on.

    Reads `preferred_tasks`, `tags` and `specialties` and nothing else. Not the
    slug, not the name, not the description: a definition that calls itself
    "the ultimate refactoring expert" in prose has claimed nothing checkable,
    and a ranker that reads prose is a ranker that can be talked into things.
    """
    vocabulary = set(_words(_declared(d, "preferred_tasks"), _declared(d, "tags"),
                            _declared(d, "specialties")))
    hits = sum(1 for word in task_words if word in vocabulary)
    return min(1.0, hits / _FULL_MARK_HITS)


def _specialty_fit(d: Any, task: TaskSpec) -> float:
    wanted = [_norm(s) for s in (task.specialties or ()) if _norm(s)]
    if not wanted:
        return 0.0
    declared = set(_declared(d, "specialties"))
    return sum(1 for s in wanted if s in declared) / float(len(wanted))


def _cost_latency_fit(d: Any) -> float:
    """Cheaper and quicker reads as 1.0. Weighted at zero — see :data:`WEIGHTS`."""
    profile = catalog.get("budget", str(getattr(d, "budget_profile", "") or ""))
    seconds = float(getattr(profile, "max_seconds", 0) or 0)
    if seconds <= 0:
        return 0.0
    return max(0.0, 1.0 - min(1.0, seconds / 43_200.0))


def rank(defs: Any, task: TaskSpec, *,
         history: Optional[Mapping[str, Any]] = None,
         availability: Optional[Mapping[str, Any]] = None) -> List[Candidate]:
    """Score the definitions that survived the filters, best first.

    The terms and their weights are :data:`WEIGHTS`, and every one of them is
    computed from something that was either DECLARED for matching or OBSERVED
    as an outcome. There is no term a definition can raise by describing itself
    more enthusiastically.

    `verified_success_rate` is read for THIS task's intent only, and it is zero
    when the task states no intent — not because an agent with no intent is
    unreliable, but because per-task reliability cannot be read without a task,
    and substituting a lifetime average would be the single global score §14
    forbids. It is also damped by how many runs it rests on: two successes are
    not the same evidence as twenty.

    Ordering is `(-score, slug)`. The alphabetical tail is the whole tie-break
    policy: deterministic, reproducible across processes, and stated in the
    trace whenever it actually decided something. It is the ONE place a slug
    influences the outcome, and it influences it only after every measured term
    came out equal.

    `history` (a `{slug: {intent: record}}` mapping, the same shape the store
    keeps) replaces the on-disk outcomes for this call. `availability` maps a
    slug to a health signal; a slug missing from a map that was supplied counts
    as a degraded dependency, while no map at all leaves everyone equal and is
    reported by :func:`select` as unobserved rather than healthy.
    """
    store = history if history is not None else _load_outcomes()
    task_words = _words(task.intent, task.description, task.specialties)
    intent = _intent_key(task.intent) if str(task.intent or "").strip() else ""
    have_health = availability is not None

    out: List[Candidate] = []
    for d in _as_defs(defs):
        slug = str(getattr(d, "slug", "") or "")
        if not slug:
            continue
        record = _record_for(store, slug, intent) if intent else {}
        runs = int(record.get("runs") or 0)
        ok = int(record.get("ok") or 0)
        recent = [bool(v) for v in (record.get("recent") or ())][-5:]

        health = _availability_of((availability or {}).get(slug)) if have_health else None
        contract = str(getattr(d, "output_contract", "") or "")
        avoided = set(_declared(d, "avoid_tasks"))

        parts: Dict[str, float] = {
            "task_fit": _task_fit(d, task_words),
            "specialty_fit": _specialty_fit(d, task),
            "verified_success_rate": ((ok / float(runs)) * min(1.0, runs / 3.0)) if runs else 0.0,
            "output_contract_fit": (0.0 if not task.output_contract
                                    else 1.0 if contract == task.output_contract
                                    else 0.5),
            "availability": health if health is not None else 0.0,
            "cost_latency_fit": _cost_latency_fit(d),
            "recent_failures": (sum(1 for v in recent if not v) / 5.0) if recent else 0.0,
            "avoided_task": 1.0 if any(w in avoided for w in _words(task.intent,
                                                                   task.description)) else 0.0,
            "degraded_dependency": 1.0 if (have_health and health is None) else 0.0,
        }
        score = round(sum(WEIGHTS[name] * value for name, value in parts.items()), 6)
        out.append(Candidate(slug=_clip(slug, _SLUG_MAX), score=score,
                             parts=MappingProxyType({k: round(v, 6) for k, v in parts.items()})))

    out.sort(key=lambda c: (-c.score, c.slug))
    return out


# ── the decision, and the record of it ─────────────────────────────────────

def _trace(requested: str, chosen: str, reason: str,
           rejected: Sequence[Candidate] = (),
           scores: Optional[Mapping[str, float]] = None) -> SelectionTrace:
    """Build the trace with every field clipped to what `contracts` accepts.

    Clipping here rather than letting the contract refuse is deliberate: a
    reason that ran four characters long must not turn an explanation into a
    failed dispatch.
    """
    return SelectionTrace(
        requested=_clip(requested, _SLUG_MAX),
        chosen=_clip(chosen, _SLUG_MAX),
        reason=_clip(reason, _REASON_MAX),
        alternatives_rejected=tuple(
            (_clip(c.slug, _SLUG_MAX), _clip(c.rejected, _REJECTION_MAX))
            for c in list(rejected)[:_MAX_REJECTIONS] if c.slug),
        scores=tuple(sorted((str(k), float(v)) for k, v in (scores or {}).items())),
    )


def _degraded_notes(defs: Any, available_models: Sequence[str],
                    available_runners: Sequence[str],
                    availability: Optional[Mapping[str, Any]]) -> List[str]:
    """What this selection could not observe, named (§1.3).

    The rule the plan states and this implements: when an integration is
    missing, degrade with an explicit reason. Never invent `health=healthy`.
    """
    rows = _as_defs(defs)
    notes: List[str] = []
    if availability is None:
        notes.append("no health signal (Immune System absent): availability unknown, not healthy")
    if not available_models and any(getattr(d, "model", "") for d in rows):
        notes.append("no model registry supplied: model availability unchecked")
    if not available_runners and any(getattr(d, "runner", "") for d in rows):
        notes.append("no runner registry supplied: runner availability unchecked")
    return notes


def _why(chosen: Candidate, runner_up: Optional[Candidate]) -> str:
    """The sentence that says what carried the decision, and what nearly did."""
    weighted = sorted(((WEIGHTS[name] * value, name) for name, value in chosen.parts.items()
                       if WEIGHTS.get(name, 0.0) > 0 and value > 0), reverse=True)
    carried = ", ".join(name for _, name in weighted[:3]) or "no positive term"
    text = f"scored {chosen.score:g} on {carried}"
    if runner_up is None:
        return text + "; no other definition passed the hard filters"
    if runner_up.score == chosen.score:
        return (text + f"; tied with `{runner_up.slug}` and chosen alphabetically, "
                       f"because nothing measured told them apart")
    return text + f"; next was `{runner_up.slug}` at {runner_up.score:g}"


def select(defs: Any, task: TaskSpec, *,
           available_models: Sequence[str] = (),
           available_runners: Sequence[str] = (),
           unhealthy: Sequence[str] = (),
           quarantined: Sequence[str] = (),
           history: Optional[Mapping[str, Any]] = None,
           availability: Optional[Mapping[str, Any]] = None) -> SelectionTrace:
    """Choose an agent, or explain why none was chosen. Never raises.

    Three paths, and the difference between them is the whole point of §14:

    * **the caller named an agent.** It is used, and if it cannot be used the
      answer says so — an empty `chosen` and a reason. A requested agent is
      never silently replaced by a better-scoring one, because a caller who
      asked for `auditor` and got `builder` was not helped, they were
      overruled without being told.
    * **the caller named a quarantined or unhealthy agent.** §1.8 puts a
      quarantined asset outside AUTOMATIC selection, not outside use. So the
      request is honoured and the trace carries `caveat:` in its reason. (It
      goes in `reason` because `SelectionTrace` has no caveat field; that field
      belongs in `contracts.py` and is worth adding there.)
    * **nobody named anything.** Hard filters first, ranking second, and the
      losers reach the trace with the reason they lost — a rejection reason for
      the ones that were filtered, a score comparison for the ones that were
      merely beaten.
    """
    index = _index(defs)
    requested = str(getattr(task, "requested_agent", "") or "").strip()
    notes = _degraded_notes(defs, available_models, available_runners, availability)

    if requested:
        definition = _find(index, requested)
        if definition is None:
            known = ", ".join(sorted(index)[:8])
            return _trace(requested, "", f"requested `{requested}` is not a definition this "
                                         f"build knows. Known: {known}",
                          [Candidate(slug=requested, score=0.0, rejected="unknown definition")])
        # Health and quarantine are the filters a human may overrule; every
        # other filter still applies, because none of them is about caution.
        survivors, refused = hard_filters([definition], task,
                                          available_models=available_models,
                                          available_runners=available_runners)
        if not survivors:
            why = refused[0].rejected if refused else "did not pass the hard filters"
            return _trace(requested, "", f"requested `{requested}` was NOT used and nothing was "
                                         f"substituted for it: it {why}", refused)
        # Checked against the slug the request RESOLVED to, not the words the
        # caller typed: `auditor` and `reviewer` are one agent, and a caveat
        # that only fires on one spelling is a caveat that can be typed around.
        chosen_slug = str(getattr(definition, "slug", "") or requested)
        caveats = []
        if _norm(chosen_slug) in {_norm(s) for s in (quarantined or ())}:
            caveats.append(f"caveat: `{chosen_slug}` is quarantined and would not have been "
                           f"selected automatically")
        if _norm(chosen_slug) in {_norm(s) for s in (unhealthy or ())}:
            caveats.append(f"caveat: `{chosen_slug}` is reported unhealthy")
        reason = "; ".join(["requested by the caller, which outranks the ranking"]
                           + caveats + notes)
        return _trace(requested, definition.slug, reason)

    survivors, refused = hard_filters(defs, task, available_models=available_models,
                                      available_runners=available_runners,
                                      unhealthy=unhealthy, quarantined=quarantined)
    if not survivors:
        return _trace("", "", "no definition passed the hard filters "
                              f"({len(refused)} rejected); see alternatives_rejected",
                      refused)

    ranked = rank(survivors, task, history=history, availability=availability)
    if not ranked:  # pragma: no cover - survivors without slugs are filtered above
        return _trace("", "", "nothing could be scored", refused)

    best = ranked[0]
    runner_up = ranked[1] if len(ranked) > 1 else None
    beaten = [Candidate(slug=c.slug, score=c.score,
                        rejected=f"scored {c.score:g} against {best.score:g} for `{best.slug}`")
              for c in ranked[1:]]
    if not str(task.intent or "").strip():
        notes.append("no task intent stated: per-task reliability could not be read, so it "
                     "counted for nothing")
    reason = "; ".join([_why(best, runner_up)] + notes)
    scores = dict(best.parts)
    scores["total"] = best.score
    return _trace("", best.slug, reason, refused + beaten, scores)


# ── observed outcomes: the only evidence of success in this module ─────────
#
# `{"schema_version": 1, "agents": {"<slug>": {"<intent>": {...}}}}`
#
# Nested by (slug, intent) because that is the shape §14 requires and because a
# flat `"slug|intent"` key is a shape somebody eventually splits on the wrong
# character. `history=` in `rank` takes the inner `agents` mapping, so a test
# and the store speak one language.

def outcomes_path() -> str:
    """`DATA_DIR/agent_selection_outcomes.json`, read through a function so a
    test can repoint `selection.DATA_DIR` between calls."""
    return os.path.join(DATA_DIR, OUTCOMES_FILENAME)


def _load_outcomes() -> Dict[str, Any]:
    """The `agents` mapping, or an empty one. Never raises.

    A missing file is the ordinary case on a fresh machine, and a corrupt one
    is a file somebody edited or a half-written crash — neither is a reason to
    fail a dispatch. Both degrade to "no observed outcomes", which is the same
    thing the selector says on day one and behaves correctly.
    """
    path = outcomes_path()
    try:
        if os.path.getsize(path) > _MAX_STORE_BYTES:
            logger.warning("agent selection outcomes at %s are larger than %d bytes and were "
                           "not read", path, _MAX_STORE_BYTES)
            return {}
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return {}
    except Exception as exc:  # noqa: BLE001 - a bad store is data, not a crash
        logger.warning("agent selection outcomes at %s could not be read (%s); "
                       "treating as no observed outcomes", path, exc)
        return {}
    agents = raw.get("agents") if isinstance(raw, dict) else None
    return agents if isinstance(agents, dict) else {}


def _save_outcomes(agents: Mapping[str, Any]) -> None:
    """Write the store atomically. Never raises.

    Temp file plus `os.replace` so a crash mid-write leaves the previous store
    intact rather than a truncated JSON file that the next read would report as
    corrupt and discard — losing every outcome ever recorded.
    """
    path = outcomes_path()
    payload = {"schema_version": OUTCOMES_SCHEMA_VERSION, "updated_at": _now(),
               "agents": dict(agents)}
    tmp = ""
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        handle, tmp = tempfile.mkstemp(prefix=".outcomes-", suffix=".json",
                                       dir=os.path.dirname(path) or ".")
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, path)
        tmp = ""
    except Exception as exc:  # noqa: BLE001 - losing a statistic is not losing a run
        logger.warning("agent selection outcomes could not be written to %s: %s", path, exc)
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass


def _record_for(agents: Mapping[str, Any], slug: str, intent: str) -> Dict[str, Any]:
    """One `(slug, intent)` record out of a store, or an empty dict."""
    per_agent = (agents or {}).get(slug)
    if not isinstance(per_agent, Mapping):
        return {}
    record = per_agent.get(_intent_key(intent))
    return dict(record) if isinstance(record, Mapping) else {}


def record_outcome(slug: str, task_intent: str, *, ok: bool, ref: str = "") -> None:
    """File one OBSERVED result under `(slug, task_intent)`. Never raises.

    This is the only door through which success enters the ranking, and it is
    deliberately narrow: something happened, it either worked or it did not,
    and `ref` points at the run, changeset or verdict that says so. Nothing an
    agent declares about itself reaches this file, because a catalogue where an
    agent can improve its own score by editing its frontmatter is a catalogue
    that ranks confidence rather than competence (§14).

    Filed PER TASK. The same agent can be the first choice for `refactor` and
    the last for `image_generation`, and one number could not say that.
    """
    key = _norm(slug)
    if not key:
        return
    intent = _intent_key(task_intent)
    agents = _load_outcomes()
    per_agent = dict(agents.get(key) or {}) if isinstance(agents.get(key), Mapping) else {}
    record = dict(per_agent.get(intent) or {}) if isinstance(per_agent.get(intent), Mapping) else {}

    recent = [bool(v) for v in (record.get("recent") or ())][-(_RECENT_KEEP - 1):]
    recent.append(bool(ok))
    record.update({
        "runs": int(record.get("runs") or 0) + 1,
        "ok": int(record.get("ok") or 0) + (1 if ok else 0),
        "recent": recent,
        "last_ok": bool(ok),
        "last_at": _now(),
    })
    if ref:
        record["last_ref"] = _clip(ref, 200)

    per_agent[intent] = record
    if len(per_agent) > _MAX_INTENTS:  # drop the least recently touched intents
        per_agent = dict(sorted(per_agent.items(),
                                key=lambda kv: str((kv[1] or {}).get("last_at") or ""),
                                reverse=True)[:_MAX_INTENTS])
    agents = dict(agents)
    agents[key] = per_agent
    if len(agents) > _MAX_AGENTS:
        agents = {k: v for k, v in list(agents.items())[-_MAX_AGENTS:]}
    _save_outcomes(agents)
    logger.debug("recorded outcome ok=%s for %r on %r", ok, key, intent)


def reliability(slug: str, task_intent: str = "") -> Dict[str, Any]:
    """What has actually been observed about this agent — per task.

    With a `task_intent`, the answer is that task's record and a
    `success_rate`. WITHOUT one, `success_rate` is `None` and the per-task
    breakdown is returned instead: an agent's reliability is not a number, and
    handing back a lifetime average would be the global score §14 rules out —
    the average of "excellent at refactoring" and "hopeless with images" is a
    figure that describes neither.

    Never raises; an unreadable store reads as no observed outcomes, and
    `runs: 0` with `success_rate: None` is the honest answer on a fresh
    machine, not a zero that would read as a measured failure.
    """
    key = _norm(slug)
    agents = _load_outcomes()
    per_agent = agents.get(key) if isinstance(agents.get(key), Mapping) else {}
    per_agent = {k: v for k, v in (per_agent or {}).items() if isinstance(v, Mapping)}

    if str(task_intent or "").strip():
        intent = _intent_key(task_intent)
        record = _record_for(agents, key, intent)
        runs = int(record.get("runs") or 0)
        ok = int(record.get("ok") or 0)
        recent = [bool(v) for v in (record.get("recent") or ())][-5:]
        return {
            "slug": key, "task_intent": intent, "scope": "task",
            "runs": runs, "ok": ok, "failed": max(0, runs - ok),
            "success_rate": (ok / float(runs)) if runs else None,
            "recent_failures": sum(1 for v in recent if not v),
            "last_ref": str(record.get("last_ref") or ""),
            "last_at": str(record.get("last_at") or ""),
            "evidence": "observed outcomes only; nothing an agent declares about itself",
        }

    runs = sum(int((r or {}).get("runs") or 0) for r in per_agent.values())
    ok = sum(int((r or {}).get("ok") or 0) for r in per_agent.values())
    return {
        "slug": key, "task_intent": "", "scope": "all_tasks",
        "runs": runs, "ok": ok, "failed": max(0, runs - ok),
        # Deliberately None. See the docstring: there is no single number.
        "success_rate": None,
        "by_task": {intent: {"runs": int((r or {}).get("runs") or 0),
                             "ok": int((r or {}).get("ok") or 0),
                             "success_rate": ((int((r or {}).get("ok") or 0)
                                               / float((r or {}).get("runs")))
                                              if int((r or {}).get("runs") or 0) else None)}
                    for intent, r in sorted(per_agent.items())},
        "evidence": "observed outcomes only; reliability is per task, so no single rate",
    }
