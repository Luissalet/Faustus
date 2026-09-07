"""state_mirror/adapters/workspace.py -- the checkout, as git and the shadow see it.

`project_state.v1` for one working tree: whether it is there at all, which
branch it is on, what has changed since the last commit, and where the shadow
checkpoint repository's own HEAD is. Nothing here is canonical; git is, and
every field below is a reading of git taken at a named time.

Three decisions, each with the failure it prevents.

**Every reading is cached, because every reading is a subprocess.**
`agent_harness.git_change_summary` runs three git commands and
`workspace_checkpoints.status` runs more, and a sweep is expected to come round
every few seconds. Without the cache below, a mirror whose whole job is to
describe the machine would become the largest single consumer of it. The cache
stores the time the git commands ACTUALLY ran, not the time the sweep asked, so
a cached answer is stamped with when it was true and `freshness` ages it
correctly instead of looking permanently new.

**A field nobody could read is absent, never false.** `git_change_summary`
answers `None` for "not a git repository, or git is not installed", and the
temptation is to turn that into `dirty=False`. That would be the system saying
"nothing to commit" about a repository it has never looked at, which is exactly
the collapse `contracts._flag` exists to prevent. When git could not be read
this adapter emits `workspace_available` and stops.

**Two checkouts that share a folder name are two projects.** The entity
identifier is the project id when the scope carries one, and otherwise the
folder name with a digest of the absolute path after it. Without the digest,
`~/work/api` and `~/clients/acme/api` would fold into one row and each sweep
would overwrite the other's branch.

What this adapter deliberately does NOT emit is written down in
`UNOBSERVED_FIELDS` below, with the reason for each.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from src.contracts.base import now_iso
from src.state_mirror.adapters.base import (
    Scope,
    ThreadedAdapter,
    entity,
    observation,
)
from src.state_mirror.contracts import StateEntity, StateObservation, entity_id

logger = logging.getLogger(__name__)

__all__ = ["WorkspaceAdapter", "UNOBSERVED_FIELDS", "SOURCE", "SCHEMA"]

#: The name every observation from this module carries, and the name a refresh
#: action names when it wants this reading taken again.
SOURCE = "workspace"

SCHEMA = "project_state.v1"

#: Fields `project_state.v1` declares that this adapter never fills, and why.
#: Kept as data rather than as prose in a docstring so a diagnostics route can
#: show a person the gap instead of leaving them to infer it from an absence.
UNOBSERVED_FIELDS: Dict[str, str] = {
    "last_verified_changeset": "src/changesets.py builds a ChangeSet and "
                               "stores nothing -- app.py says so out loud -- "
                               "so there is no last one to point at",
    "active_objectives": "the objectives adapter owns these",
    "blocked_objectives": "the objectives adapter owns these",
}

#: How long one git reading stands before the subprocesses run again. Shorter
#: than `project_state.v1.dirty`'s 60s TTL, so a sweep can still notice a
#: working tree going dirty inside the window the field is guaranteed for, and
#: long enough that a 5s sweep costs one git run in three rather than three a
#: sweep.
READ_TTL_SECONDS = 15.0

#: Passed straight through to `git_change_summary`, whose own default is 8.0.
GIT_TIMEOUT_SECONDS = 8.0

#: Characters an entity identifier may carry (contracts._ENTITY_ID_RE). Any
#: other character in a folder name becomes a dash rather than an id the
#: contract will refuse.
_UNSAFE_IDENTIFIER = re.compile(r"[^A-Za-z0-9_.@+~-]+")

_lock = threading.RLock()
#: workspace key -> (monotonic time of the read, ISO stamp of the read, reading)
_cache: Dict[str, Tuple[float, str, Dict[str, Any]]] = {}


def reset_cache() -> None:
    """Drop every cached reading. For a test, and for the doctor."""
    with _lock:
        _cache.clear()


def _slug(value: str) -> str:
    cleaned = _UNSAFE_IDENTIFIER.sub("-", str(value or "").strip()).strip("-")
    return cleaned or "workspace"


def workspace_identifier(scope: Scope) -> str:
    """The identifier half of this workspace's entity id, or `""`.

    An explicit `project_id` wins: it is what the rest of Faustus already calls
    this project, and reusing it is what lets a projection join a state row to
    a project record. Falling back, the folder name alone is not enough -- two
    checkouts called `api` are two projects -- so the absolute path's digest
    goes after it. Eight hex characters, because the digest is here to separate
    two paths a person is looking at, not to resist anybody.
    """
    project_id = str(scope.project_id or "").strip()
    if project_id:
        return _slug(project_id)
    workspace = str(scope.workspace or "").strip()
    if not workspace:
        return ""
    absolute = os.path.normcase(os.path.abspath(workspace))
    digest = hashlib.sha256(absolute.encode("utf-8", "replace")).hexdigest()[:8]
    return f"{_slug(os.path.basename(absolute.rstrip(os.sep)))}-{digest}"


def read_head(workspace: str) -> str:
    """Read the commit through the same bounded, non-interactive Git runner."""
    from src.git_invariants import current_head
    return current_head(workspace, timeout=GIT_TIMEOUT_SECONDS)


def read_branch(workspace: str) -> str:
    """The branch, or `""` when there is not one this adapter may name.

    `git_invariants.current_branch` answers `""` for a detached HEAD and for
    every way the read could fail, and it does not say which. So `""` here
    means "no branch was read", the caller omits the field, and the mirror
    says nothing rather than saying the wrong thing about a detached checkout.
    """
    from src.git_invariants import current_branch

    return current_branch(workspace, timeout=GIT_TIMEOUT_SECONDS)


def read_changes(workspace: str) -> Optional[Dict[str, Any]]:
    """`git status --porcelain` as `agent_harness` already shapes it, or None.

    None is `git_change_summary`'s own answer for "not a git repository, or git
    is unavailable", and it is passed through rather than flattened, because
    the caller has to be able to tell it from a clean tree.
    """
    from src.agent_harness import git_change_summary

    return git_change_summary(workspace, timeout=GIT_TIMEOUT_SECONDS)


def read_checkpoints(workspace: str) -> Optional[Dict[str, Any]]:
    """The shadow checkpoint repository's status, or None when it cannot be read."""
    from src import workspace_checkpoints

    return workspace_checkpoints.status(workspace)


