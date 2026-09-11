"""git_panel.py -- backend for the version-control panel (OBJ-4).

Discovers git repositories under a project's linked folders, and runs `git`
against them to answer VS Code's Source Control view: status, log, branches,
commit detail/diff, and the mutating operations (checkout, branch, fetch,
pull, push, sync, stage/unstage/discard, commit).

Design notes
------------
* **Identity is a content hash of the resolved path**: ``sha1(normcase(
  realpath(repo_root)))[:12]``. Stable across calls, and -- combined with
  discovery being the ONLY way a `repo_id` is ever looked up (there is no
  "trust me, this is a repo path" entry point) -- it is what makes a repo
  outside the owner's linked folders answer 404 for free: if discovery never
  walks there, its id never exists as far as any route is concerned.

* **`git` is never run with a shell** (`subprocess.run([...])`, no
  ``shell=True``), every invocation gets a real ``argv`` and an explicit
  ``cwd``, and every command carries the same hardening
  ``routes/workspace_routes.py`` already uses for its own git calls (see its
  "the repo being viewed must not choose what runs" note): ``-c
  core.fsmonitor=``, ``-c diff.external=`` and ``--no-ext-diff`` on the
  commands that run a diff, so a `.git/config` from a cloned/agent-written
  repo cannot execute anything when the panel opens it.

* **The environment is built here, not reused from `workspace_checkpoints._git_env`
  or `workspace_routes._git_env`.** Both of those hardcode
  ``GIT_AUTHOR_NAME``/``GIT_COMMITTER_NAME`` for their own shadow-checkpoint
  commits -- exactly right there, exactly wrong here: this module commits
  using the *repository's own configured* identity (the contract is explicit:
  "usa el user configurado del repo, nunca lo sobrescribe"), and must be able
  to tell a genuinely unconfigured repo apart from one whose identity we
  silently supplied. So `_git_env()` below strips the venv/GIT_DIR markers
  the same way, via the same shared `native_host_environment`, but injects no
  author identity of its own.

Stdlib only, save for `src.native_env.native_host_environment` (already the
shared "environment for a child that is not ours" helper).
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.native_env import native_host_environment

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Timeouts (contract: 60s default, 120s for the network ops)
# ---------------------------------------------------------------------------
GIT_TIMEOUT_DEFAULT = 60.0
GIT_TIMEOUT_LONG = 120.0

# ---------------------------------------------------------------------------
# Discovery limits
# ---------------------------------------------------------------------------
MAX_REPOS = 60
MAX_DEPTH = 3
_SKIP_DIR_NAMES = frozenset({"node_modules", ".venv", "venv", "__pycache__", "dist", "build"})

# The filesystem WALK (finding `.git` dirs under an owner's linked folders)
# is what actually costs seconds on a Windows box with 24 repos -- the live
# git status of each repo found is never cached (see `repo_summary`), only
# this. 30s per the contract; invalidated eagerly by `create_repo` so a
# freshly created/cloned repo shows up without waiting out the TTL.
_DISCOVERY_TTL = 30.0
_DISCOVERY_LOCK = threading.Lock()
# owner-key -> (monotonic timestamp, projects fingerprint, deduped repo list)
_DISCOVERY_CACHE: Dict[str, Tuple[float, Tuple[Any, ...], List[Dict[str, Any]]]] = {}

MAX_DIFF_BYTES = 200_000
STDERR_SNIPPET_LIMIT = 2000

_NULL_DEVICE = "NUL" if os.name == "nt" else "/dev/null"

# `-c core.fsmonitor=`/`-c diff.external=` neutralise the two repo-config hooks
# workspace_routes.py's own hardening note warns about; `--no-pager` keeps a
# pager from ever being spawned for a subprocess with no tty.
_HARDENING: Tuple[str, ...] = ("-c", "core.fsmonitor=", "-c", "diff.external=", "--no-pager")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class GitNotFoundError(Exception):
    """`git` is not on PATH (or the binary vanished between the check and the call)."""


class GitCommandError(Exception):
    """A git command exited non-zero (and wasn't one of the specific cases below)."""

    def __init__(self, cmd: Sequence[str], returncode: Optional[int], stdout: str, stderr: str):
        self.cmd = list(cmd)
        self.returncode = returncode
        self.stdout = stdout or ""
        self.stderr = stderr or ""
        super().__init__(f"git {' '.join(str(a) for a in self.cmd[:2])} failed rc={returncode}")


class GitDirtyCheckoutError(Exception):
    """checkout/branch-checkout refused because it would clobber local changes."""

    def __init__(self, paths: List[str]):
        self.paths = paths
        super().__init__("checkout would overwrite local changes: " + ", ".join(paths))


class GitDivergedError(Exception):
    """`pull --ff-only` refused because local and upstream diverged."""

    def __init__(self, ahead: int, behind: int):
        self.ahead = ahead
        self.behind = behind
        super().__init__(f"diverged: ahead={ahead} behind={behind}")


class GitRejectedError(Exception):
    """`push` was rejected by the remote (non-fast-forward, hook, secret scan, ...)."""

    def __init__(self, stderr: str):
        self.stderr = stderr or ""
        super().__init__("push rejected")


class GitNoIdentityError(Exception):
    """The repo has no effective user.name/user.email to commit as."""


class GitNothingToCommitError(Exception):
    """Empty message, or nothing staged (and not an --amend)."""


def stderr_snippet(text: str, limit: int = STDERR_SNIPPET_LIMIT) -> str:
    return (text or "").strip()[:limit]


# ---------------------------------------------------------------------------
# Low-level git execution
# ---------------------------------------------------------------------------
def git_available() -> bool:
    return shutil.which("git") is not None


def _git_env() -> Dict[str, str]:
    """Environment for a git child: `native_host_environment` (strips our
    venv markers so a repo hook resolves its own interpreter, not ours) plus
    the usual "don't prompt, don't lock" pair. Deliberately sets NO
    GIT_AUTHOR_*/GIT_COMMITTER_* -- see the module docstring."""
    env = native_host_environment()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        env.pop(var, None)
    return env


def _argv(args: Sequence[str]) -> List[str]:
    return ["git", *_HARDENING, *args]


def run_git(repo_dir: str, *args: str, timeout: float = GIT_TIMEOUT_DEFAULT) -> subprocess.CompletedProcess:
    """Run one git command in `repo_dir`. Never a shell, always a real argv.

    Raises `GitNotFoundError` if the binary is missing. A non-zero exit is
    NOT raised here -- most callers need to inspect stdout/stderr to decide
    which of several outcomes it is (dirty checkout vs. a plain failure,
    diverged vs. some other pull error, ...); only the low-level "the process
    could not even run" cases are exceptions.
    """
    if not git_available():
        raise GitNotFoundError()
    try:
        return subprocess.run(
            _argv(args), cwd=repo_dir, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, env=_git_env(),
        )
    except FileNotFoundError:
        raise GitNotFoundError()
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args=_argv(args), returncode=124, stdout="",
            stderr=f"git {args[0] if args else ''} timed out after {timeout:.0f}s",
        )


