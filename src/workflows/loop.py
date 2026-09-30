"""
workflows/loop.py — the `loop` node: a bounded, resumable repeat of a few nodes.

The design is in `docs/design/bounded-workflow-iterations.md`; this is the part
of it that runs. Three choices settle the questions that document left open:

**The loop is one node.** The engine still sees a flat, acyclic `needs` graph
(`_find_cycle` is untouched and still refuses every cycle). A `loop` node is
claimed, heartbeated and finished like any other; its `body` is a list of
sibling node ids that this module runs, in `needs` order, once per iteration.
Body nodes are never scheduled at the top level (`loop_body_ids`), so a body
node cannot run twice by accident, and nothing outside can wait on one.

**Progress is one row per pass** (`workflow_iteration_runs`), opened before the
pass starts under `loop_effect_idempotency_key` and updated as each body node
settles. A process that dies mid-loop leaves the row; the next pass of the
engine re-enters the loop node (a loop is not itself effectful, so its expired
lease is released), finds the open iteration and resumes at the body node it
was on. Finished iterations are never run again.

**An effect is never repeated to find out whether it happened.** A body node
that reaches outside marks its effect `pending` in the pass row before it acts.
If a pass is found open with an effect `pending` (or `confirmed` with no
result), the loop fails with `unknown_effect` for that iteration and node, in
the same words the engine uses for a top-level node, and does not retry.

Bounds: `max_iterations` is required and positive. `max_seconds` counts active
time (a pass waiting on a person is not spending it) and `max_tool_calls`
counts effectful body nodes plus what agent turns report. With an `until`
condition, running out of any of them is "exhausted": `on_exhausted: pause`
(the default) parks the loop node for a person to `extend` or stop, `fail`
ends it. Without `until`, reaching `max_iterations` is the loop's normal end.
"""
from __future__ import annotations

import logging
import time
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, Dict, List, Mapping, Optional, Tuple

from src.contracts.workflow import WorkflowDefinition, WorkflowNode, node_is_effectful
from src.contracts.workflow_iteration import LoopBudget, LoopNodeConfig, loop_effect_idempotency_key

from .handlers import evaluate

logger = logging.getLogger(__name__)

__all__ = ["run", "body_order", "MAX_HISTORY"]

#: How many iterations the loop node's own result lists. The pass rows hold all
#: of them; a result that grew with `max_iterations` would be a bigger row for
#: nothing a person reads.
MAX_HISTORY = 100

_STEP_DONE = ("completed", "skipped", "failed")


def body_order(body: Tuple[str, ...], by_id: Mapping[str, WorkflowNode]) -> List[str]:
    """The body in an order that respects `needs`, keeping the declared order
    wherever it is already valid."""
    members = set(body)
    pending = list(body)
    done: set = set()
    out: List[str] = []
    while pending:
        for nid in pending:
            if all(dep in done for dep in by_id[nid].needs if dep in members):
                out.append(nid)
                done.add(nid)
                pending.remove(nid)
                break
        else:                              # unreachable: `_find_cycle` refused it
            raise ValueError(f"the loop body has a dependency cycle: {pending}")
    return out


def _view(step: Mapping[str, Any]) -> SimpleNamespace:
    """A body step shaped like what `branch_gate` reads off a `NodeRun`."""
    return SimpleNamespace(status=step.get("status"), result=step.get("result") or {})


def _stopping(body_nodes: Mapping[str, WorkflowNode], progress: Mapping[str, Mapping[str, Any]]) -> set:
    out = set()
    for nid, step in progress.items():
        status = step.get("status")
        if status in ("skipped", "cancelled"):
            out.add(nid)
        elif status == "failed" and not body_nodes[nid].continue_on_failure:
            out.add(nid)
    return out


def _budget_for(cfg: LoopNodeConfig, extra: Mapping[str, int]) -> LoopBudget:
    """The ceilings in force now: the definition's, plus whatever a person has
    added since. A dimension the author left unbounded (0) stays unbounded —
    an extension cannot invent a limit that was not there."""
    b = cfg.budget
    return replace(
        b, max_iterations=b.max_iterations + int(extra.get("iterations") or 0),
        max_seconds=(b.max_seconds + int(extra.get("seconds") or 0)) if b.max_seconds else 0,
        max_tool_calls=(b.max_tool_calls + int(extra.get("tool_calls") or 0)) if b.max_tool_calls else 0)


