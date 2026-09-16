"""WP22 — storyboard documents and their typed ops.

Real DocumentStore (own sqlite, tmp_path), real op registry discovery
(pkgutil-based — `storyboard_ops.py` just has to exist under
`src/creator/ops/` to be picked up).
"""
from __future__ import annotations

import pytest

from src.creator import storyboard
from src.creator.errors import InvalidDocument, InvalidOperation
from src.creator.ops import registry as ops_registry
from src.creator.store import DocumentStore

OWNER = "alice"
PROJECT = "proj1"


def _store(tmp_path) -> DocumentStore:
    return DocumentStore(str(tmp_path / "docs.sqlite3"))


def _two_scenes():
    return [
        storyboard.new_scene("s1", "opening shot: the harbor at dawn",
                              start_ticks=0, duration_ticks=4000),
        storyboard.new_scene("s2", "cut to the letter on the table",
                              start_ticks=4000, duration_ticks=3000),
    ]


# ---------------------------------------------------------------------
# registry discovery
# ---------------------------------------------------------------------

def test_storyboard_ops_are_discovered_by_the_registry():
    known = ops_registry.known_op_types()
    for op_type in ("storyboard.add_scene", "storyboard.update_scene",
                     "storyboard.set_recipe", "storyboard.accept_scene",
                     "storyboard.propose_scene", "storyboard.set_references",
                     "storyboard.remove_scene", "storyboard.reorder_scenes"):
        assert op_type in known, f"{op_type} not registered: {known}"


# ---------------------------------------------------------------------
# creation / editing
# ---------------------------------------------------------------------

def test_create_storyboard_with_scenes(tmp_path):
    store = _store(tmp_path)
    doc = storyboard.create(store, OWNER, PROJECT, _two_scenes())
    assert doc.kind == "storyboard"
    assert doc.revision == 1
    scenes = storyboard.list_scenes(doc)
    assert [s["id"] for s in scenes] == ["s1", "s2"]
    assert all(s["status"] == "proposed" for s in scenes)


def test_add_scene_op_appends_and_bumps_revision(tmp_path):
    store = _store(tmp_path)
    doc = storyboard.create(store, OWNER, PROJECT, _two_scenes())
    result = store.apply_command(OWNER, doc.id, "cmd-1", doc.revision, {
        "type": "storyboard.add_scene", "id": "s3", "brief": "the reveal",
        "duration": {"start_ticks": "7000", "duration_ticks": "2000"},
    })
    assert result["applied"] is True
    new_doc = result["doc"]
    assert new_doc.revision == 2
    assert [s["id"] for s in storyboard.list_scenes(new_doc)] == ["s1", "s2", "s3"]


def test_update_scene_changes_only_given_fields(tmp_path):
    store = _store(tmp_path)
    doc = storyboard.create(store, OWNER, PROJECT, _two_scenes())
    result = store.apply_command(OWNER, doc.id, "cmd-1", doc.revision, {
        "type": "storyboard.update_scene", "object_id": "s1",
        "brief": "opening shot: the harbor at dawn, storm rolling in",
        "camera": {"shot": "wide", "movement": "static"},
    })
    scene = storyboard.scene_by_id(result["doc"], "s1")
    assert scene["brief"].endswith("storm rolling in")
    assert scene["camera"]["shot"] == "wide"
    # untouched fields survive
    assert scene["duration"]["start_ticks"] == "0"


def test_set_references_validates_shape(tmp_path):
    store = _store(tmp_path)
    doc = storyboard.create(store, OWNER, PROJECT, _two_scenes())
    with pytest.raises(InvalidOperation):
        store.apply_command(OWNER, doc.id, "cmd-1", doc.revision, {
            "type": "storyboard.set_references", "object_id": "s1",
            "reference_assets": ["ok", 5],
        })
    result = store.apply_command(OWNER, doc.id, "cmd-2", doc.revision, {
        "type": "storyboard.set_references", "object_id": "s1",
        "reference_assets": ["occ_ref1", "occ_ref2"],
    })
    scene = storyboard.scene_by_id(result["doc"], "s1")
    assert scene["reference_assets"] == ["occ_ref1", "occ_ref2"]


def test_remove_scene_keeps_at_least_one(tmp_path):
    store = _store(tmp_path)
    doc = storyboard.create(store, OWNER, PROJECT, [storyboard.new_scene(
        "only", "the only scene", start_ticks=0, duration_ticks=1000)])
    with pytest.raises(InvalidOperation):
        store.apply_command(OWNER, doc.id, "cmd-1", doc.revision, {
            "type": "storyboard.remove_scene", "object_id": "only",
        })


def test_reorder_scenes_requires_a_full_permutation(tmp_path):
    store = _store(tmp_path)
    doc = storyboard.create(store, OWNER, PROJECT, _two_scenes())
    with pytest.raises(InvalidOperation):
        store.apply_command(OWNER, doc.id, "cmd-1", doc.revision, {
            "type": "storyboard.reorder_scenes", "order": ["s1"],
        })
    result = store.apply_command(OWNER, doc.id, "cmd-2", doc.revision, {
        "type": "storyboard.reorder_scenes", "order": ["s2", "s1"],
    })
    assert [s["id"] for s in storyboard.list_scenes(result["doc"])] == ["s2", "s1"]


# ---------------------------------------------------------------------
# WP22 closure criteria
# ---------------------------------------------------------------------

