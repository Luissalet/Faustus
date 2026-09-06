"""agent_profiles/resolver.py — the effective configuration of one execution.

`contracts.py` says what a resolution IS. `completion.py` says how far a mission
pushes. This file is the thing that walks the ladder and produces the object,
and it is written to five rules that are the whole of its job.

**The precedence of §1.6 is walked as DATA.** `contracts.PRECEDENCE` is a tuple
of eight level names and every field is decided by walking it, strongest first.
A chain of `if`s could answer "greedy, model X, twelve rounds" and could not
answer *why*, and "why did this run behave like that?" is the question the whole
plan exists to answer. So every effective value remembers the level that put it
there and what the levels that lost had to say: :func:`explain` prints the three
columns — value, where it came from, what was discarded — field by field.

**An override never widens (§15).** A task may change the completion mode,
change the model/endpoint/runner when they are compatible, REDUCE rounds,
timeout and budget, ADD denies, REMOVE tools, and choose a verification profile
that is equal or stronger. It may not remove a deny, widen a path or an effect,
enable a tool outside the definition's allowlist, weaken a blocking
verification, cross the provider boundary, or exceed the budget ceiling. A
forbidden override is **not** an exception: the field is ignored, the rest of
the override still applies, and the reason lands in `caveats` where a run
receipt shows it. Refusing the whole call would turn one optimistic field into
a dead job; granting it would be the failure this module exists to prevent.

**A completion mode grants nothing (§3.3).** The mode is resolved by
`completion.resolve_mode` on its own six-level ladder, and the envelope is built
by a separate function that never reads it. `maximalist` on a read-only reviewer
is a reviewer that looks harder and still cannot write a byte — and that holds
because the two computations share no input, not because a comment asked.

**The definition revision is pinned before anything runs (§20).**
:func:`snapshot` writes the resolution and its trace down; :func:`rehydrate`
reads them back and, when the definition has been edited since, says so in
`caveats` instead of quietly re-resolving. A nightly automation must not start
doing something else because somebody improved an agent at four in the
afternoon.

**A missing integration degrades with a reason (§1.3).** There is no Immune
System in this build, so `degraded_integrations` carries `capability_health`
with the reason and availability is read from what the caller observed. Nothing
here ever invents `healthy`.

And one property that is not a rule but a promise: :func:`resolve` never raises
on the hot path. Every failure it can have — a broken definition, an unreadable
store, a catalogue that will not import — comes back as a minimal valid
resolution whose `caveats` say what went wrong. A dispatcher that cannot resolve
must still be able to refuse *with a reason*, and an exception three frames deep
is not one.

Stdlib plus this package and (lazily) `src.agent_defs`. Nothing here starts a
process, reaches the network, or writes a file.
"""
from __future__ import annotations

import logging
import textwrap
from collections import OrderedDict
from dataclasses import dataclass, replace
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from src.contracts.base import now_iso

from . import catalog
from .completion import MODE_PRECEDENCE, POLICIES, from_instruction, resolve_mode
from .contracts import (
    COMPLETION_MODES, PRECEDENCE,
    AgentRef, CompletionChoice, ExecutionScope, ModelRoute, PermissionEnvelope,
    ProfileError, ProfileSet, ResolvedAgentExecution, SelectionTrace,
    new_resolution_id, sha256_digest,
)

logger = logging.getLogger(__name__)

__all__ = [
    "Decision", "GLOBAL_DEFAULTS", "SNAPSHOT_SCHEMA", "SYSTEM_POLICY",
    "apply_task_override", "explain", "explain_rows", "rehydrate", "resolve",
    "snapshot", "trace_of",
]

#: The weakest level of §1.6: what a run gets when nothing else has an opinion.
#: `greedy` is the completion mode every worker already behaved as, so shipping
#: a quieter default would have changed every existing definition on day one
#: (§26); the four profile families fall back to their catalogue `default`.
GLOBAL_DEFAULTS: Dict[str, Any] = {
    "completion_mode": "greedy",
    "verification_profile": "default",
    "context_profile": "default",
    "budget_profile": "default",
    "collaboration_profile": "default",
}

#: The strongest level of §1.6. It is deliberately almost empty: the hard tool
#: floor of this codebase is ENFORCED, in `src/subagent_permissions.py` and in
#: the pre-execution guard of `src/agent_tools/subagent_tools.py`, and copying
#: it here would create a second list free to disagree with the one that runs.
#: What this level does own is the pair of absolute ceilings — no resolution may
#: pin more rounds or more seconds than the loader itself would accept — applied
#: as CAPS in :func:`_ceiling` rather than as values, so a run that states no
#: limit is not handed the maximum.
SYSTEM_POLICY: Dict[str, Any] = {}

#: Written into a run so a later reader can tell a pinned configuration from a
#: bare `to_dict()`.
SNAPSHOT_SCHEMA = "agent_resolution_snapshot/1"

#: Effects this resolver can DERIVE from a definition. They are read out of what
#: the definition already allows — never granted here, and never widened by a
#: level below the one that granted them.
_EFFECT_TOOLS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("read", ("read_file", "ls", "glob", "grep")),
    ("write", ("write_file", "edit_file", "apply_patch")),
    ("shell", ("bash", "python")),
    ("network", ("web_search", "web_fetch")),
)

#: Which override keys name a profile of which catalogue family.
_PROFILE_KEYS: Tuple[Tuple[str, str], ...] = (
    ("verification", "verification_profile"),
    ("context", "context_profile"),
    ("budget", "budget_profile"),
    ("collaboration", "collaboration_profile"),
    ("output", "output_contract"),
)

#: The four families where a lower level may only choose an EQUAL OR STRICTER
#: profile (§15). `output` is not one of them: an output contract is the SHAPE
#: of a handoff and cannot fabricate proof (§13), so changing it neither widens
#: authority nor buys budget.
_NARROWING_KINDS: Tuple[str, ...] = ("verification", "context", "budget", "collaboration")

#: Every key a task override or an activity policy may carry. Anything else is
#: reported (§22: an unrecognised field is a typo somebody needs to see, not a
#: default), never silently dropped.
OVERRIDE_KEYS: Tuple[str, ...] = (
    "agent", "completion_mode", "model", "endpoint_id", "runner",
    "max_rounds", "timeout_s", "tools", "deny", "deny_tools", "permission",
    "work_roots", "effects", "verification_profile", "context_profile",
    "budget_profile", "collaboration_profile", "output_contract",
    "restrictions", "mode", "slot", "capabilities", "instruction",
)

#: The levels ABOVE the definition. A value from one of these is policy, and
#: policy is applied restrictively — see :func:`_restrict`.
_OVERRIDE_LEVELS: Tuple[str, ...] = ("system_policy", "owner_policy", "project_restriction",
                                     "activity_policy", "task_override")
#: The levels that may STATE a base value.
_BASE_LEVELS: Tuple[str, ...] = ("agent_default", "project_default", "global_default")


# ── the trace: what won, and what lost ─────────────────────────────────────