def git_version() -> Optional[str]:
    if not git_available():
        return None
    try:
        proc = subprocess.run(["git", "--version"], capture_output=True, text=True,
                              timeout=5, env=_git_env())
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    out = (proc.stdout or "").strip()
    m = re.search(r"(\d+\.\d+(?:\.\d+)*)", out)
    return m.group(1) if m else (out or None)


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------
def compute_repo_id(path: str) -> str:
    """sha1(normcase(realpath(path)))[:12] -- stable across calls/platforms."""
    real = os.path.realpath(path)
    key = os.path.normcase(real)
    return hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
def _is_repo_dir(path: str) -> bool:
    """A dir with a `.git` entry -- directory (normal repo) or file (worktree
    / submodule pointer, e.g. `gitdir: ../.git/modules/x`)."""
    try:
        return os.path.exists(os.path.join(path, ".git"))
    except OSError:
        return False


def _walk_repos(root: str, *, max_depth: int = MAX_DEPTH, budget: int = MAX_REPOS) -> List[str]:
    """Pre-order DFS from `root` (depth 0) down to `max_depth`, collecting
    every directory that looks like a repo -- including nested ones, since
    a repo directory is not itself excluded from further descent (only its
    internal `.git` entry and the noise dirs are skipped). Symlinked
    directories are not followed, so a repo cannot alias itself into a loop.
    """
    found: List[str] = []
    if not os.path.isdir(root):
        return found

    def _walk(path: str, depth: int) -> None:
        if len(found) >= budget:
            return
        if _is_repo_dir(path):
            found.append(path)
        if depth >= max_depth:
            return
        try:
            names = sorted(os.listdir(path))
        except OSError:
            return
        for name in names:
            if len(found) >= budget:
                return
            if name == ".git" or name in _SKIP_DIR_NAMES:
                continue
            full = os.path.join(path, name)
            try:
                if os.path.islink(full) or not os.path.isdir(full):
                    continue
            except OSError:
                continue
            _walk(full, depth + 1)

    _walk(root, 0)
    return found


def _assign_parents(repo_paths: List[str]) -> Dict[str, Optional[str]]:
    """For each discovered repo, the closest discovered ANCESTOR repo path
    (or None). O(n^2) over a per-root repo list capped at MAX_REPOS -- cheap
    at this scale."""
    parents: Dict[str, Optional[str]] = {}
    normed = {p: os.path.normcase(os.path.realpath(p)) for p in repo_paths}
    for p in repo_paths:
        pn = normed[p]
        best: Optional[str] = None
        best_len = -1
        for q in repo_paths:
            if q == p:
                continue
            qn = normed[q]
            if pn == qn:
                continue
            if pn.startswith(qn + os.sep) and len(qn) > best_len:
                best, best_len = q, len(qn)
        parents[p] = best
    return parents


def _project_root_folders(project: Dict[str, Any]) -> List[str]:
    """The linked folders of one project: its `workspace` plus every enabled
    `folder`-kind context link, de-duplicated by realpath+normcase (the same
    identity rule `services/projects.py` uses for link dedup)."""
    from services.projects import get_store  # lazy: avoid an import-time cycle

    roots: List[str] = []
    ws = str(project.get("workspace") or "").strip()
    if ws:
        roots.append(ws)
    try:
        for link in get_store().normalized_links(project):
            if link.get("kind") == "folder" and link.get("enabled", True):
                p = str(link.get("path") or "").strip()
                if p:
                    roots.append(p)
    except Exception:  # noqa: BLE001 - a malformed links list must not break discovery
        logger.debug("git_panel: could not read links for project %s", project.get("id"), exc_info=True)

    seen = set()
    out: List[str] = []
    for r in roots:
        try:
            real = os.path.realpath(r)
        except OSError:
            continue
        key = os.path.normcase(real)
        if key in seen:
            continue
        seen.add(key)
        out.append(real)
    return out


def _owner_cache_key(owner: Optional[str]) -> str:
    return str(owner or "").strip() or "_"


def invalidate_discovery_cache(owner: Optional[str] = None) -> None:
    """Drop the cached filesystem walk -- for one owner, or (no argument)
    every owner. Called after `create_repo` succeeds, so a just-created or
    -cloned repo is visible on the very next list instead of waiting out
    `_DISCOVERY_TTL`."""
    with _DISCOVERY_LOCK:
        if owner is None:
            _DISCOVERY_CACHE.clear()
        else:
            _DISCOVERY_CACHE.pop(_owner_cache_key(owner), None)


