"""Isolation boundary for future branch executors.

W3-D closes the gap `docs/adaptations/decisions/CMP-13.md` names explicitly:
this module used to have exactly ONE implementation of `BranchIsolator`
(`InMemoryIsolator`, whose own docstring said so: "no filesystem or network
capability... before a real isolator is trusted"). `WorktreeIsolator` and
`SnapshotDirIsolator` below are that real isolator -- thin adapters over the
SAME mechanisms `src.alternatives`/`src.git_panel` already proved out for
CMP-13 (`git worktree add/remove` with hooks neutralised, and a frozen
`shutil.copytree` for a workspace with no git history), reused here under
this package's own `Protocol` names instead of a parallel reimplementation.
`isolator_for(workspace)` picks between them (and `InMemoryIsolator` for a
blank/non-directory `workspace`, i.e. today's plan/simulation branches,
which is byte-for-byte the behaviour every existing caller already gets).

Nothing in `service.py` calls `fork`/`inspect`/`cleanup` yet -- `create`/
`start_branch` there still only track a LOGICAL `namespace` string, per that
module's own comments. This module makes a REAL isolator constructible and
injectable (`BranchingService(isolator=...)`); wiring an actual fork into
branch execution is future work, not a silent side effect of this lote.
"""
from __future__ import annotations

import copy
import os
import shutil
from typing import Any, Dict, Mapping, Optional, Protocol


class BranchIsolator(Protocol):
    async def fork(self, snapshot: Mapping[str, Any], branch: Mapping[str, Any]) -> Dict[str, Any]: ...
    async def inspect(self, handle: Mapping[str, Any]) -> Dict[str, Any]: ...
    async def cleanup(self, handle: Mapping[str, Any]) -> Dict[str, Any]: ...


class InMemoryIsolator:
    """No-effect isolator for fixtures and plan/simulation branches."""

    def __init__(self) -> None:
        self._spaces: Dict[str, Dict[str, Any]] = {}

    async def fork(self, snapshot: Mapping[str, Any], branch: Mapping[str, Any]) -> Dict[str, Any]:
        namespace = str(branch.get("namespace") or "")
        if not namespace or namespace in self._spaces:
            raise ValueError("branch namespace must be new and non-blank")
        self._spaces[namespace] = {"snapshot": copy.deepcopy(dict(snapshot)), "outputs": {}}
        return {"namespace": namespace, "real_external_effects": False}

    async def inspect(self, handle: Mapping[str, Any]) -> Dict[str, Any]:
        namespace = str(handle.get("namespace") or "")
        return copy.deepcopy(self._spaces.get(namespace, {}))

    async def cleanup(self, handle: Mapping[str, Any]) -> Dict[str, Any]:
        namespace = str(handle.get("namespace") or "")
        removed = self._spaces.pop(namespace, None) is not None
        return {"removed": removed, "namespace": namespace}


def _isolation_root() -> str:
    from src.constants import DATA_DIR
    return os.path.join(DATA_DIR, "branching_futures", "isolation")


def _safe_namespace(namespace: str) -> str:
    """A branch `namespace` (e.g. `"branch:<future_id>/<branch_id>"`) is not
    a filesystem-safe path segment on its own -- flatten it the same way a
    caller would flatten any untrusted-ish identifier into one directory
    name, never nested (so `cleanup` can `shutil.rmtree`/`worktree remove`
    exactly one path with no risk of climbing back out of the isolation
    root)."""
    return namespace.replace(os.sep, "_").replace("/", "_").replace(":", "_")


