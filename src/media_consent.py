"""
media_consent.py — MEDIA-06: a clone needs a yes on file before it exists.

`src.approval_store` answers "is this ACTION allowed right now" for one plan,
spent the moment it is used. This answers a different, longer-lived
question: did the person whose voice or face a render would clone actually
agree to being cloned at all? That is not a per-run permission — it outlives
any one render, and revoking it has to stop every future one, not just
refuse to spend a single-use card again. So it is its own small registry
rather than a second use of `approval_store` for a question that store was
not built to answer.

It is also its own registry rather than a database table: consent records
are rare, small, human-reviewed decisions — the same shape `data/settings.json`
already is, not a parallel authority for something `media_runs` or
`approval_store` covers. Nothing in `core/database.py` changes.

A workflow template opts into this gate explicitly (`requires_consent` +
`consent_subject_input` in its JSON, parsed in `src.media_workflows`). None
of the four shipped templates declare it, so this adds a capability without
gating anything that exists today — the rule this whole batch works under.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from typing import Any, Dict, List, Optional

from src.constants import DATA_DIR
from src.contracts.base import now_iso

logger = logging.getLogger(__name__)

#: One JSON file, like `data/settings.json` — reused DATA_DIR (src.constants),
#: not a new place persisted state lives.
CONSENT_FILE = os.path.join(DATA_DIR, "media_consent.json")
_LOCK = threading.RLock()


def _load(path: Optional[str] = None) -> List[Dict[str, Any]]:
    target = path or CONSENT_FILE
    if not os.path.isfile(target):
        return []
    try:
        with open(target, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError) as e:
        # A registry that cannot be read is NOT "nobody has consented" for a
        # subject that in fact has a record sitting in a corrupt file — but
        # it also must not crash a caller that only wants to know. The safe
        # reading is the same either way: report no consent found, and log
        # loudly so a corrupt file is noticed rather than silently trusted.
        logger.error("media consent registry %s could not be read: %s", target, e)
        return []
    return raw if isinstance(raw, list) else []


def _save(records: List[Dict[str, Any]], path: Optional[str] = None) -> None:
    target = path or CONSENT_FILE
    os.makedirs(os.path.dirname(target), exist_ok=True)
    # Write-then-rename: a reader never observes a half-written file, and a
    # process killed mid-save leaves the previous, complete version in place.
    tmp = f"{target}.tmp-{uuid.uuid4().hex[:8]}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(records, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, target)


def register(subject: str, *, granted_by: str, scope: str = "clone", note: str = "",
            registry_path: Optional[str] = None) -> Dict[str, Any]:
    """Record that `granted_by` obtained consent from `subject` to be cloned.

    `subject` is free text on purpose — a name, an id, whatever the person
    running the render actually has on hand; this is a record of a human
    decision, not an identity system. `granted_by` is required for the same
    reason `approval_store.decide()`'s `by` is: a consent nobody is
    accountable for having obtained is not a consent.
    """
    subject_key = (subject or "").strip()
    who = (granted_by or "").strip()
    if not subject_key:
        return {"ok": False, "reason": "no_subject",
                "detail": "name whose voice or face this covers"}
    if not who:
        return {"ok": False, "reason": "no_grantor",
                "detail": "a consent record has to say who obtained it"}
    with _LOCK:
        records = _load(registry_path)
        record = {"id": f"consent_{uuid.uuid4().hex[:20]}", "subject": subject_key,
                  "scope": scope or "clone", "granted_by": who, "note": note,
                  "granted_at": now_iso(), "revoked_at": None}
        records.append(record)
        _save(records, registry_path)
    return {"ok": True, "consent": record}


def revoke(consent_id: str, *, registry_path: Optional[str] = None) -> Dict[str, Any]:
    """Withdraw one record. Idempotent: revoking an already-revoked or
    unknown id is reported, not raised — a retried click is not a bug."""
    with _LOCK:
        records = _load(registry_path)
        target = next((r for r in records if r.get("id") == consent_id), None)
        if target is None:
            return {"ok": False, "reason": "not_found"}
        if target.get("revoked_at"):
            return {"ok": True, "reason": "already_revoked", "idempotent": True}
        target["revoked_at"] = now_iso()
        _save(records, registry_path)
    return {"ok": True, "reason": "revoked"}


def has_consent(subject: str, *, scope: str = "clone",
               registry_path: Optional[str] = None) -> bool:
    """Is there a live (unrevoked) record for this subject and scope?"""
    subject_key = (subject or "").strip()
    if not subject_key:
        return False
    for r in _load(registry_path):
        if r.get("subject") == subject_key and not r.get("revoked_at") and r.get("scope") == (scope or "clone"):
            return True
    return False


def for_subject(subject: str, *, registry_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Every record — granted or revoked — for one subject, newest first;
    the audit trail a "who consented to what, and did it ever change" review
    needs, not only the current yes/no `has_consent()` answers."""
    subject_key = (subject or "").strip()
    return list(reversed([r for r in _load(registry_path) if r.get("subject") == subject_key]))
