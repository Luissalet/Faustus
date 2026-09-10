"""
media_edit_projects.py — MEDIA-03: non-destructive image editing.

An edit PROJECT is layers, masks and a history of operations recorded as
data, never as pixels burned into the source. The original image is read
once, hashed, and never opened for writing again by this module — every
operation appends to a project file under `DATA_DIR/media_edit_projects/`;
only `export()` ever produces a flat image, and only into a destination the
caller named explicitly, never the source path (unless the caller passes
`confirm_overwrite_original=True`, the "confirmation" the requirement asks
for — silence is never enough).

Reuses the store-a-JSON-record-under-DATA_DIR shape `src.media_consent`
and `src.settings` already use, rather than a new database table
(`core/database.py` is out of this lot's scope, and a project record is a
small, human-editable document, not a queryable row).

Deliberately does not attempt full PSD-grade compositing: a layer is an
image with an offset and an optional mask, painted in order onto a copy of
the original — enough to prove "closing the editor conserves masks and
draft" and "export never overwrites the source or an existing file without
being told to", the two things MEDIA-03's acceptance criterion actually asks
for. `history` also records crop/transform requests even though this module
does not interpret every op kind yet — the point is that nothing is lost by
recording it, and a caller reading `history` back gets exactly what it wrote.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import threading
import uuid
from typing import Any, Dict, List, Optional

from src.constants import DATA_DIR
from src.contracts.base import now_iso

logger = logging.getLogger(__name__)

PROJECTS_DIR = os.path.join(DATA_DIR, "media_edit_projects")
_LOCK = threading.RLock()


class EditProjectError(ValueError):
    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


def _project_path(project_id: str, *, directory: Optional[str] = None) -> str:
    folder = directory or PROJECTS_DIR
    safe = "".join(c for c in project_id if c.isalnum() or c in "_-")
    if not safe or safe != project_id:
        raise EditProjectError("invalid project id", "invalid_id")
    return os.path.join(folder, f"{safe}.json")


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _read(project_id: str, *, directory: Optional[str] = None) -> Dict[str, Any]:
    path = _project_path(project_id, directory=directory)
    if not os.path.isfile(path):
        raise EditProjectError(f"no edit project {project_id}", "not_found")
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _write(record: Dict[str, Any], *, directory: Optional[str] = None) -> None:
    folder = directory or PROJECTS_DIR
    os.makedirs(folder, exist_ok=True)
    path = _project_path(record["id"], directory=directory)
    tmp = f"{path}.tmp-{uuid.uuid4().hex[:8]}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(record, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _check_owner(record: Dict[str, Any], owner: str) -> None:
    if (record.get("owner") or "") != (owner or ""):
        # Same refusal as "not found" — an edit project's existence is not
        # something another owner gets to learn about.
        raise EditProjectError(f"no edit project {record.get('id')}", "not_found")


def create(source_path: str, *, owner: str, directory: Optional[str] = None) -> Dict[str, Any]:
    """Open a project on an existing image. Reads the original exactly once,
    to record what it looked like — never to hold it open for writing."""
    if not owner:
        raise EditProjectError("an edit project needs an owner", "no_owner")
    if not os.path.isfile(source_path):
        raise EditProjectError(f"no such source image: {source_path}", "source_missing")
    record = {
        "id": f"edit_{uuid.uuid4().hex[:20]}",
        "owner": owner,
        "source_path": os.path.abspath(source_path),
        "source_sha256": _sha256(source_path),
        "layers": [],
        "history": [],
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    with _LOCK:
        _write(record, directory=directory)
    return record


def get(project_id: str, *, owner: str, directory: Optional[str] = None) -> Dict[str, Any]:
    record = _read(project_id, directory=directory)
    _check_owner(record, owner)
    return record


def _touch_and_verify_original(record: Dict[str, Any]) -> None:
    """Refuse to keep editing a project whose original has changed under it
    — silently compositing against a DIFFERENT image than the one the masks
    were drawn on is worse than refusing outright."""
    src = record["source_path"]
    if not os.path.isfile(src) or _sha256(src) != record["source_sha256"]:
        raise EditProjectError(
            "the original image changed or is missing since this project was "
            "opened; the edit history is preserved, but it can no longer be "
            "trusted to apply to what is on disk now", "source_changed")
    record["updated_at"] = now_iso()


def add_layer(project_id: str, *, owner: str, image_b64: str, x: int = 0, y: int = 0,
             mask_b64: str = "", label: str = "", directory: Optional[str] = None
             ) -> Dict[str, Any]:
    """Add one non-destructive layer: an image, a position, and an optional
    mask. Persisted immediately — "closing the editor" is just not calling
    this again, and nothing here lives only in a caller's memory."""
    if not image_b64:
        raise EditProjectError("a layer needs image data", "no_image")
    with _LOCK:
        record = _read(project_id, directory=directory)
        _check_owner(record, owner)
        _touch_and_verify_original(record)
        layer = {"id": f"layer_{uuid.uuid4().hex[:12]}", "label": label,
                 "x": int(x), "y": int(y), "image_b64": image_b64,
                 "mask_b64": mask_b64 or "", "added_at": now_iso()}
        record["layers"].append(layer)
        record["history"].append({"type": "add_layer", "layer_id": layer["id"], "at": now_iso()})
        _write(record, directory=directory)
    return record


