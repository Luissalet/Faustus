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
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Dict, List, Mapping, Optional

from src.contracts import (
    NodeRun, WorkflowDefinition, WorkflowRun, idempotency_key,
)
from src.contracts.base import now_iso

logger = logging.getLogger(__name__)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class WorkflowStore:
    """Rows in, contracts out. No policy, no scheduling — only durability."""

    # ── runs ──────────────────────────────────────────────────────────────

    def create_run(self, definition: WorkflowDefinition, *, owner: str = "",
                   project_id: str = "", trigger: str = "manual",
                   inputs: Optional[Mapping[str, Any]] = None,
                   dedupe_key: str = "") -> Dict[str, Any]:
        """Open a run. With a `dedupe_key`, a second call for the same real
        event loses on the unique index and returns the run that already
        exists — which is how a redelivered webhook stops being two runs."""
        from core.database import SessionLocal, WorkflowRunRow

        db = SessionLocal()
        try:
            if dedupe_key:
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
            db.commit()
            return {"created": True, "reason": "created", "run_id": run.id,
                    "status": run.status}
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

    def set_run_status(self, run_id: str, status: str, *, reason: str = "") -> bool:
        from core.database import SessionLocal, WorkflowRunRow
        from src.contracts.workflow import TERMINAL_WORKFLOW

        db = SessionLocal()
        try:
            row = db.get(WorkflowRunRow, run_id)
            if row is None:
                return False
            row.status = status
            if reason:
                row.reason = reason
            if status == "running" and not row.started_at:
                row.started_at = now_iso()
            if status in TERMINAL_WORKFLOW:
                row.ended_at = row.ended_at or now_iso()
                row.started_at = row.started_at or row.ended_at
            db.commit()
            return True
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

    def start_node(self, run_id: str, node, *, attempt: int,
                   inputs: Any = None) -> Dict[str, Any]:
        """Claim a node BEFORE doing its work.

        The key is derived from the plan, so a retry produces the same one and
        loses on the unique index. That is the guarantee doing the actual work
        in a `try` never gives you: two processes cannot both believe they
        opened this attempt."""
        from core.database import NodeRunRow, SessionLocal

        key = idempotency_key(workflow_run_id=run_id, node_id=node.id,
                              config=node.config, inputs=inputs)
        db = SessionLocal()
        try:
            clash = (db.query(NodeRunRow)
                     .filter(NodeRunRow.idempotency_key == key).first())
            if clash is not None:
                return {"claimed": False, "reason": "already_attempted",
                        "status": clash.status, "attempt": clash.attempt,
                        "idempotency_key": key,
                        "result": json.loads(clash.result_json or "{}")}

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
                reopened.idempotency_key = key
                reopened.status = "running"
                reopened.attempt = attempt
                reopened.started_at = reopened.started_at or now_iso()
                reopened.ended_at = None
                db.commit()
                return {"claimed": True, "reason": "reclaimed",
                        "idempotency_key": key, "attempt": attempt}

            db.add(NodeRunRow(
                id=f"nr_{uuid.uuid4().hex[:20]}", workflow_run_id=run_id,
                node_id=node.id, status="running", attempt=attempt,
                idempotency_key=key, started_at=now_iso(), reason="",
                result_json="{}", schema_version=1,
            ))
            db.commit()
            return {"claimed": True, "reason": "claimed", "idempotency_key": key,
                    "attempt": attempt}
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()


    def finish_node(self, run_id: str, node_id: str, *, status: str,
                    result: Optional[Mapping[str, Any]] = None,
                    reason: str = "", approval_id: str = "") -> bool:
        """Write what happened, before the next node is chosen."""
        from core.database import NodeRunRow, SessionLocal
        from src.contracts.workflow import TERMINAL_NODE

        db = SessionLocal()
        try:
            row = (db.query(NodeRunRow)
                   .filter(NodeRunRow.workflow_run_id == run_id,
                           NodeRunRow.node_id == node_id)
                   .order_by(NodeRunRow.attempt.desc()).first())
            if row is None:
                return False
            row.status = status
            row.reason = reason or row.reason
            row.approval_id = approval_id or row.approval_id
            if result is not None:
                row.result_json = _json(dict(result))
            if status in TERMINAL_NODE:
                row.ended_at = row.ended_at or now_iso()
            db.commit()
            return True
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
            row.idempotency_key = None
            row.status = "pending"
            db.commit()
            return True
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
            row = (db.query(NodeRunRow)
                   .filter(NodeRunRow.workflow_run_id == run_id,
                           NodeRunRow.node_id == node_id,
                           NodeRunRow.attempt == attempt).first())
            if row is None or row.status == "completed":
                return False
            row.idempotency_key = None
            db.commit()
            return True
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
