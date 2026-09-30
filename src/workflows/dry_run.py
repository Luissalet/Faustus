"""
workflows/dry_run.py — a workflow walked end to end with nothing real behind it.

`simulate.py` answers "which nodes would run" without looking at any data. This
answers the next question: "given THESE inputs, what would the run hand back?"
— which is what an evaluation needs before it spends a model call on each case.

**Nothing here reaches outside.** No handler from `handlers.py` that touches a
model, a skill, a mail sender, the artifact store or an approval card is
called, no row is written, and no clock is waited on. Each node type has a
placeholder that is *deterministic* (the same definition and inputs give the
same outputs, byte for byte) and *respects the contract the real node keeps*:

* `agent` / `extract` — a value that satisfies the node's schema
  (`output_schema` / `schema`), built from the schema itself; with no schema,
  a fixed sentence;
* `classify` — the first declared label unless a mock picks another, with a
  receipt that says it was simulated; downstream `branch` gates then open or
  close exactly as they would for that label;
* `guard` — `pass` unless a mock says `fail`; a guard that would be run against
  real text says nothing about what real text would do, so it is not guessed;
* `condition` — really evaluated (it reads only the inputs and the placeholder
  results, never anything outside), so a workflow that branches on an input
  takes the branch that input picks;
* `loop` — really iterated, body in order, with the real `until` condition
  evaluated against the placeholders and the real ceiling; one that never
  reaches `until` ends the way a real one does (`paused` or `failed`, per
  `on_exhausted`);
* waits, approvals, skills, deliveries and stored artifacts — a marked
  placeholder (`simulated: true`); an approval is treated as granted and
  listed under `human_waits`, because a real run would stop there.

A prompt or text template is **rendered for real** against the placeholders, so
a reference to something no upstream node produces fails here, with the same
message, instead of in front of a model.

**A mock overrides one node.** `mocks = {node_id: {...}}` merges into that
node's placeholder (a classify's `label`, a guard's `passed`, any key of an
extract's `data`), or, with `status: "failed"` / `"skipped"`, makes the node
end that way. This is how an evaluation case says "suppose the classifier
answers `billing`".

Pure: no I/O, no model, no clock, no store. A placeholder is not a prediction
of what the model would say; it only proves the wiring, the schemas and the
branches hold together.
"""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from typing import Any, Dict, List, Mapping, Optional, Sequence

from src.contracts.workflow import (
    WorkflowDefinition, WorkflowNode, declared_branches, loop_body_ids,
)
from src.contracts.workflow_iteration import LoopNodeConfig

from . import schema_check
from .engine import branch_gate
from .handlers import condition_handler, evaluate, trigger_handler
from .loop import body_order
from .templating import TemplateError, render

__all__ = ["dry_run", "placeholder", "PLACEHOLDER_TEXT", "MAX_DRY_ITERATIONS"]

PLACEHOLDER_TEXT = "[simulated output]"

#: A dry run never iterates a loop more than this, whatever its ceiling says:
#: the point is to prove the body holds together, not to burn CPU on a
#: declared ceiling of a million.
MAX_DRY_ITERATIONS = 50

_DEPTH = 6


# ── values that satisfy a schema ──────────────────────────────────────────

