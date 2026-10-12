"""Local OCR of screen images: on-screen text with pixel boxes, no model needed.

Text-only models (GLM on the Sparks) cannot look at a screenshot, and apps
that draw their own UI (Blender, games, OpenGL/Qt canvases) expose no UIA
controls. Reading the text on the pixels gives those models labels and
coordinates to act on: `desktop_click {"text": ...}` and the OCR listing
attached to a screenshot for a model that cannot see it.
"""
from __future__ import annotations

import base64
import io
import logging
import re
import threading
from difflib import SequenceMatcher
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

MATCH_MIN = 0.8
_ENGINE = None
_UNAVAILABLE = ""
_LOCK = threading.Lock()


def _engine():
    global _ENGINE, _UNAVAILABLE
    if _ENGINE is not None or _UNAVAILABLE:
        return _ENGINE
    with _LOCK:
        if _ENGINE is None and not _UNAVAILABLE:
            try:
                from rapidocr_onnxruntime import RapidOCR
                _ENGINE = RapidOCR()
            except Exception as exc:  # noqa: BLE001 - optional dependency
                _UNAVAILABLE = f"{type(exc).__name__}: {exc}"
                logger.info("screen OCR unavailable: %s", _UNAVAILABLE)
    return _ENGINE


def available() -> bool:
    return _engine() is not None


def unavailable_reason() -> str:
    _engine()
    return _UNAVAILABLE


def read_text(img, *, upscale_below: int = 1000) -> List[Dict[str, Any]]:
    """Text lines of a PIL image: [{text, score, box, center}] in its pixels."""
    engine = _engine()
    if engine is None or img is None:
        return []
    import numpy as np
    from PIL import Image

    img = img.convert("RGB")
    factor = 1.0
    if max(img.size) < upscale_below:
        factor = 2.0
        img = img.resize((img.width * 2, img.height * 2), Image.LANCZOS)
    with _LOCK:
        found, _elapsed = engine(np.array(img))
    lines: List[Dict[str, Any]] = []
    for box, text, score in found or []:
        text = str(text or "").strip()
        if not text:
            continue
        xs = [float(p[0]) / factor for p in box]
        ys = [float(p[1]) / factor for p in box]
        x0, y0, x1, y1 = int(min(xs)), int(min(ys)), int(round(max(xs))), int(round(max(ys)))
        lines.append({"text": text, "score": round(float(score), 3), "box": [x0, y0, x1, y1],
                      "center": [(x0 + x1) // 2, (y0 + y1) // 2]})
    lines.sort(key=lambda line: (line["center"][1] // 8, line["center"][0]))
    return lines


def _norm(text: str) -> str:
    text = re.sub(r"['\u2019`\u00b4]", "", str(text or "").lower())
    return re.sub(r"[\W_]+", " ", text).strip()


def match_score(query: str, text: str) -> float:
    q, t = _norm(query), _norm(text)
    if not q or not t:
        return 0.0
    if q == t:
        return 1.0
    if q.replace(" ", "") == t.replace(" ", ""):
        return 0.98
    if q in t:
        return 0.85 + 0.1 * len(q) / len(t)
    return SequenceMatcher(None, q, t).ratio()


def format_lines(lines: List[Dict[str, Any]], limit: int = 250) -> str:
    rows = [f'- "{line["text"]}" at ({line["center"][0]}, {line["center"][1]})' for line in lines[:limit]]
    if len(lines) > limit:
        rows.append(f"- ... {len(lines) - limit} more lines not shown")
    return "\n".join(rows)


def ocr_result_image(result: Any) -> str:
    """OCR listing of the first image of a tool result, in that image's pixels."""
    if not available():
        return ""
    try:
        from PIL import Image
        from src.tool_images import normalize_result_images

        images = normalize_result_images(result)
        if not images:
            return ""
        raw = base64.b64decode(images[0]["data"])
        return format_lines(read_text(Image.open(io.BytesIO(raw))))
    except Exception as exc:  # noqa: BLE001 - OCR never breaks a round
        logger.debug("screen OCR of tool image failed: %s", exc)
        return ""