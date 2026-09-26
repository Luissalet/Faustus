"""chat_versions.py — the answers an edit would otherwise destroy.

Editing a message (or regenerating from it) truncates the chat: everything
after the edit point is deleted from the database and is gone. Claude and
ChatGPT both keep the old branch instead and let you flip between versions —
losing a good answer because you wanted to reword the question is a bad
trade, and it is worse here, where an answer can be twenty minutes of a local
model's time.

So before a truncation the discarded tail is written aside, and can be put
back. Storage is a JSON file per session under DATA_DIR (like agent_runs), not
a database table: no schema change, no migration, and a corrupt or deleted file
costs history, never the chat itself.

Restoring is symmetric — the tail being replaced is saved as a version of its
own first, so you can switch back and forth rather than trading one loss for
another.

Stdlib only. Never raises out of the public helpers: this is a safety net, and
a safety net that can break the fall it is catching is worse than none.
"""
from __future__ import annotations

import json
import logging
import os
import hashlib
import re
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_KEEP = 10
DEFAULT_KEEP_HOURS = 168          # a week
_MAX_FILE_BYTES = 4_000_000
_MAX_PREVIEW = 160
_SID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_LOCK = threading.Lock()


def _setting(key: str, default: Any) -> Any:
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:
        return default


def enabled() -> bool:
    return bool(_setting("chat_versions", True))


def _dir() -> str:
    try:
        from src.constants import DATA_DIR
    except Exception:                                     # pragma: no cover
        DATA_DIR = os.path.join(os.getcwd(), "data")
    return os.path.join(DATA_DIR, "chat_versions")


def _path(session_id: str) -> Optional[str]:
    sid = str(session_id or "")
    if not _SID_RE.match(sid):
        return None
    return os.path.join(_dir(), f"{sid}.json")


def _load(session_id: str) -> Dict[str, Any]:
    p = _path(session_id)
    if not p or not os.path.isfile(p):
        return {"versions": []}
    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("versions"), list):
            return data
    except (OSError, ValueError) as e:
        logger.debug("[chat-versions] unreadable %s: %s", p, e)
    return {"versions": []}


def _store(session_id: str, data: Dict[str, Any]) -> bool:
    p = _path(session_id)
    if not p:
        return False
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.replace(tmp, p)
        return True
    except (OSError, TypeError, ValueError) as e:
        logger.warning("[chat-versions] could not write %s: %s", p, e)
        return False


def _as_dict(msg: Any) -> Dict[str, Any]:
    if isinstance(msg, dict):
        out = dict(msg)
    elif hasattr(msg, "to_dict"):
        try:
            out = dict(msg.to_dict())
        except Exception:
            out = {"role": getattr(msg, "role", "assistant"),
                   "content": getattr(msg, "content", "")}
    else:
        out = {"role": getattr(msg, "role", "assistant"),
               "content": getattr(msg, "content", "")}
    out["role"] = str(out.get("role") or "assistant")
    if not isinstance(out.get("content"), (str, list, dict)):
        out["content"] = "" if out.get("content") is None else str(out["content"])
    return out


def _preview(messages: List[Dict[str, Any]]) -> str:
    """The first line of the first assistant answer — what the version *was*."""
    for m in messages:
        if m.get("role") == "assistant":
            c = m.get("content")
            text = c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
            text = " ".join(str(text).split())
            if text:
                return text[:_MAX_PREVIEW] + ("…" if len(text) > _MAX_PREVIEW else "")
    for m in messages:
        c = m.get("content")
        text = " ".join(str(c if isinstance(c, str) else "").split())
        if text:
            return text[:_MAX_PREVIEW] + ("…" if len(text) > _MAX_PREVIEW else "")
    return ""


