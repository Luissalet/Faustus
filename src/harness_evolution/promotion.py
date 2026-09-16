"""harness_evolution/promotion.py — CAS promotion, canary gate, and the
applicability recheck that reverts a regressed candidate (A28/A30).

Three operations, each a thin, auditable wrapper around
`store.HarnessEvolutionStore`:

* `promote` — the only way a `CandidatePatch` becomes the new active
  `HarnessRevision`. Refuses anything not `evaluated` and `eligible`
  (A27's gate, enforced again here so a caller cannot promote by skipping
  straight to this function), runs the bounded canary
  (`harness_evolution_canary_runs`, default 5 — read by `service.py` since
  this module never reads settings itself), then hands the actual
  transition to `store.promote_cas`, whose `StaleParent` (A30) this module
  lets propagate rather than swallow.
* `rebase` — what A30's "option to rebase" means concretely: repoint a
  stale patch at whatever revision IS active now and mark it back to
  `proposed`, so `service.py` re-runs validation and evaluation against the
  new parent before anyone promotes it again. Never re-promotes on its own.
* `recheck_applicability` — A28. `required_capabilities` on a promoted
  patch is `{capability_name: digest_recorded_at_evaluation}`; a caller
  supplies `resolve_digest(name) -> str` (production points this at
  `src.skills_runtime.discovery.skill_digest`/the equivalent for a
  specialist or instruction ref; a test points it at a fake resolver
  returning a *different* digest for the dependency that "updated"). Any
  mismatch reverts the candidate's revision back to its own
  `rollback_target` through the SAME CAS path `promote` used — a recheck is
  not a different, weaker kind of transition — and disables the patch with
  a `trace` naming exactly which capability's digest changed.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from src.contracts.base import now_iso

from .evaluator import outcome_from_dict
from .models import CandidatePatch, HarnessRevision, new_id
from .store import HarnessEvolutionStore, StaleParent

CanaryRunner = Callable[[int], bool]
DigestResolver = Callable[[str], Optional[str]]


class PromotionRefused(RuntimeError):
    pass


class CanaryFailed(RuntimeError):
    def __init__(self, run_index: int) -> None:
        super().__init__(f"canary run {run_index} failed — promotion aborted")
        self.run_index = run_index


def apply_change(parent_refs: Dict, patch: CandidatePatch) -> Dict:
    """The new revision's `refs`, derived from the parent's plus this one
    typed change. Never mutates `parent_refs` — a caller (or a failed
    promotion) still holding the parent dict must see it unchanged."""
    refs = copy.deepcopy(parent_refs or {})
    changes = patch.changes or {}
    change_type = str(changes.get("type") or "")
    if change_type == "add_skill":
        skills = dict(refs.get("skills") or {})
        skill_id = str(changes.get("skill_id") or "")
        skill_version = str(changes.get("skill_version") or "")
        if skill_id:
            skills[skill_id] = skill_version
        refs["skills"] = skills
        return refs
    ref = str(changes.get("ref") or "")
    content = changes.get("content")
    if ref in ("instructions_ref", "specialists_ref"):
        refs[ref] = content
    elif ref in (refs.get("skills") or {}):
        refs.setdefault("skills", {})[ref] = content
    elif ref in (refs.get("specialists") or {}):
        refs.setdefault("specialists", {})[ref] = content
    elif ref in (refs.get("memory_refs") or []):
        memory_refs = list(refs.get("memory_refs") or [])
        refs["memory_refs"] = [content if item == ref else item for item in memory_refs]
    else:
        refs[ref] = content
    return refs


def promote(store: HarnessEvolutionStore, patch: CandidatePatch, *, actor: str,
           canary_runs: int = 5, canary_runner: Optional[CanaryRunner] = None) -> HarnessRevision:
    """Raises `PromotionRefused` (not evaluated/eligible), `CanaryFailed`
    (a canary run failed), or `StaleParent` (A30 — the caller decides
    whether to `rebase` and retry). On success, mutates `patch` in place
    (`status="promoted"`, `promoted_revision_id`, a `rollback_target`
    defaulting to the parent it replaced) and persists it — the caller does
    not have to remember to call `store.update_patch` separately."""
    if patch.status != "evaluated":
        raise PromotionRefused(f"patch {patch.patch_id!r} is {patch.status!r}, not 'evaluated'")
    outcome = outcome_from_dict(patch.evaluation)
    if not outcome.eligible:
        raise PromotionRefused(
            f"patch {patch.patch_id!r} evaluation is not eligible for promotion "
            f"(source_pass={outcome.source_pass}, held_out_pass={outcome.held_out_pass})")
    parent = store.get_revision(patch.parent_revision)
    if parent is None or parent.status != "active":
        current = store.active_revision()
        raise StaleParent(current)
    for i in range(max(0, int(canary_runs))):
        if canary_runner is not None and not canary_runner(i):
            raise CanaryFailed(i)
    new_refs = apply_change(parent.refs, patch)
    new_revision = HarnessRevision(
        revision_id=new_id("rev"), parent_id=parent.revision_id, created_at=now_iso(),
        refs=new_refs, status="active", version=parent.version + 1)
    committed = store.promote_cas(parent_revision_id=parent.revision_id, new_revision=new_revision)
    patch.status = "promoted"
    patch.promoted_revision_id = committed.revision_id
    if not patch.rollback_target:
        patch.rollback_target = parent.revision_id
    patch.trace = f"promoted by {actor} at {now_iso()}: {parent.revision_id} -> {committed.revision_id}"
    store.update_patch(patch)
    return committed


def rebase(store: HarnessEvolutionStore, patch: CandidatePatch, stale: StaleParent) -> CandidatePatch:
    """A30's "safely rebase" option: repoint the patch at the revision that
    actually won the race and send it back to `proposed` so the caller
    (`service.rebase_and_retry`) re-validates and re-evaluates against it —
    this function never re-promotes by itself."""
    patch.parent_revision = stale.current.revision_id
    patch.status = "proposed"
    patch.trace = (f"rebased from stale parent onto {stale.current.revision_id} "
                    f"(v{stale.current.version}) at {now_iso()}")
    store.update_patch(patch)
    return patch


@dataclass(frozen=True)
class RecheckResult:
    ok: bool
    changed_capabilities: List[str]
    trace: str = ""
    reverted_revision_id: Optional[str] = None


def recheck_applicability(store: HarnessEvolutionStore, patch: CandidatePatch, *,
                          resolve_digest: DigestResolver, actor: str = "system") -> RecheckResult:
    """A28: recompute every `required_capabilities` digest via
    `resolve_digest` and compare against what was recorded when the patch
    was evaluated/promoted. A capability that no longer resolves at all
    (renamed/removed) counts as changed, same as one whose digest moved."""
    if patch.status != "promoted" or not patch.promoted_revision_id:
        return RecheckResult(ok=True, changed_capabilities=[])
    changed: List[str] = []
    for name, recorded_digest in (patch.required_capabilities or {}).items():
        current_digest = resolve_digest(name)
        if current_digest != recorded_digest:
            changed.append(name)
    if not changed:
        return RecheckResult(ok=True, changed_capabilities=[])

    active = store.get_revision(patch.promoted_revision_id)
    rollback_target = patch.rollback_target
    if active is None or active.status != "active" or not rollback_target:
        # The promoted revision already moved on (superseded, or nothing to
        # roll back to) — disable the patch and say why without touching the
        # store's active revision, which something else now owns.
        trace = (f"applicability recheck by {actor} at {now_iso()}: capabilities changed "
                 f"({', '.join(changed)}) but revision {patch.promoted_revision_id!r} is "
                 "no longer active or has no rollback_target — patch disabled only")
        patch.status = "disabled"
        patch.trace = trace
        store.update_patch(patch)
        return RecheckResult(ok=False, changed_capabilities=changed, trace=trace)

    previous = store.get_revision(rollback_target)
    if previous is None:
        trace = (f"applicability recheck by {actor} at {now_iso()}: rollback_target "
                 f"{rollback_target!r} no longer exists — patch disabled without revert")
        patch.status = "disabled"
        patch.trace = trace
        store.update_patch(patch)
        return RecheckResult(ok=False, changed_capabilities=changed, trace=trace)

    restored = HarnessRevision(
        revision_id=new_id("rev"), parent_id=active.revision_id, created_at=now_iso(),
        refs=copy.deepcopy(previous.refs), status="active", version=active.version + 1)
    committed = store.promote_cas(parent_revision_id=active.revision_id, new_revision=restored)
    trace = (f"applicability recheck by {actor} at {now_iso()}: capability digest changed "
             f"for {', '.join(changed)} — reverted {active.revision_id} to "
             f"{rollback_target}'s refs as {committed.revision_id}")
    patch.status = "reverted"
    patch.trace = trace
    store.update_patch(patch)
    return RecheckResult(ok=False, changed_capabilities=changed, trace=trace,
                         reverted_revision_id=committed.revision_id)
