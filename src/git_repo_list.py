"""The Source control repo list (`GET /api/git/repos`) within a time budget.

The list ran `git status` for every discovered repo (89 on the live install,
04-11) on each load and on the rail's 15 s light poll. One big tree (a Unity
project: ~5 s on its own) plus a loaded machine pushed the full list past the
45 s route limit (504). Here:

- every repo's status runs on one shared, bounded pool (`_POOL`, 8 workers);
  a repo already being read is never read twice at once -- overlapping polls
  and loads wait on the same `Future`;
- a status younger than `STATUS_FRESH_S` is served from memory;
- the list answers by its deadline (`LIST_BUDGET_S` full, `LIGHT_BUDGET_S`
  light). A repo whose status is not back yet keeps its last known row
  (`status_state: "stale"`, `status_age_s`) or, never read yet, a row with the
  branch read from `.git/HEAD` (`status_state: "pending"`); its read keeps
  going in the background and the next request (the panel re-asks while
  `refreshing` is true) gets it. Nothing is dropped from the list;
- the full mode's per-repo config reads (`user.*`, `remote -v`, the chosen
  SSH identity) change only when a config file does: they are cached by the
  modification times of the repo's and the global git config.

Untracked files are still listed in full (`--untracked-files=all`): the rail's
dirty badge counts them, and the speed comes from not repeating the work.
"""
from __future__ import annotations

import os
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from src import git_panel

STATUS_FRESH_S = 5.0
LIST_BUDGET_S = 12.0
LIGHT_BUDGET_S = 6.0
EXTRAS_MAX_AGE_S = 600.0
STATUS_TIMEOUT_S = 60.0

_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="git-repo-list")
# Config reads are quick and rarely needed (cached by mtime): their own small
# pool, so they never queue behind a round of statuses.
_XPOOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="git-repo-config")
_lock = threading.Lock()
# path -> (computed_at epoch, parsed `_status_branch`)
_status_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
# path -> (config fingerprint, computed_at, {"user", "remotes", "local_identity"})
_extras_cache: Dict[str, Tuple[Tuple, float, Dict[str, Any]]] = {}
_inflight: Dict[Tuple[str, str], Future] = {}
# path -> seconds its last `git status` took: the slowest are queued first,
# so one big tree starts at once instead of behind 80 small ones.
_durations: Dict[str, float] = {}


def clear_cache() -> None:
    """Forget every cached status and config read (tests; a repo mutated by a route)."""
    with _lock:
        _status_cache.clear()
        _extras_cache.clear()
        _durations.clear()
        _version.clear()


_version: List[Any] = []  # [(read_at, version)]


def git_version() -> Optional[str]:
    """`git_panel.git_version()` (a `git --version` spawn) read once every 10 min."""
    with _lock:
        hit = _version[0] if _version else None
    if hit and time.monotonic() - hit[0] < 600.0:
        return hit[1]
    value = git_panel.git_version()
    with _lock:
        _version[:] = [(time.monotonic(), value)]
    return value


def forget(path: str) -> None:
    """Drop one repo's cached status, so its next listing reads it again."""
    with _lock:
        _status_cache.pop(path, None)


def _submit(kind: str, path: str, fn: Callable[[str], Any]) -> Future:
    """`fn(path)` on the shared pool, or the read of it already running."""
    key = (kind, path)
    with _lock:
        fut = _inflight.get(key)
        if fut is not None:
            return fut

        def run() -> Any:
            try:
                return fn(path)
            finally:
                with _lock:
                    if _inflight.get(key) is fut_box[0]:
                        _inflight.pop(key, None)

        fut_box: List[Future] = []
        fut = (_XPOOL if kind == "extras" else _POOL).submit(run)
        fut_box.append(fut)
        _inflight[key] = fut
        return fut


class _StatusUnavailable(Exception):
    """`git status` timed out: keep the last known row instead of an empty one."""


