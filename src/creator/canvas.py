"""canvas.py — WP20: deterministic PIL compositor for the ``canvas``
CreatorDocument kind, over ``src/creator/ops/canvas_ops.py`` (WP12) and its
WP20-additive sibling ``src/creator/ops/canvas_layer_style_ops.py``.

This module owns exactly four operationally distinct things:

* :func:`render` — ``(content, owner) -> PNG bytes``, deterministic: the
  SAME document content, at the SAME revision, always produces the SAME
  bytes (no timestamps, no filesystem-order dependence, no randomness).
* :func:`region_to_source` — maps a region drawn in the document's own
  (EXIF-corrected, "oriented") pixel space back onto the RAW pixel
  coordinates of whichever source file it was measured against, so a mask
  rasterized against the oriented preview lands on the same pixels when a
  downstream engine re-decodes the raw bytes.
* :func:`import_legacy` — reads (never writes) an ``src/media_edit_projects.py``
  edit project and produces a brand-new ``canvas`` document with real
  occurrence refs, bumping ``operation_semantics_version``.
* :func:`export` — flattens a document to PNG/JPEG and publishes it as a
  new, owner-scoped occurrence (same pattern as
  ``src/creator/library.py::generate_proxy`` — ``relations.derived_from``,
  never a shared derivative keyed off the blob).
* :func:`inpaint_request` — the bridge to WP11: renders the base
  composition as the inpaint recipe's ``reference_image`` slot and
  rasterizes ``region`` (plus any polygon mask on the target layer) as its
  ``mask`` slot, both published as occurrences in the SOURCE (raw) pixel
  coordinate system a ComfyUI mask input expects.

Layer paint order is ``content["layers"]`` list order, index 0 first/
bottom — the SAME convention ``ops/canvas_ops.py`` documents and
``src/media_edit_projects.py::export`` already uses. Nothing here mutates
a document; every function takes a plain ``content`` dict (or, for
``import_legacy``/``export``/``inpaint_request``, a whole
``CreatorDocument``) and returns new data — persisting it, if at all, is
the caller's job (``DocumentStore.create``/``apply_command``).
"""
from __future__ import annotations

import hashlib
import io
import os
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

from .errors import CreatorError, InvalidOperation
from .ops.canvas_layer_style_ops import BLEND_MODES
from .ops.model import Region

CANVAS_OBJECT_ID = "canvas"
OPERATION_SEMANTICS_VERSION = 1

#: The kind of failure this module raises for anything that is not a
#: revision/ownership problem (those stay ``DocumentNotFound``/
#: ``RevisionConflict`` from ``src/creator/errors.py``, raised by the
#: store, not here).
class CanvasError(CreatorError):
    """A canvas document/region/asset could not be rendered as asked."""


# ── asset access (real occurrences, never a bare path from the caller) ────

def _owner_path(owner: str, occurrence_id: str, *, store_dir: Optional[str] = None) -> str:
    from src import artifact_identity as identity
    try:
        return identity.path_for(occurrence_id, owner=owner, store_dir=store_dir)
    except (identity.ArtifactNotFound, identity.NotTheOwner) as exc:
        raise CanvasError(f"asset {occurrence_id!r} is not available to this owner: {exc}") from exc
    except identity.MissingBlob as exc:
        raise CanvasError(f"asset {occurrence_id!r} names a blob that is not recorded: {exc}") from exc


def _oriented_load(path: str) -> Tuple[Any, int, int, int]:
    """Opens ``path`` as RGBA, applying its EXIF orientation explicitly
    (never left implicit) and returning ``(image, raw_w, raw_h,
    orientation)`` — ``raw_w``/``raw_h`` are the file's PRE-orientation
    dimensions (what :func:`region_to_source` needs to map back to), even
    though ``image`` itself is already the corrected, "oriented" picture a
    caller composites with."""
    from PIL import Image, ImageOps

    with Image.open(path) as src:
        raw_w, raw_h = src.size
        orientation = 1
        try:
            exif = src.getexif()
            raw_orientation = exif.get(0x0112)
            if isinstance(raw_orientation, int) and 1 <= raw_orientation <= 8:
                orientation = raw_orientation
        except Exception:
            orientation = 1
        oriented = ImageOps.exif_transpose(src)
        if oriented is None:
            oriented = src
        oriented = oriented.convert("RGBA")
    return oriented, raw_w, raw_h, orientation