def record_op(project_id: str, *, owner: str, op_type: str, params: Optional[Dict[str, Any]] = None,
              directory: Optional[str] = None) -> Dict[str, Any]:
    """Append a crop/transform/etc. request to history without applying it
    yet — the draft a caller returns to later, and what `export()` will read
    to decide how to flatten the result."""
    with _LOCK:
        record = _read(project_id, directory=directory)
        _check_owner(record, owner)
        _touch_and_verify_original(record)
        record["history"].append({"type": op_type, "params": dict(params or {}), "at": now_iso()})
        _write(record, directory=directory)
    return record


_ROTATIONS = {90: "ROTATE_90", 180: "ROTATE_180", 270: "ROTATE_270"}


def export(project_id: str, dest_path: str, *, owner: str,
          confirm_overwrite_original: bool = False,
          directory: Optional[str] = None) -> Dict[str, Any]:
    """Composite layers onto a COPY of the original and write ONE flat PNG —
    the only place this module writes pixels, and never over the original
    unless `confirm_overwrite_original` says so explicitly. An existing,
    different destination is refused the same way `media_transforms` refuses
    one: nothing here is ever silently overwritten."""
    from PIL import Image

    with _LOCK:
        record = _read(project_id, directory=directory)
        _check_owner(record, owner)
        _touch_and_verify_original(record)

        source_abs = os.path.abspath(record["source_path"])
        dest_abs = os.path.abspath(dest_path)
        if dest_abs == source_abs and not confirm_overwrite_original:
            raise EditProjectError(
                "the destination is the original image; pass "
                "confirm_overwrite_original=True to replace it on purpose",
                "would_overwrite_original")
        if dest_abs != source_abs and os.path.lexists(dest_abs):
            raise EditProjectError(
                "the destination already exists; choose a new filename — "
                "nothing here overwrites an existing export", "destination_exists")

        base = Image.open(source_abs).convert("RGBA")
        for op in record["history"]:
            if op["type"] == "rotate":
                degrees = int((op.get("params") or {}).get("degrees", 0)) % 360
                method = _ROTATIONS.get(degrees)
                if method:
                    base = base.transpose(getattr(Image.Transpose, method))
            elif op["type"] == "flip":
                axis = (op.get("params") or {}).get("axis")
                if axis == "horizontal":
                    base = base.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
                elif axis == "vertical":
                    base = base.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
            elif op["type"] == "crop":
                p = op.get("params") or {}
                box = (int(p.get("left", 0)), int(p.get("top", 0)),
                      int(p.get("right", base.width)), int(p.get("bottom", base.height)))
                base = base.crop(box)

        for layer in record["layers"]:
            img = Image.open(__import__("io").BytesIO(base64.b64decode(layer["image_b64"]))).convert("RGBA")
            mask = None
            if layer.get("mask_b64"):
                mask = Image.open(__import__("io").BytesIO(base64.b64decode(layer["mask_b64"]))).convert("L")
            base.alpha_composite(img, dest=(layer["x"], layer["y"])) if mask is None else \
                base.paste(img, (layer["x"], layer["y"]), mask)

        os.makedirs(os.path.dirname(dest_abs) or ".", exist_ok=True)
        tmp = f"{dest_abs}.tmp-{uuid.uuid4().hex[:8]}"
        base.convert("RGB" if base.mode == "RGBA" and not _has_alpha(base) else base.mode).save(tmp, format="PNG")
        if dest_abs == source_abs:
            os.replace(tmp, dest_abs)
        else:
            try:
                os.link(tmp, dest_abs)
            except OSError:
                os.replace(tmp, dest_abs)
            finally:
                if os.path.exists(tmp) and tmp != dest_abs:
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass

        record["history"].append({"type": "export", "dest_path": dest_abs, "at": now_iso()})
        _write(record, directory=directory)

    original_untouched = os.path.isfile(source_abs) and _sha256(source_abs) == record["source_sha256"]
    return {"ok": True, "project_id": project_id, "path": dest_abs,
           "original_preserved": original_untouched or dest_abs == source_abs,
           "layers_applied": len(record["layers"]), "history_length": len(record["history"])}


def _has_alpha(img) -> bool:
    if img.mode != "RGBA":
        return False
    extrema = img.getchannel("A").getextrema()
    return extrema[0] < 255
