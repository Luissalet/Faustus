"""
src/branching_futures/narrative_canon.py — canon vs. discarded alternative for
creative writing (WRITE-02 / WRITE-04, QA-47).

docs/spec/v2/acceptance_scenarios.json's QA-47: create an alternate ending,
discard it, write the next chapter — "canon original conservado; alternativa
no se recupera como hecho confirmado." Before this module the repo had no
concept of "canon" for a narrative distinct from "explored and abandoned":
`src/branching_futures` already models exactly the shape this needs (explore
several strategies for the SAME decision point, submit an observed result for
each, select one) but had no domain vocabulary for "this branch's content is
now the confirmed story" versus "this branch was an alternative that lost."

This module adds that vocabulary on top of `BranchingService` rather than
beside it (COMUN.md rule 4: no second store for the same thing). A narrative
alternative — "what if the ending were different" — IS a `future` with (at
least) the current canon path as one strategy and the alternative as another;
"discarding" the alternative is either selecting a different branch as canon
(`BranchingService.select`) or abandoning the future outright
(`BranchingService.cancel`) — both already exist and already persist through
`persistence.store()`. `alternative_status` below is a pure function of the
state those two calls already produce; no new row kind is written for it.

`canon_state(project)` is the ONE function a summarizer, a memory-recall
stage, or "write the next chapter" is meant to read for "what actually
happened so far." It is built ONLY from `future.status == "selected"`
(promoted) branches — there is no code path in this module that copies a
non-promoted branch's result into it, so a discarded alternative cannot
resurface as fact through this function even though its row is still on disk
(a deliberate audit trail, not a memory). `discarded_alternatives(project)`
exists precisely to make that absence checkable: a continuity test (or a
`docs/spec/v2` acceptance script) can assert that everything it lists is
ABSENT from `canon_state(project)`'s serialized text.

Wiring notes (see this lote's report for the exact hook): nothing outside
`src/branching_futures` currently calls `canon_state`. `src/memory_engine.py`
and `src/context_engine/` are the modules that would need to prefer it over
raw transcript recall for a project using branching futures for its
narrative — both are outside this lote's file list.
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

from src.branching_futures.service import BranchingService, service as _default_service

ALT_STATUS_DRAFT = "draft"
ALT_STATUS_DISCARDED = "discarded"
ALT_STATUS_PROMOTED = "promoted"

#: Closed vocabulary, exactly as the lote names it: "una alternativa lleva
#: status: draft|discarded|promoted y sólo promoted entra en canon."
ALT_STATUSES = (ALT_STATUS_DRAFT, ALT_STATUS_DISCARDED, ALT_STATUS_PROMOTED)

#: `commit()` currently fails closed (no real execution adapter — see
#: `BranchingService.commit`), so "committing"/"committed" are unreachable in
#: this build, but a future's canon is defined by "a selection was made", not
#: by which of these three post-selection statuses it happens to be in.
_CONFIRMED_FUTURE_STATUSES = ("selected", "committing", "committed")

#: Future-level statuses that mean "this exploration is over and nothing (or
#: something other than this branch) was confirmed" — a selection was made
#: (`_CONFIRMED_FUTURE_STATUSES`) or the future was abandoned outright.
_TERMINAL_FUTURE_STATUSES = _CONFIRMED_FUTURE_STATUSES + ("cancelled", "failed", "inconclusive")

#: Branch-level statuses that mean "this candidate is out of the running",
#: independent of whether its future ever reached a terminal status.
_LOST_BRANCH_STATUSES = ("pruned", "cancelled", "failed")


def alternative_status(branch: Mapping[str, Any], future: Mapping[str, Any]) -> str:
    """The WRITE-02 status of one branch within its future.

    - `draft`     — the future is still open and this branch hasn't lost yet.
    - `promoted`  — this is the future's selected (canon) branch.
    - `discarded` — this branch was itself pruned/cancelled/failed, OR its
                    future reached a terminal state (a different branch was
                    selected, or the whole exploration was cancelled/failed/
                    inconclusive) without confirming this one.
    """
    if branch.get("id") and future.get("selected_branch_id") == branch.get("id"):
        return ALT_STATUS_PROMOTED
    if branch.get("status") in _LOST_BRANCH_STATUSES:
        return ALT_STATUS_DISCARDED
    if future.get("status") in _TERMINAL_FUTURE_STATUSES:
        return ALT_STATUS_DISCARDED
    return ALT_STATUS_DRAFT


def _confirmed_chapter(full_future: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """The canon record for one future that has confirmed a branch, or None
    if it has no selection (nothing to confirm) or the selected branch/result
    can no longer be found (defensive — should not happen through the normal
    `select()` path)."""
    selected_id = full_future.get("selected_branch_id")
    if not selected_id:
        return None
    branch = next((b for b in full_future.get("branches", []) if b.get("id") == selected_id), None)
    result = next((r for r in full_future.get("results", []) if r.get("branch_id") == selected_id), None)
    if not branch or not result:
        return None
    selection = full_future.get("selection") or {}
    return {
        "future_id": full_future.get("id", ""),
        "title": full_future.get("title", ""),
        "branch_id": branch.get("id", ""),
        "strategy_id": (branch.get("strategy") or {}).get("id", ""),
        "strategy_title": (branch.get("strategy") or {}).get("title", ""),
        "summary": result.get("summary", ""),
        "artifact_refs": list(result.get("artifact_refs") or []),
        "selection_id": selection.get("id", ""),
        "rationale": selection.get("rationale", ""),
        "confirmed_at": selection.get("selected_at", ""),
    }


def canon_state(*, owner: str, project_id: str,
                 svc: Optional[BranchingService] = None) -> Dict[str, Any]:
    """Everything CONFIRMED for a project: the promoted content of every
    future that has made a selection, oldest first. This is what "write the
    next chapter" (or a summarizer, or memory recall) should read as the
    story so far — never the raw branch/result rows, which still hold every
    discarded alternative's text for audit purposes.
    """
    svc = svc or _default_service()
    chapters: List[Dict[str, Any]] = []
    for summary_row in svc.futures(owner=owner, project_id=project_id, limit=1000):
        if summary_row.get("status") not in _CONFIRMED_FUTURE_STATUSES:
            continue
        full = svc.future(owner=owner, future_id=summary_row["id"])
        if full is None:
            continue
        chapter = _confirmed_chapter(full)
        if chapter is not None:
            chapters.append(chapter)
    chapters.sort(key=lambda row: row.get("confirmed_at") or "")
    return {"project_id": project_id, "chapters": chapters}


def discarded_alternatives(*, owner: str, project_id: str,
                            svc: Optional[BranchingService] = None) -> List[Dict[str, Any]]:
    """Every branch across the project's futures whose current
    `alternative_status` is `discarded`, each labeled with that status.

    This exists so a caller can PROVE none of it leaked into `canon_state` —
    diff the two by branch id/summary text — not as a second surface a
    recall path should read from.
    """
    svc = svc or _default_service()
    out: List[Dict[str, Any]] = []
    for summary_row in svc.futures(owner=owner, project_id=project_id, limit=1000):
        full = svc.future(owner=owner, future_id=summary_row["id"])
        if full is None:
            continue
        for branch in full.get("branches", []):
            status = alternative_status(branch, full)
            if status != ALT_STATUS_DISCARDED:
                continue
            result = next((r for r in full.get("results", []) if r.get("branch_id") == branch.get("id")), None)
            out.append({
                "future_id": full.get("id", ""),
                "branch_id": branch.get("id", ""),
                "strategy_id": (branch.get("strategy") or {}).get("id", ""),
                "strategy_title": (branch.get("strategy") or {}).get("title", ""),
                "status": status,
                "summary": (result or {}).get("summary", ""),
            })
    return out
