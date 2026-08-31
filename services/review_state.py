"""review_state.py — "propose → apply" bookkeeping for review mode.

In review mode (a project flag) the agent's edits are applied to disk as usual
— the model needs to see its own changes to keep working coherently — but the
turn ends with every changed file marked *pending*: the user accepts or
rejects each one from the file viewer. Rejecting restores the file from the
turn's checkpoint (src/workspace_checkpoints.py); accepting just records the
decision. This module keeps that state per saved assistant message so the
chips survive a reload.

Storage: DATA_DIR/review_state.json  {message_id: {...}}. Atomic writes, one
process, small. Never raises on read; write errors propagate to the route.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_MAX_ENTRIES = 2000


def _path() -> str:
    try:
        from src.constants import DATA_DIR
    except Exception:  # pragma: no cover
        DATA_DIR = os.path.join(os.getcwd(), "data")
    return os.path.join(DATA_DIR, "review_state.json")


def _load() -> Dict[str, Any]:
    p = _path()
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data: Dict[str, Any]) -> None:
    p = _path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if len(data) > _MAX_ENTRIES:
        oldest = sorted(data.items(), key=lambda kv: kv[1].get("ts", 0))[: len(data) - _MAX_ENTRIES]
        for k, _ in oldest:
            data.pop(k, None)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=0)
    os.replace(tmp, p)


def init(message_id: Any, *, session_id: Optional[str], workspace: str, files: List[str],
         checkpoint: Optional[str]) -> Dict[str, Any]:
    """Register a turn's changed files as pending. Idempotent per message."""
    key = str(message_id)
    with _LOCK:
        data = _load()
        if key in data:
            return data[key]
        entry = {
            "ts": int(time.time()), "session_id": session_id, "workspace": workspace,
            "checkpoint": checkpoint, "pending": [str(f) for f in files if f],
            "accepted": [], "rejected": [], "restored": [],
        }
        data[key] = entry
        _save(data)
        return entry


def get(message_id: Any) -> Optional[Dict[str, Any]]:
    return _load().get(str(message_id))


def decide(message_id: Any, path: str, decision: str) -> Optional[Dict[str, Any]]:
    """Move `path` from pending to accepted/rejected. Returns the entry (None if unknown)."""
    key = str(message_id)
    with _LOCK:
        data = _load()
        entry = data.get(key)
        if not entry:
            return None
        norm = str(path)
        for bucket in ("pending", "accepted", "rejected"):
            entry[bucket] = [p for p in entry.get(bucket, []) if p != norm]
        entry["accepted" if decision == "accept" else "rejected"].append(norm)
        entry["updated"] = int(time.time())
        data[key] = entry
        _save(data)
        return entry


def pending_for_session(session_id: str) -> List[Dict[str, Any]]:
    out = []
    for mid, e in _load().items():
        if e.get("session_id") == session_id and e.get("pending"):
            out.append({"message_id": mid, **e})
    out.sort(key=lambda e: e.get("ts", 0))
    return out


def forget(message_id: Any) -> bool:
    key = str(message_id)
    with _LOCK:
        data = _load()
        if key in data:
            data.pop(key)
            _save(data)
            return True
    return False
