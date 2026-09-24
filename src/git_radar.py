"""git_radar.py -- which repositories are waiting for a commit or a push.

The Source control panel (`src/git_panel.py`, OBJ-4) answers "what is the
state of THIS repo". The radar answers the question that gets forgotten:
across every repository Faustus can see -- project links plus the
install-wide `git_watch_roots` folders -- which ones have work that never
left the machine?  A change made from a chat, then the tab closed, then
nothing pushed for a week, is exactly what this exists to surface.

It is read-only and built on the panel's own discovery and status calls
(one `git status --porcelain=v2 --branch` per repo, in parallel), so it
adds no second notion of "repo" and no second walk. What it adds is the
classification:

* ``conflicts``   -- unresolved merge/rebase entries
* ``uncommitted`` -- staged, unstaged or untracked files (count given)
* ``unpushed``    -- commits ahead of the tracking branch (count given)
* ``no_upstream`` -- on a branch that tracks nothing, while the repo DOES
                     have a remote (a `git push -u` away from safe)
* ``local_only``  -- no remote at all: nothing here has ever left the disk
* ``behind``      -- commits on the tracking branch not pulled yet (info)
* ``detached``    -- detached HEAD (info)

The first five are "attention": they are what the rail badge and the Home
block count. `behind`/`detached` are reported on the row but never counted
-- being behind is not something the person forgot to do.

The scan is cached per owner for `RADAR_TTL` seconds because three callers
poll it (rail badge, Home block, Source control strip); `refresh=True`
bypasses the cache and the discovery cache both.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional, Sequence

from src import git_panel

logger = logging.getLogger(__name__)

RADAR_TTL = 20.0
ATTENTION_KINDS = ("conflicts", "uncommitted", "unpushed", "no_upstream", "local_only")
INFO_KINDS = ("behind", "detached")
# Severity order for sorting: what loses work first comes first.
_ORDER = {k: i for i, k in enumerate(ATTENTION_KINDS + INFO_KINDS)}

_LOCK = threading.Lock()
# owner-key -> (monotonic ts, payload)
_CACHE: Dict[str, tuple[float, Dict[str, Any]]] = {}


def invalidate(owner: Optional[str] = None) -> None:
    with _LOCK:
        if owner is None:
            _CACHE.clear()
        else:
            _CACHE.pop(git_panel._owner_cache_key(owner), None)


def _last_commit_epoch(repo_path: str) -> Optional[int]:
    proc = git_panel.run_git(repo_path, "log", "-1", "--format=%ct")
    if proc.returncode != 0:
        return None
    raw = proc.stdout.strip()
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def classify(row: Dict[str, Any], *, has_remote: Optional[bool] = None) -> List[Dict[str, Any]]:
    """The reasons one `repo_summary` row needs attention, most severe
    first. `has_remote` is only consulted when the row has no upstream:
    None means "unknown" and yields `no_upstream` (the safer reading)."""
    reasons: List[Dict[str, Any]] = []
    dirty = row.get("dirty") or {}
    conflicts = int(row.get("conflicts") or 0)
    if conflicts > 0:
        reasons.append({"kind": "conflicts", "count": conflicts})
    dirty_total = int(dirty.get("staged", 0)) + int(dirty.get("unstaged", 0)) + int(dirty.get("untracked", 0))
    if dirty_total > 0:
        reasons.append({"kind": "uncommitted", "count": dirty_total})
    ahead = int(row.get("ahead") or 0)
    behind = int(row.get("behind") or 0)
    if row.get("upstream"):
        if ahead > 0:
            reasons.append({"kind": "unpushed", "count": ahead})
        if behind > 0:
            reasons.append({"kind": "behind", "count": behind})
    elif row.get("branch") and row.get("head_sha"):
        # A branch with commits and nothing to compare against.
        if has_remote is False:
            reasons.append({"kind": "local_only", "count": 0})
        else:
            reasons.append({"kind": "no_upstream", "count": 0})
    if row.get("detached"):
        reasons.append({"kind": "detached", "count": 0})
    reasons.sort(key=lambda r: _ORDER.get(r["kind"], 99))
    return reasons


def _row_from_summary(summary: Dict[str, Any], *, has_remote: Optional[bool]) -> Dict[str, Any]:
    reasons = classify(summary, has_remote=has_remote)
    attention = [r for r in reasons if r["kind"] in ATTENTION_KINDS]
    return {
        "id": summary["id"],
        "name": summary["name"],
        "path": summary["path"],
        "project_id": summary.get("project_id"),
        "project_name": summary.get("project_name"),
        "projects": summary.get("projects") or [],
        "root_folder": summary.get("root_folder"),
        "watched": bool(summary.get("watched")),
        "branch": summary.get("branch"),
        "detached": bool(summary.get("detached")),
        "upstream": summary.get("upstream"),
        "ahead": int(summary.get("ahead") or 0),
        "behind": int(summary.get("behind") or 0),
        "dirty": summary.get("dirty") or {"staged": 0, "unstaged": 0, "untracked": 0},
        "conflicts": int(summary.get("conflicts") or 0),
        "reasons": reasons,
        "attention": bool(attention),
        "severity": min((_ORDER[r["kind"]] for r in attention), default=None),
        "last_commit_at": None,
    }


def scan(owner: Optional[str], *, refresh: bool = False) -> Dict[str, Any]:
    """The radar payload for `owner`: every visible repo classified, the
    attention subset first (sorted by severity, then name), counts per
    kind, the watched roots in force, and when it was computed."""
    key = git_panel._owner_cache_key(owner)
    if not refresh:
        with _LOCK:
            hit = _CACHE.get(key)
        if hit is not None and (time.monotonic() - hit[0]) < RADAR_TTL:
            return hit[1]

    if refresh:
        git_panel.invalidate_discovery_cache(owner)
    metas = git_panel.discover_repos_for_owner(owner, use_cache=not refresh) or []
    roots = git_panel.watch_roots()
    git_ok = git_panel.git_available()

    rows: List[Dict[str, Any]] = []
    if git_ok and metas:
        summaries = git_panel.repo_summaries(metas, light=True)
        for summary in summaries:
            has_remote: Optional[bool] = None
            if not summary.get("upstream") and summary.get("branch"):
                # Only the few repos with no tracking branch pay this call.
                try:
                    has_remote = bool(git_panel.repo_remotes(summary["path"]))
                except Exception:  # noqa: BLE001 - unknown stays "no_upstream"
                    has_remote = None
            rows.append(_row_from_summary(summary, has_remote=has_remote))
        for row in rows:
            if row["attention"]:
                try:
                    row["last_commit_at"] = _last_commit_epoch(row["path"])
                except Exception:  # noqa: BLE001
                    row["last_commit_at"] = None

    rows.sort(key=lambda r: (0 if r["attention"] else 1, r["severity"] if r["severity"] is not None else 99,
                             r["name"].lower()))
    counts = {k: 0 for k in ATTENTION_KINDS + INFO_KINDS}
    for row in rows:
        for reason in row["reasons"]:
            counts[reason["kind"]] += 1
    attention_rows = [r for r in rows if r["attention"]]
    payload = {
        "repos": rows,
        "attention": attention_rows,
        "attention_count": len(attention_rows),
        "counts": counts,
        "total": len(rows),
        "watch_roots": roots,
        "git_version": git_panel.git_version() if git_ok else None,
        "scanned_at": time.time(),
    }
    with _LOCK:
        _CACHE[key] = (time.monotonic(), payload)
    return payload


def set_watch_roots(roots: Sequence[Any]) -> Dict[str, Any]:
    """Validate and persist `git_watch_roots`. Every entry must be an
    absolute path to an existing directory; the first bad one is reported
    (`{"ok": False, "error": ..., "path": ...}`) and nothing is written.
    Returns the normalized list that was stored."""
    from src.settings import update_settings

    cleaned: List[str] = []
    seen = set()
    for raw in roots:
        if not isinstance(raw, str) or not raw.strip():
            return {"ok": False, "error": "Each folder must be a non-empty path", "path": raw}
        p = raw.strip()
        if not os.path.isabs(p):
            return {"ok": False, "error": "Folder must be an absolute path", "path": p}
        real = git_panel._normalize_root(p)
        if real is None:
            return {"ok": False, "error": "Folder does not exist or is not a directory", "path": p}
        k = os.path.normcase(real)
        if k in seen:
            continue
        seen.add(k)
        cleaned.append(real)
    update_settings({"git_watch_roots": cleaned})
    git_panel.invalidate_discovery_cache()
    invalidate()
    return {"ok": True, "watch_roots": cleaned}


def format_summary(payload: Dict[str, Any]) -> str:
    """A one-paragraph plain-text account of the radar -- for the agent's
    context, a notification, or a Home card. Never lists more than 12."""
    att = payload.get("attention") or []
    if not att:
        return f"All {payload.get('total', 0)} repositories are committed and pushed."
    parts = []
    for row in att[:12]:
        bits = []
        for r in row["reasons"]:
            if r["kind"] == "uncommitted":
                bits.append(f"{r['count']} uncommitted")
            elif r["kind"] == "unpushed":
                bits.append(f"{r['count']} unpushed")
            elif r["kind"] == "conflicts":
                bits.append(f"{r['count']} conflicts")
            elif r["kind"] == "no_upstream":
                bits.append("no upstream")
            elif r["kind"] == "local_only":
                bits.append("no remote")
        parts.append(f"{row['name']} ({row.get('branch') or 'detached'}): " + ", ".join(bits))
    more = len(att) - 12
    tail = f" … and {more} more" if more > 0 else ""
    return f"{len(att)} of {payload.get('total', 0)} repositories need attention: " + "; ".join(parts) + tail
