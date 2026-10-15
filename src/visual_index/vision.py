"""The resident vision model, as the visual index and "look at the page" use it.

Nothing here loads a model: ``vision_ready`` asks the same router every other vision caller uses
(``src.vision_routing.resolve_vision_route``) whether a vision-capable model answers right now, and ``ask`` sends one
picture and one question through ``src.document_processor.analyze_image_with_vl_prompt`` (privacy gate, fallbacks and the
answer cache included). When no vision model is available the callers say so; they never describe a picture themselves.
"""
from __future__ import annotations

import io
import threading
import time
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


_CACHE: Dict[str, Tuple[float, Tuple[bool, str, str]]] = {}
_PENDING: Dict[str, threading.Thread] = {}
_LOCK = threading.Lock()
STATUS_MAX_AGE_S = 30.0
STATUS_WAIT_S = 1.5


def vision_ready_cached(owner: Optional[str], *, max_age: float = STATUS_MAX_AGE_S,
                        wait: float = STATUS_WAIT_S) -> Tuple[Optional[bool], str, str]:
    """``vision_ready`` for a status panel: the vision router can take seconds (it asks every endpoint), so a fresh
    answer is reused for ``max_age`` seconds and a slow check runs in the background. While it runs, after ``wait``
    seconds, the answer is ``(None, "checking", "")``; the next call gets the result. ``ask`` never uses this."""
    key = str(owner or "")
    now = time.monotonic()
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] <= max_age:
            return hit[1]
        worker = _PENDING.get(key)
        if worker is None or not worker.is_alive():
            def run() -> None:
                result = vision_ready(owner)
                with _LOCK:
                    _CACHE[key] = (time.monotonic(), result)
                    _PENDING.pop(key, None)
            worker = threading.Thread(target=run, name="visual-index-vision-check", daemon=True)
            _PENDING[key] = worker
            worker.start()
    worker.join(wait)
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and time.monotonic() - hit[0] <= max_age + wait + 1:
            return hit[1]
    return None, "checking", ""


def clear_vision_cache() -> None:
    with _LOCK:
        _CACHE.clear()


def png_bytes(image: Image.Image) -> bytes:
    buf = io.BytesIO()
    image.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


def ask(image: Image.Image, prompt: str, owner: Optional[str] = None) -> Dict[str, Any]:
    """``{"text", "model"}``; an empty ``model`` means no vision model answered and ``text`` says why."""
    from src.document_processor import analyze_image_with_vl_prompt

    return analyze_image_with_vl_prompt([(png_bytes(image), "image/png")], prompt, owner, None)
