"""project_audit.py — what the agent touched, per project, with a link to the turn.

One JSONL file per project (DATA_DIR/audit/<project_id>.jsonl) — or per
workspace for chats that belong to no project (DATA_DIR/audit/ws-<hash>.jsonl).
Each line is one agent turn that changed files: when, which chat, which saved
message (so the UI can jump to the exact turn), which model, which files, the
harness verdict and the checkpoint the turn can be restored to.

Append-only, best-effort, stdlib only. Never raises.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import uuid
from collections import deque
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
MAX_ENTRIES_READ = 5000

# Rotation. The file is append-only and the project page reads it whole on
# every request (load(), then files_index() reads it again), so an unbounded
# log turns into per-request latency. Past ROTATE_MAX_BYTES the file is
# rewritten with only its newest ROTATE_KEEP_LINES lines — atomically (tmp +
# os.replace), so a crash mid-rotation can never leave a truncated audit trail.
ROTATE_MAX_BYTES = 8 * 1024 * 1024
ROTATE_KEEP_LINES = MAX_ENTRIES_READ


def _dir() -> str:
    try:
        from src.constants import DATA_DIR
    except Exception:  # pragma: no cover
        DATA_DIR = os.path.join(os.getcwd(), "data")
    return os.path.join(DATA_DIR, "audit")


def workspace_key(workspace: str) -> str:
    key = os.path.realpath(os.path.expanduser(workspace)).replace("\\", "/")
    if os.name == "nt":
        key = key.lower()
    return "ws-" + hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()[:16]


def _safe_key(key: str) -> str:
    return "".join(ch for ch in str(key) if ch.isalnum() or ch in "-_")[:80] or "unknown"


def path_for(key: str) -> str:
    return os.path.join(_dir(), _safe_key(key) + ".jsonl")


def _rotate_if_needed(path: str) -> bool:
    """Trim an oversized append-only JSONL to its newest ROTATE_KEEP_LINES
    lines. The caller holds `_LOCK`. Best effort: returns False and leaves the
    file untouched on any error."""
    try:
        if ROTATE_MAX_BYTES <= 0 or os.path.getsize(path) <= ROTATE_MAX_BYTES:
            return False
    except OSError:
        return False
    tmp = f"{path}.tmp.{uuid.uuid4().hex}"
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            tail = deque(f, maxlen=max(1, int(ROTATE_KEEP_LINES)))
        with open(tmp, "w", encoding="utf-8") as out:
            for line in tail:
                out.write(line if line.endswith("\n") else line + "\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
        logger.info("[audit] rotated %s — kept the newest %d entries", os.path.basename(path), len(tail))
        return True
    except (OSError, ValueError) as e:
        logger.debug("[audit] rotation of %s failed: %s", path, e)
        return False
    finally:
        try:
            os.unlink(tmp)          # no-op after a successful os.replace
        except OSError:
            pass


def record(
    key: str,
    *,
    session_id: Optional[str],
    message_id: Optional[Any],
    model: Optional[str],
    files: List[str],
    workspace: Optional[str],
    stop_reason: Optional[str],
    checkpoint: Optional[str] = None,
    user_text: str = "",
    tests: Optional[str] = None,
    review: Optional[str] = None,
    project_id: Optional[str] = None,
    kind: str = "turn",
    extra: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    if not key or not files:
        return None
    entry: Dict[str, Any] = {
        "ts": int(time.time()),
        "kind": kind,
        "session_id": session_id,
        "message_id": message_id,
        "model": model,
        "workspace": workspace,
        "project_id": project_id,
        "files": [str(f) for f in files][:200],
        "stop_reason": stop_reason,
        "checkpoint": checkpoint,
        "request": " ".join((user_text or "").split())[:160],
        "tests": tests,
        "review": review,
    }
    if extra:
        entry.update(extra)
    try:
        os.makedirs(_dir(), exist_ok=True)
        p = path_for(key)
        with _LOCK:
            with open(p, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            # Rotate AFTER the append so the entry we just wrote is always in
            # the retained tail.
            _rotate_if_needed(p)
        return entry
    except (OSError, TypeError, ValueError) as e:
        logger.debug("[audit] write failed: %s", e)
        return None


def load(key: str, limit: int = 200) -> List[Dict[str, Any]]:
    p = path_for(key)
    if not os.path.isfile(p):
        return []
    # deque(maxlen=…) drops the oldest row in O(1). The previous
    # `rows = rows[-MAX_ENTRIES_READ:]` INSIDE the loop copied the whole
    # retained window once per line — quadratic, and the endpoint reads the
    # file twice per request (load + files_index).
    keep = deque(maxlen=MAX_ENTRIES_READ)
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    keep.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    rows: List[Dict[str, Any]] = list(keep)
    rows.reverse()  # newest first
    return rows[: max(1, int(limit))]


def files_index(key: str) -> List[Dict[str, Any]]:
    """Per-file view: every file ever touched, with the number of turns and the
    last time. Newest first."""
    by: Dict[str, Dict[str, Any]] = {}
    for e in load(key, limit=MAX_ENTRIES_READ):
        for f in e.get("files") or []:
            row = by.setdefault(f, {"path": f, "turns": 0, "last_ts": 0, "sessions": []})
            row["turns"] += 1
            row["last_ts"] = max(row["last_ts"], int(e.get("ts") or 0))
            sid = e.get("session_id")
            if sid and sid not in row["sessions"]:
                row["sessions"].append(sid)
    rows = list(by.values())
    rows.sort(key=lambda r: -r["last_ts"])
    return rows


def clear(key: str) -> bool:
    p = path_for(key)
    try:
        if os.path.isfile(p):
            os.remove(p)
        return True
    except OSError:
        return False
