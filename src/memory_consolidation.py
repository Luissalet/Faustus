"""memory_consolidation.py — run the memory tidy-up only when there is new work,
under a lease, and publish only what it decided on.

The legacy memory file is read once, a model is asked (possibly for minutes)
what to drop or clean, and the result used to be written back from that first
read. Anything saved in the meantime was overwritten, and two consolidations
racing each other each wrote their own stale copy. This module supplies the
pieces the tidy-up now uses:

* a **fingerprint** of the entries that matter for consolidation (id, text,
  category, source, pinned; never the use counter, which changes on every
  prompt), so "nothing new since last time" is a fact, not a guess;
* a **claim** on the work (``src.work_lease``) per owner, which also records the
  watermark after a successful run and backs off after a failure;
* a **plan** that is only the difference between what the tidy-up started with
  and what it decided, and a **commit** that applies that plan to a fresh read
  of the file inside the lease's commit window. An entry edited since the
  first read is skipped, an entry added since is kept, and a lease that was
  taken over publishes nothing.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set, Union

from src import work_lease

logger = logging.getLogger(__name__)

ALGORITHM_VERSION = "memory-tidy-1"
_LEASE_TTL_SECONDS = 900.0


def scope_for(owner: Optional[str]) -> str:
    return "memory-tidy:" + ((owner or "").strip().lower() or "*")


def _entry_key(entry: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": str(entry.get("id") or ""),
        "text": str(entry.get("text") or ""),
        "category": str(entry.get("category") or "fact"),
        "source": str(entry.get("source") or ""),
        "pinned": bool(entry.get("pinned")),
    }


def fingerprint(entries: Iterable[Dict[str, Any]]) -> str:
    rows = sorted((_entry_key(e) for e in entries), key=lambda r: r["id"])
    return hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def snapshot_fields(entries: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, str]]:
    """What each entry said when the tidy-up started (copies, not references)."""
    return {
        str(e.get("id")): {"text": str(e.get("text") or ""), "category": str(e.get("category") or "fact")}
        for e in entries if e.get("id")
    }


def nothing_new(scope: str, entries: List[Dict[str, Any]]) -> bool:
    """True when the last successful tidy-up already covered exactly these entries."""
    try:
        return work_lease.is_unchanged(scope, fingerprint(entries), ALGORITHM_VERSION)
    except Exception:  # noqa: BLE001 - an unreadable lease store means "unknown": run
        logger.warning("memory tidy: watermark unreadable", exc_info=True)
        return False


def claim(scope: str, holder: str) -> Union[work_lease.Lease, work_lease.LeaseDenied, None]:
    """Take the lease; ``None`` when the lease store itself is unusable.

    A broken lease store must not stop memory upkeep for good, but it also
    must not let two workers through unnoticed, so the caller logs and runs
    single-shot (the commit still reloads fresh data).
    """
    try:
        return work_lease.acquire(scope, holder, ttl=_LEASE_TTL_SECONDS)
    except Exception:  # noqa: BLE001
        logger.warning("memory tidy: lease store unavailable", exc_info=True)
        return None


@dataclass
class Plan:
    drops: Dict[str, str] = field(default_factory=dict)            # id -> text it had
    edits: Dict[str, Dict[str, str]] = field(default_factory=dict)  # id -> {from_*, to_*}

    def is_empty(self) -> bool:
        return not (self.drops or self.edits)


def make_plan(before: Dict[str, Dict[str, str]], final: Iterable[Dict[str, Any]]) -> Plan:
    """The difference between the start state and the decided state."""
    plan = Plan()
    final_by_id = {str(e.get("id")): e for e in final if e.get("id")}
    for mid, start in before.items():
        entry = final_by_id.get(mid)
        if entry is None:
            plan.drops[mid] = start["text"]
            continue
        text = str(entry.get("text") or "")
        category = str(entry.get("category") or "fact")
        if text != start["text"] or category != start["category"]:
            plan.edits[mid] = {
                "from_text": start["text"], "from_category": start["category"],
                "to_text": text, "to_category": category,
            }
    return plan


@dataclass
class CommitResult:
    committed: bool
    reason: str = ""
    dropped: int = 0
    edited: int = 0
    skipped_changed: int = 0
    kept_new: int = 0
    post: List[Dict[str, Any]] = field(default_factory=list)


def commit(manager: Any, lease: Optional[work_lease.Lease], plan: Plan,
           before_ids: Set[str]) -> CommitResult:
    """Apply ``plan`` to a fresh read, inside the lease's commit window.

    With no lease (store unavailable) it still reloads and applies by id; it
    just has no fence to check.
    """
    if plan.is_empty():
        return CommitResult(True, "nothing to apply")

    def apply() -> CommitResult:
        fresh = manager.load_all_for_update()
        result = CommitResult(False)
        out: List[Dict[str, Any]] = []
        for entry in fresh:
            mid = str(entry.get("id") or "")
            text = str(entry.get("text") or "")
            if mid in plan.drops:
                if text == plan.drops[mid]:
                    result.dropped += 1
                    continue
                result.skipped_changed += 1
            elif mid in plan.edits:
                edit = plan.edits[mid]
                if text == edit["from_text"] and str(entry.get("category") or "fact") == edit["from_category"]:
                    entry["text"] = edit["to_text"]
                    entry["category"] = edit["to_category"]
                    result.edited += 1
                else:
                    result.skipped_changed += 1
            elif mid and mid not in before_ids:
                result.kept_new += 1
            out.append(entry)
        manager.save(out)
        result.committed = True
        result.post = out
        return result

    if lease is None:
        return apply()
    with work_lease.commit_window(lease) as window:
        if not window.ok:
            return CommitResult(False, "lease lost")
        return apply()


def finish(lease: Optional[work_lease.Lease], *, success: bool,
           post_entries: Optional[List[Dict[str, Any]]], before_ids: Set[str]) -> None:
    """Release the lease; on success record what this run covered.

    The watermark fingerprints the entries the run actually saw (by id), after
    its own changes, so something saved during the run is still new next time.
    """
    if lease is None:
        return
    try:
        covered = [e for e in (post_entries or []) if str(e.get("id")) in before_ids]
        work_lease.release(
            lease, success=success, input_revision=fingerprint(covered) if success else "",
            algorithm_version=ALGORITHM_VERSION)
    except Exception:  # noqa: BLE001
        logger.warning("memory tidy: could not release the lease", exc_info=True)