def _walk_projects(projects: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The actual filesystem walk: every repo (incl. nested) under `projects`'
    linked folders, as discovery records -- id/path/name/project_id/
    project_name/root_folder/parent_repo_id. May contain duplicate paths
    when two projects (or two links of one project) point at the same
    folder; `_dedupe_repos` collapses those. Capped at MAX_REPOS entries
    (pre-dedupe) across every project/folder -- this is the part real disk
    I/O makes slow, so it is what gets cached."""
    out: List[Dict[str, Any]] = []
    for project in projects:
        if len(out) >= MAX_REPOS:
            break
        pid = project.get("id")
        pname = project.get("name")
        for root in _project_root_folders(project):
            if len(out) >= MAX_REPOS:
                break
            paths = _walk_repos(root, max_depth=MAX_DEPTH, budget=MAX_REPOS - len(out))
            parents = _assign_parents(paths)
            for path in paths:
                if len(out) >= MAX_REPOS:
                    break
                parent_path = parents.get(path)
                out.append({
                    "id": compute_repo_id(path),
                    "path": path,
                    "name": os.path.basename(path.rstrip(os.sep)) or path,
                    "project_id": pid,
                    "project_name": pname,
                    "root_folder": root,
                    "parent_repo_id": compute_repo_id(parent_path) if parent_path else None,
                })
    return out


def _dedupe_repos(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Collapse entries that share a normalized real path -- e.g. two
    projects linking the same folder on disk -- into ONE row per repo. The
    first occurrence wins for `project_id`/`project_name`/`root_folder`/
    `parent_repo_id` (kept for callers that pre-date multi-project repos);
    every project that surfaced this path is listed in `"projects"`."""
    order: List[str] = []
    by_key: Dict[str, Dict[str, Any]] = {}
    for entry in entries:
        try:
            real = os.path.realpath(entry["path"])
        except OSError:
            real = entry["path"]
        key = os.path.normcase(real)
        base = by_key.get(key)
        if base is None:
            base = dict(entry)
            base["projects"] = []
            by_key[key] = base
            order.append(key)
        proj = {"id": entry.get("project_id"), "name": entry.get("project_name")}
        if proj not in base["projects"]:
            base["projects"].append(proj)
    return [by_key[k] for k in order]


def _projects_fingerprint(projects: Sequence[Dict[str, Any]]) -> Tuple[Any, ...]:
    """A cheap signature of "what `store.list(owner)` returned" -- which
    projects exist, and the parts of each that decide what discovery would
    walk (`workspace`, and `updated_at`/`context_revision`, which bump
    whenever a project's `folder` context links change). `store.list` itself
    is just a JSON read, cheap to call on every request; comparing THIS is
    what lets the cache skip the actual filesystem walk only when nothing
    that walk depends on could have changed -- a project renamed, deleted,
    (un)linked, or created invalidates the cache for free, no explicit
    `invalidate_discovery_cache` needed for any of those."""
    return tuple(
        (p.get("id"), p.get("workspace"), p.get("updated_at"), p.get("context_revision"))
        for p in projects
    )


def discover_repos_for_owner(owner: Optional[str], project_id: str = "", *,
                             use_cache: bool = True) -> Optional[List[Dict[str, Any]]]:
    """Every repo (incl. nested) under the owner's linked project folders,
    deduped by real path (`"projects"` lists every project that links it).

    Returns `None` when `project_id` was given but does not resolve to a
    project the owner can see (caller's cue to answer 404). Otherwise a list
    of dicts: id/path/name/project_id/project_name/projects/root_folder/
    parent_repo_id -- everything discovery itself knows; live git fields are
    added by `repo_summary`/`repo_summaries`.

    The filesystem WALK for the whole-owner case (no `project_id`) is cached
    30s per owner, keyed also on `_projects_fingerprint` (`use_cache=False`
    bypasses it entirely -- e.g. right after a write a caller wants a
    guaranteed-fresh read without waiting on `invalidate_discovery_cache`).
    A `project_id`-scoped call is cheap enough (one project's folders, not
    every project) that it always walks fresh -- simpler than reasoning
    about a project-scoped cache key, and it keeps 404-on-unknown-project a
    real-time check.
    """
    from services.projects import get_store

    store = get_store()
    if project_id:
        project = store.get(project_id, owner)
        if not project:
            return None
        return _dedupe_repos(_walk_projects([project]))

    projects = store.list(owner)
    key = _owner_cache_key(owner)
    fingerprint = _projects_fingerprint(projects)
    if use_cache:
        with _DISCOVERY_LOCK:
            hit = _DISCOVERY_CACHE.get(key)
        if hit is not None:
            ts, cached_fingerprint, cached_result = hit
            if cached_fingerprint == fingerprint and (time.monotonic() - ts) < _DISCOVERY_TTL:
                return cached_result

    result = _dedupe_repos(_walk_projects(projects))
    if use_cache:
        with _DISCOVERY_LOCK:
            _DISCOVERY_CACHE[key] = (time.monotonic(), fingerprint, result)
    return result


def find_repo_meta(repo_id: str, owner: Optional[str]) -> Optional[Dict[str, Any]]:
    """The discovery record for `repo_id` among the owner's repos, or None.

    Because this is the ONLY path any route uses to turn a `repo_id` into a
    filesystem path, a repo outside the owner's linked folders -- or one the
    caller invented from an arbitrary path -- simply never appears here, and
    every caller answers it the same way discovery answers an unknown id: 404.
    """
    repos = discover_repos_for_owner(owner) or []
    for meta in repos:
        if meta["id"] == repo_id:
            return meta
    return None


# ---------------------------------------------------------------------------
# Status parsing (`git status --porcelain=v2 -z [--branch]`)
# ---------------------------------------------------------------------------
def _map_status(code: str) -> str:
    return code if code in ("A", "M", "D", "R", "C") else "M"


def _tokenize_status_v2(text: str) -> Tuple[Dict[str, str], List[Dict[str, str]], List[Dict[str, str]],
                                            List[Dict[str, str]], List[Dict[str, str]]]:
    """Shared parser for `git status --porcelain=v2 -z` output, `--branch` or
    not: with `-z`, the `# branch.*` header lines are ALSO NUL-terminated
    (verified against a real `git status`, not assumed from the docs), so
    they show up as ordinary tokens starting with `"# "` alongside the file
    entries -- one pass picks out both. Returns
    (branch_headers, staged, unstaged, untracked, conflicts); a caller that
    only wants the four lists (no `--branch`) gets an empty headers dict."""
    headers: Dict[str, str] = {}
    staged: List[Dict[str, str]] = []
    unstaged: List[Dict[str, str]] = []
    untracked: List[Dict[str, str]] = []
    conflicts: List[Dict[str, str]] = []

    tokens = text.split("\x00")
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        i += 1
        if not tok:
            continue
        if tok.startswith("# branch."):
            key, _, value = tok[len("# branch."):].partition(" ")
            headers[key] = value
            continue
        kind = tok[0]
        if kind == "1":
            fields = tok.split(" ", 8)
            if len(fields) < 9:
                continue
            xy, path = fields[1], fields[8]
            x, y = xy[0], xy[1]
            if x not in (".", "?"):
                staged.append({"path": path, "status": _map_status(x)})
            if y not in (".", "?"):
                unstaged.append({"path": path, "status": _map_status(y)})
        elif kind == "2":
            fields = tok.split(" ", 9)
            if len(fields) < 10 or i >= n:
                continue
            xy, path = fields[1], fields[9]
            old_path = tokens[i]
            i += 1
            x, y = xy[0], xy[1]
            if x not in (".", "?"):
                entry = {"path": path, "status": _map_status(x), "old_path": old_path}
                staged.append(entry)
            if y not in (".", "?"):
                unstaged.append({"path": path, "status": _map_status(y)})
        elif kind == "u":
            fields = tok.split(" ", 10)
            if len(fields) < 11:
                continue
            conflicts.append({"path": fields[10]})
        elif kind == "?":
            untracked.append({"path": tok[2:]})
        # kind == "!" (ignored) is dropped -- not part of any contract list.
    return headers, staged, unstaged, untracked, conflicts


def parse_status_v2(text: str) -> Tuple[List[Dict[str, str]], List[Dict[str, str]], List[Dict[str, str]], List[Dict[str, str]]]:
    """Parse `git status --porcelain=v2 -z` output into
    (staged, unstaged, untracked, conflicts)."""
    _headers, staged, unstaged, untracked, conflicts = _tokenize_status_v2(text)
    return staged, unstaged, untracked, conflicts


_AB_RE = re.compile(r"\+(\d+)\s+-(\d+)")


def parse_status_v2_branch(text: str) -> Dict[str, Any]:
    """Parse `git status --porcelain=v2 -z --branch` output into everything
    ONE such call can answer: branch/detached/head_sha/upstream/ahead/behind
    plus the usual staged/unstaged/untracked/conflicts -- this single
    command is what lets `repo_summary` do the whole "which branch, how far
    from upstream, how dirty" question in one `git` process instead of the
    four separate ones (`symbolic-ref`, `rev-parse --abbrev-ref @{u}`,
    `rev-list --count`, `status`) the panel started with."""
    headers, staged, unstaged, untracked, conflicts = _tokenize_status_v2(text)
    head = headers.get("head") or ""
    detached = head == "(detached)"
    branch = None if (not head or detached) else head
    oid = headers.get("oid") or ""
    head_sha = None if (not oid or oid == "(initial)") else oid
    upstream = headers.get("upstream") or None
    ahead = behind = 0
    m = _AB_RE.match(headers.get("ab") or "")
    if m:
        ahead, behind = int(m.group(1)), int(m.group(2))
    return {
        "branch": branch, "detached": detached, "head_sha": head_sha,
        "upstream": upstream, "ahead": ahead, "behind": behind,
        "staged": staged, "unstaged": unstaged, "untracked": untracked, "conflicts": conflicts,
    }


# ---------------------------------------------------------------------------
# Repo facts
# ---------------------------------------------------------------------------
def _current_branch(repo_path: str) -> Tuple[Optional[str], bool]:
    proc = run_git(repo_path, "symbolic-ref", "-q", "--short", "HEAD")
    if proc.returncode == 0:
        return proc.stdout.strip(), False
    return None, True


def _head_sha(repo_path: str) -> Optional[str]:
    proc = run_git(repo_path, "rev-parse", "HEAD")
    return proc.stdout.strip() if proc.returncode == 0 else None


def _upstream(repo_path: str) -> Optional[str]:
    proc = run_git(repo_path, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    return proc.stdout.strip() if proc.returncode == 0 else None


def _ahead_behind(repo_path: str, upstream: Optional[str]) -> Tuple[int, int]:
    if not upstream:
        return 0, 0
    proc = run_git(repo_path, "rev-list", "--left-right", "--count", f"{upstream}...HEAD")
    if proc.returncode != 0:
        return 0, 0
    parts = proc.stdout.strip().split()
    if len(parts) != 2:
        return 0, 0
    try:
        behind, ahead = int(parts[0]), int(parts[1])
    except ValueError:
        return 0, 0
    return ahead, behind


_USER_CONFIG_RE = re.compile(r"^user\.(name|email)$")


def repo_user(repo_path: str) -> Dict[str, str]:
    """The repo's EFFECTIVE (local overriding global) git identity -- ONE
    `git config --get-regexp` call instead of two separate `config
    user.name`/`config user.email` ones. `--get-regexp` can print a matched
    key more than once (once per config file it is set in, in increasing
    priority -- system, then global, then local), so the LAST line for a key
    wins here, same as a plain `git config <key>` already resolves to the
    single effective value."""
    proc = run_git(repo_path, "config", "--get-regexp", r"^user\.(name|email)$")
    name = email = ""
    if proc.returncode == 0:
        for line in proc.stdout.splitlines():
            key, sep, value = line.partition(" ")
            if not sep or not _USER_CONFIG_RE.match(key):
                continue
            if key == "user.name":
                name = value
            else:
                email = value
    return {"name": name, "email": email}


def repo_remotes(repo_path: str) -> List[Dict[str, Optional[str]]]:
    proc = run_git(repo_path, "remote", "-v")
    if proc.returncode != 0:
        return []
    by_name: Dict[str, Dict[str, Optional[str]]] = {}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t", 1)
        if len(parts) != 2:
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
        name, rest = parts
        url, _, kind = rest.rpartition(" ")
        kind = kind.strip("()")
        entry = by_name.setdefault(name, {"name": name, "fetch_url": None, "push_url": None})
        if kind == "fetch":
            entry["fetch_url"] = url.strip() or None
        elif kind == "push":
            entry["push_url"] = url.strip() or None
    return list(by_name.values())


def _status_branch(repo_path: str) -> Dict[str, Any]:
    """`git status --porcelain=v2 -z --branch --untracked-files=all`, parsed
    -- the ONE call `repo_status`/`repo_summary` build everything from."""
    proc = run_git(repo_path, "status", "--porcelain=v2", "-z", "--branch", "--untracked-files=all")
    text = proc.stdout if proc.returncode == 0 else ""
    return parse_status_v2_branch(text)


def repo_status(repo_path: str) -> Dict[str, Any]:
    status = _status_branch(repo_path)
    return {k: status[k] for k in (
        "branch", "detached", "ahead", "behind", "upstream",
        "staged", "unstaged", "untracked", "conflicts",
    )}


def repo_summary(path: str, *, project_id: Any, project_name: Any, root_folder: str,
                  parent_repo_id: Optional[str], projects: Optional[List[Dict[str, Any]]] = None,
                  light: bool = False) -> Dict[str, Any]:
    """The full `GET /api/git/repos/{id}` shape, freshly computed.

    ONE `git` call in `light` mode (branch/ahead/behind/dirty only -- for
    the polling adapter's `?light=1`), THREE in full mode (status, the
    effective `user.*`, and `remote -v`) -- down from the eight separate
    invocations (`symbolic-ref`, `rev-parse --abbrev-ref @{u}`, `rev-list
    --count`, `status`, `rev-parse HEAD`, two `config user.*`, `remote -v`)
    this used to make per repo, which is most of where 24 repos going from
    9.5s to well under 2s on Windows comes from -- a `git.exe` process spawn
    there costs far more than the git operation itself.
    """
    status = _status_branch(path)
    row: Dict[str, Any] = {
        "id": compute_repo_id(path),
        "path": path,
        "name": os.path.basename(path.rstrip(os.sep)) or path,
        "project_id": project_id,
        "project_name": project_name,
        "projects": projects if projects is not None else [{"id": project_id, "name": project_name}],
        "root_folder": root_folder,
        "parent_repo_id": parent_repo_id,
        "branch": status["branch"],
        "detached": status["detached"],
        "head_sha": status["head_sha"],
        "upstream": status["upstream"],
        "ahead": status["ahead"],
        "behind": status["behind"],
        "dirty": {
            "staged": len(status["staged"]),
            "unstaged": len(status["unstaged"]),
            "untracked": len(status["untracked"]),
        },
    }
    if light:
        return row
    row["user"] = repo_user(path)
    row["remotes"] = repo_remotes(path)
    return row


def repo_summaries(metas: Sequence[Dict[str, Any]], *, light: bool = False,
                   max_workers: int = 8) -> List[Dict[str, Any]]:
    """`repo_summary` for every discovered repo, computed IN PARALLEL: each
    repo's `git` calls run in their own worker thread (independent
    subprocesses against independent working trees -- nothing shared, so
    this is safe), which is the other half of the 24-repos-in-under-2s
    budget on Windows: `ThreadPoolExecutor(max_workers=8)` overlaps the
    process-spawn latency across repos instead of paying it serially."""
    if not metas:
        return []
    workers = max(1, min(max_workers, len(metas)))

    def _one(meta: Dict[str, Any]) -> Dict[str, Any]:
        return repo_summary(
            meta["path"], project_id=meta.get("project_id"), project_name=meta.get("project_name"),
            root_folder=meta.get("root_folder"), parent_repo_id=meta.get("parent_repo_id"),
            projects=meta.get("projects"), light=light,
        )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(_one, metas))


# ---------------------------------------------------------------------------
# Log (`git log --format=... -z`, fields split on \x1f, records on NUL)
# ---------------------------------------------------------------------------
_LOG_FIELDS = "%H\x1f%h\x1f%P\x1f%an\x1f%ae\x1f%at\x1f%s\x1f%b\x1f%D"


def _fmt_epoch(raw: str) -> str:
    try:
        ts = int(raw)
    except (TypeError, ValueError):
        return ""
    try:
        return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OSError, OverflowError, ValueError):
        return ""


