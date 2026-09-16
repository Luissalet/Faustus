"""Disk-backed overflow for mid-turn context spill.

When an agent turn piles up large tool results, the live prompt keeps a short
stub and the full text lives under ``DATA_DIR/context_overflow/<session>/``.
Blobs are content-addressed so repeated identical dumps share one file.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Dict, Optional, Set

from src.constants import CONTEXT_OVERFLOW_DIR
from src.contracts.base import now_iso
from src.settings import get_setting

logger = logging.getLogger(__name__)

OVERFLOW_DIR = CONTEXT_OVERFLOW_DIR
_SCHEMA_VERSION = 1
_SAFE_SESSION = re.compile(r"[^A-Za-z0-9._-]+")


class OverflowDisabled(Exception):
    """Raised when a durable spill was requested but persistence is off."""


def keep_hours() -> float:
    try:
        return float(get_setting("agent_context_overflow_keep_hours", 48) or 48)
    except Exception:  # noqa: BLE001
        return 48.0


def _session_dir(session_id: str) -> str:
    safe = _SAFE_SESSION.sub("-", (session_id or "session").strip())[:96] or "session"
    return os.path.join(OVERFLOW_DIR, safe)


def _blob_path(session_id: str, content_sha256: str) -> str:
    return os.path.join(_session_dir(session_id), f"{content_sha256}.json")


def persist(
    *,
    session_id: str,
    content: str,
    tool: str = "",
    call_id: str = "",
    role: str = "tool",
    run_id: str = "",
    round_num: int = 0,
    durable: bool = True,
) -> Dict[str, Any]:
    """Write ``content`` to the overflow store. Returns a small record.

    When ``durable`` is False (incognito / no_memory), raises
    ``OverflowDisabled`` — callers still build an in-memory stub without a file.
    """
    import hashlib

    text = content if isinstance(content, str) else str(content or "")
    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
    record = {
        "schema_version": _SCHEMA_VERSION,
        "session_id": session_id or "",
        "run_id": run_id or "",
        "round": int(round_num or 0),
        "tool": tool or "",
        "call_id": call_id or "",
        "role": role or "tool",
        "content": text,
        "created_at": now_iso(),
        "content_sha256": digest,
        "bytes": len(text.encode("utf-8", "replace")),
    }
    if not durable:
        raise OverflowDisabled("durable overflow disabled for this turn")

    path = _blob_path(session_id, digest)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.isfile(path):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(record, fh, ensure_ascii=False)
        os.replace(tmp, path)
    return {
        "content_sha256": digest,
        "bytes": record["bytes"],
        "path": path,
        "tool": record["tool"],
        "call_id": record["call_id"],
    }


def load(*, session_id: str, content_sha256: str) -> Optional[str]:
    path = _blob_path(session_id, content_sha256)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        content = data.get("content")
        return content if isinstance(content, str) else None
    except Exception as e:  # noqa: BLE001
        logger.debug("context_overflow.load failed: %s", e)
        return None


def prune(*, session_id: str, referenced_ids: Optional[Set[str]] = None) -> int:
    """Remove aged blobs not referenced by the live prompt. Returns count removed."""
    referenced_ids = referenced_ids or set()
    root = _session_dir(session_id)
    if not os.path.isdir(root):
        return 0
    cutoff = time.time() - max(1.0, keep_hours()) * 3600.0
    removed = 0
    for name in os.listdir(root):
        if not name.endswith(".json"):
            continue
        digest = name[:-5]
        path = os.path.join(root, name)
        if digest in referenced_ids:
            continue
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if mtime > cutoff:
            continue
        try:
            os.remove(path)
            removed += 1
        except OSError as e:
            logger.debug("context_overflow.prune skip %s: %s", path, e)
    return removed


def referenced_overflow_ids(messages) -> Set[str]:
    """Sha256 ids mentioned in in-prompt overflow stubs."""
    found: Set[str] = set()
    pattern = re.compile(r"\[overflow id=([0-9a-f]{64})\b")
    for msg in messages or ():
        if not isinstance(msg, dict):
            continue
        text = msg.get("content")
        if not isinstance(text, str):
            continue
        found.update(pattern.findall(text))
    return found
