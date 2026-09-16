"""goal.py — Goal with completion by evidence (WP27).

Faustus already has several pieces of "did this actually finish" machinery,
each solving a different half of the problem:

* ``project_objectives`` (``src/agent_loop.py``) — a per-project dashboard of
  typed ADD/EDIT/KILL deltas, but the objectives themselves are free text
  ("los ha puesto en texto, no en los objectives" — FAUSTUS.md §63): there is
  no typed, machine-checkable acceptance criterion behind an objective's
  status, so a model can mark one "done" on prose alone.
* ``src.agent_harness.TurnLedger`` — proves a turn's CLAIMS match its
  MUTATIONS (files actually touched, tests actually run) and flags
  ``claims_without_mutation``. This is real, but it is a per-turn check, not
  a per-goal one: it says "you did not lie about this turn", not "the goal
  as a whole is satisfied".
* ``src.loop_breaker.LoopPolicy`` — a pure, bounded state machine that
  detects "the SAME (tool, args, result) N times in a row" and escalates
  nudge -> block_tool -> stop. Reused here (unchanged, imported not copied)
  for the "no new evidence across evaluations" streak, because the same
  discipline applies one level up: not "the same tool call repeated" but
  "the same set of unmet criteria repeated".
* ``src.budget_account`` — per-run token/cost ledger; a goal's ``ceiling``
  reads it as one of several possible/optional signals, but does not
  replace it (CONTRATO.md rule 1: no parallel authority for budget).
* ``src.project_tests`` — runs the project's real test runner after a turn.
  ``Goal``'s ``test_passes`` criterion is deliberately NOT a wrapper over
  that module: ``project_tests`` auto-detects a runner and scopes to
  "related" files for a *turn*; a Goal's acceptance criterion names an exact
  command up front, at goal-definition time, so two different goals on the
  same project can require two different, independently reproducible
  commands without fighting over runner auto-detection.

What is new here: a **Goal** is a typed target with typed, checkable
**acceptance criteria** — never prose — a **floor** (the minimum criteria
that must pass for "done") and a **ceiling** (a hard stop: rounds, tokens,
seconds or cost, after which the goal stops trying even if it is not done —
ADR-13, "Goal tiene techo además de suelo"). ``evaluate()`` is the ONLY
place a goal can become ``done``: it runs the real checkers (subprocess,
filesystem, artifact store, an HTTP probe, another document's revision) and
appends real evidence. A model calling a tool can never set ``status="done"``
directly — see ``src/agent_tools/goal_tools.py``: ``goal_evidence`` attaches
evidence, but only after this module's own verifier confirms the ref is
real, and even a verified single piece of evidence does not flip the
goal to done — only the next ``evaluate()`` does, having re-checked
everything itself.

Persistence: sqlite under ``DATA_DIR/creator/goals.db``, same pattern as
``src/budget_account.py``/``src/harness_evolution/store.py`` (CONTRATO.md
rule 2) — one connection per call, WAL, ``BEGIN IMMEDIATE`` for real
transactions, no in-process lock as the authority. Two append-only tables
(``creator_goal_evidence``, ``creator_goal_events``) back the goal's
``evidence`` list and its evaluation history; the ``creator_goals`` row
itself carries only current state plus an optimistic ``revision`` (CAS,
same discipline as ``src/creator/store.py``'s ``DocumentStore``).
"""
from __future__ import annotations

import glob as _glob
import hashlib
import json
import os
import shlex
import sqlite3
import subprocess
import threading
import time
import urllib.request
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from .errors import CreatorError

__all__ = [
    "SCHEMA_VERSION", "CRITERION_KINDS", "STATES", "DEFAULT_NO_PROGRESS_LIMIT",
    "GoalError", "GoalNotFound", "InvalidGoal", "GoalRevisionConflict",
    "Criterion", "Ceiling", "EvidenceEntry", "Goal", "GoalReport",
    "GoalStore", "default_path", "get_store", "evaluate", "continue_step",
    "register_custom_checker",
]

SCHEMA_VERSION = 1
_BUSY_TIMEOUT_S = 30

#: The six typed, comprobable criterion kinds the ficha names. Nothing
#: outside this tuple is a valid ``Criterion.kind`` — a prose "looks good"
#: criterion cannot be constructed at all.
CRITERION_KINDS = (
    "test_passes", "file_exists", "artifact_present", "http_ok",
    "doc_revision_at_least", "custom_check",
)

#: A Goal's lifecycle. ``done``/``ceiling_reached``/``abandoned`` are
#: terminal: :func:`evaluate` refuses to do further work once in one of
#: them (see its docstring) — "el loop no sigue por inercia" is enforced
#: here, not left to the caller's discipline.
STATES = ("open", "progressing", "blocked", "done", "ceiling_reached", "abandoned")
_TERMINAL_STATES = ("done", "ceiling_reached", "abandoned")

#: "Dos vueltas sin nueva evidencia pueden pausar" (05_ORQUESTACION...).
#: Two consecutive evaluations whose UNMET required criteria are identical
#: (the same criteria, in the same failing state) trip this — a criterion
#: that starts failing differently, or a newly-added evidence entry, resets
#: the streak because that IS progress.
DEFAULT_NO_PROGRESS_LIMIT = 2

