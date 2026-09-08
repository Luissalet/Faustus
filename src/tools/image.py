"""Owner-scoped gallery edits through the same image routes as the editor."""
import base64
import io
import math
from pathlib import Path
from typing import Dict, Optional

from src.tools._common import _parse_tool_args, _internal_headers

_ROUTES = {"upscale": "upscale-local", "rembg": "remove-bg", "inpaint": "inpaint", "harmonize": "harmonize"}
_MAX_BYTES = 20 * 1024 * 1024


def _png(raw: bytes):
    from PIL import Image
    if not raw or len(raw) > _MAX_BYTES:
        raise ValueError("Image is empty or exceeds 20 MiB")
    with Image.open(io.BytesIO(raw)) as image:
        if image.width * image.height > 16_777_216:
            raise ValueError("Image exceeds 16 megapixels")
        size = image.size
        out = io.BytesIO()
        image.convert("RGBA").save(out, format="PNG")
    if out.tell() > _MAX_BYTES:
        raise ValueError("Decoded image exceeds 20 MiB")
    return out.getvalue(), size


def _owned_image(image_id: str, owner: str):
    from core.database import GalleryImage, SessionLocal
    from src.constants import GENERATED_IMAGES_DIR
    with SessionLocal() as db:
        image = db.query(GalleryImage).filter_by(id=image_id, owner=owner, is_active=True).first()
        if not image:
            raise ValueError("Gallery image not found for this owner")
        root = Path(GENERATED_IMAGES_DIR).resolve()
        path = (root / image.filename).resolve()
        if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size > _MAX_BYTES:
            raise ValueError("Gallery image file is unavailable or too large")
        return _png(path.read_bytes())


async def do_edit_image(content: str, owner: Optional[str] = None) -> Dict:
    import httpx
    from src.owner_identity import effective_storage_owner
    from src.tool_implementations import _INTERNAL_BASE
    try:
        args = _parse_tool_args(content)
        owner = effective_storage_owner(owner)
        if not owner:
            raise ValueError("An authenticated owner is required to edit gallery images")
        action = args.get("action")
        if not isinstance(action, str) or action not in _ROUTES:
            raise ValueError("Choose upscale, rembg, inpaint or harmonize")
        image_id = args.get("image_id")
        if not isinstance(image_id, str) or not image_id:
            raise ValueError("image_id must be a gallery image ID")
        source, (width, height) = _owned_image(image_id, owner)
        payload = {"image": base64.b64encode(source).decode(), "width": width, "height": height}
        prompt = args.get("prompt", "")
        if not isinstance(prompt, str) or len(prompt) > 20_000:
            raise ValueError("prompt must be text up to 20000 characters")
        payload["prompt"] = prompt
        if action == "inpaint":
            mask_id = args.get("mask_id")
            if not isinstance(mask_id, str) or not mask_id or not prompt.strip():
                raise ValueError("Inpaint requires prompt and mask_id: a same-size gallery mask, white to redraw and black to keep")
            mask, mask_size = _owned_image(mask_id, owner)
            if mask_size != (width, height):
                raise ValueError("Mask and source dimensions must match")
            payload["mask"] = base64.b64encode(mask).decode()
        if action == "upscale":
            scale = args.get("scale", 2)
            if isinstance(scale, bool) or scale not in (2, 4):
                raise ValueError("Upscale supports scale 2 or 4")
            if width * height * scale * scale > 16_777_216:
                raise ValueError("Upscaled output would exceed 16 megapixels")
            payload["scale"] = scale
        if action in {"inpaint", "harmonize"}:
            strength = args.get("strength", 0.75 if action == "inpaint" else 0.4)
            if isinstance(strength, bool) or not isinstance(strength, (int, float)) or not math.isfinite(strength) or not 0 <= strength <= 1:
                raise ValueError("strength must be between 0 and 1")
            payload["strength"] = strength
        headers = _internal_headers(owner)
        async with httpx.AsyncClient(timeout=180, follow_redirects=False) as client:
            response = await client.post(f"{_INTERNAL_BASE}/api/image/{_ROUTES[action]}", json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
            if data.get("error"):
                raise ValueError(str(data["error"]))
            encoded = data.get("image")
            if not isinstance(encoded, str) or len(encoded) > _MAX_BYTES * 4 // 3 + 4:
                raise ValueError("Image service did not return a bounded image")
            edited, _ = _png(base64.b64decode(encoded, validate=True))
            saved = await client.post(f"{_INTERNAL_BASE}/api/gallery/upload", files={"file": ("edited.png", edited, "image/png")}, headers=headers)
            saved.raise_for_status()
            result = saved.json()
        new_id, filename = result.get("id"), result.get("filename")
        if not isinstance(new_id, str) or not new_id or not isinstance(filename, str) or not filename or Path(filename).name != filename:
            raise ValueError("Gallery did not confirm the edited image")
        from urllib.parse import quote
        return {"output": f"Image edited ({action}). Image ID: {new_id}", "exit_code": 0,
                "image_id": new_id, "image_url": f"/api/generated-image/{quote(filename, safe='')}",
                "image_prompt": prompt or action, "image_model": "edit_image", "source_image_id": image_id}
    except httpx.HTTPStatusError as exc:
        return {"error": f"Image edit service returned HTTP {exc.response.status_code}; check image permissions and the configured image service", "exit_code": 1}
    except Exception as exc:
        return {"error": str(exc), "exit_code": 1}