def _shorten_ref(ref: str) -> str:
    ref = ref.strip()
    prefix = ""
    if ref.startswith("HEAD -> "):
        prefix, ref = "HEAD -> ", ref[len("HEAD -> "):]
    elif ref.startswith("tag: "):
        prefix, ref = "tag: ", ref[len("tag: "):]
    for full in ("refs/heads/", "refs/remotes/", "refs/tags/"):
        if ref.startswith(full):
            ref = ref[len(full):]
            break
    return prefix + ref


def _parse_log_record(rec: str) -> Dict[str, Any]:
    parts = rec.split("\x1f")
    while len(parts) < 9:
        parts.append("")
    full, short, parents, an, ae, at, subject, body, decor = parts[:9]
    refs = [_shorten_ref(r) for r in decor.split(",") if r.strip()] if decor else []
    return {
        "sha": full,
        "short": short,
        "parents": [p for p in parents.split(" ") if p],
        "author": an,
        "email": ae,
        "date": _fmt_epoch(at),
        "message": subject,
        "body": body,
        "refs": refs,
    }


def log_commits(repo_path: str, *, limit: int = 50, cursor: Optional[str] = None,
                 ref: str = "") -> Dict[str, Any]:
    """`GET /repos/{id}/log`. Peek-one-extra pagination: fetch one more
    commit than the page needs (plus one, to drop the cursor commit itself
    when a cursor is given) so `next_cursor` can be set correctly without a
    second round trip."""
    limit = max(1, min(int(limit or 50), 500))
    if cursor:
        want = limit + 2  # +1 dropped (the cursor commit itself), +1 peek
        range_args = [cursor]
    else:
        want = limit + 1  # +1 peek
        if ref == "all":
            range_args = ["--all"]
        elif ref:
            range_args = [ref]
        else:
            range_args = ["HEAD"]

    proc = run_git(repo_path, "log", f"--format={_LOG_FIELDS}", "--date-order",
                    "--decorate=full", "-z", "-n", str(want), *range_args, "--")
    if proc.returncode != 0:
        return {"commits": [], "next_cursor": None}

    records = [r for r in proc.stdout.split("\x00") if r.strip("\n") != ""]
    records = [r[1:] if r.startswith("\n") else r for r in records]

    if cursor and records and records[0].split("\x1f", 1)[0] == cursor:
        records = records[1:]

    more = len(records) > limit
    page = records[:limit]
    commits = [_parse_log_record(r) for r in page]
    next_cursor = commits[-1]["sha"] if more and commits else None
    return {"commits": commits, "next_cursor": next_cursor}


