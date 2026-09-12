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
from .workflow import (  # noqa: F401
    EFFECTFUL_TYPES, NODE_STATUSES, NODE_TYPES, TERMINAL_NODE, TERMINAL_WORKFLOW,
    WORKFLOW_STATUSES, NodeRun, WorkflowDefinition, WorkflowNode, WorkflowRun,
    idempotency_key,
)
from .changeset import (  # noqa: F401
    CHANGE_SOURCES, CLAIM_KINDS, INTENTS, READ_ONLY_INTENTS,
    ChangeSet, Claim, Command, FileChanges, Verification,
)
from .errors import (  # noqa: F401
    ERROR_CATEGORIES, ERROR_SUBCODES, ErrorInfo, from_exception,
)
from .tool import (  # noqa: F401
    CANCELLATION_MODES, EFFECT_CLASSES, EFFECT_STATES, EVIDENCE_LOCATOR_KINDS,
    EVIDENCE_RETENTION, EVIDENCE_SOURCE_TYPES, IDEMPOTENCY_MODES,
    SPEC_V2_SCHEMA_VERSION, TOOL_RESULT_STATUSES,
    EvidenceLocator, EvidenceRef, RetryPolicy, ToolDescriptor, ToolEffect,
    ToolInvocation, ToolRef, ToolResult, ToolUncertainty,
)
from .task import (  # noqa: F401
    ACCEPTANCE_STATES, APPROVAL_DECISION_SCOPES, APPROVAL_REQUEST_STATES,
    EVENT_ENVELOPE_TYPES, EVENT_ENVELOPE_VISIBILITY, PLAN_STEP_STATES,
    QUESTION_EXPIRY_POLICIES, TASK_STATES, TASK_TERMINAL, TASK_TRANSITIONS,
    AcceptanceCriterion, ApprovalRequest, Budget, Decision, EventEnvelope,
    PlanRevision, PlanStep, QuestionOption, QuestionRequest, TaskState,
    assert_transition,
)
from .inference import (  # noqa: F401
    ASSESSMENT_SCOPES, BENEFIT_STATES, CHECK_STATES, EFFECTIVE_STATES,
    EVIDENCE_KINDS, IDENTITY_STATES, IMPLEMENTATIONS, MANAGED_KINDS,
    METRIC_SCOPES, METRIC_SOURCES, MODEL_KINDS, SUPPORT_STATES,
    TOPOLOGY_STATES, VERIFY_STATES,
    Benefit, Check, CapabilityAssessment, Difference, EngineIdentity,
    Effective, Evidence, ExecutionMetrics, GpuInfo, HardwareSnapshot,
    LaunchReceipt, MetricValue, ModelDescriptor, Phases, RewriteStep, Tokens,
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
    "WorkflowDefinition", "WorkflowNode", "WorkflowRun", "NodeRun",
    "idempotency_key", "NODE_TYPES", "EFFECTFUL_TYPES", "NODE_STATUSES",
    "WORKFLOW_STATUSES", "TERMINAL_NODE", "TERMINAL_WORKFLOW",
    "ChangeSet", "Claim", "Command", "FileChanges", "Verification",
    "INTENTS", "READ_ONLY_INTENTS", "CHANGE_SOURCES", "CLAIM_KINDS",
    "ErrorInfo", "from_exception", "ERROR_CATEGORIES", "ERROR_SUBCODES",
    "ToolDescriptor", "ToolInvocation", "ToolResult", "ToolRef", "ToolEffect",
    "ToolUncertainty", "RetryPolicy", "EvidenceRef", "EvidenceLocator",
    "SPEC_V2_SCHEMA_VERSION", "EFFECT_CLASSES", "CANCELLATION_MODES",
    "IDEMPOTENCY_MODES", "TOOL_RESULT_STATUSES", "EFFECT_STATES",
    "EVIDENCE_SOURCE_TYPES", "EVIDENCE_LOCATOR_KINDS", "EVIDENCE_RETENTION",
    "TaskState", "PlanStep", "AcceptanceCriterion", "Decision", "Budget",
    "PlanRevision", "QuestionRequest", "QuestionOption", "ApprovalRequest",
    "EventEnvelope", "assert_transition", "TASK_STATES", "TASK_TRANSITIONS",
    "TASK_TERMINAL", "PLAN_STEP_STATES", "ACCEPTANCE_STATES",
    "QUESTION_EXPIRY_POLICIES", "APPROVAL_REQUEST_STATES",
    "APPROVAL_DECISION_SCOPES", "EVENT_ENVELOPE_TYPES", "EVENT_ENVELOPE_VISIBILITY",
    "EngineIdentity", "ModelDescriptor", "HardwareSnapshot", "GpuInfo",
    "CapabilityAssessment", "Effective", "Benefit", "Evidence",
    "LaunchReceipt", "RewriteStep", "Difference", "Check",
    "ExecutionMetrics", "MetricValue", "Phases", "Tokens",
    "IMPLEMENTATIONS", "MANAGED_KINDS", "MODEL_KINDS", "IDENTITY_STATES",
    "TOPOLOGY_STATES", "SUPPORT_STATES", "ASSESSMENT_SCOPES",
    "EFFECTIVE_STATES", "BENEFIT_STATES", "EVIDENCE_KINDS", "CHECK_STATES",
    "VERIFY_STATES", "METRIC_SOURCES", "METRIC_SCOPES",
]
