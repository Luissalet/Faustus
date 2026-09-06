"""State Mirror: what is true right now, when we last looked, and how we know.

This module holds the shapes and the closed vocabularies of the read model
described in `inspiration/PLAN_STATE_MIRROR_FAUSTUS.md`. Nothing here reads a
file, opens a socket or calls a model. It is the contract every adapter,
reducer, query and route has to agree on, and it is deliberately the only place
where the words `fresh`, `observed` and `conflict` are defined.

The five rules this file exists to enforce, each with the failure it prevents:

1.  **Every material field carries a time, a source and an epistemology.**
    A number with no `observed_at` is not operational state; it is a rumour
    with good posture. `FieldState` makes it impossible to store one without
    the other three.

2.  **An inference never silently overwrites an observation.** `EPISTEMICS` is
    ordered, `epistemic_rank()` reads that order, and the reducer consults it
    before every write. A model saying "the service is up" cannot displace a
    probe that just failed to reach it.

3.  **All state ages.** There is no `fresh` without a clock: `freshness.rate()`
    takes the observation time, the field's TTL and *now*, and answers one of
    four words. `unknown` is a real answer and is never rendered as `False`,
    `0` or an empty string, because "we have never looked" and "we looked and
    it was empty" are different facts and only one of them is safe to act on.

4.  **The canonical source stays outside.** Every entity id names the system
    that owns it (`service://`, `run://`, `artifact://`), and
    `MaterializedState` is explicitly a projection. Before an effect that
    matters, the caller revalidates against that system — `refresh_actions`
    exists so it knows how.

5.  **Namespaces never mix.** A branch simulation, a council room and a voice
    session each get their own namespace, and the reducer refuses to fold an
    observation from one into the state of another. Without this, a
    hypothetical from Branching Futures becomes a fact about the real machine.

What is deliberately NOT here: no blobs, no transcripts, no secrets, and no
canonical anything. If losing this database would lose a fact, the fact was in
the wrong place.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from src.contracts.base import (
    SCHEMA_VERSION,
    ContractError,
    as_mapping,
    fingerprint,
    now_iso,
    reject_unknown,
    text,
    text_list,
    timestamp,
)

__all__ = [
    "SCHEMA_VERSION",
    "StateError",
    "ENTITY_KINDS",
    "NAMESPACE_KINDS",
    "REAL_NAMESPACE",
    "EPISTEMICS",
    "EPISTEMIC_ORDER",
    "OBSERVED_EPISTEMICS",
    "FRESHNESS_RATINGS",
    "USABLE_FRESHNESS",
    "MIXED_FRESHNESS",
    "MINIMUM_FRESHNESS_LEVELS",
    "SENSITIVITIES",
    "RELATION_KINDS",
    "RELATION_ORIGINS",
    "STATE_SCHEMAS",
    "SCHEMA_FIELDS",
    "SCHEMA_FOR_KIND",
    "SNAPSHOT_SCHEMAS",
    "CONFLICT_STATUSES",
    "SOURCE_HEALTH",
    "StateEntity",
    "FieldState",
    "StateObservation",
    "MaterializedState",
    "StateRelation",
    "StateConflict",
    "StateProjection",
    "new_id",
    "entity_id",
    "parse_entity_id",
    "is_entity_id",
    "namespace_of",
    "epistemic_rank",
    "schema_fields",
    "is_snapshot_schema",
]


class StateError(ContractError):
    """A rejection from this module. Names the field and the value, always."""


# -- entities --------------------------------------------------------------
#
# Section 4.1's id format is `<kind>://<owner>/<namespace>/<identifier>`, and
# the kind is closed for the same reason every other vocabulary here is: a kind
# nothing routes on is a string in a database, and a typo in one produces an
# entity that exists, answers no query and is never noticed.

#: Section 4.1. What kind of thing this row is about. `project` and `repo` are
#: separate because a project can span more than one checkout, and a repo can
#: outlive the project that used it.
ENTITY_KINDS: Tuple[str, ...] = (
    "project", "repo", "service", "model", "run", "artifact", "connection",
    "objective", "session", "council", "device", "workflow", "approval",
    "capability", "resource",
)

#: Section 1.6. Which world an observation is about. `real` is the machine; the
#: rest are isolated on purpose, and the reducer never folds one into another.
#: A `branch:`/`simulation:`/`council:`/`voice:` namespace carries its own id
#: after the colon, so `namespace_of()` reads the prefix and not the whole.
NAMESPACE_KINDS: Tuple[str, ...] = (
    "real", "branch", "simulation", "council", "voice",
)

REAL_NAMESPACE = "real"

#: Section 4.1's id grammar, compiled once. The identifier half allows the
#: separators the rest of the repository already mints (`run_9f2c`,
#: `qwen3.5:9b`, `OBJ-17`, `localai-faustus`) and nothing that could carry a
#: path traversal or a query string into a route.
_ENTITY_ID_RE = re.compile(
    r"^(?P<kind>[a-z][a-z0-9_]*)://"
    r"(?P<owner>[^/\s]*)/"
    r"(?P<namespace>[a-z]+(?::[A-Za-z0-9_.:-]+)?)/"
    r"(?P<identifier>[A-Za-z0-9_.:@+~/-]+)$"
)

_NAMESPACE_RE = re.compile(r"^(?P<kind>[a-z]+)(?::(?P<ref>[A-Za-z0-9_.:-]+))?$")


# -- epistemology ----------------------------------------------------------

#: Section 2.1, ordered from most to least authoritative. The ORDER is the
#: contract: `epistemic_rank()` reads it, and the reducer refuses to let a
#: lower rank overwrite a higher one that is still fresh. Reordering this tuple
#: changes what the system believes, so it is not a cosmetic edit.
#:
#: * `observed`  -- taken directly from the authoritative system.
#: * `reported`  -- an actor said so, and nothing checked it. An agent writing
#:                  "I finished" lands here and stays here until a ChangeSet,
#:                  an artifact or `prove` moves it (section 9.3).
#: * `derived`   -- computed deterministically from observations. Reproducible.
#: * `inferred`  -- a heuristic or a model proposed it.
#: * `unknown`   -- never observed, or too old to count.
EPISTEMICS: Tuple[str, ...] = (
    "observed", "reported", "derived", "inferred", "unknown",
)

#: Rank by position: lower number, stronger claim. Built from `EPISTEMICS` so
#: the two can never disagree.
EPISTEMIC_ORDER: Dict[str, int] = {name: i for i, name in enumerate(EPISTEMICS)}

#: The epistemologies that may be presented as fact without a caveat.
#: `derived` is in and `reported` is not: arithmetic over observations is still
#: evidence, and an actor's word is not.
OBSERVED_EPISTEMICS: Tuple[str, ...] = ("observed", "derived")


# -- freshness -------------------------------------------------------------

#: Section 6.2. Four words, and the fourth is not an error state.
#:
#: * `fresh`   -- inside its guarantee.
#: * `aging`   -- usable for orientation; refresh before deciding on it.
#: * `stale`   -- do not present as current state.
#: * `unknown` -- never observed, source down, or the timestamp is unreadable.
#:
#: A fifth value, `mixed`, exists only on a PROJECTION (section 1.3) and never
#: on a field: it means the projection's fields do not share one rating, which
#: is a statement about the envelope and not about any datum in it.
FRESHNESS_RATINGS: Tuple[str, ...] = ("fresh", "aging", "stale", "unknown")

#: Ratings a caller may act on without revalidating. `aging` is deliberately
#: absent: section 6.3 says orientation may use it and a decision may not.
USABLE_FRESHNESS: Tuple[str, ...] = ("fresh",)

#: The rating a projection carries when its fields disagree. Not a field
#: rating: see `FRESHNESS_RATINGS`.
MIXED_FRESHNESS = "mixed"

#: Section 6.3's risk ladder, as the argument a consumer passes. Each level
#: names the worst rating it will accept, so a caller states its risk instead
#: of doing timestamp arithmetic of its own.
MINIMUM_FRESHNESS_LEVELS: Dict[str, Tuple[str, ...]] = {
    # "tell me roughly where things are"
    "informational": ("fresh", "aging", "stale"),
    # "I am about to choose between two courses of action"
    "decision_safe": ("fresh", "aging"),
    # "I am about to overwrite, publish or delete something"
    "action_safe": ("fresh",),
}


# -- privacy ---------------------------------------------------------------

#: Section 19. How widely a field may travel. Nothing here is a secret VALUE --
#: a secret never enters this store at all -- but `restricted` marks a field
#: that identifies a resource whose existence is itself private.
SENSITIVITIES: Tuple[str, ...] = ("public", "private", "restricted")


# -- relations -------------------------------------------------------------

#: Section 4.5. Typed, closed, and each one directional: `blocked_by` and its
#: inverse are the same row read from either end, never two rows that can
#: disagree.
RELATION_KINDS: Tuple[str, ...] = (
    "belongs_to", "depends_on", "produced_by", "uses", "derived_from",
    "blocked_by", "waiting_for", "claims", "supersedes", "runs_on",
    "requires_approval",
)

#: How a relation came to be known. The same three-way split as the field
#: epistemology, minus the ones that make no sense for an edge.
RELATION_ORIGINS: Tuple[str, ...] = ("observed", "declared", "inferred")


# -- domain schemas (section 5) --------------------------------------------
#
# "No arbitrary blobs as state." Each entity kind this build observes gets a
# versioned schema naming exactly the fields it may carry. A field outside its
# schema is a rejection, not a warning: the whole value of a read model is that
# a consumer can ask for `run.status` and know what it will get.

#: schema id -> the fields it declares. Versioned in the id (`.v1`) so that a
#: rename is a NEW schema and a replay can say "I cannot rebuild this" instead
#: of quietly producing a different state (sections 18 and 26).
SCHEMA_FIELDS: Dict[str, Tuple[str, ...]] = {
    "project_state.v1": (
        "workspace_available", "current_branch", "head", "dirty",
        "changed_paths", "changed_count", "active_objectives",
        "blocked_objectives", "last_verified_changeset", "checkpoint_head",
    ),
    "service_state.v1": (
        "health", "latency_ms", "reason", "capabilities", "last_success_at",
        "last_failure_at",
    ),
    "model_state.v1": (
        "availability", "loaded", "backend", "context_capacity", "vram_bytes",
        "shared_memory_bytes", "active_requests", "tokens_per_second",
        "expires_at", "placement",
    ),
    "run_state.v1": (
        "status", "phase", "progress", "worker_states", "started_at",
        "last_heartbeat", "budget_remaining", "approval_pending",
        "proof_status", "engine", "label",
    ),
    "artifact_state.v1": (
        "exists", "integrity", "approval", "latest_version", "derivatives",
        "stale_derivatives", "publication_state", "byte_size", "kind",
    ),
    "connection_state.v1": (
        "configured", "authenticated", "reachable", "granted_actions",
        "expires_at",
    ),
    "objective_state.v1": (
        "status", "priority", "blocked_by", "impact", "updated_at",
        "evidence_count",
    ),
    "approval_state.v1": (
        "status", "action", "requested_at", "expires_at", "uses_left",
        "requires_human",
    ),
    "device_state.v1": (
        "cpu_percent", "ram_used_bytes", "ram_total_bytes", "disk_free_bytes",
        "gpu_count", "gpu_used_bytes", "gpu_total_bytes", "gpu_utilisation",
    ),
    "session_state.v1": (
        "active", "project_id", "workspace", "turn_state", "queued_position",
        "last_activity_at",
    ),
    "council_state.v1": (
        "status", "policy", "participants", "open_objections", "held_claims",
        "running_tasks", "turn_state",
    ),
}

#: Every schema this build knows, as a closed tuple.
STATE_SCHEMAS: Tuple[str, ...] = tuple(sorted(SCHEMA_FIELDS))

#: Schemas whose observations are always COMPLETE snapshots (section 8.3). For
#: these, a field absent from the observation means "gone", and the reducer
#: removes it. For every other schema an absent field means "not sampled this
#: time" and the previous value stands.
#:
#: The membership rule is whether the source can enumerate the whole thing in
#: one read. `device_state` can -- one `collect_usage()` sees every GPU. A
#: `service_state` probe cannot: a health check that only reached one endpoint
#: must not be able to declare the others gone.
SNAPSHOT_SCHEMAS: Tuple[str, ...] = ("device_state.v1",)

#: The schema each kind uses, so a route can answer "what shape does a run
#: have" without a second table.
SCHEMA_FOR_KIND: Dict[str, str] = {
    "project": "project_state.v1",
    "repo": "project_state.v1",
    "service": "service_state.v1",
    "model": "model_state.v1",
    "run": "run_state.v1",
    "workflow": "run_state.v1",
    "artifact": "artifact_state.v1",
    "connection": "connection_state.v1",
    "objective": "objective_state.v1",
    "approval": "approval_state.v1",
    "device": "device_state.v1",
    "session": "session_state.v1",
    "council": "council_state.v1",
}


# -- conflicts (section 9) -------------------------------------------------

#: Section 9.2. A conflict is a first-class row, not a log line, because the
#: whole point is that the reducer did NOT choose. `reconciling` is the live
#: state; the other three are how it ended.
CONFLICT_STATUSES: Tuple[str, ...] = (
    "reconciling", "resolved", "superseded", "abandoned",
)

#: Section 18's source diagnostics. A source that is `degraded` still answers;
#: one that is `down` does not, and every field it owns ages to `unknown`
#: rather than keeping the last value it happened to report.
SOURCE_HEALTH: Tuple[str, ...] = ("ok", "degraded", "down", "unknown")


# -- small helpers, the house set ------------------------------------------
#
# The same four every contracts module in this repository re-declares, for the
# same reasons `src/council/contracts.py` gives: `base.whole` has an `or
# default` trap, `base.one_of` cannot express "" as a legal value, `base.ident`
# is too strict for the ids this codebase already mints, and a timestamp needs
# a spelling that `parse()` can read back out of its own `to_dict()`.

def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _int(data: Mapping[str, Any], key: str, path: str, *, default: int = 0,
         minimum: Optional[int] = None, maximum: Optional[int] = None) -> int:
    raw = data.get(key, None)
    if raw is None or raw == "":
        return default
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise StateError(f"{path}.{key}", "must be a whole number", got=raw)
    value = int(raw)
    if minimum is not None and value < minimum:
        raise StateError(f"{path}.{key}", f"must be at least {minimum}", got=raw)
    if maximum is not None and value > maximum:
        raise StateError(f"{path}.{key}", f"must be at most {maximum}", got=raw)
    return value


def _ts(data: Mapping[str, Any], key: str, path: str) -> str:
    """An ISO-8601 timestamp, or `""` for "not recorded".

    `to_dict()` writes `""` when there is no time, so `parse()` has to read
    `""` back as absent. A contract that cannot re-read its own output is not a
    contract. An unreadable NON-empty value is still an error -- that is
    `base.timestamp`'s job, and the reason this wraps it rather than replacing
    it.
    """
    raw = data.get(key, None)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return ""
    return timestamp(data, key, path, default="") or ""


def _choice(data: Mapping[str, Any], key: str, path: str, *,
            choices: Sequence[str], default: str) -> str:
    raw = data.get(key, None)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return default
    if not isinstance(raw, str):
        raise StateError(f"{path}.{key}", "must be a string", got=raw)
    value = raw.strip()
    if value not in choices:
        raise StateError(f"{path}.{key}",
                         f"must be one of {', '.join(choices)}", got=raw)
    return value


def _flag(data: Mapping[str, Any], key: str, path: str, *,
          default: Optional[bool] = None) -> Optional[bool]:
    """A tri-state boolean. `None` means "not observed" and is NOT `False`.

    This is the most load-bearing helper in the module. `dirty=False` means we
    looked at the working tree and it was clean; `dirty=None` means nobody has
    looked. Collapsing the two is how a system ends up saying "nothing to
    commit" about a repository it has never read.
    """
    raw = data.get(key, None)
    if raw is None:
        return default
    if not isinstance(raw, bool):
        raise StateError(f"{path}.{key}",
                         "must be true or false (or absent for 'not observed')",
                         got=raw)
    return raw


# -- ids -------------------------------------------------------------------

def entity_id(kind: str, owner: str, identifier: str, *,
              namespace: str = REAL_NAMESPACE) -> str:
    """Build `<kind>://<owner>/<namespace>/<identifier>` (section 4.1).

    The owner may be empty -- this install runs single-user by default and
    `src.owner_identity.effective_storage_owner` answers `""` there -- and an
    empty owner produces `service:///real/comfyui`, which is a legal id whose
    owner segment is blank. That is deliberate: refusing it would make every
    entity on a no-login install unrepresentable.

    The identifier is checked, not escaped. An id is a key and appears in
    routes; one carrying `..`, a space or a query string would be a way to
    reach a row nobody meant to name.
    """
    kind_ = str(kind or "").strip()
    if kind_ not in ENTITY_KINDS:
        raise StateError("entity.kind",
                         f"must be one of {', '.join(ENTITY_KINDS)}", got=kind)
    ident = str(identifier or "").strip()
    if not ident:
        raise StateError("entity.identifier", "an entity needs an identifier")
    built = f"{kind_}://{str(owner or '').strip()}/{_namespace(namespace)}/{ident}"
    if not _ENTITY_ID_RE.match(built):
        raise StateError("entity.id",
                         "the identifier carries a character an id may not "
                         "contain", got=identifier)
    return built


def _namespace(value: Any) -> str:
    raw = str(value or REAL_NAMESPACE).strip() or REAL_NAMESPACE
    match = _NAMESPACE_RE.match(raw)
    if not match or match.group("kind") not in NAMESPACE_KINDS:
        raise StateError("entity.namespace",
                         f"must be one of {', '.join(NAMESPACE_KINDS)}, "
                         "optionally followed by ':<id>'", got=value)
    if match.group("kind") != REAL_NAMESPACE and not match.group("ref"):
        raise StateError("entity.namespace",
                         f"{match.group('kind')!r} names an isolated world and "
                         "needs its id after a colon", got=value)
    return raw


def parse_entity_id(value: Any) -> Dict[str, str]:
    """Split an id back into its four parts, or raise naming the value."""
    raw = str(value or "").strip()
    match = _ENTITY_ID_RE.match(raw)
    if not match:
        raise StateError("entity.id",
                         "must look like <kind>://<owner>/<namespace>/<identifier>",
                         got=value)
    parts = match.groupdict()
    if parts["kind"] not in ENTITY_KINDS:
        raise StateError("entity.id", f"{parts['kind']!r} is not an entity kind",
                         got=value)
    _namespace(parts["namespace"])
    return dict(parts)


def is_entity_id(value: Any) -> bool:
    """Total. For a filter that must not raise on a caller's typo."""
    try:
        parse_entity_id(value)
        return True
    except StateError:
        return False


