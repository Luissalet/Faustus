"""Two State Mirror revisions: what moved in the world, and what we learned.

Section 15. `state_mirror/events.py` says it in one line -- "the revision is
what Delta Engine compares" -- and `MaterializedState.revision` is a counter
that only advances when a reduction CHANGED something, so a revision that moved
is a promise that a value did. This adapter is what turns that promise into
four different sentences, because §15 requires a `StateDelta` to tell them
apart and collapsing them is what fills a consumer with noise:

* **A change in the world.** Same source, a newer observation, a different
  value: `modified`, and nothing about it is hedged. The probe that said
  `running` yesterday says `completed` today.

* **A change in what we know.** The value moved because a STRONGER source
  spoke -- `epistemic_rank` fell, an inference replaced by an observation. Also
  `modified`, and the `detail` says out loud that what changed is what we know.
  An inference that a probe overturned is not the world moving, and confidence
  is bounded at `medium` because nothing here can say whether it moved as well.

* **A datum that merely aged.** The same value whose `freshness` fell to
  `stale`. `unchanged`, with the decay named as a limitation. It is NOT a
  change: ageing is a change in what the state is worth, not in what it says,
  and reporting it as a delta would bury every real one under a tide of
  timestamps. `state_mirror/queries.py` makes the same distinction from the
  other side -- "ageing is not a change to the state".

* **A correction.** The same source, the same instant or an earlier one, a
  different value. `modified` with the limitation `correction` and confidence
  dropped to `low`: a source that contradicts itself about one moment is not a
  reliable witness about that field, and every later conclusion resting on it
  inherits that.

Two more rules, each with the failure it prevents:

* **An open conflict is `unknown`, never `preserved`.** The mirror records a
  disagreement instead of resolving it (§9.2: "the reducer builds one of these
  instead of picking a winner"), and this adapter is not the one to settle it.
  Every invariant over an entity carrying an open conflict comes back `unknown`
  with the conflict named, and the field rows carry the same sentence.

* **The mirror is a projection and the canonical source is outside it.** Rule 4
  of `state_mirror/contracts.py`. Everything here compares two READ MODELS, so
  the strongest honest claim is about what the mirror believed at two moments;
  before an effect that matters, the caller revalidates against the system that
  owns the entity, and `refresh_actions` is where the mirror says how.

WHAT REACHES THIS ADAPTER. `sources.resolve` is the only door, and today it
resolves `literal` -- a mapping the caller has in hand -- while a `state`
revision comes back `readable=False` with that module's own reason: "a State
Mirror revision is a row and not a byte stream; the state adapter brings its
own observation of it". `sources.py` is not this module's to change, so the
refusal is reported rather than routed around. Three mapping shapes are read:
one `MaterializedState.to_dict()`, several of them (under `states`, or keyed by
entity id), and a `StateProjection.to_dict()`. Anything else is
`readable=False` naming the three, because an empty snapshot would compare
against a real revision as though every entity had been retired.

The freshness compared is the STORED rating, not one re-derived against the
clock now. `queries.py` re-rates on every read and is right to; a delta must
not, because a comparison whose answer depends on when it ran cannot be cached
by §22's key and would report two different things about one pair of revisions.

`registry.py` finds this module by the module-level `ADAPTER_FACTORY` at the
bottom. There is no list to add it to.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from src.delta_engine import confidence as confidence_mod
from src.delta_engine import coverage as coverage_mod
from src.delta_engine import sources
from src.delta_engine.adapters.base import (
    Element,
    Extraction,
    Finding,
    Scope,
    Snapshot,
    unreadable,
)
from src.delta_engine.contracts import (
    Coverage,
    IntentContract,
    InvariantResult,
    RevisionRef,
)
from src.state_mirror.contracts import (
    FRESHNESS_RATINGS,
    FieldState,
    MaterializedState,
    StateError,
    StateProjection,
    epistemic_rank,
    parse_entity_id,
)

logger = logging.getLogger(__name__)

__all__ = [
    "StateAdapter",
    "ADAPTER_FACTORY",
    "PROVENANCE_INVARIANT",
    "FIELD_SEPARATOR",
    "WORLD_CHANGE",
    "KNOWLEDGE_CHANGE",
    "CORRECTION",
    "DECAYED",
    "OPEN_CONFLICT",
    "DISAGREEMENT",
    "UNDATED",
]


# -- the invariant this adapter can be asked about -------------------------

#: The one id `invariants.DOMAIN_DEFAULTS["state"]` declares for this domain:
#: "Every observation in the target still names where it came from, and no
#: value lost the run that produced it." Class `provenance`, whose severity
#: floor is `material`. Written as a constant and asserted against the
#: catalogue in `tests/test_delta_engine_state.py`, so a rename there fails a
#: test here rather than producing rows that point at nothing.
PROVENANCE_INVARIANT = "state.provenance_kept"

#: What separates the entity from the field in an element address, and the same
#: character `state_mirror/projection.py::FIELD_SEPARATOR` uses -- `#` is the
#: one thing `contracts._ENTITY_ID_RE` forbids inside an identifier, which is
#: what makes the split unambiguous rather than merely usual.
#:
#: NOT imported from that module on purpose, and this is the one duplication in
#: this file: `projection` imports `queries`, which imports `persistence`, so
#: importing it here would drag the State Mirror's whole store into every
#: `registry.discover()` -- and a store that cannot open its database would take
#: this adapter out of the registry for a reason that has nothing to do with
#: comparing two mappings.
FIELD_SEPARATOR = "#"

#: The four §15 sentences, as labels a consumer can match on without reading
#: prose that will be reworded. They appear in `Finding.limitations` (and, for
#: the three that are changes, at the head of `Finding.detail`), so a caller can
#: filter "show me only what actually moved" without a regex over English.
WORLD_CHANGE = "world_change"
KNOWLEDGE_CHANGE = "knowledge_change"
CORRECTION = "correction"
DECAYED = "decayed"
OPEN_CONFLICT = "open_conflict"
DISAGREEMENT = "source_disagreement"

#: The fifth label, and the one §15 does not name because it is not a kind of
#: change: two values from one source that nobody can put in order, because at
#: least one observation carries no readable `observed_at`. It is reported as
#: itself rather than folded into `correction` -- a correction is a source
#: contradicting itself about a KNOWN instant, and calling an undated pair one
#: would accuse a source of something nobody measured.
UNDATED = "undated_observation"

#: How many element keys a coverage lists. `Coverage.regions_analyzed` accepts
#: 1024; the cut is made below it and REPORTED, because a list silently clipped
#: at the contract's ceiling reads as the complete set of regions examined.
MAX_LISTED_REGIONS = 512

#: How long a rendered value may be. `Element.value` is a SHORT rendering, and a
#: state value that needs more than this is one the mirror should not be
#: holding -- §5 forbids arbitrary blobs as state.
MAX_VALUE_CHARS = 300


# -- small helpers ---------------------------------------------------------


def _short(value: Any, limit: int = 480) -> str:
    """One line, bounded. The contracts cap observations and limitations at 500."""
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _unique(values: Sequence[Any]) -> Tuple[str, ...]:
    """Order-preserving de-duplication for lists this module CONCATENATES."""
    seen: set = set()
    out: List[str] = []
    for value in values:
        text = str(value)
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return tuple(out)


def _render(value: Any) -> str:
    """A state value as a short, stable string.

    Canonical JSON with sorted keys, so a mapping whose keys were written in a
    different order is the same value -- the mirror stores what an adapter
    handed it and two probes need not agree on key order. `None` renders as
    `null` and NOT as `""`: `_flag`'s lesson in `state_mirror/contracts.py` is
    that "not observed" and "observed as empty" are different facts, and a
    rendering that collapsed them would report one as the other.
    """
    try:
        rendered = json.dumps(value, sort_keys=True, separators=(",", ":"),
                              ensure_ascii=False, default=str)
    except (TypeError, ValueError):  # pragma: no cover - `default=str` covers it
        rendered = str(value)
    return rendered[:MAX_VALUE_CHARS]


def _freshness_rank(value: Any) -> int:
    """Position in `FRESHNESS_RATINGS`; an unrecognised word ranks last (worst).

    `FRESHNESS_RATINGS` is ordered best to worst (`fresh, aging, stale,
    unknown`), so "the rating got worse" is "the index went up". Ranking an
    unrecognised word as the worst rather than raising is the same decision
    `contracts.confidence_rank` makes: this runs while folding many fields, and
    a delta that died on one typo is worse than one that under-claims.
    """
    name = str(value or "").strip()
    if name in FRESHNESS_RATINGS:
        return FRESHNESS_RATINGS.index(name)
    return len(FRESHNESS_RATINGS)


def _weaken(base: str, limitations: Sequence[str]) -> str:
    """`base`, lowered by what this row could not settle. One place, one rule.

    `confidence.propagate` owns the first half -- a named limitation caps at
    `high`, because "a limitation removes identity, it does not turn a direct
    measurement into a guess" -- and this adds the second: an OPEN CONFLICT
    pulls it to `medium` as well. A field the mirror is holding two claims about
    is not something a comparison of one of them can be `high` about, and doing
    the fold here rather than at three call sites is what stops the three from
    drifting apart.
    """
    weakened = confidence_mod.propagate(base, limitations=tuple(limitations))
    if any(str(item).startswith(OPEN_CONFLICT) for item in limitations):
        weakened = confidence_mod.combine(weakened, "medium")
    return weakened


def _kind_of(entity_id: str) -> str:
    """The entity kind an id names, or `""` when it is not an id.

    `parse_entity_id` is the mirror's own reader and raises on a malformed id;
    this is the total wrapper an element builder needs, because a kind is
    decoration on the element and a bad id has already been refused by
    `MaterializedState.parse` where it mattered.
    """
    try:
        return parse_entity_id(entity_id)["kind"]
    except StateError:
        return ""


# -- reading one revision --------------------------------------------------


@dataclass(frozen=True)
class _Field:
    """One field of one entity, with the four things that qualify it.

    The same four `FieldState` insists on -- value, epistemology, time and
    source -- plus the freshness rating the mirror stored, because §15's four
    sentences are decided by exactly those and nothing else. A field that
    reached this adapter without them would be, in the mirror's own words, "a
    rumour with good posture", and the classification below would be a guess.
    """

    entity_id: str
    name: str
    value: Any = None
    epistemic: str = "unknown"
    freshness: str = "unknown"
    observed_at: str = ""
    source: str = ""
    schema: str = ""

    @property
    def key(self) -> str:
        return f"field:{self.entity_id}{FIELD_SEPARATOR}{self.name}"


@dataclass(frozen=True)
class _EntityRead:
    """One entity as this revision holds it.

    `revision` is `None` for a projection, which carries one revision token for
    the whole envelope rather than a counter per entity. `None` and `0` are not
    interchangeable: the first says this shape has no per-entity counter, the
    second says the entity has never been reduced.
    """

    entity_id: str
    schema: str = ""
    revision: Optional[int] = None
    conflicts: Tuple[str, ...] = ()
    fields: Tuple[_Field, ...] = ()


def _from_field_state(entity_id: str, name: str, held: FieldState,
                      schema: str) -> _Field:
    """One `_Field` out of the mirror's own `FieldState`. No reinterpretation."""
    return _Field(entity_id=entity_id, name=name, value=held.value,
                  epistemic=held.epistemic, freshness=held.freshness,
                  observed_at=held.observed_at, source=held.source, schema=schema)


