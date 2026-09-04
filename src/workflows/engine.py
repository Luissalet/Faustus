"""
workflows/engine.py — one pass over a run, and nothing clever.

`advance()` does as much as it can and returns. It does not loop forever, own
a thread, or hold state between calls: everything it needs is in the store, so
the process can die between any two nodes and the next call picks up exactly
where the rows say it was. A scheduler calling `advance()` and a person
clicking "continue" are the same code path.

The three rules it enforces, in the order they matter:

1. **A node that reaches outside is claimed before it acts.** `start_node`
   writes the idempotency key first; if the key is already there, the work
   already happened (or is happening) and the recorded result is the answer.
2. **A paused run stops, and says what it is waiting on.** Not an error, not a
   retry — a state with an approval id attached.
3. **A failure stops the branch that depended on it**, and only that branch.
   `continue_on_failure` exists for the nodes where that is wrong, and it has
   to be said in the definition rather than inferred.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol, Tuple

from src.contracts import NodeRun, WorkflowDefinition, WorkflowNode
from src.contracts.base import now_iso
from src.contracts.workflow import TERMINAL_NODE

from .store import WorkflowStore

logger = logging.getLogger(__name__)


class NodeHandler(Protocol):
    """What a node type does. Returns a dict; the engine reads three keys:

    * `status` — `completed` (default), `failed`, `paused` or `skipped`;
    * `approval_id` — required when the status is `paused`;
    * anything else is kept as the node's result and handed to what comes next.
    """

    def __call__(self, node: WorkflowNode, context: Mapping[str, Any]) -> Mapping[str, Any]:
        ...


def ready_nodes(definition: WorkflowDefinition,
                states: Mapping[str, NodeRun]) -> Tuple[List[WorkflowNode], List[WorkflowNode]]:
    """`(runnable, blocked)`.

    Blocked is returned too, because "why is nothing happening?" is answered
    by the list of nodes whose dependencies failed — not by an empty runnable
    list, which looks identical to "finished"."""
    runnable: List[WorkflowNode] = []
    blocked: List[WorkflowNode] = []
    stopped = _stopping(definition, states)
    for node in definition.nodes:
        state = states.get(node.id)
        if state is not None and state.status in TERMINAL_NODE:
            continue
        if state is not None and state.status == "paused":
            blocked.append(node)
            continue
        deps = [states.get(dep) for dep in node.needs]
        if any(d is None or d.status not in TERMINAL_NODE for d in deps):
            continue                                   # simply not its turn yet
        if any(dep in stopped for dep in node.needs):
            blocked.append(node)
            continue
        runnable.append(node)
    return runnable, blocked


def _stopping(definition: WorkflowDefinition,
              states: Mapping[str, NodeRun]) -> set:
    """Node ids whose outcome stops whatever depended on them.

    A failure stops the branch — unless the author said in the definition that
    this particular node is allowed to fail, which is the whole point of
    `continue_on_failure`. A cancellation always stops: nobody declared it
    survivable, and it means someone pulled the run.

    A **skip** stops it too, and that is not a failure: it is how a `condition`
    turns into a branch that is not taken. A skipped node produced no result,
    so anything that named it in `needs` has nothing to work from."""
    by_id = {n.id: n for n in definition.nodes}
    stopped = set()
    for nid, state in states.items():
        if state.status in ("cancelled", "skipped"):
            stopped.add(nid)
        elif state.status == "failed":
            node = by_id.get(nid)
            if node is None or not node.continue_on_failure:
                stopped.add(nid)
    return stopped


class WorkflowEngine:
    """Handlers in, durable progress out."""

    def __init__(self, handlers: Optional[Mapping[str, NodeHandler]] = None,
                 store: Optional[WorkflowStore] = None,
                 on_event: Optional[Callable[[str, Dict[str, Any]], None]] = None):
        self.handlers: Dict[str, NodeHandler] = dict(handlers or {})
        self.store = store or WorkflowStore()
        self.on_event = on_event

    def _emit(self, name: str, **data: Any) -> None:
        if not self.on_event:
            return
        try:
            self.on_event(name, data)
        except Exception:
            logger.debug("workflow event callback failed", exc_info=True)

    # ── one pass ──────────────────────────────────────────────────────────

    def advance(self, run_id: str, *, max_nodes: int = 50) -> Dict[str, Any]:
        """Run every node that can run right now, then report.

        `max_nodes` is a stop, not a schedule: a definition that somehow keeps
        producing runnable nodes should give the caller control back rather
        than spin. Hitting it is reported, never silent."""
        loaded = self.store.get_run(run_id)
        if loaded is None:
            return {"ok": False, "reason": "not_found", "run_id": run_id}
        run, definition = loaded["run"], loaded["definition"]
        if run.status in ("completed", "failed", "cancelled"):
            return {"ok": True, "reason": f"already_{run.status}", "run_id": run_id,
                    "status": run.status, "ran": []}

        if run.status in ("pending", "paused"):
            self.store.set_run_status(run_id, "running")
            if run.status == "pending":
                self._emit("workflow.started", run_id=run_id, workflow=definition.id)

        self._wake_due(run_id)

        ran: List[Dict[str, Any]] = []
        for _ in range(max(1, max_nodes)):
            states = self.store.node_runs(run_id)
            runnable, blocked = ready_nodes(definition, states)
            if not runnable:
                return self._settle(run_id, definition, states, blocked, ran)
            outcome = self._run_node(run_id, definition, runnable[0], states,
                                     inputs=run.inputs, owner=run.owner)
            ran.append(outcome)
            if outcome["status"] == "paused":
                self.store.set_run_status(run_id, "paused",
                                          reason=f"waiting on {outcome['node_id']}")
                self._emit("workflow.paused", run_id=run_id, node=outcome["node_id"],
                           approval_id=outcome.get("approval_id", ""))
                return {"ok": True, "reason": "paused", "run_id": run_id,
                        "status": "paused", "ran": ran,
                        "waiting_on": outcome["node_id"],
                        "approval_id": outcome.get("approval_id", ""),
                        "wake_at": outcome.get("wake_at", "")}

        states = self.store.node_runs(run_id)
        return {"ok": True, "reason": "max_nodes_reached", "run_id": run_id,
                "status": "running", "ran": ran,
                "detail": f"stopped after {max_nodes} nodes in one pass; call advance() again"}


    def _run_node(self, run_id: str, definition: WorkflowDefinition,
                  node: WorkflowNode, states: Mapping[str, NodeRun],
                  *, inputs: Optional[Mapping[str, Any]] = None,
                  owner: str = "") -> Dict[str, Any]:
        previous = states.get(node.id)
        # Coming back from a pause is the SAME attempt continuing, not a new
        # one. Counting each poll as an attempt is how a run waiting a week on
        # a person walks past `attempt`'s ceiling and stops being readable at
        # all — and it would be a lie besides: nothing was retried, the node
        # simply had not been answered yet.
        resumed = bool(previous and previous.status in ("pending", "paused")
                       and ((previous.result or {}).get("approval_id")
                            or (previous.result or {}).get("wake_at")))
        if previous is None:
            attempt = 1
        elif resumed:
            attempt = max(1, previous.attempt)
        else:
            attempt = previous.attempt + 1
        context = {
            "run_id": run_id,
            "workflow": definition.id,
            "attempt": attempt,
            "inputs": dict(inputs or {}),
            # Whose run this is. An approval card opened without it does not
            # appear in anyone's pending list — the gate would be asking a
            # person who is never shown the question.
            "owner": owner or "",
            "results": {nid: dict(st.result) for nid, st in states.items()
                        if st.status == "completed"},
            # What this same node returned last time it ran. A `wait` needs it
            # to know it is the second pass rather than the first, and an
            # approval gate needs it to find the card it already opened —
            # without it, both would start over on every pass.
            "previous": dict(previous.result) if previous and previous.result else {},
        }

        claim = self.store.start_node(run_id, node, attempt=attempt,
                                      inputs=context["results"])
        if not claim["claimed"]:
            # The key was already there. Either another pass is doing this node
            # or a previous process died after claiming it — in both cases the
            # answer is the row, not a second attempt at the work.
            return {"node_id": node.id, "status": claim["status"],
                    "reason": "already_attempted", "attempt": claim["attempt"],
                    "result": claim.get("result", {})}

        handler = self.handlers.get(node.type)
        if handler is None:
            self.store.finish_node(run_id, node.id, status="failed",
                                   reason=f"no handler for node type {node.type!r}")
            return {"node_id": node.id, "status": "failed",
                    "reason": f"no handler for node type {node.type!r}"}

        self._emit("workflow.node", run_id=run_id, node=node.id, type=node.type,
                   attempt=attempt, status="running")
        try:
            raw = handler(node, context) or {}
        except Exception as e:                       # a handler must not kill the run
            logger.exception("workflow node %s raised", node.id)
            failed = self._maybe_retry(run_id, node, attempt, f"{type(e).__name__}: {e}")
            return {"node_id": node.id, **failed}

        status = str(raw.get("status") or "completed")
        if status not in ("completed", "failed", "paused", "skipped"):
            status = "failed"
            raw = {**raw, "reason": f"handler returned an unknown status {raw.get('status')!r}"}

        if status == "paused":
            approval_id = str(raw.get("approval_id") or "")
            wake_at = str(raw.get("wake_at") or "")
            if not approval_id and not wake_at:
                # A pause nobody and nothing can end is a stall. Refuse it
                # rather than park the run forever: either a person can answer
                # it (an approval id) or time can (a wake time).
                self.store.finish_node(
                    run_id, node.id, status="failed",
                    reason="the handler paused without an approval id or a wake time")
                return {"node_id": node.id, "status": "failed",
                        "reason": "paused without an approval id or a wake time"}
            self.store.finish_node(run_id, node.id, status="paused",
                                   result=raw, approval_id=approval_id,
                                   reason=str(raw.get("reason")
                                              or (f"waiting until {wake_at}" if wake_at
                                                  else "waiting on approval")))
            return {"node_id": node.id, "status": "paused", "attempt": attempt,
                    "approval_id": approval_id, "wake_at": wake_at}

        if status == "failed":
            return {"node_id": node.id,
                    **self._maybe_retry(run_id, node, attempt,
                                        str(raw.get("reason") or "the node failed"))}

        self.store.finish_node(run_id, node.id, status=status, result=raw)
        self._emit("workflow.node", run_id=run_id, node=node.id, type=node.type,
                   attempt=attempt, status=status)
        return {"node_id": node.id, "status": status, "attempt": attempt,
                "result": dict(raw)}


    def _maybe_retry(self, run_id: str, node: WorkflowNode, attempt: int,
                     reason: str) -> Dict[str, Any]:
        """Retries are per node and declared in the definition, not global.

        A retry releases the key so the next pass can claim a fresh attempt —
        and the contract already refused `max_attempts > 1` on a node that
        reaches outside unless its author marked the effect idempotent, so
        this cannot quietly send an email twice."""
        if attempt < node.max_attempts:
            # `pending`, not `failed`: a failed row is terminal, and the graph
            # reader would treat the node as finished and never come back to
            # it — which is how `max_attempts: 3` silently meant one. The
            # failure is kept in `reason`, so the record still shows it.
            self.store.finish_node(
                run_id, node.id, status="pending",
                reason=f"attempt {attempt}/{node.max_attempts} failed: {reason}")
            self.store.release_key(run_id, node.id, attempt)
            return {"status": "failed", "retryable": True, "attempt": attempt,
                    "reason": reason}
        self.store.finish_node(run_id, node.id, status="failed", reason=reason)
        self._emit("workflow.node", run_id=run_id, node=node.id, type=node.type,
                   attempt=attempt, status="failed", reason=reason)
        return {"status": "failed", "retryable": False, "attempt": attempt,
                "reason": reason}

    def _settle(self, run_id: str, definition: WorkflowDefinition,
                states: Mapping[str, NodeRun], blocked, ran) -> Dict[str, Any]:
        """Nothing left to run. Decide what the run *is*, and say why."""
        paused = [n for n in blocked if (states.get(n.id) or None)
                  and states[n.id].status == "paused"]
        if paused:
            self.store.set_run_status(run_id, "paused",
                                      reason=f"waiting on {paused[0].id}")
            waiting = states[paused[0].id]
            return {"ok": True, "reason": "paused", "run_id": run_id,
                    "status": "paused", "ran": ran, "waiting_on": paused[0].id,
                    "approval_id": waiting.approval_id,
                    "wake_at": str((waiting.result or {}).get("wake_at") or "")}

        # A node the author marked `continue_on_failure` is not a run failure:
        # it is reported on the node and the run carries on. Counting it here
        # would make the flag mean nothing at the only moment it matters.
        stopping = _stopping(definition, states)
        failures = [nid for nid, st in states.items()
                    if st.status == "failed" and nid in stopping]
        tolerated = [nid for nid, st in states.items()
                     if st.status == "failed" and nid not in stopping]
        # Everything downstream of a failure, not only what depended on it
        # directly. Reporting one level deep answers "why did `write` not run?"
        # and leaves "and what about `send`?" hanging — which is the question
        # someone asks next.
        unreached = _unreachable(definition, states)
        if failures:
            detail = f"failed: {sorted(failures)}"
            if unreached:
                detail += f"; never reached: {sorted(unreached)}"
            self.store.set_run_status(run_id, "failed", reason=detail)
            self._emit("workflow.finished", run_id=run_id, status="failed", detail=detail)
            return {"ok": True, "reason": "failed", "run_id": run_id,
                    "status": "failed", "ran": ran, "failed_nodes": sorted(failures),
                    "never_reached": sorted(unreached),
                    "tolerated_failures": sorted(tolerated)}

        # Completed, but not necessarily spotless: a tolerated failure is
        # reported rather than swallowed, or `continue_on_failure` becomes a
        # way to hide broken steps behind a green run.
        detail = (f"completed with tolerated failures: {sorted(tolerated)}"
                  if tolerated else "")
        self.store.set_run_status(run_id, "completed", reason=detail)
        self._emit("workflow.finished", run_id=run_id, status="completed",
                   detail=detail)
        return {"ok": True, "reason": "completed", "run_id": run_id,
                "status": "completed", "ran": ran,
                "tolerated_failures": sorted(tolerated),
                # With no failures, an unreached node is a branch a condition
                # did not take. Naming it is the difference between "the
                # workflow finished" and "the workflow finished, and here is
                # the half of it that never happened".
                "not_taken": sorted(unreached)}

    # ── resuming a pause ──────────────────────────────────────────────────

    def resume(self, run_id: str, node_id: str) -> Dict[str, Any]:
        """The approval came through. Release the node's claim and carry on.

        Releasing is correct here precisely because the work has NOT happened:
        the node paused before acting. A completed node keeps its key forever."""
        if not self.store.reopen_node(run_id, node_id):
            return {"ok": False, "reason": "not_paused", "node_id": node_id}
        self._emit("workflow.node", run_id=run_id, node=node_id, status="resumed")
        return self.advance(run_id)

    def _wake_due(self, run_id: str) -> List[str]:
        """Reopen the nodes that were waiting for a time that has now passed.

        A `wait` is resolved by the clock rather than by a person, so nothing
        would ever call `resume()` for it. Doing this at the top of `advance()`
        means one scheduler calling `advance()` on a timer is the whole
        implementation — and a wake time still in the future is left alone, so
        calling early is harmless."""
        now = now_iso()
        woken: List[str] = []
        for node_id, state in self.store.node_runs(run_id).items():
            if state.status != "paused":
                continue
            wake_at = str((state.result or {}).get("wake_at") or "")
            if wake_at and wake_at <= now and self.store.reopen_node(run_id, node_id):
                woken.append(node_id)
                self._emit("workflow.node", run_id=run_id, node=node_id,
                           status="woken", wake_at=wake_at)
        return woken


def _unreachable(definition: WorkflowDefinition,
                 states: Mapping[str, NodeRun]) -> List[str]:
    """Nodes that never ran and now never can, transitively.

    A node is unreachable when a dependency failed or was cancelled, or when a
    dependency is itself unreachable. Computed to a fixed point rather than one
    hop, because the useful answer to "what did this failure cost?" is the
    whole tail of the branch, not the node that touched it."""
    dead = _stopping(definition, states)
    unreachable: set = set()
    changed = True
    while changed:
        changed = False
        for node in definition.nodes:
            if node.id in unreachable or node.id in dead:
                continue
            state = states.get(node.id)
            if state is not None and state.status == "completed":
                continue
            if any(dep in dead or dep in unreachable for dep in node.needs):
                unreachable.add(node.id)
                changed = True
    return sorted(unreachable)
