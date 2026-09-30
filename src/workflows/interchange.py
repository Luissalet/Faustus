"""
workflows/interchange.py — trading workflow definitions with the outside
world without pretending an unsupported design can run.

ADP-17 (aigraphstudio §6.2, "importación y exportación de gráficos"):

* `export_canonical` wraps a `WorkflowDefinition` in a small versioned
  envelope (`{schema_version, definition, layout, design_only, provenance}`)
  meant for round-tripping and for handing to a design tool. It never
  invents a second, drawing-only model of the definition: `layout` is
  node-id → `{x, y}` and nothing else, filtered to node ids the definition
  actually has, and `design_only` is opaque metadata this module never reads
  back into anything executable. `WorkflowDefinition.parse`/`.to_dict()` do
  all the real work, so a round trip through this envelope cannot change
  what the definition means (`tests/test_adp16_preflight_interchange.py`
  checks this with a fingerprint).

* `import_external` accepts either that canonical envelope, or a payload in
  the shape described below for `aigraphstudio` (`gcjordi/aigraphstudio`,
  MIT — `docs/adaptations/provenance.json` is owned by a different lot
  (W1-B/ADP-02) and, as of this file, has no entry yet for THIS module; see
  this lot's final report for the entry text to add there). Only six of its
  node types are translated into a Faustus node whose
  handler is a real, checked one in `src/workflows/handlers.py::
  default_handlers`. The plain ones: `Start`/`Input` → `manual`, `Output` →
  `artifact_store`, `Tool` → `skill`, `Human Approval` → `human_approval`,
  `Router` → `condition` (and only when its branch is a single `left`/`op`/
  `right` comparison this schema's own `condition_handler` can evaluate — see
  `_router_condition`). The model-driven ones, each only when the payload
  carries what the real node needs and never with a parameter nobody
  supplied: `Agent` → `agent` (needs a `prompt`), `LLM` → an `agent` that may
  use no tools (needs a `prompt`), `Evaluator` → `guard` (needs `text` and a
  list of deterministic `checks`), `Loop` → `loop` (needs `body` — node ids
  that themselves translate to an allowed body type — and a positive
  `max_iterations`; an optional `until` must be a single comparison). Every
  other named type (`Code`, `RAG`, `Memory`, `Parallel`, `Merge`, `Retry`), and
  any of the above missing what its node needs, becomes a `design_only` entry:
  kept for a human to look at, never translated into something that executes.
  A node whose declared predecessor did not itself survive translation is
  demoted to `design_only` too, cascading — see `_cascade_predecessors` —
  because giving an effectful node a `needs` list that silently drops its
  real prerequisite is exactly the "apariencia de ejecución a lo que no se
  soporta" ADP-17 exists to refuse. The same goes for the members of a `Loop`
  that could not be translated: they are not left behind as nodes that would
  run once, outside the loop that owned them.

**The assumed aigraphstudio JSON shape, and why it is assumed rather than
verified.** No fetch of `gcjordi/aigraphstudio`'s source was made for this
lot (COMUN rule: no external research this lote); the INFORME's §6.2 names
the node-type vocabulary (`Start, Input, Output, Tool, Agent, LLM, Code,
RAG, Memory, Human Approval, Router, Parallel, Merge, Loop, Retry,
Evaluator`) and its own contract table, but not a literal JSON schema. What
follows is this module's own mapping, built on that vocabulary and on the
ordinary node/edge shape a canvas tool like ADP-14's proposed React Flow
editor already uses elsewhere in this codebase's plans:

```json
{
  "schemaVersion": 1,
  "id": "optional-graph-id", "title": "optional title",
  "nodes": [
    {"id": "n1", "type": "Start", "data": {"label": "Begin"}},
    {"id": "n2", "type": "Tool", "data": {"tool": "web_search", "label": "Search"}},
    {"id": "n3", "type": "Router",
     "data": {"label": "score check",
              "condition": {"left": {"path": "inputs.score"}, "op": "gte", "right": 50}}},
    {"id": "n4", "type": "Human Approval", "data": {"label": "Approve publish"}},
    {"id": "n5", "type": "Output", "data": {"label": "Published report"}}
  ],
  "edges": [
    {"id": "e1", "source": "n1", "target": "n2"},
    {"id": "e2", "source": "n2", "target": "n3"},
    {"id": "e3", "source": "n3", "target": "n4"},
    {"id": "e4", "source": "n4", "target": "n5"}
  ]
}
```

If the real exporter's shape differs (a different key for an edge's
endpoints, a nested `condition`, …), `import_external` degrades to "no
recognizable node became executable" (`definition: None, executable:
false`) rather than guessing — never to a translation that looks right and
is not. Whoever wires a real aigraphstudio export up to this route should
diff a real exported file against the shape above first.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional, Sequence

from src.contracts.workflow import WorkflowDefinition
from src.workflows.handlers import OPERATORS

__all__ = ["INTERCHANGE_SCHEMA_VERSION", "export_canonical", "import_external"]

#: This module's own envelope version — distinct from `WorkflowDefinition
#: .schema_version`, which versions the definition, not the envelope around it.
INTERCHANGE_SCHEMA_VERSION = 1

#: aigraphstudio node type -> Faustus node type, ONLY for types with a real,
#: checked handler in `src/workflows/handlers.py::default_handlers`. Every
#: other named type in the INFORME's vocabulary is `design_only` — see
#: `_AIGS_DESIGN_ONLY_TYPES`.
_AIGS_MAPPABLE_TYPES: Dict[str, str] = {
    "Start": "manual",
    "Input": "manual",
    "Output": "artifact_store",
    "Tool": "skill",
    "Human Approval": "human_approval",
    "Router": "condition",
}

#: Model-driven and iterative types that DO have a Faustus node now, but only
#: when the payload supplies what that node requires (see `_model_node`,
#: `_resolve_loops`); without it they fall back to `design_only`.
_AIGS_MODEL_TYPES = frozenset({"Agent", "LLM", "Evaluator", "Loop"})

#: No Faustus node type to translate to: `Retry` is a per-node `max_attempts`
#: (not a node), `Parallel`/`Merge` are concurrency this engine's contract does
#: not model (the engine "advances on nodes that are ready"; drawing two arrows
#: out of one node is not proof two run at once), and `Code`/`RAG`/`Memory` have
#: no single node that means them without guessing parameters nobody supplied.
_AIGS_DESIGN_ONLY_TYPES = frozenset({
    "Code", "RAG", "Memory", "Parallel", "Merge", "Retry",
})

_SAFE_ID_RE = re.compile(r"[^a-z0-9]+")
_SEMVER_RE = re.compile(
    r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)


def _as_workflow_definition(definition: Any) -> WorkflowDefinition:
    if isinstance(definition, WorkflowDefinition):
        return definition
    if isinstance(definition, Mapping):
        return WorkflowDefinition.parse(definition)
    raise TypeError("export_canonical expects a WorkflowDefinition or a mapping")


def _safe_id(raw: Any, used: Dict[str, str]) -> str:
    """A `src.contracts.base._ID_RE`-legal id for an external identifier,
    stable and collision-free within one import. Faustus ids are lowercase
    `[a-z0-9]` groups joined by single `-`/`.`/`_`; an external id can be
    anything JSON allows, so every run of non-`[a-z0-9]` characters folds to
    one `-` (never a bare separator run, which `_ID_RE` also refuses) and a
    collision after folding gets a numeric suffix rather than silently
    reusing another node's id."""
    key = str(raw)
    if key in used:
        return used[key]
    folded = _SAFE_ID_RE.sub("-", key.strip().lower()).strip("-")
    base = (folded or "n")[:100]
    candidate = base
    taken = set(used.values())
    suffix = 2
    while candidate in taken:
        candidate = f"{base}-{suffix}"
        suffix += 1
    used[key] = candidate
    return candidate