def _from_meta(entity_id: str, name: str, meta: Mapping[str, Any]) -> _Field:
    """One `_Field` out of a projection's field meta.

    `projection.project()` writes `entity_id`, `schema`, `field`, `value`,
    `epistemic`, `freshness`, `observed_at`, `source`, `ttl_seconds`, `trusted`
    and `revision`; the six read here are the ones §15 decides on. A meta whose
    value was redacted carries `redactions` and a blanked value, and that is
    compared as the value it is -- a projection that lost a value to the
    redactor and one that never had it are different rows in the mirror, and
    this adapter is not the place to merge them.
    """
    return _Field(
        entity_id=entity_id, name=name, value=meta.get("value"),
        epistemic=str(meta.get("epistemic") or "unknown"),
        freshness=str(meta.get("freshness") or "unknown"),
        observed_at=str(meta.get("observed_at") or ""),
        source=str(meta.get("source") or ""),
        schema=str(meta.get("schema") or ""),
    )


def _read_materialized(raw: Any, path: str) -> Tuple[Optional[_EntityRead], str]:
    """One `MaterializedState`, through the mirror's own parser. `(read, note)`.

    Through `MaterializedState.parse` and never around it: that parser is where
    a field outside the schema is refused, where the entity id is checked
    against its four parts, and where an epistemology outside `EPISTEMICS` is
    rejected. An adapter that read the mapping directly would compare fields the
    mirror would never have stored.
    """
    try:
        state = MaterializedState.parse(raw, path)
    except StateError as exc:
        return None, _short(f"{path} is not a MaterializedState this build "
                            f"accepts: {exc}")
    fields = tuple(
        _from_field_state(state.entity_id, name, held, state.schema)
        for name, held in sorted(state.fields.items())
    )
    return _EntityRead(entity_id=state.entity_id, schema=state.schema,
                       revision=state.revision, conflicts=tuple(state.conflicts),
                       fields=fields), ""