# ---------------------------------------------------------------------------
# Branches (`for-each-ref`)
# ---------------------------------------------------------------------------
_BRANCH_FIELDS = "%(refname)\x1f%(objectname)\x1f%(upstream:short)\x1f%(upstream:track,nobracket)\x1f%(HEAD)"
_TRACK_RE = re.compile(r"(ahead|behind)\s+(\d+)")


def _parse_track(track: str) -> Tuple[int, int]:
    ahead = behind = 0
    if not track or track == "gone":
        return 0, 0
    for m in _TRACK_RE.finditer(track):
        n = int(m.group(2))
        if m.group(1) == "ahead":
            ahead = n
        else:
            behind = n
    return ahead, behind


def list_branches(repo_path: str) -> Dict[str, Any]:
    current, _detached = _current_branch(repo_path)
    proc = run_git(repo_path, "for-each-ref", f"--format={_BRANCH_FIELDS}", "refs/heads", "refs/remotes")
    local: List[Dict[str, Any]] = []
    remote: List[Dict[str, Any]] = []
    if proc.returncode == 0:
        for line in proc.stdout.split("\n"):
            if not line.strip():
                continue
            fields = line.split("\x1f")
            while len(fields) < 5:
                fields.append("")
            refname, sha, upstream, track, headmark = fields[:5]
            if refname.startswith("refs/heads/"):
                short = refname[len("refs/heads/"):]
                ahead, behind = _parse_track(track)
                local.append({
                    "name": short, "sha": sha, "upstream": upstream or None,
                    "ahead": ahead, "behind": behind, "is_current": headmark.strip() == "*",
                })
            elif refname.startswith("refs/remotes/"):
                short = refname[len("refs/remotes/"):]
                if short.endswith("/HEAD"):
                    continue
                remote.append({"name": short, "sha": sha})
    return {"current": current, "local": local, "remote": remote}


# ---------------------------------------------------------------------------
# Commit detail (`--numstat` / `--name-status`) and diffs
# ---------------------------------------------------------------------------
_COMMIT_FIELDS = "%H\x1f%h\x1f%P\x1f%an\x1f%ae\x1f%at\x1f%s\x1f%b"
_NUMSTAT_RE = re.compile(r"^(\d+|-)\t(\d+|-)\t(.*)$", re.DOTALL)


def _numstat_map(repo_path: str, sha: str) -> Dict[str, Tuple[Optional[int], Optional[int]]]:
    proc = run_git(repo_path, "diff-tree", "--no-commit-id", "-r", "-M", "-C", "-z", "--numstat", sha, "--")
    if proc.returncode != 0:
        return {}
    toks = [t for t in proc.stdout.split("\x00") if t != ""]
    out: Dict[str, Tuple[Optional[int], Optional[int]]] = {}
    i, n = 0, len(toks)
    while i < n:
        line = toks[i]
        i += 1
        m = _NUMSTAT_RE.match(line)
        if not m:
            continue
        add_s, del_s, rest = m.groups()
        add = None if add_s == "-" else int(add_s)
        dele = None if del_s == "-" else int(del_s)
        if rest == "":
            # Rename/copy with -z: the two paths follow as separate NUL fields.
            if i >= n:
                continue
            _old_path = toks[i]
            i += 1
            new_path = toks[i] if i < n else ""
            if i < n:
                i += 1
            out[new_path] = (add, dele)
        else:
            out[rest] = (add, dele)
    return out


