"""topology_export.py — Mermaid diagrams of real Faustus topology data.

Lote A4 (aigraphstudio): the two shapes worth drawing are a Branching Futures
tree (`future_to_mermaid`) and a workflow's node graph (`workflow_to_mermaid`).
Both read the SAME structures the rest of the codebase already persists and
validates (`src.branching_futures.service`'s future/branch dicts,
`src.contracts.workflow.WorkflowDefinition`) — this module never invents a
parallel drawing-only model, so the diagram cannot drift from what actually
ran.

Both functions are pure: given the same input they return the same string,
no I/O, no randomness, no clock.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Mapping, Tuple

from src.contracts.workflow import WorkflowDefinition, WorkflowNode

__all__ = ["future_to_mermaid", "workflow_to_mermaid"]


# ── shared escaping / id-safety helpers ─────────────────────────────────────

_UNSAFE_ID_CHARS = re.compile(r"[^A-Za-z0-9_]")


def _safe_id(raw: Any, used: Dict[str, str]) -> str:
    """A Mermaid-legal node id for `raw`, stable and unique within one call.

    Faustus ids (`branch_<hex>`, workflow node ids matching `_ID_RE` in
    `contracts/base.py`) already look like `[a-z0-9._-]+`; Mermaid node ids
    are safest as `[A-Za-z0-9_]+`, so `.` and `-` are folded to `_`. Folding
    can collide two different real ids (`a.b` and `a-b` both become `a_b`),
    so collisions are disambiguated with a numeric suffix rather than risking
    two different Faustus entities drawn as the same box.
    """
    key = str(raw)
    if key in used:
        return used[key]
    base = _UNSAFE_ID_CHARS.sub("_", key) or "n"
    if base[0].isdigit():
        base = "n_" + base
    candidate = base
    taken = set(used.values())
    suffix = 2
    while candidate in taken:
        candidate = f"{base}_{suffix}"
        suffix += 1
    used[key] = candidate
    return candidate


def _escape_label(text: Any) -> str:
    """Mermaid label text, safe inside a double-quoted node/edge label.

    Mermaid accepts HTML-style numeric/named character references inside a
    quoted label (its own docs use `#quot;` for a literal quote), so rather
    than stripping the characters that would otherwise break the grammar
    (`"`, `[`, `]`, `|`, `<`, `>`) they are escaped that way and stay visible
    in the rendered diagram.
    """
    value = str(text or "").replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    value = value.replace("&", "#38;")
    value = value.replace('"', "#quot;")
    value = value.replace("[", "#91;").replace("]", "#93;")
    value = value.replace("|", "#124;")
    value = value.replace("<", "#lt;").replace(">", "#gt;")
    return value.strip()


# ── Branching Futures: the tree of branches for one future ─────────────────

_BRANCH_STATUS_COLORS: Dict[str, str] = {
    "pending": "#9e9e9e",
    "running": "#1e88e5",
    "completed": "#2e7d32",
    "failed": "#c62828",
    "pruned": "#8d6e63",
    "cancelled": "#616161",
    "inconclusive": "#f9a825",
}


def _status_class(status: str) -> str:
    key = re.sub(r"[^a-z0-9]", "_", str(status or "").lower()) or "unknown"
    return f"bstatus_{key}"


def future_to_mermaid(future: Mapping[str, Any]) -> str:
    """A `flowchart TD` of one future: the root plus every branch.

    Reads exactly the shape `BranchingService.future()` returns (`id`,
    `title`, `status`, `branches`: a list of branch dicts with `id`,
    `status`, `strategy` and — for a fusion branch created by `service.fuse`
    — `parent_branch_ids`). A branch without `parent_branch_ids` is drawn as
    a direct child of the future; a fusion branch is drawn from EVERY one of
    its parents, which is the only place this tree is not a strict tree.
    """
    if not isinstance(future, Mapping):
        raise TypeError("future_to_mermaid expects a future mapping")

    used: Dict[str, str] = {}
    lines = ["flowchart TD"]

    future_id = str(future.get("id") or "future")
    safe_future = _safe_id(future_id, used)
    title = str(future.get("title") or future_id)
    status = str(future.get("status") or "")
    lines.append(
        f'    {safe_future}(["{_escape_label(title)}<br/>future: {_escape_label(status)}"])'
    )

    branches = future.get("branches")
    branches = branches if isinstance(branches, list) else []
    valid_branches = [b for b in branches if isinstance(b, Mapping) and b.get("id")]
    for branch in valid_branches:
        _safe_id(str(branch["id"]), used)

    for branch in valid_branches:
        bid = str(branch["id"])
        safe_b = used[bid]
        strategy = branch.get("strategy")
        strategy = strategy if isinstance(strategy, Mapping) else {}
        label = str(strategy.get("title") or strategy.get("id") or bid)
        b_status = str(branch.get("status") or "pending")
        lines.append(f'    {safe_b}["{_escape_label(label)}<br/>{_escape_label(b_status)}"]')
        lines.append(f"    class {safe_b} {_status_class(b_status)}")

        parents = branch.get("parent_branch_ids")
        parents = [str(p) for p in parents] if isinstance(parents, list) else []
        linked = False
        for parent in parents:
            parent_safe = used.get(parent)
            if parent_safe:
                lines.append(f"    {parent_safe} --> {safe_b}")
                linked = True
        if not linked:
            lines.append(f"    {safe_future} --> {safe_b}")

    lines.append("    classDef futureNode fill:#37474f,color:#fff;")
    lines.append(f"    class {safe_future} futureNode")
    for name, color in _BRANCH_STATUS_COLORS.items():
        lines.append(f"    classDef {_status_class(name)} fill:{color},color:#fff;")
    lines.append("    classDef bstatus_unknown fill:#bdbdbd,color:#000;")
    return "\n".join(lines)


# ── Workflows: the node graph of one definition ─────────────────────────────

#: Mermaid opening/closing bracket pair per Faustus node `type`
#: (`contracts.workflow.NODE_TYPES`), chosen so a glance at the shape says
#: what the node does: a diamond for the one node type that branches, a
#: parallelogram for the one that waits on a person, a cylinder for the one
#: that persists something, a subroutine box for the one effect that leaves
#: Faustus for good (`deliver`).
_NODE_SHAPES: Dict[str, Tuple[str, str]] = {
    "manual": ("[", "]"),
    "schedule": ("(", ")"),
    "webhook": ("{{", "}}"),
    "skill": ("[", "]"),
    "condition": ("{", "}"),
    "wait": ("([", "])"),
    "human_approval": ("[/", "/]"),
    "artifact_store": ("[(", ")]"),
    "deliver": ("[[", "]]"),
}

_NODE_TYPE_COLORS: Dict[str, str] = {
    "manual": "#546e7a",
    "schedule": "#00897b",
    "webhook": "#6d4c41",
    "skill": "#3949ab",
    "condition": "#f9a825",
    "wait": "#757575",
    "human_approval": "#8e24aa",
    "artifact_store": "#00695c",
    "deliver": "#2e7d32",
}


def _condition_label(node: WorkflowNode) -> str:
    """A short summary of a `condition` node's `config.when`, for the edges
    it gates. Falls back to the generic word when the shape is unexpected —
    `condition_handler` in `src/workflows/handlers.py` already owns rejecting
    a malformed `when`; this is a label, not a second validator."""
    when = node.config.get("when") if isinstance(node.config, Mapping) else None
    if not isinstance(when, Mapping):
        return "condition"
    left = when.get("left")
    if isinstance(left, Mapping) and left.get("path"):
        left_text = str(left.get("path"))
    elif left is not None:
        left_text = str(left)
    else:
        left_text = ""
    op = str(when.get("op") or "").strip()
    right = when.get("right")
    right_text = "" if isinstance(right, (Mapping, list)) else ("" if right is None else str(right))
    parts = [p for p in (left_text, op, right_text) if p]
    return (" ".join(parts) or "condition")[:60]


def _as_workflow_definition(definition: Any) -> WorkflowDefinition:
    if isinstance(definition, WorkflowDefinition):
        return definition
    if isinstance(definition, Mapping):
        return WorkflowDefinition.parse(definition)
    raise TypeError("workflow_to_mermaid expects a WorkflowDefinition or a mapping")


def workflow_to_mermaid(definition: Any) -> str:
    """A `flowchart TD` of one workflow definition's nodes and `needs` edges.

    `definition` may be an already-parsed `WorkflowDefinition` or a raw dict
    in the same shape `POST /api/workflows/validate` accepts — a dict is run
    through `WorkflowDefinition.parse` (same as every other workflow route),
    so an invalid definition raises `ContractError` here exactly as it would
    there, instead of drawing a diagram of something that could never run.

    An edge whose source is a `condition` node is labelled with that node's
    check (`config.when`), because that is the one edge type in this schema
    that is actually conditional — every other edge is a plain dependency.
    """
    wf = _as_workflow_definition(definition)
    used: Dict[str, str] = {}
    for node in wf.nodes:
        _safe_id(node.id, used)
    by_id = {node.id: node for node in wf.nodes}

    lines = ["flowchart TD"]
    for node in wf.nodes:
        safe = used[node.id]
        open_tok, close_tok = _NODE_SHAPES.get(node.type, ("[", "]"))
        label = node.title or node.id
        lines.append(
            f'    {safe}{open_tok}"{_escape_label(label)}<br/>{node.type}"{close_tok}'
        )
    for node in wf.nodes:
        safe = used[node.id]
        for dep in node.needs:
            dep_safe = used.get(dep)
            if not dep_safe:
                continue
            dep_node = by_id.get(dep)
            if dep_node is not None and dep_node.type == "condition":
                label = _escape_label(_condition_label(dep_node))
                lines.append(f'    {dep_safe} -->|"{label}"| {safe}')
            else:
                lines.append(f"    {dep_safe} --> {safe}")

    for node_type, color in _NODE_TYPE_COLORS.items():
        lines.append(f"    classDef nt_{node_type} fill:{color},color:#fff;")
    for node in wf.nodes:
        lines.append(f"    class {used[node.id]} nt_{node.type}")
    return "\n".join(lines)