@dataclass(frozen=True)
class Decision:
    """One effective value and the walk that produced it.

    `discarded` is the half that costs money to lose: a resolution that says
    "model qwen3-coder" without saying that the task asked for something else
    and was refused sends its reader to look for a bug in the router.
    """

    field: str
    value: Any
    level: str
    discarded: Tuple[Tuple[str, Any, str], ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {"field": self.field, "value": _plain(self.value), "level": self.level,
                "discarded": [{"level": lv, "value": _plain(val), "why": why}
                              for lv, val, why in self.discarded]}

    @classmethod
    def parse(cls, raw: Any) -> Optional["Decision"]:
        """One row back off a snapshot. A row that will not read is dropped
        rather than guessed at: a trace is evidence, and half a row of evidence
        is worse than an honest gap."""
        if not isinstance(raw, Mapping) or not str(raw.get("field") or "").strip():
            return None
        rows: List[Tuple[str, Any, str]] = []
        for item in raw.get("discarded") or ():
            if isinstance(item, Mapping):
                rows.append((str(item.get("level") or ""), item.get("value"),
                             str(item.get("why") or "")))
        return cls(field=str(raw["field"]), value=raw.get("value"),
                   level=str(raw.get("level") or ""), discarded=tuple(rows))


def _plain(value: Any) -> Any:
    """A value as it travels in a record: lists and scalars, never a tuple."""
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, (list, set, frozenset)):
        return sorted(value) if isinstance(value, (set, frozenset)) else list(value)
    return value


#: resolution_id → its trace. Bounded, because this is a convenience for the
#: page and the log and must never be the reason a long-lived process grows.
#: The DURABLE copy of a trace is the one :func:`snapshot` writes into the run;
#: this map is what makes `explain(resolve(...))` work in one breath.
_TRACES: "OrderedDict[str, Tuple[Decision, ...]]" = OrderedDict()
_TRACE_LIMIT = 256


class _Ledger:
    """Collects the decisions, the caveats and the degradations of one resolve.

    One object rather than three return values because every helper below needs
    to add to all three, and threading three accumulators through a dozen calls
    is how one of them ends up dropped on a branch nobody tested.
    """

    def __init__(self) -> None:
        self.rows: List[Decision] = []
        self.caveats: List[str] = []
        self.degraded: List[str] = []

    def decide(self, field: str, value: Any, level: str,
               discarded: Sequence[Tuple[str, Any, str]] = ()) -> Any:
        self.rows.append(Decision(field=field, value=value, level=level,
                                  discarded=tuple(discarded)))
        return value

    def caveat(self, text: str) -> None:
        line = " ".join(str(text or "").split())[:400]
        if line and line not in self.caveats:
            self.caveats.append(line)

    def degrade(self, name: str, why: str) -> None:
        entry = "{}: {}".format(name, " ".join(str(why or "").split()))[:128]
        if not any(x.split(":", 1)[0] == name for x in self.degraded):
            self.degraded.append(entry)

    def refuse(self, field: str, level: str, value: Any, why: str) -> None:
        """An override that was IGNORED. Both halves are recorded: the caveat is
        what a person reads, the trace row is what a diff compares."""
        self.caveat("{} from {} was ignored: {}".format(field, level, why))


def _remember(resolution_id: str, rows: Sequence[Decision]) -> None:
    if not resolution_id:
        return
    _TRACES[resolution_id] = tuple(rows)
    _TRACES.move_to_end(resolution_id)
    while len(_TRACES) > _TRACE_LIMIT:
        _TRACES.popitem(last=False)


def trace_of(resolution: Any) -> Tuple[Decision, ...]:
    """The decisions behind a resolution, or `()` when they are not at hand.

    Empty is a legitimate answer — a resolution parsed from a record this
    process never produced has no trace — and :func:`explain` says so rather
    than inventing levels.
    """
    key = str(getattr(resolution, "resolution_id", "") or "")
    return _TRACES.get(key, ())


# ── small readers ──────────────────────────────────────────────────────────
# Every one of them answers "nothing stated" rather than raising: the levels
# are user input (a task payload, a project file, an activity policy) and a
# resolver that threw on a stray type would take the dispatcher with it.

