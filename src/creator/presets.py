"""presets.py — WP41: presets, evaluation and discovery, on top of
``src/creator/params.py``'s (engine, task) contract.

A preset is a named, validated set of parameters for one (engine, task)
pair, plus two SEPARATE quality tracks that ADR-10 ("Calidad técnica y
calidad artística separadas") requires never be collapsed into one number:

* ``technical`` — objective, measured facts about a render this preset
  produced (resolution, duration, whether ``ffprobe``/the engine reported
  success, elapsed time, VRAM used). Appended by :func:`evaluate` /
  :meth:`PresetStore.record_evaluation`.
* ``artistic`` — human 1-5 ratings with a comment. Appended by
  :meth:`PresetStore.rate`. Never averaged into ``technical`` or vice versa;
  the two lists are read and reported independently, always.

Nothing in this module invents a second params authority: ``params.py``'s
``validate()`` is the ONLY gate a preset's stored parameters pass through,
so a preset can never carry a field outside that (engine, task) schema —
this is what "un preset nunca borra política superior" means in practice:
a preset is a named point INSIDE the contract, never a way to smuggle an
extra knob (budget, approval, model choice) the schema does not expose.

Promotion of a user/discovered preset to ``STATUS_RECOMMENDED`` never
happens inside this module's own store — it goes through
``src.harness_evolution`` (propose -> validate -> evaluate on held-out ->
promote, with a rollback target), the SAME candidate/evaluation/promotion
machinery every other harness change uses, so a preset promotion is
reviewable and revertible exactly like any other harness change. Calling
:func:`evaluate` never promotes anything by itself — it only produces an
:class:`EvaluationRecord` a caller stores as evidence.

New persistence (CONTRATO.md rule 2): ``DATA_DIR/creator/presets.db``, one
connection opened/closed per call, WAL, optimistic concurrency via a real
``BEGIN IMMEDIATE`` transaction and a ``command_id`` dedupe table — the same
pattern ``src/creator/store.py`` (WP02) and ``src/harness_evolution/store.py``
already use.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from src.contracts.base import now_iso

from . import params as params_mod
from .errors import CreatorError

_BUSY_TIMEOUT_S = 30

# ── vocabulary ───────────────────────────────────────────────────────────

ORIGIN_FACTORY = "factory"
ORIGIN_USER = "user"
ORIGIN_DISCOVERED = "discovered"
ORIGINS = frozenset({ORIGIN_FACTORY, ORIGIN_USER, ORIGIN_DISCOVERED})

#: `unreviewed` — created (by hand or discovered); usable by its own owner,
#: never surfaced as a cross-owner recommendation.
STATUS_UNREVIEWED = "unreviewed"
#: `recommended` — promoted through `harness_evolution`, held-out evaluation
#: passed. This is the ONLY status `promote_preset` produces.
STATUS_RECOMMENDED = "recommended"
#: `rejected` — a promotion attempt's held-out evaluation failed. The
#: preset itself is still the owner's own, still usable exactly as before
#: (no permission or policy changes) — only "promoted to recommended" is
#: refused; the failed attempt is kept as evidence, never silently dropped.
STATUS_REJECTED = "rejected"
#: `retired` — a previously `recommended` preset rolled back.
STATUS_RETIRED = "retired"
STATUSES = frozenset({STATUS_UNREVIEWED, STATUS_RECOMMENDED, STATUS_REJECTED, STATUS_RETIRED})

RATING_MIN = 1
RATING_MAX = 5

#: Evidence kinds this module records to `Preset.evidence`. Freeform beyond
#: this: a caller may add its own `kind` values (e.g. `"run"` from
#: `discover_from_runs`) without breaking anything that reads this list.
EVIDENCE_EVALUATION = "evaluation"
EVIDENCE_PROMOTION_PROPOSED = "promotion_proposed"
EVIDENCE_PROMOTION_REJECTED = "promotion_rejected"
EVIDENCE_PROMOTED = "promoted"
EVIDENCE_ROLLED_BACK = "rolled_back"
EVIDENCE_RUN = "run"


# ── errors ───────────────────────────────────────────────────────────────

class PresetNotFound(CreatorError):
    """No preset with that id, or it belongs to someone else — deliberately
    the SAME exception (and the same 404) for both, CONTRATO.md rule 3."""


class PresetRevisionConflict(CreatorError):
    """A mutating call was made with a stale ``expected_revision``."""

    def __init__(self, current_revision: int, message: str = "") -> None:
        self.current_revision = int(current_revision)
        super().__init__(message or (
            f"expected_revision is stale; current revision is {self.current_revision}"))


class InvalidPreset(CreatorError):
    """The preset's own fields (engine/task/params/rating) are malformed."""