def _name_status_list(repo_path: str, sha: str) -> List[Dict[str, str]]:
    proc = run_git(repo_path, "diff-tree", "--no-commit-id", "-r", "-M", "-C", "-z", "--name-status", sha, "--")
    if proc.returncode != 0:
        return []
    toks = [t for t in proc.stdout.split("\x00") if t != ""]
    out: List[Dict[str, str]] = []
    i, n = 0, len(toks)
    while i < n:
        status = toks[i]
        i += 1
        code = status[0] if status else "M"
        if code in ("R", "C") and i + 1 < n:
            old_path = toks[i]
            i += 1
            new_path = toks[i]
            i += 1
            out.append({"path": new_path, "status": code, "old_path": old_path})
        elif i < n:
            path = toks[i]
            i += 1
            out.append({"path": path, "status": code})
    return out


def commit_files(repo_path: str, sha: str) -> List[Dict[str, Any]]:
    numstat = _numstat_map(repo_path, sha)
    entries = _name_status_list(repo_path, sha)
    files: List[Dict[str, Any]] = []
    for e in entries:
        add, dele = numstat.get(e["path"], (None, None))
        row: Dict[str, Any] = {"path": e["path"], "status": e["status"], "additions": add, "deletions": dele}
        if "old_path" in e:
            row["old_path"] = e["old_path"]
        files.append(row)
    return files


def commit_detail(repo_path: str, sha: str) -> Optional[Dict[str, Any]]:
    proc = run_git(repo_path, "show", "-s", f"--format={_COMMIT_FIELDS}", sha, "--")
    if proc.returncode != 0:
        return None
    raw = proc.stdout
    if raw.endswith("\n"):
        raw = raw[:-1]
    parts = raw.split("\x1f", 7)
    while len(parts) < 8:
        parts.append("")
    full, short, parents, an, ae, at, subject, body = parts[:8]
    if not full:
        return None
    return {
        "sha": full, "short": short,
        "author": an, "email": ae, "date": _fmt_epoch(at),
        "message": subject, "body": body,
        "parents": [p for p in parents.split(" ") if p],
        "files": commit_files(repo_path, sha),
    }


def _format_diff(path: str, text: str) -> Dict[str, Any]:
    binary = bool(re.search(r"^Binary files .* differ$", text, re.MULTILINE))
    data = text.encode("utf-8", "replace")
    truncated = False
    if len(data) > MAX_DIFF_BYTES:
        data = data[:MAX_DIFF_BYTES]
        text = data.decode("utf-8", "replace")
        truncated = True
    return {"path": path, "diff": text, "truncated": truncated, "binary": binary}


def commit_file_diff(repo_path: str, sha: str, path: str) -> Dict[str, Any]:
    proc = run_git(repo_path, "diff-tree", "-p", "--root", "--no-color", "--no-ext-diff",
                    "-M", "-C", sha, "--", path)
    text = proc.stdout if proc.returncode == 0 else ""
    return _format_diff(path, text)


def is_tracked(repo_path: str, rel_path: str) -> bool:
    proc = run_git(repo_path, "ls-files", "--error-unmatch", "--", rel_path)
    return proc.returncode == 0


def working_diff(repo_path: str, rel_path: str, *, staged: bool) -> Dict[str, Any]:
    if staged:
        proc = run_git(repo_path, "diff", "--no-color", "--no-ext-diff", "-M", "-C",
                        "--cached", "--", rel_path)
    elif is_tracked(repo_path, rel_path):
        proc = run_git(repo_path, "diff", "--no-color", "--no-ext-diff", "-M", "-C",
                        "--", rel_path)
    else:
        # Untracked: diff the worktree file against the platform's null
        # device so a brand-new file still renders as an addition.
        proc = run_git(repo_path, "diff", "--no-color", "--no-ext-diff", "--no-index",
                        "--", _NULL_DEVICE, rel_path)
    text = proc.stdout if proc.returncode in (0, 1) else ""
    return _format_diff(rel_path, text)


# ---------------------------------------------------------------------------
# Path safety (`path` query/body params must stay inside the repo)
# ---------------------------------------------------------------------------
def safe_rel_path(repo_path: str, path: str) -> Optional[str]:
    """`path` resolved and confined to `repo_path`; None if it escapes (an
    absolute path, `..`, or a symlink pointing outside)."""
    if not path or not path.strip():
        return None
    # NOT stripped of a leading slash: an absolute path must stay absolute
    # into `os.path.join` (POSIX discards `root` for it; Windows still
    # anchors a `\`-rooted path at `root`'s drive) so the containment check
    # below can catch it, rather than silently reinterpreting it as relative.
    candidate = path.replace("\\", "/")
    root = os.path.realpath(repo_path)
    full = os.path.realpath(os.path.join(root, candidate))
    root_key = os.path.normcase(root)
    full_key = os.path.normcase(full)
    if full_key != root_key and not full_key.startswith(root_key + os.sep):
        return None
    return os.path.relpath(full, root).replace("\\", "/")


# ---------------------------------------------------------------------------
# Mutating operations
# ---------------------------------------------------------------------------
_OVERWRITE_MARKERS = ("would be overwritten by checkout", "would be overwritten by merge")


def _parse_would_be_overwritten(stderr: str) -> List[str]:
    lines = (stderr or "").splitlines()
    paths: List[str] = []
    capture = False
    for ln in lines:
        if any(marker in ln for marker in _OVERWRITE_MARKERS):
            capture = True
            continue
        if capture:
            if ln.startswith("\t"):
                paths.append(ln.strip())
                continue
            break
    return paths


def checkout_branch(repo_path: str, branch: str, *, create: bool = False,
                     start_point: Optional[str] = None, timeout: float = GIT_TIMEOUT_DEFAULT) -> str:
    if create:
        args = ["checkout", "-b", branch] + ([start_point] if start_point else [])
    else:
        args = ["checkout", branch]
    proc = run_git(repo_path, *args, timeout=timeout)
    if proc.returncode != 0:
        dirty = _parse_would_be_overwritten(proc.stderr or "")
        if dirty:
            raise GitDirtyCheckoutError(dirty)
        raise GitCommandError(args, proc.returncode, proc.stdout, proc.stderr)
    return branch


def create_branch(repo_path: str, name: str, *, start_point: Optional[str] = None,
                   checkout: bool = True, timeout: float = GIT_TIMEOUT_DEFAULT) -> str:
    args = ["branch", name] + ([start_point] if start_point else [])
    proc = run_git(repo_path, *args, timeout=timeout)
    if proc.returncode != 0:
        raise GitCommandError(args, proc.returncode, proc.stdout, proc.stderr)
    if checkout:
        return checkout_branch(repo_path, name, timeout=timeout)
    return name


