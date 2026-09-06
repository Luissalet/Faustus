"""Sections 1.3 and 1.8: the bounded, permission-checked answer that leaves.

A `StateProjection` is not a dump. A consumer -- Context Engine, first --
names the entities and the fields it needs and the risk it is about to take,
and gets back those fields with their freshness, the observations behind them,
the conflicts that touch them and, for anything not fresh enough, the actions
that would revalidate it. Everything else about those entities stays here.

**The field naming scheme is `"<entity_id>#<field>"`, and there is one of it.**
The alternative the plan floats, `"<entity_kind>.<field>"`, cannot survive a
projection over two runs: both would write `run.status`, one would win, and the
consumer would be handed a value with no way to tell which run it came from --
a silent collision in exactly the payload that gets acted on. So the entity id
is part of every key, and `#` separates it because `contracts._ENTITY_ID_RE`
allows `.`, `:` and `/` inside an identifier and forbids `#`, which makes the
split unambiguous rather than merely usual.

A caller may also pass a BARE field name as a selector. It is a shorthand, not
a second scheme: `"status"` means "this field on every entity in
`entity_refs` whose schema declares it", and it EXPANDS into canonical keys
before anything is built. Every key in the answer, in `unknown_fields` and in
every refresh action is `<entity_id>#<field>`. An empty `fields` means every
field of every entity named.

**Nothing is presented that the owner may not see.** An entity ref that is not
this owner's is dropped, silently and identically to one that does not exist,
and it does not appear in the returned `entity_refs` either: the refs on the
answer describe what is IN it, not what was asked for. A consumer that could
tell "not yours" from "not there" by reading the echo would have a listing of
somebody else's machine.

**Nothing here may carry a secret.** State values are not credentials by
design -- no adapter writes one -- but "by design" is a claim about every
adapter ever written and about every one that will be. The assembled fields go
through `src/contracts/event.py::Event.redact`, the same structural redactor
the event stream uses, and a field whose value was blanked says so. It fails
CLOSED: a payload that could not be scrubbed loses its value and keeps its
metadata, because "we could not check this for secrets" is not a reason to
send it.

**`token_budget` drops the LEAST fresh first, and names what it dropped.** A
projection that quietly fitted itself into a budget would be a projection whose
consumer thinks it saw everything. Each dropped field leaves a refresh action
behind carrying `reason: "token_budget"`, so the consumer knows the field
exists, knows why it is not here and knows how to ask for it.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Mapping, Sequence, Tuple

from src.contracts.base import fingerprint, now_iso
from src.state_mirror import freshness as _freshness
from src.state_mirror import queries as _queries
from src.state_mirror.contracts import (
    MINIMUM_FRESHNESS_LEVELS,
    REAL_NAMESPACE,
    FRESHNESS_RATINGS,
    MaterializedState,
    StateProjection,
    schema_fields,
)

logger = logging.getLogger(__name__)

__all__ = ["FIELD_SEPARATOR", "CHARS_PER_TOKEN", "MAX_FIELDS", "project"]

#: What separates the entity from the field in a projection key. `#` because
#: `contracts` lets an identifier carry `.`, `:`, `/`, `@`, `+`, `~` and `-`
#: and never `#`, so splitting on it cannot cut an entity id in half.
FIELD_SEPARATOR = "#"

#: The crude estimate `token_budget` is measured in. Four characters to a token
#: is the usual English approximation and it is deliberately not a tokeniser
#: call: a projection is assembled on a read path, loading a tokeniser to bound
#: a payload would cost more than the payload, and the budget is a ceiling
#: rather than an accounting figure. It over-counts JSON punctuation, which
#: errs towards sending less than the budget allows.
CHARS_PER_TOKEN = 4

#: The most fields one projection may carry, whatever was asked for. The
#: contract's own caps on `unknown_fields` (256) and `source_observation_ids`
#: (512) would refuse a bigger one anyway; refusing it here means the answer is
#: trimmed and SAID to be trimmed rather than raising out of a read.
MAX_FIELDS = 256

#: Worst first. Built from `FRESHNESS_RATINGS`, which is ordered best to worst,
#: so the two cannot disagree about which rating is the least valuable.
_FRESHNESS_RANK: Dict[str, int] = {name: i for i, name in enumerate(FRESHNESS_RATINGS)}


def key_for(entity_id: str, field: str) -> str:
    """The one spelling of a projection key. Used on the way in and out."""
    return f"{entity_id}{FIELD_SEPARATOR}{field}"


def _split(selector: str) -> Tuple[str, str]:
    """A selector as `(entity_id, field)`; `("", field)` for the shorthand."""
    raw = str(selector or "").strip()
    if FIELD_SEPARATOR not in raw:
        return "", raw
    entity_id, _, field = raw.partition(FIELD_SEPARATOR)
    return entity_id.strip(), field.strip()


def _scrub(name: str, value: Any) -> Tuple[Any, int]:
    """One field's VALUE through the envelope's redactor. Fails closed.

    The value is handed over under its own field name rather than under a key
    called `value`, and that is the whole point: `Event.redact` blanks
    secret-looking KEYS as well as secret-looking values, so a field named
    `api_key` -- which no schema declares today and which is exactly what a
    future one might -- is blanked by the same pass that blanks a credential
    found inside a string. Scrubbing it under a generic key would silently
    disable half the redactor.

    Fails closed: a value that could not be scrubbed is dropped and the drop is
    stated. "We could not check this for secrets" is not a reason to send it.
    """
    try:
        from src.contracts.event import Event

        scrubbed = Event(name="state", data={str(name): value}).redact(())
        return dict(scrubbed.data).get(str(name)), int(scrubbed.redactions)
    except Exception as exc:  # noqa: BLE001 - a read path, and it fails closed
        logger.warning("state projection: redaction unavailable, the value of "
                       "%s is dropped: %s", name, exc)
        return None, -1


def _tokens(meta: Mapping[str, Any]) -> int:
    """A field's cost against `token_budget`, over-counted rather than under."""
    try:
        body = json.dumps(meta, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001 - an unmeasurable field is an expensive one
        return MAX_FIELDS
    return max(1, len(body) // CHARS_PER_TOKEN)


def _selected(states: Sequence[MaterializedState],
              fields: Sequence[str]) -> List[Tuple[MaterializedState, str]]:
    """Every (state, field) the selectors ask for, in a stable order.

    An empty selector list means every declared field of every entity. A
    selector naming an entity that is not in this projection is dropped: it is
    either somebody else's or it does not exist, and those answer alike.

    Only fields the schema DECLARES are selectable. A selector for something
    outside the schema is not a field nobody has observed, it is not a field at
    all, and answering `unknown` for it would say the mirror might one day know
    it.
    """
    wanted = [str(f or "").strip() for f in (fields or ()) if str(f or "").strip()]
    out: List[Tuple[MaterializedState, str]] = []
    seen: set = set()
    for state in states:
        declared = schema_fields(state.schema)
        if not wanted:
            names = list(declared)
        else:
            names = []
            for selector in wanted:
                target, name = _split(selector)
                if target and target != state.entity_id:
                    continue
                if name in declared and name not in names:
                    names.append(name)
        for name in names:
            key = key_for(state.entity_id, name)
            if key in seen:
                continue
            seen.add(key)
            out.append((state, name))
    return out


def _level(value: Any) -> str:
    """A risk level from `MINIMUM_FRESHNESS_LEVELS`, failing closed.

    A level this build does not know becomes `action_safe`, the strictest, and
    is logged. The alternative -- defaulting to `informational` -- would widen
    what counts as fresh enough on a typo, invisibly, in exactly the argument a
    consumer passes when it is about to overwrite something.
    """
    name = str(value or "").strip()
    if name in MINIMUM_FRESHNESS_LEVELS:
        return name
    logger.warning("state projection: %r is not a freshness level; treated as "
                   "action_safe. The levels are %s", value,
                   list(MINIMUM_FRESHNESS_LEVELS))
    return "action_safe"


def _stamp(now: Any) -> str:
    """`now` as an ISO string, whatever it arrived as."""
    if isinstance(now, str) and now.strip():
        return now.strip()
    if now is None:
        return now_iso()
    try:
        return _freshness._now(now).isoformat().replace("+00:00", "Z")
    except Exception:  # noqa: BLE001 - a clock that will not format is not fatal
        return now_iso()


def _resolve(entity_refs: Sequence[str], owner: str, namespace: str,
             store: Any, now: Any) -> List[MaterializedState]:
    """The states this owner may see, re-rated, in the order they were asked for.

    `queries.get` does the owner check and the re-rating, so a projection can
    never be built from a stored rating -- which is the whole point of asking
    for one before an effect.
    """
    wanted = str(namespace or REAL_NAMESPACE)
    out: List[MaterializedState] = []
    seen: set = set()
    for raw in entity_refs or ():
        ref = str(raw or "").strip()
        if not ref or ref in seen:
            continue
        seen.add(ref)
        state = _queries.get(ref, owner=owner, store=store, now=now)
        if state is None or state.namespace != wanted:
            continue
        out.append(state)
    return out


def _fit(built: Dict[str, Dict[str, Any]], budget: int
         ) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """Trim to `budget` tokens and `MAX_FIELDS`, worst first. `(kept, dropped)`.

    Least fresh first, oldest first within a rating, then by key so two
    projections over the same store drop the same fields. Dropping the freshest
    would leave a consumer holding only the values it must not act on, which is
    the opposite of what a budget is for.

    A budget of zero or less is unbounded, and `MAX_FIELDS` still applies: the
    contract will not accept more than that and a projection refused on the way
    out is worse than one that says what it left behind.
    """
    order = sorted(
        built.items(),
        key=lambda kv: (
            -_FRESHNESS_RANK.get(str(kv[1].get("freshness") or "unknown"),
                                 len(FRESHNESS_RATINGS)),
            str(kv[1].get("observed_at") or ""),
            kv[0],
        ))
    kept = dict(built)
    dropped: List[str] = []
    ceiling = max(0, int(budget or 0))
    total = sum(_tokens(meta) for meta in kept.values())
    for key, meta in order:
        over_budget = ceiling and total > ceiling
        if not over_budget and len(kept) <= MAX_FIELDS:
            break
        kept.pop(key, None)
        dropped.append(key)
        total -= _tokens(meta)
    return kept, dropped


def _revision(states: Sequence[MaterializedState]) -> str:
    """The token Delta Engine compares (section 1.3's `revision`).

    One entity gets its own `state:<id>@<n>`, which is readable and is what
    `MaterializedState.revision_ref` mints. Several get a digest of every
    (entity, revision) pair, because the contract bounds this field at 512
    characters and a projection over forty entities would not fit -- and a
    truncated revision token is worse than a hashed one: the comparison is for
    equality, so a digest loses nothing, and a truncation would make two
    different projections compare equal.
    """
    if not states:
        return ""
    if len(states) == 1:
        return states[0].revision_ref()
    return "states:" + fingerprint(
        [(s.entity_id, str(s.revision)) for s in states])


def project(*, owner: str, entity_refs: Sequence[str], fields: Sequence[str],
            minimum_freshness: str, project_id: str = "",
            namespace: str = REAL_NAMESPACE, token_budget: int = 0,
            reason: str = "", store: Any = None,
            now: Any = None) -> StateProjection:
    """Assemble what one consumer asked for, and nothing else. Never raises.

    `reason` is why the consumer wants it. It is not stored -- `StateProjection`
    has no field for it and widening a contract this file does not own would be
    the worse of the two mistakes -- and it is logged with the projection it
    produced, which is what makes "who asked for this and why" answerable after
    the fact.
    """
    holder = str(owner or "")
    level = _level(minimum_freshness)
    states = _resolve(entity_refs, holder, namespace, store, now)
    unseen: Dict[str, set] = {s.entity_id: set(s.unknown_fields()) for s in states}

    built: Dict[str, Dict[str, Any]] = {}
    unknown: List[str] = []
    actions: List[Dict[str, Any]] = []
    observation_ids: List[str] = []

    for state, name in _selected(states, fields):
        key = key_for(state.entity_id, name)
        held = state.fields.get(name)
        if held is None or name in unseen.get(state.entity_id, set()):
            # Never observed. Part of the output and never a silent omission
            # (section 1.3): a consumer that cannot tell "false" from "nobody
            # looked" will eventually act on the difference.
            unknown.append(key)
            action = _freshness.refresh_action(state.schema, name,
                                               source=held.source if held else "",
                                               entity_id=state.entity_id)
            action["key"] = key
            action["reason"] = "never_observed"
            actions.append(action)
            continue
        value, redactions = _scrub(name, held.value)
        meta: Dict[str, Any] = {
            "entity_id": state.entity_id,
            "schema": state.schema,
            "field": name,
            "value": value,
            "epistemic": held.epistemic,
            "freshness": held.freshness,
            "observed_at": held.observed_at,
            "source": held.source,
            "ttl_seconds": held.ttl_seconds,
            "trusted": held.trusted(),
            "revision": state.revision,
        }
        if redactions > 0:
            meta["redactions"] = int(redactions)
        elif redactions < 0:
            meta["redaction_failed"] = True
        built[key] = meta
        if held.observation_id:
            observation_ids.append(held.observation_id)
        if not _freshness.meets(held.freshness, level):
            action = _freshness.refresh_action(state.schema, name,
                                               source=held.source,
                                               entity_id=state.entity_id)
            action["key"] = key
            action["reason"] = "below_minimum_freshness"
            action["freshness"] = held.freshness
            action["minimum_freshness"] = level
            actions.append(action)

    built, dropped = _fit(built, token_budget)
    for key in dropped:
        entity_id, _, name = key.partition(FIELD_SEPARATOR)
        schema = next((s.schema for s in states if s.entity_id == entity_id), "")
        action = _freshness.refresh_action(schema, name, entity_id=entity_id)
        action["key"] = key
        action["reason"] = "token_budget"
        actions.append(action)
    if dropped:
        logger.info("state projection: %d field(s) dropped to fit %s tokens; "
                    "the least fresh went first and each one is named in "
                    "refresh_actions", len(dropped), token_budget)

    return _build(holder, states, built, unknown, actions, observation_ids,
                  level=level, project_id=project_id, namespace=namespace,
                  store=store, now=now, reason=reason)


def _build(owner: str, states: Sequence[MaterializedState],
           built: Mapping[str, Mapping[str, Any]], unknown: Sequence[str],
           actions: Sequence[Mapping[str, Any]],
           observation_ids: Sequence[str], *, level: str, project_id: str,
           namespace: str, store: Any, now: Any, reason: str) -> StateProjection:
    """Put the envelope round it, inside the contract's own caps.

    Every list here is capped and deduplicated BEFORE `StateProjection.parse`
    sees it, because that parser refuses an over-long list by raising and this
    is a read path. A trimmed projection that says it is trimmed is an answer;
    an exception out of a read is a page that will not load.
    """
    open_conflicts: List[str] = []
    for state in states:
        for conflict in _queries.conflicts(owner=owner, entity_id=state.entity_id,
                                           open_only=True, limit=128, store=store):
            if conflict.id not in open_conflicts:
                open_conflicts.append(conflict.id)

    ratings = [str(meta.get("freshness") or "unknown") for meta in built.values()]
    payload = {
        "owner": owner,
        "project_id": str(project_id or ""),
        "namespace": str(namespace or REAL_NAMESPACE),
        "as_of": _stamp(now),
        "minimum_freshness": level,
        # What is IN the projection, not what was asked for: an entity the
        # caller may not see must not be echoed back, or the echo becomes a
        # listing of somebody else's machine.
        "entity_refs": [s.entity_id for s in states][:256],
        "fields": {k: dict(v) for k, v in built.items()},
        "source_observation_ids": list(dict.fromkeys(observation_ids))[:512],
        "freshness": _freshness.combined(ratings),
        # `unknown_fields` and `conflicts` are bounded at 128 characters an
        # item by the contract. A key longer than that is dropped rather than
        # truncated: a truncated entity id names a different entity.
        "unknown_fields": [k for k in dict.fromkeys(unknown) if len(k) <= 128][:256],
        "conflicts": [c for c in open_conflicts if len(c) <= 128][:128],
        "refresh_actions": [dict(a) for a in actions][:256],
        "revision": _revision(states),
    }
    try:
        projection = StateProjection.parse(payload)
    except Exception as exc:  # noqa: BLE001 - a read path
        logger.exception("state projection: the assembled projection was "
                         "refused by its own contract (%s); answering an empty "
                         "one, which is not sufficient() for anything", exc)
        return StateProjection(owner=owner, project_id=str(project_id or ""),
                               namespace=str(namespace or REAL_NAMESPACE),
                               as_of=_stamp(now), minimum_freshness=level)
    logger.debug("state projection %s: %d field(s) over %d entity(ies) at %s "
                 "for %r", projection.id, len(projection.fields),
                 len(projection.entity_refs), projection.freshness,
                 reason or "an unnamed consumer")
    return projection
