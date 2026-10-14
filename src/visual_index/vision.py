"""The resident vision model, as the visual index and "look at the page" use it.

Nothing here loads a model: ``vision_ready`` asks the same router every other vision caller uses
(``src.vision_routing.resolve_vision_route``) whether a vision-capable model answers right now, and ``ask`` sends one
picture and one question through ``src.document_processor.analyze_image_with_vl_prompt`` (privacy gate, fallbacks and the
answer cache included). When no vision model is available the callers say so; they never describe a picture themselves.
"""
from __future__ import annotations

import io
from typing import Any, Dict, Optional, Tuple

from PIL import Image


def vision_ready(owner: Optional[str]) -> Tuple[bool, str, str]:
    """``(available, why_not_or_note, model)`` for the vision model that would answer for ``owner``."""
    try:
        from src.settings import get_setting

        if not get_setting("vision_enabled", True):
            return False, "vision is switched off (Settings > Vision)", ""
    except Exception:  # noqa: BLE001
        pass
    try:
        from src.vision_routing import resolve_vision_route

        route = resolve_vision_route(owner=owner, configured="")
    except Exception as exc:  # noqa: BLE001
        return False, f"the vision router failed ({str(exc)[:120]})", ""
    model = str(route.get("model") or "")
    if not model or not route.get("url"):
        return False, str(route.get("error") or "no vision model is configured or running"), ""
    return True, "", model


def png_bytes(image: Image.Image) -> bytes:
    buf = io.BytesIO()
    image.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


def ask(image: Image.Image, prompt: str, owner: Optional[str] = None) -> Dict[str, Any]:
    """``{"text", "model"}``; an empty ``model`` means no vision model answered and ``text`` says why."""
    from src.document_processor import analyze_image_with_vl_prompt

    return analyze_image_with_vl_prompt([(png_bytes(image), "image/png")], prompt, owner, None)