def _read_projection(payload: Mapping[str, Any]) -> Tuple[Tuple[_EntityRead, ...],
                                                          Tuple[str, ...], str]:
    """A `StateProjection`, grouped back into entities. `(entities, notes, reason)`.

    A projection is already keyed `<entity_id>#<field>`, so the grouping is a
    partition on the separator and not a guess. A key with no separator is
    dropped WITH a note rather than filed under an empty entity: an element
    addressed at `field:#status` would align against anything else that lost its
    entity, and one silent mis-alignment produces a confident assertion about a
    path that never held that value.
    """
    try:
        projection = StateProjection.parse(payload, "projection")
    except StateError as exc:
        return (), (), _short(f"this payload is shaped like a StateProjection and "
                              f"is not one: {exc}")
    grouped: Dict[str, List[_Field]] = {}
    notes: List[str] = []
    for key, meta in sorted(projection.fields.items()):
        entity_id, separator, name = str(key).partition(FIELD_SEPARATOR)
        if not separator or not entity_id or not name:
            notes.append(_short(f"the projection key {key!r} is not "
                                f"`<entity_id>#<field>` and was not addressed"))
            continue
        grouped.setdefault(entity_id, []).append(_from_meta(entity_id, name, meta))
    conflicts = tuple(projection.conflicts)
    entities = tuple(
        # Every conflict the envelope names is attached to every entity in it:
        # `StateProjection.conflicts` is a list of ids and says which ENTITY it
        # belongs to nowhere, so narrowing it to one would be an invention. The
        # cost -- a conflict on one entity suppresses `preserved` on all of
        # them -- is the safe direction, and it is named in the limitation.
        _EntityRead(entity_id=entity_id, schema=(fields[0].schema if fields else ""),
                    revision=None, conflicts=conflicts, fields=tuple(fields))
        for entity_id, fields in sorted(grouped.items())
    )
    if not entities:
        return (), tuple(notes), ("this projection addresses no "
                                  "`<entity_id>#<field>` key, so there is "
                                  "nothing to compare")
    return entities, tuple(_unique(notes)), ""


