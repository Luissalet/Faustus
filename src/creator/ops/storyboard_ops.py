"""Storyboard (scene/shot) operations — WP22, over WP02's ``storyboard``
document kind (``src/creator/documents.py::_validate_storyboard``) and
WP12's typed-op dispatcher (``src/creator/ops/registry.py``).

A storyboard's ``content["shots"]`` list is the plan of scenes a brief turns
into. Every op here is pure — ``(doc, op) -> new_content`` — exactly the
``canvas_ops.py`` shape; ``DocumentStore.apply_command`` is what turns a
successful call into a new revision.

Two closure criteria drive the shape of these ops (``WP22.md``):

* **"Canon aceptado no se reescribe automáticamente."** A scene's
  ``status`` field (additive on top of the WP02 schema, which only
  requires the base keys to be *present* — see
  ``documents.py::_validate_storyboard``) is ``proposed`` or ``accepted``.
  Once a scene is ``accepted``, every op that would change its editorial
  content (``update_scene``, ``set_recipe``, ``set_references``) refuses
  unless the caller explicitly passes ``allow_overwrite_accepted: true`` —
  a real person choosing to redo an accepted scene, never a background
  proposal quietly landing on top of one.
* **"Especialistas producen propuestas no autorizaciones."** ``set_recipe``
  — how a specialist (an image/video/audio recipe picker) attaches an
  adapter+recipe to a scene — NEVER promotes the scene's ``status`` to
  ``accepted`` by itself; at most it leaves an already-``proposed`` scene
  ``proposed``. Only ``accept_scene`` (a deliberate, separate op) accepts.
"""
from __future__ import annotations

import copy
from typing import Any, Dict, List

from ..errors import InvalidOperation
from .model import Rational, TimeRange, find_by_id, index_by_id, require_kind

SCENE_STATES = ("proposed", "accepted")


def _shots(content: Dict[str, Any]) -> List[Dict[str, Any]]:
    shots = content.get("shots")
    if not isinstance(shots, list):
        raise InvalidOperation("storyboard.content.shots must be an array")
    return shots


def _guard_accepted(scene: Dict[str, Any], op: Dict[str, Any]) -> None:
    if scene.get("status") == "accepted" and not bool(op.get("allow_overwrite_accepted", False)):
        raise InvalidOperation(
            f"scene {scene.get('id')!r} is accepted; pass "
            "'allow_overwrite_accepted': true to deliberately redo it "
            "(WP22: accepted canon is never rewritten automatically)"
        )


def _clock(op: Dict[str, Any]) -> Dict[str, str]:
    raw = op.get("clock")
    if raw is None:
        # A sane default: 1 tick == 1 millisecond. A caller with a real
        # frame rate passes its own clock explicitly.
        return Rational(1000, 1).to_dict()
    return Rational.from_dict(raw, field_name="scene.clock").to_dict()


def _duration(op: Dict[str, Any]) -> Dict[str, str]:
    raw = op.get("duration")
    if not isinstance(raw, dict):
        raise InvalidOperation("scene 'duration' must be an object with start_ticks/duration_ticks")
    rng = TimeRange.from_dict(raw, field_name="scene.duration")
    return rng.to_dict()


