"""
src/council — a room where several models think and exactly one of them acts.

Faustus already had three quarters of a council and no council.  Group chat gave
it a shared room; `src/tournament.py` gave it blind rounds, contrast, fusion, a
judge and cancellation; `src/agent_tools/subagent_tools.py` and `src/dispatch.py`
gave it workers, file ownership, a watchdog, review and evidence.  What none of
them had is the thing that makes those three safe to combine:

    many models may think, object and review the same matter; only the
    designated owner may execute each effect or modify each resource.

So this package adds no fourth agent loop, no second lock registry, no rival
approval gate and no new evaluator.  `tournament` still runs blind rounds,
`dispatch` still runs workers, `FileLockRegistry` still refuses a write,
`prove` still issues verdicts.  What lives here is the layer above them: the
room, the turn, the ledger of what was claimed, decided and objected to, and
the persistence that makes all of it survive a restart.

Phase 0 is this: contracts and storage, and nothing that calls a model.

**`contracts.py`** holds the vocabularies, the nine shapes and the two state
graphs.  Everything a policy, a scheduler or a route needs to agree on is a
value there, so that two modules cannot hold two versions of the same rule.

**`persistence.py`** is the source of truth for operational state (§15.1) — its
own SQLite file, one lock, WAL, idempotency keys, visibility-filtered reads and
a start-up `recover()` that repeats no effect.

Imports here stay shallow on purpose.  These two modules depend on
`src.contracts.base`, `src.constants` and the standard library and on nothing
else, so importing a dataclass never drags a model client, an embedding index
or a GPU semaphore onto the turn path.
"""

from .contracts import (  # noqa: F401
    AUTHOR_KINDS,
    CLAIM_KINDS,
    CLAIM_STATES,
    DECISION_STATUSES,
    MESSAGE_TYPES,
    OBJECTION_SEVERITIES,
    OBJECTION_STATUSES,
    POLICIES,
    ROLES,
    SESSION_STATUSES,
    SESSION_TRANSITIONS,
    STOP_REASONS,
    TASK_STATUSES,
    TOOL_PROFILES,
    TRANSITIONS,
    TURN_STATES,
    VISIBILITIES,
    CouncilBudgets,
    CouncilClaim,
    CouncilDecision,
    CouncilError,
    CouncilMessage,
    CouncilObjection,
    CouncilParticipant,
    CouncilSession,
    CouncilTask,
    CouncilTurn,
    can_write,
    check_transition,
    has_blocking,
    looks_like_impersonation,
    new_id,
    role_default_profile,
    supersede,
)
from .persistence import (  # noqa: F401
    COUNCIL_DB,
    ClaimConflict,
    CouncilStore,
    CouncilStoreError,
    DuplicateTurn,
    NotFound,
    RevisionConflict,
    store,
    use_path,
)

__all__ = [
    "AUTHOR_KINDS", "CLAIM_KINDS", "CLAIM_STATES", "DECISION_STATUSES",
    "MESSAGE_TYPES", "OBJECTION_SEVERITIES", "OBJECTION_STATUSES", "POLICIES",
    "ROLES", "SESSION_STATUSES", "SESSION_TRANSITIONS", "STOP_REASONS",
    "TASK_STATUSES", "TOOL_PROFILES", "TRANSITIONS", "TURN_STATES",
    "VISIBILITIES",
    "CouncilBudgets", "CouncilClaim", "CouncilDecision", "CouncilError",
    "CouncilMessage", "CouncilObjection", "CouncilParticipant",
    "CouncilSession", "CouncilTask", "CouncilTurn",
    "can_write", "check_transition", "has_blocking", "looks_like_impersonation",
    "new_id", "role_default_profile", "supersede",
    "COUNCIL_DB", "ClaimConflict", "CouncilStore", "CouncilStoreError",
    "DuplicateTurn", "NotFound", "RevisionConflict", "store", "use_path",
]