def _trunc(value: str, limit: int) -> str:
    value = value.strip()
    return value[:limit] if len(value) > limit else value


def _coerce_version(value: Any) -> str:
    if isinstance(value, str) and _SEMVER_RE.fullmatch(value.strip()):
        return value.strip()
    return "0.0.0"


# ── export ───────────────────────────────────────────────────────────────

def export_canonical(
    definition: Any,
    layout: Optional[Mapping[str, Mapping[str, Any]]] = None,
    *,
    design_only: Optional[Sequence[Mapping[str, Any]]] = None,
    provenance: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """The canonical, versioned envelope `import_external` recognises on the
    way back in. `layout` and `design_only` are visual/informational only —
    neither is read by `WorkflowDefinition.parse`, so wrapping and
    unwrapping a definition through this function cannot change what it
    means. `layout` entries are kept only for node ids the definition
    actually has, and only when both coordinates are real numbers — an
    unknown node id or a non-numeric position is dropped rather than passed
    through to whatever renders it."""
    wf = _as_workflow_definition(definition)
    known_ids = {n.id for n in wf.nodes}
    clean_layout: Dict[str, Dict[str, float]] = {}
    if isinstance(layout, Mapping):
        for node_id, pos in layout.items():
            if node_id not in known_ids or not isinstance(pos, Mapping):
                continue
            x, y = pos.get("x"), pos.get("y")
            if (isinstance(x, (int, float)) and not isinstance(x, bool)
                    and isinstance(y, (int, float)) and not isinstance(y, bool)):
                clean_layout[str(node_id)] = {"x": float(x), "y": float(y)}
    return {
        "schema_version": INTERCHANGE_SCHEMA_VERSION,
        "definition": wf.to_dict(),
        "layout": clean_layout,
        "design_only": [dict(row) for row in (design_only or []) if isinstance(row, Mapping)],
        "provenance": dict(provenance) if isinstance(provenance, Mapping) else {},
    }


# ── import ───────────────────────────────────────────────────────────────

def _looks_canonical(payload: Mapping[str, Any]) -> bool:
    d = payload.get("definition")
    return isinstance(d, Mapping) and isinstance(d.get("nodes"), list)


def _looks_aigraphstudio(payload: Mapping[str, Any]) -> bool:
    return payload.get("schemaVersion") == 1 and isinstance(payload.get("nodes"), list)


def _no_definition(reason: str) -> Dict[str, Any]:
    return {"definition": None, "design_only": [], "rejected": [{"reason": reason}],
            "executable": False}


def _import_canonical(payload: Mapping[str, Any]) -> Dict[str, Any]:
    raw_def = payload["definition"]
    try:
        wf = WorkflowDefinition.parse(raw_def)
    except Exception as exc:  # ContractError, but a malformed envelope can
        # also raise TypeError from as_mapping — either way this is a
        # rejection, not a crash.
        path = getattr(exc, "path", "definition")
        message = getattr(exc, "message", str(exc))
        return _no_definition(f"{path}: {message}")
    design_only = payload.get("design_only")
    design_only = [dict(r) for r in design_only if isinstance(r, Mapping)] \
        if isinstance(design_only, list) else []
    return {"definition": wf.to_dict(), "design_only": design_only,
            "rejected": [], "executable": True}


_NO_SIDE = object()


def _safe_side(value: Any) -> Any:
    """One side of a `condition.left`/`right`: a path reference or a JSON
    primitive, same shapes `src/workflows/handlers.py::_side` reads. Anything
    else (a list, a nested object with no `path`) comes back as `_NO_SIDE` —
    "not expressible", not "guess and hope"."""
    if isinstance(value, Mapping):
        path = value.get("path")
        if isinstance(path, str) and path:
            return {"path": path[:512]}
        return _NO_SIDE
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return _NO_SIDE


def _router_condition(data: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """A Router node's branch, translated to `condition_handler`'s
    `{left, op, right}` ONLY when it already is exactly that shape. The
    INFORME is explicit that a router/condition needs its branch semantics
    validated against the real handler, and an incomplete equivalence must
    be refused rather than translated — so anything not already a single
    comparison (multiple outgoing branches, a free-text rule, an unknown
    operator) returns `None` and the caller keeps the node `design_only`."""
    src = data.get("condition")
    if not isinstance(src, Mapping):
        src = data.get("when") if isinstance(data.get("when"), Mapping) else None
    if not isinstance(src, Mapping):
        return None
    op = src.get("op")
    if not isinstance(op, str) or op not in OPERATORS:
        return None
    left = _safe_side(src.get("left"))
    if left is _NO_SIDE:
        return None
    when: Dict[str, Any] = {"op": op, "left": left}
    if op not in ("exists", "truthy"):
        right = _safe_side(src.get("right"))
        if right is _NO_SIDE:
            return None
        when["right"] = right
    return when


def _model_node(ext_type: str, data: Mapping[str, Any]):
    """`(faustus_type, config, "")` for an Agent/LLM/Evaluator that carries what
    the real node needs, or `(None, {}, reason)`."""
    from src.workflows import schema_check
    if ext_type in ("Agent", "LLM"):
        prompt = data.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return None, {}, (f"this '{ext_type}' has no `prompt`; an agent node runs one prompt and "
                              "guessing one would be inventing what it does")
        config: Dict[str, Any] = {"prompt": prompt.strip()[:20000]}
        if ext_type == "LLM":
            config["tools"] = []
        else:
            profile = data.get("agent") or data.get("profile")
            if isinstance(profile, str) and profile.strip():
                config["agent"] = profile.strip()[:120]
            tools = data.get("tools")
            if isinstance(tools, list) and all(isinstance(t, str) and t.strip() for t in tools):
                config["tools"] = [t.strip() for t in tools]
        schema = data.get("output_schema") or data.get("schema")
        if isinstance(schema, Mapping) and not schema_check.schema_problems(schema):
            config["output_schema"] = dict(schema)
        rounds = data.get("max_rounds")
        if isinstance(rounds, int) and not isinstance(rounds, bool) and 1 <= rounds <= 50:
            config["max_rounds"] = rounds
        return "agent", config, ""
    # Evaluator
    text = data.get("text")
    checks = data.get("checks")
    known = ("secrets", "pii", "urls", "injection")
    if not isinstance(text, str) or not text.strip():
        return None, {}, "this 'Evaluator' has no `text` to check"
    if not isinstance(checks, list) or not checks or not all(isinstance(c, str) and c in known for c in checks):
        return None, {}, (f"this 'Evaluator' has no `checks` from {list(known)}; a guard checks something "
                          "specific, not a free-text rubric")
    return "guard", {"text": text.strip()[:20000], "checks": list(checks)}, ""


def _loop_spec(data: Mapping[str, Any]):
    """`(body external ids, config without body, "")` or `(None, {}, reason)`."""
    body = data.get("body")
    if not isinstance(body, list) or not body or not all(isinstance(b, (str, int)) for b in body):
        return None, {}, "this 'Loop' names no `body` (the ids of the nodes it repeats)"
    ceiling = data.get("max_iterations")
    if isinstance(ceiling, bool) or not isinstance(ceiling, int) or ceiling < 1:
        return None, {}, ("this 'Loop' has no positive `max_iterations`; a loop without a ceiling is the "
                          "one thing Faustus refuses")
    config: Dict[str, Any] = {"budget": {"max_iterations": ceiling}}
    if data.get("until") is not None:
        when = _router_condition({"condition": data.get("until")})
        if when is None:
            return None, {}, "this 'Loop' has an `until` that is not a single left/op/right comparison"
        config["until"] = when
    return [str(b) for b in body], config, ""


def _resolve_loops(loop_specs: Dict[str, Any], dead_loops: Mapping[str, Any], used_ids: Mapping[str, str],
                   classify: Dict[str, str],
                   mapped_type: Dict[str, str], mapped_config: Dict[str, Dict[str, Any]],
                   predecessors: Dict[str, set], external_of: Mapping[str, Dict[str, str]],
                   mapped_title: Mapping[str, str], design_only_entries: List[Dict[str, Any]]) -> None:
    """Give each translatable Loop its body, and demote a Loop (and its body)
    that cannot have one. Body members wait on what the loop waits for plus
    their siblings; whatever waited on a member waits on the loop."""
    from src.contracts.workflow import LOOP_BODY_TYPES
    claimed: Dict[str, str] = {}
    body_of: Dict[str, List[str]] = {}
    failed: Dict[str, str] = {}
    body_ext: Dict[str, List[str]] = {safe: spec[0] for safe, spec in loop_specs.items()}
    body_ext.update({safe: list(ids) for safe, ids in dead_loops.items()})
    for safe, (ext_body, config) in loop_specs.items():
        members: List[str] = []
        why = ""
        for ext in ext_body:
            member = used_ids.get(ext)
            if member is None or member == safe:
                why = f"its body names {ext!r}, which is not a node of this graph"
            elif classify.get(member) != "executable":
                why = f"its body node {ext!r} was not translated into an executable node"
            elif mapped_type.get(member) not in LOOP_BODY_TYPES:
                why = (f"its body node {ext!r} became a '{mapped_type.get(member)}' node; a loop body may "
                       f"only use {', '.join(LOOP_BODY_TYPES)}")
            elif member in claimed:
                why = f"its body node {ext!r} already belongs to another loop"
            if why:
                break
            members.append(member)
        if why:
            failed[safe] = why
            continue
        for member in members:
            claimed[member] = safe
        body_of[safe] = members
        config["body"] = members
    def demote_body(safe: str) -> None:
        for ext in body_ext.get(safe, ()):
            member = used_ids.get(ext)
            if member and classify.get(member) == "executable" and member not in claimed:
                classify[member] = "design_only"
                inner = external_of.get(member, {"id": member, "type": ""})
                design_only_entries.append({
                    "id": member, "external_id": inner["id"], "type": inner["type"],
                    "label": mapped_title.get(member, member),
                    "reason": f"belongs to the loop {safe}, which could not be translated; left alone it "
                              "would run once, outside the loop that owned it"})
                mapped_type.pop(member, None)
                mapped_config.pop(member, None)

    for safe, why in failed.items():
        classify[safe] = "design_only"
        ext = external_of.get(safe, {"id": safe, "type": "Loop"})
        design_only_entries.append({"id": safe, "external_id": ext["id"], "type": ext["type"],
                                    "label": mapped_title.get(safe, safe),
                                    "reason": f"this loop cannot run as drawn: {why}"})
        mapped_type.pop(safe, None)
        mapped_config.pop(safe, None)
        demote_body(safe)
    for safe in dead_loops:
        demote_body(safe)
    for loop_id, members in body_of.items():
        roots = set(predecessors.get(loop_id, ())) - set(members)
        for member in members:
            inside = {p for p in predecessors.get(member, ()) if p in members}
            predecessors[member] = set(roots) | inside
        for node_id, preds in list(predecessors.items()):
            if node_id in members:
                continue
            if preds & set(members):
                preds.difference_update(members)
                if node_id != loop_id:
                    preds.add(loop_id)
        predecessors[loop_id] = roots


def _import_aigraphstudio(payload: Mapping[str, Any]) -> Dict[str, Any]:
    raw_nodes = payload.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        return _no_definition("nodes must be a non-empty list")
    raw_edges = payload.get("edges")
    edges = raw_edges if isinstance(raw_edges, list) else []

    used_ids: Dict[str, str] = {}
    classify: Dict[str, str] = {}           # safe id -> executable | design_only
    external_of: Dict[str, Dict[str, str]] = {}
    mapped_type: Dict[str, str] = {}
    mapped_config: Dict[str, Dict[str, Any]] = {}
    mapped_title: Dict[str, str] = {}
    design_only_entries: List[Dict[str, Any]] = []
    rejected_entries: List[Dict[str, Any]] = []
    has_unknown_type = False
    loop_specs: Dict[str, Any] = {}
    dead_loops: Dict[str, List[str]] = {}

    seen_external_ids: set = set()
    for i, raw in enumerate(raw_nodes):
        if not isinstance(raw, Mapping) or not raw.get("id"):
            rejected_entries.append({"index": i, "reason": "node is missing an id"})
            continue
        ext_id = str(raw["id"])
        if ext_id in seen_external_ids:
            # `_safe_id` would otherwise fold a repeated external id onto the
            # SAME Faustus id and silently let the second node overwrite the
            # first's type/config — a real ambiguity in the source graph,
            # not something to guess a winner for.
            rejected_entries.append({"index": i, "external_id": ext_id,
                                     "reason": "duplicate node id in the source graph"})
            continue
        seen_external_ids.add(ext_id)
        ext_type = str(raw.get("type") or "")
        safe = _safe_id(ext_id, used_ids)
        external_of[safe] = {"id": ext_id, "type": ext_type}
        data = raw.get("data") if isinstance(raw.get("data"), Mapping) else {}
        label = _trunc(str(data.get("label") or raw.get("label") or ext_id) or ext_id, 200)
        mapped_title[safe] = label

        if ext_type in _AIGS_MODEL_TYPES:
            if ext_type == "Loop":
                ext_body, loop_config, why = _loop_spec(data)
                if ext_body is not None:
                    loop_specs[safe] = (ext_body, loop_config)
                    classify[safe] = "executable"
                    mapped_type[safe] = "loop"
                    mapped_config[safe] = loop_config
                    continue
                if isinstance(data.get("body"), list):
                    dead_loops[safe] = [str(b) for b in data["body"] if isinstance(b, (str, int))]
            else:
                model_type, model_config, why = _model_node(ext_type, data)
                if model_type is not None:
                    classify[safe] = "executable"
                    mapped_type[safe] = model_type
                    mapped_config[safe] = model_config
                    continue
            classify[safe] = "design_only"
            design_only_entries.append({"id": safe, "external_id": ext_id, "type": ext_type, "label": label,
                                        "reason": why})
            continue

        if ext_type in _AIGS_DESIGN_ONLY_TYPES:
            classify[safe] = "design_only"
            design_only_entries.append({
                "id": safe, "external_id": ext_id, "type": ext_type, "label": label,
                "reason": f"'{ext_type}' has no Faustus node type with a checked handler; "
                          "kept as design metadata, never translated into anything executable",
            })
            continue

        if ext_type not in _AIGS_MAPPABLE_TYPES:
            classify[safe] = "unknown_type"
            has_unknown_type = True
            rejected_entries.append({"id": safe, "external_id": ext_id, "type": ext_type,
                                     "reason": f"unrecognized node type {ext_type!r}"})
            continue

        faustus_type = _AIGS_MAPPABLE_TYPES[ext_type]
        config: Dict[str, Any] = {}
        if faustus_type == "skill":
            tool_name = _trunc(str(data.get("tool") or data.get("skill") or label), 200)
            config["skill"] = tool_name
        elif faustus_type == "human_approval":
            config["action"] = _trunc(str(data.get("action") or "review"), 200)
            config["detail"] = label
        elif faustus_type == "condition":
            when = _router_condition(data)
            if when is None:
                classify[safe] = "design_only"
                design_only_entries.append({
                    "id": safe, "external_id": ext_id, "type": ext_type, "label": label,
                    "reason": "this Router's branch is not a single left/op/right condition "
                              "Faustus's condition_handler can evaluate; translating it anyway "
                              "would be exactly the incomplete equivalence ADP-17 refuses",
                })
                continue
            config["when"] = when

        classify[safe] = "executable"
        mapped_type[safe] = faustus_type
        mapped_config[safe] = config

    # Adjacency restricted to nodes actually seen; an edge naming an id this
    # payload never declared as a node is simply not a real edge.
    predecessors: Dict[str, set] = {}
    for edge in edges:
        if not isinstance(edge, Mapping):
            continue
        source, target = edge.get("source"), edge.get("target")
        if not isinstance(source, str) or not isinstance(target, str):
            continue
        src_safe, dst_safe = used_ids.get(source), used_ids.get(target)
        if not src_safe or not dst_safe or src_safe == dst_safe:
            continue
        predecessors.setdefault(dst_safe, set()).add(src_safe)

    _resolve_loops(loop_specs, dead_loops, used_ids, classify, mapped_type, mapped_config, predecessors,
                   external_of, mapped_title, design_only_entries)
    _cascade_predecessors(mapped_type, mapped_config, classify, predecessors,
                          external_of, mapped_title, design_only_entries)

    final_nodes = [
        {"id": safe, "type": ftype, "title": mapped_title[safe],
         "needs": sorted(p for p in predecessors.get(safe, ()) if classify.get(p) == "executable"),
         "config": mapped_config[safe]}
        for safe, ftype in mapped_type.items()
    ]

    definition_dict = None
    if final_nodes:
        graph_id = _safe_id(str(payload.get("id") or payload.get("title") or "imported-graph"), {})
        title = _trunc(str(payload.get("title") or payload.get("id") or "Imported graph")
                       or "Imported graph", 200)
        candidate = {"id": graph_id, "version": _coerce_version(payload.get("version")),
                     "title": title, "nodes": final_nodes}
        try:
            wf = WorkflowDefinition.parse(candidate)
            definition_dict = wf.to_dict()
        except Exception as exc:
            # A cycle among the executable subset, or a dangling `needs` —
            # `_find_cycle` inside `.parse()` is never bypassed here, so this
            # is the same refusal `/api/workflows/validate` would give.
            path = getattr(exc, "path", "definition")
            message = getattr(exc, "message", str(exc))
            rejected_entries.append({"reason": f"{path}: {message}"})

    if has_unknown_type and definition_dict is not None:
        rejected_entries.append({
            "reason": "at least one node in the source graph had an unrecognized type; "
                      "the graph is not enabled to execute even though the mapped subset "
                      "on its own would parse",
        })

    executable = definition_dict is not None and not has_unknown_type
    return {"definition": definition_dict, "design_only": design_only_entries,
            "rejected": rejected_entries, "executable": executable}


def _cascade_predecessors(
    mapped_type: Dict[str, str],
    mapped_config: Dict[str, Dict[str, Any]],
    classify: Dict[str, str],
    predecessors: Mapping[str, set],
    external_of: Mapping[str, Dict[str, str]],
    mapped_title: Mapping[str, str],
    design_only_entries: List[Dict[str, Any]],
) -> None:
    """An executable node whose recorded predecessor did NOT itself survive
    translation is demoted to `design_only` too, and the demotion cascades
    (a node two hops downstream of an unsupported one is caught on the next
    pass). The alternative — dropping just the one unsupported edge and
    letting the node become a root, or letting it silently `need` nothing —
    would run an effectful node without whatever the original graph actually
    gated it on. That is the "apariencia de ejecución a lo que no se
    soporta" ADP-17's acceptance criteria exist to catch, so this refuses it
    even though it means importing less than the source graph drew."""
    changed = True
    while changed:
        changed = False
        for safe in list(mapped_type):
            preds = predecessors.get(safe, ())
            bad = sorted(p for p in preds if classify.get(p) != "executable")
            if not bad:
                continue
            classify[safe] = "design_only"
            ext = external_of.get(safe, {"id": safe, "type": ""})
            design_only_entries.append({
                "id": safe, "external_id": ext["id"], "type": ext["type"],
                "label": mapped_title.get(safe, safe),
                "reason": "depends on " + ", ".join(bad) + ", which was not translated into "
                          "an executable node; running this node without its real "
                          "prerequisite is not something this importer will fake",
            })
            del mapped_type[safe]
            del mapped_config[safe]
            changed = True


def import_external(payload: Any) -> Dict[str, Any]:
    """Import a workflow definition drafted elsewhere.

    Accepts (a) this module's own canonical envelope
    (`{schema_version, definition, ...}` — see `export_canonical`), or (b)
    an aigraphstudio-shaped payload (`schemaVersion: 1`, `nodes`, `edges` —
    see the module docstring for the assumed shape and why it is assumed).
    Anything else is refused outright with `executable: false` and no
    guessing at a third format.

    Returns `{"definition": dict | None, "design_only": [...],
    "rejected": [...], "executable": bool}`. `definition`, when present, has
    already been through `WorkflowDefinition.parse()` — the exact same
    validation (including `_find_cycle`, never bypassed) `POST
    /api/workflows/validate` applies to a hand-written definition, so an
    import and a hand-written definition that describe the same graph
    produce the same verdict.
    """
    if not isinstance(payload, Mapping):
        return _no_definition("top-level payload must be a JSON object")
    if _looks_canonical(payload):
        return _import_canonical(payload)
    if _looks_aigraphstudio(payload):
        return _import_aigraphstudio(payload)
    return _no_definition(
        "unrecognized interchange format: expected the canonical envelope "
        "(schema_version + definition) or an aigraphstudio-shaped payload "
        "(schemaVersion: 1 + nodes)"
    )