def add_scene(doc: Dict[str, Any], op: Dict[str, Any]) -> Dict[str, Any]:
    require_kind(doc, "storyboard")
    content = copy.deepcopy(doc["content"])
    shots = _shots(content)

    scene_id = op.get("id")
    if not isinstance(scene_id, str) or not scene_id:
        raise InvalidOperation("storyboard.add_scene requires a non-empty string 'id'")
    if any(s.get("id") == scene_id for s in shots):
        raise InvalidOperation(f"scene id already exists: {scene_id!r}")

    brief = op.get("brief")
    if not isinstance(brief, str) or not brief:
        raise InvalidOperation("storyboard.add_scene requires a non-empty string 'brief'")

    reference_assets = op.get("reference_assets", [])
    if not isinstance(reference_assets, list) or not all(isinstance(r, str) for r in reference_assets):
        raise InvalidOperation("storyboard.add_scene 'reference_assets' must be a list of strings")

    scene: Dict[str, Any] = {
        "id": scene_id,
        "brief": brief,
        "duration": _duration(op),
        "clock": _clock(op),
        "reference_assets": list(reference_assets),
        "selected_take": op.get("selected_take"),
        "status": "proposed",
        "camera": op.get("camera"),
        "voice": op.get("voice"),
        "sources": list(op.get("sources") or []),
        "constraints": dict(op.get("constraints") or {}),
        "recipe": None,
    }

    index = op.get("index", len(shots))
    if not isinstance(index, int) or isinstance(index, bool) or not (0 <= index <= len(shots)):
        raise InvalidOperation("storyboard.add_scene 'index' must be an integer in [0, len(shots)]")
    shots.insert(index, scene)
    return content


def update_scene(doc: Dict[str, Any], op: Dict[str, Any]) -> Dict[str, Any]:
    """Edits a scene's brief/duration/camera/voice/sources/constraints —
    "cambios por selección sin rehacer todo" (WP22 item 3): every field is
    optional in the op, and every field left out keeps its current value.
    Never touches ``status``, ``recipe`` or ``reference_assets`` — those
    have their own ops."""
    require_kind(doc, "storyboard")
    content = copy.deepcopy(doc["content"])
    shots = _shots(content)
    scene = find_by_id(shots, op.get("object_id") or op.get("id"), what="scene")
    _guard_accepted(scene, op)

    if "brief" in op:
        brief = op["brief"]
        if not isinstance(brief, str) or not brief:
            raise InvalidOperation("storyboard.update_scene 'brief' must be a non-empty string")
        scene["brief"] = brief
    if "duration" in op:
        scene["duration"] = _duration(op)
    if "clock" in op:
        scene["clock"] = Rational.from_dict(op["clock"], field_name="scene.clock").to_dict()
    if "camera" in op:
        scene["camera"] = op["camera"]
    if "voice" in op:
        scene["voice"] = op["voice"]
    if "sources" in op:
        sources = op["sources"]
        if not isinstance(sources, list):
            raise InvalidOperation("storyboard.update_scene 'sources' must be a list")
        scene["sources"] = list(sources)
    if "constraints" in op:
        constraints = op["constraints"]
        if not isinstance(constraints, dict):
            raise InvalidOperation("storyboard.update_scene 'constraints' must be an object")
        scene["constraints"] = dict(constraints)
    if "selected_take" in op:
        take = op["selected_take"]
        if take is not None and (not isinstance(take, str) or not take):
            raise InvalidOperation("storyboard.update_scene 'selected_take' must be a non-empty string or null")
        scene["selected_take"] = take
    return content


def set_references(doc: Dict[str, Any], op: Dict[str, Any]) -> Dict[str, Any]:
    require_kind(doc, "storyboard")
    content = copy.deepcopy(doc["content"])
    shots = _shots(content)
    scene = find_by_id(shots, op.get("object_id") or op.get("id"), what="scene")
    _guard_accepted(scene, op)
    refs = op.get("reference_assets")
    if not isinstance(refs, list) or not all(isinstance(r, str) and r for r in refs):
        raise InvalidOperation("storyboard.set_references requires 'reference_assets' as a list of non-empty strings")
    scene["reference_assets"] = list(refs)
    return content