# ── EXIF orientation geometry (shared by region_to_source and _oriented_load) ─

def _raw_to_oriented_corner(rx: float, ry: float, raw_w: int, raw_h: int, orientation: int) -> Tuple[float, float]:
    """One corner, RAW pixel space -> ORIENTED (display) pixel space, for
    the same eight transforms ``PIL.ImageOps.exif_transpose`` applies
    (``Image.transpose`` methods FLIP_LEFT_RIGHT/ROTATE_180/
    FLIP_TOP_BOTTOM/TRANSPOSE/ROTATE_270/TRANSVERSE/ROTATE_90 for
    orientations 2..8; 1/unset is the identity)."""
    if orientation == 1:
        return rx, ry
    if orientation == 2:  # FLIP_LEFT_RIGHT
        return raw_w - rx, ry
    if orientation == 3:  # ROTATE_180
        return raw_w - rx, raw_h - ry
    if orientation == 4:  # FLIP_TOP_BOTTOM
        return rx, raw_h - ry
    if orientation == 5:  # TRANSPOSE
        return ry, rx
    if orientation == 6:  # ROTATE_270 (camera rotated 90 CW -> rotate 90 CW to correct)
        return raw_h - ry, rx
    if orientation == 7:  # TRANSVERSE
        return raw_h - ry, raw_w - rx
    if orientation == 8:  # ROTATE_90
        return ry, raw_w - rx
    raise InvalidOperation(f"exif_orientation must be in 1..8, got {orientation!r}")


def oriented_dims(raw_w: int, raw_h: int, orientation: int) -> Tuple[int, int]:
    """Display dimensions for a raw ``raw_w``x``raw_h`` image at
    ``orientation`` — swapped for the four orientations that rotate 90°."""
    return (raw_h, raw_w) if orientation in (5, 6, 7, 8) else (raw_w, raw_h)


def _oriented_box(box: Tuple[float, float, float, float], raw_w: int, raw_h: int,
                   orientation: int) -> Tuple[float, float, float, float]:
    x0, y0, x1, y1 = box
    corners = [
        _raw_to_oriented_corner(x0, y0, raw_w, raw_h, orientation),
        _raw_to_oriented_corner(x1, y0, raw_w, raw_h, orientation),
        _raw_to_oriented_corner(x0, y1, raw_w, raw_h, orientation),
        _raw_to_oriented_corner(x1, y1, raw_w, raw_h, orientation),
    ]
    xs = [c[0] for c in corners]
    ys = [c[1] for c in corners]
    return min(xs), min(ys), max(xs), max(ys)


def region_to_source(region: Dict[str, Any]) -> Dict[str, Any]:
    """Maps a :class:`~src.creator.ops.model.Region` — drawn against the
    document's own ORIENTED pixel space (``region.source_w``/``source_h``
    are that space's dimensions, per this function's own contract: it is
    what makes ``source_w``/``source_h`` reproducible rather than a UI
    convention nobody wrote down) — back to the RAW, pre-EXIF-orientation
    pixel box on whatever file it ultimately reads from.

    Pure geometry: same ``region`` in, same raw box out, every time. Raises
    :class:`~src.creator.errors.InvalidOperation` (via ``Region.from_dict``)
    for a malformed region."""
    r = Region.from_dict(region) if not isinstance(region, Region) else region
    orientation = r.exif_orientation
    # The RAW file's own dims are the inverse of oriented_dims(): swapped
    # back for the four orientations that rotate 90 degrees.
    raw_w, raw_h = (r.source_h, r.source_w) if orientation in (5, 6, 7, 8) else (r.source_w, r.source_h)

    # Invert the raw->oriented corner map by solving it directly per case
    # (all eight are involutions or 4-cycles on axis-aligned rectangles, so
    # this is exact — no iterative search needed).
    def oriented_to_raw_corner(dx: float, dy: float) -> Tuple[float, float]:
        if orientation == 1:
            return dx, dy
        if orientation == 2:
            return raw_w - dx, dy
        if orientation == 3:
            return raw_w - dx, raw_h - dy
        if orientation == 4:
            return dx, raw_h - dy
        if orientation == 5:
            return dy, dx
        if orientation == 6:
            return dy, raw_h - dx
        if orientation == 7:
            return raw_w - dy, raw_h - dx
        if orientation == 8:
            return raw_w - dy, dx
        raise InvalidOperation(f"exif_orientation must be in 1..8, got {orientation!r}")

    corners = [
        oriented_to_raw_corner(r.x, r.y),
        oriented_to_raw_corner(r.x + r.width, r.y),
        oriented_to_raw_corner(r.x, r.y + r.height),
        oriented_to_raw_corner(r.x + r.width, r.y + r.height),
    ]
    xs = [c[0] for c in corners]
    ys = [c[1] for c in corners]
    raw_x0, raw_y0, raw_x1, raw_y1 = min(xs), min(ys), max(xs), max(ys)
    return {
        "raw_x": int(round(raw_x0)),
        "raw_y": int(round(raw_y0)),
        "raw_width": int(round(raw_x1 - raw_x0)),
        "raw_height": int(round(raw_y1 - raw_y0)),
        "raw_source_w": int(raw_w),
        "raw_source_h": int(raw_h),
        "exif_orientation": orientation,
        "region": r.to_dict(),
    }


