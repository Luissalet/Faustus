"""
workflows/store.py — the part that has to be written down before anything runs.

Every method here exists so that a process killed at the worst possible moment
comes back to a row that says what it was doing. The ordering is the design:

* `start_node` writes `running` **with the idempotency key** and commits,
  before the handler is called;
* `finish_node` writes the result and commits, before the next node is chosen.

The key is unique in the table. A retry derives the same key, so the second
`start_node` loses on the unique index rather than opening a second attempt —
which is the database enforcing what the engine intends, instead of the engine
being careful.

Three things follow from taking that seriously, and they are the rest of this
module:

**Losing a race is an answer, not an exception.** A read-then-insert leaves a
window: two callers both see nothing and both insert, and one gets an
`IntegrityError` back. Raising it at the caller reports a crash for work that
is going perfectly well in another process, so every unique index here is
caught and turned into a lookup of the winner.

**A claim is a lease, not a permanent `running`.** A process killed after
claiming used to leave a row saying it was working forever, holding the key
that refuses every retry — a node stuck for good, by design. The claim now
expires, and `recover_expired_node_leases` decides what that expiry means.

**`unknown_effect` is not `failed`.** When a node that reaches outside dies
mid-call, nobody watched: it may have sent the message and died before writing
that down, or died before dialling. Retrying might send it twice and not
retrying might mean it never went. That is a state of its own, it is recorded
as one, and it asks for reconciliation or a person rather than a retry policy.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional

from src.contracts import (
    NodeRun, WorkflowDefinition, WorkflowRun, idempotency_key,
)
from src.contracts.base import now_iso
from src.contracts.workflow import EFFECTFUL_TYPES

logger = logging.getLogger(__name__)

#: How long a node claim stands with nobody touching it. Long enough that a
#: skill that genuinely takes a quarter of an hour is not declared dead under
#: itself; short enough that a run killed at lunchtime is answerable in the
#: afternoon. `heartbeat_node` is what keeps a longer one alive.
LEASE_SECONDS = 900

#: What is known about a node's side effect, which is a different question
#: from what is known about the attempt.
#:
#: none      — the node does not reach outside, or has not acted yet.
#: pending   — a handler said it is about to act. Set by the handler, because
#:             only the handler knows where in its own call it is.
#: confirmed — the handler returned, so whatever it did, it finished doing it.
#: unknown   — the worker died across the call. Neither retrying nor giving up
#:             is safe without asking the provider or a person.
EFFECT_STATES = ("none", "pending", "confirmed", "unknown")

#: This process, as a claim holder. The random tail keeps a recycled pid from
#: inheriting the claims of whatever held that pid before it.
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"

#: The boundaries where a process dying leaves the row and the world
#: disagreeing. Named so a test can kill exactly one of them and ask what the
#: next pass does — see `fault`.
FAULT_POINTS = ("before_run_insert", "after_run_insert", "before_claim",
                "before_insert", "after_claim", "before_effect",
                "after_effect", "before_result", "after_result")

_fault_hook: Optional[Callable[..., None]] = None


def set_fault_hook(hook: Optional[Callable[..., None]]):
    """Install (or clear, with None) the callable every fault point calls.

    Returns the hook that was there, so a test can put it back rather than
    leave a process-wide switch flipped for whatever runs next.
    """
    global _fault_hook
    previous = _fault_hook
    _fault_hook = hook
    return previous


def fault(point: str, **context: Any) -> None:
    """A named place where the process may be killed.

    In production this does nothing at all. In a test it is the only honest
    way to ask "if we die HERE, does the next pass do the right thing?" — the
    alternative is monkeypatching an internal and then testing the
    monkeypatch. Handlers call it too, at `before_effect` and `after_effect`,
    because the two moments that matter most are on either side of the one
    call this module cannot see.
    """
    if _fault_hook is not None:
        _fault_hook(point, **context)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _iso_in(seconds: int) -> str:
    """`now_iso()` plus a number of seconds, in exactly the same shape.

    The lease columns are compared as strings against `now_iso()` — the same
    trick `wake_at` already uses — so they have to be minted the same way or
    the comparison quietly stops meaning anything.
    """
    moment = (datetime.now(timezone.utc).replace(microsecond=0)
              + timedelta(seconds=max(1, int(seconds))))
    return moment.isoformat().replace("+00:00", "Z")


class WorkflowStore:
    """Rows in, contracts out. No policy, no scheduling — only durability."""

    # ── runs ──────────────────────────────────────────────────────────────

    def create_run(self, definition: WorkflowDefinition, *, owner: str = "",
                   project_id: str = "", trigger: str = "manual",
                   inputs: Optional[Mapping[str, Any]] = None,
                   dedupe_key: str = "",
                   budget_preset: str = "", permissions: Optional[Any] = None) -> Dict[str, Any]:
        """Open a run. With a `dedupe_key`, a second call for the same real
        event loses on the unique index and returns the run that already
        exists — which is how a redelivered webhook stops being two runs.

        AUTO-02: `budget_preset` (one of `src.autonomy_budget.PRESETS`) and
        `permissions` (a list of node TYPES or `config['action']` values this
        run may execute — `None` means "whatever the definition's own nodes
        already are", never wider) are declared HERE, at creation, and never
        change for this run. There is no column for them on `WorkflowRunRow`
        (core/database.py is outside this batch's file list) — they live
        under the reserved `__policy__` key of the run's own `inputs`, which
        is already a freeform JSON blob every run has. `WorkflowStore.get_policy`
        reads them back; `WorkflowEngine.advance` is what enforces them."""
        from core.database import SessionLocal, WorkflowRunRow
        from sqlalchemy.exc import IntegrityError

        merged_inputs = dict(inputs or {})
        if budget_preset or permissions is not None:
            merged_inputs["__policy__"] = {
                "budget_preset": budget_preset or "",
                "permissions": list(permissions) if permissions is not None else None,
            }
        inputs = merged_inputs

        db = SessionLocal()
        try:
            if dedupe_key:
                # A fast path, and only that: two redeliveries can both find
                # nothing here. The unique index below is what actually
                # decides, and this lookup only saves the common case an
                # exception.
                existing = (db.query(WorkflowRunRow)
                            .filter(WorkflowRunRow.dedupe_key == dedupe_key).first())
                if existing is not None:
                    return {"created": False, "reason": "duplicate_trigger",
                            "run_id": existing.id, "status": existing.status}
            run = WorkflowRun.parse({
                "id": f"wfr_{uuid.uuid4().hex[:20]}",
                "workflow_id": definition.id,
                "workflow_version": definition.version,
                "definition_fingerprint": definition.fingerprint(),
                "status": "pending", "owner": owner, "project_id": project_id,
                "trigger": trigger, "created_at": now_iso(),
                "inputs": dict(inputs or {}),
            })
            db.add(WorkflowRunRow(
                id=run.id, workflow_id=run.workflow_id,
                workflow_version=run.workflow_version,
                definition_fingerprint=run.definition_fingerprint,
                definition_json=_json(definition.to_dict()),
                status=run.status, owner=owner or None,
                project_id=project_id or None, trigger=run.trigger,
                inputs_json=_json(dict(inputs or {})),
                created_at_iso=run.created_at, reason="",
                dedupe_key=dedupe_key or None, schema_version=run.schema_version,
            ))
            fault("before_run_insert", run_id=run.id, dedupe_key=dedupe_key)
            db.commit()
            fault("after_run_insert", run_id=run.id, dedupe_key=dedupe_key)
            return {"created": True, "reason": "created", "run_id": run.id,
                    "status": run.status}
        except IntegrityError:
            # Both callers passed the lookup above and both inserted; this one
            # lost on `dedupe_key`. The loser wants the winner's run id, not a
            # stack trace: the caller asked for "a run for this event", and
            # there is one.
            db.rollback()
            winner = (db.query(WorkflowRunRow)
                      .filter(WorkflowRunRow.dedupe_key == dedupe_key).first()
                      if dedupe_key else None)
            if winner is None:
                raise                       # a different constraint; not ours to swallow
            return {"created": False, "reason": "duplicate_trigger",
                    "run_id": winner.id, "status": winner.status}
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()


    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        """The run and the definition it started under, together — the caller
        must never have to fetch the definition separately and risk getting a
        newer one."""
        from core.database import SessionLocal, WorkflowRunRow

        db = SessionLocal()
        try:
            row = db.get(WorkflowRunRow, run_id)
            if row is None:
                return None
            return {
                "run": WorkflowRun.parse({
                    "id": row.id, "workflow_id": row.workflow_id,
                    "workflow_version": row.workflow_version,
                    "definition_fingerprint": row.definition_fingerprint or "",
                    "status": row.status, "owner": row.owner or "",
                    "project_id": row.project_id or "", "trigger": row.trigger,
                    "created_at": row.created_at_iso, "started_at": row.started_at,
                    "ended_at": row.ended_at, "reason": row.reason or "",
                    "inputs": json.loads(row.inputs_json or "{}"),
                }),
                "definition": WorkflowDefinition.parse(json.loads(row.definition_json)),
            }
        finally:
            db.close()

    def get_policy(self, run_id: str) -> Dict[str, Any]:
        """This run's AUTO-02 budget/permissions, defaulted so a run created
        before this existed — or without either argument — behaves exactly
        as it always did: the `supervised` preset, and no permission
        restriction beyond what the definition's own nodes already are."""
        from src.autonomy_budget import DEFAULT_PRESET, PRESETS

        loaded = self.get_run(run_id)
        raw = ((loaded["run"].inputs if loaded else {}) or {}).get("__policy__") or {}
        preset = raw.get("budget_preset") or DEFAULT_PRESET
        if preset not in PRESETS:
            preset = DEFAULT_PRESET
        permissions = raw.get("permissions")
        return {"budget_preset": preset,
                "permissions": list(permissions) if isinstance(permissions, list) else None}

    def usage_so_far(self, run_id: str) -> Dict[str, float]:
        """Tool-call count and active seconds spent by this run's EFFECTFUL
        nodes, computed from `node_runs` — durable state this table already
        keeps, so the AUTO-02 ledger needs no storage of its own and survives
        a restart exactly as well as the run itself does."""
        from src.contracts.workflow import EFFECTFUL_TYPES, TERMINAL_NODE

        loaded = self.get_run(run_id)
        if loaded is None:
            return {"tool_calls": 0, "active_seconds": 0.0}
        types = self._node_types_public(loaded["definition"])
        states = self.node_runs(run_id)
        tool_calls = 0
        active_seconds = 0.0
        for node_id, run in states.items():
            if run.status not in TERMINAL_NODE:
                continue
            if types.get(node_id) not in EFFECTFUL_TYPES:
                continue
            tool_calls += 1
            if run.started_at and run.ended_at:
                try:
                    from datetime import datetime as _dt
                    start = _dt.fromisoformat(run.started_at.replace("Z", "+00:00"))
                    end = _dt.fromisoformat(run.ended_at.replace("Z", "+00:00"))
                    active_seconds += max(0.0, (end - start).total_seconds())
                except Exception:
                    pass
        return {"tool_calls": tool_calls, "active_seconds": active_seconds}

    @staticmethod
    def _node_types_public(definition: WorkflowDefinition) -> Dict[str, str]:
        return {n.id: n.type for n in definition.nodes}

    def set_run_status(self, run_id: str, status: str, *, reason: str = "") -> bool:
        from core.database import SessionLocal, WorkflowRunRow
        from src.contracts.workflow import TERMINAL_WORKFLOW
        from sqlalchemy import func

        db = SessionLocal()
        try:
            updates = {'status': status}
            if reason or status in ('running', 'completed'):
                updates['reason'] = reason
            if status == "running":
                updates['started_at'] = func.coalesce(WorkflowRunRow.started_at, now_iso())
            if status in TERMINAL_WORKFLOW:
                moment = now_iso()
                updates['ended_at'] = func.coalesce(WorkflowRunRow.ended_at, moment)
                updates['started_at'] = func.coalesce(WorkflowRunRow.started_at, moment)
            # The predicate and write are one database operation. A late
            # completion/pause cannot undo a cancellation or any terminal state.
            changed = (db.query(WorkflowRunRow)
                       .filter(WorkflowRunRow.id == run_id,
                               WorkflowRunRow.status.notin_(TERMINAL_WORKFLOW))
                       .update(updates, synchronize_session=False))
            db.commit()
            return bool(changed)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()


    # ── nodes ─────────────────────────────────────────────────────────────

    def node_runs(self, run_id: str) -> Dict[str, NodeRun]:
        from core.database import NodeRunRow, SessionLocal

        db = SessionLocal()
        try:
            rows = (db.query(NodeRunRow)
                    .filter(NodeRunRow.workflow_run_id == run_id)
                    .order_by(NodeRunRow.attempt.desc()).all())
            out: Dict[str, NodeRun] = {}
            for row in rows:
                if row.node_id in out:
                    continue                # keep the latest attempt only
                out[row.node_id] = NodeRun.parse({
                    "workflow_run_id": row.workflow_run_id, "node_id": row.node_id,
                    "status": row.status, "attempt": row.attempt,
                    "idempotency_key": row.idempotency_key or "",
                    "started_at": row.started_at, "ended_at": row.ended_at,
                    "reason": row.reason or "", "approval_id": row.approval_id or "",
                    "result": json.loads(row.result_json or "{}"),
                })
            return out
        finally:
            db.close()

    @staticmethod
    def _already_attempted(row, key: str) -> Dict[str, Any]:
        """The answer when somebody else owns this attempt: their row.

        Handed back identically whether the loser found the clash in the
        lookup or in the unique index, so a caller cannot tell — and should
        not have to — which of the two ways it lost."""
        return {"claimed": False, "reason": "already_attempted",
                "status": row.status, "attempt": row.attempt,
                "idempotency_key": key,
                "effect_state": getattr(row, "effect_state", None) or "none",
                "result": json.loads(row.result_json or "{}")}

    def start_node(self, run_id: str, node, *, attempt: int,
                   inputs: Any = None, worker_id: str = "",
                   lease_seconds: int = LEASE_SECONDS) -> Dict[str, Any]:
        """Claim a node BEFORE doing its work, and let the database decide.

        The key is derived from the plan, so a retry produces the same one and
        loses on the unique index. That is the guarantee doing the actual work
        in a `try` never gives you: two processes cannot both believe they
        opened this attempt.

        The lookup in front of the insert is a fast path, not the decision —
        two callers can both pass it. So the `IntegrityError` is caught here
        and turned into the winner's row: a claim that raised would report a
        crash for work another process is doing perfectly well.

        And the claim is a LEASE. A worker killed at this point used to leave
        `running` with the key that refuses every retry, which is a node stuck
        for good; now the lease expires and `recover_expired_node_leases`
        decides what that means for this particular node type.
        """
        from core.database import NodeRunRow, SessionLocal
        from sqlalchemy.exc import IntegrityError

        key = idempotency_key(workflow_run_id=run_id, node_id=node.id,
                              config=node.config, inputs=inputs)
        owner = worker_id or WORKER_ID
        now = now_iso()
        expires = _iso_in(lease_seconds)
        claim = {"claimed": True, "idempotency_key": key, "attempt": attempt,
                 "worker_id": owner, "lease_expires_at": expires}
        fault("before_claim", run_id=run_id, node_id=node.id, key=key)
        db = SessionLocal()
        try:
            clash = (db.query(NodeRunRow)
                     .filter(NodeRunRow.idempotency_key == key).first())
            if clash is not None:
                return self._already_attempted(clash, key)

            # A row that was reopened (a resumed pause, a released retry) is
            # this node's record and gets claimed again in place. Inserting a
            # second row instead would grow one row per poll — a workflow
            # waiting a week on an approval, checked every minute, would end
            # up with ten thousand rows for one node — and leave two rows
            # sharing an attempt number for `finish_node` to choose between.
            reopened = (db.query(NodeRunRow)
                        .filter(NodeRunRow.workflow_run_id == run_id,
                                NodeRunRow.node_id == node.id,
                                NodeRunRow.idempotency_key.is_(None),
                                NodeRunRow.status == "pending")
                        .order_by(NodeRunRow.attempt.desc()).first())
            if reopened is not None:
                # A conditional UPDATE rather than four attribute assignments:
                # two passes can both have selected this row, and the one whose
                # UPDATE matches nothing has lost and must go and read the
                # winner instead of writing over the claim it lost.
                taken = (db.query(NodeRunRow)
                         .filter(NodeRunRow.id == reopened.id,
                                 NodeRunRow.idempotency_key.is_(None),
                                 NodeRunRow.status == "pending")
                         .update({"idempotency_key": key, "status": "running",
                                  "attempt": attempt,
                                  "started_at": reopened.started_at or now,
                                  "ended_at": None,
                                  "lease_owner": owner,
                                  "lease_expires_at": expires,
                                  "lease_heartbeat_at": now},
                                 synchronize_session=False))
                db.commit()
                if taken:
                    fault("after_claim", run_id=run_id, node_id=node.id, key=key)
                    return {**claim, "reason": "reclaimed"}
                winner = (db.query(NodeRunRow)
                          .filter(NodeRunRow.idempotency_key == key).first())
                if winner is not None:
                    return self._already_attempted(winner, key)
                # The row was taken for some OTHER key while we looked at it.
                # Fall through and open a fresh attempt: this node genuinely
                # has no record under our key.

            fault("before_insert", run_id=run_id, node_id=node.id, key=key)
            db.add(NodeRunRow(
                id=f"nr_{uuid.uuid4().hex[:20]}", workflow_run_id=run_id,
                node_id=node.id, status="running", attempt=attempt,
                idempotency_key=key, started_at=now, reason="",
                result_json="{}", schema_version=1,
                lease_owner=owner, lease_expires_at=expires,
                lease_heartbeat_at=now, effect_state="none",
            ))
            db.commit()
            fault("after_claim", run_id=run_id, node_id=node.id, key=key)
            return {**claim, "reason": "claimed"}
        except IntegrityError:
            db.rollback()
            winner = (db.query(NodeRunRow)
                      .filter(NodeRunRow.idempotency_key == key).first())
            if winner is None:
                raise                       # a different constraint; not ours to swallow
            return self._already_attempted(winner, key)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()


    def finish_node(self, run_id: str, node_id: str, *, status: str,
                    result: Optional[Mapping[str, Any]] = None,
                    reason: str = "", approval_id: str = "",
                    worker_id: str = "") -> bool:
        """Write what happened, before the next node is chosen.

        The lease goes too: the attempt is over, and a row nobody is holding
        must stop looking held or the recovery sweep will keep finding it. A
        node that COMPLETED has its effect recorded as observed — the handler
        returned, which is the only evidence this module will ever get that
        the work landed. A failure leaves `effect_state` exactly as it was,
        because whether the effect happened is precisely what a failure does
        not tell you.
        """
        from core.database import NodeRunRow, SessionLocal
        from src.contracts.workflow import TERMINAL_NODE

        fault("before_result", run_id=run_id, node_id=node_id, status=status)
        db = SessionLocal()
        try:
            row = (db.query(NodeRunRow)
                   .filter(NodeRunRow.workflow_run_id == run_id,
                           NodeRunRow.node_id == node_id)
                   .order_by(NodeRunRow.attempt.desc()).first())
            if row is None:
                return False
            query = db.query(NodeRunRow).filter(NodeRunRow.id == row.id)
            if worker_id:
                query = query.filter(NodeRunRow.status == 'running',
                                     NodeRunRow.lease_owner == worker_id)
            values = {'status': status, 'reason': reason if status == 'completed' else reason or row.reason,
                      'approval_id': approval_id or row.approval_id,
                      'lease_owner': None, 'lease_expires_at': None,
                      'lease_heartbeat_at': None}
            if result is not None:
                values['result_json'] = _json(dict(result))
            if status in TERMINAL_NODE:
                values['ended_at'] = row.ended_at or now_iso()
            if status == "completed" and (row.effect_state or "none") != "unknown":
                values['effect_state'] = 'confirmed'
            changed = query.update(values, synchronize_session=False)
            db.commit()
            if changed:
                fault("after_result", run_id=run_id, node_id=node_id, status=status)
            return bool(changed)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def reopen_node(self, run_id: str, node_id: str) -> bool:
        """Clear a paused node's claim so the next pass can attempt it again.

        Only for `paused` — a node that already completed keeps its key and its
        result forever, which is the point. Used when an approval finally comes
        through: the work has not happened yet, so a fresh attempt is correct
        and must be able to claim a new key."""
        from core.database import NodeRunRow, SessionLocal

        db = SessionLocal()
        try:
            row = (db.query(NodeRunRow)
                   .filter(NodeRunRow.workflow_run_id == run_id,
                           NodeRunRow.node_id == node_id,
                           NodeRunRow.status == "paused")
                   .order_by(NodeRunRow.attempt.desc()).first())
            if row is None:
                return False
            # The key is released, not the row: the attempt stays in the record
            # so "this waited on an approval" is still visible afterwards.
            changed = (db.query(NodeRunRow)
                       .filter(NodeRunRow.id == row.id, NodeRunRow.status == 'paused')
                       .update({'idempotency_key': None, 'status': 'pending'},
                               synchronize_session=False))
            db.commit()
            return bool(changed)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()


    def retry_node(self, run_id: str, node_id: str, definition) -> Dict[str, Any]:
        """AUTO-03: let a FINISHED, non-effectful node run again — the
        extraction step the requirement's acceptance line names, not the
        send it feeds.

        Same mechanism as `reopen_node` (clear the key, go back to
        `pending`, let the next `advance()` claim a fresh attempt), widened
        from `paused` to a node whose last attempt already reached
        `completed`, `failed` or `skipped`. An `EFFECTFUL_TYPES` node
        (`skill`, `artifact_store`, `deliver` — see `src.contracts.workflow`)
        is refused outright: it already reached outside the process once,
        and nothing in this module can tell whether reopening it would do
        that again. A paused effectful node still retries through
        `resume`/approval, which is a decision on work that has not
        happened yet — a different thing from re-running work that has.

        Downstream nodes are never touched here, which is what keeps the
        acceptance criterion true: a node already `completed` stays out of
        `ready_nodes` (`TERMINAL_NODE`) regardless of what an upstream node
        it depends on does next, so retrying the extraction cannot cascade
        into repeating a `deliver` node that already confirmed.
        """
        from core.database import NodeRunRow, SessionLocal
        from src.contracts.workflow import EFFECTFUL_TYPES

        node = next((n for n in definition.nodes if n.id == node_id), None)
        if node is None:
            return {"ok": False, "reason": f"no node {node_id!r} in this run's definition"}
        if node.type in EFFECTFUL_TYPES:
            return {"ok": False, "reason": (
                f"{node.type!r} nodes reach outside the process; retry a paused one "
                "through resume, not a bare re-run of one that already finished")}

        db = SessionLocal()
        try:
            row = (db.query(NodeRunRow)
                   .filter(NodeRunRow.workflow_run_id == run_id,
                           NodeRunRow.node_id == node_id)
                   .order_by(NodeRunRow.attempt.desc()).first())
            if row is None:
                return {"ok": False, "reason": "this node has not run yet"}
            if row.status not in ("completed", "failed", "skipped"):
                return {"ok": False, "reason": f"node is {row.status!r}, not eligible for retry"}
            # The key is released, not the row: the same row keeps carrying the
            # node's history (`reopen_node`'s own reasoning applies unchanged).
            changed = (db.query(NodeRunRow)
                       .filter(NodeRunRow.id == row.id, NodeRunRow.status == row.status)
                       .update({"idempotency_key": None, "status": "pending",
                               "reason": "", "approval_id": "", "ended_at": None},
                               synchronize_session=False))
            if changed:
                # A person asking to retry a node in a run that already
                # finished is explicitly asking to reopen that run — the one
                # case `set_run_status` deliberately refuses on its own
                # (a LATE, unrequested write must never undo a terminal
                # status; this one is neither late nor unrequested). Only
                # flips it from a TERMINAL status: a `running`/`paused` run
                # (the node could not have been `completed`/`failed` while
                # still `paused` on this exact node) is left exactly as is.
                from core.database import WorkflowRunRow
                from src.contracts.workflow import TERMINAL_WORKFLOW
                db.query(WorkflowRunRow).filter(
                    WorkflowRunRow.id == run_id,
                    WorkflowRunRow.status.in_(TERMINAL_WORKFLOW),
                ).update({"status": "running", "ended_at": None, "reason": ""},
                         synchronize_session=False)
            db.commit()
            return {"ok": bool(changed)}
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def release_key(self, run_id: str, node_id: str, attempt: int) -> bool:
        """Let a later attempt claim this node again.

        Only ever called for work that did **not** succeed — a retry after a
        failure, or a pause being resumed. A completed node keeps its key
        forever, which is the whole reason the key exists. The row stays: the
        attempt that failed is part of the record."""
        from core.database import NodeRunRow, SessionLocal

        db = SessionLocal()
        try:
            changed = (db.query(NodeRunRow)
                       .filter(NodeRunRow.workflow_run_id == run_id,
                               NodeRunRow.node_id == node_id,
                               NodeRunRow.attempt == attempt,
                               NodeRunRow.status == "pending")
                       .update({"idempotency_key": None, "lease_owner": None,
                                "lease_expires_at": None, "lease_heartbeat_at": None},
                               synchronize_session=False))
            db.commit()
            return bool(changed)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    # ── leases, and what an expired one means ─────────────────────────────

    def heartbeat_node(self, run_id: str, node_id: str, *, worker_id: str = "",
                       lease_seconds: int = LEASE_SECONDS) -> bool:
        """Push our claim forward. False once the claim is no longer ours.

        A skill that legitimately runs for half an hour must not be declared
        abandoned at minute sixteen; and a worker that lost its claim while
        wedged has to learn that from the row rather than keep believing it
        holds one.
        """
        from core.database import NodeRunRow, SessionLocal
        owner = worker_id or WORKER_ID
        now = now_iso()
        db = SessionLocal()
        try:
            kept = (db.query(NodeRunRow)
                    .filter(NodeRunRow.workflow_run_id == run_id,
                            NodeRunRow.node_id == node_id,
                            NodeRunRow.status == "running",
                            NodeRunRow.lease_owner == owner)
                    .update({"lease_heartbeat_at": now,
                             "lease_expires_at": _iso_in(lease_seconds)},
                            synchronize_session=False))
            db.commit()
            return bool(kept)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def recover_expired_node_leases(self, *, now: str = "") -> List[Dict[str, Any]]:
        """Decide what a claim held by a process that is gone actually means.

        `running` with an expired lease is the state this whole change exists
        for: the row says a node is being worked on and the key on it refuses
        every retry, so without this the run is stuck for good. What is right
        to do next depends entirely on whether the node reaches outside.

        A node that does not — a condition, a wait — simply had its attempt
        interrupted. The key is released and the row goes back to `pending`,
        which the engine picks up as a fresh attempt.

        A node that DOES reach outside is neither retried nor written off as a
        plain failure. Nobody watched that call: it may have sent the message
        and died before recording it, or died before dialling. That is
        `unknown_effect` — kept apart from `failed` precisely because the two
        need different answers — and it waits for reconciliation against the
        provider or for a person. A retry policy cannot make that call and
        should not be allowed to pretend it can.

        A node whose handler got as far as `mark_effect('confirmed')` is the
        exception to all of that: the provider answered, so nothing is owed
        outside and nobody has to go and look — only the result was lost.

        A node the run's stored definition no longer describes is treated as
        effectful. Guessing wrong towards `unknown_effect` costs somebody a
        look; guessing wrong the other way sends the email twice.
        """
        from core.database import NodeRunRow, SessionLocal, WorkflowRunRow
        moment = now or now_iso()
        handled: List[Dict[str, Any]] = []
        types_by_run: Dict[str, Dict[str, str]] = {}
        db = SessionLocal()
        try:
            stale = (db.query(NodeRunRow)
                     .filter(NodeRunRow.status == "running",
                             NodeRunRow.lease_expires_at.isnot(None),
                             NodeRunRow.lease_expires_at <= moment).all())
            for row in stale:
                if row.workflow_run_id not in types_by_run:
                    types_by_run[row.workflow_run_id] = self._node_types(
                        db.get(WorkflowRunRow, row.workflow_run_id))
                node_type = types_by_run[row.workflow_run_id].get(row.node_id, "")
                effectful = node_type in EFFECTFUL_TYPES or not node_type
                values = {'lease_owner': None, 'lease_expires_at': None,
                          'lease_heartbeat_at': None}
                if (row.effect_state or "none") == "confirmed":
                    # The handler said the provider answered before the worker
                    # died. That is the one case where the effect is NOT in
                    # doubt: the result was never written, but nothing is owed
                    # to the outside world, so this needs no reconciliation and
                    # must not be retried either.
                    values['status'] = 'failed'
                    values['ended_at'] = row.ended_at or moment
                    values['reason'] = (
                        "the effect landed and was confirmed before the worker "
                        "stopped answering, but its result was never written; "
                        "nothing is owed outside, so this is not retried")
                    outcome = "effect_confirmed"
                elif effectful:
                    values['status'] = 'failed'
                    values['ended_at'] = row.ended_at or moment
                    values['effect_state'] = 'unknown'
                    values['reason'] = (
                        f"unknown_effect: the worker holding this {node_type or 'unknown'} "
                        f"node stopped answering across its call, so nobody saw whether "
                        f"the effect happened; this needs reconciling with the provider "
                        f"or a decision, not a retry")
                    outcome = "unknown_effect"
                else:
                    values['status'] = 'pending'
                    values['idempotency_key'] = None
                    values['reason'] = (
                        f"the worker holding this {node_type} node stopped answering; "
                        f"the node reaches nothing outside Faustus, so the attempt is "
                        f"simply released")
                    outcome = "released"
                # The worker may heartbeat or finish after the SELECT. Recovery
                # must lose that race, not overwrite the fresh result/lease.
                changed = (db.query(NodeRunRow)
                           .filter(NodeRunRow.id == row.id,
                                   NodeRunRow.status == 'running',
                                   NodeRunRow.lease_owner == row.lease_owner,
                                   NodeRunRow.lease_expires_at == row.lease_expires_at,
                                   NodeRunRow.effect_state == row.effect_state)
                           .update(values, synchronize_session=False))
                if not changed:
                    continue
                handled.append({"run_id": row.workflow_run_id, "node_id": row.node_id,
                                "attempt": row.attempt, "node_type": node_type,
                                "outcome": outcome})
            if handled:
                db.commit()
                logger.warning("Recovered %d expired workflow node lease(s): %s",
                               len(handled),
                               ", ".join(f"{h['run_id']}/{h['node_id']}={h['outcome']}"
                                         for h in handled))
            return handled
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _node_types(run_row) -> Dict[str, str]:
        """Node id to node type, read off the definition the RUN started under.

        Never off the current definition on disk: a workflow edited while a run
        was in flight must not change what recovery decides about that run.
        """
        if run_row is None:
            return {}
        try:
            body = json.loads(run_row.definition_json or "{}")
            return {str(n.get("id")): str(n.get("type") or "")
                    for n in (body.get("nodes") or []) if n.get("id")}
        except Exception:
            logger.debug("could not read the definition for run %s",
                         getattr(run_row, "id", "?"), exc_info=True)
            return {}

    def mark_effect(self, run_id: str, node_id: str, state: str, *,
                    worker_id: str = "", attempt: Optional[int] = None) -> bool:
        """Record what the handler knows about its own side effect.

        The handler is the only thing that can say "I am about to call the
        provider" (`pending`) or "the provider answered" (`confirmed`), and
        that is exactly the knowledge a recovery sweep is missing when it
        finds the row afterwards. Pairs with `fault('before_effect')` and
        `fault('after_effect')`, which is where a test kills it.
        """
        if state not in EFFECT_STATES:
            raise ValueError(f"effect state must be one of {EFFECT_STATES}, not {state!r}")
        from core.database import NodeRunRow, SessionLocal, WorkflowRunRow
        db = SessionLocal()
        try:
            row = (db.query(NodeRunRow)
                   .filter(NodeRunRow.workflow_run_id == run_id,
                           NodeRunRow.node_id == node_id)
                   .order_by(NodeRunRow.attempt.desc()).first())
            if row is None:
                return False
            current = row.effect_state or "none"
            allowed = {"none": {"none", "pending", "confirmed", "unknown"},
                       "pending": {"pending", "confirmed", "unknown"},
                       "unknown": {"unknown", "confirmed"}, "confirmed": {"confirmed"}}
            if state not in allowed.get(current, set()):
                return False
            query = db.query(NodeRunRow).filter(
                NodeRunRow.id == row.id, NodeRunRow.status == "running",
                NodeRunRow.effect_state == row.effect_state,
                NodeRunRow.lease_owner == row.lease_owner)
            if worker_id:
                query = query.filter(NodeRunRow.lease_owner == worker_id,
                                     NodeRunRow.lease_expires_at >= now_iso())
                if state == "pending":
                    # Starting an external action requires a live run; a
                    # confirmation for an already-started call may arrive
                    # after cancellation and must still be recorded.
                    live_run = db.query(WorkflowRunRow.id).filter(
                        WorkflowRunRow.id == run_id, WorkflowRunRow.status == "running").exists()
                    query = query.filter(live_run)
            if attempt is not None:
                query = query.filter(NodeRunRow.attempt == attempt)
            changed = query.update({"effect_state": state}, synchronize_session=False)
            db.commit()
            return bool(changed)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def claim_active(self, run_id: str, node_id: str, *, worker_id: str, attempt: int) -> bool:
        """Whether this exact attempt may keep executing, without renewing it."""
        from core.database import NodeRunRow, SessionLocal, WorkflowRunRow
        with SessionLocal() as db:
            return db.query(NodeRunRow.id).join(
                WorkflowRunRow, WorkflowRunRow.id == NodeRunRow.workflow_run_id
            ).filter(
                WorkflowRunRow.id == run_id, WorkflowRunRow.status == "running",
                NodeRunRow.node_id == node_id, NodeRunRow.status == "running",
                NodeRunRow.attempt == attempt, NodeRunRow.lease_owner == worker_id,
                NodeRunRow.lease_expires_at >= now_iso(),
            ).first() is not None

    def effect_state(self, run_id: str, node_id: str, attempt: int) -> str:
        """Read one attempt's effect without confusing it with a newer worker."""
        from core.database import NodeRunRow, SessionLocal
        with SessionLocal() as db:
            row = db.query(NodeRunRow.effect_state).filter(
                NodeRunRow.workflow_run_id == run_id, NodeRunRow.node_id == node_id,
                NodeRunRow.attempt == attempt).first()
            return str(row[0] or "none") if row else "unknown"

    def needs_reconciliation(self, *, run_id: str = "") -> List[Dict[str, Any]]:
        """The nodes whose effect nobody observed.

        This is the queue `unknown_effect` exists to fill. A run that ends up
        here is not asking to be retried — it is asking somebody to go and
        look at the provider, and the point of keeping the state separate from
        `failed` is that this list can exist at all.
        """
        from core.database import NodeRunRow, SessionLocal
        db = SessionLocal()
        try:
            query = db.query(NodeRunRow).filter(NodeRunRow.effect_state == "unknown")
            if run_id:
                query = query.filter(NodeRunRow.workflow_run_id == run_id)
            return [{"run_id": r.workflow_run_id, "node_id": r.node_id,
                     "attempt": r.attempt, "status": r.status,
                     "idempotency_key": r.idempotency_key or "",
                     "reason": r.reason or "",
                     "started_at": r.started_at, "ended_at": r.ended_at}
                    for r in query.order_by(NodeRunRow.started_at.asc()).all()]
        finally:
            db.close()
