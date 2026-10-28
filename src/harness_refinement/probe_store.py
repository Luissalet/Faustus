"""The record of every probed round: what was tried, what each repeat did, who won.

One JSON file (``DATA_DIR/harness_proposals/probes.json``), written atomically
under the same cross-process lock as the proposals. A probe record keeps the
evidence a person needs to audit an automatic decision: the task, the
candidates with their diffs, a compact trajectory of every repeat, the
aggregator's citations, the blind-selection mapping (seed and labels) and the
outcome with the ids of the proposals it produced.

Records of a round that was cut off (the app stopped, the model went away) are
marked ``aborted`` the next time the list is read; nothing is resumed.
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List, Optional

from core.atomic_io import atomic_write_json
from src.harness_refinement import store

MAX_PROBES = 120
STATUSES = ("running", "done", "skipped", "aborted", "error")

#: Probes this process is running right now (a `running` record not in here was cut off).
_ACTIVE: set = set()


def path() -> str:
    import os
    return os.path.join(store.root(), "probes.json")


def _load() -> Dict[str, Dict[str, Any]]:
    import json
    try:
        with open(path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(records: Dict[str, Dict[str, Any]]) -> None:
    store._ensure_root()
    if len(records) > MAX_PROBES:
        keep = sorted(records.values(), key=lambda r: r.get("created_at") or 0)[-MAX_PROBES:]
        records = {r["id"]: r for r in keep}
    atomic_write_json(path(), records, indent=1)


def new_id() -> str:
    return uuid.uuid4().hex[:16]


def create(session_id: str, owner: str, trigger: str, *, probe_id: Optional[str] = None,
           domain: str = "", k: int = 2) -> Dict[str, Any]:
    now = time.time()
    record = {
        "id": probe_id or new_id(), "session_id": session_id, "owner": owner or "", "trigger": trigger,
        "status": "running", "phase": "starting", "created_at": now, "updated_at": now, "finished_at": None,
        "domain": domain, "k": k, "task": None, "candidates": [], "rollouts": [], "compose": None,
        "selection": None, "outcome": None, "experience": None, "error": "", "reason": "",
    }
    with store.locked():
        records = _load()
        records[record["id"]] = record
        _save(records)
    _ACTIVE.add(record["id"])
    return record


def update(probe_id: str, **fields: Any) -> Dict[str, Any]:
    """Merge ``fields`` into the record. ``phase`` and the terminal ``status`` are the usual ones."""
    with store.locked():
        records = _load()
        record = records.get(probe_id)
        if record is None:
            return {}
        record.update(fields)
        record["updated_at"] = time.time()
        if record.get("status") != "running" and not record.get("finished_at"):
            record["finished_at"] = record["updated_at"]
        records[probe_id] = record
        _save(records)
    if record.get("status") != "running":
        _ACTIVE.discard(probe_id)
    return record


def get(probe_id: str) -> Optional[Dict[str, Any]]:
    return _load().get(str(probe_id or ""))


def reconcile() -> int:
    """Mark as aborted every ``running`` record this process is not running."""
    changed = 0
    with store.locked():
        records = _load()
        for record in records.values():
            if record.get("status") == "running" and record["id"] not in _ACTIVE:
                record.update(status="aborted", phase="aborted", reason="the app stopped while it was running",
                              finished_at=time.time(), updated_at=time.time())
                changed += 1
        if changed:
            _save(records)
    return changed


def list_probes(*, owner: Optional[str] = None, session_id: Optional[str] = None, status: Optional[str] = None,
                limit: int = 50) -> List[Dict[str, Any]]:
    reconcile()
    out = []
    for record in _load().values():
        if owner and record.get("owner") not in ("", owner):
            continue
        if session_id and record.get("session_id") != session_id:
            continue
        if status and record.get("status") != status:
            continue
        out.append(record)
    out.sort(key=lambda r: r.get("created_at") or 0, reverse=True)
    return out[:max(1, int(limit))]


def summary(record: Dict[str, Any]) -> Dict[str, Any]:
    """The light form for lists: no trajectories or deliverables."""
    outcome = record.get("outcome") or {}
    return {
        "id": record["id"], "session_id": record.get("session_id"), "status": record.get("status"),
        "phase": record.get("phase"), "created_at": record.get("created_at"),
        "finished_at": record.get("finished_at"), "domain": record.get("domain"), "k": record.get("k"),
        "task": (record.get("task") or {}).get("text", "")[:240] if record.get("task") else "",
        "candidates": [{k: c.get(k) for k in ("id", "line", "axis", "target", "op", "valid", "result",
                                               "proposal_id")} for c in record.get("candidates") or []],
        "winner": outcome.get("winner"), "mode": outcome.get("mode"), "applied": outcome.get("applied"),
        "reason": record.get("reason") or record.get("error") or "",
        "experience": (record.get("experience") or {}).get("status"),
    }