def fetch(repo_path: str, *, remote: Optional[str] = None, prune: bool = False,
          timeout: float = GIT_TIMEOUT_LONG) -> str:
    args = ["fetch"]
    if prune:
        args.append("--prune")
    if remote:
        args.append(remote)
    proc = run_git(repo_path, *args, timeout=timeout)
    if proc.returncode != 0:
        raise GitCommandError(args, proc.returncode, proc.stdout, proc.stderr)
    return (proc.stdout or "") + (proc.stderr or "")


_DIVERGE_MARKERS = ("not possible to fast-forward", "diverg", "not something we can merge")


def pull(repo_path: str, *, remote: Optional[str] = None, branch: Optional[str] = None,
         timeout: float = GIT_TIMEOUT_LONG) -> str:
    args = ["pull", "--ff-only"]
    if remote:
        args.append(remote)
        if branch:
            args.append(branch)
    proc = run_git(repo_path, *args, timeout=timeout)
    if proc.returncode != 0:
        stderr_l = (proc.stderr or "").lower()
        if any(marker in stderr_l for marker in _DIVERGE_MARKERS):
            upstream = _upstream(repo_path)
            ahead, behind = _ahead_behind(repo_path, upstream)
            raise GitDivergedError(ahead, behind)
        raise GitCommandError(args, proc.returncode, proc.stdout, proc.stderr)
    return (proc.stdout or "") + (proc.stderr or "")


def push(repo_path: str, *, remote: Optional[str] = None, branch: Optional[str] = None,
         set_upstream: bool = False, timeout: float = GIT_TIMEOUT_LONG) -> str:
    args = ["push"]
    if not set_upstream and not remote and not branch:
        # A branch that was just created has no upstream yet. Nobody should
        # have to read "fatal: The current branch X has no upstream branch"
        # and go type `git push --set-upstream origin X` in a terminal: the
        # panel's Push publishes the branch on `origin` the first time, the
        # same thing the git CLI's own suggested command does.
        try:
            if _upstream(repo_path) is None and any(r.get("name") == "origin" for r in repo_remotes(repo_path)):
                set_upstream = True
        except Exception:  # noqa: BLE001 - fall through to the plain push
            pass
    if set_upstream:
        args.append("-u")
        # `-u` needs an explicit remote+branch on a branch that has none yet
        # ("fatal: The current branch ... has no upstream branch") -- default
        # to "origin" and the checked-out branch the same way the git CLI's
        # own suggested command would.
        remote = remote or "origin"
        branch = branch or _current_branch(repo_path)[0]
    if remote:
        args.append(remote)
        if branch:
            args.append(branch)
    proc = run_git(repo_path, *args, timeout=timeout)
    if proc.returncode != 0:
        raise GitRejectedError(proc.stderr or proc.stdout or "")
    return (proc.stdout or "") + (proc.stderr or "")


def add_remote(repo_path: str, name: str, url: str, *, timeout: float = GIT_TIMEOUT_DEFAULT) -> None:
    """`git remote add <name> <url>` -- used once a GitHub repo has just been
    created (Lote 84) to point `origin` at it."""
    args = ["remote", "add", name, url]
    proc = run_git(repo_path, *args, timeout=timeout)
    if proc.returncode != 0:
        raise GitCommandError(args, proc.returncode, proc.stdout, proc.stderr)


def stage(repo_path: str, *, paths: Optional[List[str]] = None, all_: bool = False,
          timeout: float = GIT_TIMEOUT_DEFAULT) -> None:
    if all_ or not paths:
        args = ["add", "-A", "--", "."]
    else:
        args = ["add", "-A", "--", *paths]
    proc = run_git(repo_path, *args, timeout=timeout)
    if proc.returncode != 0:
        raise GitCommandError(args, proc.returncode, proc.stdout, proc.stderr)


def unstage(repo_path: str, *, paths: Optional[List[str]] = None, all_: bool = False,
            timeout: float = GIT_TIMEOUT_DEFAULT) -> None:
    # Plain `git reset` (no pathspec) unstages everything while leaving the
    # working tree untouched, and -- unlike `restore --staged` -- also works
    # on a repo with no commits yet (no HEAD to restore from).
    args = ["reset"] if (all_ or not paths) else ["reset", "--", *paths]
    proc = run_git(repo_path, *args, timeout=timeout)
    if proc.returncode != 0:
        raise GitCommandError(args, proc.returncode, proc.stdout, proc.stderr)


def discard(repo_path: str, paths: List[str], *, timeout: float = GIT_TIMEOUT_DEFAULT) -> None:
    args = ["checkout", "--", *paths]
    proc = run_git(repo_path, *args, timeout=timeout)
    if proc.returncode != 0:
        raise GitCommandError(args, proc.returncode, proc.stdout, proc.stderr)


def commit(repo_path: str, message: str, *, amend: bool = False,
           timeout: float = GIT_TIMEOUT_DEFAULT) -> Dict[str, str]:
    message = (message or "").strip()
    if not message:
        raise GitNothingToCommitError()
    user = repo_user(repo_path)
    if not user["name"] or not user["email"]:
        raise GitNoIdentityError()
    if not amend:
        status = repo_status(repo_path)
        if not status["staged"]:
            raise GitNothingToCommitError()
    args = ["commit", "-m", message]
    if amend:
        args.append("--amend")
    proc = run_git(repo_path, *args, timeout=timeout)
    if proc.returncode != 0:
        raise GitCommandError(args, proc.returncode, proc.stdout, proc.stderr)
    sha = run_git(repo_path, "rev-parse", "HEAD").stdout.strip()
    short = run_git(repo_path, "rev-parse", "--short", "HEAD").stdout.strip()
    return {"sha": sha, "short": short, "message": message}


# ---------------------------------------------------------------------------
# Public wrappers (Lote 82) -- `src/agent_git_policy.py` and
# `src/git_identities.py` need these without reaching into this module's
# underscore-prefixed internals.
# ---------------------------------------------------------------------------
def current_branch(repo_path: str) -> Tuple[Optional[str], bool]:
    """(branch name or None, detached) -- public alias of `_current_branch`."""
    return _current_branch(repo_path)


def upstream_ref(repo_path: str) -> Optional[str]:
    """Public alias of `_upstream`."""
    return _upstream(repo_path)


def repo_toplevel(path: str, *, timeout: float = GIT_TIMEOUT_DEFAULT) -> Optional[str]:
    """``git rev-parse --show-toplevel`` from `path` -- the root of the repo
    containing it (which may be a subdirectory of the repo, not the repo
    root itself), or None if `path` doesn't exist or isn't inside a git
    working tree. This is how `src/agent_git_policy.py` finds the repo an
    agent turn's `workspace` lives in."""
    if not path or not os.path.isdir(path):
        return None
    proc = run_git(path, "rev-parse", "--show-toplevel", timeout=timeout)
    if proc.returncode != 0:
        return None
    top = proc.stdout.strip()
    return os.path.realpath(top) if top else None


