"""verification.py — VER-01/02/04/05/06: the single point where "the model
says it is done" gets checked against evidence, not against its own claim.

Nothing here replaces an existing authority — every function composes one
that already exists:

  * VER-01 `verify_criterion`/`mark_not_verifiable`/`waive_criterion`/
    `criteria_summary` add explicit, evidence-only states on top of
    `contracts.task.AcceptanceCriterion` (see the note below `CRITERION_STATES`
    for why this is a second vocabulary, not a widened one).
  * VER-02 `run_verifier` is a uniform front door onto `src.project_tests`
    (tests via `detect_test_command`/`run_tests`/`compare_with_baseline`,
    already battle-tested; lint/build/typecheck via the new
    `project_tests.detect_command`) — it runs nothing on its own.
  * VER-04 `material_proof` reconciles a task's claimed evidence through
    `src.prove.prove` (ChangeSet vs. an output_oracle-checked verification).
  * VER-05 `verify_citation` reads `src.claim_verify.verify`'s five-layer
    ladder as three words instead of a bool + layer number.

Stdlib only beyond the modules above. Nothing here talks to agent_loop or
Studio — see the lot report for what wiring either up would need.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from src import prove as _prove_mod
from src import project_tests
from src.claim_verify import verify as _verify_claim
from src.contracts.task import AcceptanceCriterion

# ─────────────────────────────────────────────────────────────────────────
# VER-01 — acceptance criteria with explicit, evidence-only states
# ─────────────────────────────────────────────────────────────────────────

#: Deliberately NOT `contracts.task.ACCEPTANCE_STATES` (pending/proven/unmet/
#: not_checked/not_applicable). That tuple is part of a frozen wire contract
#: this lot does not own (`AcceptanceCriterion.from_mapping` rejects anything
#: outside it, and `src/contracts/task.py` is not in this lot's file list) —
#: widening it would be an edit to a file the lot forbids touching, dressed
#: up as an addition. This is the vocabulary the lot literally asks for
#: (`pending | verified | failed | not_verifiable | waived(by, reason)`);
#: see the lot report for the two-line change to `contracts/task.py` that
#: would let `AcceptanceCriterion.state` carry it natively instead.
CRITERION_STATES = ("pending", "verified", "failed", "not_verifiable", "waived")


@dataclass(frozen=True)
class CriterionVerdict:
    """One criterion's VERIFICATION state — never the model's own checkbox.

    `state` only ever comes from one of the three functions below, each of
    which takes EVIDENCE (a pass/fail signal, a "cannot check this" reason,
    or a named human waiver). None of them has a parameter for "the model
    marked this done" — there is nothing a model's own claim could pass
    through this module that would move a criterion off `pending` by itself.
    """

    criterion_id: str
    state: str
    evidence_refs: Tuple[str, ...] = ()
    detail: str = ""
    waived_by: Optional[str] = None
    waived_reason: Optional[str] = None

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "criterion_id": self.criterion_id, "state": self.state,
            "evidence_refs": list(self.evidence_refs), "detail": self.detail,
            "waived_by": self.waived_by, "waived_reason": self.waived_reason,
        }


def _criterion_id(criterion: Any) -> str:
    if isinstance(criterion, AcceptanceCriterion):
        return criterion.id
    if isinstance(criterion, Mapping):
        return str(criterion.get("id") or "")
    return str(getattr(criterion, "id", "") or "")


def verify_criterion(criterion: Any, *, evidence_ok: Optional[bool] = None,
                     evidence_refs: Sequence[str] = (), detail: str = "") -> CriterionVerdict:
    """The only ordinary (non-waiver) way a criterion's state moves.

    `evidence_ok` must come from evidence the caller actually looked at —
    e.g. one entry of `material_proof(task)["missing"]`, or a `run_verifier`
    result — never a raw "the model says this is done" bit (this function
    has no such parameter). `None` (no evidence examined yet, or evidence
    that neither confirms nor denies) keeps the criterion `pending`: that one
    rule is the whole of the QA-19 guarantee — a checkbox the model ticks
    with nothing behind it never becomes `verified`.
    """
    cid = _criterion_id(criterion)
    if evidence_ok is None:
        return CriterionVerdict(cid, "pending", tuple(evidence_refs), detail or "no evidence examined yet")
    state = "verified" if evidence_ok else "failed"
    return CriterionVerdict(cid, state, tuple(evidence_refs), detail)


def mark_not_verifiable(criterion: Any, *, reason: str) -> CriterionVerdict:
    """The criterion cannot be checked at all (needs a human to look at a
    screenshot, a subjective judgement call, a live external system, …) —
    distinct from `pending` ("not checked YET") and from `waived` (a human
    said "skip it" for a criterion that COULD have been checked)."""
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("mark_not_verifiable requires a non-blank reason")
    return CriterionVerdict(_criterion_id(criterion), "not_verifiable", detail=reason)


def waive_criterion(criterion: Any, *, by: str, reason: str) -> CriterionVerdict:
    """A human explicitly decided this criterion does not need to hold —
    always named (`by`) and always explained (`reason`): a "waiver" with
    either blank is not a waiver, it is a state change with the evidence
    missing, which is exactly what this module exists to refuse."""
    by = (by or "").strip()
    reason = (reason or "").strip()
    if not by or not reason:
        raise ValueError("waive_criterion requires both a non-blank `by` and `reason`")
    return CriterionVerdict(_criterion_id(criterion), "waived",
                            waived_by=by, waived_reason=reason,
                            detail=f"waived by {by}: {reason}")


def criteria_summary(verdicts: Iterable[CriterionVerdict]) -> Dict[str, Any]:
    """Counts per state, and whether a task carrying these criteria may reach
    `succeeded` (contracts.task.TASK_STATES). VER-01's literal ask —
    "bloquea succeeded mientras haya alguno pendiente" — so only `pending`
    blocks; a `failed` or `not_verifiable` criterion is the caller's problem
    to resolve (retry, waive it explicitly, or settle for `partial`), not
    something this function papers over."""
    rows = list(verdicts)
    counts = {s: 0 for s in CRITERION_STATES}
    for v in rows:
        counts[v.state] = counts.get(v.state, 0) + 1
    return {
        "total": len(rows),
        **counts,
        "all_settled": counts["pending"] == 0,
        "blocks_succeeded": counts["pending"] > 0,
    }


# ─────────────────────────────────────────────────────────────────────────
# VER-02 — deterministic verifiers with a before/after baseline
# ─────────────────────────────────────────────────────────────────────────

def run_verifier(kind: str, workspace: str, *, checkpoint_sha: Optional[str] = None,
                 changed: Optional[Iterable[str]] = None, override: Optional[str] = None,
                 timeout_s: Optional[float] = None) -> Dict[str, Any]:
    """Run `kind` ("tests" | "lint" | "build" | "typecheck") after the change
    and — when `checkpoint_sha` names a pre-change tree — again before it, so
    a caller can tell a failure the patch INTRODUCED from one that predates
    it (QA-20). One uniform shape for all four kinds::

        {"kind", "ran", "ok", "inconclusive", "command", "summary",
         "baseline": {"ran", "ok", "failed": [...], "summary"} | None,
         "after":    {"ran", "ok", "failed": [...], "summary"},
         "new_failures": [...], "fixed": [...], "preexisting": [...]}

    For "tests" this is `src.project_tests` end to end — detection, the
    scoped pytest run, `compare_with_baseline` — reshaped into the above; the
    itemised failure identity (`test_file::test_name`) it already gets right
    is unchanged, and so is the constraint it inherits: the baseline diff
    only runs when `changed` names files pytest can scope to (see that
    function's docstring). For lint/build/typecheck there is usually no
    itemised failure list to diff (parsing every linter's own output format
    is out of scope here) — the baseline diff falls back to ONE synthetic id
    for the whole command, which still correctly tells "this command was
    already red at the checkpoint" from "this command just went red": the
    exact distinction QA-20 asks for, at a coarser grain.

    Never raises: an unknown `kind` or a command that cannot be detected
    comes back `ran=False, inconclusive=True` with the reason in `summary` —
    "could not verify", never a silent pass.
    """
    changed = list(changed or [])
    out: Dict[str, Any] = {
        "kind": kind, "ran": False, "ok": None, "inconclusive": True,
        "command": "", "summary": "", "baseline": None, "after": None,
        "new_failures": [], "fixed": [], "preexisting": [],
    }
    try:
        spec = project_tests.detect_command(kind, workspace, override)
    except ValueError as e:
        out["summary"] = str(e)
        return out
    if spec is None:
        out["summary"] = f"no {kind} command detected for this project"
        return out

    if kind == "tests":
        after = project_tests.run_tests(workspace, spec, changed=changed, scope="related", timeout_s=timeout_s)
    else:
        after = project_tests.run_tests(workspace, spec, timeout_s=timeout_s)
    out.update(ran=bool(after.get("ran")), ok=after.get("ok"),
               inconclusive=bool(after.get("inconclusive")),
               command=after.get("command") or "", summary=after.get("summary") or "")
    out["after"] = {"ran": after.get("ran"), "ok": after.get("ok"),
                    "failed": list(after.get("failures") or []), "summary": after.get("summary")}

    if not checkpoint_sha or not after.get("ran"):
        # No baseline possible: every after-failure stays unclassified rather
        # than being silently called "new" with nothing to compare it to.
        out["new_failures"] = list(after.get("failures") or [])
        return out

    if kind == "tests":
        compared = project_tests.compare_with_baseline(workspace, checkpoint_sha, spec, dict(after), changed=changed)
        base = compared.get("baseline") or {}
        out["baseline"] = {"ran": base.get("ran"), "ok": base.get("ok"),
                           "failed": list(base.get("failures") or []), "summary": base.get("summary")}
        out["new_failures"] = list(compared.get("new_failures") or [])
        out["preexisting"] = list(compared.get("pre_existing") or [])
        out["fixed"] = list(compared.get("fixed") or [])
        return out

    baseline = _run_baseline_tree(workspace, checkpoint_sha, spec, timeout_s=timeout_s)
    if baseline is None:
        out["new_failures"] = list(after.get("failures") or [])
        return out
    out["baseline"] = {"ran": baseline.get("ran"), "ok": baseline.get("ok"),
                       "failed": list(baseline.get("failures") or []), "summary": baseline.get("summary")}
    failed_before = bool(baseline.get("ran")) and baseline.get("ok") is False and not baseline.get("inconclusive")
    failed_after = bool(after.get("ran")) and after.get("ok") is False and not after.get("inconclusive")
    marker = f"{kind}: {spec.get('label') or spec.get('command') or kind}"
    if failed_after and failed_before:
        out["preexisting"] = [marker]
    elif failed_after and not failed_before:
        out["new_failures"] = [marker]
    elif failed_before and not failed_after:
        out["fixed"] = [marker]
    return out


def _run_baseline_tree(workspace: str, checkpoint_sha: str, spec: Dict[str, Any], *,
                       timeout_s: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Export the checkpoint tree and run `spec` there. None when the export
    itself fails (no git, unknown sha, …) — the caller then leaves every
    after-failure unclassified instead of guessing what the checkpoint held."""
    import shutil as _shutil
    import tempfile
    try:
        from src import workspace_checkpoints as wc
    except Exception:  # pragma: no cover - defensive, matches project_tests style
        return None
    tmp = tempfile.mkdtemp(prefix="odysseus-verifier-")
    try:
        if not wc.export_tree(workspace, checkpoint_sha, tmp):
            return None
        return project_tests.run_tests(tmp, dict(spec), timeout_s=timeout_s)
    finally:
        _shutil.rmtree(tmp, ignore_errors=True)


# ─────────────────────────────────────────────────────────────────────────
# VER-04 — material proof, not just a green exit code
# ─────────────────────────────────────────────────────────────────────────

def _as_list(criteria: Any) -> List[Any]:
    if criteria is None:
        return []
    if isinstance(criteria, Mapping):
        return list(criteria.values())
    if isinstance(criteria, (str, bytes)):
        return []
    try:
        return list(criteria)
    except TypeError:
        return []


def _criterion_evidence_refs(criterion: Any) -> List[str]:
    if isinstance(criterion, AcceptanceCriterion):
        return list(criterion.evidence_refs)
    if isinstance(criterion, Mapping):
        return [str(r) for r in (criterion.get("evidence_refs") or []) if r]
    return [str(r) for r in (getattr(criterion, "evidence_refs", None) or []) if r]


def material_proof(task: Any) -> Dict[str, Any]:
    """VER-04: does a "done" task have MATERIAL evidence behind it, per
    criterion — not just a verification that ran and passed.

    `task` is a mapping (or an object with matching attributes) carrying:
      - `acceptance_criteria`: an iterable of `AcceptanceCriterion` or
        `{"id", "evidence_refs": [...]}` mappings;
      - `evidence`: the ChangeSet-shaped mapping `src.prove` reads (added /
        modified / deleted / checkpoint / source) — what a checkpoint diff
        (or the harness's mtime snapshot) actually saw change on disk;
      - `verification` (optional): an output_oracle-checked verifier result
        — typically what `run_verifier`/`project_tests.run_tests` returned,
        so a bare exit 0 has already been downgraded to
        `output_oracle.EXIT_OUTPUT_MISMATCH` wherever it lied about the
        declared output before this ever sees it.

    A criterion counts as materially proved only when it names at least one
    `evidence_refs` path AND `src.prove.prove` reconciles that path against
    the observed changes as `"proved"`. This is deliberately the QA-19
    answer at the single-criterion level: tests green with NO evidence_ref
    recorded for a criterion is exactly a criterion with empty
    `evidence_refs` — the tests passing never proves it by itself, so it
    lands in `missing` and `ok` is False (the caller keeps the task
    `partial`, never `succeeded`).

    Returns `{"ok": bool, "missing": [criterion_id, ...],
    "proofs": {criterion_id: <src.prove.prove() packet>}}`. Never raises:
    `src.prove.prove` is itself total, and a criterion this function cannot
    read evidence for is simply `missing`, not a crash.
    """
    if isinstance(task, Mapping):
        criteria = _as_list(task.get("acceptance_criteria"))
        evidence = task.get("evidence")
        verification = task.get("verification")
    else:
        criteria = _as_list(getattr(task, "acceptance_criteria", None))
        evidence = getattr(task, "evidence", None)
        verification = getattr(task, "verification", None)

    missing: List[str] = []
    proofs: Dict[str, Any] = {}
    for c in criteria:
        cid = _criterion_id(c)
        refs = _criterion_evidence_refs(c)
        if not refs:
            missing.append(cid)
            continue
        proof = _prove_mod.prove(evidence, verification, {"paths": refs})
        proofs[cid] = proof
        if proof.get("verdict") != "proved":
            missing.append(cid)
    return {"ok": bool(criteria) and not missing, "missing": missing, "proofs": proofs}


# ─────────────────────────────────────────────────────────────────────────
# VER-05 — provenance a citation must earn, not just wear
# ─────────────────────────────────────────────────────────────────────────

def verify_citation(sentence: Any, source_text: Any, *, judge: Any = None) -> str:
    """Léxico + solapamiento, sin modelo por defecto (an injected `judge` is
    passed straight through to `claim_verify.verify` for the rare caller
    that also wants its layer 5). Returns one of "supported" | "weak" |
    "unsupported".

    Built directly on `claim_verify.verify`'s five-layer ladder, read as
    three words instead of a bool + layer number:

      - **"supported"**    — `verify()["supported"]` is True (layers 1-3, or
        a judge that said yes at layer 5).
      - **"unsupported"**  — an EXPLICIT contradiction: layer 4 (the source
        is missing a figure or name the sentence states) or a judge that
        said no at layer 5. This is QA-21's case: an accessible source that
        simply does not contain the cited figure is "unsupported", never a
        stamp of a verified report.
      - **"weak"**         — nothing settled it either way (`layer` is
        `None`): the claim is plausible but no deterministic layer, and no
        judge, could confirm or refute it. "weak" is this function's word
        for exactly the case `claim_verify`'s own docstring warns against
        rounding up to "supported".

    `src.research_citations` keeps its own, more permissive
    `VERDICT_SUPPORTED/REFUTED/UNCHECKED` mapping for the citation-legend UX
    (a sentence with no figures at all reads as "sin comprobar", not
    "weak") — this function does not replace it, it is the plain
    three-tier primitive VER-05 asks for, usable directly on a
    `(sentence, source_text)` pair without a report or a source registry.
    """
    result = _verify_claim(sentence, source_text, judge=judge)
    if result.get("supported"):
        return "supported"
    if result.get("layer") in (4, 5):
        return "unsupported"
    return "weak"