def _read(payload: Mapping[str, Any]) -> Tuple[Tuple[_EntityRead, ...],
                                               Tuple[str, ...], str]:
    """A revision as entities. `(entities, notes, reason)`; a reason is a refusal.

    The three shapes §15 names, discriminated on keys the contracts themselves
    write and in the order that cannot confuse them: a `MaterializedState` has
    an `entity_id`, a collection has `states`, a `StateProjection` has `fields`
    plus the envelope's own keys, and a bare mapping of entity id to state is
    the fourth spelling the same collection reaches this adapter in.

    A payload matching none of them is REFUSED with the four named. Guessing
    would produce an empty snapshot, and an empty snapshot compared against a
    real revision reports every entity as retired -- the most expensive wrong
    answer this adapter could give.
    """
    notes: List[str] = []
    if "entity_id" in payload and isinstance(payload.get("fields"), Mapping):
        read, note = _read_materialized(payload, "state")
        if read is None:
            return (), (), note
        return (read,), (), ""

    raw_states = payload.get("states")
    if isinstance(raw_states, (list, tuple)):
        reads: List[_EntityRead] = []
        for index, item in enumerate(raw_states):
            read, note = _read_materialized(item, f"states[{index}]")
            if read is None:
                notes.append(note)
                continue
            reads.append(read)
        if not reads:
            return (), tuple(notes), ("no entry under `states` is a "
                                      "MaterializedState this build accepts")
        return tuple(sorted(reads, key=lambda item: item.entity_id)), \
            tuple(_unique(notes)), ""

    if isinstance(payload.get("fields"), Mapping) and any(
            key in payload for key in ("entity_refs", "projection_id",
                                       "minimum_freshness", "as_of")):
        return _read_projection(payload)

    if payload and all(isinstance(value, Mapping) and "fields" in value
                       for value in payload.values()):
        reads = []
        for entity_id, item in sorted(payload.items()):
            body = dict(item)
            body.setdefault("entity_id", entity_id)
            read, note = _read_materialized(body, f"states[{entity_id}]")
            if read is None:
                notes.append(note)
                continue
            reads.append(read)
        if reads:
            return tuple(reads), tuple(_unique(notes)), ""

    return (), tuple(_unique(notes)), (
        "this payload is none of the shapes a State Mirror revision arrives in: "
        "one `MaterializedState.to_dict()`, several under `states` or keyed by "
        "entity id, or a `StateProjection.to_dict()`")


# -- addressing ------------------------------------------------------------


def _elements(entities: Sequence[_EntityRead]) -> Tuple[List[Element], List[Element]]:
    """`(anchors, rest)` for one revision. §15's two element kinds.

    `entity:` is the anchor and `_budgeted` never drops one: it carries the
    revision -- the token `state_mirror/events.py` says Delta Engine compares --
    and the open conflicts every invariant row reads. A budget that removed one
    would make an entity carrying a conflict indistinguishable from an entity
    nobody disagreed about.
    """
    anchors: List[Element] = []
    rest: List[Element] = []
    for entity in sorted(entities, key=lambda item: item.entity_id):
        anchors.append(Element(
            key=f"entity:{entity.entity_id}",
            kind=_kind_of(entity.entity_id) or "entity",
            # The revision and the count of open conflicts, and nothing that
            # ages: a revision only advances on a material change, so an entity
            # whose fields merely grew stale reads as `unchanged` here, which is
            # §15's third sentence enforced at the level above the field.
            value=(f"revision {entity.revision}" if entity.revision is not None
                   else "revision not carried by this shape")
                  + f", conflicts {len(entity.conflicts)}",
            detail={"schema": entity.schema, "revision": entity.revision,
                    "conflicts": list(entity.conflicts),
                    "fields": len(entity.fields)}))
        for held in entity.fields:
            rest.append(Element(
                key=held.key, kind="field", value=_render(held.value),
                detail={"epistemic": held.epistemic, "observed_at": held.observed_at,
                        "source": held.source, "freshness": held.freshness,
                        "schema": held.schema, "entity_id": held.entity_id}))
    return anchors, rest


def _meta(element: Optional[Element]) -> Dict[str, Any]:
    """The mirror's four qualifiers off one element, or `{}`."""
    return dict(element.detail or {}) if element is not None else {}


def _entity_of(key: str) -> str:
    """The entity id a `field:` or `entity:` address belongs to."""
    body = key.split(":", 1)[1] if ":" in key else key
    return body.partition(FIELD_SEPARATOR)[0]


def _conflicted(source: Snapshot, target: Snapshot) -> Dict[str, Tuple[str, ...]]:
    """`entity id -> open conflict ids`, from either end.

    From EITHER end because a conflict the source recorded and the target
    resolved still means the two revisions disagree about how that field came to
    be what it is; suppressing `preserved` for both is the direction that cannot
    turn a recorded disagreement into a silent pass.
    """
    out: Dict[str, List[str]] = {}
    for snapshot in (source, target):
        for element in snapshot.elements:
            if not element.key.startswith("entity:"):
                continue
            conflicts = _meta(element).get("conflicts") or []
            if not conflicts:
                continue
            entity_id = _entity_of(element.key)
            out.setdefault(entity_id, []).extend(str(item) for item in conflicts)
    return {entity_id: _unique(items) for entity_id, items in out.items()}


# -- the adapter -----------------------------------------------------------