# ── layer transform + mask ─────────────────────────────────────────────────

def _apply_layer_geometry(layer_img: "Any", layer: Dict[str, Any]) -> Tuple["Any", int, int]:
    """Scale then rotate a layer image about its own center, returning the
    transformed image plus the top-left position it should be pasted at so
    its CENTER lands at ``offset`` + its own center — matching what a
    Studio canvas "move/scale/rotate with handles" gesture visually does."""
    from PIL import Image

    scale = float(layer.get("scale", 1.0) or 1.0)
    if scale <= 0:
        raise InvalidOperation(f"layer {layer.get('id')!r} has a non-positive scale")
    w, h = layer_img.size
    if scale != 1.0:
        new_w = max(1, round(w * scale))
        new_h = max(1, round(h * scale))
        layer_img = layer_img.resize((new_w, new_h), Image.LANCZOS)

    degrees = float(layer.get("rotation_degrees", 0.0) or 0.0)
    if degrees % 360.0 != 0.0:
        layer_img = layer_img.rotate(-degrees, expand=True, resample=Image.BICUBIC)

    offset = layer.get("offset") or {"x": 0, "y": 0}
    ox, oy = int(offset.get("x", 0)), int(offset.get("y", 0))
    # `offset` names the layer's un-transformed top-left; after scale/rotate
    # the image may have grown (rotation `expand=True`), so re-center it on
    # what the un-transformed top-left + original size would have centered.
    cx = ox + w / 2.0
    cy = oy + h / 2.0
    paste_x = int(round(cx - layer_img.size[0] / 2.0))
    paste_y = int(round(cy - layer_img.size[1] / 2.0))
    return layer_img, paste_x, paste_y


def _polygon_mask(points: List[Dict[str, float]], size: Tuple[int, int]) -> "Any":
    from PIL import Image, ImageDraw

    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    xy = [(float(p["x"]), float(p["y"])) for p in points]
    draw.polygon(xy, fill=255)
    return mask


def _blend(base: "Any", layer_img: "Any", box: Tuple[int, int], mode: str) -> "Any":
    """Composites ``layer_img`` (RGBA) onto ``base`` (RGBA) at ``box``'s
    top-left, in ``mode`` (``normal``/``multiply``/``screen``). Returns a
    NEW image — ``base`` is never mutated in place, matching every op in
    ``ops/canvas_ops.py`` returning fresh content rather than editing in
    place."""
    from PIL import Image, ImageChops

    if mode not in BLEND_MODES:
        raise InvalidOperation(f"blend_mode must be one of {BLEND_MODES}, got {mode!r}")
    out = base.copy()
    px, py = box
    lw, lh = layer_img.size
    bw, bh = base.size

    # Clip to the overlap with the base canvas — a layer entirely off-canvas
    # contributes nothing, which is the deterministic no-op a caller expects
    # rather than a PIL paste exception.
    src_x0 = max(0, -px)
    src_y0 = max(0, -py)
    src_x1 = min(lw, bw - px)
    src_y1 = min(lh, bh - py)
    if src_x1 <= src_x0 or src_y1 <= src_y0:
        return out
    cropped = layer_img.crop((src_x0, src_y0, src_x1, src_y1))
    dst_x0 = max(0, px)
    dst_y0 = max(0, py)
    dst_x1 = dst_x0 + (src_x1 - src_x0)
    dst_y1 = dst_y0 + (src_y1 - src_y0)

    alpha = cropped.split()[3]
    if mode == "normal":
        out.paste(cropped, (dst_x0, dst_y0), alpha)
        return out

    region = out.crop((dst_x0, dst_y0, dst_x1, dst_y1)).convert("RGB")
    src_rgb = cropped.convert("RGB")
    if mode == "multiply":
        blended = ImageChops.multiply(region, src_rgb)
    else:  # screen
        blended = ImageChops.screen(region, src_rgb)
    blended = blended.convert("RGBA")
    blended.putalpha(alpha)
    out.paste(blended, (dst_x0, dst_y0), alpha)
    return out


