"""UX-01 — a chat's unsent composer text, held server-side too.

Studio already keeps a draft in `localStorage` (`studio/src/screens/
Studio.tsx`'s `DRAFT_PREFIX`/`ATTACHMENTS_PREFIX`), which is enough for "left
the tab, came back on the same browser" but loses the draft the moment
someone opens the same chat from a different device or browser profile —
the actual UX-01 gap. This module is the server-side half: one small record
per (owner, session), the same content-addressed-file idiom
`src/chat_team.py` already uses for per-chat, owner-scoped state, so this
does not invent a second storage convention.

No revision/conflict semantics on purpose (unlike chat_team's `expected_
revision`): a draft is exactly one person's own in-progress typing, never
concurrently edited by two collaborators the way a saved team config can
be, so last-write-wins is the whole contract. The caller (routes/session_
routes.py, and Studio.tsx's own debounce-and-merge-by-recency logic) decides
when to write, not this module.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Dict, List

from core.atomic_io import atomic_write_text
from src.constants import DATA_DIR

#: Same cap the route enforces on the raw request body (COMUN: keep one
#: number, not two that can drift) — the *text* alone is capped here so a
#: caller that reads this module directly gets the same guarantee.
MAX_TEXT_CHARS = 64 * 1024
MAX_ATTACHMENTS = 64
MAX_ATTACHMENT_ID_CHARS = 300

EMPTY: Dict[str, Any] = {"text": "", "attachment_ids": [], "updated_at": 0}


def _path(session_id: str, owner: str) -> Path:
    if not session_id:
        raise ValueError("A saved conversation is required")
    key = hashlib.sha256(
        json.dumps([str(owner or ""), str(session_id)]).encode()
    ).hexdigest()
    return Path(DATA_DIR) / "session_drafts" / (key + ".json")


def _validate(text: Any, attachment_ids: Any) -> Dict[str, Any]:
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    if len(text) > MAX_TEXT_CHARS:
        raise ValueError(f"Draft text exceeds {MAX_TEXT_CHARS} characters")
    if not isinstance(attachment_ids, list) or len(attachment_ids) > MAX_ATTACHMENTS:
        raise ValueError(f"attachment_ids must be a list of at most {MAX_ATTACHMENTS} ids")
    ids: List[str] = []
    for aid in attachment_ids:
        if not isinstance(aid, str) or not aid.strip() or len(aid) > MAX_ATTACHMENT_ID_CHARS:
            raise ValueError("Invalid attachment id")
        ids.append(aid.strip())
    return {"text": text, "attachment_ids": ids}


def load(session_id: str, owner: str) -> Dict[str, Any]:
    """The saved draft for this (owner, session), or the empty record when
    none was ever saved — never raises for a missing file."""
    try:
        raw = json.loads(_path(session_id, owner).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return dict(EMPTY)
    except (OSError, ValueError):
        # A corrupted record must not crash the composer — the same "empty
        # draft" a first-ever visit gets, not an error the user cannot act on.
        return dict(EMPTY)
    if not isinstance(raw, dict):
        return dict(EMPTY)
    try:
        validated = _validate(raw.get("text", ""), raw.get("attachment_ids", []))
    except ValueError:
        return dict(EMPTY)
    validated["updated_at"] = raw.get("updated_at") or 0
    return validated


def save(session_id: str, owner: str, text: Any, attachment_ids: Any) -> Dict[str, Any]:
    """Persist the draft, or delete its file when both `text` and
    `attachment_ids` are empty — the store never grows with blank drafts,
    same discipline the localStorage side already keeps."""
    validated = _validate(text, attachment_ids)
    path = _path(session_id, owner)
    if not validated["text"].strip() and not validated["attachment_ids"]:
        path.unlink(missing_ok=True)
        return dict(EMPTY)
    validated["updated_at"] = time.time()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(str(path), json.dumps(validated, ensure_ascii=False))
    return validated