def _read_status(path: str) -> Dict[str, Any]:
    t0 = time.monotonic()
    proc = git_panel.run_git(path, "status", "--porcelain=v2", "-z", "--branch",
                             "--untracked-files=all", timeout=STATUS_TIMEOUT_S)
    with _lock:
        _durations[path] = time.monotonic() - t0
    if proc.returncode == 124:
        raise _StatusUnavailable(proc.stderr)
    # Any other failure parses as an empty status, as `_status_branch` does.
    status = git_panel.parse_status_v2_branch(proc.stdout if proc.returncode == 0 else "")
    with _lock:
        _status_cache[path] = (time.time(), status)
    return status


def _git_dir(path: str) -> Optional[str]:
    dot = os.path.join(path, ".git")
    if os.path.isdir(dot):
        return dot
    try:
        with open(dot, encoding="utf-8", errors="replace") as f:
            text = f.read().strip()
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    target = text[len("gitdir:"):].strip()
    return target if os.path.isabs(target) else os.path.normpath(os.path.join(path, target))


def _common_dir(git_dir: str) -> str:
    try:
        with open(os.path.join(git_dir, "commondir"), encoding="utf-8", errors="replace") as f:
            rel = f.read().strip()
    except OSError:
        return git_dir
    return rel if os.path.isabs(rel) else os.path.normpath(os.path.join(git_dir, rel))


def head_status(path: str) -> Dict[str, Any]:
    """What `.git/HEAD` alone says (branch or detached sha), shaped like a
    parsed status with nothing counted -- the row of a repo not read yet."""
    out: Dict[str, Any] = {
        "branch": None, "detached": False, "head_sha": None, "upstream": None,
        "ahead": 0, "behind": 0, "staged": [], "unstaged": [], "untracked": [], "conflicts": [],
    }
    gd = _git_dir(path)
    if not gd:
        return out
    try:
        with open(os.path.join(gd, "HEAD"), encoding="utf-8", errors="replace") as f:
            head = f.read().strip()
    except OSError:
        return out
    if head.startswith("ref:"):
        ref = head[4:].strip()
        out["branch"] = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
    elif head:
        out["detached"] = True
        out["head_sha"] = head
    return out


def _config_fingerprint(path: str) -> Tuple:
    gd = _git_dir(path) or os.path.join(path, ".git")
    files = [os.path.join(gd, "config"), os.path.join(_common_dir(gd), "config"),
             os.environ.get("GIT_CONFIG_GLOBAL") or os.path.join(os.path.expanduser("~"), ".gitconfig")]
    stamps: List[Any] = []
    for name in files:
        try:
            st = os.stat(name)
            stamps.append((st.st_mtime_ns, st.st_size))
        except OSError:
            stamps.append(None)
    return tuple(stamps)


def _read_extras(path: str) -> Dict[str, Any]:
    from src import git_identities
    fp = _config_fingerprint(path)
    preferred = git_panel.run_git(path, "config", "--local", "--get", git_identities._REPO_IDENTITY_CONFIG_KEY)
    extras = {
        "user": git_panel.repo_user(path),
        "remotes": git_panel.repo_remotes(path),
        "local_identity": preferred.stdout.strip() if preferred.returncode == 0 else "",
    }
    with _lock:
        _extras_cache[path] = (fp, time.time(), extras)
    return extras


def cached_extras(path: str) -> Optional[Dict[str, Any]]:
    """The config reads of `path` when no config file changed since."""
    with _lock:
        hit = _extras_cache.get(path)
    if not hit or time.time() - hit[1] >= EXTRAS_MAX_AGE_S:
        return None
    return hit[2] if hit[0] == _config_fingerprint(path) else None


def submit_side_read(fn: Callable[..., Any], *args: Any) -> Future:
    """Run a read the list needs besides the statuses (the identity list) on
    the config pool, so it overlaps the statuses instead of following them."""
    return _XPOOL.submit(fn, *args)