def namespace_of(value: Any) -> str:
    """The namespace of an id, or `""` when it is not an id.

    Used by the reducer's guard, which is why it does not raise: refusing to
    fold an observation is the right answer for a malformed id too.
    """
    try:
        return parse_entity_id(value)["namespace"]
    except StateError:
        return ""


def epistemic_rank(value: Any) -> int:
    """Position in `EPISTEMICS`; an unknown word ranks last, never first.

    Ranking an unrecognised epistemology as `unknown` rather than raising is
    deliberate: an adapter from a newer build may report one this version has
    never heard of, and the safe reading of "I do not know how you know this"
    is "weakly".
    """
    return EPISTEMIC_ORDER.get(str(value or "").strip(),
                               EPISTEMIC_ORDER["unknown"])


def schema_fields(schema: Any) -> Tuple[str, ...]:
    """The fields a schema declares, or `()` for one this build does not know."""
    return SCHEMA_FIELDS.get(str(schema or "").strip(), ())


def is_snapshot_schema(schema: Any) -> bool:
    return str(schema or "").strip() in SNAPSHOT_SCHEMAS


# -- the shapes ------------------------------------------------------------

@dataclass(frozen=True)
class StateEntity:
    """Section 4.2. A thing the mirror has an opinion about.

    Frozen and owner-scoped. `retired_at` is a timestamp rather than a delete,
    because "this service used to exist" is an answer a reconciliation sweep
    has to be able to give: a row that vanished and a row that was retired look
    identical to a consumer, and only one of them means the sweep worked.

    `kind`, `owner` and `namespace` are all derivable from `id`, and `parse()`
    checks that the supplied ones AGREE rather than ignoring them. Two fields
    that can disagree are two fields that eventually will, and here the
    disagreement would be about who may read the row.
    """

    id: str = ""
    kind: str = ""
    owner: str = ""
    namespace: str = REAL_NAMESPACE
    project_id: str = ""
    display_name: str = ""
    labels: Tuple[str, ...] = ()
    source_refs: Tuple[str, ...] = ()
    schema: str = ""
    created_at: str = ""
    updated_at: str = ""
    retired_at: str = ""
    sensitivity: str = "private"
    schema_version: int = SCHEMA_VERSION
    _KEYS = ("id", "kind", "owner", "namespace", "project_id", "display_name",
             "labels", "source_refs", "schema", "created_at", "updated_at",
             "retired_at", "sensitivity", "schema_version")

    @classmethod
    def parse(cls, raw: Any, path: str = "entity") -> "StateEntity":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        ident = text(data, "id", path, required=False, max_len=512)
        if not ident:
            raise StateError(f"{path}.id", "an entity needs its canonical id")
        parts = parse_entity_id(ident)
        kind = _choice(data, "kind", path, choices=ENTITY_KINDS,
                       default=parts["kind"])
        if kind != parts["kind"]:
            raise StateError(f"{path}.kind",
                             f"disagrees with the id, which says {parts['kind']!r}",
                             got=kind)
        owner = text(data, "owner", path, required=False, max_len=256,
                     allow_blank=True) or parts["owner"]
        if owner != parts["owner"]:
            raise StateError(f"{path}.owner",
                             f"disagrees with the id, which says {parts['owner']!r}",
                             got=owner)
        namespace = _namespace(data.get("namespace") or parts["namespace"])
        if namespace != parts["namespace"]:
            raise StateError(f"{path}.namespace",
                             f"disagrees with the id, which says "
                             f"{parts['namespace']!r}", got=namespace)
        return cls(
            id=ident,
            kind=kind,
            owner=owner,
            namespace=namespace,
            project_id=text(data, "project_id", path, required=False, max_len=256),
            display_name=text(data, "display_name", path, required=False, max_len=512),
            labels=text_list(data, "labels", path, max_items=32, max_len=64),
            source_refs=text_list(data, "source_refs", path, max_items=32, max_len=512),
            schema=_choice(data, "schema", path, choices=STATE_SCHEMAS,
                           default=SCHEMA_FOR_KIND.get(kind, "")),
            created_at=_ts(data, "created_at", path) or now_iso(),
            updated_at=_ts(data, "updated_at", path),
            retired_at=_ts(data, "retired_at", path),
            sensitivity=_choice(data, "sensitivity", path, choices=SENSITIVITIES,
                                default="private"),
            schema_version=_int(data, "schema_version", path,
                                default=SCHEMA_VERSION, minimum=1),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "kind": self.kind, "owner": self.owner,
            "namespace": self.namespace, "project_id": self.project_id,
            "display_name": self.display_name, "labels": list(self.labels),
            "source_refs": list(self.source_refs), "schema": self.schema,
            "created_at": self.created_at, "updated_at": self.updated_at,
            "retired_at": self.retired_at, "sensitivity": self.sensitivity,
            "schema_version": self.schema_version,
        }

    def retired(self) -> bool:
        return bool(self.retired_at)

    def identity(self) -> str:
        """A stable hash of WHAT this entity is, not of when it was seen."""
        return fingerprint([
            ("id", self.id), ("kind", self.kind), ("owner", self.owner),
            ("namespace", self.namespace), ("schema", self.schema),
        ])