def set_recipe(doc: Dict[str, Any], op: Dict[str, Any]) -> Dict[str, Any]:
    """A specialist's PROPOSAL for how to render a scene — an adapter id, an
    operation/recipe name and its parameters. Never authorises anything by
    itself: it never sets ``status`` to ``accepted`` (WP22: "especialistas
    producen propuestas no autorizaciones"). If the scene is currently
    ``accepted`` this refuses like every other content-changing op unless
    ``allow_overwrite_accepted`` is explicit — a specialist re-proposing over
    accepted canon must be a deliberate act, not a silent background one."""
    require_kind(doc, "storyboard")
    content = copy.deepcopy(doc["content"])
    shots = _shots(content)
    scene = find_by_id(shots, op.get("object_id") or op.get("id"), what="scene")
    _guard_accepted(scene, op)

    adapter_id = op.get("adapter_id")
    if not isinstance(adapter_id, str) or not adapter_id:
        raise InvalidOperation("storyboard.set_recipe requires a non-empty string 'adapter_id'")
    operation = op.get("operation")
    if not isinstance(operation, str) or not operation:
        raise InvalidOperation("storyboard.set_recipe requires a non-empty string 'operation'")
    parameters = op.get("parameters", {})
    if not isinstance(parameters, dict):
        raise InvalidOperation("storyboard.set_recipe 'parameters' must be an object")

    scene["recipe"] = {
        "adapter_id": adapter_id, "operation": operation,
        "parameters": dict(parameters),
        "proposed_by": str(op.get("proposed_by") or "specialist"),
    }
    # A proposal never demotes an accepted scene back to proposed either —
    # only an explicit `propose_scene`/`accept_scene` changes `status`.
    return content


def propose_scene(doc: Dict[str, Any], op: Dict[str, Any]) -> Dict[str, Any]:
    require_kind(doc, "storyboard")
    content = copy.deepcopy(doc["content"])
    shots = _shots(content)
    scene = find_by_id(shots, op.get("object_id") or op.get("id"), what="scene")
    scene["status"] = "proposed"
    return content


def accept_scene(doc: Dict[str, Any], op: Dict[str, Any]) -> Dict[str, Any]:
    """The one deliberate, human-shaped op that locks a scene's canon."""
    require_kind(doc, "storyboard")
    content = copy.deepcopy(doc["content"])
    shots = _shots(content)
    scene = find_by_id(shots, op.get("object_id") or op.get("id"), what="scene")
    scene["status"] = "accepted"
    return content


def remove_scene(doc: Dict[str, Any], op: Dict[str, Any]) -> Dict[str, Any]:
    require_kind(doc, "storyboard")
    content = copy.deepcopy(doc["content"])
    shots = _shots(content)
    scene = find_by_id(shots, op.get("object_id") or op.get("id"), what="scene")
    _guard_accepted(scene, op)
    if len(shots) <= 1:
        raise InvalidOperation("storyboard.content.shots must stay non-empty; cannot remove the last scene")
    idx = index_by_id(shots, scene["id"], what="scene")
    del shots[idx]
    return content


def reorder_scenes(doc: Dict[str, Any], op: Dict[str, Any]) -> Dict[str, Any]:
    require_kind(doc, "storyboard")
    content = copy.deepcopy(doc["content"])
    shots = _shots(content)
    order = op.get("order")
    if not isinstance(order, list) or not all(isinstance(x, str) for x in order):
        raise InvalidOperation("storyboard.reorder_scenes requires 'order' as a list of scene ids")
    current_ids = {s.get("id") for s in shots}
    if set(order) != current_ids or len(order) != len(shots):
        raise InvalidOperation("storyboard.reorder_scenes 'order' must be a permutation of the current scene ids")
    by_id = {s["id"]: s for s in shots}
    content["shots"] = [by_id[i] for i in order]
    return content


OPS = {
    "storyboard.add_scene": add_scene,
    "storyboard.update_scene": update_scene,
    "storyboard.set_references": set_references,
    "storyboard.set_recipe": set_recipe,
    "storyboard.propose_scene": propose_scene,
    "storyboard.accept_scene": accept_scene,
    "storyboard.remove_scene": remove_scene,
    "storyboard.reorder_scenes": reorder_scenes,
}
