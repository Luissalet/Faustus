"""Immutable, owner-scoped receipts produced by real turn/dispatch completion.

Builders and HTTP previews never write here. A receipt freezes evidence and
its verdict, not the working tree: fetching a live diff remains a separate
operation. SQLite commits atomically; SHA-256 detects accidental corruption,
not tampering by somebody who controls the database. No global connections.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3
from typing import Any

from src import changesets
from src.contracts import ChangeSet
from src.contracts.base import now_iso

logger = logging.getLogger(__name__)
MAX_BYTES = 2_000_000
VERSION = 1


class ReceiptError(ValueError):
    pass


def default_path() -> Path:
    from src.constants import DATA_DIR
    return Path(DATA_DIR) / "changesets.sqlite3"


def workspace_key(value: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.expanduser(value))) if value else ""


def _json(value: Any) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ReceiptError("receipt is not finite JSON") from exc
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise ReceiptError("receipt exceeds 2 MB")
    return text


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Store:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path is not None else default_path()

    @contextmanager
    def _db(self, *, write: bool = False):
        if write:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        # Read-only access must not create an empty database or modify schema.
        uri = self.path.resolve().as_uri() + ("?mode=rwc" if write else "?mode=ro")
        db = sqlite3.connect(uri, uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 0 and write:
                if db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
                    raise ReceiptError("unrecognized receipt database")
                db.execute("""CREATE TABLE receipts (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL UNIQUE, owner TEXT NOT NULL,
                    project_id TEXT NOT NULL, workspace_key TEXT NOT NULL,
                    run_id TEXT NOT NULL, verified INTEGER NOT NULL,
                    payload TEXT NOT NULL, digest TEXT NOT NULL)""")
                db.execute("CREATE INDEX receipt_scope ON receipts(owner, project_id, workspace_key, seq)")
                db.execute(f"PRAGMA user_version={VERSION}")
            elif version != VERSION:
                raise ReceiptError("unsupported receipt database version")
            yield db
            if write:
                db.commit()
        except Exception:
            if write:
                db.rollback()
            raise
        finally:
            db.close()

    def save(self, changeset: ChangeSet, proof: dict, *, source: str,
             source_id: str, completed: bool) -> dict:
        """Internal completion hook only. Reusing an id cannot replace evidence."""
        if source not in {"turn", "dispatch"} or not isinstance(source_id, str) or not 1 <= len(source_id) <= 128:
            raise ReceiptError("receipt needs a completion source")
        if changeset.schema_version != 1:
            raise ReceiptError("unsupported changeset version")
        changeset = ChangeSet.parse(changeset.to_dict())
        if not isinstance(proof, dict) or proof.get("verdict") not in {
                "proved", "partial", "unproved", "contradicted"}:
            raise ReceiptError("receipt needs an observed proof verdict")
        v = changeset.verification
        verified = (completed is True and proof["verdict"] == "proved"
                    and v.passed and not v.inconclusive and not v.pre_existing_only
                    and not v.failures and changeset.files.exact
                    and not changeset.unsupported_claims())
        payload = {"version": VERSION, "changeset": changeset.to_dict(),
                   "fingerprint": changeset.fingerprint(), "proof": proof,
                   "source": source, "source_id": source_id,
                   "completed": completed is True, "verified": bool(verified),
                   "rendered": changesets.render(changeset, proof),
                   "diff_kind": "live_workspace_not_frozen", "stored_at": now_iso()}
        encoded = _json(payload)
        with self._db(write=True) as db:
            old = db.execute("SELECT * FROM receipts WHERE id=?", (changeset.id,)).fetchone()
            if old is not None:
                existing = self._decode(old)
                proposed = dict(payload, stored_at=existing["stored_at"])
                if _json(existing) != _json(proposed):
                    raise ReceiptError("receipt id is already bound to different evidence")
                return existing
            db.execute("""INSERT INTO receipts
                (id, owner, project_id, workspace_key, run_id, verified, payload, digest)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                       (changeset.id, changeset.owner, changeset.project_id,
                        workspace_key(changeset.workspace), changeset.run_id,
                        int(verified), encoded, _digest(encoded)))
        return payload

    @staticmethod
    def _decode(row) -> dict:
        text = row["payload"]
        if len(text.encode("utf-8")) > MAX_BYTES or _digest(text) != row["digest"]:
            raise ReceiptError("receipt integrity check failed")
        try:
            payload = json.loads(text)
            cs = ChangeSet.parse(payload["changeset"])
            valid = (payload["version"] == VERSION and cs.schema_version == 1
                     and cs.id == row["id"] and cs.owner == row["owner"]
                     and cs.project_id == row["project_id"] and cs.run_id == row["run_id"]
                     and payload["verified"] == bool(row["verified"])
                     and payload["fingerprint"] == cs.fingerprint())
        except (KeyError, TypeError, ValueError) as exc:
            raise ReceiptError("invalid receipt") from exc
        if not valid:
            raise ReceiptError("receipt identity or version mismatch")
        return payload

    def get(self, receipt_id: str, *, owner: str) -> dict | None:
        if not self.path.exists():
            return None
        with self._db() as db:
            row = db.execute("SELECT * FROM receipts WHERE id=? AND owner=?",
                             (receipt_id, owner)).fetchone()
            return self._decode(row) if row is not None else None

    def list(self, *, owner: str, project_id: str | None = None,
             workspace: str | None = None, run_id: str | None = None,
             verified_only: bool = False, limit: int = 50, before: int | None = None) -> list[dict]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ReceiptError("limit must be between 1 and 200")
        if before is not None and (isinstance(before, bool) or not isinstance(before, int) or before < 1):
            raise ReceiptError("invalid cursor")
        if not self.path.exists():
            return []
        where, args = ["owner=?"], [owner]
        for column, value in (("project_id", project_id), ("workspace_key", workspace_key(workspace) if workspace is not None else None), ("run_id", run_id)):
            if value is not None:
                where.append(f"{column}=?")
                args.append(value)
        if verified_only:
            where.append("verified=1")
        if before is not None:
            where.append("seq<?")
            args.append(before)
        with self._db() as db:
            rows = db.execute("SELECT * FROM receipts WHERE " + " AND ".join(where)
                              + " ORDER BY seq DESC LIMIT ?", [*args, limit])
            out = []
            for row in rows:
                payload = self._decode(row)
                cs = payload["changeset"]
                out.append({"id": cs["id"], "title": cs["title"], "run_id": cs["run_id"],
                            "project_id": cs["project_id"], "workspace": cs["workspace"],
                            "source": payload["source"], "source_id": payload["source_id"],
                            "created_at": cs["created_at"], "stored_at": payload["stored_at"],
                            "verified": payload["verified"], "verdict": payload["proof"]["verdict"],
                            "fingerprint": payload["fingerprint"], "cursor": row["seq"]})
            return out


