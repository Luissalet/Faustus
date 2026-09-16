"""Canvas (image) operations — WP12 / IMG01.

Replaces "reinterpret the last saved blob" with typed, declarative edits
over an explicit ``object_id``: a layer id, or the sentinel ``"canvas"`` for
the document's base composition. Every op returns a brand new ``content``
dict (the caller — ``DocumentStore.apply_command``, via ``registry.apply``
— is responsible for turning that into a new revision; nothing here ever
mutates its input in place, so "undo" is just "go back to an earlier
revision's content", never a separate reverse-op to get right).

Layer order in ``content["layers"]`` IS the paint/compose order (index 0
first / bottom, per ``src/media_edit_projects.py``'s existing convention —
grepped, its ``add_layer`` appends). Reordering only ever permutes that
list; nothing here recomputes pixels — a preview/export renderer reads the
same list the same way every time, which is what "capas y transforms
producen el orden mostrado" (WP12 closure criterion) requires.
"""
from __future__ import annotations

import copy
from typing import Any, Dict, List

from ..errors import InvalidOperation
from .model import Region, find_by_id, index_by_id, require_kind

LAYER_KINDS = ("raster", "mask", "text", "control")
CANVAS_OBJECT_ID = "canvas"


def _layers(content: Dict[str, Any]) -> List[Dict[str, Any]]:
    layers = content.get("layers")
    if not isinstance(layers, list):
        raise InvalidOperation("canvas.content.layers must be an array")
    return layers


def _offset(d: Any, *, field_name: str = "offset") -> Dict[str, int]:
    if not isinstance(d, dict) or not isinstance(d.get("x"), int) or not isinstance(d.get("y"), int):
        raise InvalidOperation(f"{field_name} must be an object with integer x/y")
    return {"x": d["x"], "y": d["y"]}


def add_layer(doc: Dict[str, Any], op: Dict[str, Any]) -> Dict[str, Any]:
    require_kind(doc, "canvas")
    content = copy.deepcopy(doc["content"])
    layers = _layers(content)

    object_id = op.get("object_id")
    if not isinstance(object_id, str) or not object_id:
        raise InvalidOperation("canvas.add_layer requires a non-empty string 'object_id'")
    if object_id == CANVAS_OBJECT_ID:
        raise InvalidOperation(f"object_id {CANVAS_OBJECT_ID!r} is reserved for the base canvas")
    if any(l.get("id") == object_id for l in layers):
        raise InvalidOperation(f"layer object_id already exists: {object_id!r}")

    layer_kind = op.get("kind")
    if layer_kind not in LAYER_KINDS:
        raise InvalidOperation(f"canvas.add_layer 'kind' must be one of {LAYER_KINDS}")

    opacity = op.get("opacity", 1.0)
    if not isinstance(opacity, (int, float)) or isinstance(opacity, bool) or not (0 <= opacity <= 1):
        raise InvalidOperation("canvas.add_layer 'opacity' must be a number in [0, 1]")

    new_layer: Dict[str, Any] = {
        "id": object_id,
        "kind": layer_kind,
        "visible": bool(op.get("visible", True)),
        "opacity": float(opacity),
        "offset": _offset(op.get("offset", {"x": 0, "y": 0})),
    }
    asset_ref = op.get("asset_ref")
    if asset_ref is not None:
        if not isinstance(asset_ref, str) or not asset_ref:
            raise InvalidOperation("canvas.add_layer 'asset_ref' must be a non-empty string when given")
        new_layer["asset_ref"] = asset_ref

    index = op.get("index", len(layers))
    if not isinstance(index, int) or isinstance(index, bool) or not (0 <= index <= len(layers)):
        raise InvalidOperation("canvas.add_layer 'index' must be an integer in [0, len(layers)]")
    layers.insert(index, new_layer)
    return content


def move_layer(doc: Dict[str, Any], op: Dict[str, Any]) -> Dict[str, Any]:
    """Sets a layer's offset to an ABSOLUTE value — never a delta against
    "wherever it currently is", so replaying the same command twice (e.g.
    after a dedup miss) is a no-op on state, not a double move."""
    require_kind(doc, "canvas")
    content = copy.deepcopy(doc["content"])
    layers = _layers(content)
    layer = find_by_id(layers, op.get("object_id"), what="layer")
    layer["offset"] = _offset(op.get("offset"))
    return content