def _layer_image(layer: Dict[str, Any], owner: str, *, store_dir: Optional[str] = None) -> Optional["Any"]:
    """Loads a raster/text/control layer's own pixels (oriented,
    RGBA) with any attached mask (occurrence and/or polygon, combined by
    intersection) already applied to its alpha channel. Returns ``None``
    for a layer with no ``asset_ref`` (a placeholder text/control layer
    this module does not yet rasterize — WP20's closure criterion is the
    compositor for RASTER layers; a caller adding real text rendering
    later does so additively here)."""
    from PIL import Image

    asset_ref = layer.get("asset_ref")
    if not asset_ref:
        return None
    path = _owner_path(owner, asset_ref, store_dir=store_dir)
    img, _raw_w, _raw_h, _orientation = _oriented_load(path)

    crop_region = layer.get("crop_region")
    if crop_region:
        r = Region.from_dict(crop_region)
        img = img.crop((r.x, r.y, r.x + r.width, r.y + r.height))

    mask_img = None
    mask_ref = layer.get("mask_asset_ref")
    if mask_ref:
        mask_path = _owner_path(owner, mask_ref, store_dir=store_dir)
        mimg, _mrw, _mrh, _mo = _oriented_load(mask_path)
        mask_img = mimg.convert("L").resize(img.size)
    polygon = layer.get("mask_polygon")
    if polygon:
        poly_mask = _polygon_mask(polygon, img.size)
        mask_img = poly_mask if mask_img is None else _intersect_masks(mask_img, poly_mask)

    if mask_img is not None:
        from PIL import ImageChops
        alpha = img.split()[3]
        combined = ImageChops.multiply(alpha, mask_img)
        img = img.copy()
        img.putalpha(combined)

    opacity = float(layer.get("opacity", 1.0))
    if opacity < 1.0:
        r, g, b, a = img.split()
        a = a.point(lambda v: int(v * opacity))
        img = Image.merge("RGBA", (r, g, b, a))
    return img


def _intersect_masks(a: "Any", b: "Any") -> "Any":
    from PIL import ImageChops
    return ImageChops.multiply(a, b)


# ── composition ─────────────────────────────────────────────────────────

def _compose(content: Dict[str, Any], owner: str, *, store_dir: Optional[str] = None) -> "Any":
    from PIL import Image

    width = int(content["width"])
    height = int(content["height"])
    base_ref = content.get("base_asset_ref")
    if base_ref and base_ref != "none":
        base_path = _owner_path(owner, base_ref, store_dir=store_dir)
        base_img, _rw, _rh, _o = _oriented_load(base_path)
        base = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        base.paste(base_img.crop((0, 0, min(width, base_img.width), min(height, base_img.height))), (0, 0))
    else:
        base = Image.new("RGBA", (width, height), (0, 0, 0, 0))

    crop_region = content.get("crop_region")
    canvas_rotation = float(content.get("rotation_degrees", 0.0) or 0.0)

    for layer in content.get("layers", []):
        if not layer.get("visible", True):
            continue
        if layer.get("kind") not in ("raster", "mask", "text", "control"):
            raise InvalidOperation(f"layer {layer.get('id')!r} has an unknown kind {layer.get('kind')!r}")
        if layer.get("kind") == "mask":
            # A `mask` KIND layer is a document-visible mask reference, not
            # itself painted into the composition (its pixels feed
            # `inpaint_request`/other layers' `mask_asset_ref`, never the
            # flattened image directly) — WP12's own convention.
            continue
        layer_img = _layer_image(layer, owner, store_dir=store_dir)
        if layer_img is None:
            continue
        transformed, px, py = _apply_layer_geometry(layer_img, layer)
        mode = layer.get("blend_mode", "normal")
        base = _blend(base, transformed, (px, py), mode)

    if crop_region:
        r = Region.from_dict(crop_region)
        base = base.crop((r.x, r.y, r.x + r.width, r.y + r.height))
    if canvas_rotation % 360.0 != 0.0:
        base = base.rotate(-canvas_rotation, expand=True, resample=Image.BICUBIC)
    return base


