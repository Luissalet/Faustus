"""
src/contracts — the vocabulary the rest of Faustus agrees on.

Eight objects, from the masterplan: Identity, Project (still implicit, carried
as `project_id` until it earns a module), Skill, Run, ExecutionSpec, Artifact,
MemoryEntry/MemoryView and Event, plus Approval, which is what makes the other
seven safe to act on.

Nothing in this package touches the database, the filesystem, the network or
the model.  It parses, it validates, it fingerprints, and it refuses.  That is
deliberate: a contract that can reach a side effect is a contract you cannot
run in a test, and one nobody will trust to say no.
"""

from .base import (  # noqa: F401
    ContractError, SCHEMA_VERSION, fingerprint, now_iso,
)
from .skill import (  # noqa: F401
    APPROVAL_TRIGGERS, ARTIFACT_KINDS, MEMORY_SCOPES, MemoryPolicy,
    Permissions, SkillManifest, TypeSpec,
)
from .execution import (  # noqa: F401
    ATTENDED_ONLY, ISOLATION_LEVELS, REFUSED_REASONS, RESULT_STATUSES,
    ExecutionResult, ExecutionSpec, ResourceLimits,
)
from .run import (  # noqa: F401
    OUTCOME_OF, RUN_KINDS, STATUSES, TERMINAL, TRANSITIONS, Run, check_transition,
)
from .artifact import (  # noqa: F401
    RETENTION_POLICIES, Artifact, Provenance, Retention,
)
from .event import (  # noqa: F401
    EVENT_NAMES, Event, emit,
)
from .approval import (  # noqa: F401
    APPROVAL_STATUSES, PLAN_FIELDS, Approval, ApprovalPlan,
)
from .memory import (  # noqa: F401
    DROP_REASONS, MEMORY_SOURCES, TRUST_CLASSES, MemoryEntry, MemoryView,
)
from .identity import (  # noqa: F401
    CAPABILITIES, ExternalIdentity,
)

__all__ = [
    "SCHEMA_VERSION", "ContractError", "fingerprint", "now_iso",
    "SkillManifest", "Permissions", "MemoryPolicy", "TypeSpec",
    "APPROVAL_TRIGGERS", "ARTIFACT_KINDS", "MEMORY_SCOPES",
    "ExecutionSpec", "ResourceLimits", "ISOLATION_LEVELS", "ATTENDED_ONLY",
    "ExecutionResult", "RESULT_STATUSES", "REFUSED_REASONS",
    "Run", "check_transition", "STATUSES", "TERMINAL", "TRANSITIONS",
    "RUN_KINDS", "OUTCOME_OF",
    "Artifact", "Provenance", "Retention", "RETENTION_POLICIES",
    "Event", "emit", "EVENT_NAMES",
    "Approval", "ApprovalPlan", "APPROVAL_STATUSES", "PLAN_FIELDS",
    "MemoryEntry", "MemoryView", "TRUST_CLASSES", "MEMORY_SOURCES", "DROP_REASONS",
    "ExternalIdentity", "CAPABILITIES",
]