def fresh_for(duration: Optional[float]) -> float:
    """How long a status stays fresh: `STATUS_FRESH_S`, or three times what the
    repo's last read took (at most a minute), so a tree whose `git status`
    takes 10 s is not read again by every poll."""
    return max(STATUS_FRESH_S, min(60.0, 3.0 * float(duration or 0.0)))


def _done(fut: Optional[Future]) -> bool:
    return fut is not None and fut.done() and fut.exception() is None


def summaries(metas: Sequence[Dict[str, Any]], *, light: bool = False,
              deadline: Optional[float] = None) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Rows shaped like `git_panel.repo_summaries` (full mode adds `user`,
    `remotes` and the private `_local_identity`), answered by `deadline`
    (`time.monotonic()`; default now + the mode's budget), plus counts
    `{fresh, stale, pending, extras_pending}`. Each row carries
    `status_state` and `status_age_s`; a full row whose config reads are not
    back yet has `extras_pending: true` and empty `user`/`remotes`."""
    if deadline is None:
        deadline = time.monotonic() + (LIGHT_BUDGET_S if light else LIST_BUDGET_S)
    now = time.time()
    with _lock:
        hits = {m["path"]: _status_cache.get(m["path"]) for m in metas}
        slow = dict(_durations)
    futures: Dict[str, Future] = {}
    stale_paths = [m["path"] for m in metas
                   if hits[m["path"]] is None or now - hits[m["path"]][0] >= fresh_for(slow.get(m["path"]))]
    for path in sorted(stale_paths, key=lambda p: -slow.get(p, 1.0)):
        futures[path] = _submit("status", path, _read_status)
    plan = []
    # A repo whose last read took longer than the time left, and that has a
    # row to show meanwhile, is not waited for: it comes back `stale` at once
    # and its read finishes in the background (seen live: one Unity project
    # held every 15 s light poll to its full budget).
    remaining = deadline - time.monotonic()
    waiting: List[Future] = [fut for path, fut in futures.items()
                             if hits[path] is None or slow.get(path, 0.0) <= remaining]
    for meta in metas:
        path = meta["path"]
        extras = xfut = None
        if not light:
            extras = cached_extras(path)
            if extras is None:
                xfut = _submit("extras", path, _read_extras)
                waiting.append(xfut)
        plan.append((meta, hits[path], futures.get(path), extras, xfut))
    if waiting:
        wait(waiting, timeout=max(0.0, deadline - time.monotonic()))

    counts = {"fresh": 0, "stale": 0, "pending": 0, "extras_pending": 0}
    rows: List[Dict[str, Any]] = []
    for meta, hit, fut, extras, xfut in plan:
        path = meta["path"]
        if fut is None:
            status, at, state = hit[1], hit[0], "fresh"
        elif _done(fut):
            status, at, state = fut.result(), time.time(), "fresh"
        elif hit is not None:
            status, at, state = hit[1], hit[0], "stale"
        else:
            status, at, state = head_status(path), None, "pending"
        counts[state] += 1
        row = git_panel._summary_row(
            path, status, project_id=meta.get("project_id"), project_name=meta.get("project_name"),
            root_folder=meta.get("root_folder"), parent_repo_id=meta.get("parent_repo_id"),
            projects=meta.get("projects"), watched=bool(meta.get("watched")),
        )
        row["status_state"] = state
        row["status_age_s"] = round(max(0.0, time.time() - at), 1) if at is not None else None
        if not light:
            if extras is None and _done(xfut):
                extras = xfut.result()
            if extras is None:
                counts["extras_pending"] += 1
                row["extras_pending"] = True
                extras = {"user": {"name": "", "email": ""}, "remotes": [], "local_identity": None}
            row["user"] = extras["user"]
            row["remotes"] = extras["remotes"]
            row["_local_identity"] = extras["local_identity"]
        rows.append(row)
    return rows, counts