def set_mask(doc: Dict[str, Any], op: Dict[str, Any]) -> Dict[str, Any]:
    """Attaches (or clears, with ``mask_asset_ref: null``) a mask asset to
    a layer. Additive field (``mask_asset_ref``) — the reference plan schema
    doesn't carry it, but this repo's structural validator only checks
    required keys are present (see ``src/creator/documents.py``), so an
    extra field never breaks an existing document."""
    require_kind(doc, "canvas")
    content = copy.deepcopy(doc["content"])
    layers = _layers(content)
    layer = find_by_id(layers, op.get("object_id"), what="layer")
    mask_ref = op.get("mask_asset_ref")
    if mask_ref is not None and (not isinstance(mask_ref, str) or not mask_ref):
        raise InvalidOperation("canvas.set_mask 'mask_asset_ref' must be a non-empty string or null")
    if mask_ref is None:
        layer.pop("mask_asset_ref", None)
    else:
        layer["mask_asset_ref"] = mask_ref
    return content


def crop(doc: Dict[str, Any], op: Dict[str, Any]) -> Dict[str, Any]:
    """Crops the base canvas or a single layer to ``region`` (see
    ``model.Region`` — dimensions + EXIF orientation + layer transform, so
    the crop is reproducible, not just "the rectangle the UI drew")."""
    require_kind(doc, "canvas")
    content = copy.deepcopy(doc["content"])
    region = Region.from_dict(op.get("region"))
    object_id = op.get("object_id", CANVAS_OBJECT_ID)
    if object_id == CANVAS_OBJECT_ID:
        content["width"] = region.width
        content["height"] = region.height
        content["crop_region"] = region.to_dict()
    else:
        layers = _layers(content)
        layer = find_by_id(layers, object_id, what="layer")
        layer["crop_region"] = region.to_dict()
    return content


def rotate(doc: Dict[str, Any], op: Dict[str, Any]) -> Dict[str, Any]:
    """Sets the target's absolute rotation in degrees (normalized to
    [0, 360)) — declarative over object_id, never "rotate 90 more from
    wherever it is", which would make a replayed command apply twice."""
    require_kind(doc, "canvas")
    content = copy.deepcopy(doc["content"])
    degrees = op.get("degrees")
    if not isinstance(degrees, (int, float)) or isinstance(degrees, bool):
        raise InvalidOperation("canvas.rotate requires a numeric 'degrees'")
    normalized = float(degrees) % 360.0
    object_id = op.get("object_id", CANVAS_OBJECT_ID)
    if object_id == CANVAS_OBJECT_ID:
        content["rotation_degrees"] = normalized
        if normalized in (90.0, 270.0):
            content["width"], content["height"] = content["height"], content["width"]
    else:
        layers = _layers(content)
        layer = find_by_id(layers, object_id, what="layer")
        layer["rotation_degrees"] = normalized
    return content


def reorder(doc: Dict[str, Any], op: Dict[str, Any]) -> Dict[str, Any]:
    """Replaces the layer list's order with the given permutation of ids —
    the paint order IS this list order, so this is the whole operation;
    nothing else needs to change for a later render to show it."""
    require_kind(doc, "canvas")
    content = copy.deepcopy(doc["content"])
    layers = _layers(content)
    order = op.get("order")
    if not isinstance(order, list) or not all(isinstance(x, str) for x in order):
        raise InvalidOperation("canvas.reorder requires 'order' as a list of layer ids")
    current_ids = {l.get("id") for l in layers}
    if set(order) != current_ids or len(order) != len(layers):
        raise InvalidOperation("canvas.reorder 'order' must be a permutation of the current layer ids")
    by_id = {l["id"]: l for l in layers}
    content["layers"] = [by_id[i] for i in order]
    return content


OPS = {
    "canvas.add_layer": add_layer,
    "canvas.move_layer": move_layer,
    "canvas.set_mask": set_mask,
    "canvas.crop": crop,
    "canvas.rotate": rotate,
    "canvas.reorder": reorder,
}