DEFAULT_HTTP_TIMEOUT_S = 5.0
DEFAULT_TEST_TIMEOUT_S = 120.0


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class GoalError(CreatorError):
    """Base class for every error this module raises on purpose."""


class GoalNotFound(GoalError):
    """No goal with that id, or it belongs to someone else.

    Deliberately the same exception (and the same route response, 404) for
    both cases, per CONTRATO.md rule 3.
    """


class InvalidGoal(GoalError):
    """The goal definition (statement/acceptance/floor/ceiling) is malformed."""


class GoalRevisionConflict(GoalError):
    """A write raced another write on the same goal (optimistic CAS)."""

    def __init__(self, current_revision: int, message: str = "") -> None:
        self.current_revision = int(current_revision)
        super().__init__(message or f"stale revision; current is {self.current_revision}")


# ---------------------------------------------------------------------------
# Typed shapes
# ---------------------------------------------------------------------------

def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise InvalidGoal(msg)


@dataclass(frozen=True)
class Criterion:
    """One typed, checkable acceptance criterion.

    ``spec`` is kind-specific and validated structurally at construction
    time (see :func:`_validate_criterion_spec`) so a malformed criterion
    fails at ``goal_define`` time, not silently at ``evaluate`` time.
    """

    id: str
    kind: str
    spec: Dict[str, Any]
    required: bool = True
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "kind": self.kind, "spec": dict(self.spec),
            "required": self.required, "description": self.description,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Criterion":
        _require(isinstance(raw, dict), "each acceptance criterion must be an object")
        kind = str(raw.get("kind") or "").strip()
        _require(kind in CRITERION_KINDS, f"unknown criterion kind: {kind!r} (must be one of {CRITERION_KINDS})")
        cid = str(raw.get("id") or "").strip() or f"crit_{uuid.uuid4().hex[:10]}"
        spec = raw.get("spec")
        _require(isinstance(spec, dict), f"criterion {cid!r}: spec must be an object")
        _validate_criterion_spec(kind, spec)
        return cls(
            id=cid, kind=kind, spec=dict(spec),
            required=bool(raw.get("required", True)),
            description=str(raw.get("description") or ""),
        )


def _validate_criterion_spec(kind: str, spec: Dict[str, Any]) -> None:
    if kind == "test_passes":
        _require(bool(str(spec.get("cmd") or "").strip()), "test_passes: `spec.cmd` is required")
    elif kind == "file_exists":
        _require(bool(str(spec.get("path") or "").strip()), "file_exists: `spec.path` is required")
    elif kind == "artifact_present":
        has_occ = bool(str(spec.get("occurrence_id") or "").strip())
        has_kind = bool(str(spec.get("kind") or "").strip())
        _require(has_occ or has_kind, "artifact_present: `spec.occurrence_id` or `spec.kind` is required")
    elif kind == "http_ok":
        _require(bool(str(spec.get("url") or "").strip()), "http_ok: `spec.url` is required")
    elif kind == "doc_revision_at_least":
        _require(bool(str(spec.get("doc_id") or "").strip()), "doc_revision_at_least: `spec.doc_id` is required")
        try:
            int(spec.get("revision"))
        except (TypeError, ValueError):
            raise InvalidGoal("doc_revision_at_least: `spec.revision` must be an integer")
    elif kind == "custom_check":
        _require(bool(str(spec.get("tool") or "").strip()), "custom_check: `spec.tool` is required")


@dataclass(frozen=True)
class Ceiling:
    """The hard stop — "Goal tiene techo además de suelo" (ADR-13). Any
    field left ``None`` is simply not enforced; all ``None`` means no
    ceiling at all (an explicit, deliberate choice, never a silent default)."""

    max_rounds: Optional[int] = None
    max_tokens: Optional[int] = None
    max_seconds: Optional[float] = None
    max_cost_usd: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "max_rounds": self.max_rounds, "max_tokens": self.max_tokens,
            "max_seconds": self.max_seconds, "max_cost_usd": self.max_cost_usd,
        }

    @classmethod
    def from_dict(cls, raw: Optional[Dict[str, Any]]) -> "Ceiling":
        raw = raw or {}
        _require(isinstance(raw, dict), "ceiling must be an object")

        def _opt_num(key: str, cast):
            v = raw.get(key)
            if v is None:
                return None
            try:
                v = cast(v)
            except (TypeError, ValueError):
                raise InvalidGoal(f"ceiling.{key} must be a number")
            if v < 0:
                raise InvalidGoal(f"ceiling.{key} must not be negative")
            return v

        return cls(
            max_rounds=_opt_num("max_rounds", int),
            max_tokens=_opt_num("max_tokens", int),
            max_seconds=_opt_num("max_seconds", float),
            max_cost_usd=_opt_num("max_cost_usd", float),
        )

    def exceeded_by(self, usage: Dict[str, Any]) -> Optional[str]:
        """The first ceiling dimension `usage` has crossed, or None."""
        if self.max_rounds is not None and float(usage.get("rounds") or 0) > self.max_rounds:
            return f"rounds {usage.get('rounds')} > max_rounds {self.max_rounds}"
        if self.max_tokens is not None and float(usage.get("tokens") or 0) > self.max_tokens:
            return f"tokens {usage.get('tokens')} > max_tokens {self.max_tokens}"
        if self.max_seconds is not None and float(usage.get("seconds") or 0) > self.max_seconds:
            return f"seconds {usage.get('seconds')} > max_seconds {self.max_seconds}"
        if self.max_cost_usd is not None and float(usage.get("cost_usd") or 0) > self.max_cost_usd:
            return f"cost_usd {usage.get('cost_usd')} > max_cost_usd {self.max_cost_usd}"
        return None