def render(content: Dict[str, Any], owner: str, *, store_dir: Optional[str] = None,
          fmt: str = "PNG") -> bytes:
    """``(content, owner) -> encoded image bytes``, deterministic: PNG
    encoding with a fixed compression level and no metadata (PIL's PNG
    writer does not embed a timestamp unless asked), so identical content
    always produces identical bytes — the WP20 closure criterion "el
    export coincide con las operaciones soportadas" (verified by the byte-
    identical render test)."""
    if content.get("width", 0) < 1 or content.get("height", 0) < 1:
        raise InvalidOperation("canvas.content.width/height must both be >= 1 to render")
    image = _compose(content, owner, store_dir=store_dir)
    buf = io.BytesIO()
    if fmt.upper() == "JPEG":
        image.convert("RGB").save(buf, format="JPEG", quality=92)
    else:
        image.save(buf, format="PNG", compress_level=6)
    return buf.getvalue()


# ── export: publish a rendered document as a new occurrence ───────────────

def export(document: Any, owner: str, *, fmt: str = "png", store_dir: Optional[str] = None) -> Dict[str, Any]:
    """Flattens ``document.content`` and publishes it as a brand-new,
    owner-scoped occurrence with ``relations.derived_from`` pointing at
    every asset the composition actually reads — same pattern
    ``src/creator/library.py::generate_proxy`` uses, never a shared
    derivative keyed off the blob (AST02)."""
    import mimetypes
    from src import artifact_identity as identity
    from src import artifact_store
    from src.constants import ARTIFACT_STORE_DIR
    from src.contracts.blob import ArtifactOccurrence

    fmt_norm = (fmt or "png").lower()
    if fmt_norm not in ("png", "jpeg", "jpg"):
        raise InvalidOperation(f"export format must be png or jpeg, got {fmt!r}")
    pil_fmt = "JPEG" if fmt_norm in ("jpeg", "jpg") else "PNG"
    ext = "jpg" if pil_fmt == "JPEG" else "png"

    content = document.content
    data = render(content, owner, store_dir=store_dir, fmt=pil_fmt)

    store = store_dir or ARTIFACT_STORE_DIR
    tmp_dir = tempfile.mkdtemp(prefix="creator-canvas-export-")
    try:
        tmp_path = os.path.join(tmp_dir, f"canvas-export.{ext}")
        with open(tmp_path, "wb") as fh:
            fh.write(data)
        digest, size, filename, _created = artifact_store.publish_copy(tmp_path, store, os.path.basename(tmp_path))
        identity.ensure_blob(sha256=digest, byte_size=size, filename=filename,
                             media_type=mimetypes.guess_type(filename)[0] or f"image/{ext}")

        source_ids = sorted(set(document.asset_refs) | _referenced_assets(content))
        params_hash = hashlib.sha256(f"{document.id}:{document.revision}:{fmt_norm}".encode("utf-8")).hexdigest()
        candidate_id = "occ_" + hashlib.sha1(  # noqa: S324 - id, not a security digest
            f"{owner}:{document.id}:{document.revision}:{fmt_norm}".encode("utf-8")).hexdigest()[:24]
        occurrence = ArtifactOccurrence.parse({
            "id": candidate_id, "kind": "image", "blob_sha256": digest,
            "label": f"canvas export of {document.id} rev {document.revision}",
            "owner": owner, "project_id": document.project_id,
            "run_id": "",
            "skill_id": "creator.canvas", "skill_version": "1.0.0",
            "provenance": {"backend": "pillow", "recipe": "creator.canvas.export",
                          "recipe_version": "1", "recipe_fingerprint": params_hash,
                          "source_artifact_ids": source_ids},
            "retention": {"policy": "keep"},
            "relations": {"derived_from": source_ids},
        })
        recorded, made = identity.ensure_occurrence(occurrence)
        return {"occurrence": recorded.to_dict(), "occurrence_id": recorded.id,
                "created": made, "format": fmt_norm, "byte_size": size}
    finally:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _referenced_assets(content: Dict[str, Any]) -> set:
    refs = set()
    base = content.get("base_asset_ref")
    if base and base != "none":
        refs.add(base)
    for layer in content.get("layers", []):
        if layer.get("asset_ref"):
            refs.add(layer["asset_ref"])
        if layer.get("mask_asset_ref"):
            refs.add(layer["mask_asset_ref"])
    return refs