def record_turn(changeset: ChangeSet, proof: dict, *, session_id: str,
                completed: bool, incognito: bool = False) -> dict:
    if incognito:
        return {"stored": False, "storage_reason": "incognito"}
    try:
        saved = Store().save(changeset, proof, source="turn", source_id=session_id or changeset.id,
                             completed=completed)
        return {"stored": True, "evidence_verified": saved["verified"],
                "receipt_url": f"/api/changesets/receipts/{changeset.id}"}
    except Exception as exc:
        logger.warning("change receipt was not saved (%s)", type(exc).__name__)
        return {"stored": False, "storage_reason": "unavailable"}


def record_dispatch(job) -> None:
    """Called once after settling, outside the event loop; never breaks a job."""
    try:
        from src import dispatch
        if not job.workspace or job.proof is None:
            return
        cs = changesets.from_dispatch(dispatch.compact(job), workspace=job.workspace,
                                      owner=job.owner or "", changeset_id=f"chg_dispatch_{job.id}")
        cs = replace(cs, created_at=datetime.fromtimestamp(
            job.finished or job.created, timezone.utc).isoformat())
        # Preserve the job's proof: it includes cancelled workers and native
        # runner gaps that judging just the file list would silently discard.
        Store().save(cs, job.proof, source="dispatch", source_id=job.id,
                     completed=job.status == "done")
    except Exception as exc:
        logger.warning("dispatch receipt was not saved (%s)", type(exc).__name__)