@dataclass(frozen=True)
class EvidenceEntry:
    """One append-only evidence record. ``by`` is always ``"evaluate"`` for
    evidence produced by a real checker run, or ``"goal_evidence"`` for a
    manually-attached ref — and even then only after this module's own
    verifier confirmed the ref independently (see
    :func:`verify_manual_evidence`). Nothing ever writes ``by="model"``:
    there is no code path that accepts an unverified self-report."""

    criterion_id: str
    kind: str
    ref: str
    verified_at: float
    by: str
    ok: bool
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "criterion_id": self.criterion_id, "kind": self.kind, "ref": self.ref,
            "verified_at": self.verified_at, "by": self.by, "ok": self.ok,
            "detail": self.detail,
        }


@dataclass
class Goal:
    id: str
    project_id: str
    owner: str
    statement: str
    acceptance: List[Criterion]
    floor: Dict[str, Any]
    ceiling: Ceiling
    status: str
    schema_version: int
    revision: int
    usage: Dict[str, Any]
    no_progress_streak: int
    no_progress_limit: int
    last_unmet_signature: Optional[str]
    stop_reason: str
    created_at: float
    updated_at: float
    evidence: List[EvidenceEntry] = field(default_factory=list)

    def required_criterion_ids(self) -> List[str]:
        explicit = self.floor.get("required_criterion_ids") if isinstance(self.floor, dict) else None
        if isinstance(explicit, list) and explicit:
            known = {c.id for c in self.acceptance}
            return [cid for cid in explicit if cid in known]
        return [c.id for c in self.acceptance if c.required]

    def to_public_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "project_id": self.project_id, "statement": self.statement,
            "acceptance": [c.to_dict() for c in self.acceptance],
            "floor": dict(self.floor), "ceiling": self.ceiling.to_dict(),
            "status": self.status, "schema_version": self.schema_version,
            "revision": self.revision, "usage": dict(self.usage),
            "no_progress_streak": self.no_progress_streak,
            "no_progress_limit": self.no_progress_limit,
            "stop_reason": self.stop_reason,
            "created_at": self.created_at, "updated_at": self.updated_at,
            "evidence": [e.to_dict() for e in self.evidence],
        }


@dataclass(frozen=True)
class GoalReport:
    """The result of one :func:`evaluate` call — what changed, why, and the
    evidence produced, so a caller never has to re-derive it from the
    goal's own before/after state."""

    goal_id: str
    status: str
    previous_status: str
    floor_met: bool
    checked: List[Dict[str, Any]]
    met: List[str]
    unmet: List[str]
    new_evidence: List[Dict[str, Any]]
    no_progress: bool
    no_progress_streak: int
    ceiling_hit: Optional[str]
    stop_reason: str
    evaluated_at: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "goal_id": self.goal_id, "status": self.status, "previous_status": self.previous_status,
            "floor_met": self.floor_met, "checked": self.checked, "met": self.met, "unmet": self.unmet,
            "new_evidence": self.new_evidence, "no_progress": self.no_progress,
            "no_progress_streak": self.no_progress_streak, "ceiling_hit": self.ceiling_hit,
            "stop_reason": self.stop_reason, "evaluated_at": self.evaluated_at,
        }


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

def default_path() -> str:
    from src.constants import DATA_DIR
    return os.path.join(DATA_DIR, "creator", "goals.db")


def _row_to_goal(row: sqlite3.Row, evidence: List[EvidenceEntry]) -> Goal:
    return Goal(
        id=row["id"], project_id=row["project_id"], owner=row["owner"],
        statement=row["statement"],
        acceptance=[Criterion.from_dict(c) for c in json.loads(row["acceptance_json"])],
        floor=json.loads(row["floor_json"]), ceiling=Ceiling.from_dict(json.loads(row["ceiling_json"])),
        status=row["status"], schema_version=row["schema_version"], revision=row["revision"],
        usage=json.loads(row["usage_json"]), no_progress_streak=row["no_progress_streak"],
        no_progress_limit=row["no_progress_limit"], last_unmet_signature=row["last_unmet_signature"],
        stop_reason=row["stop_reason"] or "", created_at=row["created_at"], updated_at=row["updated_at"],
        evidence=evidence,
    )