def _spend(rows: List[Dict[str, Any]], by_id: Mapping[str, WorkflowNode]) -> Dict[str, float]:
    from .store import loop_iteration_spend
    calls, seconds = 0, 0.0
    for row in rows:
        spent = loop_iteration_spend(row, by_id)
        calls += spent["tool_calls"]
        seconds += spent["seconds"]
    return {"tool_calls": calls, "seconds": seconds}


def run(engine: Any, definition: WorkflowDefinition, node: WorkflowNode,
        context: Mapping[str, Any]) -> Dict[str, Any]:
    """Drive `node` (a `loop`) from wherever its pass rows say it was."""
    store = engine.store
    run_id = str(context.get("run_id") or "")
    cfg = LoopNodeConfig.parse(node.config, "loop.config")
    by_id = {n.id: n for n in definition.nodes}
    body_nodes = {b: by_id[b] for b in cfg.body}
    order = body_order(cfg.body, by_id)
    cancelled = context.get("cancel_requested")

    def stopped() -> bool:
        try:
            return bool(callable(cancelled) and cancelled())
        except Exception:                                  # noqa: BLE001 - fail open, like the handlers
            return False

    rows = {r["iteration"]: r for r in store.iteration_rows(run_id, node.id)}
    extra = store.loop_extra(run_id, node.id)
    budget = _budget_for(cfg, extra)
    history: List[Dict[str, Any]] = []
    last_results: Dict[str, Any] = {}

    def remember(row: Mapping[str, Any]) -> None:
        nonlocal last_results
        body = (row.get("result") or {}).get("body") or {}
        last_results = {b: s.get("result") or {} for b, s in body.items() if s.get("status") == "completed"}
        history.append({"iteration": row["iteration"], "status": row["status"],
                        "until": ((row.get("result") or {}).get("until") or {}).get("passed"),
                        "nodes": {b: s.get("status") for b, s in body.items()}})

    iteration = 1
    while iteration in rows and rows[iteration]["status"] == "completed":
        remember(rows[iteration])
        if (rows[iteration].get("result") or {}).get("exit") == "until":
            return _finish(rows[iteration]["iteration"], "until", last_results, history, rows, by_id, cfg)
        iteration += 1

    def exhausted(kind: str, detail: str) -> Dict[str, Any]:
        summary = {"iterations": len(history), "exited_by": kind, "results": last_results,
                   "history": history[-MAX_HISTORY:], "budget": _summary(rows, by_id, budget, extra)}
        if cfg.budget.on_exhausted == "fail":
            return {"status": "failed", "loop_budget_exhausted": True, "exhausted_by": kind,
                    "reason": f"loop budget exhausted: {detail}", **summary}
        return {"status": "paused", "loop_budget_exhausted": True, "exhausted_by": kind,
                "reason": (f"loop budget exhausted: {detail}; extend it or stop the run"),
                **summary}

    while True:
        if stopped():
            return {"status": "failed", "reason": "the loop's claim was lost or the run stopped",
                    "iterations": len(history), "results": last_results}
        row = rows.get(iteration)
        if row is None:
            spent = _spend([rows[i] for i in sorted(rows)], by_id)
            done = len(history)
            if done >= budget.max_iterations:
                if cfg.until is None:
                    return _finish(done, "max_iterations", last_results, history, rows, by_id, cfg)
                return exhausted("max_iterations",
                                 f"{done} of {budget.max_iterations} iterations ran and `until` never held")
            over = budget.exhausted_by(iterations_used=done, seconds_used=spent["seconds"],
                                       tool_calls_used=int(spent["tool_calls"]))
            if over in ("max_seconds", "max_tool_calls"):
                return exhausted(over, f"{over} reached ({spent['seconds']:.0f}s, "
                                       f"{int(spent['tool_calls'])} tool calls)")
            run_limit = _run_budget(store, run_id)
            if run_limit:
                return exhausted("run_budget", run_limit)
            key = loop_effect_idempotency_key(workflow_run_id=run_id, node_id=node.id,
                                              iteration=iteration, config=node.config,
                                              inputs=dict(context.get("results") or {}))
            row = store.open_iteration(run_id, node.id, iteration, key=key)
            rows[iteration] = row
            engine._emit("workflow.loop_iteration", run_id=run_id, node=node.id,
                         iteration=iteration, status="started")

        outcome = _run_pass(engine, definition, node, cfg, order, body_nodes, by_id, context,
                            row, last_results, budget, stopped)
        rows[iteration] = outcome["row"]
        if outcome["kind"] == "paused":
            return outcome["raw"]
        if outcome["kind"] == "failed":
            return {"status": "failed", "reason": outcome["reason"], "iteration": iteration,
                    "iterations": len(history), "results": last_results,
                    "history": history[-MAX_HISTORY:], **outcome.get("extra", {})}
        remember(outcome["row"])
        engine._emit("workflow.loop_iteration", run_id=run_id, node=node.id,
                     iteration=iteration, status="completed",
                     until=outcome["until_met"])
        if outcome["until_met"]:
            return _finish(iteration, "until", last_results, history, rows, by_id, cfg)
        iteration += 1


