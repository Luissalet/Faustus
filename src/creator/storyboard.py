"""storyboard.py — WP22: a brief becomes an editable plan of scenes.

Sits on top of two authorities that already exist and are never
reimplemented here:

* ``src.creator.store.DocumentStore`` / ``src.creator.documents`` (WP02) —
  the ``storyboard`` document kind, its revisioned CAS storage and its
  structural validation (``documents._validate_storyboard``).
* ``src.creator.ops.storyboard_ops`` (WP22, alongside this module) — the
  typed, declarative ops (``storyboard.add_scene`` and friends) that
  ``DocumentStore.apply_command`` dispatches through the WP12 registry.

This module is the ergonomic layer a route or a specialist calls instead of
hand-building op dicts: scene construction helpers, an animatic preview that
needs no video model (WP22 closure criterion: "storyboard sin modelo de
vídeo sigue siendo editable"), and per-scene/per-storyboard budget
estimates that reuse WP09's ``preflight.run_preflight`` verbatim — never a
second cost model.
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence

from .documents import CreatorDocument
from .errors import InvalidDocument
from .ops.model import Rational, TimeRange
from .store import DocumentStore

__all__ = [
    "new_scene", "new_storyboard_content", "create", "get",
    "list_scenes", "scene_by_id", "animatic_preview",
    "estimate_scene_budget", "estimate_storyboard_budget",
]


# ── construction ────────────────────────────────────────────────────────

def new_scene(
    scene_id: str, brief: str, *, start_ticks: int, duration_ticks: int,
    clock_numerator: int = 1000, clock_denominator: int = 1,
    reference_assets: Sequence[str] = (), selected_take: Optional[str] = None,
    camera: Any = None, voice: Any = None, sources: Sequence[Any] = (),
    constraints: Optional[Mapping[str, Any]] = None,
    recipe: Optional[Mapping[str, Any]] = None, status: str = "proposed",
) -> Dict[str, Any]:
    """Build one scene dict matching what ``storyboard_ops`` produces — a
    duration objetiva as a rational (start_ticks/duration_ticks at an
    explicit ticks-per-second rate, never a bare float second count),
    references as occurrence ids, and an optional specialist recipe."""
    if status not in ("proposed", "accepted"):
        raise InvalidDocument(f"scene status must be 'proposed' or 'accepted', got {status!r}")
    clock = Rational(clock_numerator, clock_denominator)
    duration = TimeRange(start_ticks, duration_ticks)
    return {
        "id": scene_id,
        "brief": brief,
        "duration": duration.to_dict(),
        "clock": clock.to_dict(),
        "reference_assets": list(reference_assets),
        "selected_take": selected_take,
        "status": status,
        "camera": camera,
        "voice": voice,
        "sources": list(sources),
        "constraints": dict(constraints or {}),
        "recipe": dict(recipe) if recipe else None,
    }


def new_storyboard_content(scenes: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    if not scenes:
        raise InvalidDocument("a storyboard needs at least one scene")
    return {"shots": [dict(s) for s in scenes]}


# ── store access ────────────────────────────────────────────────────────

def create(store: DocumentStore, owner: str, project_id: str,
           scenes: Sequence[Mapping[str, Any]]) -> CreatorDocument:
    content = new_storyboard_content(scenes)
    return store.create(owner, project_id, "storyboard", content)


def get(store: DocumentStore, owner: str, doc_id: str) -> Optional[CreatorDocument]:
    doc = store.get(owner, doc_id)
    if doc is None or doc.kind != "storyboard":
        return None
    return doc


def list_scenes(doc: CreatorDocument) -> List[Dict[str, Any]]:
    return list(doc.content.get("shots") or [])


def scene_by_id(doc: CreatorDocument, scene_id: str) -> Optional[Dict[str, Any]]:
    for scene in list_scenes(doc):
        if scene.get("id") == scene_id:
            return scene
    return None


# ── animatic preview (no video model needed) ──────────────────────────

def animatic_preview(doc: CreatorDocument) -> List[Dict[str, Any]]:
    """An ordered, timeline-shaped preview of the storyboard: one entry per
    scene, in ``duration.start_ticks`` order, carrying only what already
    exists (a reference still or an already-selected take) — never a
    generated frame. This is what makes "editable without a video model"
    true: a storyboard with zero recipes attached still previews cleanly."""
    scenes = list_scenes(doc)

    def _start(scene: Dict[str, Any]) -> int:
        try:
            return int(scene.get("duration", {}).get("start_ticks", "0"))
        except (TypeError, ValueError):
            return 0

    ordered = sorted(scenes, key=_start)
    preview: List[Dict[str, Any]] = []
    for scene in ordered:
        duration = scene.get("duration") or {}
        preview.append({
            "scene_id": scene.get("id"),
            "status": scene.get("status", "proposed"),
            "start_ticks": duration.get("start_ticks"),
            "duration_ticks": duration.get("duration_ticks"),
            "clock": scene.get("clock"),
            "preview_asset": scene.get("selected_take")
                or (scene.get("reference_assets") or [None])[0],
            "has_recipe": scene.get("recipe") is not None,
            "brief": scene.get("brief"),
        })
    return preview


# ── budget per scene / storyboard (WP09 reused, never re-derived) ──────

def estimate_scene_budget(*, owner: str, project_id: str,
                           scene: Mapping[str, Any],
                           profile: Optional[Mapping[str, Any]] = None) -> Any:
    """One scene's ``preflight.run_preflight`` report, built from its
    attached ``recipe`` proposal (if any). Pure, no side effect — the same
    guarantee WP09's own module carries. A scene with no recipe yet still
    returns a well-formed report (``ok=False``, one 'missing: param recipe'
    entry) rather than raising, because a storyboard with un-recipe'd scenes
    is a normal, editable state, not an error."""
    from .preflight import Estimate, Admission, BudgetOutlook, PreflightReport
    import time as _time

    recipe = scene.get("recipe")
    if not recipe:
        return PreflightReport(
            ok=False,
            missing=({"kind": "param", "id": "recipe",
                      "detail": "no adapter/recipe has been proposed for this scene yet"},),
            estimate=Estimate(notes=("no recipe: cost is not yet estimable",)),
            admission=Admission(fits=True, reason="no recipe; admission not gated"),
            requires_approval=False, approval_digest=None, approval_plan=None,
            budget=BudgetOutlook(reservation_needed=None, ceiling=None, remaining=None,
                                  verdict="allow", reason="no recipe yet"),
            operation="", engine="", deployment_id="", project_id=project_id,
            created_at=_time.time(),
        )

    from .preflight import run_preflight

    return run_preflight(
        owner=owner, project_id=project_id, operation=str(recipe.get("operation") or ""),
        engine=str(recipe.get("adapter_id") or ""), deployment_id=str(recipe.get("adapter_id") or ""),
        params=dict(recipe.get("parameters") or {}),
        inputs=list(scene.get("reference_assets") or []), profile=profile,
    )


def estimate_storyboard_budget(*, owner: str, project_id: str, doc: CreatorDocument,
                                profile: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Aggregates :func:`estimate_scene_budget` over every scene. The total
    cost is ``"unknown"`` the instant ANY scene's cost is unknown — never a
    partial sum that silently drops what it could not price, matching
    ``preflight.Estimate``'s own "unknown, never a guessed zero" discipline."""
    per_scene = []
    total_tokens: Optional[int] = 0
    total_cost: Any = 0.0
    any_unknown_cost = False
    any_unknown_tokens = False
    ok = True
    for scene in list_scenes(doc):
        report = estimate_scene_budget(owner=owner, project_id=project_id, scene=scene, profile=profile)
        per_scene.append({"scene_id": scene.get("id"), "preflight": report.to_dict()})
        ok = ok and report.ok
        tokens = report.estimate.tokens
        if tokens is None:
            any_unknown_tokens = True
        elif total_tokens is not None:
            total_tokens += tokens
        cost = report.estimate.cost_usd
        if cost == "unknown" or cost is None:
            any_unknown_cost = True
        elif not any_unknown_cost:
            total_cost += float(cost)
    return {
        "ok": ok,
        "scenes": per_scene,
        "total_tokens": None if any_unknown_tokens else total_tokens,
        "total_cost_usd": "unknown" if any_unknown_cost else round(total_cost, 6),
    }
