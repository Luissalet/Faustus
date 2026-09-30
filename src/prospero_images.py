"""Local Prospero image adapter, with durable request receipts and owned output.

Callers supply server-owned identities and resolve input ownership. The adapter
checks current image settings, privileges and persisted session ownership.
Provide a stable request_id to recover an interrupted invocation; no id means
a new intentional request. Ambiguous writes are never automatically repeated.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import urlsplit
import uuid
import warnings

import httpx
from PIL import Image

from src.constants import AUTH_FILE, DATA_DIR, GENERATED_IMAGES_DIR

MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
POLL_TIMEOUT_S = 300.0
POLL_INTERVAL_S = 0.75
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


class AdapterError(Exception):
    pass


def _authorize(owner, session_id):
    from src.settings import get_user_setting
    from src.owner_identity import auth_disabled, effective_storage_owner
    from core.auth import DEFAULT_PRIVILEGES
    from src.database import Session, SessionLocal
    if not get_user_setting("image_gen_enabled", owner or "", False):
        raise AdapterError("Image generation is disabled for this account")
    if not auth_disabled():
        try:
            with open(AUTH_FILE, "r", encoding="utf-8") as stream:
                user = json.load(stream).get("users", {}).get(owner)
            if not isinstance(user, dict):
                raise AdapterError("An authenticated image owner is required")
            if not user.get("is_admin") and not (user.get("privileges") or {}).get(
                    "can_generate_images", DEFAULT_PRIVILEGES["can_generate_images"]):
                raise AdapterError("This account cannot generate images")
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError("Image privileges could not be verified") from exc
    if session_id:
        db = SessionLocal()
        try:
            session = db.query(Session).filter(Session.id == session_id).first()
            stored_owner = effective_storage_owner(session.owner) if session and auth_disabled() else (session.owner if session else None)
            if not session or (stored_owner or "") != (owner or ""):
                raise AdapterError("The image session does not belong to this account")
        finally:
            db.close()


def _identifier(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise AdapterError("Prospero returned an invalid identifier")
    return value


def _bound_url(value):
    try:
        parts = urlsplit(str(value))
        host = parts.hostname
        port = parts.port
        literal = host == "localhost" or (host and ipaddress.ip_address(host).is_loopback)
    except ValueError as exc:
        raise AdapterError("Invalid Prospero connection URL") from exc
    if (not literal or parts.scheme != "http" or not port or parts.username is not None
            or parts.password is not None or "?" in str(value) or "#" in str(value)
            or parts.path not in ("", "/")):
        raise AdapterError("Prospero connection must use a plain local loopback HTTP origin with an explicit port")
    # Prospero's Host guard accepts these three spellings, not arbitrary 127/8.
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise AdapterError("Prospero connection host is unsupported")
    # Do not resolve localhost through mutable DNS/hosts configuration.
    host = "127.0.0.1" if host == "localhost" else host
    return f"http://{'[' + host + ']' if host == '::1' else host}:{port}"


def _connection(owner):
    from src import plugin_runtime
    found = plugin_runtime.resolve("prospero", owner=owner)
    row = found.get("connector")
    if (not row or row.get("preset_id") != "prospero"
            or (row.get("owner") and row.get("owner") != owner)):
        raise AdapterError("No visible Prospero connection is configured")
    return str(row["id"]), _bound_url(row.get("app_url"))


def _db():
    Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(Path(DATA_DIR) / "prospero_images.db"), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA synchronous=FULL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS projects (
            scope TEXT PRIMARY KEY, state TEXT NOT NULL, project_id TEXT);
        CREATE TABLE IF NOT EXISTS requests (
            key TEXT PRIMARY KEY, request_id TEXT NOT NULL, owner TEXT NOT NULL,
            session_id TEXT NOT NULL, connector_id TEXT NOT NULL, origin TEXT NOT NULL,
            fingerprint TEXT NOT NULL, prompt TEXT, state TEXT NOT NULL, project_id TEXT,
            job_id TEXT, input_asset_id TEXT, asset_id TEXT, gallery_id TEXT, filename TEXT,
            operation TEXT DEFAULT 'generate', mask_asset_id TEXT);
    """)
    conn.execute("BEGIN IMMEDIATE")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(requests)")}
    for name, declaration in (("prompt", "TEXT"), ("operation", "TEXT DEFAULT 'generate'"), ("mask_asset_id", "TEXT")):
        if name not in columns:
            conn.execute(f"ALTER TABLE requests ADD COLUMN {name} {declaration}")
    conn.commit()
    return conn