def _run_budget(store: Any, run_id: str) -> str:
    """The run-level autonomy budget, consulted between passes: the same ledger
    the engine checks between top-level nodes, so a loop cannot be the way out
    of it. Returns the reason text, or empty."""
    try:
        from src.autonomy_budget import Ledger, resolve_budget
        policy = store.get_policy(run_id)
        usage = store.usage_so_far(run_id)
        hit = Ledger(tool_calls=int(usage["tool_calls"]),
                     active_seconds=usage["active_seconds"]).check(resolve_budget(policy["budget_preset"]))
    except Exception:                                      # noqa: BLE001 - a broken ledger must not stop a loop silently
        logger.debug("loop run-budget check failed", exc_info=True)
        return ""
    if hit is None:
        return ""
    return f"the run's budget ({hit.kind}: {hit.used} >= {hit.limit}) is spent"


def _summary(rows: Mapping[int, Mapping[str, Any]], by_id: Mapping[str, WorkflowNode],
             budget: LoopBudget, extra: Mapping[str, int]) -> Dict[str, Any]:
    spent = _spend([rows[i] for i in sorted(rows)], by_id)
    return {"max_iterations": budget.max_iterations, "max_seconds": budget.max_seconds,
            "max_tool_calls": budget.max_tool_calls, "used_seconds": round(spent["seconds"], 1),
            "used_tool_calls": int(spent["tool_calls"]), "extended_by": dict(extra)}


def _finish(iterations: int, exited_by: str, results: Mapping[str, Any],
            history: List[Dict[str, Any]], rows: Mapping[int, Mapping[str, Any]],
            by_id: Mapping[str, WorkflowNode], cfg: LoopNodeConfig) -> Dict[str, Any]:
    spent = _spend([rows[i] for i in sorted(rows)], by_id)
    return {"iterations": iterations, "exited_by": exited_by, "results": dict(results),
            "history": history[-MAX_HISTORY:], "history_truncated": len(history) > MAX_HISTORY,
            "until_met": exited_by == "until",
            "budget": {"max_iterations": cfg.budget.max_iterations,
                       "used_seconds": round(spent["seconds"], 1),
                       "used_tool_calls": int(spent["tool_calls"])}}


# ── one pass ──────────────────────────────────────────────────────────────