class GoalStore:
    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = db_path or default_path()
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self._init_lock = threading.Lock()
        self._ensure_schema()

    @contextmanager
    def _conn(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=_BUSY_TIMEOUT_S, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys = ON")
            if immediate:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            if immediate:
                conn.execute("COMMIT")
        except Exception:
            if immediate:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        with self._init_lock, self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS creator_goals (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    revision INTEGER NOT NULL,
                    statement TEXT NOT NULL,
                    acceptance_json TEXT NOT NULL,
                    floor_json TEXT NOT NULL,
                    ceiling_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    usage_json TEXT NOT NULL,
                    no_progress_streak INTEGER NOT NULL DEFAULT 0,
                    no_progress_limit INTEGER NOT NULL DEFAULT 2,
                    last_unmet_signature TEXT,
                    stop_reason TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_creator_goals_owner_project "
                "ON creator_goals(owner, project_id)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS creator_goal_evidence (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    goal_id TEXT NOT NULL,
                    criterion_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    ref TEXT NOT NULL,
                    ok INTEGER NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    verified_at REAL NOT NULL,
                    by TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_creator_goal_evidence_goal "
                "ON creator_goal_evidence(goal_id, seq)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS creator_goal_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    goal_id TEXT NOT NULL,
                    event TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )

    # -- reads ------------------------------------------------------------

    def _evidence_for(self, conn: sqlite3.Connection, goal_id: str) -> List[EvidenceEntry]:
        rows = conn.execute(
            "SELECT criterion_id, kind, ref, ok, detail, verified_at, by "
            "FROM creator_goal_evidence WHERE goal_id = ? ORDER BY seq ASC",
            (goal_id,),
        ).fetchall()
        return [
            EvidenceEntry(
                criterion_id=r["criterion_id"], kind=r["kind"], ref=r["ref"],
                ok=bool(r["ok"]), detail=r["detail"], verified_at=r["verified_at"], by=r["by"],
            )
            for r in rows
        ]

    def get(self, owner: str, goal_id: str) -> Optional[Goal]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM creator_goals WHERE id = ? AND owner = ?",
                (goal_id, owner or ""),
            ).fetchone()
            if row is None:
                return None
            return _row_to_goal(row, self._evidence_for(conn, goal_id))

    def list_for_project(self, owner: str, project_id: str) -> List[Goal]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM creator_goals WHERE owner = ? AND project_id = ? "
                "ORDER BY updated_at DESC",
                (owner or "", project_id),
            ).fetchall()
            return [_row_to_goal(r, self._evidence_for(conn, r["id"])) for r in rows]

    def events(self, owner: str, goal_id: str) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            owned = conn.execute(
                "SELECT 1 FROM creator_goals WHERE id = ? AND owner = ?",
                (goal_id, owner or ""),
            ).fetchone()
            if owned is None:
                return []
            rows = conn.execute(
                "SELECT event, payload_json, created_at FROM creator_goal_events "
                "WHERE goal_id = ? ORDER BY seq ASC",
                (goal_id,),
            ).fetchall()
            return [
                {"event": r["event"], "payload": json.loads(r["payload_json"]), "created_at": r["created_at"]}
                for r in rows
            ]

    # -- writes -------------------------------------------------------------

    def define(
        self, owner: str, project_id: str, statement: str,
        acceptance: List[Dict[str, Any]], *,
        floor: Optional[Dict[str, Any]] = None,
        ceiling: Optional[Dict[str, Any]] = None,
        no_progress_limit: int = DEFAULT_NO_PROGRESS_LIMIT,
    ) -> Goal:
        owner = str(owner or "").strip()
        project_id = str(project_id or "").strip()
        _require(bool(owner), "owner is required")
        _require(bool(project_id), "project_id is required")
        _require(bool(str(statement or "").strip()), "statement is required")
        _require(isinstance(acceptance, list) and len(acceptance) > 0,
                  "acceptance must be a non-empty list of criteria")
        criteria = [Criterion.from_dict(c) for c in acceptance]
        ids = [c.id for c in criteria]
        _require(len(ids) == len(set(ids)), "criterion ids must be unique")
        floor = dict(floor or {})
        ceiling_obj = Ceiling.from_dict(ceiling)
        limit = max(1, int(no_progress_limit or DEFAULT_NO_PROGRESS_LIMIT))

        goal_id = f"goal_{uuid.uuid4().hex[:20]}"
        now = time.time()
        usage = {"rounds": 0, "tokens": 0, "seconds": 0.0, "cost_usd": 0.0}
        with self._conn(immediate=True) as conn:
            conn.execute(
                """
                INSERT INTO creator_goals (
                    id, project_id, owner, schema_version, revision, statement,
                    acceptance_json, floor_json, ceiling_json, status, usage_json,
                    no_progress_streak, no_progress_limit, last_unmet_signature,
                    stop_reason, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, 'open', ?, 0, ?, NULL, '', ?, ?)
                """,
                (
                    goal_id, project_id, owner, SCHEMA_VERSION, statement,
                    json.dumps([c.to_dict() for c in criteria]), json.dumps(floor),
                    json.dumps(ceiling_obj.to_dict()), json.dumps(usage), limit, now, now,
                ),
            )
            conn.execute(
                "INSERT INTO creator_goal_events (goal_id, event, payload_json, created_at) "
                "VALUES (?, 'defined', ?, ?)",
                (goal_id, json.dumps({"statement": statement, "acceptance": [c.to_dict() for c in criteria]}), now),
            )
        return self.get(owner, goal_id)  # type: ignore[return-value]

    def record_usage(
        self, owner: str, goal_id: str, *,
        rounds_delta: int = 0, tokens_delta: int = 0,
        seconds_delta: float = 0.0, cost_delta: float = 0.0,
    ) -> Goal:
        """Adds to the goal's running usage (rounds/tokens/seconds/cost) — the
        signal :meth:`Ceiling.exceeded_by` compares against. Pure bookkeeping;
        never changes ``status`` itself (only :func:`evaluate` does that)."""
        with self._conn(immediate=True) as conn:
            row = conn.execute(
                "SELECT usage_json FROM creator_goals WHERE id = ? AND owner = ?",
                (goal_id, owner or ""),
            ).fetchone()
            if row is None:
                raise GoalNotFound(goal_id)
            usage = json.loads(row["usage_json"])
            usage["rounds"] = float(usage.get("rounds") or 0) + rounds_delta
            usage["tokens"] = float(usage.get("tokens") or 0) + tokens_delta
            usage["seconds"] = float(usage.get("seconds") or 0) + seconds_delta
            usage["cost_usd"] = float(usage.get("cost_usd") or 0) + cost_delta
            now = time.time()
            conn.execute(
                "UPDATE creator_goals SET usage_json = ?, updated_at = ? WHERE id = ? AND owner = ?",
                (json.dumps(usage), now, goal_id, owner or ""),
            )
        return self.get(owner, goal_id)  # type: ignore[return-value]

    def abandon(self, owner: str, goal_id: str, reason: str = "") -> Goal:
        with self._conn(immediate=True) as conn:
            row = conn.execute(
                "SELECT status FROM creator_goals WHERE id = ? AND owner = ?",
                (goal_id, owner or ""),
            ).fetchone()
            if row is None:
                raise GoalNotFound(goal_id)
            now = time.time()
            conn.execute(
                "UPDATE creator_goals SET status = 'abandoned', stop_reason = ?, updated_at = ? "
                "WHERE id = ? AND owner = ?",
                (str(reason or "abandoned by caller"), now, goal_id, owner or ""),
            )
            conn.execute(
                "INSERT INTO creator_goal_events (goal_id, event, payload_json, created_at) "
                "VALUES (?, 'abandoned', ?, ?)",
                (goal_id, json.dumps({"reason": reason}), now),
            )
        return self.get(owner, goal_id)  # type: ignore[return-value]

    # Internal: only called from evaluate() / append_manual_evidence() below,
    # both of which hold the goal_id/owner they already validated.
    def _append_evidence(self, conn: sqlite3.Connection, goal_id: str, entries: List[EvidenceEntry]) -> None:
        now = time.time()
        for e in entries:
            conn.execute(
                "INSERT INTO creator_goal_evidence "
                "(goal_id, criterion_id, kind, ref, ok, detail, verified_at, by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (goal_id, e.criterion_id, e.kind, e.ref, int(e.ok), e.detail, e.verified_at, e.by),
            )
        conn.execute("UPDATE creator_goals SET updated_at = ? WHERE id = ?", (now, goal_id))

    def _apply_evaluation(
        self, owner: str, goal_id: str, *,
        new_status: str, no_progress_streak: int, unmet_signature: Optional[str],
        stop_reason: str, new_evidence: List[EvidenceEntry],
    ) -> Goal:
        """The one write path :func:`evaluate` uses. Everything happens in one
        ``BEGIN IMMEDIATE`` transaction: append the new evidence, update the
        goal's status/streak/signature, log the event — all or nothing."""
        now = time.time()
        with self._conn(immediate=True) as conn:
            row = conn.execute(
                "SELECT revision, status FROM creator_goals WHERE id = ? AND owner = ?",
                (goal_id, owner or ""),
            ).fetchone()
            if row is None:
                raise GoalNotFound(goal_id)
            self._append_evidence(conn, goal_id, new_evidence)
            conn.execute(
                "UPDATE creator_goals SET status = ?, revision = revision + 1, "
                "no_progress_streak = ?, last_unmet_signature = ?, stop_reason = ?, updated_at = ? "
                "WHERE id = ? AND owner = ?",
                (new_status, no_progress_streak, unmet_signature, stop_reason, now, goal_id, owner or ""),
            )
            conn.execute(
                "INSERT INTO creator_goal_events (goal_id, event, payload_json, created_at) "
                "VALUES (?, 'evaluated', ?, ?)",
                (goal_id, json.dumps({
                    "status": new_status, "no_progress_streak": no_progress_streak,
                    "stop_reason": stop_reason,
                }), now),
            )
        return self.get(owner, goal_id)  # type: ignore[return-value]

    def append_manual_evidence(
        self, owner: str, goal_id: str, entry: EvidenceEntry,
    ) -> Goal:
        """Append one independently-verified evidence entry WITHOUT touching
        ``status`` — see :func:`verify_manual_evidence`: this is only called
        once that verification already ran. The model calling
        ``goal_evidence`` therefore can attach real, checkable refs, but
        never move the goal to ``done`` — only the next :func:`evaluate`
        decides that, re-checking everything itself."""
        with self._conn(immediate=True) as conn:
            row = conn.execute(
                "SELECT 1 FROM creator_goals WHERE id = ? AND owner = ?",
                (goal_id, owner or ""),
            ).fetchone()
            if row is None:
                raise GoalNotFound(goal_id)
            self._append_evidence(conn, goal_id, [entry])
            conn.execute(
                "INSERT INTO creator_goal_events (goal_id, event, payload_json, created_at) "
                "VALUES (?, 'manual_evidence', ?, ?)",
                (goal_id, json.dumps(entry.to_dict()), time.time()),
            )
        return self.get(owner, goal_id)  # type: ignore[return-value]