def placeholder(schema: Any, _depth: int = 0) -> Any:
    """A deterministic value that satisfies `schema` (the subset
    `schema_check` enforces), or the nearest thing when it cannot be built
    (a `pattern` has no generator: the caller checks the value and reports).
    Never random, never dependent on the clock."""
    if not isinstance(schema, Mapping) or _depth > _DEPTH:
        return PLACEHOLDER_TEXT
    if "const" in schema:
        return copy.deepcopy(schema["const"])
    if isinstance(schema.get("enum"), (list, tuple)) and schema["enum"]:
        return copy.deepcopy(schema["enum"][0])
    for key in ("anyOf", "oneOf"):
        options = schema.get(key)
        if isinstance(options, (list, tuple)) and options:
            return placeholder(options[0], _depth + 1)
    merged: Dict[str, Any] = {k: v for k, v in schema.items() if k != "allOf"}
    for part in schema.get("allOf") or []:
        if isinstance(part, Mapping):
            for key, value in part.items():
                if key in ("properties",) and isinstance(value, Mapping):
                    merged.setdefault("properties", {})
                    merged["properties"] = {**merged["properties"], **value}
                elif key == "required" and isinstance(value, (list, tuple)):
                    merged["required"] = list(dict.fromkeys([*(merged.get("required") or []), *value]))
                else:
                    merged.setdefault(key, value)
    declared = merged.get("type")
    kind = declared[0] if isinstance(declared, (list, tuple)) and declared else declared
    if kind is None:
        kind = "object" if "properties" in merged else "string"

    if kind == "object":
        props = merged.get("properties") if isinstance(merged.get("properties"), Mapping) else {}
        return {name: placeholder(sub, _depth + 1) for name, sub in props.items()}
    if kind == "array":
        low = merged.get("minItems") if isinstance(merged.get("minItems"), int) else 0
        high = merged.get("maxItems") if isinstance(merged.get("maxItems"), int) else 1
        count = max(low, min(1, high))
        return [placeholder(merged.get("items"), _depth + 1) for _ in range(count)]
    if kind == "string":
        text = PLACEHOLDER_TEXT
        low = merged.get("minLength") if isinstance(merged.get("minLength"), int) else 0
        high = merged.get("maxLength") if isinstance(merged.get("maxLength"), int) else None
        if len(text) < low:
            text = (text + " ") * (low // (len(text) + 1) + 1)
            text = text[:max(low, 1)]
        if high is not None and len(text) > high:
            text = text[:max(high, 0)]
        return text
    if kind in ("integer", "number"):
        value: float = 0
        lo, hi = merged.get("minimum"), merged.get("maximum")
        xlo, xhi = merged.get("exclusiveMinimum"), merged.get("exclusiveMaximum")
        if isinstance(lo, (int, float)) and not isinstance(lo, bool):
            value = max(value, lo) if value < lo else value
        if isinstance(xlo, (int, float)) and not isinstance(xlo, bool) and value <= xlo:
            value = xlo + 1
        if isinstance(hi, (int, float)) and not isinstance(hi, bool) and value > hi:
            value = hi
        if isinstance(xhi, (int, float)) and not isinstance(xhi, bool) and value >= xhi:
            value = xhi - 1
        return int(value) if kind == "integer" else value
    if kind == "boolean":
        return False
    return None


def _merge(base: Any, extra: Mapping[str, Any]) -> Any:
    """`extra` laid over `base`, key by key, recursively for objects."""
    if not isinstance(base, dict):
        return copy.deepcopy(dict(extra))
    out = dict(base)
    for key, value in extra.items():
        out[key] = _merge(out.get(key), value) if isinstance(value, Mapping) and isinstance(out.get(key), dict) \
            else copy.deepcopy(value)
    return out


# ── one node ──────────────────────────────────────────────────────────────

def _failed(reason: str, **extra: Any) -> Dict[str, Any]:
    return {"status": "failed", "reason": reason, **extra}


def _text(node: WorkflowNode, key: str, context: Mapping[str, Any], *, required: bool = True) -> Any:
    """The rendered text of `config[key]`, or a failed-result dict."""
    template = (node.config or {}).get(key)
    if template in (None, ""):
        return _failed(f"a {node.type} node needs `config.{key}`") if required else ""
    try:
        return render(template, context)
    except TemplateError as exc:
        return _failed(str(exc))


def _checked(value: Any, schema: Mapping[str, Any], note: List[str], node_id: str) -> Dict[str, Any]:
    issues = schema_check.validate(value, schema)
    if issues:
        note.append(f"{node_id}: the placeholder cannot satisfy this schema ({'; '.join(issues[:2])}); "
                    "give this node a mock")
    return {"schema_ok": not issues}


def _simulate_node(node: WorkflowNode, context: Mapping[str, Any], mock: Mapping[str, Any],
                   warnings: List[str], human_waits: List[str]) -> Dict[str, Any]:
    kind = node.type
    config = node.config or {}
    if mock.get("status") in ("failed", "skipped"):
        return {"status": mock["status"], "reason": str(mock.get("reason") or f"mocked as {mock['status']}"),
                "simulated": True}
    body = {k: v for k, v in mock.items() if k not in ("status", "reason")}

    if kind in ("manual", "schedule", "webhook"):
        return {**trigger_handler(node, context), "at": "simulated", **body}
    if kind == "condition":
        return {**condition_handler(node, context)}

    if kind == "agent":
        prompt = _text(node, "prompt", context)
        if isinstance(prompt, dict):
            return prompt
        system = _text(node, "system", context, required=False)
        if isinstance(system, dict):
            return system
        result: Dict[str, Any] = {"rounds": 0, "tool_calls": 0, "model": "simulated", "profile": str(config.get("agent") or ""),
                                  "stop_reason": "completed", "simulated": True}
        schema = config.get("output_schema")
        if isinstance(schema, Mapping):
            data = _merge(placeholder(schema), body.get("data") or {}) if isinstance(body.get("data"), Mapping) \
                else placeholder(schema)
            result.update(data=data, repaired=False, text=json.dumps(data, ensure_ascii=False, sort_keys=True),
                          **_checked(data, schema, warnings, node.id))
        else:
            result["text"] = PLACEHOLDER_TEXT
        result.update({k: v for k, v in body.items() if k != "data"})
        return result

    if kind == "extract":
        text = _text(node, "text", context)
        if isinstance(text, dict):
            return text
        schema = config.get("schema")
        if not isinstance(schema, Mapping):
            return _failed("an extract node needs `config.schema`")
        data = _merge(placeholder(schema), body["data"]) if isinstance(body.get("data"), Mapping) else placeholder(schema)
        return {"data": data, "repaired": False, "input_truncated": False, "simulated": True,
                **_checked(data, schema, warnings, node.id)}

    if kind == "classify":
        text = _text(node, "text", context)
        if isinstance(text, dict):
            return text
        labels = declared_branches(node)
        if len(labels) < 2:
            return _failed("a classify node needs at least two labels to choose between")
        picked = body.get("label") or body.get("branch") or labels[0]
        if picked not in labels:
            return _failed(f"the mock picks {picked!r}, which is not one of this node's labels {list(labels)}")
        return {"branch": picked, "label": picked, "simulated": True,
                "receipt": {"options": list(labels), "choice": picked, "confidence": 1.0, "fallback": False,
                            "model": "simulated", "simulated": True}}

    if kind == "guard":
        text = _text(node, "text", context)
        if isinstance(text, dict):
            return text
        passed = body.get("passed")
        if passed is None:
            passed = body.get("branch", "pass") == "pass"
        checks = []
        for i, spec in enumerate(config.get("checks") or []):
            check_type = spec if isinstance(spec, str) else str((spec or {}).get("type") or "")
            check_id = (spec.get("id") if isinstance(spec, Mapping) and spec.get("id") else f"{check_type}-{i + 1}")
            checks.append({"id": check_id, "type": check_type, "status": "pass" if passed else "fail",
                           "simulated": True})
        return {"branch": "pass" if passed else "fail", "passed": bool(passed), "checks": checks,
                "failed": [] if passed else [c["id"] for c in checks], "unknown": [],
                "on_unknown": str(config.get("on_unknown") or "fail"), "input_truncated": False,
                "simulated": True}

    if kind == "human_approval":
        human_waits.append(node.id)
        return {"approved": True, "simulated": True, "detail": "treated as granted; a real run waits for a person",
                **body}
    if kind == "wait_for_event":
        return {"simulated": True, "settled": True, "timed_out": False, "events": [], "count": 0, **body}
    if kind in ("wait", "wait_until"):
        return {"simulated": True, "waited": True, "timed_out": False, **body}
    if kind == "deliver":
        return {"delivered": False, "simulated": True, "detail": "nothing was sent", **body}
    if kind == "artifact_store":
        return {"stored": False, "simulated": True, "artifact_id": f"simulated-{node.id}", **body}
    if kind == "skill":
        return {"simulated": True, "skill": str(config.get("skill") or ""), "output": {},
                "detail": "the skill did not run", **body}
    return _failed(f"no placeholder for node type {kind!r}")


# ── the walk ──────────────────────────────────────────────────────────────

_TERMINAL = ("completed", "failed", "skipped")


def _halts(state: Any, node: WorkflowNode) -> bool:
    return state.status in ("skipped", "paused") or (state.status == "failed" and not node.continue_on_failure)


def _run_nodes(order: Sequence[WorkflowNode], states: Dict[str, Any], by_id: Mapping[str, WorkflowNode],
               context_for: Any, mocks: Mapping[str, Mapping[str, Any]], warnings: List[str],
               human_waits: List[str], scope: Mapping[str, WorkflowNode]) -> None:
    """Settle every node in `order` (already in dependency order) into `states`."""
    for node in order:
        deps = [d for d in node.needs if d in scope]
        blocked = next((d for d in deps if _halts(states[d], by_id[d])), None)
        if blocked is not None:
            states[node.id] = SimpleNamespace(status="skipped", result={"upstream_stopped": True},
                                              reason=f"{blocked} did not complete")
            continue
        if branch_gate(node, states) is False:
            states[node.id] = SimpleNamespace(status="skipped", result={"branch_not_taken": True},
                                              reason="branch not taken")
            continue
        raw = _simulate_node(node, context_for(node), dict(mocks.get(node.id) or {}), warnings, human_waits)
        status = str(raw.get("status") or "completed")
        reason = str(raw.get("reason") or "")
        result = {k: v for k, v in raw.items() if k not in ("status",)}
        states[node.id] = SimpleNamespace(status=status, result=result, reason=reason)


def _order(nodes: Sequence[WorkflowNode], members: Optional[set] = None) -> List[WorkflowNode]:
    """`nodes` in an order that respects `needs` (ties keep definition order)."""
    pool = [n for n in nodes if members is None or n.id in members]
    ids = {n.id for n in pool}
    out: List[WorkflowNode] = []
    done: set = set()
    pending = list(pool)
    while pending:
        progressed = False
        for node in list(pending):
            if all(d in done for d in node.needs if d in ids):
                out.append(node)
                done.add(node.id)
                pending.remove(node)
                progressed = True
        if not progressed:                        # `parse` refuses cycles; stay total anyway
            out.extend(pending)
            break
    return out


def _run_loop(loop: WorkflowNode, definition: WorkflowDefinition, by_id: Mapping[str, WorkflowNode],
              base: Mapping[str, Any], mocks: Mapping[str, Mapping[str, Any]], warnings: List[str],
              human_waits: List[str]) -> Dict[str, Any]:
    cfg = LoopNodeConfig.parse(loop.config, "loop.config")
    ceiling = cfg.budget.max_iterations
    cap = min(ceiling, MAX_DRY_ITERATIONS)
    if cap < ceiling:
        warnings.append(f"{loop.id}: the loop's ceiling is {ceiling}; a dry run stops after {cap} iterations")
    scope = {b: by_id[b] for b in cfg.body}
    order = [by_id[b] for b in body_order(cfg.body, by_id)]
    history: List[Dict[str, Any]] = []
    last: Dict[str, Any] = {}
    met = False
    for iteration in range(1, cap + 1):
        states: Dict[str, Any] = {}

        def context_for(node: WorkflowNode, _i: int = iteration) -> Dict[str, Any]:
            seen = {**base["results"], **{b: s.result for b, s in states.items() if s.status == "completed"}}
            return {**base, "results": seen, "node_id": node.id,
                    "loop": {"node": loop.id, "iteration": _i, "max_iterations": ceiling, "first": _i == 1,
                             "results": {b: s.result for b, s in states.items() if s.status == "completed"},
                             "previous": dict(last)}}

        _run_nodes(order, states, by_id, context_for, mocks, warnings, human_waits, scope)
        failed = [b for b, s in states.items() if s.status == "failed" and not by_id[b].continue_on_failure]
        last = {b: s.result for b, s in states.items() if s.status == "completed"}
        history.append({"iteration": iteration, "status": "failed" if failed else "completed",
                        "nodes": {b: s.status for b, s in states.items()}})
        if failed:
            why = "; ".join(f"{b}: {states[b].reason}" for b in failed)
            return {"status": "failed", "reason": f"iteration {iteration}: {why}", "iterations": iteration,
                    "results": last, "history": history, "simulated": True}
        if cfg.until is not None:
            verdict = evaluate(cfg.until.to_dict(), {**base, "results": {**base["results"], **last},
                                                     "loop": {"node": loop.id, "iteration": iteration,
                                                              "max_iterations": ceiling, "first": iteration == 1,
                                                              "results": dict(last), "previous": {}}})
            history[-1]["until"] = bool(verdict.get("passed")) and not verdict.get("error")
            if history[-1]["until"]:
                met = True
                break
    result = {"iterations": len(history), "results": last, "history": history, "until_met": met,
              "exited_by": "until" if met else "max_iterations", "simulated": True}
    if cfg.until is not None and not met:
        result["loop_budget_exhausted"] = True
        result["exhausted_by"] = "max_iterations"
        result["status"] = "failed" if cfg.budget.on_exhausted == "fail" else "paused"
        result["reason"] = (f"{len(history)} of {ceiling} iterations ran and `until` never held "
                            "on the placeholder results; give a body node a mock that satisfies it")
    return result


def dry_run(definition: Any, inputs: Optional[Mapping[str, Any]] = None, *,
            mocks: Optional[Mapping[str, Mapping[str, Any]]] = None) -> Dict[str, Any]:
    """Walk `definition` with `inputs`; return
    `{status, nodes, outputs, human_waits, warnings, simulated: True}`.

    `status` is `completed`, `failed` (a node failed and nothing absorbed it) or
    `paused` (a loop used its ceiling without reaching `until`; what is
    downstream of it is skipped, as in a real run). `nodes` is
    `{id: {status, reason?, result?}}` for every top-level node; `outputs` is
    what nothing else consumes — the same set a finished real run reports."""
    if isinstance(definition, Mapping):
        definition = WorkflowDefinition.parse(definition)
    if not isinstance(definition, WorkflowDefinition):
        raise TypeError("dry_run expects a WorkflowDefinition or a mapping")
    mocks = {str(k): dict(v) for k, v in (mocks or {}).items() if isinstance(v, Mapping)}
    by_id = {n.id: n for n in definition.nodes}
    unknown = sorted(set(mocks) - set(by_id))
    if unknown:
        raise ValueError(f"mocks name node(s) {unknown}, which this workflow does not have")
    owned = loop_body_ids(definition.nodes)
    top = {n.id: n for n in definition.nodes if n.id not in owned}
    warnings: List[str] = []
    human_waits: List[str] = []
    states: Dict[str, Any] = {}
    base = {"run_id": "dry-run", "workflow": definition.id, "attempt": 1,
            "inputs": dict(inputs or {}), "owner": "", "project_id": "", "results": {}, "previous": {}}

    def context_for(node: WorkflowNode) -> Dict[str, Any]:
        return {**base, "node_id": node.id,
                "results": {i: s.result for i, s in states.items() if s.status == "completed"}}

    for node in _order(definition.nodes, set(top)):
        if node.type == "loop":
            deps = [d for d in node.needs if d in top]
            blocked = next((d for d in deps if _halts(states[d], by_id[d])), None)
            if blocked is not None:
                states[node.id] = SimpleNamespace(status="skipped", result={"upstream_stopped": True},
                                                  reason=f"{blocked} did not complete")
                continue
            if branch_gate(node, states) is False:
                states[node.id] = SimpleNamespace(status="skipped", result={"branch_not_taken": True},
                                                  reason="branch not taken")
                continue
            raw = _run_loop(node, definition, by_id, {**context_for(node)}, mocks, warnings, human_waits)
            status = str(raw.get("status") or "completed")
            states[node.id] = SimpleNamespace(
                status=status, result={k: v for k, v in raw.items() if k != "status"},
                reason=str(raw.get("reason") or ""))
            continue
        _run_nodes([node], states, by_id, context_for, mocks, warnings, human_waits, top)

    failed = [n for n, s in states.items() if s.status == "failed" and not by_id[n].continue_on_failure]
    paused = [n for n, s in states.items() if s.status == "paused"]
    status = "failed" if failed else ("paused" if paused else "completed")
    from .published import outputs_of
    outputs = outputs_of(definition, {i: s for i, s in states.items()}) if status == "completed" else {}
    return {
        "simulated": True, "status": status, "workflow": definition.id, "version": definition.version,
        "nodes": {i: {"status": s.status, **({"reason": s.reason} if s.reason else {}),
                      **({"result": s.result} if s.result else {})} for i, s in states.items()},
        "outputs": outputs, "human_waits": human_waits, "warnings": warnings,
        **({"failed": [{"node": n, "reason": states[n].reason} for n in failed]} if failed else {}),
        **({"paused_on": paused} if paused else {}),
    }
