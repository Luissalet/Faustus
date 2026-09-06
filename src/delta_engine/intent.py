"""Turning a request into a contract, before anybody has seen the result. §6.

Rule 3 of `contracts.py`: an intent that was not frozen before the target was
observed can be edited to match it, and then every evaluation scores full
marks. This module is where the freezing happens, and everything in it is
arranged around one refusal.

**`compile` is deterministic and calls no model.** §6's "Generación" lists a
model for interpreting ambiguous language and it does not run here, for two
reasons that are not the same reason:

* §3.3 forbids building the diff by asking the model that produced the result.
  A probabilistic reading of the REQUEST is the same failure one step earlier:
  the contract is what arbitrates the result, and a contract a model wrote is a
  contract the model can be graded against by its own interpretation.
* A compiler that guesses is a compiler whose output changes between two runs
  over the same words, and `IntentContract.fingerprint` -- the §22 cache key --
  would stop recognising the same question.

So what `compile` does instead is narrow and boring on purpose:

* it takes the conditions the caller already handed over STRUCTURED;
* it adds the domain's default invariants through `invariants.resolve`;
* it recognises, deterministically, a handful of shapes the plan itself writes:
  `path: condition` in a fragment, `becomes(x)`, and the words `only` / `sólo`
  / `solo` marking a bounded scope;
* and **every fragment of `text` it cannot turn into a checkable condition goes
  to `intent.unknowns`, word for word.** Not dropped, not guessed at. A
  contract with unknowns still runs; the one thing it cannot do is report
  `matched`, and `verdict.assess` already enforces that.

The `only` rule has a second half that matters more than the first: the word
marks the scope as bounded IF AND ONLY IF the caller named the regions. "Change
only the jacket", with nobody having declared which region the jacket is, is an
`unknown` and not a scope -- a boundary drawn from a word nobody defined would
make every change outside an imaginary region read as incidental.

`revise` never mutates. It builds a NEW contract whose `supersedes` names the
old one and whose `frozen_at` is new, so the audit trail keeps both and the
second cannot pretend to have been the first.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from src.contracts.base import now_iso
from src.delta_engine import invariants as invariants_mod
from src.delta_engine.contracts import (
    CONDITION_KINDS,
    DeltaError,
    IntentContract,
    RequestedChange,
    ScopeRule,
    new_id,
)

__all__ = [
    "DEFAULT_PROFILE",
    "ONLY_WORDS",
    "compile",
    "revise",
    "unmet_confirmations",
    "render",
]

#: One definition, in `invariants`, re-exported here. Two spellings of "the
#: normal profile" would let a contract that named nothing and a contract that
#: named the default resolve to two different invariant sets and two different
#: fingerprints for one question.
DEFAULT_PROFILE = invariants_mod.DEFAULT_PROFILE

#: The words that mark a request as bounded. Closed and matched on word
#: boundaries, so `solo` does not fire inside `solomillo`.
#:
#: The brief for this module names `only`, `sólo` and `solo`; the other three
#: are here because §6's own worked example is "Cambia únicamente la chaqueta a
#: rojo", and a recogniser that misses the plan's example is a recogniser
#: nobody can use. The list stays closed: a synonym nobody wrote down is not
#: recognised, it is an `unknown`, which is the safe direction.
ONLY_WORDS: Tuple[str, ...] = (
    "only", "sólo", "solo", "solamente", "únicamente", "unicamente",
)

#: `intent.unknowns` is a `text_list`: at most 256 entries of at most 500
#: characters, no duplicates and no blanks. These two constants exist so the
#: cut is made HERE, where it can be described, instead of arriving as a
#: `ContractError` the caller reads as a bug in the contract.
_MAX_UNKNOWNS = 256
_MAX_UNKNOWN_CHARS = 500

_ONLY_RE = re.compile(r"(?<!\w)(?:%s)(?!\w)" % "|".join(ONLY_WORDS), re.IGNORECASE)
_CALL_RE = re.compile(r"^(?P<name>[A-Za-z_]+)\s*\(\s*(?P<value>.*?)\s*\)$", re.DOTALL)


def _fragments(text: str) -> Tuple[str, ...]:
    """The request, split into the pieces a condition could occupy.

    Newlines and semicolons only. A full stop is NOT a separator, because
    `character.jacket.color` is a path and splitting on the dot would turn one
    checkable condition into three fragments nobody can check -- and each of
    those three would then be filed as an `unknown`, which reads as the
    compiler being cautious when it was being careless.
    """
    out: List[str] = []
    for line in str(text or "").splitlines():
        for piece in line.split(";"):
            cleaned = piece.strip()
            if cleaned:
                out.append(cleaned)
    return tuple(out)


def _condition_of(right: str) -> Optional[Tuple[str, str]]:
    """`(condition, value)` out of the right-hand side of a fragment, or None.

    Two shapes and no third: `becomes(red)` -- a call whose name is one of
    `CONDITION_KINDS` -- and a bare condition word. `becomes` with an empty
    value answers `None` rather than a condition, because `RequestedChange`
    refuses a `becomes` with no value for the reason that matters here too: a
    target value nobody named cannot be compared against the result, so this is
    not a condition and belongs in `unknowns`.
    """
    candidate = str(right or "").strip()
    matched = _CALL_RE.match(candidate)
    if matched:
        name = matched.group("name").strip().lower()
        value = matched.group("value").strip()
        if name in CONDITION_KINDS and (name != "becomes" or value):
            return name, value
        return None
    word = candidate.lower()
    if word in CONDITION_KINDS and word != "becomes":
        return word, ""
    return None


def _as_condition(fragment: str) -> Optional[RequestedChange]:
    """`path: condition` out of one fragment, or `None` when it is not one.

    Colons are tried from the RIGHT, because a domain path may contain one
    (`state:project://admin/real/abc`) and the separator is whichever colon
    leaves a parseable condition on its right and a whitespace-free path on its
    left. Requiring the path to have no whitespace is what keeps prose out:
    "Note: the jacket must be red" has a path candidate of `Note` and a
    right-hand side that is not a condition, so it is an unknown and not a
    request about a field called `Note`.

    A `DeltaError` from `RequestedChange.parse` -- a path over 512 characters,
    a value over 1024 -- is caught and answered `None`. That is not a swallowed
    error: the question this function asks is "is this fragment a checkable
    condition", and a fragment the contract refuses is exactly a fragment that
    is not one. It goes to `unknowns` verbatim, which is where the caller can
    see it.
    """
    text = str(fragment or "")
    for index in range(len(text) - 1, -1, -1):
        if text[index] != ":":
            continue
        left = text[:index].strip()
        right = text[index + 1:]
        if not left or any(character.isspace() for character in left):
            continue
        parsed = _condition_of(right)
        if parsed is None:
            continue
        name, value = parsed
        payload: Dict[str, Any] = {"path": left, "condition": name}
        if value:
            payload["value"] = value
        try:
            return RequestedChange.parse(payload, "intent.text")
        except DeltaError:
            return None
    return None


def _fit(value: str, limit: int = _MAX_UNKNOWN_CHARS) -> str:
    """One unknown, cut to the contract's field limit and saying it was cut.

    The rule is "word for word", and a fragment longer than `IntentContract`
    accepts cannot be stored word for word by anybody. Cutting it with the cut
    NAMED is the only option that neither drops the fragment nor produces a
    payload the contract throws back at the caller.
    """
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    marker = " …(cut at the 500-character limit of intent.unknowns)"
    return text[:limit - len(marker)] + marker


def _read_text(text: str, *, scope: ScopeRule) -> Tuple[Tuple[RequestedChange, ...],
                                                        Tuple[str, ...], str]:
    """`(conditions, unknowns, scope note)` out of the free-text request.

    The strict half: a fragment that parses as a condition becomes one, and
    EVERY other fragment becomes an unknown, verbatim. There is no third
    outcome and in particular no "recognised well enough to discard" -- that
    category is how "keep the citations" disappears out of a contract and the
    delta comes back `matched`.

    The `only` half is independent of it and does not consume a fragment: a
    sentence containing `only` is still not a checkable condition, so it stays
    an unknown; what the word adds is a note on the scope, and only when the
    caller actually named the regions. §6's "cambia sólo la chaqueta" with no
    declared jacket mask therefore produces an unknown and NO boundary, which
    is the whole point -- a boundary drawn around a region nobody defined would
    make every other change in the image read as incidental.
    """
    conditions: List[RequestedChange] = []
    unknowns: List[str] = []
    said_only = False
    for fragment in _fragments(text):
        if _ONLY_RE.search(fragment):
            said_only = True
        parsed = _as_condition(fragment)
        if parsed is not None:
            conditions.append(parsed)
            continue
        unknowns.append(_fit(fragment))
    note = ""
    if said_only and scope.bounded:
        note = ("the request says `only`, and the regions it applies to are the "
                "ones named in `allowed`; anything changed outside them was not "
                "requested")
    return tuple(conditions), _bounded_unique(unknowns), note


def _bounded_unique(values: Sequence[str]) -> Tuple[str, ...]:
    """Order-preserving de-duplication, cut to the field's item limit.

    De-duplicated because this list is ASSEMBLED here out of the fragments of
    one request, and `text_list` refuses a duplicate -- the same sentence twice
    in a prompt is one unknown, not a contradiction the caller has to fix. The
    cut is reported as its own final entry rather than performed silently, for
    the reason the whole module exists: a request nobody could read is a fact
    the contract has to carry.
    """
    seen = set()
    out: List[str] = []
    for value in values:
        text = str(value).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    if len(out) <= _MAX_UNKNOWNS:
        return tuple(out)
    kept = out[:_MAX_UNKNOWNS - 1]
    kept.append(_fit(f"and {len(out) - len(kept)} further fragments of the "
                     f"request that this contract has no room to list; "
                     f"`intent.unknowns` holds at most {_MAX_UNKNOWNS} entries"))
    return tuple(kept)


def _dedupe(changes: Sequence[RequestedChange]) -> Tuple[RequestedChange, ...]:
    """One row per distinct condition. Same condition twice is one errand.

    `classification.classify_all` computes `unmet` by walking `intent.requested`
    and looking for an observation that satisfies each entry; a condition
    listed twice -- once structured by the caller and once recognised in the
    text they also passed -- would appear in `unmet` twice and make one errand
    nobody ran look like two.

    Identity is `(path, condition, value)`. `detail` and `priority` are not part
    of it: two rows differing only in priority are still one condition, and the
    first one wins because it is the caller's own structured statement.
    """
    seen = set()
    out: List[RequestedChange] = []
    for change in changes:
        key = (change.path, change.condition, change.value)
        if key in seen:
            continue
        seen.add(key)
        out.append(change)
    return tuple(out)


def compile(*, owner: str, domain: str, text: str = "",
            requested: Sequence[Mapping[str, Any]] = (),
            invariants: Sequence[Mapping[str, Any]] = (),
            scope: Optional[Mapping[str, Any]] = None,
            tolerances: Optional[Mapping[str, Any]] = None,
            acceptance: Sequence[str] = (),
            evaluation_profile: str = DEFAULT_PROFILE,
            project_id: str = "", supersedes: str = "",
            now: Optional[str] = None) -> IntentContract:
    """Freeze one request into a contract. Deterministic; no model runs here.

    Every field is assembled and then handed to `IntentContract.parse`, which
    is the only door: the dataclass constructor accepts anything, and the
    refusals that matter -- a `becomes` with no value, two invariants sharing an
    id, a `frozen_at` that is not a timestamp -- all live in `parse`. Building
    the contract directly here would be a second door into it, and the day the
    two disagree the caller who used the quiet one wins.

    `now` is a parameter rather than a call to the clock so a test can freeze
    it. It is not a way to backdate a contract: `frozen_at` is the moment the
    request stopped being editable, and a caller passing a time it did not
    freeze at is lying to its own audit trail, not to this function.

    `supersedes` is accepted here for the caller that is re-freezing a contract
    it already has, and `revise` is the path that fills it in correctly.
    """
    scope_rule = ScopeRule.parse(dict(scope or {}), "intent.scope")
    structured = tuple(
        RequestedChange.parse(item, f"intent.requested[{index}]")
        for index, item in enumerate(requested)
    )
    recognised, unknowns, note = _read_text(text, scope=scope_rule)
    conditions = _dedupe(structured + recognised)
    if note and not scope_rule.note:
        # The caller's own note wins: they wrote a sentence about their scope
        # and this one is a description of a word we recognised in their prose.
        scope_payload = dict(scope_rule.to_dict())
        scope_payload["note"] = note
        scope_rule = ScopeRule.parse(scope_payload, "intent.scope")
    profile = str(evaluation_profile or DEFAULT_PROFILE)
    resolved = invariants_mod.resolve(
        domain,
        explicit=tuple(invariants),
        requested=conditions,
        scope=scope_rule,
        profile=profile,
    )
    payload: Dict[str, Any] = {
        "id": new_id("intent"),
        "owner": owner,
        "domain": domain,
        "requested": [change.to_dict() for change in conditions],
        "invariants": [invariant.to_dict() for invariant in resolved],
        "scope": scope_rule.to_dict(),
        "tolerances": dict(tolerances or {}),
        "acceptance": list(acceptance),
        "unknowns": list(unknowns),
        "source_text": str(text or ""),
        "evaluation_profile": profile,
        "project_id": str(project_id or ""),
        "supersedes": str(supersedes or ""),
        "frozen_at": str(now or "").strip() or now_iso(),
    }
    return IntentContract.parse(payload, "intent")


#: What `revise` accepts. `supersedes` and `id` are deliberately absent: a
#: revision always points at the contract it revises, and a caller that could
#: set `supersedes` itself could build a chain that skips a link -- which is
#: the audit trail rule 3 exists to keep.
_REVISABLE: Tuple[str, ...] = (
    "owner", "domain", "text", "requested", "invariants", "scope",
    "tolerances", "acceptance", "evaluation_profile", "project_id", "now",
)


def revise(previous: IntentContract, **changes: Any) -> IntentContract:
    """A NEW contract that supersedes `previous`. Nothing is mutated.

    Rule 3 of `contracts.py`: "a change of mind is a NEW contract whose
    `supersedes` names this one, so the audit trail keeps both and the second
    cannot pretend to have been the first". `IntentContract` is a frozen
    dataclass, so mutation is impossible by construction; what this function
    adds is that the new contract cannot be built WITHOUT the back-pointer, and
    gets a new `frozen_at` -- a revision that kept the old timestamp would claim
    to have been frozen before a result it may well have been written after.

    An unrecognised keyword is an error naming it, not a default. This is
    rule 2 of `contracts/base.py`: `revise(contract, requsted=[...])` with a
    typo would otherwise return the old contract's requests under a new id, and
    nothing in the delta afterwards would say the change never arrived.
    """
    unknown = sorted(key for key in changes if key not in _REVISABLE)
    if unknown:
        raise DeltaError(
            "revise",
            f"does not take {unknown}; a keyword it ignored would be a change "
            f"the caller believes it made. It takes: {list(_REVISABLE)}",
            got=unknown,
        )
    return compile(
        owner=changes.get("owner", previous.owner),
        domain=changes.get("domain", previous.domain),
        text=changes.get("text", previous.source_text),
        requested=changes.get(
            "requested", [change.to_dict() for change in previous.requested]),
        invariants=changes.get(
            "invariants", [inv.to_dict() for inv in previous.invariants]),
        scope=changes.get("scope", previous.scope.to_dict()),
        tolerances=changes.get("tolerances", dict(previous.tolerances)),
        acceptance=changes.get("acceptance", list(previous.acceptance)),
        evaluation_profile=changes.get("evaluation_profile",
                                       previous.evaluation_profile),
        project_id=changes.get("project_id", previous.project_id),
        supersedes=previous.id,
        now=changes.get("now"),
    )


def unmet_confirmations(intent: IntentContract) -> Tuple[str, ...]:
    """§6's "elementos desconocidos que requieren confirmación", as sentences.

    Everything in this contract that a person still has to answer before the
    comparison it drives could ever report `matched`:

    * every entry in `unknowns` -- a fragment of the request nobody turned into
      a checkable condition;
    * every invariant `invariants.is_unknown` recognises -- a property the
      caller asked to preserve that has no method behind it.

    Both are absences, which is why they need naming: a delta whose assertions
    all came back clean and whose contract carried three of these looks, in
    every summary, exactly like a delta that checked everything.

    Empty means nothing needs confirming; it does NOT mean the contract is
    complete in some larger sense, and §6's other confirmation trigger -- a
    substantial change in scope or cost -- is a decision the caller makes with
    numbers this module never sees.
    """
    out: List[str] = []
    for unknown in intent.unknowns:
        out.append(f"not translated into a checkable condition: {unknown}")
    for invariant in intent.invariants:
        if invariants_mod.is_unknown(invariant):
            target = invariant.path or invariant.id
            out.append(f"no method is registered to check {target}; its result "
                       f"will be `unknown` and never `preserved`")
    seen = set()
    unique: List[str] = []
    for line in out:
        if line in seen:
            continue
        seen.add(line)
        unique.append(line)
    return tuple(unique)


def render(intent: IntentContract) -> str:
    """The frozen contract as plain text, for a card, a log or a review.

    Built from the fields on every call rather than stored, for
    `UniversalDelta`'s reason: a stored rendering is a second source of truth
    about the same facts, and the day it drifts the reader believes the shorter
    one.

    `unknowns` and the confirmations are printed LAST and never omitted when
    empty-looking, because this text is what a person reads before approving a
    comparison, and the section they need to see is the one saying what the
    contract could not express.
    """
    lines: List[str] = [
        f"intent {intent.id} ({intent.domain}, profile {intent.evaluation_profile})",
        f"  owner: {intent.owner}",
        f"  frozen at: {intent.frozen_at}",
    ]
    if intent.supersedes:
        lines.append(f"  supersedes: {intent.supersedes}")
    lines.append("  requested:")
    if intent.requested:
        for change in intent.requested:
            value = f"({change.value})" if change.value else ""
            lines.append(f"    - {change.path}: {change.condition}{value}")
    else:
        lines.append("    - nothing was stated as a checkable condition")
    lines.append("  invariants:")
    for invariant in intent.invariants:
        lines.append(f"    - {invariants_mod.describe(invariant)}")
    if intent.scope.allowed or intent.scope.forbidden:
        lines.append("  scope:")
        for rule in intent.scope.allowed:
            lines.append(f"    - allowed: {rule}")
        for rule in intent.scope.forbidden:
            lines.append(f"    - forbidden: {rule}")
        if intent.scope.note:
            lines.append(f"    - note: {intent.scope.note}")
    else:
        lines.append("  scope: unbounded; no editable region was named")
    if intent.tolerances:
        rendered = ", ".join(f"{key}={intent.tolerances[key]}"
                             for key in sorted(intent.tolerances))
        lines.append(f"  tolerances: {rendered}")
    for criterion in intent.acceptance:
        lines.append(f"  accepted when: {criterion}")
    confirmations = unmet_confirmations(intent)
    lines.append("  needs confirmation:")
    if confirmations:
        for line in confirmations:
            lines.append(f"    - {line}")
        lines.append("    (a contract with any of these cannot report `matched`)")
    else:
        lines.append("    - nothing; every part of the request became a "
                     "condition or an invariant")
    return "\n".join(lines)
