"""
memory_view.py — what a single run was actually shown, and what it was not.

`memory_engine` decides what Faustus knows. This module decides what one run
gets to see, and — the part that earns it a file of its own — records what it
left out and why.

Without that record, "the model knew about the brand voice" is unfalsifiable.
With it, a wrong answer splits into two different bugs with two different
fixes: an entry that was included and led the model astray, or an entry that
was dropped for budget and should not have been. A view that lists what it
kept and stays quiet about what it cut hides the reason for half of the
model's behaviour.

Three rules, and the first is the only one that is a security property:

* **scope is a wall, not a label.** A project entry never reaches a run in
  another project, even when that run declared `project` readable. The check
  lives in `contracts.MemoryEntry.readable_by` so there is one copy of it.
* **the order is deterministic.** Same entries and same budget produce the
  same view and the same fingerprint, so "it worked yesterday" is a
  comparison rather than an argument.
* **a degraded view says what it lost.** The contract refuses to build one
  that will not.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from src.contracts import MemoryEntry, MemoryView
from src.contracts.base import now_iso

logger = logging.getLogger(__name__)

#: Anti-patterns first: a rule the curator inverted after it kept causing
#: failures is the one most worth spending budget on, and the one whose
#: absence is hardest to notice. Then proven, then candidates.
TRUST_ORDER = {"anti_pattern": 0, "proven": 1, "candidate": 2, "retired": 9}

DEFAULT_BUDGET_CHARS = 4000


def _sort_key(entry: MemoryEntry) -> Tuple[int, str, str]:
    """Trust, then newest, then id. The id breaks the tie so two entries
    written in the same second cannot swap places between runs."""
    return (TRUST_ORDER.get(entry.trust, 5),
            "" if not entry.updated_at and not entry.created_at
            else (entry.updated_at or entry.created_at),
            entry.id)


def build(entries: Iterable[MemoryEntry], *, scopes: Sequence[str],
          project_id: str = "", skill_id: str = "", run_id: str = "",
          budget_chars: Optional[int] = DEFAULT_BUDGET_CHARS,
          degraded: bool = False, degraded_reason: str = "",
          ) -> Tuple[MemoryView, List[MemoryEntry]]:
    """Select what this run sees. Returns the view and the entries in it.

    Every entry that does not make it appears in `view.dropped` with a reason
    from `contracts.DROP_REASONS` — including the ones filtered by scope,
    which is the case a "top N by relevance" selector would silently omit."""
    scopes = tuple(scopes or ())
    kept: List[MemoryEntry] = []
    dropped: List[Dict[str, str]] = []
    seen_bodies: Dict[str, str] = {}
    used = 0

    for entry in sorted(entries, key=_sort_key):
        if entry.trust == "retired":
            dropped.append({"id": entry.id, "reason": "retired", "detail": ""})
            continue
        if not entry.readable_by(scopes, project_id=project_id, skill_id=skill_id):
            dropped.append({
                "id": entry.id, "reason": "scope",
                "detail": f"{entry.scope} entry, and this run reads {list(scopes)}"
                          + (f" of project {project_id!r}" if entry.scope == "project" else ""),
            })
            continue
        body = " ".join(entry.body.split()).lower()
        if body in seen_bodies:
            dropped.append({"id": entry.id, "reason": "duplicate",
                            "detail": f"same text as {seen_bodies[body]}"})
            continue
        cost = len(entry.body) + 1
        if budget_chars is not None and used + cost > budget_chars:
            dropped.append({"id": entry.id, "reason": "budget",
                            "detail": f"{used + cost} chars would pass the {budget_chars} budget"})
            continue
        seen_bodies[body] = entry.id
        kept.append(entry)
        used += cost

    view = MemoryView.parse({
        "run_id": run_id, "scopes": list(scopes),
        "entry_ids": [e.id for e in kept], "dropped": dropped,
        "budget_chars": budget_chars, "used_chars": used,
        "degraded": degraded, "degraded_reason": degraded_reason,
        "built_at": now_iso(),
    })
    return view, kept


def render(entries: Sequence[MemoryEntry]) -> str:
    """The block a run is given. Anti-patterns keep their `AVOID:` shape and
    say what they were inverted from, because a rule with no history behind it
    is one the user cannot argue with."""
    if not entries:
        return ""
    lines: List[str] = []
    for entry in entries:
        if entry.trust == "anti_pattern":
            lines.append(f"AVOID: {entry.body}"
                         + (f"  (inverted from {entry.inverted_from})"
                            if entry.inverted_from else ""))
        else:
            lines.append(entry.body)
    return "\n".join(lines)


def explain(view: MemoryView, *, limit: int = 20) -> str:
    """Why the run saw what it saw, in the words an operator needs. Reads as a
    sentence rather than a table because the question it answers — "why did it
    not know about X?" — is usually asked once, in a hurry."""
    parts = [f"{len(view.entry_ids)} entries in scope {list(view.scopes)}"]
    if view.budget_chars is not None:
        parts.append(f"{view.used_chars}/{view.budget_chars} chars")
    if view.degraded:
        parts.append(f"DEGRADED: {view.degraded_reason}")
    head = " · ".join(parts)
    if not view.dropped:
        return head + " · nothing was dropped"
    by_reason: Dict[str, List[str]] = {}
    for item in view.dropped:
        by_reason.setdefault(item["reason"], []).append(item["id"])
    tail = "; ".join(f"{reason}: {len(ids)} ({', '.join(ids[:limit])}"
                     + (", …" if len(ids) > limit else "") + ")"
                     for reason, ids in sorted(by_reason.items()))
    return head + " · dropped — " + tail