def test_set_recipe_is_a_proposal_never_an_authorization(tmp_path):
    """"Especialistas producen propuestas no autorizaciones": attaching a
    recipe never flips status to accepted."""
    store = _store(tmp_path)
    doc = storyboard.create(store, OWNER, PROJECT, _two_scenes())
    result = store.apply_command(OWNER, doc.id, "cmd-1", doc.revision, {
        "type": "storyboard.set_recipe", "object_id": "s1",
        "adapter_id": "fake", "operation": "noop", "parameters": {"scenario": "success"},
    })
    scene = storyboard.scene_by_id(result["doc"], "s1")
    assert scene["recipe"]["adapter_id"] == "fake"
    assert scene["status"] == "proposed"   # NOT accepted


def test_accepted_scene_is_never_silently_rewritten(tmp_path):
    """"Canon aceptado no se reescribe automáticamente": update_scene,
    set_recipe and set_references all refuse on an accepted scene unless
    the caller explicitly opts in."""
    store = _store(tmp_path)
    doc = storyboard.create(store, OWNER, PROJECT, _two_scenes())
    accepted = store.apply_command(OWNER, doc.id, "cmd-1", doc.revision, {
        "type": "storyboard.accept_scene", "object_id": "s1",
    })["doc"]
    assert storyboard.scene_by_id(accepted, "s1")["status"] == "accepted"

    with pytest.raises(InvalidOperation):
        store.apply_command(OWNER, doc.id, "cmd-2", accepted.revision, {
            "type": "storyboard.update_scene", "object_id": "s1", "brief": "a silent rewrite",
        })
    with pytest.raises(InvalidOperation):
        store.apply_command(OWNER, doc.id, "cmd-3", accepted.revision, {
            "type": "storyboard.set_recipe", "object_id": "s1",
            "adapter_id": "fake", "operation": "noop", "parameters": {},
        })

    # A deliberate override still works.
    overridden = store.apply_command(OWNER, doc.id, "cmd-4", accepted.revision, {
        "type": "storyboard.update_scene", "object_id": "s1", "brief": "a deliberate redo",
        "allow_overwrite_accepted": True,
    })["doc"]
    assert storyboard.scene_by_id(overridden, "s1")["brief"] == "a deliberate redo"
    # It stays accepted — the override only changed content, not the lock.
    assert storyboard.scene_by_id(overridden, "s1")["status"] == "accepted"


def test_storyboard_without_any_recipe_is_still_fully_editable(tmp_path):
    """"Storyboard sin modelo de vídeo sigue siendo editable": a storyboard
    with zero recipes attached can still be created, edited and previewed
    (the animatic) with no adapter/model involved at all."""
    store = _store(tmp_path)
    doc = storyboard.create(store, OWNER, PROJECT, _two_scenes())
    assert all(s["recipe"] is None for s in storyboard.list_scenes(doc))

    edited = store.apply_command(OWNER, doc.id, "cmd-1", doc.revision, {
        "type": "storyboard.update_scene", "object_id": "s2", "brief": "revised beat",
    })["doc"]
    assert storyboard.scene_by_id(edited, "s2")["brief"] == "revised beat"

    preview = storyboard.animatic_preview(edited)
    assert [p["scene_id"] for p in preview] == ["s1", "s2"]
    assert all(p["has_recipe"] is False for p in preview)


def test_animatic_preview_is_ordered_by_start_ticks(tmp_path):
    store = _store(tmp_path)
    scenes = [
        storyboard.new_scene("late", "later scene", start_ticks=5000, duration_ticks=1000),
        storyboard.new_scene("early", "earlier scene", start_ticks=0, duration_ticks=1000),
    ]
    doc = storyboard.create(store, OWNER, PROJECT, scenes)
    preview = storyboard.animatic_preview(doc)
    assert [p["scene_id"] for p in preview] == ["early", "late"]


# ---------------------------------------------------------------------
# per-scene / per-storyboard budget (WP09 reused)
# ---------------------------------------------------------------------

def test_scene_budget_without_recipe_reports_missing_recipe_not_a_crash(tmp_path):
    store = _store(tmp_path)
    doc = storyboard.create(store, OWNER, PROJECT, _two_scenes())
    scene = storyboard.scene_by_id(doc, "s1")
    report = storyboard.estimate_scene_budget(owner=OWNER, project_id=PROJECT, scene=scene)
    assert report.ok is False
    assert any(m["id"] == "recipe" for m in report.missing)


def test_scene_budget_with_recipe_uses_preflight(tmp_path, monkeypatch):
    from src.creator import preflight as pf

    monkeypatch.setattr(pf, "_capabilities_for",
                        lambda deployment_id: {"operations": {"noop": {}}})
    monkeypatch.setattr(pf, "_validate_params", lambda engine, task, params: {"ok": True})

    store = _store(tmp_path)
    doc = storyboard.create(store, OWNER, PROJECT, _two_scenes())
    result = store.apply_command(OWNER, doc.id, "cmd-1", doc.revision, {
        "type": "storyboard.set_recipe", "object_id": "s1",
        "adapter_id": "fake", "operation": "noop",
        "parameters": {"estimated_tokens": 100},
    })
    doc = result["doc"]
    scene = storyboard.scene_by_id(doc, "s1")
    report = storyboard.estimate_scene_budget(owner=OWNER, project_id=PROJECT, scene=scene)
    assert report.estimate.tokens == 100

    total = storyboard.estimate_storyboard_budget(owner=OWNER, project_id=PROJECT, doc=doc)
    # s1 has a token estimate, s2 does not -> total is honestly unknown.
    assert total["total_tokens"] is None