_store_lock = threading.Lock()
_store: Optional[GoalStore] = None


def get_store() -> GoalStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = GoalStore()
        return _store


def reset_store_for_tests(store: Optional[GoalStore] = None) -> None:
    """Test-only seam: lets a test point the module-level singleton at a
    tmp_path-backed store instead of monkeypatching ``default_path``."""
    global _store
    with _store_lock:
        _store = store


# ---------------------------------------------------------------------------
# Criterion checkers — the only place "done" gets to mean something real
# ---------------------------------------------------------------------------

CustomChecker = Callable[[Dict[str, Any], Dict[str, Any]], Tuple[bool, str, str]]
_CUSTOM_CHECKERS: Dict[str, CustomChecker] = {}


def register_custom_checker(name: str, fn: CustomChecker) -> None:
    """Register a ``custom_check`` tool by name. ``fn(args, expect) ->
    (ok, ref, detail)``. Kept as an explicit, small registry — never a
    generic ``eval``/arbitrary-tool dispatch — so a criterion cannot be
    defined against a tool nobody vetted for this purpose."""
    _CUSTOM_CHECKERS[str(name)] = fn


def _check_file_glob_exists(args: Dict[str, Any], expect: Dict[str, Any]) -> Tuple[bool, str, str]:
    pattern = str(args.get("pattern") or "")
    matches = sorted(_glob.glob(pattern, recursive=True)) if pattern else []
    min_count = int(expect.get("min_count", 1) or 1)
    ok = len(matches) >= min_count
    ref = matches[0] if matches else pattern
    return ok, ref, f"{len(matches)} match(es) for {pattern!r} (need >= {min_count})"