def _map(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _word(value: Any, limit: int = 200) -> str:
    return str(value).strip()[:limit] if isinstance(value, str) else ""


def _words(value: Any, limit: int = 128) -> Tuple[str, ...]:
    """A list of names. A bare string is read as ONE name, never split on
    commas: guessing there is how an allowlist becomes something nobody wrote
    (the same reason `contracts._as_tuple` refuses it outright)."""
    if isinstance(value, str):
        name = value.strip()
        return (name,) if name else ()
    if not isinstance(value, (list, tuple, set, frozenset)):
        return ()
    out: List[str] = []
    for item in list(value)[:limit]:
        name = _word(item, 512)
        if name and name not in out:
            out.append(name)
    return tuple(out)


def _count(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = int(value)
    return number if number > 0 else None


def _rules(value: Any) -> Tuple[str, ...]:
    """Permission rules as text, in order. A rule whose effect cannot be read
    is dropped here rather than merged as if it granted something."""
    out: List[str] = []
    for item in _words(value, 128):
        if isinstance(item, str) and item.split(None, 1)[:1] and \
                item.split(None, 1)[0].lower() in ("allow", "deny"):
            out.append(item)
    return tuple(out)


def _is_deny(rule: str) -> bool:
    parts = str(rule or "").split(None, 1)
    return bool(parts) and parts[0].lower() == "deny"


def _defs_index(defs: Any, workspace: Optional[str], ledger: _Ledger) -> Dict[str, Any]:
    """`{slug: AgentDef}` from whatever the caller had at hand.

    A `LoadResult`, a mapping, a list, or nothing — in which case the store is
    read. Never raises: an unreadable store degrades to an empty catalogue with
    a named degradation, because "there are no definitions" and "the disk is
    broken" must not look the same to whoever reads the resolution.
    """
    if defs is None:
        try:
            from src import agent_defs
            return agent_defs.load_all(workspace).by_slug()
        except Exception as exc:  # noqa: BLE001 - a broken store is not a crash
            ledger.degrade("agent_definitions", "the definition store could not be read ({})"
                           .format(type(exc).__name__))
            return {}
    by_slug = getattr(defs, "by_slug", None)
    if callable(by_slug):
        try:
            return dict(by_slug())
        except Exception:  # noqa: BLE001
            return {}
    if isinstance(defs, Mapping):
        return {str(k): v for k, v in defs.items()}
    if isinstance(defs, (list, tuple)):
        return {str(getattr(d, "slug", "") or ""): d for d in defs if getattr(d, "slug", "")}
    return {}


def _vocabulary(ledger: _Ledger) -> Tuple[str, ...]:
    """Every tool name this build knows, for materialising an allowlist.

    An empty answer is reported rather than treated as "no tools": a definition
    with no `tools` list means "everything not denied", and materialising that
    into an empty envelope while claiming to know the answer would understate
    what the run can do — which is the one direction a permission record must
    never be wrong in.
    """
    try:
        from src import agent_defs
        names = tuple(sorted(agent_defs.known_tools()))
    except Exception as exc:  # noqa: BLE001
        names = ()
        logger.debug("resolver: tool vocabulary unavailable: %s", exc)
    if not names:
        ledger.degrade("tool_index", "the tool vocabulary could not be read; an open allowlist "
                                     "could not be materialised")
    return names


# ── the ladder, as data ────────────────────────────────────────────────────

def _from_definition(d: Any) -> Dict[str, Any]:
    """What the `agent_default` level of §1.6 offers.

    Read off the MATERIALISED definition (`agent_defs.resolve_extends` has
    already folded any parent in), so a child that inherits a deny offers that
    deny here, and the ladder never has to know inheritance existed.
    """
    if d is None:
        return {}
    said = set(getattr(d, "stated", ()) or ())

    def stated(key: str, field: str) -> str:
        """A field whose default is not blank counts as an opinion only when
        the FILE stated it.

        `default_completion_mode` is `greedy` and the four profile references
        are `default` on every definition ever written, including the ones
        written before those fields existed. Reading those defaults as an
        opinion would put `agent_default` above `project_default` for every
        definition in the store and make a project-wide default impossible to
        set — which is the precedence of §1.6 broken by a dataclass default.
        A definition parsed from a payload rather than a file carries no
        `stated`, and there the field IS the only evidence there is.
        """
        return _word(getattr(d, field, "")) if (not said or key in said) else ""

    return {
        "model": _word(getattr(d, "model", "")),
        "endpoint_id": _word(getattr(d, "endpoint_id", "")),
        "runner": _word(getattr(d, "runner", "")),
        "max_rounds": _count(getattr(d, "max_rounds", None)),
        "timeout_s": _count(getattr(d, "timeout_s", None)),
        "completion_mode": stated("default_completion_mode", "default_completion_mode"),
        "verification_profile": stated("verification_profile", "verification_profile"),
        "context_profile": stated("context_profile", "context_profile"),
        "budget_profile": stated("budget_profile", "budget_profile"),
        "collaboration_profile": stated("collaboration_profile", "collaboration_profile"),
        "output_contract": _word(getattr(d, "output_contract", "")),
    }


def _levels(*, definition: Any, task: Mapping[str, Any], activity: Mapping[str, Any],
            project_defaults: Mapping[str, Any], owner_policy: Mapping[str, Any],
            ) -> "OrderedDict[str, Dict[str, Any]]":
    """The eight levels of §1.6, strongest first, each with what it offers.

    This function is the reason the precedence is auditable: the ORDER is
    `contracts.PRECEDENCE` and nothing else, and each entry is a plain mapping,
    so a reader can print the ladder and see exactly which level had an opinion
    about which field. A level that says nothing carries an empty mapping and
    still appears — an absent level and a silent one are the same fact and get
    the same spelling.
    """
    project = {k: v for k, v in project_defaults.items() if k != "restrictions"}
    offered: Dict[str, Dict[str, Any]] = {
        "system_policy": dict(SYSTEM_POLICY),
        "owner_policy": dict(owner_policy),
        "project_restriction": _map(project_defaults.get("restrictions")),
        "activity_policy": dict(activity),
        "task_override": dict(task),
        "agent_default": _from_definition(definition),
        "project_default": dict(project),
        "global_default": dict(GLOBAL_DEFAULTS),
    }
    return OrderedDict((name, offered.get(name, {})) for name in PRECEDENCE)


def _pick(field: str, levels: "OrderedDict[str, Dict[str, Any]]", ledger: _Ledger, *,
          read=_word, validate=None, label: Optional[str] = None) -> Any:
    """Walk the ladder strongest-first and take the first level with a value.

    `validate(level, value)` returns a reason when that level may not have that
    value — which is how "an override may not cross the provider boundary"
    becomes data rather than a special case: the value is discarded WITH its
    reason and the walk carries on to the level below, so the run still starts
    on the route the definition named instead of failing.
    """
    discarded: List[Tuple[str, Any, str]] = []
    winner, chosen, found = "", read(None), False
    for level, offered in levels.items():
        if field not in offered:
            continue
        value = read(offered.get(field))
        if not value:
            continue
        why = validate(level, value) if validate is not None else ""
        if why:
            discarded.append((level, value, why))
            ledger.refuse(label or field, level, value, why)
            continue
        if not found:
            winner, chosen, found = level, value, True
            continue
        # The walk does not stop at the winner. A level that was merely
        # outranked is not a refusal and earns no caveat, but it IS what
        # somebody else asked for, and a table that omitted it would answer
        # "why this model?" with half the story.
        discarded.append((level, value, "outranked by " + winner))
    return ledger.decide(label or field, chosen, winner, discarded)


def _ceiling(field: str, levels: "OrderedDict[str, Dict[str, Any]]", ledger: _Ledger,
             cap: Optional[int]) -> Optional[int]:
    """A limit that every level may only LOWER (§15).

    Not `_pick`: for a ceiling the strongest level does not simply win, because
    a task that asks for MORE rounds than its definition allows is asking for
    authority it does not have. So the effective value is the smallest anybody
    stated, credited to the strongest level that stated it, and every level
    whose number did not reduce is recorded saying so. `cap` is the system
    ceiling (§1.6's top level): it binds without being a value, so a run that
    states no limit is not handed the maximum.
    """
    stated: List[Tuple[str, int]] = []
    for level, offered in levels.items():
        number = _count(offered.get(field))
        if number is not None:
            stated.append((level, number))
    if not stated:
        return ledger.decide(field, None, "")
    best = min(number for _, number in stated)
    winner = next(level for level, number in stated if number == best)
    discarded = [(level, number, "does not reduce {}".format(best))
                 for level, number in stated if number != best]
    # A base level whose number did not bind is not a refusal — the definition
    # said twelve and the task said four, and four is what §15 allows. It goes
    # in the trace and nowhere else. A level ABOVE the definition asking for a
    # bigger number is the sixth prohibition ("superar el presupuesto máximo"),
    # and that one earns a caveat where a run receipt will show it.
    for level, number, _why in discarded:
        if level in _OVERRIDE_LEVELS:
            ledger.refuse(field, level, number,
                          "an override may only reduce this limit, and {} is above the {} that "
                          "{} set".format(number, best, winner))
    if cap is not None and best > cap:
        discarded.append((winner, best, "above the hard ceiling of {}".format(cap)))
        ledger.refuse(field, winner, best, "above the hard ceiling of {}".format(cap))
        return ledger.decide(field, cap, "system_policy", discarded)
    return ledger.decide(field, best, winner, discarded)


def _system_caps() -> Dict[str, Optional[int]]:
    """The absolute ceilings, read from the loader that already enforces them.

    Imported rather than repeated: two copies of "40 rounds" in one codebase is
    how a definition becomes loadable and unresolvable. A build where the import
    fails gets no cap rather than a guessed one.
    """
    try:
        from src import agent_defs
        return {"max_rounds": int(agent_defs.MAX_ROUNDS),
                "timeout_s": int(agent_defs.MAX_TIMEOUT_S)}
    except Exception:  # noqa: BLE001
        return {"max_rounds": None, "timeout_s": None}


# ── where it runs ──────────────────────────────────────────────────────────

def _route(levels: "OrderedDict[str, Dict[str, Any]]", ledger: _Ledger, *,
           available_models: Sequence[str], available_runners: Sequence[str],
           unhealthy: Sequence[str], quarantined: Sequence[str]) -> ModelRoute:
    """The model, the endpoint and the runner, each decided on its own ladder.

    Three walks, not one, because they fail separately: a model that is not
    loaded, an endpoint that is unreachable and a runner this machine does not
    have are three different sentences to a user.

    An override may change the route only when the route it names is one this
    machine can actually take (§15's "cambiar modelo/endpoint/runner si
    compatible"). That is the provider boundary, and it is checked against what
    the CALLER observed — `available_models`, `available_runners`, `unhealthy`,
    `quarantined` — because there is no Immune System in this build to ask.
    An empty list means "the caller did not observe"; it is read as "cannot
    check", never as "nothing is available", or every override would be refused
    by a missing integration rather than by a real one.
    """
    blocked = {name.strip().lower() for name in list(unhealthy) + list(quarantined) if name}
    models = {name.strip().lower() for name in available_models if name}
    runners = {name.strip().lower() for name in available_runners if name}

    def check(known: set, kind: str):
        def validate(level: str, value: str) -> str:
            if value.lower() in blocked:
                return "`{}` is unhealthy or quarantined on this machine".format(value)
            if level in _BASE_LEVELS:
                return ""     # the definition's own route: reported, never refused
            if known and value.lower() not in known:
                return "`{}` is not one of the {}s this machine can reach".format(value, kind)
            return ""
        return validate

    model = _pick("model", levels, ledger, validate=check(models, "model"))
    endpoint = _pick("endpoint_id", levels, ledger, validate=check(set(), "endpoint"))
    runner = _pick("runner", levels, ledger, validate=check(runners, "runner"))
    # A route that came from a level ABOVE the definition was checked on the way
    # in; one that came from the definition itself was not, because refusing it
    # would leave the run with no route at all. It is reported instead, and only
    # then — saying "the definition names X" about a value an owner policy set
    # would send its reader to the wrong file.
    def unavailable(field: str, known: set) -> bool:
        row = next((r for r in reversed(ledger.rows) if r.field == field), None)
        value = str(row.value if row else "")
        return bool(value and known and value.lower() not in known
                    and (row.level in _BASE_LEVELS))

    if unavailable("model", models):
        ledger.caveat("the definition names model `{}`, which this machine did not report as "
                      "available; the runtime will route it or say why it could not".format(model))
    if unavailable("runner", runners):
        ledger.caveat("the definition names runner `{}`, which is not installed here"
                      .format(runner))
    return ModelRoute(model=model, endpoint_id=endpoint, runner=runner)


# ── how far it pushes ──────────────────────────────────────────────────────

def _completion(levels: "OrderedDict[str, Dict[str, Any]]", ledger: _Ledger,
                instruction: str) -> CompletionChoice:
    """The effective completion mode, on `completion.py`'s own six-level ladder.

    The three levels of §1.6 that are missing here — system policy, owner
    policy, project restrictions — are missing on purpose and `MODE_PRECEDENCE`
    says so: they gate permissions and effects, a mode is neither, and letting
    them decide a mode would be the first step towards reading one as authority.

    An explicit instruction is read at the TASK level and only when the task
    stated no mode: `from_instruction` is deterministic, offline and returns
    nothing the moment a sentence carries cues for two modes, so a guess never
    outranks something a person actually wrote.
    """
    offered: Dict[str, str] = {}
    for level, values in levels.items():
        mode = _word(values.get("completion_mode"), 40).lower()
        if not mode:
            continue
        if mode not in COMPLETION_MODES:
            ledger.refuse("completion_mode", level, mode,
                          "not a completion mode this build knows ({})"
                          .format(", ".join(COMPLETION_MODES)))
            continue
        offered[level] = mode
    spoken = ""
    if "task_override" not in offered and instruction:
        spoken = from_instruction(instruction)
        if spoken:
            ledger.caveat("the completion mode `{}` was read out of the instruction itself; "
                          "nothing at the task level stated one".format(spoken))
    ignored = [(level, mode, "a completion mode is depth, not authority: the permission levels "
                             "of §1.6 do not set one")
               for level, mode in offered.items()
               if level not in ("activity_policy", "task_override") + _BASE_LEVELS]
    for level, mode, why in ignored:
        ledger.refuse("completion_mode", level, mode, why)
    try:
        choice = resolve_mode(
            activity=offered.get("activity_policy", ""),
            task=offered.get("task_override", "") or spoken,
            run="",
            agent_default=offered.get("agent_default", ""),
            project_default=offered.get("project_default", ""),
            global_default=offered.get("global_default", "") or GLOBAL_DEFAULTS["completion_mode"],
        )
    except ProfileError as exc:  # pragma: no cover - every value was checked above
        ledger.caveat("the completion mode could not be resolved ({}); falling back to `{}`"
                      .format(exc.message, GLOBAL_DEFAULTS["completion_mode"]))
        policy = POLICIES[GLOBAL_DEFAULTS["completion_mode"]]
        choice = CompletionChoice(mode=policy.mode, policy_version=policy.policy_version,
                                  source="global_default", reason="resolution failed; the global "
                                                                  "default was used")
    losers = [(level, mode, "outranked by {}".format(choice.source))
              for level, mode in offered.items()
              if level in MODE_PRECEDENCE + ("activity_policy", "task_override")
              and mode != choice.mode]
    # `completion.py` spells the activity level `activity` and the contract
    # normalises that to `activity_override`; the ladder in §1.6 calls the same
    # level `activity_policy`. One spelling reaches the trace, or the table
    # would name a level the precedence tuple does not contain.
    level = {"activity_override": "activity_policy"}.get(choice.source, choice.source)
    ledger.decide("completion_mode", choice.mode, level, losers + ignored)
    return choice


# ── which versioned policies it references ─────────────────────────────────

def _equal_or_stricter(kind: str, base: str, candidate: str) -> bool:
    """Is `candidate` at least as strict as `base`?

    `catalog.stricter` returns its FIRST argument on a tie, which is what makes
    this readable in one line: asking it with `candidate` first answers "base is
    not strictly stricter", i.e. candidate ties or wins. §15 allows an override
    to choose a verification that is equal *or* stronger, so a tie has to pass.
    """
    try:
        return catalog.stricter(kind, candidate, base) == candidate
    except ProfileError:
        return False


def _profiles(levels: "OrderedDict[str, Dict[str, Any]]", ledger: _Ledger) -> ProfileSet:
    """The five profile references, each walked down the ladder.

    The base comes from the definition (or the project, or the catalogue
    default). Above it, a level may only choose a profile that is EQUAL OR
    STRICTER for the four families where strictness means authority or spend —
    verification, context, budget, collaboration — which is §15's "escoger
    verificación igual o más fuerte" and "reducir budget" in one rule instead of
    two. `output` is exempt: a contract is the shape of a handoff, it cannot
    fabricate proof (§13), and forcing it to grow monotonically would block a
    legitimate change of handoff shape for no safety gained.

    An id this build does not know is ignored and named, never substituted: a
    silent fallback to `default` is how a run claims a verification it never ran.
    """
    chosen: Dict[str, str] = {}
    for kind, key in _PROFILE_KEYS:
        base = ""
        base_level = ""
        discarded: List[Tuple[str, Any, str]] = []
        # The base is the STRONGEST base level that named a profile this build
        # knows: the definition beats the project, and the project beats the
        # catalogue default. Walking to the end instead of stopping here is how
        # the weakest level quietly wins, which is the precedence upside down.
        for level in _BASE_LEVELS:
            value = _word(levels.get(level, {}).get(key), 128)
            if value and (kind == "output" or catalog.exists(kind, value)):
                base, base_level = value, level
                break
        for level in _BASE_LEVELS:
            value = _word(levels.get(level, {}).get(key), 128)
            if value and value != base and not catalog.exists(kind, value) and kind != "output":
                ledger.refuse(key, level, value,
                              "no such {} profile in this build".format(kind))
        effective, winner = base, base_level
        for level in _OVERRIDE_LEVELS:
            value = _word(levels.get(level, {}).get(key), 128)
            if not value or value == effective:
                continue
            if not catalog.exists(kind, value):
                discarded.append((level, value, "no such {} profile in this build".format(kind)))
                ledger.refuse(key, level, value, "no such {} profile in this build".format(kind))
                continue
            if kind in _NARROWING_KINDS and effective and not _equal_or_stricter(kind, effective, value):
                why = ("`{}` is weaker than `{}`, and an override may only choose an equal or "
                       "stronger one".format(value, effective))
                discarded.append((level, value, why))
                ledger.refuse(key, level, value, why)
                continue
            effective, winner = value, level
        chosen[kind] = ledger.decide(key, effective, winner, discarded) or ""
    return ProfileSet(context=chosen.get("context") or "default",
                      verification=chosen.get("verification") or "default",
                      budget=chosen.get("budget") or "default",
                      collaboration=chosen.get("collaboration") or "default",
                      output=chosen.get("output") or "")


# ── what it may actually touch ─────────────────────────────────────────────

def _effects_of(tools: Sequence[str], deny: Sequence[str], may_delegate: bool) -> Tuple[str, ...]:
    """The effects a definition's own allowlist already implies.

    DERIVED, never granted: an effect appears here because a tool that produces
    it is allowed and not denied, so an envelope can no more gain `write` from
    this function than a definition could gain `write_file` from a comment.
    """
    allowed = {t for t in tools if t not in set(deny)}
    out = [name for name, family in _EFFECT_TOOLS if allowed.intersection(family)]
    if may_delegate:
        out.append("delegate")
    return tuple(out)


def _base_envelope(definition: Any, levels: "OrderedDict[str, Dict[str, Any]]",
                   ledger: _Ledger) -> PermissionEnvelope:
    """The envelope as the DEFINITION states it, before any level narrows it.

    Two things this materialises rather than leaves implicit, because
    `PermissionEnvelope` reads an empty tuple as "nothing", not as "unset":

    * a definition with no `tools` list means "everything not denied", so the
      tool vocabulary is expanded into the envelope here. When the vocabulary
      cannot be read the envelope stays empty and `degraded_integrations` says
      why — understating what a run may do is the safe direction to be wrong in,
      but only if the record admits it;
    * `work_roots` come from the PROJECT (§1.6: a definition never names its own
      project or roots). No project means no roots, which grants nothing.
    """
    deny = _words(getattr(definition, "deny", ()) or ())
    declared = _words(getattr(definition, "tools", ()) or ())
    if declared:
        tools = declared
    else:
        vocabulary = _vocabulary(ledger)
        tools = tuple(t for t in vocabulary if t not in set(deny))
        if vocabulary:
            ledger.caveat("the definition names no tool allowlist, so every tool this build knows "
                          "({}) minus its denies is what it may use".format(len(vocabulary)))
    rules = tuple(r.as_text() for r in (getattr(definition, "permission", ()) or ())
                  if hasattr(r, "as_text"))
    roots = _words(levels.get("project_default", {}).get("work_roots"), 64)
    may_delegate = bool(getattr(definition, "may_delegate", lambda: False)())
    return PermissionEnvelope(tools=tools, deny=deny, work_roots=roots,
                              effects=_effects_of(tools, deny, may_delegate),
                              permission_rules=rules)


def _within(child: str, parent: str) -> bool:
    """Whether one work root lies inside another, textually.

    The authority on matching a PATH against a pattern is
    `src.subagent_permissions`; all this has to answer is whether a narrower
    root is inside a wider one, and anything cleverer here would be a second
    matcher free to disagree with the first.
    """
    inner = str(child or "").strip().replace("\\", "/").rstrip("/").lower()
    outer = str(parent or "").strip().replace("\\", "/").rstrip("/").lower()
    return bool(inner) and bool(outer) and (inner == outer or inner.startswith(outer + "/"))


def _restrict(base: PermissionEnvelope, levels: "OrderedDict[str, Dict[str, Any]]",
              ledger: _Ledger) -> PermissionEnvelope:
    """Fold every level's restrictions into the definition's envelope.

    This function is §15's list of six, written as five operations that have no
    way to express the opposite:

    * `tools` — INTERSECT. A name the definition does not allow is ignored and
      named; enabling a tool outside the allowlist is the third prohibition.
    * `deny` — UNION. There is no code path that removes one; removing a deny is
      the first prohibition, and a level that lists a name here can only ever
      add to the refusals.
    * `permission` — DENY rules are appended (last match wins, so an appended
      deny beats every allow before it). An `allow` rule from any level is
      ignored: widening a path is the second prohibition, and this resolver has
      no authority to grant one — that is an edit to the definition.
    * `work_roots` — CONTAINMENT-AWARE INTERSECT. A root inside an existing one
      narrows; a root outside is ignored. Adding a root to an empty set is a
      widening too, because an empty `work_roots` grants nothing.
    * `effects` — INTERSECT. An effect the definition does not already have is
      ignored: the second prohibition again, on the other axis.

    The remaining two prohibitions of §15 live where their values do —
    verification in :func:`_profiles`, budget and the ceilings in
    :func:`_ceiling` and :func:`_profiles`, the provider boundary in
    :func:`_route` — because a rule enforced next to the value it constrains is
    a rule somebody can find.
    """
    tools, deny = list(base.tools), list(base.deny)
    roots, effects = list(base.work_roots), list(base.effects)
    rules = list(base.permission_rules)
    shaped: Dict[str, List[str]] = {name: ["agent_default"] for name in
                                    ("tools", "deny", "permission_rules", "effects")}
    shaped["work_roots"] = ["project_default"] if roots else []
    dropped: Dict[str, List[Tuple[str, Any, str]]] = {name: [] for name in shaped}

    def refuse(field: str, level: str, value: Any, why: str) -> None:
        dropped[field].append((level, value, why))
        ledger.refuse(field, level, value, why)

    for level in _OVERRIDE_LEVELS:
        offered = levels.get(level, {})
        if not offered:
            continue
        wanted = _words(offered.get("tools"))
        if wanted:
            outside = [t for t in wanted if t not in set(base.tools)]
            for name in outside:
                refuse("tools", level, name,
                       "`{}` is not in the definition's allowlist, and an override may remove a "
                       "tool but never enable one".format(name))
            keep = [t for t in tools if t in set(wanted)]
            if keep != tools:
                tools = keep
                shaped["tools"].append(level)
        for name in _words(offered.get("deny")) + _words(offered.get("deny_tools")):
            if name not in deny:
                deny.append(name)
                shaped["deny"].append(level)
            if name in tools:
                tools = [t for t in tools if t != name]
                shaped["tools"].append(level)
        for rule in _rules(offered.get("permission")):
            if not _is_deny(rule):
                refuse("permission_rules", level, rule,
                       "an allow rule would widen what the definition permits; only a deny may be "
                       "added from a level below the definition")
                continue
            if rule not in rules:
                rules.append(rule)
                shaped["permission_rules"].append(level)
        wanted_roots = _words(offered.get("work_roots"), 64)
        if wanted_roots:
            inside = [r for r in wanted_roots if any(_within(r, have) for have in base.work_roots)]
            for root in wanted_roots:
                if root not in inside:
                    refuse("work_roots", level, root,
                           "`{}` is not inside a root the project already granted".format(root))
            if inside:
                # The same containment-aware intersection `PermissionEnvelope`
                # performs: `src` ∩ `src/lib` is `src/lib`, not the empty set,
                # because a plain string intersection would collapse a
                # legitimate narrowing into "no roots at all" and a reader
                # would have to guess whether that meant everything or nothing.
                merged: List[str] = []
                for current in roots:
                    for want in inside:
                        pick = want if _within(want, current) else (
                            current if _within(current, want) else "")
                        if pick and pick not in merged:
                            merged.append(pick)
                if merged != roots:
                    roots = merged
                    shaped["work_roots"].append(level)
        wanted_effects = _words(offered.get("effects"), 32)
        if wanted_effects:
            for name in wanted_effects:
                if name not in set(base.effects):
                    refuse("effects", level, name,
                           "`{}` is not an effect the definition already has, and an override may "
                           "drop one but never add one".format(name))
            keep_effects = [e for e in effects if e in set(wanted_effects)]
            if keep_effects != effects:
                effects = keep_effects
                shaped["effects"].append(level)

    effective = PermissionEnvelope(tools=tuple(tools), deny=tuple(deny),
                                   work_roots=tuple(roots), effects=tuple(effects),
                                   permission_rules=tuple(rules))
    for field, value in (("tools", effective.tools), ("deny", effective.deny),
                         ("work_roots", effective.work_roots), ("effects", effective.effects),
                         ("permission_rules", effective.permission_rules)):
        # The level column names every level that SHAPED the value, weakest
        # first. One name would have to pick between "the definition allowed
        # these four" and "the task removed one of them", and both are true.
        ledger.decide(field, list(value), " + ".join(dict.fromkeys(shaped[field])),
                      dropped[field])
    return effective


# ── which agent ────────────────────────────────────────────────────────────

def _selector():
    """`selection.select` and `selection.TaskSpec`, or `None`.

    The selector lands in this package on its own schedule and this module must
    work before it does: an agent that was NAMED never needed it, and one that
    was not degrades to a declared fallback rather than to an exception.
    """
    try:
        from .selection import TaskSpec, select     # type: ignore
        return TaskSpec, select
    except Exception:  # noqa: BLE001 - not built yet, or broken: same answer here
        return None


def _call_filtered(func: Any, **kwargs: Any) -> Any:
    """Call `func` with the subset of `kwargs` it actually declares.

    The selector is written by somebody else, in parallel with this file. Its
    signature is its own business, and guessing it would make this module break
    the day it grows a parameter — so what is passed is what it asked for.
    """
    import inspect
    try:
        names = set(inspect.signature(func).parameters)
    except (TypeError, ValueError):  # pragma: no cover - a builtin, unlikely here
        names = set()
    return func(**{k: v for k, v in kwargs.items() if k in names})


def _fallback_pick(index: Mapping[str, Any], task: Mapping[str, Any]) -> Tuple[Any, str]:
    """The agent to run when nobody named one and there is no selector.

    Deterministic and dull on purpose: the mode the task asked for, then
    alphabetical order. A cleverer guess here would be a second selector with
    no trace, competing with the real one.
    """
    wanted = _word(task.get("mode") or task.get("slot"), 40).lower()
    rows = sorted(index.items())
    for slug, definition in rows:
        if wanted and str(getattr(definition, "mode", "")) == wanted:
            return definition, "the only rule available: the first definition whose mode is `{}`" \
                .format(wanted)
    for slug, definition in rows:
        if str(getattr(definition, "mode", "")) == "worker":
            return definition, "the only rule available: the first `worker` definition, in " \
                               "alphabetical order"
    return (rows[0][1] if rows else None), "the only rule available: the first definition, in " \
                                           "alphabetical order"


def _choose(agent: str, task: Mapping[str, Any], index: Mapping[str, Any],
            ledger: _Ledger, *, unhealthy: Sequence[str],
            quarantined: Sequence[str]) -> Tuple[Any, SelectionTrace]:
    """The definition this execution runs, and the record of why (§14).

    An agent is never chosen for having a nice name: either the caller named it
    (and that is the reason), or the selector chose it and its own trace is
    kept, or there is no selector and the fallback SAYS it is a fallback. A
    resolution whose selection reason is empty is one nobody can audit.
    """
    requested = _word(agent, 80) or _word(task.get("agent"), 80)
    if requested:
        definition = index.get(requested)
        if definition is not None:
            return definition, SelectionTrace(requested=requested, chosen=requested,
                                              reason="the caller named this agent")
        ledger.caveat("agent `{}` is not a definition this build knows".format(requested))
    selector = _selector()
    if selector is not None:
        TaskSpec, select = selector
        try:
            wanted = dict(task)
            wanted.setdefault("instruction", "")
            spec = _call_filtered(TaskSpec, **wanted)
            trace = _call_filtered(select, task=spec, spec=spec, defs=index, agents=index,
                                   candidates=list(index.values()), unhealthy=list(unhealthy),
                                   quarantined=list(quarantined))
            if isinstance(trace, SelectionTrace) and trace.chosen in index:
                return index[trace.chosen], trace
            ledger.caveat("the selector returned nothing this build could use; a fallback was "
                          "chosen instead")
        except Exception as exc:  # noqa: BLE001 - a selector fault is not a crash
            ledger.caveat("the selector failed ({}); a fallback was chosen instead"
                          .format(type(exc).__name__))
    else:
        ledger.degrade("agent_selection", "no selector in this build; the fallback rule chose")
    definition, reason = _fallback_pick(index, task)
    chosen = _word(getattr(definition, "slug", ""), 80)
    rejected = tuple((slug, "not compared: there is no selector to compare with")
                     for slug in sorted(index)[:8] if slug != chosen)
    return definition, SelectionTrace(requested=requested, chosen=chosen, reason=reason,
                                      alternatives_rejected=rejected)


# ── the one entry point ────────────────────────────────────────────────────

def _scope(raw: Mapping[str, Any], ledger: _Ledger) -> ExecutionScope:
    """The runtime's own fields, and only those (§1.6).

    An unknown key is reported rather than dropped (§22), and no key is read
    from the definition: an agent file that could name its own owner or project
    would be a file that grants itself a context.
    """
    known = {k: _word(raw.get(k), 128) for k in ExecutionScope._KEYS}
    for key in raw:
        if key not in ExecutionScope._KEYS:
            ledger.caveat("`{}` is not part of an execution scope and was not read".format(key))
    return ExecutionScope(**known)


def _unknown_keys(name: str, offered: Mapping[str, Any], ledger: _Ledger) -> None:
    for key in offered:
        if key not in OVERRIDE_KEYS:
            ledger.caveat("`{}` in the {} is not a field this build reads; it changed nothing"
                          .format(key, name))


def _minimal(agent: str, scope: Mapping[str, Any], ledger: _Ledger,
             reason: str) -> ResolvedAgentExecution:
    """The answer when resolution itself failed.

    Valid, serialisable, and grants nothing: empty tools, empty roots, empty
    effects. A dispatcher that cannot resolve must still be able to refuse WITH
    a reason, and an exception three frames deep is not one.
    """
    policy = POLICIES[GLOBAL_DEFAULTS["completion_mode"]]
    ledger.caveat(reason)
    ledger.caveat("this is a MINIMAL resolution: it grants nothing and is not the configuration "
                  "the definition asked for. Do not run it without reading the caveats.")
    return ResolvedAgentExecution(
        agent=AgentRef(slug=_word(agent, 80) or "unresolved"),
        execution_scope=ExecutionScope(owner=_word((scope or {}).get("owner"), 128),
                                       project_id=_word((scope or {}).get("project_id"), 128),
                                       session_id=_word((scope or {}).get("session_id"), 128),
                                       run_id=_word((scope or {}).get("run_id"), 128),
                                       turn_id=_word((scope or {}).get("turn_id"), 128)),
        completion=CompletionChoice(mode=policy.mode, policy_version=policy.policy_version,
                                    source="global_default",
                                    reason="resolution failed; the global default was used"),
        caveats=tuple(ledger.caveats), degraded_integrations=tuple(ledger.degraded))


def resolve(*, agent: str = "", task: Optional[Mapping] = None,
            scope: Optional[Mapping] = None, defs: Any = None,
            project_defaults: Optional[Mapping] = None,
            activity: Optional[Mapping] = None,
            available_models: Sequence[str] = (),
            available_runners: Sequence[str] = (),
            unhealthy: Sequence[str] = (), quarantined: Sequence[str] = (),
            instruction: str = "",
            owner_policy: Optional[Mapping] = None,
            workspace: Optional[str] = None) -> ResolvedAgentExecution:
    """The effective configuration of one execution, pinned before it runs.

    `agent` names the definition (blank asks the selector); `task` is the
    override contract of §15; `scope` is the runtime's owner/project/session;
    `defs` is a `LoadResult`, a mapping or a list — omit it and the store is
    read. `project_defaults` carries the project's defaults and, under
    `restrictions`, the project level of §1.6; `activity` is the Council /
    Branching / workflow policy. The four availability lists are what the
    CALLER observed, because there is no Immune System here to ask.

    `owner_policy` and `workspace` are the two additions to the plan's
    signature: the owner level of §1.6 needs a channel that is not the scope
    (an execution scope is identity, not policy), and the definition store
    needs to know which folder's repo definitions are in play. Both are
    optional and both default to "nothing stated".

    **Never raises.** Every failure comes back as a resolution whose `caveats`
    say what went wrong; the worst case is :func:`_minimal`, which grants
    nothing.
    """
    ledger = _Ledger()
    task_map, activity_map = _map(task), _map(activity)
    project_map, owner_map = _map(project_defaults), _map(owner_policy)
    scope_map = _map(scope)
    try:
        _unknown_keys("task override", task_map, ledger)
        _unknown_keys("activity policy", activity_map, ledger)
        index = _defs_index(defs, workspace, ledger)
        definition, selection = _choose(agent, task_map, index, ledger,
                                        unhealthy=unhealthy, quarantined=quarantined)
        if definition is None:
            return _minimal(agent or _word(task_map.get("agent"), 80), scope_map, ledger,
                            "there is no agent definition to resolve: the store is empty and "
                            "nothing was named")
        levels = _levels(definition=definition, task=task_map, activity=activity_map,
                         project_defaults=project_map, owner_policy=owner_map)
        caps = _system_caps()
        # The health signal comes from the caller because there is nothing here
        # to ask. Saying so is the point of §1.3: an absent integration degrades
        # with a reason, and never to an invented `healthy`.
        ledger.degrade("capability_health",
                       "no Immune System in this build; availability was read from the {} model(s) "
                       "and {} runner(s) the caller observed"
                       .format(len(list(available_models)), len(list(available_runners))))
        route = _route(levels, ledger, available_models=available_models,
                       available_runners=available_runners, unhealthy=unhealthy,
                       quarantined=quarantined)
        completion = _completion(levels, ledger, instruction)
        profiles = _profiles(levels, ledger)
        permissions = _restrict(_base_envelope(definition, levels, ledger), levels, ledger)
        rounds = _ceiling("max_rounds", levels, ledger, caps.get("max_rounds"))
        timeout = _ceiling("timeout_s", levels, ledger, caps.get("timeout_s"))
        revision = ""
        try:
            from src import agent_defs
            revision = agent_defs.revision_of(definition)
        except Exception as exc:  # noqa: BLE001
            ledger.caveat("the definition's revision could not be computed ({}), so this "
                          "resolution cannot prove which version it started from"
                          .format(type(exc).__name__))
        for caveat in (getattr(definition, "caveats", ()) or ()):
            ledger.caveat(str(caveat))
        resolution = ResolvedAgentExecution(
            agent=AgentRef(slug=_word(getattr(definition, "slug", ""), 80),
                           definition_revision=revision,
                           source=_word(getattr(definition, "source", ""), 40)),
            execution_scope=_scope(scope_map, ledger),
            model_route=route, completion=completion, profiles=profiles,
            permissions=permissions, selection=selection,
            max_rounds=rounds, timeout_s=timeout,
            caveats=tuple(ledger.caveats),
            degraded_integrations=tuple(ledger.degraded),
            # The prompt itself never enters the record (§21, §23): what a run
            # started from is provable from the digest, and a prompt in a log is
            # both a leak and dead weight.
            prompt_digest=sha256_digest("{}\n\n{}".format(
                getattr(definition, "prompt", "") or "", instruction or "")),
        )
    except Exception as exc:  # noqa: BLE001 - the hot path never raises
        logger.warning("resolver: falling back to a minimal resolution: %s", exc, exc_info=True)
        return _minimal(agent, scope_map, ledger,
                        "the resolution failed with {}: {}".format(type(exc).__name__, exc))
    _remember(resolution.resolution_id, ledger.rows)
    logger.debug("resolved %s for agent %s (%s)", resolution.resolution_id,
                 resolution.agent.slug, resolution.completion.mode)
    return resolution


# ── saying it out loud ─────────────────────────────────────────────────────

def _show(value: Any) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value) if value else "(none)"
    return str(value)


def _rows_from_record(resolution: ResolvedAgentExecution) -> List[Decision]:
    """The table a resolution can still produce with no trace at hand.

    Every level here is the one the RECORD itself knows — the completion
    source, the definition's own source — and every other row says `pinned`
    rather than guessing which level put it there. A resolution read back from
    a run has lost the walk, and inventing it would be worse than admitting it.
    """
    p = resolution.permissions
    rows = [
        Decision("completion_mode", resolution.completion.mode,
                 resolution.completion.source or "pinned"),
        Decision("model", resolution.model_route.model, "pinned"),
        Decision("endpoint_id", resolution.model_route.endpoint_id, "pinned"),
        Decision("runner", resolution.model_route.runner, "pinned"),
        Decision("max_rounds", resolution.max_rounds, "pinned"),
        Decision("timeout_s", resolution.timeout_s, "pinned"),
    ]
    rows += [Decision(key, getattr(resolution.profiles, kind), "pinned")
             for kind, key in _PROFILE_KEYS]
    rows += [Decision("tools", list(p.tools), "pinned"),
             Decision("deny", list(p.deny), "pinned"),
             Decision("work_roots", list(p.work_roots), "pinned"),
             Decision("effects", list(p.effects), "pinned"),
             Decision("permission_rules", list(p.permission_rules), "pinned")]
    return rows


def explain_rows(resolution: ResolvedAgentExecution) -> List[Dict[str, Any]]:
    """The table :func:`explain` prints, as data — for the API and the page."""
    rows = trace_of(resolution) or _rows_from_record(resolution)
    return [row.to_dict() for row in rows]


def explain(resolution: ResolvedAgentExecution) -> str:
    """The resolution as plain text: value, level, and what was discarded.

    Three columns because three questions get asked in this order — what is it
    set to, who set it, and what did somebody else want instead. A receipt that
    answers only the first sends its reader to re-derive the other two from the
    definition file, which by then may have changed.
    """
    rows = trace_of(resolution) or _rows_from_record(resolution)
    head = ["resolution {} — agent `{}` ({}, revision {})".format(
        resolution.resolution_id or "(unsaved)", resolution.agent.slug or "?",
        resolution.agent.source or "unknown source",
        resolution.agent.definition_revision or "not computed")]
    scope = resolution.execution_scope
    where = [x for x in ("owner " + scope.owner if scope.owner else "",
                         "project " + scope.project_id if scope.project_id else "",
                         "session " + scope.session_id if scope.session_id else "") if x]
    if where:
        head.append("  " + " · ".join(where))
    if not trace_of(resolution):
        head.append("  (read back from a record: the walk that produced it was not kept, so every "
                    "level below reads `pinned`)")
    width = max([len(r.field) for r in rows] + [5])
    value_width = min(52, max([len(_show(r.value)) for r in rows] + [5]))
    head.append("")
    head.append("  {}  {}  {}".format("field".ljust(width), "value".ljust(value_width), "from"))
    for row in rows:
        # Wrapped, never truncated. A tool list cut off at the column width is
        # an audit table that quietly disagrees with the envelope it describes.
        lines = textwrap.wrap(_show(row.value), value_width) or ["—"]
        head.append("  {}  {}  {}".format(row.field.ljust(width), lines[0].ljust(value_width),
                                          row.level or "—"))
        head += ["  {}  {}".format(" " * width, extra) for extra in lines[1:]]
        for level, value, why in row.discarded:
            head.append("  {}  discarded: {} wanted {} — {}".format(
                " " * width, level, _show(value), why))
    if resolution.caveats:
        head.append("")
        head.append("  caveats:")
        head += ["    - " + c for c in resolution.caveats]
    if resolution.degraded_integrations:
        head.append("")
        head.append("  degraded integrations:")
        head += ["    - " + d for d in resolution.degraded_integrations]
    return "\n".join(head)


# ── pinning it to a run, and reading it back (§20) ─────────────────────────

def snapshot(resolution: ResolvedAgentExecution) -> Dict[str, Any]:
    """The resolution as it is fixed into a run, a branch or a queued job.

    Carries three things beyond `to_dict()`, each of which stops a question
    from becoming an investigation: `identity`, so two branches can be compared
    without re-deriving anything; `trace`, so the table :func:`explain` prints
    survives the process that produced it; and `pinned_at`, so "when was this
    decided" does not have to be inferred from a log line.

    What it does NOT carry is the prompt. `prompt_digest` proves which prompt a
    run started from; the text itself would be a leak in every place this dict
    travels (§21, §23).
    """
    return {
        "schema": SNAPSHOT_SCHEMA,
        "pinned_at": now_iso(),
        "identity": resolution.identity(),
        "resolution": resolution.to_dict(),
        "trace": [row.to_dict() for row in trace_of(resolution)],
    }


def rehydrate(raw: Mapping, *, defs: Any = None,
              workspace: Optional[str] = None) -> ResolvedAgentExecution:
    """A pinned snapshot back into the object the runtimes consume.

    Accepts the wrapper :func:`snapshot` writes or a bare `to_dict()`, so a run
    recorded before the wrapper existed still reads.

    **It never re-resolves.** The whole point of §20 is that a job encoded today
    does not change behaviour because somebody edited an AGENT.md tomorrow — so
    when the definition's current revision differs from the pinned one, that is
    said in `caveats` and the PINNED configuration is what comes back. Silently
    re-resolving would be the exact failure the revision exists to prevent, and
    silently ignoring the change would leave the operator wondering why the run
    does not match the file they are reading.
    """
    data = _map(raw)
    body = _map(data.get("resolution")) if isinstance(data.get("resolution"), Mapping) else data
    notes: List[str] = []
    try:
        resolution = ResolvedAgentExecution.parse(body)
    except ProfileError as exc:
        ledger = _Ledger()
        return _minimal(_word(_map(body.get("agent")).get("slug"), 80), _map(body.get(
            "execution_scope")), ledger,
            "the pinned snapshot could not be read ({}: {})".format(exc.path, exc.message))
    pinned_identity = _word(data.get("identity"), 100)
    if pinned_identity and pinned_identity != resolution.identity():
        notes.append("this snapshot's stored identity does not match the configuration it "
                     "carries; it was edited after it was pinned")
    slug = resolution.agent.slug
    if slug:
        try:
            index = _defs_index(defs, workspace, _Ledger())
            current = index.get(slug)
            if current is None:
                notes.append("agent `{}` is not a definition this build knows any more; the "
                             "pinned configuration is what runs".format(slug))
            else:
                from src import agent_defs
                now = agent_defs.revision_of(current)
                if resolution.agent.definition_revision and now != resolution.agent.definition_revision:
                    notes.append(
                        "agent `{}` has been edited since this was pinned ({} → {}); the PINNED "
                        "configuration is what runs, not the file as it reads today"
                        .format(slug, resolution.agent.definition_revision[:19], now[:19]))
        except Exception as exc:  # noqa: BLE001 - a store fault is not a crash
            notes.append("the definition could not be re-read to check the revision ({})"
                         .format(type(exc).__name__))
    rows = [row for row in (Decision.parse(item) for item in data.get("trace") or ())
            if row is not None]
    if rows:
        _remember(resolution.resolution_id, rows)
    if not notes:
        return resolution
    return replace(resolution, caveats=tuple(dict.fromkeys(list(resolution.caveats) + notes)))


def apply_task_override(base: ResolvedAgentExecution, override: Mapping, *,
                        available_models: Sequence[str] = (),
                        available_runners: Sequence[str] = (),
                        unhealthy: Sequence[str] = (),
                        quarantined: Sequence[str] = ()) -> ResolvedAgentExecution:
    """Narrow an already-resolved execution with a task override (§15).

    The same six prohibitions, applied to a resolution instead of a definition:
    the pinned configuration takes the place of the `agent_default` level and
    the override is the only other level with anything to say. What comes back
    is a NEW resolution — a new id, a caveat naming the one it narrows — because
    a different configuration with the same id is two runs a branch comparison
    cannot tell apart.

    Never raises, for the same reason :func:`resolve` does not.
    """
    ledger = _Ledger()
    ledger.caveats = list(base.caveats)
    ledger.degraded = list(base.degraded_integrations)
    over = _map(override)
    try:
        _unknown_keys("task override", over, ledger)
        levels: "OrderedDict[str, Dict[str, Any]]" = OrderedDict(
            (name, {}) for name in PRECEDENCE)
        levels["task_override"] = dict(over)
        levels["agent_default"] = {
            "model": base.model_route.model, "endpoint_id": base.model_route.endpoint_id,
            "runner": base.model_route.runner, "max_rounds": base.max_rounds,
            "timeout_s": base.timeout_s, "completion_mode": base.completion.mode,
            "verification_profile": base.profiles.verification,
            "context_profile": base.profiles.context,
            "budget_profile": base.profiles.budget,
            "collaboration_profile": base.profiles.collaboration,
            "output_contract": base.profiles.output,
        }
        caps = _system_caps()
        route = _route(levels, ledger, available_models=available_models,
                       available_runners=available_runners, unhealthy=unhealthy,
                       quarantined=quarantined)
        completion = _completion(levels, ledger, "")
        profiles = _profiles(levels, ledger)
        permissions = _restrict(base.permissions, levels, ledger)
        rounds = _ceiling("max_rounds", levels, ledger, caps.get("max_rounds"))
        timeout = _ceiling("timeout_s", levels, ledger, caps.get("timeout_s"))
        ledger.caveat("narrowed from resolution {}".format(base.resolution_id or "(unsaved)"))
        narrowed = ResolvedAgentExecution(
            resolution_id=new_resolution_id(), agent=base.agent,
            execution_scope=base.execution_scope, model_route=route, completion=completion,
            profiles=profiles, permissions=permissions, selection=base.selection,
            max_rounds=rounds, timeout_s=timeout, caveats=tuple(ledger.caveats),
            degraded_integrations=tuple(ledger.degraded),
            prompt_digest=base.prompt_digest)
    except Exception as exc:  # noqa: BLE001
        logger.warning("resolver: task override could not be applied: %s", exc, exc_info=True)
        ledger.caveat("the task override could not be applied ({}); the resolution it was meant to "
                      "narrow is unchanged".format(type(exc).__name__))
        return replace(base, caveats=tuple(dict.fromkeys(ledger.caveats)))
    _remember(narrowed.resolution_id, ledger.rows)
    return narrowed
