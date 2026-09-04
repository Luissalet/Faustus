"""
changesets.py — assemble the evidence Faustus already had, in one shape.

The contract in `contracts/changeset.py` is pure. This is the part that knows
where the pieces live: the checkpoint that produced the diff, the test run
that produced the verdict, the review that read the diff, and `prove`, which
turns all of it into a verdict with named doubts.

It is deliberately thin, and it recomputes **nothing**:

* the diff comes from `workspace_checkpoints.diff_since`, and is fetched only
  when somebody asks — a ChangeSet holds the sha, not four hundred kilobytes
  of text nobody has read;
* the change list, the verification block and the claims are the shapes
  `prove` already parses, so the verdict is `prove.prove()` and not a fifth
  vocabulary. Faustus already has four words for "did it work" and adding one
  more would be the whole problem, not the solution;
* per-file accept and reject stays in `services/review_state`. A ChangeSet is
  the report; what a person did about it is a different question with a
  different lifetime.

The one thing this module adds is the **claim check**: what the model said it
touched, against what the checkpoint says changed. That comparison already
existed in three places with three shapes (`agent_harness.claimed_untouched_paths`,
`dispatch.claimed_only`, `prove._claims`); here it has one.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Mapping, Optional, Sequence

from src.contracts import ChangeSet, ContractError
from src.contracts.base import now_iso

logger = logging.getLogger(__name__)


def build(*, intent: str, workspace: str = "", checkpoint: str = "",
          changes: Optional[Mapping[str, Any]] = None,
          verification: Optional[Mapping[str, Any]] = None,
          claims: Optional[Sequence[Mapping[str, Any]]] = None,
          commands: Optional[Sequence[Mapping[str, Any]]] = None,
          review: Optional[Mapping[str, Any]] = None,
          plan: str = "", title: str = "", run_id: str = "",
          owner: str = "", project_id: str = "",
          artifact_ids: Optional[Sequence[str]] = None,
          changeset_id: str = "") -> ChangeSet:
    """One change set out of the blocks the rest of Faustus already produces.

    `changes` is `dispatch._changes_block`'s shape, `verification` is
    `dispatch._verify`'s (or `project_tests.compact()`'s), and both are passed
    through rather than reshaped — a translation layer here would be a second
    place for the two to drift apart."""
    return ChangeSet.parse({
        "id": changeset_id or f"chg_{uuid.uuid4().hex[:20]}",
        "intent": intent,
        "workspace": workspace,
        "title": title,
        "plan": plan,
        "checkpoint": checkpoint,
        "files": _changes_block(changes),
        "claims": [dict(c) for c in (claims or ())],
        "commands": [dict(c) for c in (commands or ())],
        "verification": _verification_block(verification),
        "review": dict(review or {}),
        "artifact_ids": list(artifact_ids or ()),
        "run_id": run_id, "owner": owner, "project_id": project_id,
        "created_at": now_iso(),
    })


def _changes_block(raw: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Keep only the keys the contract knows, and keep them as they were.

    `dispatch` and `project_tests` both carry a few extra keys for their own
    use. Dropping them here rather than widening the contract keeps the
    envelope narrow — a ChangeSet is what someone reads to decide whether to
    believe a report, not a dump of everything that was measured."""
    data = dict(raw or {})
    return {k: data[k] for k in
            ("source", "added", "modified", "deleted", "checkpoint", "truncated")
            if k in data}