def _check_env_var_set(args: Dict[str, Any], expect: Dict[str, Any]) -> Tuple[bool, str, str]:
    name = str(args.get("name") or "")
    value = os.environ.get(name)
    expected = expect.get("equals")
    ok = value is not None and (expected is None or str(value) == str(expected))
    return ok, f"env:{name}", f"value={value!r}"


register_custom_checker("file_glob_exists", _check_file_glob_exists)
register_custom_checker("env_var_set", _check_env_var_set)


def _run_test_passes(spec: Dict[str, Any], workspace: Optional[str]) -> Tuple[bool, str, str]:
    cmd = str(spec.get("cmd") or "")
    cwd = spec.get("cwd") or workspace or "."
    try:
        timeout = float(spec.get("timeout") or DEFAULT_TEST_TIMEOUT_S)
    except (TypeError, ValueError):
        timeout = DEFAULT_TEST_TIMEOUT_S
    try:
        argv = shlex.split(cmd, posix=(os.name != "nt"))
    except ValueError as exc:
        return False, cmd, f"could not parse cmd: {exc}"
    if not argv:
        return False, cmd, "empty cmd"
    try:
        proc = subprocess.run(
            argv, cwd=cwd, timeout=timeout, capture_output=True, text=True,
        )
    except subprocess.TimeoutExpired:
        return False, cmd, f"timed out after {timeout}s"
    except OSError as exc:
        return False, cmd, f"could not run: {exc}"
    tail = (proc.stdout or "")[-2000:] + (proc.stderr or "")[-1000:]
    ok = proc.returncode == 0
    return ok, f"exit_code:{proc.returncode}", tail.strip()


def _run_file_exists(spec: Dict[str, Any], workspace: Optional[str]) -> Tuple[bool, str, str]:
    path = str(spec.get("path") or "")
    full = path if os.path.isabs(path) else os.path.join(workspace or ".", path)
    ok = os.path.exists(full)
    if ok and spec.get("min_bytes"):
        try:
            ok = os.path.getsize(full) >= int(spec["min_bytes"])
        except OSError:
            ok = False
    return ok, full, "exists" if ok else "not found"


def _run_artifact_present(spec: Dict[str, Any], owner: str) -> Tuple[bool, str, str]:
    occurrence_id = str(spec.get("occurrence_id") or "")
    want_kind = str(spec.get("kind") or "")
    try:
        from src import artifact_identity
    except ImportError:
        return False, occurrence_id or want_kind, "artifact_identity unavailable"
    if occurrence_id:
        try:
            occ = artifact_identity.for_owner(occurrence_id, owner=owner)
        except Exception as exc:  # noqa: BLE001 — ArtifactNotFound/NotTheOwner et al.
            return False, occurrence_id, f"not found or not owned: {exc}"
        if want_kind and occ.kind != want_kind:
            return False, occurrence_id, f"kind {occ.kind!r} != expected {want_kind!r}"
        return True, occurrence_id, f"kind={occ.kind}"
    return False, want_kind, "occurrence_id required (kind-only lookup not indexed)"