def _run_pass(engine, definition, loop_node, cfg, order, body_nodes, by_id, base, row,
              previous_results, budget, stopped) -> Dict[str, Any]:
    """Run (or resume) iteration `row["iteration"]`. Returns `{kind, row, ...}`
    with kind `completed`, `paused` or `failed`."""
    store = engine.store
    run_id = str(base.get("run_id") or "")
    iteration = int(row["iteration"])
    result = dict(row.get("result") or {})
    progress: Dict[str, Dict[str, Any]] = {b: dict(s) for b, s in (result.get("body") or {}).items()}
    carried = float((row.get("budget_used") or {}).get("seconds") or 0.0)
    began = time.monotonic()

    def save(status: Optional[str] = None, reason: Optional[str] = None,
             extra_result: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        payload = {**{k: v for k, v in result.items() if k != "body"}, "body": progress,
                   **(extra_result or {})}
        result.update(payload)
        store.save_iteration(run_id, loop_node.id, iteration, status=status, result=payload,
                             reason=reason,
                             budget_used={"seconds": round(carried + time.monotonic() - began, 3)})
        fresh = dict(row)
        fresh.update(result=dict(payload), status=status or row.get("status"),
                     reason=reason if reason is not None else row.get("reason"),
                     budget_used={"seconds": round(carried + time.monotonic() - began, 3)})
        if status in ("completed", "failed"):
            from src.contracts.base import now_iso
            fresh["ended_at"] = now_iso()
        return fresh

    def fail(reason: str, **extra: Any) -> Dict[str, Any]:
        return {"kind": "failed", "reason": reason, "row": save("failed", reason), "extra": extra}

    if row.get("status") == "failed":
        # A later attempt at a pass that failed (the loop node has
        # `max_attempts > 1`): steps that only read are tried again; a step
        # that reaches outside keeps its failure, because whether it landed is
        # exactly what a failure does not say.
        for b in list(progress):
            if progress[b].get("status") == "failed" and not node_is_effectful(body_nodes[b]):
                progress.pop(b)

    local_results = {b: (s.get("result") or {}) for b, s in progress.items() if s.get("status") == "completed"}

    for bid in order:
        node = body_nodes[bid]
        step = progress.get(bid) or {}
        if step.get("status") in _STEP_DONE:
            if step["status"] == "failed" and not node.continue_on_failure:
                return fail(f"iteration {iteration}: {bid} failed earlier: {step.get('reason', '')}")
            continue
        if stopped():
            return fail("the loop's claim was lost or the run stopped")

        views = {b: _view(s) for b, s in progress.items()}
        halted = _stopping(body_nodes, progress)
        deps = [d for d in node.needs if d in body_nodes]
        if any(d in halted for d in deps):
            progress[bid] = {"status": "skipped", "reason": "an earlier body node was skipped or failed",
                             "result": {"upstream_stopped": True}}
            continue
        from .engine import branch_gate
        if branch_gate(node, views) is False:
            progress[bid] = {"status": "skipped", "reason": "branch not taken",
                             "result": {"branch_not_taken": True}}
            continue

        denial = engine._permission_denial(run_id, node)
        if denial:
            progress[bid] = {"status": "failed", "reason": denial, "result": {}}
            if not node.continue_on_failure:
                return fail(f"iteration {iteration}: {bid}: {denial}")
            continue

        effectful = node_is_effectful(node)
        attempt = int(step.get("attempt") or 0)
        resuming_pause = step.get("status") == "paused"
        if step.get("status") == "running":
            effect = step.get("effect") or "none"
            if effectful or effect != "none":
                # The worker died across a body node that reaches outside.
                # Nobody saw whether it landed (the same rule the engine applies
                # to a top-level node with an expired lease); a second run could
                # repeat it.
                progress[bid] = {**step, "status": "failed", "effect": "unknown",
                                 "reason": "unknown_effect: the worker stopped across this node's call"}
                return fail(
                    f"unknown_effect: iteration {iteration}, body node {bid!r} was interrupted "
                    f"across its call ({effect}); the loop does not repeat it to find out. "
                    "Reconcile the effect, then start a new run",
                    unknown_effect={"iteration": iteration, "node": bid})
        key = step.get("idempotency_key") or loop_effect_idempotency_key(
            workflow_run_id=run_id, node_id=bid, iteration=iteration, config=node.config,
            inputs=dict(base.get("results") or {}))
        partial = dict(step.get("result") or {}) if resuming_pause else {}

        while True:
            attempt = attempt + 1 if not resuming_pause else max(1, attempt)
            progress[bid] = {"status": "running", "attempt": attempt, "effect": "none",
                             "idempotency_key": key, "result": partial}
            row = save("running")

            def mark(state: str, _bid=bid) -> bool:
                if state == "pending" and stopped():
                    return False
                if state in ("pending", "confirmed", "none", "unknown"):
                    progress[_bid]["effect"] = state
                    save()
                return True

            merged = {**(base.get("results") or {}), **local_results}
            ctx = {
                "run_id": run_id, "workflow": definition.id, "attempt": attempt,
                "inputs": dict(base.get("inputs") or {}), "owner": base.get("owner") or "",
                "project_id": base.get("project_id") or "", "results": merged,
                "previous": partial, "idempotency_key": key, "node_id": bid,
                "mark_effect": mark, "cancel_requested": base.get("cancel_requested"),
                "loop": {"node": loop_node.id, "iteration": iteration,
                         "max_iterations": budget.max_iterations, "first": iteration == 1,
                         "results": dict(local_results), "previous": dict(previous_results)},
            }
            handler = engine.handlers.get(node.type)
            if handler is None:
                raw: Mapping[str, Any] = {"status": "failed",
                                          "reason": f"no handler for node type {node.type!r}"}
            else:
                try:
                    raw = handler(node, ctx) or {}
                except Exception as exc:                    # noqa: BLE001 - a handler must not kill the run
                    logger.exception("loop body node %s raised", bid)
                    raw = {"status": "failed", "reason": f"{type(exc).__name__}: {exc}"}
            status = str(raw.get("status") or "completed")
            if status not in ("completed", "failed", "paused", "skipped"):
                raw = {**raw, "reason": f"handler returned an unknown status {raw.get('status')!r}"}
                status = "failed"
            effect = progress[bid].get("effect", "none")

            if status == "paused":
                approval_id = str(raw.get("approval_id") or "")
                wake_at = str(raw.get("wake_at") or "")
                if not approval_id and not wake_at:
                    progress[bid] = {**progress[bid], "status": "failed",
                                     "reason": "the body node paused without an approval id or a wake time",
                                     "result": dict(raw)}
                    return fail(f"iteration {iteration}: {bid} paused without an approval id or a wake time")
                progress[bid] = {**progress[bid], "status": "paused", "result": dict(raw),
                                 "approval_id": approval_id, "wake_at": wake_at}
                fresh = save("paused", str(raw.get("reason") or f"waiting on {bid}"))
                return {"kind": "paused", "row": fresh, "raw": {
                    **{k: v for k, v in raw.items() if k in ("approval_id", "wake_at", "approval_cards")},
                    "status": "paused", "approval_id": approval_id, "wake_at": wake_at,
                    "reason": str(raw.get("reason") or f"iteration {iteration}: waiting on {bid}"),
                    "iteration": iteration, "paused_node": bid}}

            if status == "failed":
                if effect == "pending":
                    effect = progress[bid]["effect"] = "unknown"
                retry = (attempt < node.max_attempts and effect == "none" and not resuming_pause)
                reason = str(raw.get("reason") or "the node failed")
                if effect in ("unknown", "confirmed"):
                    reason = f"{effect}_effect: {reason}; automatic retry withheld"
                if retry:
                    resuming_pause = False
                    continue
                progress[bid] = {**progress[bid], "status": "failed", "reason": reason, "result": dict(raw)}
                row = save("running")
                if not node.continue_on_failure:
                    return fail(f"iteration {iteration}: body node {bid!r} failed: {reason}")
                break

            if status == "skipped":
                progress[bid] = {**progress[bid], "status": "skipped", "result": dict(raw),
                                 "reason": str(raw.get("reason") or "")}
                row = save("running")
                break

            progress[bid] = {**progress[bid], "status": "completed", "result": dict(raw),
                             "effect": "confirmed" if effectful and effect != "unknown" else effect}
            local_results[bid] = dict(raw)
            row = save("running")
            break

    # every body node has settled: decide whether the loop is done
    until_info: Dict[str, Any] = {}
    met = False
    if cfg.until is not None:
        ctx = {"inputs": dict(base.get("inputs") or {}),
               "results": {**(base.get("results") or {}), **local_results},
               "loop": {"node": loop_node.id, "iteration": iteration,
                        "max_iterations": budget.max_iterations, "first": iteration == 1,
                        "results": dict(local_results), "previous": dict(previous_results)}}
        verdict = evaluate(cfg.until.to_dict(), ctx)
        met = bool(verdict.get("passed")) and not verdict.get("error")
        until_info = {"passed": met, "detail": str(verdict.get("detail") or ""),
                      **({"error": verdict["error"]} if verdict.get("error") else {})}
    fresh = save("completed", "", {"until": until_info, **({"exit": "until"} if met else {})})
    return {"kind": "completed", "row": fresh, "until_met": met}