class StateAdapter:
    """The `state` domain. Implements the `DeltaAdapter` Protocol.

    One revision shape reaches `snapshot` today: a `literal` holding one of the
    three mappings named in the module docstring. A revision of kind `state` is
    refused by `sources.resolve` with that module's own reason -- there is no
    resolver against the State Mirror store and `sources.py` is not this
    module's to change -- and the refusal travels into the snapshot rather than
    becoming an exception, because which end failed and why is exactly what the
    caller needs.
    """

    domain = "state"
    version = "1"

    def available(self) -> bool:
        """Always true. `src.state_mirror.contracts` imports nothing heavy.

        Deliberately NOT a probe for the mirror's store: this adapter compares
        two mappings the caller already holds and never reads a database, so a
        store that is down is not a reason to refuse a comparison that does not
        need it. If a `state` resolver ever lands in `sources.py`, the store
        being unreachable is a `readable=False` on that revision -- an answer
        about one end -- and still not an unavailable adapter.
        """
        return True

    # -- reading one end ---------------------------------------------------

    def snapshot(self, revision: RevisionRef, *, scope: Scope) -> Snapshot:
        """One revision as entities and fields, with the mirror's own metadata."""
        resolved = sources.resolve(revision, scope=scope)
        if not resolved.readable:
            return unreadable(revision, resolved.reason, tier="parser")
        try:
            payload = resolved.mapping()
        except sources.SourceError as exc:
            return unreadable(revision, _short(exc), tier="parser")

        entities, notes, reason = _read(payload)
        if reason:
            return unreadable(revision, _short(reason), tier="parser")

        anchors, rest = _elements(entities)
        elements, cut = self._budgeted(anchors, rest, scope=scope)
        return Snapshot(
            revision=revision,
            elements=elements,
            readable=True,
            # `parser`: every element came out of `MaterializedState.parse` or
            # `StateProjection.parse`, the mirror's own readers. Nothing here was
            # inferred from the shape of a value.
            tier="parser",
            excluded=tuple(_unique([cut] if cut else [])),
            notes=tuple(_unique(notes)),
            truncated=bool(cut),
            detail={
                "entities": len(entities),
                "conflicts": sorted(_unique(
                    [item for entity in entities for item in entity.conflicts])),
                "addressable": len(anchors) + len(rest),
                "addressed": len(elements),
            },
        )

    def _budgeted(self, anchors: List[Element], rest: List[Element], *,
                  scope: Scope) -> Tuple[Tuple[Element, ...], str]:
        """The elements, cut to `budget.max_elements`, and the sentence for it.

        `entity:` first and never dropped; the cut lands on fields, in address
        order, so two runs over one revision produce the same snapshot -- which
        is what the §22 cache key depends on.
        """
        limit = scope.budget.max_elements
        ordered = (sorted(anchors, key=lambda item: item.key)
                   + sorted(rest, key=lambda item: item.key))
        if limit is None or len(ordered) <= limit:
            return tuple(ordered), ""
        kept = ordered[:max(limit, len(anchors))]
        dropped = len(ordered) - len(kept)
        return tuple(kept), (
            f"{dropped} field(s) were not addressed; `budget.max_elements` "
            f"({limit}) stopped the snapshot and nothing past that point was "
            f"compared")

    # -- comparing two ends ------------------------------------------------

    def compare(self, source: Snapshot, target: Snapshot, *,
                scope: Scope) -> Extraction:
        """What differs between two revisions, and which of §15's sentences it is.

        Elements are matched by address, and the address is the mirror's own:
        `<entity_id>#<field>`, the one spelling `projection.py` chose so that a
        projection over two runs cannot collide on `run.status`. There is
        nothing here for a rename detector to do -- an entity id that changed is
        a different entity, by §4.1's construction.

        An unreadable end produces no findings rather than a `missing` for every
        field: "we could not read it" and "every entity was retired" are
        opposite facts.
        """
        readable = source.readable and target.readable
        conflicts = _conflicted(source, target) if readable else {}
        limitations = [
            "this compares two READ MODELS: the mirror is a projection and the "
            "canonical source is outside it, so before an effect that matters "
            "the caller revalidates against the system that owns the entity",
            "the freshness compared is the rating the mirror STORED, not one "
            "re-derived against the clock now; a delta whose answer depended on "
            "when it ran could not be cached and would say two things about one "
            "pair of revisions",
        ]
        if not readable:
            which = "source" if not source.readable else "target"
            limitations.append(
                f"the {which} revision could not be read, so nothing was "
                f"compared; this is not a report that nothing changed")
            return Extraction(
                findings=(),
                coverage=self._coverage(source, target, regions=(), readable=False),
                invariants=(),
                extractor_versions={self.domain: self.version},
                limitations=tuple(_unique(limitations)),
            )
        for entity_id, ids in sorted(conflicts.items()):
            limitations.append(_short(
                f"{entity_id} carries {len(ids)} open conflict(s) "
                f"({', '.join(ids[:3])}); the mirror recorded the disagreement "
                f"instead of resolving it, so nothing about that entity is "
                f"reported as preserved"))

        left, right = source.index(), target.index()
        keys = sorted(set(left) | set(right))
        findings: List[Finding] = []
        for key in keys:
            finding = self._finding(key, left.get(key), right.get(key), conflicts)
            if finding is not None:
                findings.append(finding)

        return Extraction(
            findings=tuple(findings),
            coverage=self._coverage(source, target, regions=tuple(keys),
                                    readable=True),
            invariants=(),
            extractor_versions={self.domain: self.version},
            limitations=tuple(_unique(limitations)),
        )

    def _finding(self, key: str, before: Optional[Element],
                 after: Optional[Element],
                 conflicts: Mapping[str, Tuple[str, ...]]) -> Optional[Finding]:
        """One observation about one address. Never a classification.

        The `classification` axis belongs to `classification.classify_all`,
        which has seen the frozen intent; what §15 asks THIS layer for is a
        finer OPERATION reading -- whether a difference is the world moving, our
        knowledge moving, a datum ageing or a source contradicting itself -- and
        those live in `operation`, `detail` and `limitations`, which is where a
        consumer can act on them without the intent.
        """
        if before is None and after is None:  # pragma: no cover - union of keys
            return None
        common: Dict[str, Any] = {
            "path": key,
            "method": "manifest_field",
            "tier": confidence_mod.tier_of("manifest_field"),
        }
        if key.startswith("field:"):
            return self._field_finding(key, before, after, common, conflicts)
        return self._entity_finding(key, before, after, common, conflicts)

    def _conflict_note(self, key: str,
                       conflicts: Mapping[str, Tuple[str, ...]]) -> List[str]:
        """The `open_conflict` limitation for one address, or an empty list."""
        ids = conflicts.get(_entity_of(key), ())
        if not ids:
            return []
        return [_short(f"{OPEN_CONFLICT}: the mirror holds {len(ids)} open "
                       f"conflict(s) on {_entity_of(key)} ({', '.join(ids[:3])}); "
                       f"it recorded the disagreement rather than resolving it, "
                       f"and this adapter is not the one to settle it")]

    def _entity_finding(self, key: str, before: Optional[Element],
                        after: Optional[Element], common: Dict[str, Any],
                        conflicts: Mapping[str, Tuple[str, ...]]) -> Finding:
        """An entity that appeared, was retired, or whose revision moved.

        `unchanged` here is the level above §15's third sentence: a revision
        only advances when a reduction CHANGED something, so an entity all of
        whose fields merely grew stale keeps its number and reads as unchanged.
        That is the mirror's own guarantee, borrowed rather than recomputed.
        """
        limitations = self._conflict_note(key, conflicts)
        confidence = _weaken("exact", limitations)
        if before is None and after is not None:
            return Finding(operation="added", after=after.value,
                           element_kind=after.kind, confidence=confidence,
                           limitations=tuple(limitations),
                           detail=_short(f"{_entity_of(key)} is in the target and "
                                         f"not in the source"), **common)
        if after is None and before is not None:
            return Finding(operation="missing", before=before.value,
                           element_kind=before.kind, confidence=confidence,
                           limitations=tuple(limitations),
                           detail=_short(f"{_entity_of(key)} is in the source and "
                                         f"not in the target"), **common)
        if before is None or after is None:  # pragma: no cover - handled above
            return Finding(operation="unchanged", confidence=confidence, **common)
        operation = "unchanged" if before.value == after.value else "modified"
        return Finding(operation=operation, before=before.value, after=after.value,
                       element_kind=after.kind, confidence=confidence,
                       limitations=tuple(limitations), **common)

    def _field_finding(self, key: str, before: Optional[Element],
                       after: Optional[Element], common: Dict[str, Any],
                       conflicts: Mapping[str, Tuple[str, ...]]) -> Finding:
        """§15's four sentences, decided in the one place that can tell them apart.

        The order of the questions is the claim, and it is not arbitrary:

        1. Did a STRONGER source speak? Then what changed is what we know, and
           that answer stands whatever the timestamps say -- an inference
           replaced by an observation is not the world moving even if the
           observation is newer.
        2. Otherwise, is it the same source with a newer observation? Then it is
           the world.
        3. Same source, same instant or earlier? Then the source contradicts
           itself about one moment, which is a correction and makes it a weaker
           witness about this field from here on.
        4. Anything else -- two different sources, or two observations nobody
           can order -- is reported as exactly that, at `low`, because telling a
           change from a disagreement needs a fact neither revision carries.
        """
        limitations = self._conflict_note(key, conflicts)
        if before is None and after is not None:
            return Finding(operation="added", after=after.value,
                           element_kind=after.kind, confidence="exact",
                           limitations=tuple(limitations),
                           detail=_short(f"the mirror holds {key[len('field:'):]} "
                                         f"and did not hold it before"), **common)
        if after is None and before is not None:
            return Finding(operation="missing", before=before.value,
                           element_kind=before.kind, confidence="exact",
                           limitations=tuple(limitations),
                           detail=_short(f"the mirror no longer holds "
                                         f"{key[len('field:'):]}"), **common)
        if before is None or after is None:  # pragma: no cover - handled above
            return Finding(operation="unchanged", confidence="exact", **common)

        was, now = _meta(before), _meta(after)
        if before.value == after.value:
            return self._steady_finding(before, after, was, now, common,
                                        limitations)
        return self._moved_finding(before, after, was, now, common, limitations)

    def _steady_finding(self, before: Element, after: Element,
                        was: Mapping[str, Any], now: Mapping[str, Any],
                        common: Dict[str, Any],
                        limitations: List[str]) -> Finding:
        """The same value. §15's third sentence: a datum that merely aged.

        `unchanged` and not a change, which is the whole point of this branch.
        Ageing is a change in what the state is WORTH -- `queries.py` re-rates
        on every read for exactly that reason -- and filing it as a delta would
        put one row per field per hour in front of a reader who is looking for
        the one row where something moved. The decay is named as a limitation so
        that a caller about to act still sees it.
        """
        decayed = _freshness_rank(now.get("freshness")) > _freshness_rank(
            was.get("freshness"))
        detail = ""
        if decayed:
            limitations = list(limitations) + [_short(
                f"{DECAYED}: the value did not move; its freshness fell from "
                f"{was.get('freshness')!r} to {now.get('freshness')!r}, which is "
                f"the datum ageing and not the world changing")]
            detail = _short(
                f"same value, weaker freshness ({was.get('freshness')} -> "
                f"{now.get('freshness')}); this is not a change")
        confidence = _weaken("exact", limitations)
        return Finding(operation="unchanged", before=before.value,
                       after=after.value, element_kind=after.kind,
                       confidence=confidence, limitations=tuple(_unique(limitations)),
                       detail=detail, **common)

    def _moved_finding(self, before: Element, after: Element,
                       was: Mapping[str, Any], now: Mapping[str, Any],
                       common: Dict[str, Any],
                       limitations: List[str]) -> Finding:
        """The value differs. Which of §15's three changes it is, and how sure."""
        before_source = str(was.get("source") or "")
        after_source = str(now.get("source") or "")
        same_source = bool(before_source) and before_source == after_source
        before_at = _instant(was.get("observed_at"))
        after_at = _instant(now.get("observed_at"))

        if epistemic_rank(now.get("epistemic")) < epistemic_rank(was.get("epistemic")):
            confidence = "medium"
            detail = _short(
                f"{KNOWLEDGE_CHANGE}: the value changed because a stronger source "
                f"spoke -- {was.get('epistemic')} -> {now.get('epistemic')}. What "
                f"changed is what we KNOW; an inference replaced by an "
                f"observation is not the world moving")
            note = ("a stronger epistemology overturned the previous claim, so "
                    "whether the world also moved is not something these two "
                    "revisions can say")
        elif same_source and before_at is not None and after_at is not None and after_at > before_at:
            confidence = "exact"
            detail = _short(
                f"{WORLD_CHANGE}: {before_source} observed a different value at a "
                f"later instant ({was.get('observed_at')} -> "
                f"{now.get('observed_at')}); same witness, new observation")
            note = ""
        elif same_source and before_at is not None and after_at is not None:
            confidence = "low"
            detail = _short(
                f"{CORRECTION}: {before_source} reports a different value for the "
                f"same instant or an earlier one ({was.get('observed_at')} -> "
                f"{now.get('observed_at')}); it corrected itself")
            note = (f"{CORRECTION}: a source that contradicts itself about one "
                    f"moment is not a reliable witness about this field, and "
                    f"every conclusion resting on it inherits that")
        elif same_source:
            confidence = "low"
            detail = _short(
                f"{UNDATED}: {before_source} reports a different value and at "
                f"least one of the two observations carries no readable time")
            note = (f"{UNDATED}: without two orderable `observed_at` values a "
                    f"correction cannot be told from a change in the world")
        else:
            confidence = "low"
            detail = _short(
                f"{DISAGREEMENT}: {before_source or '<no source>'} said one thing "
                f"and {after_source or '<no source>'} says another; two sources "
                f"reported the two values")
            note = (f"{DISAGREEMENT}: two different sources produced the two "
                    f"values, so a change in the world cannot be told from a "
                    f"disagreement between witnesses")
        if note:
            limitations = list(limitations) + [_short(note)]
        confidence = _weaken(confidence, limitations)
        return Finding(operation="modified", before=before.value, after=after.value,
                       element_kind=after.kind, confidence=confidence,
                       limitations=tuple(_unique(limitations)), detail=detail,
                       **common)

    def _coverage(self, source: Snapshot, target: Snapshot, *,
                  regions: Tuple[str, ...], readable: bool) -> Coverage:
        """How much was compared, on the two axes this adapter can speak to.

        `structural` is the share of addressable entities and fields the budget
        let through. `temporal` is REAL here and not a token zero: §15's four
        sentences are decided on `observed_at`, so the fraction of compared
        fields that carry an orderable one is a measurement of how much of the
        comparison could be made at all. A comparison with no field addresses
        reports no `temporal` ratio -- `Coverage.ratio` reads that as "nobody
        measured this axis", which is true, and a zero there would say "measured
        and covered none", which is not.
        """
        structural = 0.0
        if readable:
            structural = min(_addressed_ratio(source), _addressed_ratio(target))
        dimensions: Dict[str, float] = {"structural": structural}
        temporal = _temporal_ratio(source, target) if readable else None
        if temporal is not None:
            dimensions["temporal"] = temporal
        listed = regions[:MAX_LISTED_REGIONS]
        notes = [
            "the mirror is a projection of systems that live outside it; this "
            "compares what it believed at two moments and revalidates nothing",
        ]
        if len(regions) > len(listed):
            notes.append(f"{len(regions) - len(listed)} further element keys "
                         f"were compared and are not listed here")
        return coverage_mod.build(
            source_readable=source.readable,
            target_readable=target.readable,
            dimensions=dimensions,
            regions=listed,
            excluded=_unique(list(source.excluded) + list(target.excluded)),
            notes=_unique(notes + list(source.notes) + list(target.notes)),
        )

    # -- the invariants ----------------------------------------------------

    def check_invariants(self, intent: IntentContract, source: Snapshot,
                         target: Snapshot, *,
                         scope: Scope) -> Tuple[InvariantResult, ...]:
        """One result per declared invariant, and one id this adapter answers.

        An OPEN CONFLICT suppresses `preserved` on every row and nothing else:
        a violation found by a real observation is still worth reporting, and
        the mirror holding a disagreement is a reason not to certify a property,
        not a reason to stop looking. §9.2 -- the reducer records the conflict
        instead of picking a winner -- is exactly why this adapter must not pick
        one either.
        """
        readable = source.readable and target.readable
        gap = _gap(source, target)
        conflicts = _conflicted(source, target) if readable else {}
        complete = bool(readable and not gap)

        results: List[InvariantResult] = []
        for invariant in intent.invariants:
            payload: Dict[str, Any] = {
                "invariant_id": invariant.id,
                "severity": invariant.severity,
                "method": "manifest_field",
                "tier": "parser",
            }
            if not readable:
                which = "source" if not source.readable else "target"
                payload.update({
                    "status": "unknown", "confidence": "unknown",
                    "limitations": [f"the {which} revision could not be read, so "
                                    f"no property of it was observed"],
                })
            elif invariant.id == PROVENANCE_INVARIANT:
                payload.update(self._provenance_result(
                    target, complete=complete, gap=gap, conflicts=conflicts))
            elif invariant.klass == "budget":
                payload.update(self._budget_result(source, target))
            else:
                payload.update({
                    "status": "unknown", "confidence": "unknown",
                    "limitations": [_short(
                        f"this adapter has no method for a `{invariant.klass}` "
                        f"property; it compares entities, field values and the "
                        f"epistemology, time, source and freshness the mirror "
                        f"stored beside each one, and nothing here observed "
                        f"that property")],
                })
            results.append(InvariantResult.parse(
                payload, f"invariant_result[{invariant.id}]"))
        return tuple(results)

    def _provenance_result(self, target: Snapshot, *, complete: bool, gap: str,
                           conflicts: Mapping[str, Tuple[str, ...]]) -> Dict[str, Any]:
        """`state.provenance_kept`: every value in the target still names a source.

        `FieldState` makes it impossible to store a value without a source, a
        time and an epistemology -- rule 1 of `state_mirror/contracts.py` -- so a
        field that reaches here with an empty `source` is a real violation and
        not a reading error. The `preserved` branch is earned by that same rule:
        the four qualifiers are on every field or the mirror would not have
        stored it, and this reads all of them.
        """
        blank: List[str] = []
        counted = 0
        for element in target.elements:
            if not element.key.startswith("field:"):
                continue
            counted += 1
            if not str(_meta(element).get("source") or ""):
                blank.append(element.key[len("field:"):])
        if blank:
            return {
                "status": "violated",
                "confidence": "exact" if complete else "high",
                "observations": [_short(f"{name} carries no source")
                                 for name in sorted(blank)][:8],
                "limitations": [gap] if gap else [],
            }
        if conflicts:
            names = sorted(conflicts)
            return {
                "status": "unknown", "confidence": "unknown",
                "limitations": [_short(
                    f"{OPEN_CONFLICT}: the mirror holds an open conflict on "
                    f"{', '.join(names[:3])}; it recorded the disagreement "
                    f"instead of resolving it, so nothing about those entities "
                    f"is certified here")],
            }
        if not complete:
            return {"status": "unknown", "confidence": "unknown",
                    "limitations": [gap or "the comparison was not complete"]}
        return {
            "status": "preserved", "confidence": "exact",
            "observations": [
                _short(f"every one of the target's {counted} field(s) names the "
                       f"source that produced it"),
                "`FieldState` cannot hold a value without a source, a time and "
                "an epistemology, so reading all of them is reading the whole "
                "of what provenance means here",
            ],
        }

    def _budget_result(self, source: Snapshot, target: Snapshot) -> Dict[str, Any]:
        """Whether the comparison stayed inside its budget. Exact, and ours."""
        if source.truncated or target.truncated:
            excluded = _unique(list(source.excluded) + list(target.excluded))
            return {
                "status": "violated", "confidence": "exact",
                "observations": [_short(item) for item in excluded][:8]
                                or ["a snapshot was truncated"],
            }
        return {
            "status": "preserved", "confidence": "exact",
            "observations": [_short(
                f"neither snapshot was truncated: {len(source.elements)} source "
                f"and {len(target.elements)} target elements were addressed "
                f"within the budget")],
        }


