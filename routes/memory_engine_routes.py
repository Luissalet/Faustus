"""Learned-memory API — /api/memory-engine/* (FAUSTUS).

The Brain page's "Learned rules" section talks to these six endpoints. They
are the human end of the loop the agent runs on its own: read what was
learned, add a rule by hand (which lands with the highest trust class,
``human_explicit``), vote a rule up or down, delete one, run the Curator, and
see the EXACT block the model would be given for a query.

Admin-only, like the rest of the brain: the store holds standing instructions
the agent will follow, so writing to it is an authority change.

The two reads (``/items`` and ``/pack``) also answer in robot mode
(``?robot=1`` / ``?format=toon``, src/robot_envelope.py) for a coordinating
model reading this machine's learned rules; a call without those query
parameters answers exactly as it always did.

``/items`` sends the LEAN projection there (src/robot_projection.py): one flat
row per item — the score fields, the feedback COUNTS and the text — without
the event arrays behind them. ``/pack`` is already the one block the model
would be given, so robot mode sends it as it stands.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from core.middleware import require_admin
from src import robot_envelope as robot
from src import robot_projection as lean
from src.auth_helpers import effective_user

logger = logging.getLogger(__name__)


class ItemCreate(BaseModel):
    text: str
    level: Optional[str] = None
    category: Optional[str] = None
    project: Optional[str] = None
    #: MEM-01/MEM-05: the same vocabulary `engine.add_item()` already
    #: validates (`TYPES`) — exposed here so a human-written item can say
    #: what kind of memory it is instead of always falling back to
    #: `add_item`'s default.
    type: Optional[str] = None
    #: Narrows `scope` below global/project (`engine._compute_scope`):
    #: passing a session_id scopes the item to that one conversation.
    session_id: Optional[str] = None
    #: Free-form provenance dict (`engine.add_item`'s own `provenance` kwarg,
    #: stored verbatim and returned as `scope`'s sibling column on read —
    #: `public_item()` already includes both, this is what lets a caller
    #: SET provenance on create rather than only ever reading the default).
    provenance: Optional[Dict[str, Any]] = None


class FeedbackBody(BaseModel):
    kind: str
    reason: Optional[str] = None


class CurateBody(BaseModel):
    project: Optional[str] = None


class ForgetBody(BaseModel):
    reason: Optional[str] = None


class CorrectBody(BaseModel):
    text: str
    reason: Optional[str] = None


class PinBody(BaseModel):
    pinned: bool = True


class SuppressBody(BaseModel):
    suppressed: bool = True


class FailureBody(BaseModel):
    """MEM-03: one occurrence of a failure."""
    signature: str
    summary: str
    project: Optional[str] = None
    severity: Optional[str] = "medium"
    evidence_ref: Optional[str] = None
    session_id: Optional[str] = None
    test_ref: Optional[str] = None
    confirm: Optional[bool] = False


class DecisionBody(BaseModel):
    """MEM-04: one technical decision."""
    text: str
    project: Optional[str] = None
    alternatives: Optional[List[str]] = None
    scope: Optional[str] = "project"
    artifact_refs: Optional[List[str]] = None
    session_id: Optional[str] = None


class InvalidateDecisionBody(BaseModel):
    reason: str
    superseded_by: Optional[str] = None


class ResolveConflictBody(BaseModel):
    keep: str  # "new" | "old" | "both"


def _owner(request: Request) -> str:
    try:
        return str(effective_user(request) or "")
    except Exception:  # noqa: BLE001 - attribution must not 500 the route
        return ""


def setup_memory_engine_routes() -> APIRouter:
    router = APIRouter(prefix="/api/memory-engine", tags=["memory-engine"])

    @router.get("/items")
    async def list_items(
        request: Request,
        project: Optional[str] = None,
        status: Optional[str] = None,
        level: Optional[str] = None,
        limit: int = 200,
        _admin: None = Depends(require_admin),
    ) -> Dict[str, Any]:
        """Items with their COMPUTED effective_score / harmful_ratio."""
        from src import memory_engine as engine

        def payload() -> Dict[str, Any]:
            owner = _owner(request)
            try:
                items = engine.list_items(owner=owner, project=project, status=status,
                                          level=level, limit=limit)
            except engine.MemoryEngineError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            now = engine._utcnow()
            return {
                "status": "success",
                "items": [engine.public_item(item, now) for item in items],
                "stats": engine.stats(owner, project),
                "levels": list(engine.LEVELS),
                "trust_classes": dict(engine.TRUST_CLASSES),
            }
        if robot.wants(request):
            return await robot.reply(request, lambda: lean.memory_items(payload()))
        return payload()

    @router.post("/items")
    async def create_item(request: Request, body: ItemCreate,
                          _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        """A human wrote this down, so it lands as ``human_explicit`` (0.85) —
        the highest trust class the store has."""
        from src import memory_engine as engine
        try:
            item = engine.add_item(
                body.text,
                owner=_owner(request),
                project=str(body.project or ""),
                level=body.level or "procedural",
                category=body.category or "",
                trust_class="human_explicit",
                evidence=[{"kind": "chat", "excerpt": "added by the owner in the Brain page"}],
                type=body.type,
                session_id=str(body.session_id or ""),
                provenance=body.provenance,
            )
        except engine.MemoryEngineError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        # Job A: a human write must be visible in the NEXT session, not
        # hidden behind whatever block another live session already froze.
        engine.invalidate_all_snapshots()
        return {"status": "success", "item": engine.public_item(item)}

    @router.post("/items/{item_id}/feedback")
    async def item_feedback(item_id: str, body: FeedbackBody,
                            _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        from src import memory_engine as engine
        try:
            item = engine.add_feedback(item_id, body.kind, reason=body.reason or "",
                                       ref=f"human:{item_id}")
        except engine.MemoryEngineError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if not item:
            raise HTTPException(status_code=404, detail="no such memory item")
        engine.invalidate_all_snapshots()
        return {"status": "success", "item": engine.public_item(item)}

    @router.delete("/items/{item_id}")
    async def remove_item(item_id: str,
                          _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        from src import memory_engine as engine
        if not engine.delete_item(item_id):
            raise HTTPException(status_code=404, detail="no such memory item")
        engine.invalidate_all_snapshots()
        return {"status": "success", "deleted": True, "id": item_id}

    @router.delete("/items/{item_id}/forget")
    async def forget_item(item_id: str, body: Optional[ForgetBody] = None,
                          _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        """MEM-02: like DELETE /items/{id}, but leaves a tombstone so the same
        text cannot silently resurrect through a later reindex/import/
        consolidation (`engine.forget`) — the human-facing "no, and don't
        bring this back" action, distinct from the plain delete above."""
        from src import memory_engine as engine
        reason = body.reason if body else None
        tombstone = engine.forget(item_id, reason=reason or "", ref=f"human:{item_id}")
        if tombstone is None:
            raise HTTPException(status_code=404, detail="no such memory item")
        engine.invalidate_all_snapshots()
        return {"status": "success", "forgotten": True, "id": item_id, "tombstone": tombstone}

    @router.post("/items/{item_id}/correct")
    async def correct_item(item_id: str, body: CorrectBody,
                           _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        """MEM-02: replace an item with corrected text — the original is
        tombstoned (`engine.correct`, same guarantee as `forget` above) and a
        fresh item is written in its place, linked back via `provenance.
        corrected_from`."""
        from src import memory_engine as engine
        # Guard BEFORE calling engine.correct(): that function tombstones
        # (deletes) the original unconditionally and only THEN calls
        # add_item(new_text, ...) — if new_text is blank, add_item raises
        # and the original is already gone. Reject empty text here so a bad
        # request never destroys the item it was trying to correct.
        # (src/memory_engine.py is not in this lote's vía libre; the deeper
        # fix — validating new_text before tombstoning — belongs there; see
        # the final report.)
        if not str(body.text or "").strip():
            raise HTTPException(status_code=400, detail="text must not be empty")
        try:
            item = engine.correct(item_id, body.text, reason=body.reason or "corrected")
        except engine.MemoryEngineError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if item is None:
            raise HTTPException(status_code=404, detail="no such memory item")
        engine.invalidate_all_snapshots()
        return {"status": "success", "item": engine.public_item(item)}

    @router.post("/items/{item_id}/pin")
    async def pin_item(item_id: str, body: PinBody,
                       _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        """Job C (owner review panel): pin/unpin — a pinned item is always in
        `pack_detail()` ahead of the rest of its section, regardless of
        score. Goes through `engine.set_pinned`, never a raw store write."""
        from src import memory_engine as engine
        item = engine.set_pinned(item_id, body.pinned)
        if item is None:
            raise HTTPException(status_code=404, detail="no such memory item")
        engine.invalidate_all_snapshots()
        return {"status": "success", "item": engine.public_item(item)}

    @router.post("/items/{item_id}/suppress")
    async def suppress_item(item_id: str, body: SuppressBody,
                            _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        """Job C: suppress/unsuppress — a suppressed item is excluded from
        every pack section and from search(), without being deleted or
        tombstoned. Use DELETE .../forget instead to never bring it back."""
        from src import memory_engine as engine
        item = engine.set_suppressed(item_id, body.suppressed)
        if item is None:
            raise HTTPException(status_code=404, detail="no such memory item")
        engine.invalidate_all_snapshots()
        return {"status": "success", "item": engine.public_item(item)}

    @router.post("/curate")
    async def run_curator(request: Request, body: Optional[CurateBody] = None,
                          _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        """Layer 2, on demand. Deterministic — no model is called."""
        from src import memory_engine as engine
        from src import memory_curator
        project = (body.project if body else None)
        report = memory_curator.safe_curate(owner=_owner(request), project=project)
        return {"status": "success", "report": report,
                "stats": engine.stats(_owner(request), project)}

    @router.get("/pack")
    async def preview_pack(
        request: Request,
        project: Optional[str] = None,
        query: str = "",
        session: str = "",
        _admin: None = Depends(require_admin),
    ) -> Dict[str, Any]:
        """The exact block the model would see — nothing regenerated, the same
        function the prompt builder calls.

        With `session`, this is the very block that conversation is using:
        `pack_for_session()` returns the frozen snapshot when the per-session
        snapshot is on, so the preview shows what the model actually has in
        front of it rather than a freshly assembled block. Either way the
        answer carries the snapshot flag and timestamp, and what the hard
        character cap had to drop (`dropped_count`/`dropped_ids`/`cap_chars`/
        `truncated_item`), so a reader can tell a complete block from a
        trimmed one.
        """
        from src import memory_engine as engine

        def payload() -> Dict[str, Any]:
            empty = {"text": "", "ids": [], "degraded": False,
                     "dropped_ids": [], "dropped_count": 0,
                     "cap_chars": engine.block_max_chars(),
                     "truncated_item": False,
                     "snapshot": False, "snapshot_taken_at": None}
            try:
                if str(session or "").strip():
                    detail = engine.pack_for_session(
                        session, _owner(request), project, query,
                        engine.injection_budget())
                else:
                    detail = engine.pack_detail(_owner(request), project, query,
                                                engine.injection_budget())
            except Exception as exc:  # noqa: BLE001 - mirrors pack()'s own posture
                logger.debug("memory engine: pack preview failed: %s", exc)
                detail = dict(empty)
            return {
                "status": "success",
                "pack": detail.get("text") or "",
                "ids": detail.get("ids") or [],
                "degraded": bool(detail.get("degraded")),
                "chars": len(detail.get("text") or ""),
                "budget": engine.injection_budget(),
                "enabled": engine.injection_enabled(),
                "session": str(session or "").strip(),
                "snapshot": bool(detail.get("snapshot")),
                "snapshot_taken_at": detail.get("snapshot_taken_at"),
                "dropped_count": int(detail.get("dropped_count") or 0),
                "dropped_ids": detail.get("dropped_ids") or [],
                "cap_chars": int(detail.get("cap_chars") or engine.block_max_chars()),
                "truncated_item": bool(detail.get("truncated_item")),
            }
        if robot.wants(request):
            return await robot.reply(request, payload)
        return payload()

    # ── MEM-03: failure memory, controlled promotion ────────────────────────

    @router.post("/failures")
    async def register_failure(request: Request, body: FailureBody,
                               _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        """One occurrence of a failure. Promotes to an active anti-pattern
        ONLY when both a regression-test reference is attached and the
        incident has repeated (or an explicit `confirm` is passed) — see
        `src.memory_failures.register_failure`."""
        from src import memory_failures

        result = memory_failures.register_failure(
            body.signature, body.summary, owner=_owner(request),
            project=str(body.project or ""), severity=body.severity or "medium",
            evidence_ref=str(body.evidence_ref or ""),
            session_id=str(body.session_id or ""),
            test_ref=str(body.test_ref or ""), confirm=bool(body.confirm))
        return {"status": "success", **result}

    @router.get("/failures")
    async def list_failure_candidates(request: Request, project: Optional[str] = None,
                                      _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        """The "problema recurrente -> regla propuesta -> pruebas -> activacion"
        panel's data: every candidate still short of promotion."""
        from src import memory_failures

        candidates = memory_failures.list_candidates(owner=_owner(request),
                                                      project=str(project or ""))
        return {"status": "success", "candidates": candidates}

    @router.post("/failures/sweep")
    async def sweep_expired_failures(request: Request, project: Optional[str] = None,
                                     _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        """Demote (never delete) a promoted anti-pattern whose caducidad
        window passed without reconfirmation."""
        from src import memory_failures

        result = memory_failures.sweep_expired(owner=_owner(request),
                                                project=str(project or "") or None)
        return {"status": "success", **result}

    # ── MEM-04: project continuity and decisions ────────────────────────────

    @router.post("/decisions")
    async def create_decision(request: Request, body: DecisionBody,
                              _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        from src import memory_decisions

        item = memory_decisions.record_decision(
            body.text, owner=_owner(request), project=str(body.project or ""),
            alternatives=body.alternatives or [], scope=body.scope or "project",
            artifact_refs=body.artifact_refs or [], session_id=str(body.session_id or ""))
        return {"status": "success", "decision": item}

    @router.get("/decisions")
    async def list_decisions(request: Request, project: Optional[str] = None,
                             include_invalidated: bool = False,
                             _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        """The decision timeline for a project — current truth by default,
        `include_invalidated=true` for the full history a "linked to
        artifacts and changes" panel needs."""
        from src import memory_decisions

        rows = memory_decisions.list_decisions(owner=_owner(request),
                                                project=str(project or ""),
                                                include_invalidated=include_invalidated)
        return {"status": "success", "decisions": rows}

    @router.post("/decisions/{decision_id}/invalidate")
    async def invalidate_decision(decision_id: str, body: InvalidateDecisionBody,
                                  _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        """Its premise changed. The row stays (rule 3) — see
        `src.memory_decisions.invalidate_decision`."""
        from src import memory_decisions

        item = memory_decisions.invalidate_decision(
            decision_id, body.reason, superseded_by=str(body.superseded_by or ""))
        if item is None:
            raise HTTPException(status_code=404, detail="no such decision")
        return {"status": "success", "decision": item}

    # ── contradiction tracking (memory_conflicts.py) ────────────────────────

    @router.get("/conflicts")
    async def list_conflicts(request: Request, status: Optional[str] = "open",
                             limit: int = 200,
                             _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        """Open (or, with `status`, any-status) contradictions for this
        owner, both items' current text included so the panel never has to
        make a second call per row."""
        from src import memory_conflicts, memory_engine as engine

        owner = _owner(request)
        rows = memory_conflicts.list_conflicts(owner=owner, status=status, limit=limit)
        out = []
        for row in rows:
            entry = dict(row)
            new_item = engine.get_item(row.get("new_id"))
            old_item = engine.get_item(row.get("old_id"))
            entry["new_text"] = (new_item or {}).get("text", "")
            entry["old_text"] = (old_item or {}).get("text", "")
            entry["new_updated_at"] = (new_item or {}).get("updated_at", "")
            entry["old_updated_at"] = (old_item or {}).get("updated_at", "")
            out.append(entry)
        return {"status": "success", "conflicts": out}

    @router.post("/conflicts/{conflict_id}/resolve")
    async def resolve_conflict(conflict_id: str, body: ResolveConflictBody,
                               request: Request,
                               _admin: None = Depends(require_admin)) -> Dict[str, Any]:
        from src import memory_conflicts, memory_engine as engine

        try:
            result = memory_conflicts.resolve(conflict_id, body.keep,
                                              owner=_owner(request))
        except memory_conflicts.MemoryConflictError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if result is None:
            raise HTTPException(status_code=404, detail="no such open conflict")
        engine.invalidate_all_snapshots()
        return {"status": "success", "conflict": result}

    return router