def _read_uncached(workspace: str) -> Dict[str, Any]:
    """One pass over git and the shadow. Each source fails on its own.

    A missing shadow repository must not cost the branch, and a repository git
    refuses to read must not cost the knowledge that the folder is there, so
    each reader is caught separately and its absence recorded as None.
    """
    reading: Dict[str, Any] = {"available": os.path.isdir(workspace)}
    if not reading["available"]:
        return reading
    for key, reader in (("head", read_head),
                        ("branch", read_branch),
                        ("changes", read_changes),
                        ("checkpoints", read_checkpoints)):
        try:
            reading[key] = reader(workspace)
        except Exception as exc:                                   # noqa: BLE001
            logger.debug("workspace: %s could not be read for %s: %s",
                         key, workspace, exc)
            reading[key] = None
    return reading


def read(workspace: str) -> Tuple[str, Dict[str, Any]]:
    """`(observed_at, reading)`, running git at most once per `READ_TTL_SECONDS`.

    The stamp is the moment the subprocesses ran, so two sweeps served from one
    cached reading carry the same `observed_at` and the reducer treats the
    second as the confirmation it is rather than as news.
    """
    key = os.path.normcase(os.path.abspath(workspace))
    with _lock:
        hit = _cache.get(key)
        if hit is not None and time.monotonic() - hit[0] < READ_TTL_SECONDS:
            return hit[1], hit[2]
    stamp = now_iso()
    reading = _read_uncached(workspace)
    with _lock:
        _cache[key] = (time.monotonic(), stamp, reading)
    return stamp, reading


def _state_from(reading: Dict[str, Any]) -> Dict[str, Any]:
    """The `project_state.v1` body for one reading. Absent means not sampled.

    `dirty` comes off `changed_count` and not off `changed_paths`, because
    `git_change_summary` caps the path list at 200 and reports the full count
    beside it. A tree with 300 changes would otherwise be described by a list
    that says 200, and the count is the honest half of that pair.
    """
    state: Dict[str, Any] = {"workspace_available": bool(reading.get("available"))}
    if not state["workspace_available"]:
        return state

    branch = str(reading.get("branch") or "").strip()
    head = str(reading.get("head") or "").strip()
    if head:
        state["head"] = head
    if branch:
        state["current_branch"] = branch

    changes = reading.get("changes")
    if isinstance(changes, dict):
        paths = [str(row.get("path") or "").strip()
                 for row in (changes.get("changed") or ())
                 if isinstance(row, dict)]
        count = int(changes.get("changed_count") or 0)
        state["changed_paths"] = [p for p in paths if p]
        state["changed_count"] = count
        state["dirty"] = count > 0

    checkpoints = reading.get("checkpoints")
    if isinstance(checkpoints, dict) and checkpoints.get("present"):
        head = str(checkpoints.get("head") or "").strip()
        if head:
            state["checkpoint_head"] = head
    return state


class WorkspaceAdapter(ThreadedAdapter):
    """git plus the shadow checkpoints, as one `project_state.v1` row."""

    name = SOURCE
    schemas = (SCHEMA,)

    def _target(self, scope: Scope) -> str:
        identifier = workspace_identifier(scope)
        if not identifier:
            return ""
        return entity_id("project", scope.owner, identifier,
                         namespace=scope.namespace)

    def discover(self, scope: Scope) -> List[StateEntity]:
        identifier = self._safe(workspace_identifier, scope, default="")
        if not identifier:
            return []
        workspace = str(scope.workspace or "").strip()
        row = self._safe(
            entity, "project", identifier,
            scope=scope,
            display_name=os.path.basename(os.path.abspath(workspace)) if workspace else "",
            schema=SCHEMA,
            # The path is what a person would use to go and look for
            # themselves. It is not a secret, and section 4.2 wants the
            # canonical source named on the row rather than inferred.
            source_refs=[f"workspace:{workspace}"] if workspace else (),
            default=None,
        )
        return [row] if row is not None else []

    def observe(self, scope: Scope) -> List[StateObservation]:
        target = self._safe(self._target, scope, default="")
        workspace = str(scope.workspace or "").strip()
        if not target or not workspace:
            return []
        result = self._safe(read, workspace, default=None)
        if not result:
            return []
        observed_at, reading = result
        obs = observation(
            target, SOURCE, _state_from(reading),
            scope=scope,
            schema=SCHEMA,
            # Every value below was read off the working tree by git or by a
            # stat of the folder. Nothing here is anybody's account of it.
            epistemic="observed",
            observed_at=observed_at,
            # `project_state.v1` is not a snapshot schema: this reading sees
            # the tree, and knows nothing about the objectives another adapter
            # writes to the same row.
            partial=True,
        )
        return [obs] if obs is not None else []
