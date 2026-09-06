"""
src/state_mirror -- what is true right now, when we last looked, and how we know.

Faustus could already see almost everything about itself and could not ANSWER
anything about itself. `dispatch` knows which jobs are running, `agent_runs`
knows which chats are busy, `capability_registry` knows which backends are up,
`approval_store` knows what is waiting for a person, `services/objectives.py`
knows what is blocked. Every one of those is a separate registry with its own
vocabulary and its own idea of time, so "what is this machine doing" had no
answer any single piece of code could give -- and an agent asking it got
whichever registry the code path happened to reach.

This package is the read model over all of them, and it is a PROJECTION and
never a warehouse. Nothing here is canonical: losing the database costs a
rebuild from the sources that already own the facts, and it cannot cost a fact.
That is what makes the rest of the design affordable -- observations can be
compacted without ceremony, a corrupt file can be quarantined rather than
repaired, and "why did it think that" always has an answer somebody can check
against the system that actually owns the row.

Three ideas carry the whole thing, and each one is a module:

**`contracts.py`** holds the vocabularies and the shapes. Every material field
carries a value, a time, a source and an epistemology -- a number with no
`observed_at` is a rumour with good posture -- and `unknown` is a real answer
that is never rendered as `False`, `0` or an empty string.

**`freshness.py`** decides how old is too old, per field, with the clock passed
in. All state ages, so there is no `fresh` without a clock, and a read re-rates
rather than trusting what was stored: "the server was up yesterday" must stop
reading as "the server is up".

**`persistence.py`** is where observations and materialised state live -- its
own SQLite file, because probes append several rows a second while work is
running and that traffic does not belong in a database a user's sessions also
live in. Reads never raise; writes raise typed errors.

Imports here stay shallow on purpose. This module pulls in `contracts` and
`persistence` and nothing else: no adapter, no reducer, no service. Importing
a dataclass must never drag in sqlalchemy, the council store, the session
database or a GPU probe -- `state_mirror.adapters` builds eleven source modules
on import, and a caller that only wanted `StateEntity` should not pay for one
of them.
"""

from .contracts import (  # noqa: F401
    CONFLICT_STATUSES,
    ENTITY_KINDS,
    EPISTEMICS,
    EPISTEMIC_ORDER,
    FRESHNESS_RATINGS,
    MINIMUM_FRESHNESS_LEVELS,
    MIXED_FRESHNESS,
    NAMESPACE_KINDS,
    OBSERVED_EPISTEMICS,
    REAL_NAMESPACE,
    RELATION_KINDS,
    RELATION_ORIGINS,
    SCHEMA_FIELDS,
    SCHEMA_FOR_KIND,
    SENSITIVITIES,
    SNAPSHOT_SCHEMAS,
    SOURCE_HEALTH,
    STATE_SCHEMAS,
    USABLE_FRESHNESS,
    FieldState,
    MaterializedState,
    StateConflict,
    StateEntity,
    StateError,
    StateObservation,
    StateProjection,
    StateRelation,
    entity_id,
    epistemic_rank,
    is_entity_id,
    is_snapshot_schema,
    namespace_of,
    new_id,
    parse_entity_id,
    schema_fields,
)
from .persistence import (  # noqa: F401
    ANY_OWNER,
    NotFound,
    StateStore,
    StateStoreError,
    db_path,
    store,
    use_path,
)

__all__ = [
    "CONFLICT_STATUSES", "ENTITY_KINDS", "EPISTEMICS", "EPISTEMIC_ORDER",
    "FRESHNESS_RATINGS", "MINIMUM_FRESHNESS_LEVELS", "MIXED_FRESHNESS",
    "NAMESPACE_KINDS", "OBSERVED_EPISTEMICS", "REAL_NAMESPACE",
    "RELATION_KINDS", "RELATION_ORIGINS", "SCHEMA_FIELDS", "SCHEMA_FOR_KIND",
    "SENSITIVITIES", "SNAPSHOT_SCHEMAS", "SOURCE_HEALTH", "STATE_SCHEMAS",
    "USABLE_FRESHNESS",
    "FieldState", "MaterializedState", "StateConflict", "StateEntity",
    "StateError", "StateObservation", "StateProjection", "StateRelation",
    "entity_id", "epistemic_rank", "is_entity_id", "is_snapshot_schema",
    "namespace_of", "new_id", "parse_entity_id", "schema_fields",
    "ANY_OWNER", "NotFound", "StateStore", "StateStoreError", "db_path",
    "store", "use_path",
]
