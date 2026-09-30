"""
workflows/published.py — saved workflows, offered as tools to outside callers.

A workflow that is saved (`library.py`), enabled, and declares an `inputs`
JSON schema becomes a tool:

* **named from the workflow** (`wf_<name>`), **described from the workflow**
  (its description, else its title, plus the declared inputs);
* **its arguments are the workflow's declared inputs**, checked against the
  schema before anything starts, with `default`s filled in;
* plus three reserved arguments — `overrides`, `wait_seconds`,
  `idempotency_key` — that no workflow input may be called.

**`overrides`** override the `config` of named nodes for that one run:
`{"node_id": {"field": value}}`. Two gates, both closed by default:
the field must be on the whitelist below (prompts, thresholds, ceilings — never
a tool list, a recipient, a skill id, a check, a label or a profile, which are
what a workflow's author decided and what a caller must not widen), and its
value must pass that field's schema. A loop's ceilings can only be lowered.
The owner can switch overrides off per workflow (`allow_overrides`). The overridden
definition is what the run snapshots, so what ran is visible in the run.

**A call starts a run and returns.** It answers with the run id and, if the run
finished (or stopped to wait for somebody) within the wait budget, its result;
otherwise the run keeps going in the background and `run_status` is the way
back to it. The wait is a convenience, never the run's clock.

Everything here is owner-scoped: one owner's tools are not another's.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from src.contracts import ContractError, WorkflowDefinition
from src.contracts.workflow import TERMINAL_WORKFLOW, loop_body_ids

from . import schema_check
from .library import RESERVED_INPUTS, TOOL_PREFIX, WorkflowLibrary

logger = logging.getLogger(__name__)

__all__ = ["PublishError", "OVERRIDABLE", "tool_specs", "prepare_call", "apply_overrides",
           "overrides_schema", "start_run", "run_status", "DEFAULT_WAIT_S", "MAX_WAIT_S",
           "outputs_of"]

DEFAULT_WAIT_S = 30.0
MAX_WAIT_S = 300.0
#: Steps a single background drive may take before handing back: a safety net
#: under `advance`, which is already bounded per pass.
_MAX_PASSES = 500


class PublishError(ValueError):
    """The call was refused before anything started. `code` is stable
    (`unknown_tool`, `bad_arguments`, `bad_overrides`, `overrides_disabled`); the
    message says what to change."""

    def __init__(self, code: str, message: str, problems: Optional[List[str]] = None):
        super().__init__(message)
        self.code = code
        self.problems = list(problems or [])


# ── overrides ────────────────────────────────────────────────────────────────

_TEXT = lambda n: {"type": "string", "minLength": 1, "maxLength": n}        # noqa: E731

#: `{node type: {config field: schema}}`. Everything a caller may change per
#: run, and nothing else. What is absent is absent on purpose: `tools`,
#: `agent` (a profile carries permissions), `labels`, `checks`, `skill`,
#: recipients and backends — the things that decide what a workflow may touch.
OVERRIDABLE: Dict[str, Dict[str, Dict[str, Any]]] = {
    "agent": {"prompt": _TEXT(20000), "system": _TEXT(5000),
              "max_rounds": {"type": "integer", "minimum": 1, "maximum": 50},
              "timeout_s": {"type": "number", "minimum": 5, "maximum": 3600},
              "output_schema": {"type": "object"}},
    "classify": {"threshold": {"type": "number", "minimum": 0, "maximum": 1},
                 "fallback": _TEXT(120),
                 "on_uncertain": {"type": "string", "enum": ["fallback", "ask"]},
                 "instructions": _TEXT(2000), "question": _TEXT(500),
                 "timeout_s": {"type": "number", "minimum": 1, "maximum": 600}},
    "extract": {"instructions": _TEXT(2000), "schema": {"type": "object"},
                "timeout_s": {"type": "number", "minimum": 1, "maximum": 600}},
    "guard": {"on_unknown": {"type": "string", "enum": ["fail", "pass"]},
              "instructions": _TEXT(2000),
              "timeout_s": {"type": "number", "minimum": 1, "maximum": 600}},
    "loop": {"max_iterations": {"type": "integer", "minimum": 1},
             "max_seconds": {"type": "integer", "minimum": 1},
             "max_tool_calls": {"type": "integer", "minimum": 1}},
}

_LOOP_FIELDS = ("max_iterations", "max_seconds", "max_tool_calls")


def overrides_schema(definition: WorkflowDefinition) -> Dict[str, Any]:
    """The `overrides` argument as JSON schema for THIS workflow: one property per
    node that has something overridable, listing exactly those fields."""
    properties: Dict[str, Any] = {}
    owned = loop_body_ids(definition.nodes)
    for node in definition.nodes:
        fields = OVERRIDABLE.get(node.type)
        if not fields:
            continue
        props = {k: dict(v) for k, v in fields.items()}
        label = f"{node.type} node" + (" (inside a loop)" if node.id in owned else "")
        properties[node.id] = {"type": "object", "description": f"{label}: per-run overrides",
                               "properties": props, "additionalProperties": False}
    return {"type": "object", "properties": properties, "additionalProperties": False,
            "description": "Optional per-run overrides of node settings: {node_id: {field: value}}. "
                           "Only the fields listed here can be changed; a loop's ceilings can only be lowered."}


def apply_overrides(definition: WorkflowDefinition, overrides: Any) -> Tuple[WorkflowDefinition, List[Dict[str, Any]]]:
    """A copy of `definition` with `overrides` merged into the named nodes'
    `config`, and the list of what changed. Raises :class:`PublishError`
    (`bad_overrides`) naming every problem at once."""
    if overrides in (None, {}):
        return definition, []
    if not isinstance(overrides, Mapping):
        raise PublishError("bad_overrides", "`overrides` must be an object of {node_id: {field: value}}")
    body = definition.to_dict()
    nodes = {n["id"]: n for n in body["nodes"]}
    problems: List[str] = []
    applied: List[Dict[str, Any]] = []
    for node_id, changes in overrides.items():
        node = nodes.get(node_id)
        if node is None:
            problems.append(f"{node_id}: no such node")
            continue
        fields = OVERRIDABLE.get(node["type"])
        if not fields:
            problems.append(f"{node_id}: a '{node['type']}' node has nothing that can be overridden")
            continue
        if not isinstance(changes, Mapping) or not changes:
            problems.append(f"{node_id}: expected an object of {{field: value}}")
            continue
        for field, value in changes.items():
            if field not in fields:
                problems.append(f"{node_id}.{field}: not overridable on a '{node['type']}' node "
                                f"(allowed: {sorted(fields)})")
                continue
            issues = schema_check.validate(value, fields[field])
            if issues:
                problems.append(f"{node_id}.{field}: " + "; ".join(issues[:2]))
                continue
            config = node.setdefault("config", {})
            if node["type"] == "loop":
                budget = config.setdefault("budget", {})
                declared = budget.get(field)
                if field == "max_iterations" and value > int(declared or 0):
                    problems.append(f"{node_id}.{field}: a caller can only lower a loop's ceiling "
                                    f"({declared}), not raise it")
                    continue
                if field in ("max_seconds", "max_tool_calls") and declared and value > declared:
                    problems.append(f"{node_id}.{field}: a caller can only lower a loop's ceiling "
                                    f"({declared}), not raise it")
                    continue
                if field in ("max_seconds", "max_tool_calls") and not declared:
                    pass                               # adding a ceiling is always allowed
                before = declared
                budget[field] = value
            else:
                before = config.get(field)
                config[field] = value
            applied.append({"node": node_id, "field": field, "from": before, "to": value})
    if problems:
        raise PublishError("bad_overrides", "the overrides were refused: " + " | ".join(problems[:6]), problems)
    try:
        return WorkflowDefinition.parse(body), applied
    except ContractError as exc:
        raise PublishError("bad_overrides", f"the overridden workflow no longer validates: {exc.path}: {exc.message}")


# ── the tool list ─────────────────────────────────────────────────────────

def _input_schema(definition: WorkflowDefinition, allow_overrides: bool) -> Dict[str, Any]:
    schema = copy.deepcopy(dict(definition.inputs))
    props = schema.setdefault("properties", {})
    props["wait_seconds"] = {
        "type": "number", "minimum": 0, "maximum": MAX_WAIT_S,
        "description": f"How long to wait for the run to finish before answering with its run id "
                       f"(default {DEFAULT_WAIT_S:g}s, at most {MAX_WAIT_S:g}s). The run keeps going either way."}
    props["idempotency_key"] = {
        "type": "string", "maxLength": 120,
        "description": "Optional. Calling again with the same key returns the run the first call "
                       "started instead of starting a second one."}
    if allow_overrides:
        tw = overrides_schema(definition)
        if tw["properties"]:
            props["overrides"] = tw
    return schema


def tool_specs(owner: str, library: Optional[WorkflowLibrary] = None) -> List[Dict[str, Any]]:
    """One spec per saved, enabled workflow with declared inputs:
    `{name, description, inputSchema, workflow, version}`."""
    library = library or WorkflowLibrary()
    out: List[Dict[str, Any]] = []
    for view in library.list(owner, enabled_only=True):
        if not view["publishable"]:
            continue
        try:
            definition = library.definition(owner, view["name"])
        except ValueError:
            logger.warning("published workflow %s no longer validates; left out of the tool list", view["name"])
            continue
        if definition is None:
            continue
        summary = (definition.description or definition.title or definition.id).strip()
        out.append({
            "name": view["tool"],
            "description": f"{summary} (workflow {definition.id} v{definition.version}; "
                           "starts a run and returns its id, plus the result if it finishes within the wait).",
            "inputSchema": _input_schema(definition, view["allow_overrides"]),
            "workflow": definition.id, "version": definition.version,
        })
    return out


# ── a call ────────────────────────────────────────────────────────────────

def _with_defaults(schema: Mapping[str, Any], arguments: Mapping[str, Any]) -> Dict[str, Any]:
    merged = dict(arguments)
    for key, spec in (schema.get("properties") or {}).items():
        if key not in merged and isinstance(spec, Mapping) and "default" in spec:
            merged[key] = copy.deepcopy(spec["default"])
    return merged


def prepare_call(owner: str, tool: str, arguments: Any,
                 library: Optional[WorkflowLibrary] = None) -> Dict[str, Any]:
    """Everything a call needs, checked, before a run exists:
    `{definition, inputs, wait_seconds, dedupe_key, applied_overrides, name}`.
    Raises :class:`PublishError`."""
    library = library or WorkflowLibrary()
    if not tool.startswith(TOOL_PREFIX):
        raise PublishError("unknown_tool", f"{tool!r} is not a published workflow tool")
    name = tool[len(TOOL_PREFIX):]
    view = library.get(owner, name, with_definition=False)
    if view is None or not view["enabled"] or not view["publishable"]:
        raise PublishError("unknown_tool", f"no published workflow tool named {tool!r}")
    try:
        definition = library.definition(owner, name)
    except ValueError as exc:
        raise PublishError("unknown_tool", str(exc))
    if definition is None:
        raise PublishError("unknown_tool", f"no published workflow tool named {tool!r}")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, Mapping):
        raise PublishError("bad_arguments", "the arguments must be an object")
    args = dict(arguments)
    overrides = args.pop("overrides", None)
    wait = args.pop("wait_seconds", None)
    key = args.pop("idempotency_key", None)
    if wait is None:
        wait_s = DEFAULT_WAIT_S
    elif isinstance(wait, bool) or not isinstance(wait, (int, float)) or not 0 <= wait <= MAX_WAIT_S:
        raise PublishError("bad_arguments", f"`wait_seconds` must be a number from 0 to {MAX_WAIT_S:g}")
    else:
        wait_s = float(wait)
    if key is not None and (not isinstance(key, str) or not key.strip() or len(key) > 120):
        raise PublishError("bad_arguments", "`idempotency_key` must be a non-empty string of at most 120 characters")
    inputs = _with_defaults(definition.inputs, args)
    issues = schema_check.validate(inputs, definition.inputs)
    if issues:
        raise PublishError("bad_arguments", "the arguments do not match the workflow's inputs: "
                           + "; ".join(issues[:6]), issues)
    if overrides not in (None, {}) and not view["allow_overrides"]:
        raise PublishError("overrides_disabled", "this workflow's owner has turned overrides off")
    overridden, applied = apply_overrides(definition, overrides)
    return {"definition": overridden, "inputs": inputs, "wait_seconds": wait_s, "name": name,
            "dedupe_key": f"pub:{owner}:{name}:{key.strip()}" if key else "",
            "applied_overrides": applied}


# ── running in the background ─────────────────────────────────────────────

_POOL_LOCK = threading.Lock()
_POOL: Optional[ThreadPoolExecutor] = None


def _pool() -> ThreadPoolExecutor:
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            workers = max(1, min(16, int(os.environ.get("FAUSTUS_PUBLISHED_WORKFLOW_WORKERS", "4") or 4)))
            _POOL = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="wf-published")
        return _POOL


def drive(engine: Any, run_id: str) -> Dict[str, Any]:
    """Advance a run until it finishes, pauses, or has nothing this process can
    do (another pass owns the next node). What a scheduler would do, for one
    run."""
    last: Dict[str, Any] = {}
    for _ in range(_MAX_PASSES):
        last = engine.advance(run_id)
        if not last.get("ok"):
            break
        status = last.get("status")
        if status in TERMINAL_WORKFLOW or status == "paused":
            break
        if last.get("reason") in ("max_nodes_reached",):
            continue
        break                                  # waiting_on_worker, claim_lost, worker_stopping
    return last


def start_run(store: Any, engine: Any, owner: str, prepared: Mapping[str, Any]) -> Dict[str, Any]:
    """Create the run and start driving it on a worker thread. Returns
    `{run_id, created, future}`; `future` resolves to the last advance result
    (or is None for a duplicate call that started nothing)."""
    definition: WorkflowDefinition = prepared["definition"]
    created = store.create_run(definition, owner=owner, trigger="manual",
                               inputs=dict(prepared["inputs"]),
                               dedupe_key=str(prepared.get("dedupe_key") or ""))
    future: Optional[Future] = None
    if created.get("created"):
        future = _pool().submit(drive, engine, created["run_id"])
    return {"run_id": created["run_id"], "created": bool(created.get("created")),
            "duplicate": not created.get("created"), "future": future}


# ── reading a run back ────────────────────────────────────────────────────

_MAX_STRING = 8000
_MAX_ITEMS = 50
_DROP_KEYS = {"idempotency_key", "session_id", "tool_events", "history", "distribution"}


def _compact(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return "[nested too deep]"
    if isinstance(value, str):
        return value if len(value) <= _MAX_STRING else value[:_MAX_STRING] + f"\n[truncated {len(value) - _MAX_STRING} characters]"
    if isinstance(value, Mapping):
        return {str(k): _compact(v, depth + 1) for k, v in list(value.items())[:_MAX_ITEMS]
                if k not in _DROP_KEYS}
    if isinstance(value, (list, tuple)):
        items = [_compact(v, depth + 1) for v in list(value)[:_MAX_ITEMS]]
        if len(value) > _MAX_ITEMS:
            items.append(f"[{len(value) - _MAX_ITEMS} more]")
        return items
    return value


def outputs_of(definition: WorkflowDefinition, states: Mapping[str, Any]) -> Dict[str, Any]:
    """What a finished run produced: the results of the nodes nothing else
    depends on (the ends of the graph), compacted. A loop's body is inside its
    loop's result, not listed separately."""
    owned = set(loop_body_ids(definition.nodes))
    needed = {dep for n in definition.nodes for dep in n.needs}
    out: Dict[str, Any] = {}
    for node in definition.nodes:
        if node.id in owned or node.id in needed:
            continue
        state = states.get(node.id)
        if state is not None and state.status == "completed" and state.result:
            out[node.id] = _compact(state.result)
    return out