@dataclass(frozen=True)
class FieldState:
    """Section 4.4's `field_meta`: one value, and the four things that qualify it.

    This is the smallest unit of honesty in the system. A `MaterializedState`
    is a mapping of field name to one of these, and there is no way to write a
    value into it without also writing where it came from, when it was seen and
    how strongly it is known -- which is the whole reason the mapping is not
    just a dict of values.

    `freshness` is STORED rather than computed on read because a rating is
    relative to a clock, and a state that recomputed it on access would answer
    differently to two callers reading the same revision. The stored value is
    what the rating was at `computed_at`; `freshness.rate()` re-derives it for
    now, and that is what a query returns.
    """

    value: Any = None
    epistemic: str = "unknown"
    freshness: str = "unknown"
    observed_at: str = ""
    observation_id: str = ""
    source: str = ""
    ttl_seconds: int = 0
    computed_at: str = ""
    _KEYS = ("value", "epistemic", "freshness", "observed_at", "observation_id",
             "source", "ttl_seconds", "computed_at")

    @classmethod
    def parse(cls, raw: Any, path: str = "field") -> "FieldState":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        return cls(
            value=data.get("value", None),
            epistemic=_choice(data, "epistemic", path, choices=EPISTEMICS,
                              default="unknown"),
            freshness=_choice(data, "freshness", path, choices=FRESHNESS_RATINGS,
                              default="unknown"),
            observed_at=_ts(data, "observed_at", path),
            observation_id=text(data, "observation_id", path, required=False,
                                max_len=128),
            source=text(data, "source", path, required=False, max_len=128),
            ttl_seconds=_int(data, "ttl_seconds", path, default=0, minimum=0),
            computed_at=_ts(data, "computed_at", path),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "value": self.value, "epistemic": self.epistemic,
            "freshness": self.freshness, "observed_at": self.observed_at,
            "observation_id": self.observation_id, "source": self.source,
            "ttl_seconds": self.ttl_seconds, "computed_at": self.computed_at,
        }

    def trusted(self) -> bool:
        """Fresh AND observed or derived. The test a caller about to act runs.

        Both halves are needed and neither is enough on its own: a fresh rumour
        is still a rumour, and yesterday's measurement is still yesterday's.
        """
        return (self.freshness in USABLE_FRESHNESS
                and self.epistemic in OBSERVED_EPISTEMICS)


