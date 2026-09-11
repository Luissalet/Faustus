"""
skill_import_review.py — ADP-25: a skill discovered under a project's
`.odysseus/skills` (or `.agents/skills`, `.claude/skills`) folder is content
someone else wrote; sitting next to an already-approved skill must not be
enough to run it.

The gate this module protects: `src.skills_runtime.discovery` and `.bridge`
will happily turn ANY `SKILL.md` in one of the three watched folders into a
`SkillManifest` — that is their whole job, and it is correct, because listing
and describing a skill is a read. What must NOT follow automatically from "it
parses" is "it may run" or "the agent may treat it as a granted capability":
the masterplan's non-negotiable is that a skill's own words never grant it
more than a human explicitly signed off on, and "explicitly" here means
pinned to exact bytes, not to a name or a folder.

Three separate facts, tracked separately, on purpose:

* **digest** — sha256 of the SKILL.md plus every sibling file in its folder
  (`skills_runtime.discovery.skill_digest`). Any byte anywhere in the skill's
  own folder changing produces a different digest — there is no field-by-
  field allowlist of "safe" changes that skip review.
* **tools_required** — `manifest.permissions.backends`, tracked as its own
  column even though it is already covered by the digest, because the
  acceptance criterion asks for it to be checked explicitly: a caller that
  wants to know WHY a skill needs re-review (prose edited vs. a backend
  added) can tell from this instead of re-diffing bytes.
* **privilege request** — a skill's frontmatter asking to turn down the
  approval system around it (`disabled_tools`, `tool_approval_mode`, …) is
  not something this module can approve at any digest: `approve()` refuses it
  outright, the same way `Permissions.parse` refuses an unknown key.

Nothing here executes a skill or grants it a backend — `execution_router`
still does that, and `src/workflows/skills.py::run` is today's only caller
that turns a discovered skill into an effect. This module is the one
question that caller (or any future one) needs to ask first: does what is
about to run match what a human looked at and said yes to. Wiring that call
in is out of this module's file ownership for this change — see the ADP-25
report for the exact call site and the one-line hook it still needs.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from difflib import unified_diff
from typing import Any, Dict, List, Mapping, Optional

from core.atomic_io import atomic_write_json, atomic_write_text
from src.constants import DATA_DIR

APPROVALS_FILE = os.path.join(DATA_DIR, "skill_approvals.json")
#: Copies of the exact SKILL.md text that was approved, so `diff()` can show
#: what changed instead of only "the hash no longer matches". Kept out of
#: `skill_approvals.json` itself so that file stays small and greppable.
SNAPSHOT_DIR = os.path.join(DATA_DIR, "skill_approvals")

#: Frontmatter keys that ask to weaken the tool-approval system itself,
#: rather than declare a capability the skill needs (that is `permissions_*`,
#: and is a normal, reviewable request — see `skills_runtime.bridge`).
#: Presence of ANY of these is refused outright, at any value: naming the key
#: is itself the request, and there is no digest at which turning off
#: approval for the *user's other tools* becomes something a skill gets to
#: ask for. Matches the real settings these names shadow
#: (`routes/model_routes.py`'s `disabled_tools`, `routes/auth_routes.py`'s
#: `tool_approval_mode`) plus the obvious synonyms an author might reach for.
PRIVILEGE_REQUEST_KEYS = frozenset({
    "disabled_tools", "tool_approval_mode", "disable_approval", "skip_approval",
    "bypass_approval", "approval_mode", "auto_approve", "require_admin",
    "tool_approval", "approval_required", "skip_review",
})

__all__ = [
    "APPROVALS_FILE", "SNAPSHOT_DIR", "PRIVILEGE_REQUEST_KEYS",
    "SkillReviewError", "ReviewStatus",
    "load_approvals", "get_approval", "tools_required_of",
    "privilege_request_keys", "status_of", "review", "approve", "diff",
]


class SkillReviewError(Exception):
    """Carries a stable `error_class` for the route layer — the same shape
    `routes/board_routes.py`'s `BoardError` gives its callers — so a refusal
    is always a named 4xx, never a bare 500 a caller has to string-match."""

    def __init__(self, error_class: str, message: str):
        super().__init__(message)
        self.error_class = error_class
        self.message = message


@dataclass(frozen=True)
class ReviewStatus:
    state: str            # approved | needs_review | unreviewed
    reason: str
    approval: Optional[Dict[str, Any]]

    def to_dict(self) -> Dict[str, Any]:
        return {"state": self.state, "reason": self.reason, "approval": self.approval}


def _safe_id(skill_id: str) -> str:
    """Filesystem-safe stem for the snapshot file. The authoritative mapping
    stays in `skill_approvals.json`; this is only ever a cache key, so a hash
    suffix is enough to avoid two different ids colliding after sanitizing."""
    digest = hashlib.sha256(skill_id.encode("utf-8")).hexdigest()[:16]
    stem = "".join(c if c.isalnum() or c in "-_." else "_" for c in skill_id)[:80]
    return f"{stem}.{digest}"


def _snapshot_path(skill_id: str) -> str:
    return os.path.join(SNAPSHOT_DIR, _safe_id(skill_id) + ".md")


def load_approvals() -> Dict[str, Dict[str, Any]]:
    """Every recorded approval, keyed by skill id. A missing or corrupt file
    reads as empty — the same "absence is not a crash" rule as every other
    `DATA_DIR` JSON store in this repo. A skill with no entry here is simply
    `unreviewed`, not an error."""
    try:
        with open(APPROVALS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def get_approval(skill_id: str) -> Optional[Dict[str, Any]]:
    return load_approvals().get(skill_id)


def tools_required_of(manifest: Any) -> List[str]:
    """What ADP-25 calls "herramientas requeridas": the execution backends
    the manifest declares (`permissions.backends`) — the only place a
    `SkillManifest` names what it needs in order to run. Network/filesystem/
    host access are tracked in the manifest's own permissions and shown
    alongside this in `review()`, not folded into it."""
    return sorted(set(manifest.permissions.backends))


def privilege_request_keys(frontmatter: Mapping[str, Any]) -> List[str]:
    fm = frontmatter or {}
    return sorted(k for k in fm if k in PRIVILEGE_REQUEST_KEYS)


def _frontmatter(manifest_text: str) -> Dict[str, Any]:
    from services.memory.skill_format import parse_frontmatter
    try:
        fm, _body = parse_frontmatter(manifest_text or "")
    except Exception:
        return {}
    return fm if isinstance(fm, dict) else {}


def status_of(skill_id: str, *, digest: str, tools_required: List[str]) -> ReviewStatus:
    """The gate itself: approved / needs_review / unreviewed, and why.

    Reusable outside the HTTP layer on purpose — this is the function a
    loader calls before treating a discovered skill as runnable, not
    something reimplemented per caller. A digest that fails to compute
    (`skills_runtime.discovery.skill_digest`'s `error:` strings) is compared
    like any other digest: it will not match a prior approval computed from
    real content, so it reads as `needs_review`, never as trivially approved.
    """
    approval = get_approval(skill_id)
    if approval is None:
        return ReviewStatus("unreviewed", "no approval on file for this skill id", None)
    if approval.get("digest") != digest:
        return ReviewStatus(
            "needs_review",
            "content changed since the last approval (hash mismatch)", approval)
    approved_tools = sorted(approval.get("tools_required") or [])
    if approved_tools != sorted(tools_required):
        return ReviewStatus(
            "needs_review",
            "required tools changed since the last approval", approval)
    return ReviewStatus("approved", "matches the approved digest and tools", approval)


def review(*, skill_id: str, origin: str, manifest: Any, manifest_text: str,
          digest: str) -> Dict[str, Any]:
    """Everything a human needs before approving: origin, version, hash,
    tools required, declared permissions, any privilege request, and the
    current verdict against whatever was approved before (if anything)."""
    fm = _frontmatter(manifest_text)
    tools = tools_required_of(manifest)
    status = status_of(skill_id, digest=digest, tools_required=tools)
    return {
        "id": skill_id,
        "origin": origin,
        "version": manifest.version,
        "digest": digest,
        "tools_required": tools,
        "permissions": manifest.permissions.to_dict(),
        "privilege_request": privilege_request_keys(fm),
        "status": status.to_dict(),
    }


def approve(*, skill_id: str, manifest: Any, manifest_text: str, digest: str,
           by: str) -> Dict[str, Any]:
    """Record an approval pinned to `digest`.

    Refuses outright — never "approves with a warning" — a skill whose
    frontmatter asks to weaken the approval system around itself
    (`PRIVILEGE_REQUEST_KEYS`); that request cannot be granted at any digest,
    so there is nothing a re-approval could fix without the author editing
    the skill to withdraw it.
    """
    fm = _frontmatter(manifest_text)
    flags = privilege_request_keys(fm)
    if flags:
        raise SkillReviewError(
            "skills.privilege_request",
            "skill frontmatter asks to change approval/tool behaviour "
            f"({', '.join(flags)}); refused",
        )
    tools_required = tools_required_of(manifest)
    entry = {
        "digest": digest,
        "approved_at": time.time(),
        "tools_required": tools_required,
        "by": by or "unknown",
        "version": manifest.version,
    }
    all_approvals = load_approvals()
    all_approvals[skill_id] = entry
    atomic_write_json(APPROVALS_FILE, all_approvals, indent=2)
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    atomic_write_text(_snapshot_path(skill_id), manifest_text or "")
    return dict(entry)


def diff(*, skill_id: str, manifest_text: str) -> Dict[str, Any]:
    """Unified diff of the current `SKILL.md` text against the last approved
    copy. No approval on file yet reads as "no diff to show" with a reason,
    not an error — a caller needs to tell "nothing changed" apart from
    "nothing has ever been approved"."""
    approval = get_approval(skill_id)
    if approval is None:
        return {"has_approved": False, "diff": "", "reason": "no approved version on file"}
    path = _snapshot_path(skill_id)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            approved_text = fh.read()
    except OSError:
        return {"has_approved": False, "diff": "",
                "reason": "approval recorded but its snapshot is missing; approve again"}
    lines = unified_diff(
        approved_text.splitlines(keepends=True),
        (manifest_text or "").splitlines(keepends=True),
        fromfile=f"{skill_id}@approved", tofile=f"{skill_id}@current",
    )
    return {"has_approved": True, "diff": "".join(lines), "reason": ""}
