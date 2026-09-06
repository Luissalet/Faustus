"""What must hold still across a change, and where each one came from. §7.

`contracts.Invariant` is the shape; this module decides WHICH invariants a
request actually carries, out of four sources that routinely disagree: the
mandatory security floor, what the caller asked for, what the domain's
conservative defaults add, and what nothing here can check at all.

Four rules, each with the failure it prevents:

* **Security and permissions are mandatory in every profile.** §1.7: "todos
  mantienen invariantes de seguridad obligatorias". An `evaluation_profile`
  buys less COVERAGE -- fewer axes examined, cheaper tiers -- and never less
  security. A profile that could drop `permissions.not_widened` would make
  "compare this cheaply" a way to ship a permission change unexamined, which
  is the one trade nobody is allowed to make.

* **A weaker source never lowers a stronger one.** `SOURCE_STRENGTH` orders
  them `security > request > project_policy > capability > decision >
  operation > artifact > domain_default`, and `merge` also takes the STRONGEST
  severity of the two. A project policy that filed the same id as `minor` must
  not soften an invariant the user named: the policy would win on paper and the
  violation would be summarised away in practice.

* **Defaults are few and conservative.** A domain default is a claim that this
  property is worth checking on every comparison in the domain, and a long list
  of them produces a delta whose `unknown` rows outnumber its observations --
  at which point nobody reads any of them. Each entry below is one an adapter
  in this repository can actually be asked about.

* **§7's "invariantes desconocidas": this module does not promise "everything
  else identical".** A property the caller asked to preserve that no known
  invariant covers comes back as a `content` invariant with an EMPTY threshold
  and a description saying there is no method for it. It will be reported
  `unknown` and never `preserved`, which is rule 1 of `contracts.py` reaching
  all the way back into the contract that was frozen before the run.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from src.delta_engine.classification import covers
from src.delta_engine.contracts import (
    DOMAINS,
    DeltaError,
    Invariant,
    RequestedChange,
    ScopeRule,
    strongest_severity,
)

__all__ = [
    "DEFAULT_PROFILE",
    "MANDATORY_CLASSES",
    "TRIMMING_PROFILES",
    "SOURCE_STRENGTH",
    "SECURITY_INVARIANTS",
    "DOMAIN_DEFAULTS",
    "UNKNOWN_PREFIX",
    "resolve",
    "defaults_for",
    "merge",
    "describe",
    "is_unknown",
]

#: The id prefix `_unknown_invariants` gives a property nobody can check. A
#: NAMED constant rather than a string literal compared in two files: `intent`
#: has to be able to tell these rows apart to list them as confirmations, and a
#: predicate that drifted from the producer would report a checkable invariant
#: as unconfirmable, or worse, stop reporting an unconfirmable one.
UNKNOWN_PREFIX = "unknown."

#: The profile every caller gets when it names none. Same word as
#: `IntentContract.evaluation_profile`'s default, on purpose: two spellings of
#: "the normal one" would make a contract that named nothing and a contract
#: that named the default resolve to different invariant sets.
DEFAULT_PROFILE = "default"

#: The classes no profile may trim. §1.7, as data rather than as a sentence in
#: a docstring somebody edits later.
MANDATORY_CLASSES: Tuple[str, ...] = ("security", "permissions")

#: Profiles that buy fewer checks. `literal` is §1.7's cheapest completion
#: mode: it drops the optional domain defaults and keeps every mandatory class.
#: A profile this module does not recognise is treated as `default` -- the
#: WIDEST set -- because an unrecognised profile name must never be the reason
#: a property went unchecked, and erring towards more invariants can only
#: produce more `unknown` rows, never fewer violations.
TRIMMING_PROFILES: Tuple[str, ...] = ("literal",)

#: Who outranks whom when two sources file the same invariant id. §16 requires
#: a `required` change to be traceable to something other than the executor's
#: own opinion, and this is the same idea applied to the property being held
#: still: an invariant the USER named is not negotiable by a policy, and
#: nothing is negotiable by a domain default.
SOURCE_STRENGTH: Dict[str, int] = {
    "security": 7,
    "request": 6,
    "project_policy": 5,
    "capability": 4,
    "decision": 3,
    "operation": 2,
    "artifact": 1,
    "domain_default": 0,
}


#: The floor under every domain and every profile. `source="security"` is the
#: strongest entry in `SOURCE_STRENGTH`, so nothing can file a weaker version
#: of one of these ids and win, and `_CLASS_SEVERITY_FLOOR` in `contracts.py`
#: already refuses any of them at a severity below `blocking`.
#:
#: Three and not thirty. Each is a question an adapter can be handed for any
#: artifact -- did a credential appear, did a permission widen, did the reach
#: of the thing grow -- and a longer list would be a list of properties nobody
#: checks, which reads as coverage and is not.
SECURITY_INVARIANTS: Tuple[Dict[str, Any], ...] = (
    {
        "id": "security.secrets_not_exposed",
        "class": "security",
        "source": "security",
        "description": (
            "No credential, token or key appears in the target that was not "
            "already in the source. A change that leaks one is blocking "
            "however well it satisfied the request."
        ),
    },
    {
        "id": "security.permissions_not_widened",
        "class": "permissions",
        "source": "security",
        "description": (
            "The target grants no permission the source did not. §25: an "
            "IntentContract cannot widen permissions, so neither can the "
            "change it describes."
        ),
    },
    {
        "id": "security.network_not_widened",
        "class": "security",
        "source": "security",
        "description": (
            "The target reaches no host, port or external service the source "
            "did not reach."
        ),
    },
)

#: Conservative, few, and per domain. The security three above are NOT repeated
#: here for any domain: they apply to all of them, `defaults_for` puts them in
#: front of whatever this table holds, and a second copy under `code` would be
#: an id that two sources file -- which works, because `merge` collapses it,
#: and which would still be a lie about where the requirement comes from.
#:
#: `code` therefore reads as compatibility plus budget, and gets its
#: permissions, network and secrets invariants from `SECURITY_INVARIANTS` like
#: everything else.
DOMAIN_DEFAULTS: Dict[str, Tuple[Dict[str, Any], ...]] = {
    "code": (
        {
            "id": "code.public_symbols_kept",
            "class": "compatibility",
            "description": (
                "No public symbol that existed in the source is missing from "
                "the target. A withdrawn export breaks callers this comparison "
                "cannot see."
            ),
        },
        {
            "id": "code.comparison_budget",
            "class": "budget",
            "description": (
                "The comparison stayed inside its element and byte budget. "
                "Past it the delta describes part of the change, and the part "
                "it did not read is not evidence that nothing happened there."
            ),
        },
    ),
    "document": (
        {
            "id": "document.citations_kept",
            "class": "provenance",
            "description": (
                "Every citation in the source is still present and still "
                "resolves to the same source in the target."
            ),
        },
        {
            "id": "document.figures_kept",
            "class": "content",
            "description": (
                "Numbers the source states are unchanged in the target unless "
                "the request named them. A figure edited in passing is the "
                "single most expensive incidental change a document can carry."
            ),
        },
    ),
    "workflow": (
        {
            "id": "workflow.effects_not_widened",
            "class": "security",
            "description": (
                "The target declares no side effect -- write, spawn, external "
                "call -- that the source did not declare."
            ),
        },
    ),
    "skill": (
        {
            "id": "skill.effects_not_widened",
            "class": "security",
            "description": (
                "The target declares no side effect -- write, spawn, external "
                "call -- that the source did not declare."
            ),
        },
    ),
    "state": (
        {
            "id": "state.provenance_kept",
            "class": "provenance",
            "description": (
                "Every observation in the target still names where it came "
                "from, and no value lost the run that produced it."
            ),
        },
    ),
    "image": (
        {
            "id": "image.format_kept",
            "class": "format",
            "description": "Container, colour depth and aspect ratio are unchanged.",
        },
        {
            "id": "image.framing_kept",
            "class": "format",
            "description": (
                "The framing -- dimensions and crop -- is unchanged. A re-crop "
                "that keeps the aspect ratio changes what the image is of."
            ),
        },
    ),
    "video": (
        {
            "id": "video.format_kept",
            "class": "format",
            "description": "Container, resolution and frame rate are unchanged.",
        },
        {
            "id": "video.duration_kept",
            "class": "format",
            "description": (
                "The duration is unchanged. A cut that shortens the timeline "
                "moves every later timestamp any other system holds."
            ),
        },
    ),
    "audio": (
        {
            "id": "audio.format_kept",
            "class": "format",
            "description": "Container, sample rate and channel count are unchanged.",
        },
        {
            "id": "audio.duration_kept",
            "class": "format",
            "description": "The duration is unchanged.",
        },
    ),
    "binary": (
        {
            "id": "binary.media_type_kept",
            "class": "format",
            "description": (
                "The declared media type is unchanged. It is the only thing a "
                "byte comparer can say about format without reading the bytes."
            ),
        },
    ),
}


_SLUG = re.compile(r"[^a-z0-9]+")

#: The only keys a capability contract may carry into this module. `id` is read
#: for traceability and `invariants` is read for content; anything else is a
#: rejection, because a capability field skipped in silence is a restriction
#: nobody enforced and everybody believed in.
_CAPABILITY_KEYS: Tuple[str, ...] = ("id", "invariants")


def merge(base: Sequence[Invariant], extra: Sequence[Invariant]) -> Tuple[Invariant, ...]:
    """One list out of two, by source strength, and never downwards.

    Two decisions, both of which had a tempting wrong answer:

    * the WINNER is chosen by `SOURCE_STRENGTH`, so a `domain_default` filing
      the id a `request` already filed loses. "Last one wins" would make the
      result depend on the order the caller happened to assemble its lists in.

    * the SEVERITY is the strongest of the two regardless of who won. Choosing
      the winner's severity would let a stronger source lower a weaker one's
      alarm -- a `request` filing `code.public_symbols_kept` as `minor` would
      quietly demote the domain default that had it at `material` -- and §12
      forbids summarising a material row away, so the demotion would be
      invisible in exactly the delta that needed it.

    Order is base first, then the extras that were not already there, so two
    identical inputs produce one identical tuple and `IntentContract.fingerprint`
    recognises the same request twice.
    """
    out: list = []
    index: Dict[str, int] = {}
    for invariant in list(base) + list(extra):
        position = index.get(invariant.id)
        if position is None:
            index[invariant.id] = len(out)
            out.append(invariant)
            continue
        out[position] = _stronger(out[position], invariant)
    return tuple(out)


def _stronger(incumbent: Invariant, challenger: Invariant) -> Invariant:
    """The one that survives a collision on `id`, at the higher severity.

    Rebuilt through `Invariant.parse` rather than `dataclasses.replace` when
    the severity has to rise: `parse` is where the class severity floor lives,
    and a second door into this contract that skips it is how a `security`
    invariant ends up at `info` without anyone writing that down.
    """
    severity = strongest_severity([incumbent.severity, challenger.severity])
    challenger_rank = SOURCE_STRENGTH.get(challenger.source, -1)
    incumbent_rank = SOURCE_STRENGTH.get(incumbent.source, -1)
    winner = challenger if challenger_rank > incumbent_rank else incumbent
    if winner.severity == severity:
        return winner
    payload = dict(winner.to_dict())
    payload["severity"] = severity
    return Invariant.parse(payload, f"invariant[{winner.id}]")


def _parse_all(items: Sequence[Mapping[str, Any]], *, path: str,
               source: str = "") -> Tuple[Invariant, ...]:
    """Parse a list of invariant payloads, optionally forcing their source.

    `source` is forced rather than defaulted where the CALLER's own position in
    `SOURCE_STRENGTH` is known -- an invariant handed to `resolve()` as
    `explicit` came from the request, and letting it keep `Invariant.parse`'s
    `domain_default` would put the user's own instruction at the bottom of the
    strength order, where a project policy overrides it.
    """
    out = []
    for index, item in enumerate(items):
        payload = dict(item)
        if source and not payload.get("source"):
            payload["source"] = source
        out.append(Invariant.parse(payload, f"{path}[{index}]"))
    return tuple(out)


def defaults_for(domain: str, *, profile: str = DEFAULT_PROFILE) -> Tuple[Invariant, ...]:
    """The invariants a domain carries before anybody asks for anything.

    Security first and always: `SECURITY_INVARIANTS` is in front of the domain
    table for every domain and survives every profile, because §1.7 makes the
    completion mode a decision about coverage and never about safety. A
    trimming profile (`literal`) drops the domain's optional defaults and keeps
    every `MANDATORY_CLASSES` entry, whichever table it came from.

    An unknown domain is refused by name rather than answered with the security
    three: `DOMAINS` is closed, and a domain nothing routes on would get a
    plausible-looking invariant set for a comparison no adapter can run.
    """
    name = str(domain or "")
    if name not in DOMAINS:
        raise DeltaError(
            "domain",
            f"is not a domain this system compares; known: {list(DOMAINS)}",
            got=domain)
    mandatory = _parse_all(SECURITY_INVARIANTS, path=f"invariants.security.{name}")
    optional = _parse_all(DOMAIN_DEFAULTS.get(name, ()),
                          path=f"invariants.defaults.{name}",
                          source="domain_default")
    if str(profile or DEFAULT_PROFILE) in TRIMMING_PROFILES:
        optional = tuple(inv for inv in optional if inv.klass in MANDATORY_CLASSES)
    return merge(mandatory, optional)


def _capability_invariants(capability: Optional[Mapping[str, Any]]) -> Tuple[Invariant, ...]:
    """The invariants a capability or workflow contract brings with it. §7.

    Only `invariants` is read, and every other key is REFUSED by name rather
    than ignored. A capability field this module silently skipped would be a
    restriction somebody wrote down and believed was being enforced, and the
    delta would come back clean because nothing ever looked. That is rule 2 of
    `contracts/base.py` -- an unknown key is an error, never a default --
    applied to somebody else's contract.
    """
    if not capability:
        return ()
    if not isinstance(capability, Mapping):
        raise DeltaError("capability", "expected an object", got=capability)
    # `reject_unknown`'s rule, raised as this package's own error type. Every
    # other rejection a caller of `resolve` can meet is a `DeltaError`, and one
    # that arrived as the base `ContractError` instead would slip past an
    # `except DeltaError` written around exactly this call.
    unknown = sorted(key for key in capability if key not in _CAPABILITY_KEYS)
    if unknown:
        raise DeltaError(
            "capability",
            f"carries {unknown}, which this module does not read; a capability "
            f"field silently ignored is a restriction somebody wrote down and "
            f"believed was being enforced. It reads: {list(_CAPABILITY_KEYS)}",
            got=unknown,
        )
    raw = capability.get("invariants") or ()
    if isinstance(raw, Mapping) or isinstance(raw, str):
        raise DeltaError("capability.invariants", "expected a list", got=raw)
    return _parse_all(tuple(raw), path="capability.invariants", source="capability")


def _scope_invariants(scope: Optional[ScopeRule]) -> Tuple[Invariant, ...]:
    """One `scope` invariant per forbidden path. §16's fourth question.

    `classification` already reads `scope.forbidden` to file a change there as
    a regression, and this is not that: a forbidden path that did NOT change
    produces no finding at all, so without an invariant the delta has no row
    saying anybody looked. "We checked `settings.py` and it held" and "nothing
    in the delta mentions `settings.py`" are different answers, and only the
    first one is evidence.

    `allowed` gets no invariant. A bounded scope changes what `incidental`
    MEANS, which is a classification decision `_out_of_scope` already makes per
    finding; turning it into one invariant over the whole comparison would
    produce a single row that is violated by any change anywhere and names
    none of them.
    """
    if scope is None or not scope.forbidden:
        return ()
    payloads = [
        {
            "id": f"scope.forbidden.{_slug(path)}",
            "class": "scope",
            "path": path,
            "source": "request",
            "description": (
                f"{path} is forbidden by the request's scope and must be "
                f"unchanged, even if it sits inside an allowed region."
            ),
        }
        for path in scope.forbidden
    ]
    return _parse_all(payloads, path="scope.forbidden")


def _unknown_invariants(requested: Sequence[RequestedChange],
                        covered: Sequence[Invariant]) -> Tuple[Invariant, ...]:
    """§7's "invariantes desconocidas", as rows instead of as a silence.

    A `preserved` condition on a path no invariant covers comes back as a
    `content` invariant with an EMPTY threshold and a description saying there
    is no method for it. Empty is the load-bearing part: a threshold is what an
    adapter measures against, so an invariant without one cannot be reported
    `preserved` by anything honest, and `InvariantResult.parse` refuses
    `preserved` with no observation anyway. The result is `unknown`, which is
    the correct answer and the one this module refuses to round up.

    Coverage is decided by PATH, using `classification.covers` -- the same
    prefix rule the classifier uses, so an invariant on `character` covers a
    request about `character.pose` here exactly as it does there. An invariant
    with no path covers nothing: it is a statement about the artifact as a
    whole, and reading it as a promise about one named property is how "we
    check the format" becomes "we checked the face".
    """
    out = []
    for change in requested:
        if change.condition != "preserved":
            continue
        if any(inv.path and covers(inv.path, change.path) for inv in covered):
            continue
        out.append({
            "id": f"{UNKNOWN_PREFIX}{_slug(change.path)}",
            "class": "content",
            "path": change.path,
            "source": "request",
            "description": (
                f"The request asks for {change.path} to be preserved and no "
                f"known invariant covers it. There is no method registered to "
                f"check this property, so its result is `unknown`; not "
                f"detected is not preserved."
            ),
        })
    return _parse_all(out, path="invariants.unknown")


def _slug(path: str) -> str:
    """An id fragment out of a domain path. Stable, lowercase, bounded.

    Bounded at 100 characters because `Invariant.id` is capped at 120 by the
    contract and a JSON-pointer-shaped path can be far longer than that; a slug
    that overflows would turn an honest `unknown` row into a parse error the
    caller reads as a bug in the contract.
    """
    cleaned = _SLUG.sub("_", str(path or "").strip().lower()).strip("_")
    return cleaned[:100] or "unnamed"


def resolve(domain: str, *, explicit: Sequence[Mapping[str, Any]] = (),
            requested: Sequence[RequestedChange] = (),
            scope: Optional[ScopeRule] = None,
            profile: str = DEFAULT_PROFILE,
            capability: Optional[Mapping[str, Any]] = None) -> Tuple[Invariant, ...]:
    """Everything this request has to hold still, from every source, merged.

    The order the sources are merged in is the order they are READ in, and it
    is chosen so the result is stable rather than so the strongest wins --
    `merge` decides that, by `SOURCE_STRENGTH`, whichever order they arrive in.
    What the order does decide is the sequence of the returned tuple, and a
    stable sequence is what makes `IntentContract.fingerprint` recognise the
    same request twice and hit the §22 cache.

    The unknown-invariant pass runs LAST, over everything the earlier passes
    produced: a `preserved` request is only unknown once nobody else has
    covered its path, and computing it earlier would file an `unknown` row
    beside the explicit invariant that answers it.

    This function never widens permissions and never invents a threshold. §25:
    "el IntentContract no puede ampliar permisos" -- everything here either
    holds a property still or admits it cannot check one.
    """
    base = defaults_for(domain, profile=profile)
    with_capability = merge(base, _capability_invariants(capability))
    with_scope = merge(with_capability, _scope_invariants(scope))
    with_explicit = merge(with_scope, _parse_all(tuple(explicit),
                                                 path="invariants.explicit",
                                                 source="request"))
    return merge(with_explicit, _unknown_invariants(requested, with_explicit))


def describe(invariant: Invariant) -> str:
    """One line about one invariant, for a card or a log.

    Says the class, the property, where the requirement came from and how
    loudly a violation asks to be read -- and, for an invariant `is_unknown`
    recognises, says that NOTHING here can check it. That last clause is the one
    this function exists for: an interface listing invariants without it shows a
    row that looks like a check being performed, and §7 is precisely the rule
    that "everything else identical" must not be implied.

    A missing threshold on its own does not earn that clause. `security.
    secrets_not_exposed` has none and is perfectly checkable -- an adapter
    answers it yes or no -- and describing it as unmeasurable would cry wolf on
    the row that most needs to be believed when it does fire.
    """
    parts = [f"[{invariant.klass}] {invariant.id}"]
    if invariant.path:
        parts.append(f"on {invariant.path}")
    parts.append(f"({invariant.source}, {invariant.severity})")
    line = " ".join(parts)
    detail = invariant.description or ""
    if is_unknown(invariant):
        detail = (f"{detail} No method is registered for this property, so its "
                  f"result is `unknown` and never `preserved`.").strip()
    elif invariant.threshold:
        measured = ", ".join(f"{key}={invariant.threshold[key]}"
                             for key in sorted(invariant.threshold))
        detail = f"{detail} Measured against {measured}.".strip()
    else:
        detail = (f"{detail} No numeric threshold: the domain's adapter answers "
                  f"it as held or violated.").strip()
    return f"{line}: {detail}"


def is_unknown(invariant: Invariant) -> bool:
    """Whether this is a property §7 admits nothing here can check.

    The two conditions together, never either alone: the id prefix says this
    module produced it as an unknown, and the empty threshold says nothing has
    since given it a way to be measured. Checking only the prefix would keep
    calling an invariant unconfirmable after somebody attached a threshold to
    it; checking only the threshold would sweep in every security default,
    which is checked by an adapter answering yes or no and needs no threshold
    at all.
    """
    return invariant.id.startswith(UNKNOWN_PREFIX) and not invariant.threshold
