"""question_store.py — the runtime behind an `ask_user` question.

`AskUserTool` (src/agent_tools/interaction_tools.py) mints a question and its
options; this is what remembers that the question was asked, so the answer
that comes back later can be checked against something instead of trusted on
its face. The failure mode this exists to close (CALL-07 / TASK-04) is a
stale or duplicate decision read as consent:

* an answer to a question that was **cancelled** — the model moved on, or a
  newer question superseded it — is rejected with a reason, never silently
  applied (QA-14);
* an answer whose **revision** does not match the one the question was opened
  with is rejected the same way — the caller told us what they thought they
  were answering, and that has since changed;
* a **second** answer to an already-answered question is rejected (dedupe): a
  double click or a replayed request cannot flip a decision that was already
  recorded;
* **no answer is never a yes.** There is no code path in this module that
  turns silence, a timeout, or an expiry into an affirmative outcome (QA-13);
  `expire_stale` moves an overdue question to `expired`, a status distinct
  from `answered` that every caller must check for explicitly.

Self-contained SQLite (own file under DATA_DIR, own schema), the same shape as
`src/changeset_store.py`: no shared table, no global connection, one writer
transaction per call.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import sqlite3
from typing import Any, Dict, List, Optional
import uuid

logger = logging.getLogger(__name__)
VERSION = 1
MAX_BYTES = 200_000

#: `open` sets `open`. Every other status is terminal: once a question leaves
#: `open` nothing moves it back, which is what makes "answered" trustworthy.
STATUSES = ("open", "answered", "cancelled", "expired")


class QuestionError(ValueError):
    pass


def default_path() -> Path:
    from src.constants import DATA_DIR
    return Path(DATA_DIR) / "questions.sqlite3"


def _now() -> str:
    from src.contracts.base import now_iso
    return now_iso()


def _normalized_owner(owner: Any) -> str:
    """Same normalization as `src.tool_approvals._normalized_owner`: an
    owner comparison must not depend on case or incidental whitespace."""
    return str(owner or "").strip().casefold()


def _expires(ttl_seconds: Optional[int]) -> Optional[str]:
    if ttl_seconds is None:
        return None
    when = datetime.now(timezone.utc) + timedelta(seconds=max(1, int(ttl_seconds)))
    return when.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _json(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise QuestionError("question payload too large")
    return text


class Store:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path is not None else default_path()

    @contextmanager
    def _db(self, *, write: bool = False):
        if write:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        uri = self.path.resolve().as_uri() + ("?mode=rwc" if write else "?mode=ro")
        db = sqlite3.connect(uri, uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 0 and write:
                if db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
                    raise QuestionError("unrecognized question database")
                db.execute("""CREATE TABLE questions (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL UNIQUE,
                    session_id TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    status TEXT NOT NULL,
                    question TEXT NOT NULL,
                    options_json TEXT NOT NULL,
                    multi INTEGER NOT NULL,
                    allow_free_text INTEGER NOT NULL,
                    revision INTEGER NOT NULL,
                    opened_at TEXT NOT NULL,
                    expires_at TEXT,
                    answered_at TEXT,
                    answer_json TEXT,
                    reason TEXT NOT NULL DEFAULT '')""")
                db.execute("CREATE INDEX question_session ON questions(session_id, seq)")
                db.execute(f"PRAGMA user_version={VERSION}")
            elif version != VERSION:
                raise QuestionError("unsupported question database version")
            yield db
            if write:
                db.commit()
        except Exception:
            if write:
                db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _decode(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "question_id": row["id"],
            "session_id": row["session_id"],
            "owner": row["owner"],
            "status": row["status"],
            "question": row["question"],
            "options": json.loads(row["options_json"]),
            "multi": bool(row["multi"]),
            "allow_free_text": bool(row["allow_free_text"]),
            "revision": row["revision"],
            "opened_at": row["opened_at"],
            "expires_at": row["expires_at"],
            "answered_at": row["answered_at"],
            "answer": json.loads(row["answer_json"]) if row["answer_json"] else None,
            "reason": row["reason"] or "",
        }

    def open(self, question: str, *, session_id: str, owner: str = "",
             options: Optional[List[Dict[str, Any]]] = None, revision: int = 1,
             multi: bool = False, allow_free_text: bool = True,
             ttl_seconds: Optional[int] = None,
             question_id: Optional[str] = None,
             supersede_open: bool = True) -> Dict[str, Any]:
        """Register a freshly asked question. `question_id` lets the caller
        reuse the id it already showed the user (AskUserTool mints one so the
        streamed card and the stored record agree); left unset, one is minted
        here. When `supersede_open` (default), any other question still
        `open` for this session is cancelled first — the model asking a new
        question means the previous one is no longer the one to answer, and
        an answer that crosses in flight for it must not be read as consent
        for this one (QA-14's "stale" case)."""
        text = (question or "").strip()
        if not text:
            raise QuestionError("question text is required")
        if not session_id:
            raise QuestionError("session_id is required")
        qid = question_id or f"qst_{uuid.uuid4().hex[:20]}"
        opts = options or []
        with self._db(write=True) as db:
            if supersede_open:
                db.execute(
                    "UPDATE questions SET status='cancelled', reason='superseded' "
                    "WHERE session_id=? AND status='open'",
                    (session_id,),
                )
            db.execute(
                """INSERT INTO questions
                (id, session_id, owner, status, question, options_json, multi,
                 allow_free_text, revision, opened_at, expires_at, answered_at,
                 answer_json, reason)
                VALUES (?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, NULL, NULL, '')""",
                (qid, session_id, owner or "", text, _json(opts), int(bool(multi)),
                 int(bool(allow_free_text)), max(1, int(revision)), _now(),
                 _expires(ttl_seconds)),
            )
            row = db.execute("SELECT * FROM questions WHERE id=?", (qid,)).fetchone()
        logger.info("question opened: %s (session=%s, %d options)", qid, session_id, len(opts))
        return self._decode(row)

    def get(self, question_id: str, *, owner: Any = None) -> Optional[Dict[str, Any]]:
        """SEC-06: when `owner` is given, a question opened for a DIFFERENT
        owner is reported exactly as if it did not exist — the same
        fail-closed shape `src.tool_approvals.PendingApprovalStore.consume`
        uses, so a leaked `question_id` (a log line, a shared screenshot, a
        support ticket) cannot be used to read another owner's open question.
        `owner=None` (the default) skips the check entirely: a caller that
        never passes it — single-user/no-auth mode, or code written before
        this check existed — behaves exactly as before. A question opened
        with no owner at all (`owner=""`, e.g. a pre-SEC-06 row, or a flow
        with no authenticated user) has nothing to isolate and is never
        gated by this check either."""
        if not self.path.exists():
            return None
        with self._db() as db:
            row = db.execute("SELECT * FROM questions WHERE id=?", (question_id,)).fetchone()
            if row is None:
                return None
            if owner is not None and row["owner"] and _normalized_owner(row["owner"]) != _normalized_owner(owner):
                return None
            return self._decode(row)

    def _settle_expiry(self, db: sqlite3.Connection, row: sqlite3.Row) -> sqlite3.Row:
        """A question past its deadline is `expired`, not silently treated as
        anything else — this is the one place an `open` row's status changes
        without an explicit decision, and it never becomes `answered`."""
        if row["status"] == "open" and row["expires_at"] and _now() > row["expires_at"]:
            db.execute("UPDATE questions SET status='expired' WHERE id=? AND status='open'",
                       (row["id"],))
            row = db.execute("SELECT * FROM questions WHERE id=?", (row["id"],)).fetchone()
        return row

    def resolve(self, question_id: str, answer: Any, *,
                revision: Optional[int] = None, owner: Any = None) -> Dict[str, Any]:
        """Record the user's answer. Never interprets missing input as
        consent (QA-13): there is no default answer here, only what is
        explicitly passed as `answer`.

        SEC-06: `owner`, when given, must match the question's — checked
        first and reported as `not_found` on mismatch (never `cancelled` /
        `stale_revision` / anything that would confirm a question with this
        id exists for someone else). `owner=None` skips the check, same
        back-compat contract as `get`."""
        with self._db(write=True) as db:
            row = db.execute("SELECT * FROM questions WHERE id=?", (question_id,)).fetchone()
            if row is None:
                return {"ok": False, "reason": "not_found", "detail": question_id}
            if owner is not None and row["owner"] and _normalized_owner(row["owner"]) != _normalized_owner(owner):
                return {"ok": False, "reason": "not_found", "detail": question_id}
            row = self._settle_expiry(db, row)
            if row["status"] == "cancelled":
                return {"ok": False, "reason": "cancelled",
                        "detail": row["reason"] or "question was cancelled"}
            if row["status"] == "expired":
                return {"ok": False, "reason": "expired", "detail": row["expires_at"]}
            if row["status"] == "answered":
                # Dedupe: the first answer stands, the second is told so
                # rather than silently ignored or allowed to overwrite it.
                return {"ok": False, "reason": "already_answered",
                        "detail": f"answered at {row['answered_at']}"}
            if revision is not None and int(revision) != row["revision"]:
                return {"ok": False, "reason": "stale_revision",
                        "detail": f"question is at revision {row['revision']}"}
            stamp = _now()
            changed = db.execute(
                "UPDATE questions SET status='answered', answered_at=?, answer_json=? "
                "WHERE id=? AND status='open' AND revision=?",
                (stamp, _json(answer), question_id, row["revision"]),
            ).rowcount
            if not changed:
                # Lost a race (another resolve/cancel/expiry won first) —
                # report the current terminal state rather than raise.
                current = db.execute("SELECT * FROM questions WHERE id=?", (question_id,)).fetchone()
                return {"ok": False, "reason": f"already_{current['status']}" if current is not None else "not_found",
                        "detail": "concurrent change"}
            updated = db.execute("SELECT * FROM questions WHERE id=?", (question_id,)).fetchone()
        logger.info("question answered: %s", question_id)
        return {"ok": True, "reason": "answered", "question": self._decode(updated)}

    def cancel(self, question_id: str, *, reason: str = "", owner: Any = None) -> Dict[str, Any]:
        """SEC-06: same owner gate as `resolve` — a mismatch reads as
        `not_found`, `owner=None` skips the check."""
        with self._db(write=True) as db:
            row = db.execute("SELECT * FROM questions WHERE id=?", (question_id,)).fetchone()
            if row is None:
                return {"ok": False, "reason": "not_found", "detail": question_id}
            if owner is not None and row["owner"] and _normalized_owner(row["owner"]) != _normalized_owner(owner):
                return {"ok": False, "reason": "not_found", "detail": question_id}
            if row["status"] != "open":
                return {"ok": False, "reason": f"already_{row['status']}",
                        "detail": "only an open question can be cancelled"}
            changed = db.execute(
                "UPDATE questions SET status='cancelled', reason=? WHERE id=? AND status='open'",
                (reason or "cancelled", question_id),
            ).rowcount
            if not changed:
                return {"ok": False, "reason": "concurrent_change", "detail": question_id}
        logger.info("question cancelled: %s (%s)", question_id, reason or "no reason given")
        return {"ok": True, "reason": "cancelled"}

    def list_open(self, *, owner: Any = None, limit: int = 50) -> List[Dict[str, Any]]:
        """Every question still `open`, most recently opened first — the
        activity tray's "answer this" queue (ACT-03).

        SEC-06: `owner`, when given, is a positive filter, the same
        fail-open-for-unowned-rows shape `get()`/`resolve()` already use
        (`owner=None` — the default — lists every owner's open questions, for
        a caller that has already decided it wants that; a row opened with no
        owner at all has nothing to isolate and is never excluded by this
        filter either).

        Expiry is settled per row before it is reported (same as
        `resolve()`/`cancel()` already do), so a question sitting past its
        deadline never shows up in "waiting for an answer" — a listing gets no
        weaker a guarantee than a single `get()`.
        """
        if not self.path.exists():
            return []
        limit = max(1, min(int(limit or 50), 200))
        out: List[Dict[str, Any]] = []
        with self._db(write=True) as db:  # write: _settle_expiry may UPDATE
            rows = db.execute(
                "SELECT * FROM questions WHERE status='open' ORDER BY seq DESC"
            ).fetchall()
            for row in rows:
                row = self._settle_expiry(db, row)
                if row["status"] != "open":
                    continue
                if owner is not None and row["owner"] and _normalized_owner(row["owner"]) != _normalized_owner(owner):
                    continue
                out.append(self._decode(row))
                if len(out) >= limit:
                    break
        return out

    def expire_stale(self, *, now: Optional[str] = None) -> int:
        """Sweep every open question past its deadline. Idempotent, safe to
        call from a scheduler: an expired question is a fact, not a cleanup."""
        stamp = now or _now()
        with self._db(write=True) as db:
            changed = db.execute(
                "UPDATE questions SET status='expired' "
                "WHERE status='open' AND expires_at IS NOT NULL AND expires_at < ?",
                (stamp,),
            ).rowcount
        return changed


def open_question(question: str, *, session_id: str, owner: str = "", **kwargs) -> Dict[str, Any]:
    """Module-level convenience over the default store (mirrors
    approval_store's function-style API for callers that don't need a
    specific db path, e.g. agent_loop.py)."""
    return Store().open(question, session_id=session_id, owner=owner, **kwargs)


def resolve_question(question_id: str, answer: Any, *, revision: Optional[int] = None,
                      owner: Any = None) -> Dict[str, Any]:
    return Store().resolve(question_id, answer, revision=revision, owner=owner)


def cancel_question(question_id: str, *, reason: str = "", owner: Any = None) -> Dict[str, Any]:
    return Store().cancel(question_id, reason=reason, owner=owner)


def get_question(question_id: str, *, owner: Any = None) -> Optional[Dict[str, Any]]:
    return Store().get(question_id, owner=owner)


def list_open(*, owner: Any = None, limit: int = 50) -> List[Dict[str, Any]]:
    return Store().list_open(owner=owner, limit=limit)
