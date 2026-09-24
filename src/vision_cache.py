"""A small disk cache of vision-model answers.

A text-only model working from images asks a separate vision model, and on
a local machine that model may run on the CPU at minutes per question. Live
(24-09-2026), the same page was described again and again across rounds and
turns: every repeat paid the full price. An answer depends only on the image
bytes, the prompt and the model, so it is cached under a hash of the three.

Entries live in ``<DATA_DIR>/vision_cache/<sha256>.json``; off with
``vision_cache_enabled = false``; entries older than
``vision_cache_max_age_days`` (30 by default) are ignored. Failure notes
(bracketed "[... unavailable ...]" texts) are never stored.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Iterable, Optional, Tuple


def _setting(key: str, default):
    try:
        from src.settings import get_setting
        value = get_setting(key, default)
        return default if value is None else value
    except Exception:  # noqa: BLE001
        return default


def enabled() -> bool:
    return bool(_setting("vision_cache_enabled", True))


def _cache_dir() -> str:
    try:
        from src.constants import DATA_DIR
        base = DATA_DIR
    except Exception:  # noqa: BLE001
        base = os.path.join(os.getcwd(), "data")
    return os.path.join(str(base), "vision_cache")


def key_for(model: str, prompt: str, images: Iterable[Tuple[bytes, str]]) -> str:
    h = hashlib.sha256()
    h.update((model or "").encode("utf-8"))
    h.update(b"\0")
    h.update((prompt or "").encode("utf-8"))
    for raw, mime in images:
        h.update(b"\0")
        h.update((mime or "").encode("utf-8"))
        h.update(hashlib.sha256(raw or b"").digest())
    return h.hexdigest()


def get(key: str) -> Optional[dict]:
    if not enabled():
        return None
    path = os.path.join(_cache_dir(), f"{key}.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            entry = json.load(fh)
    except (OSError, ValueError):
        return None
    max_age = float(_setting("vision_cache_max_age_days", 30) or 30) * 86400
    if time.time() - float(entry.get("at") or 0) > max_age:
        return None
    if not entry.get("text"):
        return None
    return {"text": entry["text"], "model": entry.get("model") or "", "cached": True}


def put(key: str, text: str, model: str) -> None:
    if not enabled() or not text or not model:
        return
    stripped = text.strip()
    if stripped.startswith("[") and "unavailable" in stripped[:80].lower():
        return
    folder = _cache_dir()
    try:
        os.makedirs(folder, exist_ok=True)
        tmp = os.path.join(folder, f"{key}.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"text": text, "model": model, "at": time.time()}, fh, ensure_ascii=False)
        os.replace(tmp, os.path.join(folder, f"{key}.json"))
    except OSError:
        pass