@dataclass(frozen=True)
class StateObservation:
    """Section 4.3. One look at one entity, append-only, never edited.

    `observed_at` and `received_at` are both stored because they answer
    different questions, and a lost event is the difference between them
    (section 8.2). Ordering on the remote clock alone is how a machine with a
    skewed clock rewrites history.

    `partial` is the field the reducer routes on. `False` plus a snapshot
    schema means "these are all the fields there are, delete the rest"; `True`
    -- the default -- means "these are the ones I sampled", and every field not
    listed keeps the value it had (section 8.3). Defaulting to `True` is the
    safe direction: a probe that forgot to declare itself complete deletes
    nothing.
    """

    id: str = ""
    entity_id: str = ""
    owner: str = ""
    namespace: str = REAL_NAMESPACE
    project_id: str = ""
    schema: str = ""
    source: str = ""
    epistemic: str = "observed"
    sequence: int = 0
    observed_at: str = ""
    received_at: str = ""
    valid_for_seconds: int = 0
    partial: bool = True
    state: Mapping[str, Any] = field(default_factory=dict)
    evidence_refs: Tuple[str, ...] = ()
    source_revision: str = ""
    sensitivity: str = "private"
    schema_version: int = SCHEMA_VERSION
    _KEYS = ("id", "entity_id", "owner", "namespace", "project_id", "schema",
             "source", "epistemic", "sequence", "observed_at", "received_at",
             "valid_for_seconds", "partial", "state", "evidence_refs",
             "source_revision", "sensitivity", "schema_version")

    @classmethod
    def parse(cls, raw: Any, path: str = "observation") -> "StateObservation":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        target = text(data, "entity_id", path, required=False, max_len=512)
        if not target:
            raise StateError(f"{path}.entity_id",
                             "an observation is about exactly one entity")
        parts = parse_entity_id(target)
        schema = _choice(data, "schema", path, choices=STATE_SCHEMAS,
                         default=SCHEMA_FOR_KIND.get(parts["kind"], ""))
        if not schema:
            raise StateError(f"{path}.schema",
                             f"nothing in this build declares a schema for a "
                             f"{parts['kind']!r}; an observation without one is "
                             "a blob", got=data.get("schema"))
        source = text(data, "source", path, required=False, max_len=128)
        if not source:
            raise StateError(f"{path}.source",
                             "an observation names the adapter that made it; "
                             "state with no source cannot be revalidated")
        body = as_mapping(data.get("state") or {}, f"{path}.state")
        declared = schema_fields(schema)
        unknown = sorted(k for k in body if k not in declared)
        if unknown:
            raise StateError(f"{path}.state",
                             f"{schema} does not declare {', '.join(unknown)}; "
                             f"it declares {', '.join(declared)}")
        return cls(
            id=text(data, "id", path, required=False, max_len=128) or new_id("obs"),
            entity_id=target,
            owner=text(data, "owner", path, required=False, max_len=256,
                       allow_blank=True) or parts["owner"],
            namespace=parts["namespace"],
            project_id=text(data, "project_id", path, required=False, max_len=256),
            schema=schema,
            source=source,
            epistemic=_choice(data, "epistemic", path, choices=EPISTEMICS,
                              default="observed"),
            sequence=_int(data, "sequence", path, default=0, minimum=0),
            observed_at=_ts(data, "observed_at", path) or now_iso(),
            received_at=_ts(data, "received_at", path) or now_iso(),
            valid_for_seconds=_int(data, "valid_for_seconds", path,
                                   default=0, minimum=0),
            partial=bool(_flag(data, "partial", path, default=True)),
            state=dict(body),
            evidence_refs=text_list(data, "evidence_refs", path,
                                    max_items=64, max_len=512),
            source_revision=text(data, "source_revision", path,
                                 required=False, max_len=256),
            sensitivity=_choice(data, "sensitivity", path, choices=SENSITIVITIES,
                                default="private"),
            schema_version=_int(data, "schema_version", path,
                                default=SCHEMA_VERSION, minimum=1),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "entity_id": self.entity_id, "owner": self.owner,
            "namespace": self.namespace, "project_id": self.project_id,
            "schema": self.schema, "source": self.source,
            "epistemic": self.epistemic, "sequence": self.sequence,
            "observed_at": self.observed_at, "received_at": self.received_at,
            "valid_for_seconds": self.valid_for_seconds, "partial": self.partial,
            "state": dict(self.state), "evidence_refs": list(self.evidence_refs),
            "source_revision": self.source_revision,
            "sensitivity": self.sensitivity, "schema_version": self.schema_version,
        }

    def identity(self) -> str:
        """What makes this observation the same observation.

        Deliberately excludes `received_at` and `id`: the same probe result
        arriving twice -- a replayed event, a retried webhook, a reconnecting
        subscription -- is ONE observation, and dedupe (section 8.1) joins on
        this. `observed_at` is included, because the same values seen at a
        later time are genuinely new information about how long they have held.
        """
        return fingerprint([
            ("entity_id", self.entity_id), ("source", self.source),
            ("schema", self.schema), ("observed_at", self.observed_at),
            ("sequence", self.sequence), ("source_revision", self.source_revision),
            ("state", sorted((k, repr(v)) for k, v in self.state.items())),
        ])

    def complete(self) -> bool:
        """Whether this observation may delete fields it does not mention.

        Both halves are required: the schema has to be one whose source can see
        everything at once, AND this particular sample has to have been declared
        complete. A snapshot schema sampled partially is still partial.
        """
        return (not self.partial) and is_snapshot_schema(self.schema)