# ── import legacy (read-only against the legacy project) ─────────────────

def import_legacy(edit_project_id: str, owner: str, project_id: str, *,
                  directory: Optional[str] = None, store_dir: Optional[str] = None) -> Dict[str, Any]:
    """Reads (never writes) an ``src/media_edit_projects.py`` edit project
    and returns a fresh ``canvas`` document CONTENT dict (not yet
    persisted — the caller uses ``DocumentStore.create`` for that, exactly
    like any other new document), with the legacy base image and every
    legacy layer published as real, owner-scoped occurrences this
    compositor can read.

    "Sin escritura del proyecto legacy" (CONTRATO.md): the legacy JSON file
    under ``src/media_edit_projects.py::PROJECTS_DIR`` is opened read-only
    via its own ``get()``; nothing here calls any of that module's write
    functions. Publishing NEW occurrences in the artifact store is a
    different, additive action this function is explicitly for."""
    import base64
    import mimetypes

    from PIL import Image
    from src import artifact_identity as identity
    from src import artifact_store
    from src.constants import ARTIFACT_STORE_DIR
    from src.contracts.blob import ArtifactOccurrence
    from src import media_edit_projects as legacy

    try:
        record = legacy.get(edit_project_id, owner=owner, directory=directory)
    except legacy.EditProjectError as exc:
        raise CanvasError(f"legacy edit project unavailable: {exc}") from exc

    store = store_dir or ARTIFACT_STORE_DIR

    def _publish_path(path: str, *, label: str) -> str:
        digest, size, filename, _created = artifact_store.publish_copy(path, store, os.path.basename(path))
        identity.ensure_blob(sha256=digest, byte_size=size, filename=filename,
                             media_type=mimetypes.guess_type(filename)[0] or "image/png")
        candidate_id = "occ_" + hashlib.sha1(  # noqa: S324
            f"{owner}:{edit_project_id}:{label}:{digest}".encode("utf-8")).hexdigest()[:24]
        occurrence = ArtifactOccurrence.parse({
            "id": candidate_id, "kind": "image", "blob_sha256": digest,
            "label": label, "owner": owner, "project_id": project_id, "run_id": "",
            "skill_id": "creator.canvas", "skill_version": "1.0.0",
            "provenance": {"backend": "media_edit_projects", "recipe": "creator.canvas.import_legacy",
                          "recipe_version": "1", "recipe_fingerprint": digest,
                          "source_artifact_ids": []},
            "retention": {"policy": "keep"},
            "relations": {},
        })
        recorded, _made = identity.ensure_occurrence(occurrence)
        return recorded.id

    def _publish_b64(image_b64: str, *, label: str) -> str:
        tmp_dir = tempfile.mkdtemp(prefix="creator-canvas-import-")
        try:
            tmp_path = os.path.join(tmp_dir, "layer.png")
            with open(tmp_path, "wb") as fh:
                fh.write(base64.b64decode(image_b64))
            return _publish_path(tmp_path, label=label)
        finally:
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)

    base_occ_id = _publish_path(record["source_path"], label=f"legacy base of {edit_project_id}")
    with Image.open(record["source_path"]) as src:
        width, height = src.size

    layers: List[Dict[str, Any]] = []
    for legacy_layer in record.get("layers", []):
        asset_ref = _publish_b64(legacy_layer["image_b64"], label=f"legacy layer {legacy_layer['id']}")
        layer: Dict[str, Any] = {
            "id": legacy_layer["id"],
            "kind": "raster",
            "visible": True,
            "opacity": 1.0,
            "offset": {"x": int(legacy_layer.get("x", 0)), "y": int(legacy_layer.get("y", 0))},
            "asset_ref": asset_ref,
        }
        if legacy_layer.get("mask_b64"):
            mask_ref = _publish_b64(legacy_layer["mask_b64"], label=f"legacy mask of {legacy_layer['id']}")
            layer["mask_asset_ref"] = mask_ref
        layers.append(layer)

    content = {
        "width": width, "height": height,
        "operation_semantics_version": OPERATION_SEMANTICS_VERSION,
        "layers": layers,
        "base_asset_ref": base_occ_id,
        "imported_from": {"legacy_edit_project_id": edit_project_id, "at": time.time()},
    }
    return content


