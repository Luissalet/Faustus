"""Images that come back from tools (FAUSTUS).

One small, dependency-light place for the three things every image-bearing
tool result needs, so the agent loop, the desktop tools and the context
budget agree on them:

* ``normalize_result_images``: the wire shape.  An MCP server (Playwright's
  ``browser_take_screenshot``) returns ``{"images": [{"data": <b64>,
  "mimeType": "image/png"}]}`` and the builtin ``desktop_screenshot`` returns
  the same, so one reader serves both.
* ``downscale_b64``: nothing above ``agent_tool_image_max_px`` on its longest
  side goes to the model.  A 2560x1440 PNG screenshot is ~1.5 MB of base64;
  the same frame at 1280 px as JPEG q80 is ~150 KB and reads just as well.
  Pillow is a transitive dependency today, but its absence must not break a
  tool result — every helper degrades to "pass the image through unchanged".
* ``screenshot_data_url``: the ``data:`` URL the chat UI renders under the
  tool bubble (``tool_output.screenshot``), derived from the same payload.
"""

from __future__ import annotations

import base64
import binascii
import io
import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

try:  # settings are optional here so the module imports in bare contexts
    from src.settings import get_setting
except Exception:  # pragma: no cover - defensive
    def get_setting(key: str, default: Any = None) -> Any:  # type: ignore[misc]
        return default


DEFAULT_MAX_PX = 1280
JPEG_QUALITY = 80

_MIME_FORMATS = {
    "image/png": "PNG",
    "image/jpeg": "JPEG",
    "image/jpg": "JPEG",
    "image/webp": "WEBP",
    "image/gif": "GIF",
}


def image_max_px() -> int:
    """``agent_tool_image_max_px`` as a sane positive int (0 = no downscale)."""
    try:
        value = int(get_setting("agent_tool_image_max_px", DEFAULT_MAX_PX) or 0)
    except (TypeError, ValueError):
        value = DEFAULT_MAX_PX
    return max(0, value)


def tool_images_enabled() -> bool:
    try:
        return bool(get_setting("agent_tool_images", True))
    except Exception:  # pragma: no cover - defensive
        return True


def normalize_result_images(result: Any) -> List[Dict[str, str]]:
    """``result["images"]`` as ``[{"data": <bare b64>, "mimeType": <mime>}]``.

    Accepts the MCP shape and a bare ``data:`` URL in ``data``; skips
    anything that is not a dict with non-empty image data.
    """
    if not isinstance(result, dict):
        return []
    raw = result.get("images")
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        data = item.get("data")
        if not isinstance(data, str) or not data.strip():
            continue
        data = data.strip()
        mime = str(item.get("mimeType") or item.get("mime_type") or "").strip().lower()
        if data.startswith("data:"):
            header, _, payload = data.partition(",")
            if not payload:
                continue
            data = payload.strip()
            if not mime:
                mime = header[5:].split(";", 1)[0].strip().lower()
        if not mime:
            mime = "image/png"
        out.append({"data": data, "mimeType": mime})
    return out


def data_url(b64: str, mime: str) -> str:
    return f"data:{mime or 'image/png'};base64,{b64}"


def screenshot_data_url(result: Any) -> str:
    """The data URL the UI shows for a tool result, or ""."""
    if not isinstance(result, dict):
        return ""
    explicit = result.get("screenshot")
    if isinstance(explicit, str) and explicit.startswith("data:image/"):
        return explicit
    images = normalize_result_images(result)
    if images:
        return data_url(images[0]["data"], images[0]["mimeType"])
    return ""


def _open_image(raw: bytes):
    from PIL import Image  # lazy: optional at runtime

    img = Image.open(io.BytesIO(raw))
    img.load()
    return img


def downscale_b64(
    b64: str,
    mime: str,
    max_px: Optional[int] = None,
) -> Tuple[str, str, Dict[str, Any]]:
    """Shrink an image so its longest side is ``max_px``; JPEG q80 when shrunk.

    Returns ``(b64, mime, info)`` where ``info`` carries ``width``/``height``
    of the returned image and ``scale`` (returned / original, 1.0 when
    untouched).  Anything that cannot be decoded — no Pillow, garbage input,
    an unsupported format — is returned unchanged with ``scale`` 1.0: a
    tool result must never fail because of the thumbnailer.
    """
    limit = image_max_px() if max_px is None else max(0, int(max_px))
    info: Dict[str, Any] = {"scale": 1.0, "width": None, "height": None}
    if not isinstance(b64, str) or not b64:
        return b64, mime, info
    try:
        raw = base64.b64decode(b64, validate=False)
        img = _open_image(raw)
    except (binascii.Error, ValueError, OSError, ImportError, Exception) as exc:  # noqa: BLE001
        logger.debug("tool image left untouched (%s)", exc)
        return b64, mime, info
    width, height = img.size
    info["width"], info["height"] = width, height
    if limit <= 0 or max(width, height) <= limit:
        return b64, mime, info
    scale = limit / float(max(width, height))
    new_w = max(1, int(round(width * scale)))
    new_h = max(1, int(round(height * scale)))
    try:
        from PIL import Image

        resample = getattr(Image, "LANCZOS", None) or getattr(Image.Resampling, "LANCZOS")
        resized = img.resize((new_w, new_h), resample)
        if resized.mode not in ("RGB", "L"):
            resized = resized.convert("RGB")
        buf = io.BytesIO()
        resized.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    except Exception as exc:  # noqa: BLE001 - keep the original on any failure
        logger.debug("tool image downscale failed, sending original (%s)", exc)
        return b64, mime, info
    info.update({"scale": scale, "width": new_w, "height": new_h})
    return base64.b64encode(buf.getvalue()).decode("ascii"), "image/jpeg", info


def encode_image(img, *, max_px: Optional[int] = None) -> Tuple[str, str, Dict[str, Any]]:
    """PIL image -> ``(b64, mime, info)`` honouring the same size cap.

    PNG when the frame is within the cap (lossless UI text), JPEG q80 when it
    had to shrink.  ``info["scale"]`` maps returned pixels back to source
    pixels (``source = returned / scale``).
    """
    limit = image_max_px() if max_px is None else max(0, int(max_px))
    width, height = img.size
    scale = 1.0
    out = img
    if limit > 0 and max(width, height) > limit:
        scale = limit / float(max(width, height))
        new_w = max(1, int(round(width * scale)))
        new_h = max(1, int(round(height * scale)))
        from PIL import Image

        resample = getattr(Image, "LANCZOS", None) or getattr(Image.Resampling, "LANCZOS")
        out = img.resize((new_w, new_h), resample)
    buf = io.BytesIO()
    if scale < 1.0:
        if out.mode not in ("RGB", "L"):
            out = out.convert("RGB")
        out.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        mime = "image/jpeg"
    else:
        if out.mode not in ("RGB", "RGBA", "L"):
            out = out.convert("RGB")
        out.save(buf, format="PNG", optimize=True)
        mime = "image/png"
    info = {"scale": scale, "width": out.size[0], "height": out.size[1],
            "source_width": width, "source_height": height}
    return base64.b64encode(buf.getvalue()).decode("ascii"), mime, info


def image_is_blank(img) -> bool:
    """True when every channel is a single value (black/empty capture)."""
    try:
        extrema = img.getextrema()
    except Exception:  # noqa: BLE001
        return False
    if not extrema:
        return False
    if isinstance(extrema[0], (int, float)):
        extrema = (extrema,)
    return all(lo == hi for lo, hi in extrema)