@dataclass(frozen=True)
class MaterializedState:
    """Section 4.4. What we currently believe about one entity, and why.

    `revision` is a monotonic counter per entity, and it is what the Universal
    Delta Engine compares (section 1.5). It increments only when a reduction
    CHANGED something: an observation that confirms what we already knew
    refreshes the timestamps and leaves the revision alone, so "revision 42 ->
    43" always means a material change and never means "we looked again".
    """

    entity_id: str = ""
    owner: str = ""
    namespace: str = REAL_NAMESPACE
    project_id: str = ""
    schema: str = ""
    revision: int = 0
    fields: Mapping[str, FieldState] = field(default_factory=dict)
    conflicts: Tuple[str, ...] = ()
    updated_at: str = ""
    schema_version: int = SCHEMA_VERSION
    _KEYS = ("entity_id", "owner", "namespace", "project_id", "schema",
             "revision", "fields", "conflicts", "updated_at", "schema_version")

    @classmethod
    def parse(cls, raw: Any, path: str = "state") -> "MaterializedState":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        target = text(data, "entity_id", path, required=False, max_len=512)
        if not target:
            raise StateError(f"{path}.entity_id", "a state belongs to one entity")
        parts = parse_entity_id(target)
        schema = _choice(data, "schema", path, choices=STATE_SCHEMAS,
                         default=SCHEMA_FOR_KIND.get(parts["kind"], ""))
        raw_fields = as_mapping(data.get("fields") or {}, f"{path}.fields")
        declared = schema_fields(schema)
        unknown = sorted(k for k in raw_fields if k not in declared)
        if unknown:
            raise StateError(f"{path}.fields",
                             f"{schema} does not declare {', '.join(unknown)}")
        return cls(
            entity_id=target,
            owner=text(data, "owner", path, required=False, max_len=256,
                       allow_blank=True) or parts["owner"],
            namespace=parts["namespace"],
            project_id=text(data, "project_id", path, required=False, max_len=256),
            schema=schema,
            revision=_int(data, "revision", path, default=0, minimum=0),
            fields={k: FieldState.parse(v, f"{path}.fields.{k}")
                    for k, v in raw_fields.items()},
            conflicts=text_list(data, "conflicts", path, max_items=64, max_len=128),
            updated_at=_ts(data, "updated_at", path),
            schema_version=_int(data, "schema_version", path,
                                default=SCHEMA_VERSION, minimum=1),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entity_id": self.entity_id, "owner": self.owner,
            "namespace": self.namespace, "project_id": self.project_id,
            "schema": self.schema, "revision": self.revision,
            "fields": {k: v.to_dict() for k, v in self.fields.items()},
            "conflicts": list(self.conflicts), "updated_at": self.updated_at,
            "schema_version": self.schema_version,
        }

    def values(self) -> Dict[str, Any]:
        """Just the values, for a caller that has already checked freshness.

        Separate from `to_dict()` on purpose: reaching for the bare values has
        to be a decision somebody wrote down, not the shape that happens to be
        easiest to reach for.
        """
        return {k: v.value for k, v in self.fields.items()}

    def get(self, name: str) -> Optional[FieldState]:
        return self.fields.get(str(name or "").strip())

    def revision_ref(self) -> str:
        """The token Delta Engine compares (section 1.3's `revision`)."""
        return f"state:{self.entity_id}@{self.revision}"

    def unknown_fields(self) -> Tuple[str, ...]:
        """Fields the schema declares that nothing has ever observed.

        Part of the output, never a silent omission (section 1.3): a consumer
        that cannot tell "false" from "never looked" will eventually act on the
        difference.
        """
        seen = set(self.fields)
        return tuple(f for f in schema_fields(self.schema) if f not in seen)