def run_status(store: Any, owner: str, run_id: str) -> Optional[Dict[str, Any]]:
    """A compact, owner-checked view of a run, or None when it is not this
    owner's (or does not exist — the two look the same on purpose)."""
    loaded = store.get_run(run_id)
    if loaded is None or (loaded["run"].owner or "") != owner:
        return None
    run, definition = loaded["run"], loaded["definition"]
    states = store.node_runs(run_id)
    owned = set(loop_body_ids(definition.nodes))
    nodes = {nid: {"status": st.status, **({"reason": st.reason} if st.reason else {})}
             for nid, st in states.items() if nid not in owned}
    waiting = [{"node": nid, "approval_id": st.approval_id,
                **({"wake_at": st.result.get("wake_at")} if st.result.get("wake_at") else {}),
                **({"loop_budget_exhausted": True} if st.result.get("loop_budget_exhausted") else {})}
               for nid, st in states.items() if st.status == "paused"]
    failed = [{"node": nid, "reason": st.reason} for nid, st in states.items() if st.status == "failed"]
    out: Dict[str, Any] = {
        "run_id": run_id, "workflow": run.workflow_id, "version": run.workflow_version,
        "status": run.status, "finished": run.status in TERMINAL_WORKFLOW,
        "reason": run.reason or "", "nodes": nodes,
    }
    if waiting:
        out["waiting_on"] = waiting
    if failed:
        out["failed"] = failed
    if run.status == "completed":
        out["result"] = outputs_of(definition, states)
    return out


def wait_for(future: Optional[Future], seconds: float) -> bool:
    """True when the drive finished within `seconds` (a duplicate call, which
    started nothing, counts as finished: there is nothing to wait for)."""
    if future is None:
        return True
    try:
        future.result(timeout=max(0.0, seconds))
        return True
    except FutureTimeout:
        return False
    except Exception:                                  # noqa: BLE001 - the drive's own error is in the run's rows
        logger.exception("published workflow drive raised")
        return True