def _run_http_ok(spec: Dict[str, Any]) -> Tuple[bool, str, str]:
    url = str(spec.get("url") or "")
    try:
        timeout = float(spec.get("timeout") or DEFAULT_HTTP_TIMEOUT_S)
    except (TypeError, ValueError):
        timeout = DEFAULT_HTTP_TIMEOUT_S
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — operator-supplied URL, local tool
            status = int(resp.status)
    except Exception as exc:  # noqa: BLE001 — any network/URL failure is "not ok", not a crash
        return False, url, str(exc)
    ok = 200 <= status < 400
    return ok, url, f"status={status}"


def _run_doc_revision_at_least(spec: Dict[str, Any], owner: str) -> Tuple[bool, str, str]:
    doc_id = str(spec.get("doc_id") or "")
    try:
        want_rev = int(spec.get("revision"))
    except (TypeError, ValueError):
        return False, doc_id, "invalid `revision` in spec"
    try:
        from src.creator.store import get_store as get_document_store
    except ImportError:
        return False, doc_id, "creator document store unavailable"
    doc = get_document_store().get(owner, doc_id)
    if doc is None:
        return False, doc_id, "document not found or not owned"
    ok = doc.revision >= want_rev
    return ok, doc_id, f"revision={doc.revision} (need >= {want_rev})"


def _run_custom_check(spec: Dict[str, Any]) -> Tuple[bool, str, str]:
    tool = str(spec.get("tool") or "")
    fn = _CUSTOM_CHECKERS.get(tool)
    if fn is None:
        return False, tool, f"unknown custom_check tool: {tool!r}"
    args = spec.get("args") or {}
    expect = spec.get("expect") or {}
    try:
        return fn(dict(args), dict(expect))
    except Exception as exc:  # noqa: BLE001 — a bad checker fails its criterion, not the whole evaluate()
        return False, tool, f"checker raised: {exc}"


def _run_criterion(c: Criterion, goal: Goal, workspace: Optional[str]) -> Tuple[bool, str, str]:
    if c.kind == "test_passes":
        return _run_test_passes(c.spec, workspace)
    if c.kind == "file_exists":
        return _run_file_exists(c.spec, workspace)
    if c.kind == "artifact_present":
        return _run_artifact_present(c.spec, goal.owner)
    if c.kind == "http_ok":
        return _run_http_ok(c.spec)
    if c.kind == "doc_revision_at_least":
        return _run_doc_revision_at_least(c.spec, goal.owner)
    if c.kind == "custom_check":
        return _run_custom_check(c.spec)
    return False, "", f"unknown criterion kind: {c.kind!r}"  # unreachable: validated at definition time


def verify_manual_evidence(goal: Goal, criterion_id: str, ref: str) -> Tuple[bool, str]:
    """Independently checks a ref the caller (typically the model, via
    ``goal_evidence``) claims is evidence for ``criterion_id`` — re-running
    that SAME criterion's real checker rather than trusting the ref as
    given. Returns ``(ok, detail)``; the caller only appends the evidence
    entry (with ``ok`` set from this) — it never flips the goal to done.
    """
    criterion = next((c for c in goal.acceptance if c.id == criterion_id), None)
    if criterion is None:
        return False, f"unknown criterion_id: {criterion_id!r}"
    ok, actual_ref, detail = _run_criterion(criterion, goal, None)
    if not ok:
        return False, detail or "criterion re-check failed"
    if ref and actual_ref and str(ref) != str(actual_ref):
        # The ref must match what the real checker independently found —
        # a plausible-looking but wrong ref (a different file, a stale
        # occurrence id) is rejected rather than trusted at face value.
        return False, f"ref {ref!r} does not match verified ref {actual_ref!r}"
    return True, detail


# ---------------------------------------------------------------------------
# evaluate() / continue_step()
# ---------------------------------------------------------------------------

