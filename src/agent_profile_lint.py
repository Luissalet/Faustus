"""agent_profile_lint.py — a linter over REAL agent profiles and workflows.

Lote A4 (aigraphstudio): `lint_profile`/`lint_all` walk the five catalogue
types in `src/agent_profiles/catalog.py` (already schema-validated at
registration by `catalog._validate`) looking for configurations that are
*legal* but suspicious — the same distinction a style linter draws against a
compiler. `lint_workflow` does the equivalent for one
`src.contracts.workflow.WorkflowDefinition`, using Tarjan's strongly-connected
-components algorithm to catch a cycle even when the input bypassed
`WorkflowDefinition.parse()`'s own cycle rejection (a definition rebuilt by
hand, e.g. in a test or an older stored row, is exactly the case a linter
exists for).

Every rule below is anchored to a field that actually exists on the
dataclasses it inspects — no `max_iterations`, no "irreversible" flag, no
loop-node type are invented, because none of the three exist in this
codebase (`grep`ped before writing this module). Where the real schema has no
signal for something aigraphstudio's original design leans on, the rule is
simply not implemented, and the gap is called out in `docs/api/topology.md`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Sequence, Tuple

from src.agent_profiles import catalog
from src.contracts.workflow import EFFECTFUL_TYPES, WorkflowDefinition, WorkflowNode

__all__ = ["Finding", "lint_profile", "lint_all", "lint_workflow", "find_cycles"]

SEVERITIES: Tuple[str, ...] = ("info", "warn", "error")


@dataclass(frozen=True)
class Finding:
    """One lint result. `code` is a stable identifier (`LINT-...`) a caller
    can filter or suppress on; `subject` names what it is about, human
    enough to show directly and stable enough to diff between two runs."""

    code: str
    severity: str
    subject: str
    message: str
    hint: str = ""

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            raise ValueError(f"Finding.severity must be one of {SEVERITIES}, got {self.severity!r}")

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "severity": self.severity, "subject": self.subject,
                "message": self.message, "hint": self.hint}


def _get(profile: Any, name: str, default: Any = None) -> Any:
    """One field off a profile, whether `profile` is a catalogue dataclass or
    a plain dict (e.g. from `catalog.list_profiles`) — the two shapes carry
    the same fields, and a caller lints whichever one it has on hand."""
    if isinstance(profile, Mapping):
        return profile.get(name, default)
    return getattr(profile, name, default)


# ── profile rules (§9-§13 of the agent-profiles plan) ───────────────────────

#: The numeric ceilings a `BudgetProfile` declares. `0` is this codebase's own
#: "unlimited" sentinel — see `src/autonomy_budget.py`'s documented "0 =
#: unlimited" convention — so a profile whose ceiling is `0` was written to
#: uncap that dimension, not to forbid it.
_BUDGET_CEILINGS: Tuple[str, ...] = (
    "max_tokens", "max_seconds", "max_rounds", "max_tool_calls", "max_branches", "max_retries",
)


def _lint_budget(profile: Any) -> List[Finding]:
    pid = str(_get(profile, "id", "?"))
    zero_fields = [f for f in _BUDGET_CEILINGS if int(_get(profile, f, 0) or 0) == 0]
    findings: List[Finding] = []
    if bool(_get(profile, "allows_network", False)) and "max_tokens" in zero_fields:
        # Faustus principle: "never pass to paid silently". A profile that
        # both reaches the network and has no token ceiling is exactly the
        # shape that principle exists to catch.
        findings.append(Finding(
            code="LINT-BUDGET-UNCAPPED-NETWORK-SPEND", severity="error",
            subject=f"budget:{pid}",
            message="allows_network is true and max_tokens is 0 (this codebase's 'unlimited' "
                    "sentinel), so a run under this profile can spend without any token ceiling",
            hint="set a non-zero max_tokens, or turn allows_network off if this profile must stay free",
        ))
        zero_fields = [f for f in zero_fields if f != "max_tokens"]
    for field in zero_fields:
        findings.append(Finding(
            code="LINT-BUDGET-NO-CAP", severity="warn",
            subject=f"budget:{pid}.{field}",
            message=f"'{field}' is 0, which this codebase treats as unlimited",
            hint="set an explicit ceiling, or confirm unlimited is intended for this profile",
        ))
    return findings


def _lint_verification(profile: Any) -> List[Finding]:
    pid = str(_get(profile, "id", "?"))
    checks = tuple(_get(profile, "checks", ()) or ())
    blocking = tuple(_get(profile, "blocking", ()) or ())
    findings: List[Finding] = []
    stray = [c for c in blocking if c not in checks]
    if stray:
        findings.append(Finding(
            code="LINT-VERIF-BLOCKING-UNDECLARED", severity="error",
            subject=f"verification:{pid}",
            message=f"blocking names {stray}, which are not declared in checks",
            hint="add them to checks, or remove them from blocking",
        ))
    if not blocking:
        findings.append(Finding(
            code="LINT-VERIF-NO-BLOCKING", severity="warn",
            subject=f"verification:{pid}",
            message="no blocking checks: nothing declared here can stop a run",
            hint="add at least one check to blocking, e.g. 'prove'",
        ))
    return findings


def _lint_collaboration(profile: Any) -> List[Finding]:
    pid = str(_get(profile, "id", "?"))
    write_policy = str(_get(profile, "write_policy", "") or "")
    speak_policy = str(_get(profile, "speak_policy", "") or "")
    findings: List[Finding] = []
    if write_policy not in catalog.WRITE_POLICIES:
        findings.append(Finding(
            code="LINT-COLLAB-UNKNOWN-WRITE-POLICY", severity="error",
            subject=f"collaboration:{pid}",
            message=f"write_policy {write_policy!r} is not one of {list(catalog.WRITE_POLICIES)}",
            hint="use one of the known write policies",
        ))
    if speak_policy not in catalog.SPEAK_POLICIES:
        findings.append(Finding(
            code="LINT-COLLAB-UNKNOWN-SPEAK-POLICY", severity="error",
            subject=f"collaboration:{pid}",
            message=f"speak_policy {speak_policy!r} is not one of {list(catalog.SPEAK_POLICIES)}",
            hint="use one of the known speak policies",
        ))
    if write_policy in ("scoped_write", "write") and not bool(_get(profile, "review_own_work", True)):
        findings.append(Finding(
            code="LINT-COLLAB-WRITE-NO-REVIEW", severity="info",
            subject=f"collaboration:{pid}",
            message="this role can write and does not review its own work",
            hint="confirm an independent reviewer profile covers this role elsewhere",
        ))
    return findings


def _lint_output(profile: Any) -> List[Finding]:
    pid = str(_get(profile, "id", "?"))
    fields = tuple(_get(profile, "fields", ()) or ())
    required = tuple(_get(profile, "required", ()) or ())
    stray = [f for f in required if f not in fields]
    if not stray:
        return []
    return [Finding(
        code="LINT-OUTPUT-REQUIRED-UNDECLARED", severity="error",
        subject=f"output:{pid}",
        message=f"required names {stray}, which are not declared in fields",
        hint="add them to fields, or remove them from required",
    )]


def _lint_context(profile: Any) -> List[Finding]:
    pid = str(_get(profile, "id", "?"))
    findings: List[Finding] = []
    tokens = _get(profile, "budget_tokens", 0)
    if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens <= 0:
        findings.append(Finding(
            code="LINT-CONTEXT-NO-BUDGET", severity="error",
            subject=f"context:{pid}",
            message="budget_tokens is not a positive integer",
            hint="set budget_tokens to a positive integer",
        ))
    if not str(_get(profile, "engine_profile_id", "") or "").strip():
        findings.append(Finding(
            code="LINT-CONTEXT-NO-ENGINE-REF", severity="error",
            subject=f"context:{pid}",
            message="engine_profile_id is blank",
            hint="point it at a real Context Engine profile id",
        ))
    return findings


_PROFILE_LINTERS = {
    "verification": _lint_verification,
    "context": _lint_context,
    "budget": _lint_budget,
    "collaboration": _lint_collaboration,
    "output": _lint_output,
}

#: Kind -> the catalogue's own registry, walked by `lint_all`.
_PROFILE_REGISTRIES = {
    "verification": catalog.VERIFICATION_PROFILES,
    "context": catalog.CONTEXT_PROFILES,
    "budget": catalog.BUDGET_PROFILES,
    "collaboration": catalog.COLLABORATION_PROFILES,
    "output": catalog.OUTPUT_CONTRACTS,
}


def lint_profile(kind: str, profile: Any) -> List[Finding]:
    """Findings for one profile of one kind. `profile` may be a catalogue
    dataclass instance or an equivalent plain mapping."""
    linter = _PROFILE_LINTERS.get(str(kind or "").strip())
    if linter is None:
        raise ValueError(f"unknown profile kind {kind!r}; known kinds are {list(_PROFILE_LINTERS)}")
    return linter(profile)


def lint_all() -> List[Finding]:
    """Every finding for every profile currently registered in the catalogue,
    ordered by kind then id so two runs against the same catalogue diff
    cleanly."""
    findings: List[Finding] = []
    for kind in ("verification", "context", "budget", "collaboration", "output"):
        registry = _PROFILE_REGISTRIES[kind]
        for profile_id in sorted(registry):
            findings.extend(lint_profile(kind, registry[profile_id]))
    return findings


# ── workflow rules ───────────────────────────────────────────────────────────

def _as_workflow_definition(definition: Any) -> WorkflowDefinition:
    if isinstance(definition, WorkflowDefinition):
        return definition
    if isinstance(definition, Mapping):
        return WorkflowDefinition.parse(definition)
    raise TypeError("lint_workflow expects a WorkflowDefinition or a mapping")


def find_cycles(nodes: Sequence[WorkflowNode]) -> List[Tuple[str, ...]]:
    """Every strongly-connected component of size > 1 in the `needs` graph,
    plus any single node whose `needs` names itself — Tarjan's algorithm,
    iterative-recursive (fine here: workflow node counts are small and this
    runs at author time, not per request). Returned sorted by each cycle's
    smallest node id, for a stable diff between two lint runs.

    `WorkflowDefinition.parse()` already refuses both shapes at construction
    time, so this only ever fires on a definition assembled without going
    through `.parse()` — exactly the defensive case a linter is for.
    """
    adjacency: Dict[str, Tuple[str, ...]] = {n.id: tuple(dict.fromkeys(n.needs)) for n in nodes}
    index_counter = [0]
    indices: Dict[str, int] = {}
    lowlink: Dict[str, int] = {}
    on_stack: Dict[str, bool] = {}
    stack: List[str] = []
    sccs: List[Tuple[str, ...]] = []

    def strongconnect(v: str) -> None:
        indices[v] = index_counter[0]
        lowlink[v] = index_counter[0]
        index_counter[0] += 1
        stack.append(v)
        on_stack[v] = True
        for w in adjacency.get(v, ()):
            if w not in adjacency:
                continue  # a dangling reference is not this function's concern
            if w not in indices:
                strongconnect(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif on_stack.get(w):
                lowlink[v] = min(lowlink[v], indices[w])
        if lowlink[v] == indices[v]:
            component: List[str] = []
            while True:
                w = stack.pop()
                on_stack[w] = False
                component.append(w)
                if w == v:
                    break
            sccs.append(tuple(reversed(component)))

    for node in nodes:
        if node.id not in indices:
            strongconnect(node.id)

    cycles = []
    for scc in sccs:
        if len(scc) > 1:
            cycles.append(scc)
        elif scc[0] in adjacency.get(scc[0], ()):
            cycles.append(scc)  # a node that needs itself
    cycles.sort(key=lambda c: c[0])
    return cycles


def _ancestors(by_id: Mapping[str, WorkflowNode], node_id: str) -> set:
    """Every node reachable by following `needs` backward from `node_id`,
    not including `node_id` itself. Safe on a cyclic graph: each id is only
    ever pushed onto the walk once."""
    seen: set = set()
    stack = list(by_id[node_id].needs) if node_id in by_id else []
    while stack:
        current = stack.pop()
        if current in seen or current not in by_id:
            continue
        seen.add(current)
        stack.extend(by_id[current].needs)
    return seen


def _unreachable_nodes(wf: WorkflowDefinition) -> List[str]:
    """Node ids no chain of `needs` from a root (`needs == ()`) ever reaches
    — forward reachability over the same edges `needs` encodes. On a valid,
    `.parse()`-checked DAG this is always empty (every node's `needs` chain
    bottoms out at a root by induction); it only fires on a definition
    assembled by hand whose nodes form an island `.parse()` never saw."""
    ids = [n.id for n in wf.nodes]
    known = set(ids)
    forward: Dict[str, List[str]] = {i: [] for i in ids}
    for n in wf.nodes:
        for dep in n.needs:
            if dep in known:
                forward[dep].append(n.id)
    roots = [n.id for n in wf.nodes if not n.needs]
    seen = set(roots)
    stack = list(roots)
    while stack:
        current = stack.pop()
        for nxt in forward.get(current, ()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return [i for i in ids if i not in seen]


def lint_workflow(definition: Any) -> List[Finding]:
    """Findings for one workflow definition (a `WorkflowDefinition`, or a raw
    dict in the same shape `POST /api/workflows/validate` accepts — a dict is
    parsed first, so an invalid definition raises `ContractError` exactly as
    that route would, rather than being linted as if it were runnable)."""
    wf = _as_workflow_definition(definition)
    by_id = {n.id: n for n in wf.nodes}
    findings: List[Finding] = []

    for cycle in find_cycles(wf.nodes):
        findings.append(Finding(
            code="LINT-WF-CYCLE-NO-BOUND", severity="error",
            subject="workflow:" + "->".join(cycle),
            message="these nodes depend on each other in a circle (" + " -> ".join(cycle) +
                    ") with no bounded exit — no node type or field in this schema caps how many "
                    "times a cycle repeats, so it would never finish",
            hint="break the cycle, or route it through a `condition` node with a real exit edge",
        ))

    for node_id in _unreachable_nodes(wf):
        findings.append(Finding(
            code="LINT-WF-UNREACHABLE", severity="error",
            subject=f"node:{node_id}",
            message="no chain of `needs` from a root node ever reaches this node, so it can never start",
            hint="give it a `needs` path back to a root node, or remove it",
        ))

    for node in wf.nodes:
        if node.type not in ("deliver", "artifact_store"):
            continue
        ancestor_types = {by_id[a].type for a in _ancestors(by_id, node.id)}
        if not ({"condition", "human_approval"} & ancestor_types):
            findings.append(Finding(
                code="LINT-WF-OUTPUT-NO-EVALUATOR", severity="warn",
                subject=f"node:{node.id}",
                message=f"'{node.id}' ({node.type}) has no `condition` or `human_approval` node "
                        "anywhere upstream of it — nothing evaluates the run before this output",
                hint="add a condition or human_approval node ahead of it, or document why none is needed",
            ))

    for node in wf.nodes:
        if node.type not in EFFECTFUL_TYPES:
            continue
        if bool((node.config or {}).get("idempotent")):
            continue
        ancestor_types = {by_id[a].type for a in _ancestors(by_id, node.id)}
        if "human_approval" not in ancestor_types:
            findings.append(Finding(
                code="LINT-WF-EFFECT-NO-HUMAN", severity="warn",
                subject=f"node:{node.id}",
                message=f"'{node.id}' ({node.type}) reaches outside Faustus, is not marked "
                        "`config.idempotent`, and has no `human_approval` node upstream of it",
                hint="add a human_approval node ahead of it, or set `config.idempotent: true` "
                     "if repeating the effect is actually safe",
            ))

    return findings
