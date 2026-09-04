"""
contracts/changeset.py — "I fixed it" with the evidence attached.

The masterplan's rule for Phase 5 is one sentence: *no claim of a fix ends
without a diff and evidence appropriate to the mode.* This contract is that
sentence made checkable.

Faustus already produces every ingredient, in modules that do not know about
each other: `workspace_checkpoints` makes the diff, `project_tests` runs the
tests and separates new failures from pre-existing ones, `auto_review` reads
the diff, `git_invariants` says whether committing is safe, and `prove` turns
evidence into a verdict with named doubts. A `ChangeSet` is the envelope that
holds them **by reference** and refuses the combinations that would be a lie.

Three refusals, and each one is a shape a report has actually taken:

**A claim that names no file.** "Fixed the rate limiter" with an empty change
list is the single most common false report an agent produces, and it is
indistinguishable from a real fix in a summary. So `claims` and `files` are
compared here rather than by whoever reads the summary.

**Exactness that was not earned.** `files.source` says where the change list
came from. Only a checkpoint diff is exact; an mtime scan and a truncated
listing are not, and a ChangeSet may not say it is sure when its evidence
cannot be. Same rule as the capability registry: intent and observation are
different fields.

**A mode that promised more than it did.** An `explore` that mutated files is
a contradiction — it said it was going to look. An `implement` with no
verification is not refused, because sometimes there is no test runner, but it
cannot come out `proved`; that is what `prove` is for, and this contract will
not let a caller write the verdict by hand.

Pure, like the rest of `src/contracts`: no database, no filesystem, no model.
It holds a `checkpoint` sha rather than diff text precisely so that reading a
ChangeSet costs nothing and the diff is fetched when someone actually looks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from .base import (
    ContractError, SCHEMA_VERSION, as_mapping, fingerprint, ident, now_iso,
    one_of, reject_unknown, text, text_list, timestamp, whole,
)

#: What a turn set out to do. The mode decides what evidence is *owed*, which
#: is the whole point of naming it: a review that changed nothing is a success
#: and an implement that changed nothing is a failed one.
INTENTS = ("explore", "plan", "implement", "review", "fix")

#: Intents that are supposed to leave the workspace alone. One of these with
#: mutations in it is not a stricter result, it is a different thing than what
#: was announced.
READ_ONLY_INTENTS = ("explore", "plan", "review")

#: Where a change list came from, worst to best. Only `checkpoint` is exact:
#: a git status can miss an ignored file, and mtimes miss a rewrite that
#: restored the original bytes.
CHANGE_SOURCES = ("none", "mtime", "git", "checkpoint")

#: What a claim says it did to a path. `kind` is checked against what was
#: observed, so "created" for a file that already existed is a contradiction
#: rather than a rounding error.
CLAIM_KINDS = ("created", "modified", "deleted", "moved", "untouched")


@dataclass(frozen=True)
class FileChanges:
    """What actually happened on disk, and how sure we are of it.

    Deliberately the same shape `prove._Changes` already reads, so a ChangeSet
    can be handed to `prove()` without translation. `truncated` is not
    cosmetic: a truncated list cannot be used to contradict a claim, because
    the file might be in the part that was cut."""

    source: str = "none"
    added: Tuple[str, ...] = ()
    modified: Tuple[str, ...] = ()
    deleted: Tuple[str, ...] = ()
    checkpoint: str = ""
    truncated: bool = False

    _KEYS = ("source", "added", "modified", "deleted", "checkpoint",
             "truncated", "count")

    @property
    def paths(self) -> Tuple[str, ...]:
        return tuple(self.added) + tuple(self.modified) + tuple(self.deleted)

    @property
    def exact(self) -> bool:
        """Only a checkpoint diff is exact, and only when it was not cut."""
        return self.source == "checkpoint" and not self.truncated

    @classmethod
    def parse(cls, raw: Any, path: str = "files") -> "FileChanges":
        data = as_mapping(raw or {}, path)
        reject_unknown(data, cls._KEYS, path)
        source = one_of(data, "source", path, choices=CHANGE_SOURCES,
                        required=False, default="none")
        made = cls(
            source=source,
            added=text_list(data, "added", path, max_items=5000, max_len=500),
            modified=text_list(data, "modified", path, max_items=5000, max_len=500),
            deleted=text_list(data, "deleted", path, max_items=5000, max_len=500),
            checkpoint=text(data, "checkpoint", path, required=False, max_len=64),
            truncated=bool(data.get("truncated", False)),
        )
        if source == "checkpoint" and not made.checkpoint:
            raise ContractError(
                f"{path}.checkpoint",
                "a change list that claims to come from a checkpoint has to name "
                "it; without the sha nobody can produce the diff it is claiming "
                "to be exact about")
        return made

    def to_dict(self) -> Dict[str, Any]:
        return {"source": self.source, "count": len(self.paths),
                "added": list(self.added), "modified": list(self.modified),
                "deleted": list(self.deleted), "checkpoint": self.checkpoint,
                "truncated": self.truncated}


@dataclass(frozen=True)
class Verification:
    """Whether anything was actually run, and what it said.

    `ok` is three-valued on purpose and `None` means **not verified**, never
    "passed". That distinction is the reason this class exists rather than a
    boolean: a run with no test command and a run whose tests passed look
    identical the moment `None` is allowed to fall to `False`."""

    mode: str = "none"                 # none | tests | command | typecheck
    ran: bool = False
    ok: Optional[bool] = None
    inconclusive: bool = False
    pre_existing_only: bool = False
    command: str = ""
    summary: str = ""
    failures: Tuple[str, ...] = ()

    _KEYS = ("mode", "ran", "ok", "inconclusive", "pre_existing_only",
             "command", "summary", "failures")

    @property
    def passed(self) -> bool:
        return self.ran and self.ok is True

    @property
    def failed(self) -> bool:
        return self.ran and self.ok is False and not self.pre_existing_only

    @classmethod
    def parse(cls, raw: Any, path: str = "verification") -> "Verification":
        data = as_mapping(raw or {}, path)
        reject_unknown(data, cls._KEYS, path)
        ok = data.get("ok")
        if ok is not None and not isinstance(ok, bool):
            raise ContractError(f"{path}.ok",
                                "expected true, false, or nothing at all — and "
                                "nothing at all means NOT VERIFIED, not passed",
                                got=ok)
        ran = bool(data.get("ran", False))
        if ok is not None and not ran:
            raise ContractError(
                f"{path}.ok",
                "is set while `ran` is false; a verdict from a run that did not "
                "happen is the exact claim this contract exists to refuse")
        return cls(
            mode=text(data, "mode", path, required=False, max_len=32) or "none",
            ran=ran, ok=ok,
            inconclusive=bool(data.get("inconclusive", False)),
            pre_existing_only=bool(data.get("pre_existing_only", False)),
            command=text(data, "command", path, required=False, max_len=500),
            summary=text(data, "summary", path, required=False, max_len=1000),
            failures=text_list(data, "failures", path, max_items=200, max_len=500),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {"mode": self.mode, "ran": self.ran, "ok": self.ok,
                "inconclusive": self.inconclusive,
                "pre_existing_only": self.pre_existing_only,
                "command": self.command, "summary": self.summary,
                "failures": list(self.failures)}


@dataclass(frozen=True)
class Claim:
    """One thing the model said it did, in a form that can be checked."""

    path: str
    kind: str = "modified"
    detail: str = ""

    _KEYS = ("path", "kind", "detail")

    @classmethod
    def parse(cls, raw: Any, path: str = "claim") -> "Claim":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        return cls(
            path=text(data, "path", path, max_len=500),
            kind=one_of(data, "kind", path, choices=CLAIM_KINDS,
                        required=False, default="modified"),
            detail=text(data, "detail", path, required=False, max_len=500),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {"path": self.path, "kind": self.kind, "detail": self.detail}


@dataclass(frozen=True)
class Command:
    """Something that was run, as argv and an exit code.

    argv rather than a string, the same rule as `ExecutionSpec`: a command
    recorded as one string cannot be replayed without guessing where the
    quoting was."""

    argv: Tuple[str, ...] = ()
    exit_code: Optional[int] = None
    duration_ms: Optional[int] = None
    cwd: str = ""

    _KEYS = ("argv", "exit_code", "duration_ms", "cwd")

    @classmethod
    def parse(cls, raw: Any, path: str = "command") -> "Command":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        argv = data.get("argv")
        if isinstance(argv, str):
            raise ContractError(
                f"{path}.argv",
                "expected a list of arguments, not one string; a command kept as "
                "a string cannot be replayed without guessing the quoting",
                got=argv)
        return cls(
            argv=text_list(data, "argv", path, max_items=200, max_len=1000),
            exit_code=whole(data, "exit_code", path, minimum=-255, maximum=255),
            duration_ms=whole(data, "duration_ms", path, minimum=0),
            cwd=text(data, "cwd", path, required=False, max_len=500),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {"argv": list(self.argv), "exit_code": self.exit_code,
                "duration_ms": self.duration_ms, "cwd": self.cwd}


@dataclass(frozen=True)
class ChangeSet:
    """One turn's work, and what would convince somebody it happened."""

    id: str
    intent: str
    workspace: str = ""
    title: str = ""
    plan: str = ""
    checkpoint: str = ""
    files: FileChanges = field(default_factory=FileChanges)
    claims: Tuple[Claim, ...] = ()
    commands: Tuple[Command, ...] = ()
    verification: Verification = field(default_factory=Verification)
    review: Mapping[str, Any] = field(default_factory=dict)
    artifact_ids: Tuple[str, ...] = ()
    run_id: str = ""
    owner: str = ""
    project_id: str = ""
    created_at: str = ""
    schema_version: int = SCHEMA_VERSION

    _KEYS = ("id", "intent", "workspace", "title", "plan", "checkpoint", "files",
             "claims", "commands", "verification", "review", "artifact_ids",
             "run_id", "owner", "project_id", "created_at", "schema_version")

    @classmethod
    def parse(cls, raw: Any, path: str = "changeset") -> "ChangeSet":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)

        intent = one_of(data, "intent", path, choices=INTENTS)
        files = FileChanges.parse(data.get("files"), f"{path}.files")
        verification = Verification.parse(data.get("verification"),
                                          f"{path}.verification")
        claims = tuple(Claim.parse(c, f"{path}.claims[{i}]")
                       for i, c in enumerate(data.get("claims") or ()))
        commands = tuple(Command.parse(c, f"{path}.commands[{i}]")
                         for i, c in enumerate(data.get("commands") or ()))

        if intent in READ_ONLY_INTENTS and files.paths:
            # Not a stricter outcome — a different one than the one announced.
            # The point of naming the intent is that somebody agreed to it.
            raise ContractError(
                f"{path}.files",
                f"an intent of '{intent}' says the workspace will be left alone, "
                f"but {len(files.paths)} file(s) changed: "
                f"{', '.join(files.paths[:5])}. Say 'implement' or 'fix' if that "
                "was the plan")

        checkpoint = text(data, "checkpoint", path, required=False, max_len=64)
        if files.checkpoint and checkpoint and files.checkpoint != checkpoint:
            raise ContractError(
                f"{path}.files.checkpoint",
                f"names {files.checkpoint!r} while the change set says it started "
                f"from {checkpoint!r}; a diff against a different starting point "
                "is not evidence about this turn")

        review = data.get("review")
        if review is not None and not isinstance(review, Mapping):
            raise ContractError(f"{path}.review", "expected an object", got=review)

        return cls(
            id=text(data, "id", path, max_len=64),
            intent=intent,
            workspace=text(data, "workspace", path, required=False, max_len=1000),
            title=text(data, "title", path, required=False, max_len=200),
            plan=text(data, "plan", path, required=False, max_len=4000),
            checkpoint=checkpoint or files.checkpoint,
            files=files, claims=claims, commands=commands,
            verification=verification,
            review=dict(review or {}),
            artifact_ids=text_list(data, "artifact_ids", path,
                                   max_items=500, max_len=64),
            run_id=text(data, "run_id", path, required=False, max_len=64),
            owner=text(data, "owner", path, required=False, max_len=128),
            project_id=text(data, "project_id", path, required=False, max_len=64),
            created_at=timestamp(data, "created_at", path, default=now_iso()),
            schema_version=whole(data, "schema_version", path,
                                 default=SCHEMA_VERSION, minimum=1),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version, "id": self.id,
            "intent": self.intent, "workspace": self.workspace,
            "title": self.title, "plan": self.plan, "checkpoint": self.checkpoint,
            "files": self.files.to_dict(),
            "claims": [c.to_dict() for c in self.claims],
            "commands": [c.to_dict() for c in self.commands],
            "verification": self.verification.to_dict(),
            "review": dict(self.review),
            "artifact_ids": list(self.artifact_ids),
            "run_id": self.run_id, "owner": self.owner,
            "project_id": self.project_id, "created_at": self.created_at,
        }

    def fingerprint(self) -> str:
        """Identity of the WORK, not of the record.

        `created_at` and `id` are left out so that two reports of the same turn
        have the same fingerprint — which is what makes "you already told me
        this" answerable."""
        return fingerprint([
            ("intent", self.intent), ("workspace", self.workspace),
            ("checkpoint", self.checkpoint),
            ("files", self.files.to_dict()),
            ("claims", [c.to_dict() for c in self.claims]),
            ("commands", [c.to_dict() for c in self.commands]),
            ("verification", self.verification.to_dict()),
        ])

    # ── the checks that make a claim answerable ───────────────────────────

    def unsupported_claims(self) -> Tuple[Dict[str, Any], ...]:
        """Claims the change list does not back up.

        Empty when the evidence is not exact — a truncated or mtime-derived
        list cannot contradict anything, and reporting a claim as unsupported
        on that basis would be the same overreach in the other direction."""
        if not self.files.exact:
            return ()
        observed = {
            "created": set(self.files.added),
            "modified": set(self.files.modified),
            "deleted": set(self.files.deleted),
        }
        every = set(self.files.paths)
        problems = []
        for claim in self.claims:
            if claim.kind == "untouched":
                if _matches(claim.path, every):
                    problems.append({"path": claim.path, "claimed": claim.kind,
                                     "reason": "it changed"})
                continue
            if not _matches(claim.path, every):
                problems.append({"path": claim.path, "claimed": claim.kind,
                                 "reason": "nothing changed at that path"})
            elif not _matches(claim.path, observed.get(claim.kind, set())):
                where = next((k for k, v in observed.items()
                              if _matches(claim.path, v)), "nothing")
                problems.append({"path": claim.path, "claimed": claim.kind,
                                 "reason": f"it was {where}, not {claim.kind}"})
        return tuple(problems)

    def unclaimed_changes(self) -> Tuple[str, ...]:
        """Files that changed and nobody mentioned.

        The quieter half of the same question. A turn that edited something it
        never talked about is not necessarily wrong, but it is the thing a
        reviewer most wants pointed out."""
        if not self.files.exact or not self.claims:
            return ()
        claimed = {c.path for c in self.claims if c.kind != "untouched"}
        return tuple(p for p in self.files.paths
                     if not any(_matches(c, {p}) for c in claimed))

    def evidence_gaps(self) -> Tuple[Dict[str, str], ...]:
        """What this change set cannot account for, in `prove`'s vocabulary.

        Named the same way on purpose: these become uncertainties, and having
        two different words for the same doubt is how a report ends up looking
        more certain in one place than another."""
        gaps = []
        if self.intent in ("implement", "fix"):
            if not self.files.paths:
                gaps.append({"kind": "no_changes",
                             "detail": f"an intent of '{self.intent}' with nothing "
                                       "changed on disk"})
            if not self.verification.ran:
                gaps.append({"kind": "no_verification_runner",
                             "detail": "nothing was run to check the change"})
            elif self.verification.ok is None:
                gaps.append({"kind": "verification_inconclusive",
                             "detail": self.verification.summary
                                       or "the check ran and decided nothing"})
        if self.files.source == "mtime":
            gaps.append({"kind": "mtime_only",
                         "detail": "the change list comes from timestamps, which "
                                   "miss a rewrite that restored the original bytes"})
        if self.files.truncated:
            gaps.append({"kind": "truncated_changes",
                         "detail": "the change list was cut, so it cannot "
                                   "contradict a claim"})
        if self.verification.pre_existing_only:
            gaps.append({"kind": "pre_existing_failures",
                         "detail": "the failures were already there before this turn"})
        for problem in self.unsupported_claims():
            gaps.append({"kind": "claim_not_on_disk",
                         "detail": f"{problem['path']}: claimed {problem['claimed']}, "
                                   f"{problem['reason']}"})
        return tuple(gaps)


def _matches(claim_path: str, observed: Sequence[str]) -> bool:
    """Is this claimed path one of the observed ones?

    Suffix-tolerant, because a model says `cart.py` for `src/cart.py` all the
    time and calling that a false claim would train everyone to ignore the
    check. It is deliberately NOT prefix-tolerant: `cart.py` must not match
    `shopping_cart.py`, so the suffix has to start at a path boundary."""
    claim = claim_path.replace("\\", "/").strip("/")
    if not claim:
        return False
    for path in observed:
        real = str(path).replace("\\", "/").strip("/")
        if real == claim or real.endswith("/" + claim) or claim.endswith("/" + real):
            return True
    return False
