"""tool_approval_grants.py — the untrusted-context gate remembered per workspace.

The "Allow this task to continue?" card had two lasting answers: for this
task, for this chat. Both die with the chat, so every new conversation in
the same project asked again — Luis, 14-09-2026: «que se quede así siempre
en el proyecto, no se me resetee en cada chat nuevo». This is the third
answer: **for this workspace folder**, kept on disk under the owner and the
folder's real path, honoured by every later chat whose workspace is that
folder (or inside it).

What it grants is exactly what the chat-scope answer grants and nothing
more: `approval_gate_bypassed` for the post-external-context gate. The
destructive command guard, per-call desktop confirmations, the
non-admin denylist, plan mode and every tool restriction stay where they
are — see `ToolRunSecurityContext.decision_for`, which checks the guard
BEFORE it looks at the bypass.

Stdlib only; one JSON file, rewritten whole under a lock.
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
_FILE_NAME = "tool_approval_grants.json"


def _path() -> str:
    from src.constants import DATA_DIR
    return os.path.join(DATA_DIR, _FILE_NAME)


def _norm_owner(owner: Any) -> str:
    return str(owner or "").strip().lower()


def _norm_workspace(workspace: Any) -> str:
    raw = str(workspace or "").strip()
    if not raw:
        return ""
    try:
        return os.path.normcase(os.path.realpath(os.path.expanduser(raw)))
    except (OSError, ValueError):
        return os.path.normcase(raw)


def _load() -> Dict[str, Dict[str, Dict[str, Any]]]:
    try:
        with open(_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning("tool_approval_grants: unreadable store, starting empty: %s", exc)
        return {}


def _save(data: Dict[str, Any]) -> None:
    path = _path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, path)


def grant(owner: Any, workspace: Any, *, tool: str = "", session_id: str = "") -> Optional[Dict[str, Any]]:
    """Remember that `owner` allowed the gate for `workspace`. Returns the
    record, or None when there is no workspace to key on (a chat without a
    folder has nothing lasting to attach the answer to)."""
    o, w = _norm_owner(owner), _norm_workspace(workspace)
    if not w:
        return None
    record = {
        "workspace": str(workspace or ""),
        "granted_at": time.time(),
        "tool": str(tool or ""),
        "session_id": str(session_id or ""),
    }
    with _LOCK:
        data = _load()
        data.setdefault(o, {})[w] = record
        _save(data)
    logger.info("tool_approval_grants: owner=%r workspace=%r granted", o, w)
    return record


def is_granted(owner: Any, workspace: Any) -> bool:
    """True when `workspace` is a granted folder or lies inside one."""
    o, w = _norm_owner(owner), _norm_workspace(workspace)
    if not w:
        return False
    with _LOCK:
        rows = _load().get(o) or {}
    for granted in rows:
        try:
            if w == granted or os.path.commonpath([w, granted]) == granted:
                return True
        except ValueError:
            continue
    return False


def revoke(owner: Any, workspace: Any) -> bool:
    o, w = _norm_owner(owner), _norm_workspace(workspace)
    with _LOCK:
        data = _load()
        rows = data.get(o) or {}
        if w not in rows:
            return False
        del rows[w]
        if not rows:
            data.pop(o, None)
        _save(data)
    return True


def list_for(owner: Any) -> List[Dict[str, Any]]:
    o = _norm_owner(owner)
    with _LOCK:
        rows = _load().get(o) or {}
    return [{"key": k, **v} for k, v in sorted(rows.items())]