# -- module-level readers used by the adapter ------------------------------


def _instant(value: Any) -> Optional[datetime]:
    """An `observed_at` as a comparable moment, or `None`.

    Parsed rather than compared as a string: the mirror's `_ts` accepts what
    `base.timestamp` accepts, and two ISO-8601 spellings with different offsets
    order correctly as moments and incorrectly as text. `None` for an
    unreadable one, which pushes the classification into the branch that says so
    -- a correction cannot be told from a change without two orderable times,
    and guessing an order would decide §15's hardest distinction on a typo.
    """
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _addressed_ratio(snapshot: Snapshot) -> float:
    """How much of this end the budget let the snapshot address, 0..1."""
    detail = dict(snapshot.detail or {})
    addressable = int(detail.get("addressable") or 0)
    addressed = int(detail.get("addressed") or 0)
    if addressable <= 0:
        return 1.0
    return max(0.0, min(1.0, round(addressed / addressable, 4)))


def _temporal_ratio(source: Snapshot, target: Snapshot) -> Optional[float]:
    """The share of compared fields whose observations can actually be ordered.

    `None` when there are no field addresses at all: an unmeasured axis and an
    axis measured as empty are different answers, and `Coverage.ratio` keeps
    them apart only if this does.
    """
    left, right = source.index(), target.index()
    keys = sorted(key for key in set(left) | set(right) if key.startswith("field:"))
    if not keys:
        return None
    datable = 0
    for key in keys:
        sides = [element for element in (left.get(key), right.get(key))
                 if element is not None]
        if sides and all(_instant(_meta(element).get("observed_at")) is not None
                         for element in sides):
            datable += 1
    return max(0.0, min(1.0, round(datable / len(keys), 4)))


def _gap(source: Snapshot, target: Snapshot) -> str:
    """The one sentence naming why a check could not be complete, or `""`."""
    if source.truncated or target.truncated:
        return ("a budget cut at least one snapshot, so the fields nobody "
                "addressed are not evidence that nothing happened to them")
    skipped = _unique(list(source.notes) + list(target.notes))
    if skipped:
        return _short(f"part of a revision could not be addressed: "
                      f"{'; '.join(skipped[:3])}")
    return ""


#: How `registry.py` finds this adapter. A module-level factory and NOT an entry
#: in a tuple somewhere else: `state_mirror/adapters/__init__.py` records what
#: the tuple cost -- five correct adapters that did not exist as far as the
#: running system was concerned, with green tests and no warning, because nobody
#: added them to it. Writing this line IS the registration.
ADAPTER_FACTORY = StateAdapter
