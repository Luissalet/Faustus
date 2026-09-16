"""Canvas layer STYLE operations — WP20 additive extension of WP12's
``ops/canvas_ops.py``.

``ops/canvas_ops.py`` (WP12, owned by that lot, not touched here) already
gives a layer ``offset``, ``opacity``, ``rotation_degrees`` (via
``rotate``), a raster/mask asset, and a crop region. WP20's canvas
compositor (``src/creator/canvas.py``) additionally needs, per layer:

* a uniform ``scale`` factor (WP20 "mover/escalar/rotar" — rotate already
  existed, scale did not),
* a ``blend_mode`` in ``normal|multiply|screen`` (WP20 "blend normal/
  multiply/screen"),
* a freehand ``mask_polygon`` — a list of ``{x, y}`` points in the
  document's own pixel space — as an ALTERNATIVE to ``mask_asset_ref``
  (WP20 "máscaras (occurrence o polígono)"); either, both or neither may
  be present on a layer, and ``canvas.py`` combines them (intersection)
  when both are set.

These are new, independent op types discovered by the SAME
``pkgutil``-based registry ``ops/canvas_ops.py`` uses (see
``ops/registry.py``) — a brand new sibling module, never an edit to
WP12's file, per CONTRATO.md's "amplía de forma aditiva". Every field
here is OPTIONAL and additive on the layer dict: ``documents.py``'s
``_validate_canvas`` only checks its own required keys are present, so a
layer with none of these fields is exactly the WP12 shape and still
validates; a layer with them is still WP12-shaped plus extra keys any
older reader ignores.
"""
from __future__ import annotations

import copy
from typing import Any, Dict, List

from ..errors import InvalidOperation
from .model import find_by_id, require_kind

BLEND_MODES = ("normal", "multiply", "screen")


def _layers(content: Dict[str, Any]) -> List[Dict[str, Any]]:
    layers = content.get("layers")
    if not isinstance(layers, list):
        raise InvalidOperation("canvas.content.layers must be an array")
    return layers


def set_layer_transform(doc: Dict[str, Any], op: Dict[str, Any]) -> Dict[str, Any]:
    """Sets a layer's ABSOLUTE scale (and, optionally, rotation in the same
    call) — never a delta, for the same replay-safety reason
    ``canvas_ops.move_layer``/``rotate`` are absolute (CONTRATO.md rule 4:
    a deduplicated replay of the same command must be a no-op on state)."""
    require_kind(doc, "canvas")
    content = copy.deepcopy(doc["content"])
    layers = _layers(content)
    layer = find_by_id(layers, op.get("object_id"), what="layer")

    scale = op.get("scale", 1.0)
    if not isinstance(scale, (int, float)) or isinstance(scale, bool) or scale <= 0:
        raise InvalidOperation("canvas.set_layer_transform 'scale' must be a positive number")
    layer["scale"] = float(scale)

    if "degrees" in op and op["degrees"] is not None:
        degrees = op["degrees"]
        if not isinstance(degrees, (int, float)) or isinstance(degrees, bool):
            raise InvalidOperation("canvas.set_layer_transform 'degrees' must be numeric when given")
        layer["rotation_degrees"] = float(degrees) % 360.0
    return content


def set_blend_mode(doc: Dict[str, Any], op: Dict[str, Any]) -> Dict[str, Any]:
    require_kind(doc, "canvas")
    content = copy.deepcopy(doc["content"])
    layers = _layers(content)
    layer = find_by_id(layers, op.get("object_id"), what="layer")
    mode = op.get("blend_mode")
    if mode not in BLEND_MODES:
        raise InvalidOperation(f"canvas.set_blend_mode 'blend_mode' must be one of {BLEND_MODES}")
    layer["blend_mode"] = mode
    return content


def _polygon(points: Any) -> List[Dict[str, int]]:
    if not isinstance(points, list) or len(points) < 3:
        raise InvalidOperation("canvas.set_polygon_mask 'points' must have at least 3 {x,y} points")
    out: List[Dict[str, int]] = []
    for i, p in enumerate(points):
        if not isinstance(p, dict) or not isinstance(p.get("x"), (int, float)) or not isinstance(p.get("y"), (int, float)):
            raise InvalidOperation(f"canvas.set_polygon_mask points[{i}] must be an object with numeric x/y")
        if isinstance(p.get("x"), bool) or isinstance(p.get("y"), bool):
            raise InvalidOperation(f"canvas.set_polygon_mask points[{i}] x/y must not be bool")
        out.append({"x": float(p["x"]), "y": float(p["y"])})
    return out


def set_polygon_mask(doc: Dict[str, Any], op: Dict[str, Any]) -> Dict[str, Any]:
    """Attaches (or clears, with ``points: null``) a freehand polygon mask
    directly on the layer's own document — no engine round trip needed for
    a simple selection, unlike ``mask_asset_ref`` which names a rasterized
    occurrence. Points are in the DOCUMENT's own pixel space (``content.
    width``/``height``), i.e. already in the coordinate system
    ``canvas.region_to_source`` maps back to the base image's raw pixels."""
    require_kind(doc, "canvas")
    content = copy.deepcopy(doc["content"])
    layers = _layers(content)
    layer = find_by_id(layers, op.get("object_id"), what="layer")
    points = op.get("points")
    if points is None:
        layer.pop("mask_polygon", None)
    else:
        layer["mask_polygon"] = _polygon(points)
    return content


OPS = {
    "canvas.set_layer_transform": set_layer_transform,
    "canvas.set_blend_mode": set_blend_mode,
    "canvas.set_polygon_mask": set_polygon_mask,
}