class WorktreeIsolator:
    """Real filesystem isolation for a git-backed workspace: `fork` is
    `src.git_panel.worktree_add` (hooks neutralised via `-c core.hooksPath=`,
    see that function's own docstring), `cleanup` is `worktree_remove`. Two
    branches forked from the same snapshot -- and the workspace's own main
    checkout -- can each hold different uncommitted content at the same
    time because a git worktree IS a second working directory sharing one
    object store; nothing here diffs or merges branches back together
    (that stays `src.alternatives.apply_alternative`'s job, a different
    layer -- see CMP-13.md's "componer, no integrar").
    """

    def __init__(self, root: Optional[str] = None) -> None:
        self._root = root or _isolation_root()

    async def fork(self, snapshot: Mapping[str, Any], branch: Mapping[str, Any]) -> Dict[str, Any]:
        from src import git_panel

        workspace = str(snapshot.get("workspace") or "").strip()
        base_ref = str(snapshot.get("base_ref") or "HEAD").strip() or "HEAD"
        namespace = str(branch.get("namespace") or "").strip()
        if not workspace or not os.path.isdir(workspace):
            raise ValueError("snapshot['workspace'] must be an existing directory")
        if not namespace:
            raise ValueError("branch namespace must be new and non-blank")
        worktree_dir = os.path.join(self._root, _safe_namespace(namespace))
        try:
            path = git_panel.worktree_add(workspace, worktree_dir, base_ref)
        except git_panel.GitWorktreeError as exc:
            raise RuntimeError(
                f"worktree_add failed: {git_panel.stderr_snippet(exc.stderr)}"
            ) from exc
        return {"namespace": namespace, "path": path, "workspace": workspace,
                "real_external_effects": True}

    async def inspect(self, handle: Mapping[str, Any]) -> Dict[str, Any]:
        path = str(handle.get("path") or "")
        return {"namespace": handle.get("namespace"), "path": path,
                "exists": bool(path) and os.path.isdir(path)}

    async def cleanup(self, handle: Mapping[str, Any]) -> Dict[str, Any]:
        from src import git_panel

        workspace = str(handle.get("workspace") or "")
        path = str(handle.get("path") or "")
        removed = False
        if workspace and path:
            try:
                git_panel.worktree_remove(workspace, path, force=True)
                removed = True
            except git_panel.GitWorktreeError:
                removed = False
        return {"removed": removed, "namespace": handle.get("namespace")}


class SnapshotDirIsolator:
    """Real filesystem isolation for a workspace with no git history: `fork`
    takes an independent `shutil.copytree` of `snapshot['workspace']` (or a
    frozen `snapshot['base_dir']` the caller already took, when the live
    workspace keeps changing) -- the same "no git history to fall back on"
    reasoning `src.alternatives.create_experiment` documents for its own
    `snapshot_dir` isolation. Every fork gets its own directory under the
    isolation root, so it can never collide with another branch's."""

    def __init__(self, root: Optional[str] = None) -> None:
        self._root = root or _isolation_root()

    async def fork(self, snapshot: Mapping[str, Any], branch: Mapping[str, Any]) -> Dict[str, Any]:
        source = str(snapshot.get("base_dir") or snapshot.get("workspace") or "").strip()
        namespace = str(branch.get("namespace") or "").strip()
        if not source or not os.path.isdir(source):
            raise ValueError("snapshot['workspace'] (or ['base_dir']) must be an existing directory")
        if not namespace:
            raise ValueError("branch namespace must be new and non-blank")
        target = os.path.join(self._root, _safe_namespace(namespace))
        if os.path.exists(target):
            raise ValueError(f"branch namespace {namespace!r} already has an isolated copy")
        os.makedirs(self._root, exist_ok=True)
        shutil.copytree(source, target)
        return {"namespace": namespace, "path": target, "real_external_effects": True}

    async def inspect(self, handle: Mapping[str, Any]) -> Dict[str, Any]:
        path = str(handle.get("path") or "")
        return {"namespace": handle.get("namespace"), "path": path,
                "exists": bool(path) and os.path.isdir(path)}

    async def cleanup(self, handle: Mapping[str, Any]) -> Dict[str, Any]:
        path = str(handle.get("path") or "")
        removed = bool(path) and os.path.isdir(path)
        if removed:
            shutil.rmtree(path, ignore_errors=True)
        return {"removed": removed, "namespace": handle.get("namespace")}


def isolator_for(workspace: Optional[str]) -> "BranchIsolator":
    """Pick the isolator CMP-13's own mechanism already proves works, by
    what `workspace` actually is -- mirrors `src.alternatives.
    create_experiment`'s own git-repo-or-plain-directory split:

      * a git working tree      -> `WorktreeIsolator`
      * an existing plain dir   -> `SnapshotDirIsolator`
      * blank / not a directory -> `InMemoryIsolator` (today's plan/
        simulation branches, unchanged: no real workspace, no real effect).
    """
    workspace = (workspace or "").strip()
    if not workspace or not os.path.isdir(workspace):
        return InMemoryIsolator()
    from src import git_panel

    repo_root = git_panel.repo_toplevel(workspace)
    if repo_root:
        return WorktreeIsolator()
    return SnapshotDirIsolator()