def _unmet_signature(unmet_detail: List[Tuple[str, str]]) -> str:
    """A stable signature of "which required criteria are failing, and how"
    — used to detect "no new evidence" across evaluations. Two evaluations
    with the exact same unmet set AND the exact same failure detail hash
    identically; anything that changed (a criterion newly passing, a
    different failure reason) hashes differently and resets the streak."""
    canon = sorted(unmet_detail)
    blob = json.dumps(canon, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


def evaluate(store: GoalStore, owner: str, goal_id: str, *, workspace: Optional[str] = None) -> GoalReport:
    """Runs every acceptance criterion for real (subprocess, filesystem,
    artifact store, HTTP, another document's revision) and decides the
    goal's next status FROM THAT EVIDENCE ALONE. This is the only function
    in the whole Goal domain allowed to set ``status = "done"``.

    Order of decisions, each one final for this call:
      1. Already terminal (done/ceiling_reached/abandoned)? Return a no-op
         report — "ceiling alcanzado -> ninguna evaluación más" is enforced
         HERE, not by convention.
      2. Run every criterion. Required ones failing = unmet.
      3. Floor met (no unmet required criteria)? -> ``done``. Evidence wins
         over ceiling/no-progress: a goal that just finished is done even if
         it finished exactly at its ceiling.
      4. Ceiling exceeded? -> ``ceiling_reached``, with the reason.
      5. Same unmet signature as last time? Streak += 1; at
         ``no_progress_limit`` -> ``blocked``. A different signature (some
         criterion started passing, or failing differently) resets the
         streak to 0 — that is progress, not a loop, the same rule
         ``src.loop_breaker.LoopPolicy`` applies one level down for repeated
         tool calls.
      6. Otherwise -> ``progressing``.
    """
    goal = store.get(owner, goal_id)
    if goal is None:
        raise GoalNotFound(goal_id)

    if goal.status in _TERMINAL_STATES:
        return GoalReport(
            goal_id=goal.id, status=goal.status, previous_status=goal.status, floor_met=(goal.status == "done"),
            checked=[], met=[], unmet=[], new_evidence=[], no_progress=False,
            no_progress_streak=goal.no_progress_streak, ceiling_hit=None,
            stop_reason=goal.stop_reason or f"goal is already {goal.status}", evaluated_at=time.time(),
        )

    now = time.time()
    checked: List[Dict[str, Any]] = []
    new_evidence: List[EvidenceEntry] = []
    met: List[str] = []
    unmet: List[str] = []
    unmet_detail: List[Tuple[str, str]] = []
    required = set(goal.required_criterion_ids())

    for c in goal.acceptance:
        ok, ref, detail = _run_criterion(c, goal, workspace)
        entry = EvidenceEntry(
            criterion_id=c.id, kind=c.kind, ref=ref, verified_at=now, by="evaluate", ok=ok, detail=detail,
        )
        new_evidence.append(entry)
        checked.append({"criterion_id": c.id, "kind": c.kind, "ok": ok, "ref": ref, "detail": detail,
                         "required": c.id in required})
        if ok:
            met.append(c.id)
        elif c.id in required:
            unmet.append(c.id)
            unmet_detail.append((c.id, detail))

    floor_met = not unmet
    ceiling_hit = goal.ceiling.exceeded_by(goal.usage)

    if floor_met:
        new_status = "done"
        stop_reason = ""
        no_progress = False
        streak = 0
        signature = None
    elif ceiling_hit:
        new_status = "ceiling_reached"
        stop_reason = ceiling_hit
        no_progress = False
        streak = goal.no_progress_streak
        signature = goal.last_unmet_signature
    else:
        signature = _unmet_signature(unmet_detail)
        if signature == goal.last_unmet_signature:
            streak = goal.no_progress_streak + 1
        else:
            streak = 0
        if streak >= goal.no_progress_limit:
            new_status = "blocked"
            stop_reason = (
                f"no new evidence across {streak} consecutive evaluations "
                f"(no_progress_limit={goal.no_progress_limit}); unmet: {', '.join(unmet)}"
            )
            no_progress = True
        else:
            new_status = "progressing" if goal.evidence or checked else "open"
            stop_reason = ""
            no_progress = False

    updated = store._apply_evaluation(
        owner, goal_id, new_status=new_status, no_progress_streak=streak,
        unmet_signature=signature, stop_reason=stop_reason, new_evidence=new_evidence,
    )

    return GoalReport(
        goal_id=goal.id, status=updated.status, previous_status=goal.status, floor_met=floor_met,
        checked=checked, met=met, unmet=unmet, new_evidence=[e.to_dict() for e in new_evidence],
        no_progress=no_progress, no_progress_streak=updated.no_progress_streak, ceiling_hit=ceiling_hit,
        stop_reason=updated.stop_reason, evaluated_at=now,
    )


def continue_step(goal: Goal) -> Dict[str, Any]:
    """The next concrete step, or a reason to stop — WITHOUT running any
    checker itself (that only happens in :func:`evaluate`), so calling this
    twice in a row with no intervening :func:`evaluate` call is perfectly
    idempotent: same goal state in, same answer out, no repeated work and
    no repeated escalation.
    """
    if goal.status == "done":
        return {"action": "complete", "goal_id": goal.id, "reason": "floor met with real evidence"}
    if goal.status == "ceiling_reached":
        return {"action": "stop", "goal_id": goal.id, "reason": goal.stop_reason or "ceiling reached"}
    if goal.status == "abandoned":
        return {"action": "stop", "goal_id": goal.id, "reason": goal.stop_reason or "abandoned"}
    if goal.status == "blocked":
        return {
            "action": "no_progress", "goal_id": goal.id,
            "reason": goal.stop_reason or "no progress across recent evaluations",
            "no_progress_streak": goal.no_progress_streak,
        }
    required = set(goal.required_criterion_ids())
    latest_by_criterion: Dict[str, EvidenceEntry] = {}
    for e in goal.evidence:
        latest_by_criterion[e.criterion_id] = e  # append-only in seq order -> last write wins
    next_unmet = None
    for c in goal.acceptance:
        if c.id not in required:
            continue
        entry = latest_by_criterion.get(c.id)
        if entry is None or not entry.ok:
            next_unmet = c
            break
    if next_unmet is None:
        # Nothing has been evaluated yet, or every required criterion's
        # latest evidence already passes but a fresh evaluate() has not run
        # to confirm/close it out.
        return {"action": "evaluate", "goal_id": goal.id, "reason": "call goal_evaluate to confirm current state"}
    return {
        "action": "work", "goal_id": goal.id, "next_criterion": next_unmet.to_dict(),
        "reason": "required criterion not yet met",
    }
