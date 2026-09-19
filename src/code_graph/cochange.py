"""src/code_graph/cochange.py — historical co-change signal.

Static call/import graphs miss coupling that only git history shows: a
config file, a template, an i18n bundle or a test that always changes
alongside some module but is never imported/called by it. This module mines
`git log` for that pattern: "files that usually change together with this
one".

One `git log --no-merges --name-only` call per (workspace, HEAD sha,
max_commits) — cached in-process, same spirit as `query.py`'s
subprocess/timeout/cwd guard for `detect_changes`, confined to the
workspace root through the same `_root` (`query._root`, re-exported here to
avoid an import cycle). A commit touching more than `_MAX_COMMIT_FILES`
files (a mass rename or a formatter pass) is dropped entirely — it would
otherwise link every file in the repo to every other file. Lockfiles and
other generated artifacts are dropped per-file: they change with nearly
everything and would drown out real coupling.

For each other file seen alongside the target(s):
  support    = number of (kept) commits touching both
  confidence = support / (number of kept commits touching the target)
  score      = recency-weighted sum, exponential decay by commit index
               (index 0 = most recent), half-life ~100 commits — a file
               that co-changed with the target 5 commits ago counts far
               more than one that last did so 400 commits ago, even at
               equal raw support.

This is a correlation signal, not a dependency — `impact()` labels it
accordingly ("often changed together", never "depends on").
"""
from __future__ import annotations

import logging
import math
import os
import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

logger = logging.getLogger(__name__)

_MAX_COMMIT_FILES = 50
_HALF_LIFE_COMMITS = 100.0
_RECORD_SEP = "\x1f"
_COMMIT_MARK = "\x01commit\x01"

_LOCKFILE_RE = re.compile(
    r"(^|/)(package-lock\.json|npm-shrinkwrap\.json|yarn\.lock|pnpm-lock\.ya?ml|"
    r"Pipfile\.lock|poetry\.lock|uv\.lock|Cargo\.lock|composer\.lock|go\.sum|"
    r"Gemfile\.lock|mix\.lock)$"
)
_GENERATED_RE = re.compile(
    r"\.(min\.js|min\.css|map)$|(^|/)(dist|build|node_modules|vendor|"
    r"__pycache__|\.next|target)/"
)

# (workspace, head_sha, max_commits) -> parsed, filtered commit history.
_CACHE: Dict[Tuple[str, str, int], List[Tuple[str, str, frozenset]]] = {}
_CACHE_ORDER: List[Tuple[str, str, int]] = []
_CACHE_MAX = 16


def _is_noise_path(path: str) -> bool:
    return bool(_LOCKFILE_RE.search(path) or _GENERATED_RE.search(path))


