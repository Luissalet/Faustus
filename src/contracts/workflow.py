"""
contracts/workflow.py — a process that has to survive a restart.

A chat turn can afford to be lost. A workflow that sends an email, renders an
hour of video or publishes something cannot: the failure mode is not "it
stopped", it is "it ran twice". So the contract is built around the two things
that make a second run harmless.

**An idempotency key per node, derived from the plan.** Two attempts at the
same node in the same run carry the same key, so the thing that actually sends
the email can refuse the second one without knowing anything about workflows.

**A node's outcome is written before the next node starts.** A run that comes
back after a crash reads what is already recorded rather than redoing it —
which is why `NodeRun` keeps its result, not just its status.

`paused` is a first-class state, not an error. A workflow waiting on a human
approval is working correctly; treating it as a failure is how a system starts
timing out the person it is asking.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from .base import (
    ContractError, SCHEMA_VERSION, as_mapping, fingerprint, flag, ident,
    now_iso, one_of, reject_unknown, text, text_list, timestamp, whole,
)

#: What a node can be. Closed: a node type nothing can execute is a comment.
NODE_TYPES = ("manual", "schedule", "webhook", "skill", "condition", "wait",
              "wait_until", "wait_for_event",
              "human_approval", "artifact_store", "deliver",
              "agent", "classify", "extract", "guard")

#: Types that reach outside — the ones where running twice is the real damage
#: and the idempotency key has to be honoured by whatever performs them.
#: `agent` is here because an agent turn may call tools (write a file, send a
#: message): a second run of the same turn is not a harmless re-read.
EFFECTFUL_TYPES = ("skill", "artifact_store", "deliver", "agent")

#: Nodes whose result names the branch that was taken (`result.branch`). A node
#: that lists one of these in `needs` may say `branch: {that_node: label}` and
#: then only runs when that label was chosen.
BRANCHING_TYPES = ("classify", "guard")

#: The two outcomes of a `guard` node.
GUARD_BRANCHES = ("pass", "fail")

WORKFLOW_STATUSES = ("pending", "running", "paused", "completed", "failed", "cancelled")

NODE_STATUSES = ("pending", "running", "paused", "completed", "failed",
                 "skipped", "cancelled")

TERMINAL_WORKFLOW = frozenset({"completed", "failed", "cancelled"})
TERMINAL_NODE = frozenset({"completed", "failed", "skipped", "cancelled"})


@dataclass(frozen=True)
class WorkflowNode:
    """One step. `config` is the type's own payload and is fingerprinted whole,
    so changing what a node does changes its idempotency key."""

    id: str
    type: str
    title: str = ""
    needs: Tuple[str, ...] = ()
    config: Mapping[str, Any] = field(default_factory=dict)
    max_attempts: int = 1
    continue_on_failure: bool = False
    #: `{needs_id: (label, ...)}` — run this node only when the named
    #: branching node (`classify`/`guard`) chose one of these labels. Empty
    #: for every node that has no branch gate, and then absent from
    #: `to_dict()` so a definition written before this field existed keeps
    #: the same fingerprint.
    branch: Mapping[str, Tuple[str, ...]] = field(default_factory=dict)

    _KEYS = ("id", "type", "title", "needs", "config", "max_attempts",
             "continue_on_failure", "branch")

    @classmethod
    def parse(cls, raw: Any, path: str = "node") -> "WorkflowNode":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        node_type = one_of(data, "type", path, choices=NODE_TYPES)
        config = data.get("config")
        if config is not None and not isinstance(config, Mapping):
            raise ContractError(f"{path}.config", "expected an object", got=config)
        attempts = whole(data, "max_attempts", path, default=1, minimum=1, maximum=10)
        if (node_type in EFFECTFUL_TYPES and attempts > 1
                and not (config or {}).get("idempotent")
                and not is_pure_reasoning(node_type, config)):
            # Retrying something that reaches outside is exactly how a
            # publication happens twice. It is allowed, but only when the
            # author says the effect can take it.
            raise ContractError(
                f"{path}.max_attempts",
                f"a '{node_type}' node reaches outside Faustus; retrying it needs "
                "`config.idempotent: true`, because the second attempt is the one "
                "that sends the email again",
                got=attempts,
            )
        return cls(
            id=ident(data, "id", path),
            type=node_type,
            title=text(data, "title", path, required=False, max_len=200),
            needs=text_list(data, "needs", path, max_items=32, max_len=128),
            config=dict(config or {}),
            max_attempts=attempts,
            continue_on_failure=flag(data, "continue_on_failure", path, default=False),
            branch=_parse_branch(data.get("branch"), f"{path}.branch"),
        )

    def to_dict(self) -> Dict[str, Any]:
        out = {"id": self.id, "type": self.type, "title": self.title,
               "needs": list(self.needs), "config": dict(self.config),
               "max_attempts": self.max_attempts,
               "continue_on_failure": self.continue_on_failure}
        if self.branch:
            out["branch"] = {dep: list(labels) for dep, labels in self.branch.items()}
        return out


def is_pure_reasoning(node_type: str, config: Optional[Mapping[str, Any]]) -> bool:
    """An `agent` node that was given an explicit empty tool list can only
    think and answer: it reaches nothing outside Faustus, so running it twice
    costs tokens, not a second email. Everything else of an effectful type is
    treated as reaching outside."""
    tools = (config or {}).get("tools")
    return node_type == "agent" and isinstance(tools, (list, tuple)) and not tools


def node_is_effectful(node: "WorkflowNode") -> bool:
    return node.type in EFFECTFUL_TYPES and not is_pure_reasoning(node.type, node.config)


def _parse_branch(raw: Any, path: str) -> Dict[str, Tuple[str, ...]]:
    """`{"classify-id": "billing"}` or `{"classify-id": ["billing", "refund"]}`
    → `{"classify-id": ("billing", "refund")}`. Shape only: whether the keys
    are real `needs` and the labels are declared is checked against the whole
    graph in `WorkflowDefinition.parse`."""
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ContractError(path, "expected an object of {needs_id: label or [labels]}", got=raw)
    out: Dict[str, Tuple[str, ...]] = {}
    for dep, labels in raw.items():
        if not isinstance(dep, str) or not dep:
            raise ContractError(path, "keys must be node ids", got=dep)
        values = [labels] if isinstance(labels, str) else labels
        if (not isinstance(values, (list, tuple)) or not values
                or any(not isinstance(v, str) or not v.strip() or len(v) > 120 for v in values)):
            raise ContractError(f"{path}.{dep}", "expected a label or a non-empty list of labels",
                                got=labels)
        out[dep] = tuple(dict.fromkeys(v.strip() for v in values))
    return out


def declared_branches(node: "WorkflowNode") -> Tuple[str, ...]:
    """The labels a branching node can choose between: a `classify` node's
    declared `config.labels`, or `pass`/`fail` for a `guard`. Empty for a node
    that does not branch — and for a `classify` whose labels are malformed,
    which is then reported where a branch names one of them."""
    if node.type == "guard":
        return GUARD_BRANCHES
    if node.type != "classify":
        return ()
    labels = (node.config or {}).get("labels")
    names = []
    if isinstance(labels, (list, tuple)):
        for item in labels:
            if isinstance(item, str) and item.strip():
                names.append(item.strip())
            elif isinstance(item, Mapping) and isinstance(item.get("name"), str) and item["name"].strip():
                names.append(item["name"].strip())
    return tuple(dict.fromkeys(names))


@dataclass(frozen=True)
class WorkflowDefinition:
    """The shape of the process, versioned.

    Validated as a graph, not a list: a `needs` that names nothing, or a cycle,
    is a definition that would hang at run time with no useful message. Better
    to refuse it while someone is still looking at the file."""

    id: str
    version: str
    title: str
    nodes: Tuple[WorkflowNode, ...] = ()
    description: str = ""
    schema_version: int = SCHEMA_VERSION

    _KEYS = ("id", "version", "title", "nodes", "description", "schema_version")

    @classmethod
    def parse(cls, raw: Any, path: str = "workflow") -> "WorkflowDefinition":
        from .base import semver
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        raw_nodes = data.get("nodes")
        if not isinstance(raw_nodes, (list, tuple)) or not raw_nodes:
            raise ContractError(f"{path}.nodes", "expected a non-empty list of nodes")

        nodes = tuple(WorkflowNode.parse(n, f"{path}.nodes[{i}]")
                      for i, n in enumerate(raw_nodes))
        ids = [n.id for n in nodes]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise ContractError(f"{path}.nodes", f"duplicate node ids: {duplicates}")

        known = set(ids)
        for node in nodes:
            unknown = sorted(set(node.needs) - known)
            if unknown:
                raise ContractError(
                    f"{path}.nodes[{node.id}].needs",
                    f"names {unknown}, which no node in this workflow defines")
            if node.id in node.needs:
                raise ContractError(f"{path}.nodes[{node.id}].needs",
                                    "a node cannot depend on itself")
            _check_branch(node, {n.id: n for n in nodes}, f"{path}.nodes[{node.id}].branch")
        cycle = _find_cycle(nodes)
        if cycle:
            raise ContractError(
                f"{path}.nodes",
                "these nodes depend on each other in a circle and nothing could "
                f"ever start: {' → '.join(cycle)}")

        return cls(
            id=ident(data, "id", path),
            version=semver(data, "version", path),
            title=text(data, "title", path, max_len=200),
            description=text(data, "description", path, required=False, max_len=2000),
            nodes=nodes,
            schema_version=whole(data, "schema_version", path,
                                 default=SCHEMA_VERSION, minimum=1),
        )


    def node(self, node_id: str) -> Optional[WorkflowNode]:
        return next((n for n in self.nodes if n.id == node_id), None)

    def roots(self) -> Tuple[str, ...]:
        return tuple(n.id for n in self.nodes if not n.needs)

    def to_dict(self) -> Dict[str, Any]:
        return {"schema_version": self.schema_version, "id": self.id,
                "version": self.version, "title": self.title,
                "description": self.description,
                "nodes": [n.to_dict() for n in self.nodes]}

    def fingerprint(self) -> str:
        return fingerprint([("id", self.id), ("version", self.version),
                            ("nodes", [n.to_dict() for n in self.nodes])])


def _check_branch(node: "WorkflowNode", by_id: Mapping[str, "WorkflowNode"], path: str) -> None:
    """A branch gate has to point at a real, branching dependency and name a
    label that dependency can actually produce — otherwise the node would sit
    behind a door nothing can ever open, and say nothing about why."""
    for dep, labels in node.branch.items():
        if dep not in node.needs:
            raise ContractError(f"{path}.{dep}",
                                f"a branch can only refer to a node listed in `needs`; "
                                f"{dep!r} is not one of {list(node.needs)}")
        source = by_id[dep]
        if source.type not in BRANCHING_TYPES:
            raise ContractError(f"{path}.{dep}",
                                f"{dep!r} is a '{source.type}' node and does not branch; only "
                                f"{', '.join(BRANCHING_TYPES)} nodes choose a label")
        known = declared_branches(source)
        unknown = [label for label in labels if label not in known]
        if unknown:
            raise ContractError(f"{path}.{dep}",
                                f"{source.type} node {dep!r} declares the branches {list(known)}; "
                                f"{unknown} is not one of them")


def _find_cycle(nodes: Sequence[WorkflowNode]) -> Tuple[str, ...]:
    """The nodes in one cycle, in order, or empty. Returning the path rather
    than a boolean is the difference between "there is a cycle" and a message
    someone can act on."""
    edges = {n.id: tuple(n.needs) for n in nodes}
    state: Dict[str, int] = {}          # 0 = visiting, 1 = done
    stack: list = []

    def walk(node_id: str) -> Tuple[str, ...]:
        if state.get(node_id) == 1:
            return ()
        if state.get(node_id) == 0:
            start = stack.index(node_id)
            return tuple(stack[start:] + [node_id])
        state[node_id] = 0
        stack.append(node_id)
        for dep in edges.get(node_id, ()):
            found = walk(dep)
            if found:
                return found
        stack.pop()
        state[node_id] = 1
        return ()

    for node in nodes:
        found = walk(node.id)
        if found:
            return found
    return ()


def idempotency_key(*, workflow_run_id: str, node_id: str,
                    config: Mapping[str, Any], inputs: Any = None) -> str:
    """What makes a second attempt safe.

    Derived from the run, the node and what the node was asked to do — never
    from the attempt number or the clock, because two attempts at the same work
    must produce the *same* key. That is the whole mechanism: the thing that
    sends the email refuses a key it has already seen, and does not need to
    know a workflow exists."""
    return fingerprint([
        ("run", workflow_run_id),
        ("node", node_id),
        ("config", dict(config or {})),
        ("inputs", inputs),
    ])


@dataclass(frozen=True)
class NodeRun:
    """One attempt's worth of truth about one node.

    `result` is kept, not only `status`, because that is what makes a restart
    cheap: a run that comes back reads what the node already produced instead
    of doing it again."""

    workflow_run_id: str
    node_id: str
    status: str = "pending"
    attempt: int = 0
    idempotency_key: str = ""
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    reason: str = ""
    approval_id: str = ""
    result: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    _KEYS = ("workflow_run_id", "node_id", "status", "attempt", "idempotency_key",
             "started_at", "ended_at", "reason", "approval_id", "result",
             "schema_version")

    @classmethod
    def parse(cls, raw: Any, path: str = "node_run") -> "NodeRun":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        status = one_of(data, "status", path, choices=NODE_STATUSES,
                        required=False, default="pending")
        result = data.get("result")
        if result is not None and not isinstance(result, Mapping):
            raise ContractError(f"{path}.result", "expected an object", got=result)
        ended = timestamp(data, "ended_at", path)
        if status in TERMINAL_NODE and not ended:
            raise ContractError(f"{path}.ended_at",
                                f"is required once a node is '{status}'")
        if status == "paused" and not text(data, "approval_id", path, required=False) \
                and not (result or {}).get("wake_at"):
            # Two things can end a pause: a person (an approval id) or the
            # clock (`result.wake_at`). Requiring one of them is the invariant;
            # requiring specifically an approval would make `wait` impossible
            # to express, and a pause nobody and nothing can resolve is a stall
            # with better manners.
            raise ContractError(
                f"{path}.approval_id",
                "a paused node has to say what will end the pause: an approval id "
                "for a person, or `result.wake_at` for a time")
        return cls(
            workflow_run_id=text(data, "workflow_run_id", path, max_len=64),
            node_id=ident(data, "node_id", path),
            status=status,
            attempt=whole(data, "attempt", path, default=0, minimum=0, maximum=100),
            idempotency_key=text(data, "idempotency_key", path, required=False, max_len=64),
            started_at=timestamp(data, "started_at", path),
            ended_at=ended,
            reason=text(data, "reason", path, required=False, max_len=1000),
            approval_id=text(data, "approval_id", path, required=False, max_len=64),
            result=dict(result or {}),
            schema_version=whole(data, "schema_version", path,
                                 default=SCHEMA_VERSION, minimum=1),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {"schema_version": self.schema_version,
                "workflow_run_id": self.workflow_run_id, "node_id": self.node_id,
                "status": self.status, "attempt": self.attempt,
                "idempotency_key": self.idempotency_key,
                "started_at": self.started_at, "ended_at": self.ended_at,
                "reason": self.reason, "approval_id": self.approval_id,
                "result": dict(self.result)}


@dataclass(frozen=True)
class WorkflowRun:
    """One execution of a definition, and the version it ran under.

    The version is stored, not looked up: a definition edited while a run is
    paused must not change what the rest of that run does. That is the same
    rule as an approval naming a skill version, for the same reason."""

    id: str
    workflow_id: str
    workflow_version: str
    definition_fingerprint: str = ""
    status: str = "pending"
    owner: str = ""
    project_id: str = ""
    trigger: str = "manual"
    created_at: str = ""
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    reason: str = ""
    inputs: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    _KEYS = ("id", "workflow_id", "workflow_version", "definition_fingerprint",
             "status", "owner", "project_id", "trigger", "created_at",
             "started_at", "ended_at", "reason", "inputs", "schema_version")

    @classmethod
    def parse(cls, raw: Any, path: str = "workflow_run") -> "WorkflowRun":
        from .base import semver
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        status = one_of(data, "status", path, choices=WORKFLOW_STATUSES,
                        required=False, default="pending")
        inputs = data.get("inputs")
        if inputs is not None and not isinstance(inputs, Mapping):
            raise ContractError(f"{path}.inputs", "expected an object", got=inputs)
        ended = timestamp(data, "ended_at", path)
        if status in TERMINAL_WORKFLOW and not ended:
            raise ContractError(f"{path}.ended_at", f"is required once a run is '{status}'")
        if status not in TERMINAL_WORKFLOW and ended:
            raise ContractError(f"{path}.ended_at", f"is set while the run is still '{status}'")
        return cls(
            id=text(data, "id", path, max_len=64),
            workflow_id=ident(data, "workflow_id", path),
            workflow_version=semver(data, "workflow_version", path),
            definition_fingerprint=text(data, "definition_fingerprint", path,
                                        required=False, max_len=64),
            status=status,
            owner=text(data, "owner", path, required=False, max_len=128),
            project_id=text(data, "project_id", path, required=False, max_len=128),
            trigger=one_of(data, "trigger", path,
                           choices=("manual", "schedule", "webhook", "event"),
                           required=False, default="manual"),
            created_at=timestamp(data, "created_at", path, default=now_iso()),
            started_at=timestamp(data, "started_at", path),
            ended_at=ended,
            reason=text(data, "reason", path, required=False, max_len=1000),
            inputs=dict(inputs or {}),
            schema_version=whole(data, "schema_version", path,
                                 default=SCHEMA_VERSION, minimum=1),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {"schema_version": self.schema_version, "id": self.id,
                "workflow_id": self.workflow_id,
                "workflow_version": self.workflow_version,
                "definition_fingerprint": self.definition_fingerprint,
                "status": self.status, "owner": self.owner,
                "project_id": self.project_id, "trigger": self.trigger,
                "created_at": self.created_at, "started_at": self.started_at,
                "ended_at": self.ended_at, "reason": self.reason,
                "inputs": dict(self.inputs)}
