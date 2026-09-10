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

import hashlib
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


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8", "replace")).hexdigest()


def decide(message_id: Any, path: str, decision: str, *, content: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Move `path` from pending to accepted/rejected. Returns the entry (None if unknown).

    `content` is optional and additive (existing 3-positional-arg callers are
    unaffected): the file's text at the moment of an "accept" decision. When
    given, its sha256 is kept as the approval's signature so a later edit can
    be told apart from the version a human actually looked at
    (`approval_still_valid`).

    VER-06: a human accepting a diff is a DIFFERENT fact from an automatic
    verification passing, and this function only ever writes a key called
    `human_approved` — never `verified`. Nothing in this module decides
    whether tests passed; nothing that decides whether tests passed writes
    here. A caller that wants "was this both tested green AND approved by a
    person" reads both facts and combines them itself.
    """
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
        approvals = entry.setdefault("human_approved", {})
        if decision == "accept":
            approvals[norm] = {
                "at": entry["updated"],
                "content_sha256": _sha256(content) if content is not None else None,
            }
        else:
            approvals.pop(norm, None)
        data[key] = entry
        _save(data)
        return entry


def is_human_approved(entry: Dict[str, Any], path: str) -> bool:
    """True if `path` was accepted by a human in this review entry.

    Deliberately independent of any automatic verification state (VER-06):
    this reads only what `decide(..., "accept")` recorded, never a test
    result or a scorecard verdict."""
    return str(path) in (entry.get("human_approved") or {})


def approval_still_valid(entry: Dict[str, Any], path: str, current_content: Optional[str]) -> bool:
    """False when `path` was approved WITH a recorded signature and
    `current_content` no longer matches it — the diff moved on after a human
    signed off on a specific version of it.

    True when there is nothing to contradict: no approval on record (nothing
    to invalidate — call `is_human_approved` first if that distinction
    matters to the caller), no signature was captured at accept time (older
    callers that do not pass `content=`), or the caller has no current
    content to compare. This function only ever narrows an approval that
    provably drifted; it never manufactures a "yes" that was not there.
    """
    approval = (entry.get("human_approved") or {}).get(str(path))
    if not approval:
        return True
    signature = approval.get("content_sha256")
    if signature is None or current_content is None:
        return True
    return _sha256(current_content) == signature


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