@dataclass(frozen=True)
class StateRelation:
    """Section 4.5. A typed edge, with how it came to be known.

    Edges are stored in one direction only. `blocked_by` from A to B is the
    same fact as "B blocks A", and storing both would create two rows that can
    disagree; a query reads either end of the single row.
    """

    id: str = ""
    from_id: str = ""
    to_id: str = ""
    kind: str = ""
    owner: str = ""
    namespace: str = REAL_NAMESPACE
    origin: str = "observed"
    source: str = ""
    observed_at: str = ""
    created_at: str = ""
    retired_at: str = ""
    schema_version: int = SCHEMA_VERSION
    _KEYS = ("id", "from_id", "to_id", "kind", "owner", "namespace", "origin",
             "source", "observed_at", "created_at", "retired_at",
             "schema_version")

    @classmethod
    def parse(cls, raw: Any, path: str = "relation") -> "StateRelation":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        from_id = text(data, "from_id", path, required=False, max_len=512)
        to_id = text(data, "to_id", path, required=False, max_len=512)
        if not from_id or not to_id:
            raise StateError(f"{path}.from_id",
                             "a relation joins two entities and needs both ids")
        left = parse_entity_id(from_id)
        right = parse_entity_id(to_id)
        if left["namespace"] != right["namespace"]:
            raise StateError(f"{path}.to_id",
                             f"crosses namespaces ({left['namespace']} -> "
                             f"{right['namespace']}); a branch's state may not "
                             "point at the real world's", got=to_id)
        kind = _choice(data, "kind", path, choices=RELATION_KINDS, default="")
        if not kind:
            raise StateError(f"{path}.kind",
                             f"must be one of {', '.join(RELATION_KINDS)}",
                             got=data.get("kind"))
        return cls(
            id=text(data, "id", path, required=False, max_len=128) or new_id("rel"),
            from_id=from_id,
            to_id=to_id,
            kind=kind,
            owner=text(data, "owner", path, required=False, max_len=256,
                       allow_blank=True) or left["owner"],
            namespace=left["namespace"],
            origin=_choice(data, "origin", path, choices=RELATION_ORIGINS,
                           default="observed"),
            source=text(data, "source", path, required=False, max_len=128),
            observed_at=_ts(data, "observed_at", path),
            created_at=_ts(data, "created_at", path) or now_iso(),
            retired_at=_ts(data, "retired_at", path),
            schema_version=_int(data, "schema_version", path,
                                default=SCHEMA_VERSION, minimum=1),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "from_id": self.from_id, "to_id": self.to_id,
            "kind": self.kind, "owner": self.owner, "namespace": self.namespace,
            "origin": self.origin, "source": self.source,
            "observed_at": self.observed_at, "created_at": self.created_at,
            "retired_at": self.retired_at, "schema_version": self.schema_version,
        }

    def key(self) -> str:
        """What makes two rows the same edge. The unique index uses this."""
        return f"{self.from_id}|{self.kind}|{self.to_id}"