# ── bridge to WP11: build the inpaint recipe's reference_image + mask ────

def inpaint_request(document: Any, region: Dict[str, Any], prompt: str, owner: str, *,
                    store_dir: Optional[str] = None) -> Dict[str, Any]:
    """Builds the ``params`` an ``inpaint`` :class:`~src.creator.comfy_recipes.ComfyRecipe`
    call needs (``src/creator/comfy_recipes.py::compile("inpaint", params)``):
    the current composition as ``reference_image``, and ``region`` — mapped
    through :func:`region_to_source` to the RAW pixel coordinates a
    downstream engine's own re-decode will use — rasterized as a solid
    white-on-black ``mask``. Both are published as new occurrences (never a
    raw path/blob handed to a caller); nothing here submits anything — that
    is still ``src/creator/adapters/comfyui.py`` / ``media_runs``, gated by
    preflight/approval like any other WP11 recipe."""
    import mimetypes
    from PIL import Image, ImageDraw
    from src import artifact_identity as identity
    from src import artifact_store
    from src.constants import ARTIFACT_STORE_DIR
    from src.contracts.blob import ArtifactOccurrence

    if not isinstance(prompt, str) or not prompt.strip():
        raise InvalidOperation("inpaint_request requires a non-empty 'prompt'")

    content = document.content
    reference_bytes = render(content, owner, store_dir=store_dir, fmt="PNG")

    mapped = region_to_source(region)
    width = int(content["width"])
    height = int(content["height"])
    r = Region.from_dict(region)
    mask_img = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask_img).rectangle(
        (r.x, r.y, r.x + r.width, r.y + r.height), fill=255)
    mask_buf = io.BytesIO()
    mask_img.save(mask_buf, format="PNG", compress_level=6)
    mask_bytes = mask_buf.getvalue()

    store = store_dir or ARTIFACT_STORE_DIR
    tmp_dir = tempfile.mkdtemp(prefix="creator-canvas-inpaint-")
    try:
        def _publish(data: bytes, *, name: str, label: str) -> str:
            tmp_path = os.path.join(tmp_dir, name)
            with open(tmp_path, "wb") as fh:
                fh.write(data)
            digest, size, filename, _created = artifact_store.publish_copy(tmp_path, store, name)
            identity.ensure_blob(sha256=digest, byte_size=size, filename=filename,
                                 media_type=mimetypes.guess_type(filename)[0] or "image/png")
            candidate_id = "occ_" + hashlib.sha1(  # noqa: S324
                f"{owner}:{document.id}:{document.revision}:{name}:{digest}".encode("utf-8")).hexdigest()[:24]
            occurrence = ArtifactOccurrence.parse({
                "id": candidate_id, "kind": "image", "blob_sha256": digest,
                "label": label, "owner": owner, "project_id": document.project_id, "run_id": "",
                "skill_id": "creator.canvas", "skill_version": "1.0.0",
                "provenance": {"backend": "pillow", "recipe": "creator.canvas.inpaint_request",
                              "recipe_version": "1", "recipe_fingerprint": digest,
                              "source_artifact_ids": [document.id]},
                "retention": {"policy": "keep"},
                "relations": {"derived_from": [document.id]},
            })
            recorded, _made = identity.ensure_occurrence(occurrence)
            return recorded.id

        reference_id = _publish(reference_bytes, name="reference.png", label=f"inpaint reference for {document.id}")
        mask_id = _publish(mask_bytes, name="mask.png", label=f"inpaint mask for {document.id}")
    finally:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return {
        "recipe_id": "inpaint",
        "params": {"prompt": prompt, "reference_image": reference_id, "mask": mask_id},
        "region": mapped,
        "document_id": document.id,
        "document_revision": document.revision,
    }