def _head_sha(root: str, git: str) -> str:
    try:
        proc = subprocess.run(
            [git, "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True,
            timeout=10, shell=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def _fetch_history(root: str, *, max_commits: int) -> List[Tuple[str, str, frozenset]]:
    """[(short_sha, date, frozenset(files)), ...] newest-first, huge commits
    (mass renames/formatting) dropped entirely, noise files dropped per-file."""
    git = shutil.which("git")
    if not git:
        return []
    try:
        proc = subprocess.run(
            [git, "log", "--no-merges", "--name-only", "--date=short",
             f"--pretty=format:{_COMMIT_MARK}%h{_RECORD_SEP}%ad",
             "-n", str(max_commits)],
            cwd=root, capture_output=True, text=True, timeout=30, shell=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("code_graph.cochanges: git log failed: %s", exc)
        return []
    if proc.returncode != 0:
        return []

    commits: List[Tuple[str, str, frozenset]] = []
    cur_sha: Optional[str] = None
    cur_date = ""
    cur_files: List[str] = []

    def _flush() -> None:
        if cur_sha is None:
            return
        if len(cur_files) > _MAX_COMMIT_FILES:
            return  # mass rename / formatter pass — would link everything
        kept = frozenset(f for f in cur_files if not _is_noise_path(f))
        if kept:
            commits.append((cur_sha, cur_date, kept))

    for line in proc.stdout.splitlines():
        if line.startswith(_COMMIT_MARK):
            _flush()
            rest = line[len(_COMMIT_MARK):]
            sha, _, date = rest.partition(_RECORD_SEP)
            cur_sha, cur_date, cur_files = sha, date, []
        elif line.strip():
            cur_files.append(line.strip())
    _flush()
    return commits


def _history(root: str, *, max_commits: int) -> List[Tuple[str, str, frozenset]]:
    git = shutil.which("git")
    if not git:
        return []
    head = _head_sha(root, git)
    key = (root, head, int(max_commits))
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    history = _fetch_history(root, max_commits=max_commits)
    _CACHE[key] = history
    _CACHE_ORDER.append(key)
    if len(_CACHE_ORDER) > _CACHE_MAX:
        stale = _CACHE_ORDER.pop(0)
        _CACHE.pop(stale, None)
    return history


def _decay(index: int) -> float:
    return 0.5 ** (index / _HALF_LIFE_COMMITS)


def cochanges(path_or_paths: Union[str, Sequence[str]], *, workspace: str = "",
              max_commits: int = 500, limit: int = 15,
              min_support: int = 2) -> Dict[str, Any]:
    """Files that historically change together with `path_or_paths`.

    `path_or_paths` is one workspace-relative path or a list of them (a
    diff's seed files); a commit "touches the target" if it touches any of
    them, and files that are themselves a target are never suggested."""
    from .query import _root, _clip, DEFAULT_OUTPUT_CHARS  # local: avoid import cycle

    try:
        root = _root(workspace)
    except ValueError as exc:
        return {"error": str(exc), "exit_code": 1}

    targets = [path_or_paths] if isinstance(path_or_paths, str) else list(path_or_paths or [])
    targets = [t.replace("\\", "/").strip() for t in targets if str(t or "").strip()]
    if not targets:
        return {"output": "no path given", "exit_code": 1, "results": [], "root": root}
    target_set = set(targets)

    try:
        max_commits_n = max(1, min(int(max_commits or 500), 5000))
    except (TypeError, ValueError):
        max_commits_n = 500
    try:
        limit_n = max(1, min(int(limit or 15), 200))
    except (TypeError, ValueError):
        limit_n = 15
    try:
        min_support_n = max(1, int(min_support or 2))
    except (TypeError, ValueError):
        min_support_n = 2

    history = _history(root, max_commits=max_commits_n)
    if not history:
        return {"output": "no git history available", "exit_code": 0, "results": [],
                "root": root, "targets": targets, "commits_scanned": 0}

    target_commit_count = 0
    support: Dict[str, int] = {}
    score: Dict[str, float] = {}
    last_seen: Dict[str, Tuple[str, str]] = {}  # other path -> (sha, date), most recent first

    for idx, (sha, date, files) in enumerate(history):
        if not files & target_set:
            continue
        target_commit_count += 1
        weight = _decay(idx)
        for other in files:
            if other in target_set:
                continue
            support[other] = support.get(other, 0) + 1
            score[other] = score.get(other, 0.0) + weight
            if other not in last_seen:
                last_seen[other] = (sha, date)

    results: List[Dict[str, Any]] = []
    for other, n in support.items():
        if n < min_support_n:
            continue
        confidence = n / target_commit_count if target_commit_count else 0.0
        sha, date = last_seen.get(other, ("", ""))
        results.append({
            "path": other,
            "support": n,
            "confidence": round(confidence, 4),
            "score": round(score.get(other, 0.0), 4),
            "last_seen": {"commit": sha, "date": date},
            "exists_now": os.path.isfile(os.path.join(root, other)),
        })
    results.sort(key=lambda e: (-e["score"], -e["support"], e["path"]))
    results = results[:limit_n]

    lines = [f"Often changed together with {', '.join(targets)} "
             f"({target_commit_count} commit(s) touching target, "
             f"{len(history)} scanned):"]
    for r in results:
        gone = "" if r["exists_now"] else "  (no longer exists)"
        lines.append(
            f"  {r['path']}  support={r['support']} confidence={r['confidence']:.2f} "
            f"last={r['last_seen']['commit']}/{r['last_seen']['date']}{gone}")
    if not results:
        lines.append("  (no file meets min_support)")

    return {
        "output": _clip("\n".join(lines), DEFAULT_OUTPUT_CHARS), "exit_code": 0,
        "root": root, "targets": targets, "results": results,
        "commits_scanned": len(history), "target_commit_count": target_commit_count,
    }