class PromotionRejected(CreatorError):
    """A promotion attempt's held-out evaluation failed (A27's gate, reused
    verbatim). Carries the harness patch id and the evaluation record that
    was still saved as evidence — a rejection is never silent."""

    def __init__(self, patch_id: str, evaluation: Mapping[str, Any], message: str = "") -> None:
        self.patch_id = patch_id
        self.evaluation = dict(evaluation)
        super().__init__(message or f"held-out evaluation failed for candidate {patch_id!r}")


# ── data shapes ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class EvaluationRecord:
    """The result of :func:`evaluate` — pure evidence, never a promotion
    decision on its own (ADR-10 / this ficha's closing criterion: "evaluar
    no promociona"). ``technical`` carries ONLY objective, measured facts;
    it never contains a human rating."""

    preset_id: str
    fixture_run_id: str
    technical: Dict[str, Any]
    passed: bool
    notes: str
    evaluated_at: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "preset_id": self.preset_id, "fixture_run_id": self.fixture_run_id,
            "technical": dict(self.technical), "passed": self.passed,
            "notes": self.notes, "evaluated_at": self.evaluated_at,
        }


@dataclass(frozen=True)
class Preset:
    id: str
    owner: str
    engine: str
    task: str
    params: Dict[str, Any]
    deployment_ids: Tuple[str, ...] = ()
    origin: str = ORIGIN_USER
    status: str = STATUS_UNREVIEWED
    previous_status: str = ""
    technical: Tuple[Dict[str, Any], ...] = ()
    artistic: Tuple[Dict[str, Any], ...] = ()
    evidence: Tuple[Dict[str, Any], ...] = ()
    harness_patch_id: str = ""
    revision: int = 1
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "owner": self.owner, "engine": self.engine, "task": self.task,
            "params": dict(self.params), "deployment_ids": list(self.deployment_ids),
            "origin": self.origin, "status": self.status,
            "quality": {"technical": [dict(t) for t in self.technical],
                        "artistic": [dict(a) for a in self.artistic]},
            "evidence": [dict(e) for e in self.evidence],
            "harness_patch_id": self.harness_patch_id, "revision": self.revision,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }


def _params_fingerprint(engine: str, task: str, params: Mapping[str, Any]) -> str:
    payload = json.dumps({"engine": engine, "task": task, "params": params},
                          sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ── store ────────────────────────────────────────────────────────────────

def default_db_path() -> str:
    from src.constants import DATA_DIR
    return os.path.join(DATA_DIR, "creator", "presets.db")


def _new_id() -> str:
    import uuid
    return f"preset_{uuid.uuid4().hex[:20]}"


def _row_to_preset(row: sqlite3.Row) -> Preset:
    return Preset(
        id=row["id"], owner=row["owner"], engine=row["engine"], task=row["task"],
        params=json.loads(row["params_json"]),
        deployment_ids=tuple(json.loads(row["deployment_ids_json"])),
        origin=row["origin"], status=row["status"], previous_status=row["previous_status"] or "",
        technical=tuple(json.loads(row["technical_json"])),
        artistic=tuple(json.loads(row["artistic_json"])),
        evidence=tuple(json.loads(row["evidence_json"])),
        harness_patch_id=row["harness_patch_id"] or "",
        revision=int(row["revision"]), created_at=row["created_at"], updated_at=row["updated_at"],
    )


class PresetStore:
    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = db_path or default_db_path()
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
                CREATE TABLE IF NOT EXISTS creator_presets (
                    id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    engine TEXT NOT NULL,
                    task TEXT NOT NULL,
                    params_json TEXT NOT NULL,
                    deployment_ids_json TEXT NOT NULL,
                    origin TEXT NOT NULL,
                    status TEXT NOT NULL,
                    previous_status TEXT,
                    technical_json TEXT NOT NULL,
                    artistic_json TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    harness_patch_id TEXT,
                    fingerprint TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_creator_presets_owner_engine_task "
                "ON creator_presets(owner, engine, task)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_creator_presets_fingerprint "
                "ON creator_presets(owner, fingerprint)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS creator_preset_commands (
                    preset_id TEXT NOT NULL,
                    command_id TEXT NOT NULL,
                    result_revision INTEGER NOT NULL,
                    at REAL NOT NULL,
                    PRIMARY KEY (preset_id, command_id)
                )
                """
            )

    # ── reads ────────────────────────────────────────────────────────────

    def get(self, owner: str, preset_id: str) -> Optional[Preset]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM creator_presets WHERE id = ? AND owner = ?",
                (preset_id, owner or ""),
            ).fetchone()
            return _row_to_preset(row) if row else None

    def list_for_owner(self, owner: str, *, engine: str = "", task: str = "") -> List[Preset]:
        query = "SELECT * FROM creator_presets WHERE owner = ?"
        args: List[Any] = [owner or ""]
        if engine:
            query += " AND engine = ?"
            args.append(engine)
        if task:
            query += " AND task = ?"
            args.append(task)
        query += " ORDER BY updated_at DESC"
        with self._conn() as conn:
            rows = conn.execute(query, tuple(args)).fetchall()
            return [_row_to_preset(r) for r in rows]

    def find_by_fingerprint(self, owner: str, engine: str, task: str,
                            params: Mapping[str, Any]) -> Optional[Preset]:
        fp = _params_fingerprint(engine, task, params)
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM creator_presets WHERE owner = ? AND fingerprint = ? "
                "ORDER BY created_at ASC LIMIT 1",
                (owner or "", fp),
            ).fetchone()
            return _row_to_preset(row) if row else None

    # ── create ───────────────────────────────────────────────────────────

    def create(self, owner: str, engine: str, task: str, params: Mapping[str, Any], *,
              deployment_ids: Sequence[str] = (), origin: str = ORIGIN_USER) -> Preset:
        """Validate ``params`` against ``params.get_schema(engine, task)`` —
        the ONLY params authority — and store exactly the NORMALIZED result.
        A field outside the schema can never end up on a preset (WP41's
        closing criterion: "un preset nunca borra política superior")."""
        if origin not in ORIGINS:
            raise InvalidPreset(f"unknown origin: {origin!r}")
        result = params_mod.validate(engine, task, params)
        if not result.ok:
            raise InvalidPreset(
                f"params do not validate for engine={engine!r} task={task!r}: "
                f"{[e.to_dict() for e in result.errors]}")
        preset_id = _new_id()
        stamp = time.time()
        fp = _params_fingerprint(engine, task, result.normalized)
        with self._conn(immediate=True) as conn:
            conn.execute(
                "INSERT INTO creator_presets (id, owner, engine, task, params_json, "
                "deployment_ids_json, origin, status, previous_status, technical_json, "
                "artistic_json, evidence_json, harness_patch_id, fingerprint, revision, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (preset_id, owner or "", engine, task, json.dumps(result.normalized),
                 json.dumps(list(deployment_ids)), origin, STATUS_UNREVIEWED, "",
                 json.dumps([]), json.dumps([]), json.dumps([]), "", fp, 1, stamp, stamp),
            )
        return Preset(id=preset_id, owner=owner or "", engine=engine, task=task,
                      params=dict(result.normalized), deployment_ids=tuple(deployment_ids),
                      origin=origin, status=STATUS_UNREVIEWED, revision=1,
                      created_at=stamp, updated_at=stamp)

    # ── generic CAS mutation, shared by rate / record_evaluation / status ──

    def _apply(self, owner: str, preset_id: str, command_id: str,
              expected_revision: int,
              mutator: Callable[[Preset], Preset]) -> Tuple[Preset, bool]:
        """Returns ``(preset, deduped)``. Raises :class:`PresetNotFound`,
        :class:`PresetRevisionConflict`. ``mutator`` receives the current
        preset and returns the ONLY the fields that changed applied onto a
        fresh dataclass — callers below build that with ``dataclasses.
        replace``-style helpers."""
        if not command_id:
            raise InvalidPreset("command_id is required")
        with self._conn(immediate=True) as conn:
            row = conn.execute(
                "SELECT * FROM creator_presets WHERE id = ? AND owner = ?",
                (preset_id, owner or ""),
            ).fetchone()
            if row is None:
                raise PresetNotFound(preset_id)
            current = _row_to_preset(row)

            existing_cmd = conn.execute(
                "SELECT result_revision FROM creator_preset_commands "
                "WHERE preset_id = ? AND command_id = ?",
                (preset_id, command_id),
            ).fetchone()
            if existing_cmd is not None:
                # Replay: the row already reflects that command's effect
                # (or a later one) — return current state, never re-apply.
                return current, True

            if int(expected_revision) != current.revision:
                raise PresetRevisionConflict(current.revision)

            updated = mutator(current)
            new_revision = current.revision + 1
            stamp = time.time()
            conn.execute(
                "UPDATE creator_presets SET params_json=?, deployment_ids_json=?, "
                "origin=?, status=?, previous_status=?, technical_json=?, artistic_json=?, "
                "evidence_json=?, harness_patch_id=?, revision=?, updated_at=? "
                "WHERE id=? AND owner=?",
                (json.dumps(updated.params), json.dumps(list(updated.deployment_ids)),
                 updated.origin, updated.status, updated.previous_status,
                 json.dumps(list(updated.technical)), json.dumps(list(updated.artistic)),
                 json.dumps(list(updated.evidence)), updated.harness_patch_id,
                 new_revision, stamp, preset_id, owner or ""),
            )
            conn.execute(
                "INSERT INTO creator_preset_commands (preset_id, command_id, "
                "result_revision, at) VALUES (?,?,?,?)",
                (preset_id, command_id, new_revision, stamp),
            )
            final = Preset(
                id=current.id, owner=current.owner, engine=current.engine, task=current.task,
                params=updated.params, deployment_ids=updated.deployment_ids,
                origin=updated.origin, status=updated.status,
                previous_status=updated.previous_status, technical=updated.technical,
                artistic=updated.artistic, evidence=updated.evidence,
                harness_patch_id=updated.harness_patch_id, revision=new_revision,
                created_at=current.created_at, updated_at=stamp,
            )
            return final, False

    # ── artistic rating ─────────────────────────────────────────────────

    def rate(self, owner: str, preset_id: str, *, rating: int, comment: str = "",
             rated_by: str = "", command_id: str, expected_revision: int) -> Preset:
        """Append a human 1-5 rating with an optional comment. Never touches
        ``technical`` — ADR-10's separation, enforced structurally: this is
        the only method that writes to ``artistic``."""
        rating = int(rating)
        if rating < RATING_MIN or rating > RATING_MAX:
            raise InvalidPreset(f"rating must be between {RATING_MIN} and {RATING_MAX}")

        def _mutate(current: Preset) -> Preset:
            entry = {"rating": rating, "comment": str(comment or ""),
                     "rated_by": rated_by or owner, "rated_at": now_iso()}
            new_artistic = tuple(current.artistic) + (entry,)
            return Preset(**{**current.to_kwargs(), "artistic": new_artistic})

        preset, _deduped = self._apply(owner, preset_id, command_id, expected_revision, _mutate)
        return preset

    # ── technical evaluation evidence ───────────────────────────────────

    def record_evaluation(self, owner: str, preset_id: str, record: EvaluationRecord, *,
                          command_id: str, expected_revision: int) -> Preset:
        """Append an :class:`EvaluationRecord` to ``technical`` AND to
        ``evidence`` — this is evidence, not a promotion (see module
        docstring); it never changes ``status``."""

        def _mutate(current: Preset) -> Preset:
            new_technical = tuple(current.technical) + (record.to_dict(),)
            ev = {"kind": EVIDENCE_EVALUATION, "id": record.fixture_run_id,
                  "at": record.evaluated_at, "passed": record.passed}
            new_evidence = tuple(current.evidence) + (ev,)
            return Preset(**{**current.to_kwargs(), "technical": new_technical,
                            "evidence": new_evidence})

        preset, _deduped = self._apply(owner, preset_id, command_id, expected_revision, _mutate)
        return preset

    # ── status / promotion bookkeeping (harness_evolution is the gate) ──

    def _set_status(self, owner: str, preset_id: str, *, status: str,
                    previous_status: str = "", harness_patch_id: str = "",
                    evidence_entry: Optional[Mapping[str, Any]] = None,
                    command_id: str, expected_revision: int) -> Preset:
        if status not in STATUSES:
            raise InvalidPreset(f"unknown status: {status!r}")

        def _mutate(current: Preset) -> Preset:
            new_evidence = tuple(current.evidence)
            if evidence_entry is not None:
                new_evidence = new_evidence + (dict(evidence_entry),)
            return Preset(**{**current.to_kwargs(), "status": status,
                            "previous_status": previous_status or current.previous_status,
                            "harness_patch_id": harness_patch_id or current.harness_patch_id,
                            "evidence": new_evidence})

        preset, _deduped = self._apply(owner, preset_id, command_id, expected_revision, _mutate)
        return preset


def _preset_to_kwargs(self: Preset) -> Dict[str, Any]:
    return {
        "id": self.id, "owner": self.owner, "engine": self.engine, "task": self.task,
        "params": self.params, "deployment_ids": self.deployment_ids, "origin": self.origin,
        "status": self.status, "previous_status": self.previous_status,
        "technical": self.technical, "artistic": self.artistic, "evidence": self.evidence,
        "harness_patch_id": self.harness_patch_id, "revision": self.revision,
        "created_at": self.created_at, "updated_at": self.updated_at,
    }


# Attached rather than defined in the class body so the dataclass stays a
# plain, ordinary dataclass (matching every other Creator module's style)
# while `_apply`'s mutators can still clone-with-overrides tersely.
Preset.to_kwargs = _preset_to_kwargs  # type: ignore[attr-defined]


_default_store_lock = threading.Lock()
_default_store: Optional[PresetStore] = None


def get_store() -> PresetStore:
    global _default_store
    with _default_store_lock:
        if _default_store is None:
            _default_store = PresetStore()
        return _default_store


def reset_default_store_for_tests() -> None:
    global _default_store
    with _default_store_lock:
        _default_store = None


# ── evaluate(preset, fixture_run) -> EvaluationRecord ───────────────────

def evaluate(preset: Preset, fixture_run: Mapping[str, Any]) -> EvaluationRecord:
    """Turn one fixture/held-out run's OBJECTIVE facts into an
    :class:`EvaluationRecord`. Never touches a store, never promotes
    anything — a caller persists it with
    :meth:`PresetStore.record_evaluation` if it wants it kept as evidence.

    ``fixture_run`` is whatever the caller already measured about a real (or
    harness-fixture) render, e.g.::

        {"run_id": "mrun_1", "status": "completed",
         "metrics": {"resolution": "1024x1024", "ffprobe_ok": True},
         "elapsed_s": 12.4, "vram_mb": 6100, "notes": "wp36 fixture #3"}

    ``passed`` is True only when the run reports ``status == "completed"``
    AND at least one objective metric was actually recorded — an empty
    metrics dict is never silently counted as a pass (this module never
    asserts an engine succeeded from a mock; a caller that has no real
    metrics should not call this with a fabricated ``status``)."""
    status = str(fixture_run.get("status") or "")
    metrics: Dict[str, Any] = dict(fixture_run.get("metrics") or {})
    for key in ("elapsed_s", "vram_mb", "resolution", "duration_s", "ffprobe_ok"):
        if key in fixture_run and key not in metrics:
            metrics[key] = fixture_run[key]
    passed = status == "completed" and bool(metrics)
    return EvaluationRecord(
        preset_id=preset.id, fixture_run_id=str(fixture_run.get("run_id") or ""),
        technical=metrics, passed=passed, notes=str(fixture_run.get("notes") or ""),
        evaluated_at=now_iso(),
    )


# ── discovery: propose preset candidates from real, well-rated runs ──────

def discover_from_runs(owner: str, project_id: str, *,
                       run_ratings: Mapping[str, int],
                       min_rating: int = 4,
                       store: Optional[PresetStore] = None,
                       runs: Optional[Sequence[Mapping[str, Any]]] = None) -> List[Preset]:
    """Propose preset candidates from THIS owner's own completed runs that
    already have a good rating attached (``run_ratings``, run_id -> 1-5 —
    this module does not invent a second rating authority for runs; a
    caller supplies whatever already rated them, e.g. a Studio review
    step). A run this owner has no rating for is never proposed — "buena
    valoración" is required evidence, not assumed from mere completion.

    Reads only ``src.media_runs.recent(owner=owner)`` (never another
    owner's runs — CONTRATO.md rule 3) filtered to this ``project_id`` and
    ``status == "completed"``. ``runs`` lets a test (or a future caller with
    its own run listing) skip that import.

    Every proposal is inserted with ``origin=ORIGIN_DISCOVERED`` and
    ``status=STATUS_UNREVIEWED`` — discovery only ever produces a candidate
    for a human (or a later ``promote_preset`` call) to review; it never
    marks anything ``recommended`` itself. A run whose (engine, task,
    normalized params) already matches an existing preset for this owner is
    skipped (deduped by fingerprint), so re-running discovery is idempotent
    rather than spamming near-duplicates."""
    store = store or get_store()
    if runs is None:
        from src import media_runs
        runs = media_runs.recent(owner=owner, limit=200)

    proposed: List[Preset] = []
    seen_fingerprints: set = set()
    for run in runs:
        run_owner = str(run.get("owner") or "")
        if run_owner != (owner or ""):
            continue  # never propose from another owner's run
        if str(run.get("project_id") or "") != project_id:
            continue
        if str(run.get("status") or "") != "completed":
            continue
        run_id = str(run.get("id") or "")
        rating = int(run_ratings.get(run_id, 0) or 0)
        if rating < min_rating:
            continue

        engine = str(run.get("engine") or "").strip()
        values = dict(run.get("values") or {})
        task = str(values.pop("task", "") or "").strip()
        if not task:
            # `workflow` is written as "<adapter>:<op>" by
            # src/creator/production_runs.py (WP10) for non-ComfyUI
            # adapters; the part after ':' is the closest thing to a task
            # label this run carries when the caller did not store one
            # explicitly under values['task'].
            workflow = str(run.get("workflow") or "")
            task = workflow.split(":", 1)[1] if ":" in workflow else workflow
        if not engine or not task:
            continue

        schema = params_mod.get_schema(engine, task)
        if schema is None:
            continue  # no contract to validate against yet; not this WP's job to invent one
        result = params_mod.validate(engine, task, values)
        if not result.ok:
            continue  # the run's own recorded values do not fit today's schema; skip, don't guess

        fp = _params_fingerprint(engine, task, result.normalized)
        if fp in seen_fingerprints:
            continue
        seen_fingerprints.add(fp)
        if store.find_by_fingerprint(owner, engine, task, result.normalized) is not None:
            continue

        preset = store.create(owner, engine, task, result.normalized, origin=ORIGIN_DISCOVERED)
        preset = store.record_evaluation(
            owner, preset.id,
            EvaluationRecord(preset_id=preset.id, fixture_run_id=run_id,
                             technical={"source_run_rating": rating}, passed=True,
                             notes="discovered from a completed, well-rated run",
                             evaluated_at=now_iso()),
            command_id=f"discover:{run_id}", expected_revision=preset.revision,
        )
        proposed.append(preset)
    return proposed


# ── promotion: user preset -> recommended, via harness_evolution ─────────

TaskRunner = Callable[[str], bool]

#: Deterministic, dependency-free checks used when a caller supplies no
#: ``runner``/task ids of its own AND `tests.creator_harness` (WP36) is not
#: importable. These do NOT execute any engine and never claim to — they
#: check facts already on the preset (its params still validate; it already
#: carries at least one PASSED technical evaluation). Documented explicitly
#: so nobody mistakes this for "the engine was actually run".
_DEFAULT_SOURCE_TASKS: Tuple[str, ...] = ("params_schema_valid",)
_DEFAULT_HELD_OUT_TASKS: Tuple[str, ...] = ("has_passed_technical_evaluation",)


def _default_runner(preset: Preset) -> TaskRunner:
    def _run(task_id: str) -> bool:
        if task_id == "params_schema_valid":
            return params_mod.validate(preset.engine, preset.task, preset.params).ok
        if task_id == "has_passed_technical_evaluation":
            return any(bool(t.get("passed")) for t in preset.technical)
        return False
    return _run


def _wp36_held_out_tasks(engine: str, task: str) -> Optional[Tuple[str, ...]]:
    """The ficha: "held-out: los fixtures de WP36 si existen, si no una
    lista de tareas de evaluación". WP36 (`tests/creator_harness/`) may not
    be merged yet — imported lazily and never required."""
    try:
        from tests.creator_harness import fixtures as wp36_fixtures  # type: ignore
    except ImportError:
        return None
    getter = getattr(wp36_fixtures, "task_ids_for", None)
    if not callable(getter):
        return None
    try:
        ids = getter(engine, task)
    except Exception:  # noqa: BLE001 - WP36 fixtures are not this WP's authority
        return None
    return tuple(str(i) for i in ids) if ids else None


def promote_preset(owner: str, preset_id: str, *, actor: str,
                   runner: Optional[TaskRunner] = None,
                   source_task_ids: Optional[Sequence[str]] = None,
                   held_out_task_ids: Optional[Sequence[str]] = None,
                   store: Optional[PresetStore] = None,
                   harness_service: Optional[Any] = None) -> Preset:
    """Promote a ``user``/``discovered`` preset to ``STATUS_RECOMMENDED``,
    ONLY through ``src.harness_evolution``: propose a candidate patch that
    updates this preset's own harness "specialist" ref, evaluate it on
    source + held-out tasks, and promote it only if BOTH pass (A27's gate,
    reused, never re-implemented). A held-out failure raises
    :class:`PromotionRejected` — the preset's ``status`` is left exactly as
    it was, but the rejection IS recorded as evidence on the preset (this
    WP's closing criterion: "promoción... con fallos... con evidencia
    guardada", never silently dropped)."""
    from src import harness_evolution as he
    from src.harness_evolution.store import StaleParent

    store = store or get_store()
    preset = store.get(owner, preset_id)
    if preset is None:
        raise PresetNotFound(preset_id)

    service = harness_service or he.HarnessEvolutionService()
    ref = f"creator_preset:{preset.engine}:{preset.task}:{preset.id}"
    _ensure_specialist_ref(service.store, ref, json.dumps(preset.params, sort_keys=True))

    patch = service.propose_candidate(
        changes={"type": "update_specialist", "ref": ref,
                "content": json.dumps(preset.params, sort_keys=True)},
        source_trace_ids=[e.get("id", "") for e in preset.evidence if e.get("id")],
        scope=f"creator_preset:{preset.engine}:{preset.task}",
    )
    if patch.status != "validated":
        # e.g. the ref was raced away between _ensure and propose — surface
        # this as a rejection with the validator's own reason as evidence,
        # never a silent no-op.
        store._set_status(
            owner, preset.id, status=preset.status, previous_status=preset.previous_status,
            evidence_entry={"kind": EVIDENCE_PROMOTION_REJECTED, "id": patch.patch_id,
                            "at": now_iso(), "reason": patch.trace},
            command_id=f"promote-validate-fail:{patch.patch_id}",
            expected_revision=preset.revision,
        )
        raise PromotionRejected(patch.patch_id, {}, patch.trace)

    the_runner = runner or _default_runner(preset)
    wp36_ids = _wp36_held_out_tasks(preset.engine, preset.task)
    sources = tuple(source_task_ids) if source_task_ids else _DEFAULT_SOURCE_TASKS
    held_out = tuple(held_out_task_ids) if held_out_task_ids else (
        wp36_ids or _DEFAULT_HELD_OUT_TASKS)

    patch = service.evaluate_candidate(
        patch.patch_id, runner=the_runner, source_task_ids=sources, held_out_task_ids=held_out)

    preset = store._set_status(
        owner, preset.id, status=preset.status, previous_status=preset.previous_status,
        harness_patch_id=patch.patch_id,
        evidence_entry={"kind": EVIDENCE_PROMOTION_PROPOSED, "id": patch.patch_id,
                        "at": now_iso(), "evaluation": patch.evaluation},
        command_id=f"promote-evaluated:{patch.patch_id}", expected_revision=preset.revision,
    )

    if patch.status != "evaluated":
        preset = store._set_status(
            owner, preset.id, status=STATUS_REJECTED, previous_status=preset.status,
            evidence_entry={"kind": EVIDENCE_PROMOTION_REJECTED, "id": patch.patch_id,
                            "at": now_iso(), "evaluation": patch.evaluation},
            command_id=f"promote-rejected:{patch.patch_id}", expected_revision=preset.revision,
        )
        raise PromotionRejected(patch.patch_id, patch.evaluation)

    try:
        service.promote_candidate(patch.patch_id, actor=actor)
    except StaleParent:
        # Someone else's promotion landed first for the same ref; the
        # candidate itself is still sound evidence, but THIS attempt did
        # not land — surface it plainly rather than pretending success.
        raise PromotionRejected(patch.patch_id, patch.evaluation,
                                "harness revision changed concurrently; retry")

    preset = store._set_status(
        owner, preset.id, status=STATUS_RECOMMENDED, previous_status=preset.status,
        harness_patch_id=patch.patch_id,
        evidence_entry={"kind": EVIDENCE_PROMOTED, "id": patch.patch_id, "at": now_iso()},
        command_id=f"promote-final:{patch.patch_id}", expected_revision=preset.revision,
    )
    return preset


def rollback_preset(owner: str, preset_id: str, *, actor: str, reason: str,
                    store: Optional[PresetStore] = None,
                    harness_service: Optional[Any] = None) -> Preset:
    """Undo a promotion: rolls back the harness revision it created
    (``harness_evolution.rollback_candidate``, the same manual-rollback path
    every other harness change uses) and restores the preset's own
    ``status`` to whatever it was immediately before that promotion."""
    from src import harness_evolution as he

    store = store or get_store()
    preset = store.get(owner, preset_id)
    if preset is None:
        raise PresetNotFound(preset_id)
    if preset.status != STATUS_RECOMMENDED or not preset.harness_patch_id:
        raise InvalidPreset(f"preset {preset_id!r} is not a promoted (recommended) preset")

    service = harness_service or he.HarnessEvolutionService()
    service.rollback_candidate(preset.harness_patch_id, actor=actor, reason=reason)

    restored_status = preset.previous_status or STATUS_UNREVIEWED
    return store._set_status(
        owner, preset.id, status=STATUS_RETIRED, previous_status=restored_status,
        evidence_entry={"kind": EVIDENCE_ROLLED_BACK, "id": preset.harness_patch_id,
                        "at": now_iso(), "reason": reason, "actor": actor},
        command_id=f"rollback:{preset.harness_patch_id}:{now_iso()}",
        expected_revision=preset.revision,
    )


def _ensure_specialist_ref(store: Any, ref: str, initial_content: str) -> None:
    """First-use registration of this preset's harness "specialist" ref.
    ``harness_evolution``'s ``update_specialist`` change type (by design,
    see ``src/harness_evolution/validator.py::_validate_ref_update``) only
    accepts a patch against a ref that ALREADY exists on the parent
    revision — there is no ``add_specialist`` change type. Registering a
    brand-new preset's ref is therefore a direct, idempotent revision bump
    through the store's own public ``promote_cas`` (the exact method
    ``harness_evolution.promotion.promote`` itself uses), not a private
    write path — declaring that a specialist ref exists, with no content
    judged yet, is not the same action as approving a content change to it,
    and carries no evaluation claim of its own."""
    from src.harness_evolution.models import HarnessRevision, new_id
    from src.harness_evolution.store import StaleParent

    active = store.active_revision()
    specialists = dict((active.refs or {}).get("specialists") or {})
    if ref in specialists:
        return
    specialists[ref] = initial_content
    new_refs = dict(active.refs or {})
    new_refs["specialists"] = specialists
    candidate = HarnessRevision(revision_id=new_id("rev"), parent_id=active.revision_id,
                                created_at=now_iso(), refs=new_refs, status="active",
                                version=active.version + 1)
    try:
        store.promote_cas(parent_revision_id=active.revision_id, new_revision=candidate)
    except StaleParent:
        # Someone else registered a ref (this one or another) concurrently;
        # re-check whether OURS is now present before giving up.
        current = store.active_revision()
        if ref in ((current.refs or {}).get("specialists") or {}):
            return
        raise


__all__ = [
    "ORIGIN_FACTORY", "ORIGIN_USER", "ORIGIN_DISCOVERED", "ORIGINS",
    "STATUS_UNREVIEWED", "STATUS_RECOMMENDED", "STATUS_REJECTED", "STATUS_RETIRED", "STATUSES",
    "RATING_MIN", "RATING_MAX",
    "PresetNotFound", "PresetRevisionConflict", "InvalidPreset", "PromotionRejected",
    "EvaluationRecord", "Preset", "PresetStore",
    "default_db_path", "get_store", "reset_default_store_for_tests",
    "evaluate", "discover_from_runs", "promote_preset", "rollback_preset",
]