def _verification_block(raw: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    data = dict(raw or {})
    return {k: data[k] for k in
            ("mode", "ran", "ok", "inconclusive", "pre_existing_only",
             "command", "summary", "failures")
            if k in data}


# ── the verdict, delegated ────────────────────────────────────────────────

def judge(changeset: ChangeSet) -> Dict[str, Any]:
    """Hand the change set to `prove` and return its proof.

    Deliberately not a method on the contract: `prove` reads settings and the
    contract package touches nothing. And deliberately not a new verdict:
    `proved | partial | unproved | contradicted` with a confidence and named
    doubts already exists and is the rigorous one."""
    from src import prove as prove_mod

    evidence = {**changeset.files.to_dict(), "checkpoint": changeset.checkpoint}
    verification = changeset.verification.to_dict()
    claims = [{"kind": _claim_kind(c.kind), "path": c.path}
              for c in changeset.claims if c.kind != "untouched"]

    proof = prove_mod.prove(evidence, verification, claims)

    # The gaps the contract can see for itself — an `explore` that verified
    # nothing is fine, an `implement` that did is not — are folded in here so
    # one report does not sound surer in one field than in another.
    extra = [g for g in changeset.evidence_gaps()
             if not any(u.get("kind") == g["kind"]
                        for u in proof.get("uncertainty") or ())]
    if extra:
        proof = dict(proof)
        proof["uncertainty"] = list(proof.get("uncertainty") or ()) + extra
    return proof


def _claim_kind(kind: str) -> str:
    """`prove` speaks of files and dirs; the contract speaks of what happened
    to them. A deletion is still a claim about a path, so it maps across."""
    return "file"


# ── reading one ───────────────────────────────────────────────────────────

def diff_of(changeset: ChangeSet, *, path: str = "",
            max_chars: int = 400_000) -> Dict[str, Any]:
    """The actual diff, fetched now rather than stored then.

    A ChangeSet holds the checkpoint sha precisely so this can be a separate,
    expensive call. Answers `{ok, diff, source, reason}` — and when the
    checkpoint is gone it says so, because a missing diff and an empty diff
    are opposite facts."""
    from src import workspace_checkpoints

    if not changeset.checkpoint:
        return {"ok": False, "reason": "no_checkpoint", "diff": "",
                "detail": "this change set was not anchored to a checkpoint, so "
                          "there is nothing to diff against"}
    if not changeset.workspace:
        return {"ok": False, "reason": "no_workspace", "diff": ""}
    if not workspace_checkpoints.has_checkpoint(changeset.workspace,
                                                changeset.checkpoint):
        # Every read in `workspace_checkpoints` answers an unknown sha with
        # the same empty result it gives for "nothing changed", and those are
        # opposite facts. Reporting "no diff" here would quietly turn a
        # checkpoint from another data directory into "the work did nothing".
        return {"ok": False, "reason": "unknown_checkpoint", "diff": "",
                "checkpoint": changeset.checkpoint,
                "detail": f"{changeset.workspace} has no checkpoint "
                          f"{changeset.checkpoint[:12]}; it may belong to a "
                          "different data directory, or a reset threw it away. "
                          "An empty diff would be a different claim."}
    try:
        text = workspace_checkpoints.diff_since(
            changeset.workspace, changeset.checkpoint,
            path=path or None, max_chars=max_chars)
    except Exception as e:                      # a missing shadow repo is a fact
        return {"ok": False, "reason": "unreadable", "diff": "",
                "detail": f"{type(e).__name__}: {e}"}
    return {"ok": True, "diff": text or "", "source": "checkpoint",
            "checkpoint": changeset.checkpoint,
            "empty": not (text or "").strip()}


def render(changeset: ChangeSet, proof: Optional[Mapping[str, Any]] = None) -> str:
    """One screen a person reads before believing the summary.

    Order matters: the verdict first, then what is missing, then what changed.
    Somebody scanning this is asking "can I trust the sentence above" — and
    the answer to that is the doubts, not the file list."""
    from src import prove as prove_mod

    proof = proof if proof is not None else judge(changeset)
    lines = [f"{changeset.id} · {changeset.intent}"
             + (f" · {changeset.title}" if changeset.title else "")]
    lines.append("  " + prove_mod.line(proof))

    verification = changeset.verification
    if verification.ran:
        state = ("passed" if verification.passed else
                 "FAILED" if verification.failed else "inconclusive")
        detail = verification.summary or verification.command or ""
        lines.append(f"  {verification.mode}: {state}"
                     + (f" — {detail}" if detail else ""))
        for failure in verification.failures[:5]:
            lines.append(f"      {failure}")
    else:
        lines.append("  nothing was run to check this")

    files = changeset.files
    if files.paths:
        lines.append(f"  {len(files.paths)} file(s) changed [{files.source}"
                     + (", TRUNCATED" if files.truncated else "") + "]")
        for path in list(files.added)[:10]:
            lines.append(f"      + {path}")
        for path in list(files.modified)[:10]:
            lines.append(f"      ~ {path}")
        for path in list(files.deleted)[:10]:
            lines.append(f"      - {path}")
    else:
        lines.append("  nothing changed on disk")

    for problem in changeset.unsupported_claims():
        lines.append(f"  CLAIMED BUT NOT SEEN: {problem['path']} "
                     f"({problem['claimed']}) — {problem['reason']}")
    unclaimed = changeset.unclaimed_changes()
    if unclaimed:
        lines.append(f"  changed without being mentioned: "
                     f"{', '.join(unclaimed[:8])}")

    review = changeset.review or {}
    if review.get("verdict"):
        errors = [f for f in review.get("findings") or []
                  if f.get("severity") == "error"]
        lines.append(f"  review: {review['verdict']}"
                     + (f" · {len(errors)} error finding(s)" if errors else ""))
    if changeset.commands:
        shown = ", ".join(" ".join(c.argv)[:60] for c in changeset.commands[:3])
        lines.append(f"  ran: {shown}")
    if changeset.artifact_ids:
        lines.append(f"  artifacts: {', '.join(changeset.artifact_ids[:5])}")
    return "\n".join(lines)


# ── from the records Faustus already keeps ────────────────────────────────

def from_turn(summary: Mapping[str, Any], *, intent: str = "implement",
              workspace: str = "", claims: Optional[Sequence[Mapping[str, Any]]] = None,
              **extra: Any) -> ChangeSet:
    """A `TurnLedger.summary()` → a ChangeSet.

    The ledger records what the turn DID; the change set is what would
    convince somebody of it. Most of the mapping is a rename, which is the
    point: this is a shape, not a new measurement."""
    tests = dict(summary.get("tests") or {})
    checkpoint = summary.get("checkpoint") or ""
    if isinstance(checkpoint, Mapping):
        checkpoint = checkpoint.get("sha") or ""

    mutated = list(summary.get("mutations") or ())
    changes = {"source": "checkpoint" if checkpoint else "none",
               "modified": mutated, "checkpoint": checkpoint}
    verification = {
        "mode": "tests" if tests.get("ran") else "none",
        "ran": bool(tests.get("ran")),
        "ok": tests.get("ok"),
        "pre_existing_only": bool(tests.get("pre_existing_only")),
        "command": tests.get("command") or "",
        "summary": tests.get("summary") or "",
        "failures": list(tests.get("failures") or ())[:50],
    }
    if not verification["ran"]:
        verification.pop("ok", None)      # a verdict from a run that did not happen

    return build(intent=intent, workspace=workspace, checkpoint=checkpoint,
                 changes=changes, verification=verification,
                 claims=claims or [], review=summary.get("review") or {},
                 **extra)


def from_dispatch(compact: Mapping[str, Any], *, intent: str = "implement",
                  workspace: str = "", **extra: Any) -> ChangeSet:
    """A `dispatch.compact(job)` → a ChangeSet.

    Dispatch already does the hard half — it overwrites the workers' claimed
    file list with what Faustus SAW on disk, and keeps the difference in
    `claimed_only`. That difference is exactly what becomes a claim here, so
    the check has something to be wrong about."""
    changes = dict(compact.get("changes") or {})
    claims = [{"path": p, "kind": "modified"}
              for p in (compact.get("claimed_only") or ())]
    return build(intent=intent, workspace=workspace,
                 checkpoint=str(changes.get("checkpoint") or ""),
                 changes=changes,
                 verification=compact.get("verification") or {},
                 claims=claims,
                 title=str(compact.get("title") or ""),
                 run_id=str(compact.get("id") or ""),
                 **extra)
