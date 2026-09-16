"""harness_evolution/service.py — the propose -> validate -> evaluate ->
promote -> canary -> recheck flow (dictamen §7), coordinating the pieces
that already exist rather than re-storing what they already store.

This module owns no data of its own beyond `HarnessEvolutionStore`'s two
tables. Everything else it does is calling into:

* `src.skills_runtime.bridge` / `src.skill_governance` — schema validation
  and privilege-escalation screening for `add_skill` candidates (A26), and
  `skill_governance.validate_promotion` as the SAME review gate Teach-Mode-
  sourced skills already have to pass before becoming a rule other projects
  pick up: a candidate whose `source_trace_ids` point at a Teach Mode
  session (`patch.changes.get("origin") == "teach_mode"`) cannot be
  promoted un-reviewed just because it arrived through this newer path.
* `src.context_engine.experiences` — `source_trace_ids` that look like real
  experience ids (`exp_...`) are resolved with `experiences.get` (best
  effort, never raises) so a candidate's evidence trail is checked against
  the store that already holds it, instead of harness_evolution inventing
  a second trace-id namespace. An id that does not resolve is kept as an
  opaque reference, not rejected — not every trace has to be an admitted
  experience (a raw task id from the evaluator is a trace too).
* `src.settings` — `harness_evolution_canary_runs` (default 5) is read
  once per promotion, not cached across calls, per rule 8.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from src.contracts.base import now_iso

from . import promotion, validator
from .evaluator import EvaluationOutcome, TaskRunner, evaluate
from .models import CandidatePatch, HarnessRevision
from .store import HarnessEvolutionStore, StaleParent

DEFAULT_CANARY_RUNS = 5


class HarnessEvolutionError(RuntimeError):
    pass


def _resolve_trace_ids(source_trace_ids: Sequence[str]) -> List[Dict[str, Any]]:
    from src.context_engine import experiences
    resolved: List[Dict[str, Any]] = []
    for trace_id in source_trace_ids:
        experience = experiences.get(trace_id) if trace_id else None
        resolved.append({"trace_id": trace_id, "resolved": bool(experience),
                         "kind": "experience" if experience else "opaque"})
    return resolved


class HarnessEvolutionService:
    def __init__(self, store: Optional[HarnessEvolutionStore] = None) -> None:
        self.store = store or HarnessEvolutionStore()

    # ── read ─────────────────────────────────────────────────────────────

    def list_revisions(self) -> List[HarnessRevision]:
        return self.store.list_revisions()

    def active_revision(self) -> HarnessRevision:
        return self.store.active_revision()

    def get_patch(self, patch_id: str) -> Optional[CandidatePatch]:
        return self.store.get_patch(patch_id)

    def list_patches(self) -> List[CandidatePatch]:
        return self.store.list_patches()

    # ── propose + validate (A26) ────────────────────────────────────────

    def propose_candidate(self, *, changes: Mapping[str, Any],
                          source_trace_ids: Sequence[str] = (),
                          scope: str = "", required_capabilities: Optional[Mapping[str, str]] = None,
                          parent_revision: Optional[str] = None) -> CandidatePatch:
        """Create the patch and immediately run `validator.validate` against
        it (rule: a mechanism the contract asks to gate BEFORE availability
        cannot have a window where an invalid patch sits as usable). A
        schema/reference failure still inserts the row — `status="rejected"`
        with the reason in `trace` — so a caller can see *why* something
        was refused instead of getting nothing back, but the row is never
        `validated`/`evaluated`/`promoted`, and the active revision is
        untouched either way."""
        parent = (self.store.get_revision(parent_revision) if parent_revision
                 else self.store.active_revision())
        if parent is None:
            raise HarnessEvolutionError(f"no such revision {parent_revision!r}")
        patch = CandidatePatch(
            patch_id=self.store.new_patch_id(), parent_revision=parent.revision_id,
            changes=dict(changes), source_trace_ids=list(source_trace_ids), scope=scope,
            required_capabilities=dict(required_capabilities or {}), created_at=now_iso())
        result = validator.try_validate(patch, parent=parent)
        if not result.ok:
            patch.status = "rejected"
            patch.trace = f"rejected at proposal by validator ({result.field}): {result.reason}"
            self.store.insert_patch(patch)
            return patch
        if result.manifest:
            # Fold the parsed manifest's real id/version back into the
            # change so `promotion.apply_change` records the exact skill
            # that was validated, not whatever the proposer typed.
            patch.changes["skill_id"] = result.manifest.get("id", "")
            patch.changes["skill_version"] = result.manifest.get("version", "")
        patch.status = "validated"
        patch.trace = "validated at proposal"
        self.store.insert_patch(patch)
        return patch

    # ── evaluate (A27) ──────────────────────────────────────────────────

    def evaluate_candidate(self, patch_id: str, *, runner: TaskRunner,
                           source_task_ids: Sequence[str],
                           held_out_task_ids: Sequence[str], notes: str = "") -> CandidatePatch:
        patch = self._require(patch_id)
        if patch.status != "validated":
            raise HarnessEvolutionError(
                f"patch {patch_id!r} is {patch.status!r}, not 'validated'")
        outcome = evaluate(runner=runner, source_task_ids=source_task_ids,
                           held_out_task_ids=held_out_task_ids, notes=notes)
        patch.evaluation = outcome.to_dict()
        patch.evaluation["trace_ids"] = _resolve_trace_ids(patch.source_trace_ids)
        patch.status = "evaluated" if outcome.eligible else "evaluated_failed"
        patch.trace = (
            f"evaluated at {now_iso()}: source_pass={outcome.source_pass} "
            f"held_out_pass={outcome.held_out_pass}")
        self.store.update_patch(patch)
        return patch

    # ── promote (A28/A30) ───────────────────────────────────────────────

    def _canary_runs_setting(self) -> int:
        from src.settings import get_setting
        try:
            return max(0, int(get_setting("harness_evolution_canary_runs", DEFAULT_CANARY_RUNS)))
        except (TypeError, ValueError):
            return DEFAULT_CANARY_RUNS

    def promote_candidate(self, patch_id: str, *, actor: str,
                          canary_runner: Optional[Callable[[int], bool]] = None,
                          canary_runs: Optional[int] = None) -> HarnessRevision:
        patch = self._require(patch_id)
        origin = str((patch.changes or {}).get("origin") or "").strip().lower()
        if origin:
            from src import skill_governance
            gate = skill_governance.validate_promotion(
                {"source": origin}, target_status="active",
                reviewed=bool((patch.changes or {}).get("reviewed")))
            if not gate["ok"]:
                raise HarnessEvolutionError(gate["reason"])
        runs = self._canary_runs_setting() if canary_runs is None else max(0, int(canary_runs))
        return promotion.promote(self.store, patch, actor=actor, canary_runs=runs,
                                 canary_runner=canary_runner)

    def rebase_and_retry(self, patch_id: str, stale: StaleParent, *,
                         runner: TaskRunner, source_task_ids: Sequence[str],
                         held_out_task_ids: Sequence[str]) -> CandidatePatch:
        """A30's rebase option, spelled out end to end: repoint the patch at
        the revision that won, re-validate against it (a ref the old parent
        had may not exist on the new one), and — only if that still passes —
        re-evaluate. Returns the patch in whatever state that leaves it;
        the caller re-attempts `promote_candidate` itself."""
        patch = self._require(patch_id)
        promotion.rebase(self.store, patch, stale)
        new_parent = self.store.get_revision(patch.parent_revision)
        if new_parent is None:
            raise HarnessEvolutionError("rebase target revision vanished")
        result = validator.try_validate(patch, parent=new_parent)
        if not result.ok:
            patch.status = "rejected"
            patch.trace = f"rejected on rebase revalidation ({result.field}): {result.reason}"
            self.store.update_patch(patch)
            return patch
        patch.status = "validated"
        self.store.update_patch(patch)
        return self.evaluate_candidate(patch.patch_id, runner=runner,
                                       source_task_ids=source_task_ids,
                                       held_out_task_ids=held_out_task_ids)

    # ── recheck / rollback ──────────────────────────────────────────────

    def recheck_candidate(self, patch_id: str, *, resolve_digest: Callable[[str], Optional[str]],
                          actor: str = "system") -> promotion.RecheckResult:
        patch = self._require(patch_id)
        return promotion.recheck_applicability(self.store, patch, resolve_digest=resolve_digest,
                                               actor=actor)

    def rollback_candidate(self, patch_id: str, *, actor: str, reason: str) -> HarnessRevision:
        """A manual, immediate rollback (as opposed to `recheck_candidate`'s
        automatic one) — always allowed on a `promoted` patch, restores
        `rollback_target` through the same CAS transition."""
        patch = self._require(patch_id)
        if patch.status != "promoted" or not patch.promoted_revision_id:
            raise HarnessEvolutionError(f"patch {patch_id!r} is not a promoted candidate")
        if not patch.rollback_target:
            raise HarnessEvolutionError(f"patch {patch_id!r} has no rollback_target")
        active = self.store.get_revision(patch.promoted_revision_id)
        if active is None or active.status != "active":
            active = self.store.active_revision()
        previous = self.store.get_revision(patch.rollback_target)
        if previous is None:
            raise HarnessEvolutionError(
                f"rollback_target {patch.rollback_target!r} no longer exists")
        import copy
        from .models import HarnessRevision as _Rev, new_id
        restored = _Rev(revision_id=new_id("rev"), parent_id=active.revision_id,
                        created_at=now_iso(), refs=copy.deepcopy(previous.refs),
                        status="active", version=active.version + 1)
        committed = self.store.promote_cas(parent_revision_id=active.revision_id,
                                           new_revision=restored)
        patch.status = "reverted"
        patch.trace = f"rolled back by {actor} at {now_iso()}: {reason}"
        self.store.update_patch(patch)
        return committed

    def _require(self, patch_id: str) -> CandidatePatch:
        patch = self.store.get_patch(patch_id)
        if patch is None:
            raise HarnessEvolutionError(f"no such candidate patch {patch_id!r}")
        return patch