@dataclass(frozen=True)
class StateConflict:
    """Section 9.2. Two sources that disagree, recorded rather than decided.

    The reducer builds one of these instead of picking a winner. The field it
    covers keeps the stronger claim while `next_check` says what would settle
    it. A conflict that settled itself is `resolved` with the value that won;
    one that stopped mattering -- the entity retired, the run finished -- is
    `abandoned`, which is not the same thing and must not read as if somebody
    checked.
    """

    id: str = ""
    entity_id: str = ""
    field: str = ""
    owner: str = ""
    namespace: str = REAL_NAMESPACE
    status: str = "reconciling"
    claims: Tuple[Mapping[str, Any], ...] = ()
    next_check: str = ""
    detected_at: str = ""
    resolved_at: str = ""
    resolution: str = ""
    schema_version: int = SCHEMA_VERSION
    _KEYS = ("id", "entity_id", "field", "owner", "namespace", "status",
             "claims", "next_check", "detected_at", "resolved_at", "resolution",
             "schema_version")

    @classmethod
    def parse(cls, raw: Any, path: str = "conflict") -> "StateConflict":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        target = text(data, "entity_id", path, required=False, max_len=512)
        if not target:
            raise StateError(f"{path}.entity_id", "a conflict is about one entity")
        parts = parse_entity_id(target)
        name = text(data, "field", path, required=False, max_len=128)
        if not name:
            raise StateError(f"{path}.field",
                             "a conflict is about one field; 'the state is wrong' "
                             "is not something a next check can settle")
        raw_claims = data.get("claims") or ()
        if not isinstance(raw_claims, (list, tuple)):
            raise StateError(f"{path}.claims", "must be a list", got=raw_claims)
        claims = tuple(dict(as_mapping(c, f"{path}.claims[{i}]"))
                       for i, c in enumerate(raw_claims))
        if len(claims) < 2:
            raise StateError(f"{path}.claims",
                             "a conflict needs at least two claims; one claim is "
                             "just a value", got=len(claims))
        return cls(
            id=text(data, "id", path, required=False, max_len=128) or new_id("cfl"),
            entity_id=target,
            field=name,
            owner=text(data, "owner", path, required=False, max_len=256,
                       allow_blank=True) or parts["owner"],
            namespace=parts["namespace"],
            status=_choice(data, "status", path, choices=CONFLICT_STATUSES,
                           default="reconciling"),
            claims=claims,
            next_check=text(data, "next_check", path, required=False, max_len=256),
            detected_at=_ts(data, "detected_at", path) or now_iso(),
            resolved_at=_ts(data, "resolved_at", path),
            resolution=text(data, "resolution", path, required=False, max_len=512),
            schema_version=_int(data, "schema_version", path,
                                default=SCHEMA_VERSION, minimum=1),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "entity_id": self.entity_id, "field": self.field,
            "owner": self.owner, "namespace": self.namespace,
            "status": self.status, "claims": [dict(c) for c in self.claims],
            "next_check": self.next_check, "detected_at": self.detected_at,
            "resolved_at": self.resolved_at, "resolution": self.resolution,
            "schema_version": self.schema_version,
        }

    def open(self) -> bool:
        return self.status == "reconciling"

    def key(self) -> str:
        """One live conflict per (entity, field). The unique index uses this."""
        return f"{self.entity_id}|{self.field}"