def _prune(versions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    try:
        keep = int(_setting("chat_versions_keep", DEFAULT_KEEP) or DEFAULT_KEEP)
    except (TypeError, ValueError):
        keep = DEFAULT_KEEP
    try:
        hours = float(_setting("chat_versions_keep_hours", DEFAULT_KEEP_HOURS) or DEFAULT_KEEP_HOURS)
    except (TypeError, ValueError):
        hours = DEFAULT_KEEP_HOURS
    cutoff = time.time() - max(1.0, hours) * 3600.0
    fresh = [v for v in versions if float(v.get("created_at") or 0) >= cutoff]
    fresh = fresh[-max(1, keep):]
    # Size cap: drop from the oldest until the file fits.
    while len(fresh) > 1 and len(json.dumps({"versions": fresh}, ensure_ascii=False)) > _MAX_FILE_BYTES:
        fresh = fresh[1:]
    return fresh


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [p.get("text", "") for p in content if isinstance(p, dict) and isinstance(p.get("text"), str)]
        return "\n".join(parts)
    return "" if content is None else json.dumps(content, ensure_ascii=False, default=str)


def prefix_signature(messages: Any) -> str:
    """What the chat looked like before the cut: a later edit further up
    changes it, and an old version of message N no longer belongs there."""
    h = hashlib.sha1()
    for m in (messages or []):
        d = _as_dict(m)
        h.update(d["role"].encode("utf-8", "replace"))
        h.update(b"\x00")
        h.update(_text(d.get("content")).encode("utf-8", "replace"))
        h.update(b"\x01")
    return h.hexdigest()[:20]


def save(session_id: str, messages: Any, *, keep_count: int = 0,
         reason: str = "edit", prefix_sig: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Set the dropped tail aside. Returns the version summary, or None."""
    if not enabled():
        return None
    msgs = [_as_dict(m) for m in (messages or [])]
    if not msgs:
        return None
    record = {
        "id": uuid.uuid4().hex[:12],
        "created_at": time.time(),
        "reason": str(reason or "edit")[:40],
        "keep_count": max(0, int(keep_count or 0)),
        "count": len(msgs),
        "preview": _preview(msgs),
        "messages": msgs,
    }
    if prefix_sig:
        record["prefix_sig"] = str(prefix_sig)[:40]
    with _LOCK:
        data = _load(session_id)
        data["versions"] = _prune(list(data.get("versions") or []) + [record])
        if not _store(session_id, data):
            return None
    return summary(record)


def summary(record: Dict[str, Any]) -> Dict[str, Any]:
    return {k: record.get(k) for k in ("id", "created_at", "reason", "keep_count", "count", "preview")}


def list_versions(session_id: str) -> List[Dict[str, Any]]:
    """Newest first."""
    with _LOCK:
        data = _load(session_id)
    return [summary(v) for v in reversed(list(data.get("versions") or []))]


def get(session_id: str, version_id: str) -> Optional[Dict[str, Any]]:
    with _LOCK:
        data = _load(session_id)
    for v in data.get("versions") or []:
        if v.get("id") == version_id:
            return v
    return None


_MAX_ANSWER = 40_000


def _answer_model(md: Dict[str, Any]) -> Optional[str]:
    for src in (md, md.get("metrics") if isinstance(md.get("metrics"), dict) else {}):
        if isinstance(src.get("model"), str) and src["model"]:
            return src["model"]
    return None


def alternatives(session_id: str, history: Any) -> Dict[str, List[Dict[str, Any]]]:
    """Earlier answers to the questions still in the chat, by the history
    index of the question: what a regenerate or an edit replaced, so the chat
    can flip between them (‹ 1/3 ›) instead of only restoring whole tails.

    A version belongs to question N when it was cut at N, starts with a user
    message, and the chat before N is still the one it was cut from (its
    `prefix_sig`; versions saved before the signature existed match by
    position only)."""
    hist = [_as_dict(m) for m in (history or [])]
    with _LOCK:
        data = _load(session_id)
    out: Dict[str, List[Dict[str, Any]]] = {}
    sigs: Dict[int, str] = {}
    for v in data.get("versions") or []:
        msgs = v.get("messages") or []
        k = int(v.get("keep_count") or 0)
        if not msgs or not isinstance(msgs[0], dict) or msgs[0].get("role") != "user":
            continue
        if k >= len(hist) or hist[k].get("role") != "user":
            continue
        if v.get("prefix_sig"):
            if k not in sigs:
                sigs[k] = prefix_signature(hist[:k])
            if v["prefix_sig"] != sigs[k]:
                continue
        answer = next((m for m in msgs[1:] if isinstance(m, dict) and m.get("role") == "assistant"
                       and _text(m.get("content")).strip()), None)
        if answer is None:
            continue
        md = answer.get("metadata") if isinstance(answer.get("metadata"), dict) else {}
        question = _text(msgs[0].get("content"))
        text = _text(answer.get("content"))
        out.setdefault(str(k), []).append({
            "id": v.get("id"), "created_at": v.get("created_at"), "reason": v.get("reason"),
            "question": question[:600], "same_question": question.strip() == _text(hist[k].get("content")).strip(),
            "answer": text[:_MAX_ANSWER], "truncated": len(text) > _MAX_ANSWER,
            "model": _answer_model(md),
            "messages": len(msgs),
        })
    return out


def drop(session_id: str, version_id: str) -> bool:
    with _LOCK:
        data = _load(session_id)
        before = len(data.get("versions") or [])
        data["versions"] = [v for v in (data.get("versions") or []) if v.get("id") != version_id]
        if len(data["versions"]) == before:
            return False
        return _store(session_id, data)


def clear(session_id: str) -> int:
    with _LOCK:
        data = _load(session_id)
        n = len(data.get("versions") or [])
        p = _path(session_id)
        if p and os.path.isfile(p):
            try:
                os.remove(p)
            except OSError:
                _store(session_id, {"versions": []})
    return n
