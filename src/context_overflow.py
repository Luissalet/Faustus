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


# ---------------------------------------------------------------------------
# A15 — reacquisition: when the model asks for a detail that compaction
# omitted, it re-reads the ORIGINAL spilled body (by its overflow id) or an
# artifact, and that re-read has a real cost — this records it so a run's
# report can show "compaction saved N tokens, then the model paid M of them
# back". Schema lives in the shared Context Engine database, same pattern
# `context_engine.compaction_pins` uses for its own event log.
# ---------------------------------------------------------------------------

from src.context_engine import store as _store  # noqa: E402  (after constants import above)

_REACQ_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS context_reacquisitions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL DEFAULT '',
        run_id TEXT NOT NULL DEFAULT '',
        overflow_id TEXT NOT NULL DEFAULT '',
        artifact_id TEXT NOT NULL DEFAULT '',
        chars INTEGER NOT NULL DEFAULT 0,
        tokens_est INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_context_reacq_scope "
    "ON context_reacquisitions(session_id, run_id, created_at)",
)
_store.register_schema("context_reacquisitions", _REACQ_SCHEMA)


def _tokens_est(chars: int) -> int:
    # Same rough 4-chars-per-token heuristic `model_context.estimate_tokens`
    # uses elsewhere in the compaction path; good enough for a cost figure,
    # not for context-window accounting.
    return max(0, int(chars / 4))


def record_reacquisition(
    *,
    session_id: str,
    run_id: str = "",
    overflow_id: str = "",
    artifact_id: str = "",
    chars: int,
) -> Dict[str, Any]:
    """Record that the model paid to re-read `overflow_id` or `artifact_id`.
    Never raises — a broken log must not break the re-read it is logging."""
    tokens_est = _tokens_est(chars)
    record = {
        "session_id": session_id or "", "run_id": run_id or "",
        "overflow_id": overflow_id or "", "artifact_id": artifact_id or "",
        "chars": int(chars or 0), "tokens_est": tokens_est, "at": now_iso(),
    }
    try:
        with _store.db() as conn:
            conn.execute(
                "INSERT INTO context_reacquisitions"
                "(session_id, run_id, overflow_id, artifact_id, chars, tokens_est, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (record["session_id"], record["run_id"], record["overflow_id"],
                 record["artifact_id"], record["chars"], record["tokens_est"],
                 record["at"]),
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("context_overflow.record_reacquisition failed: %s", e)
    return record


def reacquisitions_for(session_id: str, run_id: str = "") -> List[Dict[str, Any]]:
    """The queryable log a run/session report reads back: every reacquisition
    recorded for this session (optionally narrowed to one run)."""
    sid = str(session_id or "").strip()
    if not sid:
        return []
    try:
        with _store.db() as conn:
            if run_id:
                cur = conn.execute(
                    "SELECT session_id, run_id, overflow_id, artifact_id, chars, "
                    "tokens_est, created_at AS at FROM context_reacquisitions "
                    "WHERE session_id=? AND run_id=? ORDER BY created_at ASC",
                    (sid, run_id),
                )
            else:
                cur = conn.execute(
                    "SELECT session_id, run_id, overflow_id, artifact_id, chars, "
                    "tokens_est, created_at AS at FROM context_reacquisitions "
                    "WHERE session_id=? ORDER BY created_at ASC",
                    (sid,),
                )
            return _store.rows(cur)
    except Exception as e:  # noqa: BLE001
        logger.warning("context_overflow.reacquisitions_for failed: %s", e)
        return []


def reacquisition_summary(session_id: str, run_id: str = "") -> Dict[str, int]:
    """`reacquired_count`/`reacquired_chars` — the two counters a compaction
    report adds on top of its own `tokens_before/after`."""
    entries = reacquisitions_for(session_id, run_id)
    return {
        "reacquired_count": len(entries),
        "reacquired_chars": sum(int(e.get("chars") or 0) for e in entries),
        "reacquired_tokens_est": sum(int(e.get("tokens_est") or 0) for e in entries),
    }


def read_overflow(
    *,
    session_id: str,
    content_sha256: str,
    run_id: str = "",
) -> Optional[Dict[str, Any]]:
    """Re-acquire an original spilled body by its overflow id and record the
    cost. Returns ``{"content", "content_sha256", "chars"}`` or ``None`` when
    nothing is stored under that id — the caller (a tool, a route) decides
    how to report a miss."""
    content = load(session_id=session_id, content_sha256=content_sha256)
    if content is None:
        return None
    record_reacquisition(
        session_id=session_id, run_id=run_id,
        overflow_id=content_sha256, chars=len(content),
    )
    return {"content": content, "content_sha256": content_sha256, "chars": len(content)}


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