def _read(key):
    conn = _db()
    try:
        row = conn.execute("SELECT * FROM requests WHERE key=?", (key,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _update(key, **fields):
    conn = _db()
    try:
        # Keys are private constants from this module, never client data.
        conn.execute("UPDATE requests SET " + ",".join(f"{field}=?" for field in fields) + " WHERE key=?",
                     (*fields.values(), key))
        conn.commit()
    finally:
        conn.close()


def _digest(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()


def _reserve(key, request_id, owner, session_id, connector_id, origin, fingerprint, prompt, operation):
    conn = _db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute("SELECT * FROM requests WHERE key=?", (key,)).fetchone()
        if existing:
            row = dict(existing)
            if (row["fingerprint"] != fingerprint or row["connector_id"] != connector_id
                    or row["origin"] != origin):
                raise AdapterError("This image request ID was already bound to different input or connection")
            if row["prompt"] is None:
                conn.execute("UPDATE requests SET prompt=? WHERE key=?", (prompt, key))
                conn.commit()
            return False
        conn.execute("INSERT INTO requests(key,request_id,owner,session_id,connector_id,origin,fingerprint,prompt,operation,state)"
                     " VALUES(?,?,?,?,?,?,?,?,?,'preparing')",
                     (key, request_id, owner or "", session_id or "", connector_id, origin, fingerprint, prompt, operation))
        conn.commit()
        return True
    finally:
        conn.close()


def _validate_image(content, *, png_only=False):
    if not content or len(content) > MAX_IMAGE_BYTES:
        raise AdapterError("Image exceeds the allowed size or is empty")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as image:
                if image.format not in (("PNG",) if png_only else ("PNG", "JPEG", "WEBP")):
                    raise AdapterError("Prospero image format is unsupported")
                width, height = image.size
                if width * height > MAX_IMAGE_PIXELS or width < 1 or height < 1:
                    raise AdapterError("Image dimensions exceed the allowed limit")
                image.verify()
            # verify checks the container; decoding also rejects truncated data.
            with Image.open(io.BytesIO(content)) as image:
                image.load()
                kind = image.format
        return width, height, kind
    except AdapterError:
        raise
    except Exception as exc:
        raise AdapterError("Prospero returned an invalid image") from exc


async def _json(client, method, path, **kwargs):
    response = await client.request(method, path, **kwargs)
    if response.status_code != 200:
        raise AdapterError(f"Prospero request failed (HTTP {response.status_code})")
    if len(response.content) > 2 * 1024 * 1024:
        raise AdapterError("Prospero response exceeds the allowed size")
    body = response.json()
    if not isinstance(body, dict):
        raise AdapterError("Prospero returned an invalid response")
    return body


async def _project(client, scope):
    conn = _db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM projects WHERE scope=?", (scope,)).fetchone()
        if row:
            if row["state"] != "ready":
                raise AdapterError("Prospero project creation is unresolved; it will not be repeated")
            return _identifier(row["project_id"])
        conn.execute("INSERT INTO projects(scope,state) VALUES(?,'create_intent')", (scope,))
        conn.commit()
    finally:
        conn.close()
    # Only this reserved creator sends the request. A crash/timeout leaves an
    # unresolved receipt rather than choosing an existing user project.
    body = await _json(client, "POST", "/api/projects", json={"name": "Faustus image workspace " + scope[:12]})
    project_id = _identifier(body.get("id"))
    conn = _db()
    try:
        conn.execute("UPDATE projects SET state='ready',project_id=? WHERE scope=?", (project_id, scope))
        conn.commit()
    finally:
        conn.close()
    return project_id


async def _import_reference(client, key, project_id, content, *, mask=False):
    row = _read(key)
    _authorize(row["owner"] or None, row["session_id"] or None)
    _, _, kind = _validate_image(content, png_only=mask)
    extension = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp"}[kind]
    _update(key, state="mask_import_intent" if mask else "import_intent")
    asset = await _json(client, "POST", f"/api/projects/{project_id}/import-upload",
        params={"kind": "image"}, files={"file": (("mask." if mask else "reference.") + extension,
            content, "image/jpeg" if extension == "jpg" else "image/" + extension)})
    if asset.get("project_id") != project_id or asset.get("kind") != "image":
        raise AdapterError("Prospero reference belongs to an unexpected project or type")
    reference = _identifier(asset.get("id"))
    if mask and reference == row["input_asset_id"]:
        raise AdapterError("Prospero returned the source identifier for the mask")
    _update(key, **({"mask_asset_id": reference} if mask else {"input_asset_id": reference}))
    return reference


async def _prepare(client, key, scope, prompt, content, *, operation="generate", mask_bytes=None, strength=None):
    row = _read(key)
    _authorize(row["owner"] or None, row["session_id"] or None)
    project_id = await _project(client, scope)
    _update(key, project_id=project_id)
    _authorize(row["owner"] or None, row["session_id"] or None)
    payload = {"prompt": prompt, "count": 1, "wait_s": 0}
    if content is not None:
        reference = await _import_reference(client, key, project_id, content)
        payload["reference_asset_id"] = reference
    path = f"/api/projects/{project_id}/generate"
    if operation == "inpaint":
        mask_id = await _import_reference(client, key, project_id, mask_bytes, mask=True)
        payload.pop("reference_asset_id")
        payload.update(operation="inpaint", asset_id=reference, mask_asset_id=mask_id)
        if strength is not None:
            payload["strength"] = strength
        path = f"/api/assets/{reference}/edit"
    elif operation == "img2img":
        # Harmonize: the studio's existing SDXL img2img edit of the imported
        # source; `strength` is the fraction redrawn (denoise).
        payload.pop("reference_asset_id")
        payload.update(operation="img2img", asset_id=reference)
        if strength is not None:
            payload["strength"] = strength
        path = f"/api/assets/{reference}/edit"
    # Persist and fsync the intent before the non-idempotent job POST.
    _authorize(row["owner"] or None, row["session_id"] or None)
    _update(key, state="submit_intent")
    generated = await _json(client, "POST", path, json=payload)
    job = generated.get("job") or {}
    if job.get("project_id") != project_id:
        raise AdapterError("Prospero job belongs to an unexpected project")
    _update(key, job_id=_identifier(job.get("id")), state="waiting")


async def _download(client, asset_id):
    content = bytearray()
    async with client.stream("GET", f"/api/assets/{asset_id}/file") as response:
        if response.status_code != 200 or response.headers.get("content-type", "").split(";")[0] != "image/png":
            raise AdapterError("Prospero output is not a PNG image")
        async for chunk in response.aiter_bytes():
            content.extend(chunk)
            if len(content) > MAX_IMAGE_BYTES:
                raise AdapterError("Prospero output exceeds the allowed size")
    return bytes(content)


def _record_output(key, asset_id):
    conn = _db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM requests WHERE key=?", (key,)).fetchone()
        if row["asset_id"] and row["asset_id"] != asset_id:
            raise AdapterError("Prospero output changed after it was recorded")
        gallery_id = row["gallery_id"] or str(uuid.uuid4())
        conn.execute("UPDATE requests SET asset_id=?,gallery_id=?,filename=? WHERE key=?",
                     (asset_id, gallery_id, gallery_id + ".png", key))
        conn.commit()
    finally:
        conn.close()


def _publish(row, content, prompt):
    from src.database import GalleryImage, SessionLocal
    from sqlalchemy.exc import IntegrityError
    width, height, _ = _validate_image(content, png_only=True)
    content_hash = hashlib.sha256(content).hexdigest()

    def matches(image):
        return bool(image and image.is_active and (image.owner or "") == row["owner"]
            and (image.session_id or "") == row["session_id"] and image.filename == row["filename"]
            and image.file_hash == content_hash)

    db = SessionLocal()
    try:
        existing = db.query(GalleryImage).filter(GalleryImage.id == row["gallery_id"]).first()
        if existing and not matches(existing):
            raise AdapterError("Image receipt does not match its gallery owner or bytes")
        directory = Path(GENERATED_IMAGES_DIR)
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / row["filename"]
        temporary = directory / (row["filename"] + "." + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        if not existing:
            db.add(GalleryImage(id=row["gallery_id"], filename=row["filename"], prompt=prompt,
                model="prospero", size=f"{width}x{height}", quality="studio",
                owner=row["owner"] or None, session_id=row["session_id"] or None,
                file_hash=content_hash, file_size=len(content), width=width, height=height))
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                existing = db.query(GalleryImage).filter(GalleryImage.id == row["gallery_id"]).first()
                if not matches(existing):
                    raise
    finally:
        db.close()
    _update(row["key"], state="done")


def _result(row, prompt):
    from src.database import GalleryImage, SessionLocal
    db = SessionLocal()
    try:
        image = db.query(GalleryImage).filter(GalleryImage.id == row["gallery_id"]).first()
        if (not image or not image.is_active or (image.owner or "") != row["owner"]
                or (image.session_id or "") != row["session_id"]
                or image.filename != row["filename"] or not (Path(GENERATED_IMAGES_DIR) / row["filename"]).is_file()):
            raise AdapterError("The recorded Prospero image is no longer available in this gallery")
        size = image.size
    finally:
        db.close()
    return {"results": f"Generated image for: {prompt[:100]}\nGallery image ID: {row['gallery_id']}\nImage request ID: {json.dumps(row['request_id'])}",
        "image_url": "/api/generated-image/" + row["filename"], "image_id": row["gallery_id"],
        "image_prompt": prompt, "image_model": "prospero", "image_quality": "studio", "image_size": size,
        "request_id": row["request_id"], "prospero_job_id": row["job_id"],
        "prospero_project_id": row["project_id"], "prospero_asset_id": row["asset_id"]}


def _unknown(request_id, message, *, job_id=None, state="unknown"):
    return {"error": message + "\nImage request ID: " + json.dumps(request_id) + "; check it with image_job instead of generating again.", "state": state, "request_id": request_id,
        "result_status": "outcome_unknown", "status": "outcome_unknown",
        "uncertainty": {"reason": "Prospero may have accepted or completed the image work; its final result is unconfirmed.",
            "reconcile_action": "Resume with the same request_id and inspect the existing job; do not submit another generation."},
        **({"prospero_job_id": job_id} if job_id else {})}


async def _poll(client, key, prompt, *, poll_timeout=None):
    deadline = time.monotonic() + (POLL_TIMEOUT_S if poll_timeout is None else poll_timeout)
    while True:
        row = _read(key)
        _authorize(row["owner"] or None, row["session_id"] or None)
        job = await _json(client, "GET", f"/api/jobs/{_identifier(row['job_id'])}")
        _authorize(row["owner"] or None, row["session_id"] or None)
        if job.get("id") != row["job_id"] or job.get("project_id") != row["project_id"]:
            raise AdapterError("Prospero job identity does not match this image request")
        state = job.get("state")
        if state == "done":
            outputs = job.get("outputs") or {}
            ids = outputs.get("asset_ids") or ([outputs["asset_id"]] if outputs.get("asset_id") else [])
            if not isinstance(ids, list) or len(ids) != 1:
                raise AdapterError("Prospero did not return exactly one image")
            asset_id = _identifier(ids[0])
            asset = await _json(client, "GET", f"/api/assets/{asset_id}")
            if (asset.get("id") != asset_id or asset.get("project_id") != row["project_id"]
                    or asset.get("kind") != "image"):
                raise AdapterError("Prospero output belongs to an unexpected project or type")
            if row["asset_id"] and row["asset_id"] != asset_id:
                raise AdapterError("Prospero output changed after it was recorded")
            _record_output(key, asset_id)
            _authorize(row["owner"] or None, row["session_id"] or None)
            content = await _download(client, asset_id)
            _authorize(row["owner"] or None, row["session_id"] or None)
            _publish(_read(key), content, prompt)
            return _result(_read(key), prompt)
        if state in ("failed", "cancelled"):
            if state == "failed" and str(job.get("message") or "").startswith("outcome_unknown"):
                _update(key, state="unknown")
                return _unknown(row["request_id"], "Prospero could not confirm the submitted image outcome", job_id=row["job_id"])
            _update(key, state=state)
            raise AdapterError("Prospero image job " + state)
        if state not in ("queued", "waiting_gpu", "running"):
            raise AdapterError("Prospero returned an unknown job state")
        if time.monotonic() >= deadline:
            return _unknown(row["request_id"], "Prospero image is still pending; resume with the same request ID",
                state="pending", job_id=row["job_id"])
        await asyncio.sleep(min(POLL_INTERVAL_S, max(0, deadline - time.monotonic())))


async def run_image(prompt: str, session_id: str | None, owner: str | None,
                    image_path: str | None = None, *, request_id: str | None = None,
                    image_bytes: bytes | None = None, operation: str = "generate",
                    mask_bytes: bytes | None = None, strength: float | None = None) -> dict:
    """Generate/edit one owned image; stable request IDs recover without resubmit."""
    key = None
    request_id = str(uuid.uuid4()) if request_id is None else request_id
    try:
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 20_000:
            raise AdapterError("A bounded image prompt is required")
        if not isinstance(request_id, str) or not request_id or len(request_id) > 256:
            raise AdapterError("Invalid image request ID")
        if (owner is not None and not isinstance(owner, str)) or (session_id is not None and not isinstance(session_id, str)):
            raise AdapterError("Invalid image owner or session")
        from src.owner_identity import effective_storage_owner
        owner = effective_storage_owner(owner)
        _authorize(owner, session_id)
        if image_path is not None and image_bytes is not None:
            raise AdapterError("Supply either image bytes or an image path")
        content = image_bytes
        if image_path is not None:
            path = Path(image_path)
            if path.stat().st_size > MAX_IMAGE_BYTES:
                raise AdapterError("Reference image exceeds the allowed size")
            with path.open("rb") as stream:
                content = stream.read(MAX_IMAGE_BYTES + 1)
        if content is not None:
            if not isinstance(content, bytes):
                raise AdapterError("Reference image must be bytes")
            _validate_image(content)
        if operation not in ("generate", "inpaint", "img2img"):
            raise AdapterError("Unsupported image operation")
        if operation == "inpaint":
            if content is None or not isinstance(mask_bytes, bytes):
                raise AdapterError("Inpaint requires source image bytes and mask bytes")
            if _validate_image(content)[:2] != _validate_image(mask_bytes, png_only=True)[:2]:
                raise AdapterError("Inpaint source and mask dimensions must match")
            if strength is not None and (isinstance(strength, bool) or not isinstance(strength, (int, float))
                    or not 0 <= strength <= 1 or not math.isfinite(strength)):
                raise AdapterError("Inpaint strength must be a finite number between 0 and 1")
        elif operation == "img2img":
            if content is None or mask_bytes is not None:
                raise AdapterError("img2img requires source image bytes and no mask")
            if strength is not None and (isinstance(strength, bool) or not isinstance(strength, (int, float))
                    or not 0 <= strength <= 1 or not math.isfinite(strength)):
                raise AdapterError("img2img strength must be a finite number between 0 and 1")
        elif mask_bytes is not None or strength is not None:
            raise AdapterError("Mask and strength are only supported for inpaint")
        connector_id, origin = _connection(owner)
        key = _digest(owner or "", session_id or "", request_id)
        fingerprint = _digest(prompt, hashlib.sha256(content).hexdigest() if content is not None else None)
        if operation == "inpaint":
            fingerprint = _digest(prompt, hashlib.sha256(content).hexdigest(), operation,
                hashlib.sha256(mask_bytes).hexdigest(), float(strength) if strength is not None else None)
        elif operation == "img2img":
            fingerprint = _digest(prompt, hashlib.sha256(content).hexdigest(), operation,
                float(strength) if strength is not None else None)
        created = _reserve(key, request_id, owner, session_id, connector_id, origin, fingerprint, prompt, operation)
        row = _read(key)
        if not created and row["state"] == "done":
            return _result(row, prompt)
        if not created and not row["job_id"]:
            return _unknown(request_id, "Prospero submission outcome is unresolved; it will not be repeated")
        if row["state"] in ("failed", "cancelled"):
            return {"error": "Prospero image job " + row["state"], "state": row["state"], "request_id": request_id}
        async with httpx.AsyncClient(base_url=origin, follow_redirects=False,
                timeout=httpx.Timeout(30, read=60), trust_env=False) as client:
            health = await _json(client, "GET", "/api/health")
            if health.get("service") != "prosperos-hoard":
                raise AdapterError("The connection did not identify itself as Prospero")
            if created:
                try:
                    await _prepare(client, key, _digest(owner or "", session_id or "", connector_id, origin), prompt, content,
                        operation=operation, mask_bytes=mask_bytes, strength=strength)
                except BaseException:
                    _update(key, state="unknown")
                    raise
            return await _poll(client, key, prompt)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        try:
            row = _read(key) if key else None
        except Exception:
            return _unknown(request_id, "The local image receipt could not be read; do not resubmit")
        message = str(exc) if isinstance(exc, AdapterError) else "Prospero image operation failed"
        if row and row["state"] in ("preparing", "submit_intent", "import_intent", "mask_import_intent", "unknown", "waiting"):
            return _unknown(request_id, message, job_id=row["job_id"], state="pending" if row["job_id"] else "unknown")
        return {"error": message,
            "state": "unknown" if row and not row["job_id"] else "pending" if row and row["state"] == "waiting" else "failed",
            "request_id": request_id,
            **({"prospero_job_id": row["job_id"]} if row and row["job_id"] else {})}


async def resume_image(request_id: str, session_id: str | None, owner: str | None) -> dict:
    """Inspect one owned receipt once; never create, import or submit work."""
    key = None
    try:
        if not isinstance(request_id, str) or not request_id or len(request_id) > 256:
            raise AdapterError("Invalid image request ID")
        if (owner is not None and not isinstance(owner, str)) or (session_id is not None and not isinstance(session_id, str)):
            raise AdapterError("Invalid image owner or session")
        from src.owner_identity import effective_storage_owner
        owner = effective_storage_owner(owner)
        _authorize(owner, session_id)
        connector_id, origin = _connection(owner)
        key = _digest(owner or "", session_id or "", request_id)
        row = _read(key)
        if not row or row["owner"] != (owner or "") or row["session_id"] != (session_id or ""):
            raise AdapterError("No image request exists for this account and session")
        if row["connector_id"] != connector_id or row["origin"] != origin:
            raise AdapterError("The image request is bound to a different Prospero connection")
        if row["state"] == "done":
            return _result(row, row["prompt"] or "")
        if not row["job_id"]:
            return _unknown(request_id, "Prospero submission outcome is unresolved; it will not be repeated")
        if row["state"] in ("failed", "cancelled"):
            return {"error": "Prospero image job " + row["state"], "state": row["state"], "request_id": request_id}
        if row["prompt"] is None:
            raise AdapterError("This legacy image receipt needs its original invocation to restore the prompt")
        async with httpx.AsyncClient(base_url=origin, follow_redirects=False,
                timeout=httpx.Timeout(30, read=60), trust_env=False) as client:
            health = await _json(client, "GET", "/api/health")
            if health.get("service") != "prosperos-hoard":
                raise AdapterError("The connection did not identify itself as Prospero")
            return await _poll(client, key, row["prompt"], poll_timeout=0)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # Read-side failures must not turn an accepted job into retryable work.
        try:
            row = _read(key) if key else None
        except Exception:
            return _unknown(request_id, "The local image receipt could not be read; do not resubmit")
        message = str(exc) if isinstance(exc, AdapterError) else "Prospero image status could not be read"
        if row and row["job_id"] and row["state"] not in ("done", "failed", "cancelled"):
            return _unknown(request_id, message, job_id=row["job_id"])
        # A job observed as cancelled reports that, not a generic failure.
        return {"error": message, "state": "cancelled" if row and row["state"] == "cancelled" else "failed",
                "request_id": request_id}


async def cancel_image(request_id: str, session_id: str | None, owner: str | None) -> dict:
    """Cancel one owned, submitted image job; never creates or repeats work.

    Queued work is cancelled at once; a running render is flagged and stops at
    the studio's next checkpoint. A job that already finished is collected
    instead, so the produced image is never lost to a late cancel.
    """
    key = None
    try:
        if not isinstance(request_id, str) or not request_id or len(request_id) > 256:
            raise AdapterError("Invalid image request ID")
        if (owner is not None and not isinstance(owner, str)) or (session_id is not None and not isinstance(session_id, str)):
            raise AdapterError("Invalid image owner or session")
        from src.owner_identity import effective_storage_owner
        owner = effective_storage_owner(owner)
        _authorize(owner, session_id)
        connector_id, origin = _connection(owner)
        key = _digest(owner or "", session_id or "", request_id)
        row = _read(key)
        if not row or row["owner"] != (owner or "") or row["session_id"] != (session_id or ""):
            raise AdapterError("No image request exists for this account and session")
        if row["connector_id"] != connector_id or row["origin"] != origin:
            raise AdapterError("The image request is bound to a different Prospero connection")
        if row["state"] == "done":
            return {**_result(row, row["prompt"] or ""), "cancel": "already_done"}
        if row["state"] in ("failed", "cancelled"):
            return {"output": "Image request already " + row["state"], "state": row["state"],
                    "request_id": request_id, "exit_code": 0}
        if not row["job_id"]:
            return _unknown(request_id, "Prospero submission outcome is unresolved; there is no job to cancel")
        async with httpx.AsyncClient(base_url=origin, follow_redirects=False,
                timeout=httpx.Timeout(30, read=60), trust_env=False) as client:
            health = await _json(client, "GET", "/api/health")
            if health.get("service") != "prosperos-hoard":
                raise AdapterError("The connection did not identify itself as Prospero")
            _authorize(owner, session_id)
            job = await _json(client, "POST", f"/api/jobs/{_identifier(row['job_id'])}/cancel")
            if job.get("id") != row["job_id"] or job.get("project_id") != row["project_id"]:
                raise AdapterError("Prospero job identity does not match this image request")
            state = job.get("state")
            if state == "cancelled":
                _update(key, state="cancelled")
                return {"output": "Image request cancelled; no image was produced.", "state": "cancelled",
                        "request_id": request_id, "prospero_job_id": row["job_id"], "exit_code": 0}
            if state in ("done", "failed"):
                # Finished before the cancel landed: collect or record it.
                return await _poll(client, key, row["prompt"] or "", poll_timeout=0)
            if state == "running":
                return {"output": "Cancellation requested; the running render stops at its next checkpoint. "
                                  "Check it with image_job.", "state": "cancel_requested",
                        "request_id": request_id, "prospero_job_id": row["job_id"], "exit_code": 0}
            raise AdapterError("Prospero returned an unknown job state")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        try:
            row = _read(key) if key else None
        except Exception:
            row = None
        message = str(exc) if isinstance(exc, AdapterError) else "Prospero image cancellation failed"
        return {"error": message, "state": row["state"] if row else "failed", "request_id": request_id,
                **({"prospero_job_id": row["job_id"]} if row and row["job_id"] else {})}


def _waiting_receipts(limit):
    if not (Path(DATA_DIR) / "prospero_images.db").is_file():
        return []
    conn = _db()
    try:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM requests WHERE state='waiting' AND job_id IS NOT NULL AND prompt IS NOT NULL"
            " ORDER BY rowid LIMIT ?", (int(limit),))]
    finally:
        conn.close()


async def reconcile_pending_images(*, poll_timeout: float | None = None, limit: int = 50) -> dict:
    """Collect submitted jobs a previous process left waiting (run at startup).

    Only receipts with a recorded studio job are polled, under their own owner,
    session and connection; nothing is created, imported or resubmitted, and a
    receipt whose connection changed is left for its owner to inspect.
    """
    summary = {"checked": 0, "done": 0, "pending": 0, "closed": 0, "skipped": 0}
    try:
        rows = _waiting_receipts(limit)
    except Exception:
        return {**summary, "error": "image receipts could not be read"}
    for row in rows:
        summary["checked"] += 1
        try:
            owner = row["owner"] or None
            _authorize(owner, row["session_id"] or None)
            connector_id, origin = _connection(owner)
            if row["connector_id"] != connector_id or row["origin"] != origin:
                summary["skipped"] += 1
                continue
            async with httpx.AsyncClient(base_url=origin, follow_redirects=False,
                    timeout=httpx.Timeout(30, read=60), trust_env=False) as client:
                health = await _json(client, "GET", "/api/health")
                if health.get("service") != "prosperos-hoard":
                    raise AdapterError("The connection did not identify itself as Prospero")
                result = await _poll(client, row["key"], row["prompt"],
                    poll_timeout=POLL_TIMEOUT_S if poll_timeout is None else poll_timeout)
            if result.get("image_url"):
                summary["done"] += 1
            elif result.get("state") == "unknown":
                summary["closed"] += 1
            else:
                summary["pending"] += 1
        except asyncio.CancelledError:
            raise
        except Exception:
            try:
                state = (_read(row["key"]) or {}).get("state")
            except Exception:
                state = None
            summary["closed" if state in ("failed", "cancelled") else "skipped"] += 1
    return summary