# ---------------------------------------------------------------------------
# Owner's linked folders (Lote 82) -- where a new repo may be created/cloned
# ---------------------------------------------------------------------------
def list_owner_folders(owner: Optional[str]) -> List[Dict[str, Any]]:
    """Every linked folder of every project the owner can see, de-duplicated
    by realpath+normcase (`GET /api/git/folders`): the set of places
    `create_repo` is allowed to write into."""
    from services.projects import get_store  # lazy: avoid an import-time cycle

    out: List[Dict[str, Any]] = []
    seen = set()
    for project in get_store().list(owner):
        pid = project.get("id")
        pname = project.get("name")
        for root in _project_root_folders(project):
            key = os.path.normcase(root)
            if key in seen:
                continue
            seen.add(key)
            out.append({"path": root, "project_id": pid, "project_name": pname})
    return out


def resolve_allowed_parent_folder(owner: Optional[str], parent_folder: str) -> Optional[str]:
    """`parent_folder` (realpath'd) if it IS one of the owner's linked
    folders, or lives inside one -- else None. The containment check
    `POST /api/git/repos` relies on so a new repo can never be written
    outside the owner's linked project folders, the same guarantee
    discovery already gives every other route in this module."""
    candidate = os.path.realpath(str(parent_folder or ""))
    if not candidate or not os.path.isdir(candidate):
        return None
    cand_key = os.path.normcase(candidate)
    for folder in list_owner_folders(owner):
        root_key = os.path.normcase(folder["path"])
        if cand_key == root_key or cand_key.startswith(root_key + os.sep):
            return candidate
    return None


# ---------------------------------------------------------------------------
# Repo creation: init / clone (Lote 82)
# ---------------------------------------------------------------------------
_REPO_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class GitInvalidNameError(Exception):
    """`name` is empty, has characters outside `[A-Za-z0-9._-]`, an unknown
    `mode` was given, or `mode="clone"` was given no `url`."""


class GitRepoExistsError(Exception):
    """The target directory already exists and is not empty."""

    def __init__(self, path: str):
        self.path = path
        super().__init__(f"{path} already exists and is not empty")


class GitCloneFailedError(Exception):
    def __init__(self, stderr: str):
        self.stderr = stderr or ""
        super().__init__("clone failed")


def init_repo(target: str, *, default_branch: str = "main", initial_commit: bool = True,
              repo_name: str = "", git_user_name: Optional[str] = None,
              git_user_email: Optional[str] = None, timeout: float = GIT_TIMEOUT_DEFAULT) -> None:
    """`git init` at `target` (already created by the caller), with its
    initial branch named `default_branch` from the very first commit --
    set via `symbolic-ref` before anything is committed, which works on any
    git version (`git init -b` needs 2.28+). `git_user_name`/`git_user_email`,
    when given, become this repo's LOCAL identity -- never the global one.
    `initial_commit` adds a `README.md` naming the repo and commits it (this
    is where `GitNoIdentityError` can surface, via `commit()`, if neither an
    identity was given nor the host has a global git identity configured)."""
    proc = run_git(target, "init", timeout=timeout)
    if proc.returncode != 0:
        raise GitCommandError(["init"], proc.returncode, proc.stdout, proc.stderr)
    branch = (default_branch or "main").strip() or "main"
    run_git(target, "symbolic-ref", "HEAD", f"refs/heads/{branch}", timeout=timeout)
    if git_user_name:
        run_git(target, "config", "user.name", git_user_name, timeout=timeout)
    if git_user_email:
        run_git(target, "config", "user.email", git_user_email, timeout=timeout)
    if initial_commit:
        readme = os.path.join(target, "README.md")
        if not os.path.exists(readme):
            with open(readme, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(f"# {repo_name or os.path.basename(target.rstrip(os.sep))}\n")
        stage(target, paths=["README.md"], timeout=timeout)
        commit(target, "Initial commit", timeout=timeout)


def clone_repo(url: str, target: str, *, timeout: float = GIT_TIMEOUT_LONG) -> None:
    proc = run_git(os.path.dirname(target) or ".", "clone", "--", url, target, timeout=timeout)
    if proc.returncode != 0:
        raise GitCloneFailedError(proc.stderr or proc.stdout or "")


def create_repo(parent_folder: str, name: str, *, mode: str, owner: Optional[str] = None,
                url: Optional[str] = None, identity_id: Optional[str] = None,
                initial_commit: bool = True, default_branch: str = "main",
                timeout_clone: float = GIT_TIMEOUT_LONG) -> str:
    """Create (`mode="init"`) or clone (`mode="clone"`) a repo named `name`
    directly under `parent_folder` (already vetted by the caller via
    `resolve_allowed_parent_folder`). Returns the new repo's absolute path.

    `name` is restricted to `[A-Za-z0-9._-]` before it ever reaches a path,
    so it cannot smuggle a `..` segment or become absolute -- the one thing
    standing between "create a repo in one of my linked folders" and
    "write anywhere the process can reach".

    Raises `GitInvalidNameError`, `GitRepoExistsError`, `GitNotFoundError`,
    `GitCloneFailedError`, `GitCommandError`, or (from an init's initial
    commit) `GitNoIdentityError`.
    """
    name = (name or "").strip()
    if not name or name in (".", "..") or not _REPO_NAME_RE.match(name):
        raise GitInvalidNameError(name)
    if mode not in ("init", "clone"):
        raise GitInvalidNameError(f"unknown mode {mode!r}")
    if not git_available():
        raise GitNotFoundError()

    parent_real = os.path.realpath(parent_folder)
    target = os.path.join(parent_real, name)
    if os.path.exists(target) and (not os.path.isdir(target) or os.listdir(target)):
        raise GitRepoExistsError(target)

    if mode == "clone":
        clone_url = (url or "").strip()
        if not clone_url:
            raise GitInvalidNameError("url is required to clone")
        if identity_id:
            from src import git_identities  # lazy: avoid an import-time cycle
            identity = git_identities.find_identity(owner, identity_id)
            if identity and identity.get("ssh_host"):
                rewritten = git_identities.rewrite_remote_alias(clone_url, identity["ssh_host"])
                if rewritten:
                    clone_url = rewritten
        clone_repo(clone_url, target, timeout=timeout_clone)
    else:
        os.makedirs(target, exist_ok=True)
        git_user_name = git_user_email = None
        if identity_id:
            from src import git_identities  # lazy: avoid an import-time cycle
            identity = git_identities.find_identity(owner, identity_id)
            if identity:
                git_user_name = identity.get("git_user_name")
                git_user_email = identity.get("git_user_email")
        init_repo(target, default_branch=default_branch, initial_commit=initial_commit,
                  repo_name=name, git_user_name=git_user_name, git_user_email=git_user_email)
    # The cached filesystem walk (`discover_repos_for_owner`) doesn't know
    # about `target` yet -- drop it so the very next list sees the new repo
    # instead of waiting out `_DISCOVERY_TTL`.
    invalidate_discovery_cache(owner)
    return target