@dataclass(frozen=True)
class StateProjection:
    """Section 1.3. A bounded, permission-checked answer for one consumer.

    This is what leaves the subsystem. It is not a dump: a consumer names the
    entities and fields it needs and the risk it is taking, and gets back those
    fields with their freshness, the observations behind them, the conflicts
    that touch them, and -- when something is not fresh enough -- the actions
    that would revalidate it.

    `freshness` here may be `mixed`, which no FIELD may be. It means the fields
    do not share a rating, and it exists so that a caller checking one number
    before an effect is forced to look at the fields instead.
    """

    id: str = ""
    owner: str = ""
    project_id: str = ""
    namespace: str = REAL_NAMESPACE
    as_of: str = ""
    minimum_freshness: str = "informational"
    entity_refs: Tuple[str, ...] = ()
    fields: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    source_observation_ids: Tuple[str, ...] = ()
    freshness: str = "unknown"
    unknown_fields: Tuple[str, ...] = ()
    conflicts: Tuple[str, ...] = ()
    refresh_actions: Tuple[Mapping[str, Any], ...] = ()
    revision: str = ""
    schema_version: int = SCHEMA_VERSION
    #: `projection_id` is accepted on the way in and written on the way out:
    #: section 1.3 publishes that spelling and this repository uses `id`.
    #: Accepting both is what lets a consumer written against the plan round-trip
    #: through `parse(to_dict(x))` without losing its own key.
    _KEYS = ("id", "projection_id", "owner", "project_id", "namespace", "as_of",
             "minimum_freshness", "entity_refs", "fields",
             "source_observation_ids", "freshness", "unknown_fields",
             "conflicts", "refresh_actions", "revision", "schema_version")

    @classmethod
    def parse(cls, raw: Any, path: str = "projection") -> "StateProjection":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        raw_actions = data.get("refresh_actions") or ()
        if not isinstance(raw_actions, (list, tuple)):
            raise StateError(f"{path}.refresh_actions", "must be a list",
                             got=raw_actions)
        ident = (text(data, "id", path, required=False, max_len=128)
                 or text(data, "projection_id", path, required=False, max_len=128)
                 or new_id("stateproj"))
        return cls(
            id=ident,
            owner=text(data, "owner", path, required=False, max_len=256,
                       allow_blank=True),
            project_id=text(data, "project_id", path, required=False, max_len=256),
            namespace=_namespace(data.get("namespace") or REAL_NAMESPACE),
            as_of=_ts(data, "as_of", path) or now_iso(),
            minimum_freshness=_choice(data, "minimum_freshness", path,
                                      choices=tuple(MINIMUM_FRESHNESS_LEVELS),
                                      default="informational"),
            entity_refs=text_list(data, "entity_refs", path, max_items=256,
                                  max_len=512),
            fields={str(k): dict(as_mapping(v, f"{path}.fields.{k}"))
                    for k, v in as_mapping(data.get("fields") or {},
                                           f"{path}.fields").items()},
            source_observation_ids=text_list(data, "source_observation_ids", path,
                                             max_items=512, max_len=128),
            freshness=_choice(data, "freshness", path,
                              choices=FRESHNESS_RATINGS + (MIXED_FRESHNESS,),
                              default="unknown"),
            unknown_fields=text_list(data, "unknown_fields", path,
                                     max_items=256, max_len=128),
            conflicts=text_list(data, "conflicts", path, max_items=128, max_len=128),
            refresh_actions=tuple(dict(as_mapping(a, f"{path}.refresh_actions[{i}]"))
                                  for i, a in enumerate(raw_actions)),
            revision=text(data, "revision", path, required=False, max_len=512),
            schema_version=_int(data, "schema_version", path,
                                default=SCHEMA_VERSION, minimum=1),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "projection_id": self.id, "id": self.id, "owner": self.owner,
            "project_id": self.project_id, "namespace": self.namespace,
            "as_of": self.as_of, "minimum_freshness": self.minimum_freshness,
            "entity_refs": list(self.entity_refs),
            "fields": {k: dict(v) for k, v in self.fields.items()},
            "source_observation_ids": list(self.source_observation_ids),
            "freshness": self.freshness,
            "unknown_fields": list(self.unknown_fields),
            "conflicts": list(self.conflicts),
            "refresh_actions": [dict(a) for a in self.refresh_actions],
            "revision": self.revision, "schema_version": self.schema_version,
        }

    def sufficient(self) -> bool:
        """Whether every field meets the risk level this projection was asked for.

        `mixed` is never sufficient by itself -- that is what the word is for.
        A caller wanting to act on part of a mixed projection reads the fields.
        An EMPTY projection is not sufficient either: nothing is not a proof of
        anything.
        """
        allowed = MINIMUM_FRESHNESS_LEVELS.get(self.minimum_freshness, ())
        if not allowed or not self.fields:
            return False
        return all(str(meta.get("freshness") or "unknown") in allowed
                   for meta in self.fields.values())
