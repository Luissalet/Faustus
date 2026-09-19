"""src/code_graph/risk.py — deterministic CHANGE-RISK score.

Turns the signals `code_graph.impact` and `code_graph.cochange` already
compute into a single 0..100 score with a level (low/medium/high), so the
agent (and the person reviewing its diff) can see how risky an edit is
before or after making it, without re-reading the whole call graph by hand.

Seven factors, each normalised to 0..1 against a fixed cap (so the score is
reproducible across runs and across repos, not scaled to "biggest seen in
this codebase"):

  fan_in          distinct callers reached within 2 hops of the seed symbols
  breadth         distinct files reached by that same walk
  test_coverage   inverse of how many test files reach the change (none = 1.0)
  churn           commits touching the seed file(s) in the last N commits
  coupling        top historical co-change confidence to a file NOT in the
                   change (config/template coupling a call graph can't see)
  size            lines added+removed in the diff, only when diff-seeded
  hub             seed file imported by many other modules (blast radius)

score = round(100 * sum(weight_i * normalised_i)) — the weights sum to 1.0,
so the score is a weighted percentage, not an arbitrary scale. `top_reasons`
is the 3 largest weight*normalised contributors; `suggestions` are derived
straight from those same numbers (never recomputed independently), so they
never disagree with the score.

Determinism: every factor comes from a single, ordered pass (BFS frontier
order, sorted paths, the same cached git-log parse `cochange.py` uses) — no
set-iteration order leaks into the score, and the same inputs always
produce the same output.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.context_engine import code_index

from .cochange import _history as _cochange_history
from .cochange import cochanges as _cochanges
from .query import (
    DEFAULT_OUTPUT_CHARS,
    _changed_seeds,
    _IMPACT_INDEX_BUDGET,
    _clip,
    _import_affected_tests,
    _is_test_path,
    _resolve_symbol,
    _root,
)

logger = logging.getLogger(__name__)

# Fixed caps a raw factor value is divided by before clamping to 1.0 -- the
# score is reproducible across repos, not scaled to "biggest seen here".
_FAN_IN_CAP = 20.0
_BREADTH_CAP = 15.0
_TEST_FILES_CAP = 3.0        # >= this many affected test files -> 0 risk from this factor
_CHURN_CAP = 15.0
_SIZE_CAP = 300.0
_HUB_CAP = 10.0
_HUB_MODULES_THRESHOLD = 5   # imported by >= this many other modules counts as a "hub"
# Matches `cochanges()`'s own default max_commits, so the cached, parsed git
# log `cochange._history` builds for the coupling factor is reused for churn
# instead of triggering a second `git log` subprocess.
_CHURN_LOOKBACK_COMMITS = 500
_FANIN_DEPTH = 2
_FANIN_NODE_CAP = 500

_WEIGHTS: Dict[str, float] = {
    "fan_in": 0.20,
    "breadth": 0.15,
    "test_coverage": 0.20,
    "churn": 0.15,
    "coupling": 0.10,
    "size": 0.10,
    "hub": 0.10,
}
assert abs(sum(_WEIGHTS.values()) - 1.0) < 1e-9, "risk weights must sum to 1.0"

# (score_below, level) checked in order -- fixed thresholds, not repo-relative.
_LEVEL_THRESHOLDS: Tuple[Tuple[float, str], ...] = (
    (30.0, "low"),
    (65.0, "medium"),
)


def _level(score: float) -> str:
    for cap, name in _LEVEL_THRESHOLDS:
        if score < cap:
            return name
    return "high"


def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)


# ── seed resolution ─────────────────────────────────────────────────────

def _is_path_like(item: str, root: str) -> bool:
    norm = item.replace("\\", "/").strip()
    if not norm:
        return False
    if "/" in norm and os.path.splitext(norm)[1]:
        return True
    abs_path = norm if os.path.isabs(norm) else os.path.join(root, norm)
    return os.path.isfile(abs_path)


def _rel_path(item: str, root: str) -> str:
    norm = item.replace("\\", "/").strip()
    if os.path.isabs(norm):
        try:
            return os.path.relpath(norm, root).replace("\\", "/")
        except ValueError:
            return norm
    return norm.lstrip("/")


def _resolve_explicit_seeds(items: Sequence[str], *, root: str, project_id: str
                             ) -> Tuple[List[str], List[str], List[str]]:
    """`(seed_paths, seed_symbol_ids, unresolved)` for user-given paths/symbols.

    A path-like item seeds every non-module symbol defined in that file (so
    fan-in/breadth walk from all its callables); a symbol name resolves to
    its one definition, same as `impact()`."""
    seed_paths: List[str] = []
    seed_symbol_ids: List[str] = []
    unresolved: List[str] = []
    for raw in items:
        item = str(raw or "").strip()
        if not item:
            continue
        if _is_path_like(item, root):
            rel = _rel_path(item, root)
            if rel not in seed_paths:
                seed_paths.append(rel)
            for sym in code_index.symbols_in(rel, workspace=root, project_id=project_id):
                if sym.kind != "module":
                    seed_symbol_ids.append(sym.id)
            continue
        sym = _resolve_symbol(item, root=root, project_id=project_id)
        if not sym:
            unresolved.append(item)
            continue
        path = str(sym["path"])
        if path not in seed_paths:
            seed_paths.append(path)
        seed_symbol_ids.append(sym["id"])
    return seed_paths, seed_symbol_ids, unresolved


def _diff_seeds(root: str, *, base_ref: str, project_id: str
                 ) -> Tuple[List[str], List[str]]:
    """`(seed_paths, seed_symbol_ids)` from the current git diff, same
    resolution `impact()` uses in diff mode."""
    changed = _changed_seeds(root, base_ref=base_ref, project_id=project_id)
    seed_paths: List[str] = []
    seed_symbol_ids: List[str] = []
    for entry in changed:
        path = str(entry.get("path") or "")
        if path and path not in seed_paths:
            seed_paths.append(path)
        seed_symbol_ids.append(entry["id"])
    return seed_paths, seed_symbol_ids


def _diff_numstat(root: str, base_ref: str) -> int:
    """Total added+removed lines from `git diff --numstat` -- the raw size
    factor when the change is diff-seeded."""
    git = shutil.which("git")
    if not git:
        return 0
    try:
        proc = subprocess.run(
            [git, "diff", "--numstat", "--no-color", str(base_ref or "HEAD")],
            cwd=root, capture_output=True, text=True, timeout=30, shell=False)
    except (OSError, subprocess.TimeoutExpired):
        return 0
    if proc.returncode not in (0, 1):
        return 0
    total = 0
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        added, removed = parts[0], parts[1]
        try:
            total += int(added) + int(removed)
        except ValueError:
            continue  # binary file: "-\t-\tpath"
    return total


# ── factor computation ──────────────────────────────────────────────────

def _fanin_and_breadth(seed_symbol_ids: Sequence[str], seed_paths: Sequence[str],
                        ) -> Tuple[int, int, List[str]]:
    """BFS over incoming `calls` edges up to `_FANIN_DEPTH` hops from the
    seed symbols -- the same walk `impact()` does, only needing magnitude
    here rather than the full reachable set."""
    seed_ids = list(dict.fromkeys(seed_symbol_ids))
    seed_id_set = set(seed_ids)
    seed_path_set = set(seed_paths)
    visited = set(seed_id_set)
    frontier = list(seed_ids)
    reached_symbols: set = set()
    reached_paths: List[str] = []
    reached_path_set: set = set()
    d = 0
    while frontier and d < _FANIN_DEPTH and len(reached_symbols) < _FANIN_NODE_CAP:
        d += 1
        nxt: List[str] = []
        for sym_id in frontier:
            for hop in code_index.neighbors(sym_id, kinds=("calls",)):
                if hop.get("direction") != "in" or not hop.get("resolved"):
                    continue
                other = str(hop.get("symbol_id") or "")
                if not other or other in seed_id_set:
                    continue
                if other not in reached_symbols:
                    reached_symbols.add(other)
                    path = str(hop.get("path") or "")
                    if path and path not in seed_path_set and path not in reached_path_set:
                        reached_path_set.add(path)
                        reached_paths.append(path)
                if other not in visited:
                    visited.add(other)
                    nxt.append(other)
        frontier = nxt
    reached_paths.sort()
    return len(reached_symbols), len(reached_paths), reached_paths


def _hub_count(root: str, project_id: str, seed_paths: Sequence[str]
               ) -> Tuple[int, str]:
    """`(max distinct importing modules, which seed path)` -- the seed file
    most other modules import, i.e. the widest blast radius if it breaks."""
    best_n = 0
    best_path = ""
    for path in seed_paths:
        try:
            module_sym = next(
                (s for s in code_index.symbols_in(path, workspace=root, project_id=project_id)
                 if s.kind == "module"), None)
        except Exception:  # noqa: BLE001
            module_sym = None
        if not module_sym:
            continue
        try:
            hops = code_index.neighbors(module_sym.id, kinds=("imports",))
        except Exception:  # noqa: BLE001
            hops = []
        importers = {str(h.get("path") or "") for h in hops
                     if h.get("direction") == "in" and h.get("resolved")
                     and str(h.get("path") or "") != path}
        n = len(importers)
        if n > best_n:
            best_n, best_path = n, path
    return best_n, best_path


def _churn_count(root: str, seed_paths: Sequence[str]) -> int:
    """Commits touching any seed file in the last `_CHURN_LOOKBACK_COMMITS`
    commits, reusing `cochange.py`'s cached, already-parsed git log."""
    if not seed_paths:
        return 0
    history = _cochange_history(root, max_commits=_CHURN_LOOKBACK_COMMITS)
    seed_set = set(seed_paths)
    return sum(1 for _sha, _date, files in history if files & seed_set)


def _test_coverage_files(root: str, project_id: str, seed_paths: Sequence[str],
                          call_reached_paths: Sequence[str]) -> List[str]:
    test_files: set = set(p for p in call_reached_paths if _is_test_path(p))
    test_files |= {p for p in seed_paths if _is_test_path(p)}
    test_files |= set(_import_affected_tests(root, project_id, seed_paths))
    return sorted(test_files)


# ── main entry point ────────────────────────────────────────────────────

def change_risk(paths_or_symbols: Optional[Sequence[str]] = None, *, workspace: str = "",
                 base_ref: str = "HEAD", project_id: str = "",
                 output_chars: int = DEFAULT_OUTPUT_CHARS) -> Dict[str, Any]:
    """Deterministic 0..100 change-risk score for an edit.

    Seeds are `paths_or_symbols` (file paths and/or symbol names) when
    given, otherwise every symbol the current git diff vs `base_ref`
    touches -- the same seeding `impact()` uses. Returns `score`, `level`
    (low/medium/high), the per-factor breakdown (`factors`), `top_reasons`
    (the 3 biggest contributors) and `suggestions`."""
    try:
        root = _root(workspace)
    except ValueError as exc:
        return {"error": str(exc), "exit_code": 1}
    try:
        code_index.refresh(root, project_id=project_id, budget_files=_IMPACT_INDEX_BUDGET)
    except Exception as exc:  # noqa: BLE001
        logger.warning("code_graph.change_risk refresh failed: %s", exc)

    items = [str(p) for p in (paths_or_symbols or []) if str(p or "").strip()]
    diff_seeded = not items
    unresolved: List[str] = []
    if items:
        mode = "explicit"
        seed_paths, seed_symbol_ids, unresolved = _resolve_explicit_seeds(
            items, root=root, project_id=project_id)
    else:
        mode = "diff"
        seed_paths, seed_symbol_ids = _diff_seeds(root, base_ref=base_ref, project_id=project_id)

    if not seed_paths and not seed_symbol_ids:
        msg = (f"no changes vs {base_ref}" if mode == "diff"
               else f"nothing resolved from: {', '.join(items)}")
        return {"output": msg, "exit_code": 0 if mode == "diff" else 1, "root": root,
                "mode": mode, "seeds": [], "unresolved": unresolved,
                "score": 0, "level": "low", "factors": {}, "top_reasons": [],
                "suggestions": []}

    fan_in_raw, breadth_raw, call_reached_paths = _fanin_and_breadth(seed_symbol_ids, seed_paths)
    test_files = _test_coverage_files(root, project_id, seed_paths, call_reached_paths)
    test_raw = len(test_files)
    churn_raw = _churn_count(root, seed_paths)

    coupling_result = _cochanges(seed_paths, workspace=root, max_commits=_CHURN_LOOKBACK_COMMITS,
                                  limit=10, min_support=2)
    # A file's own tests changing with it is coverage, not hidden coupling.
    coupling_top = next((c for c in (coupling_result.get("results") or [])
                         if c.get("path") not in test_files), None)
    coupling_raw = float(coupling_top["confidence"]) if coupling_top else 0.0

    size_raw = _diff_numstat(root, base_ref) if diff_seeded else 0

    hub_raw, hub_path = _hub_count(root, project_id, seed_paths)

    factors: Dict[str, Dict[str, Any]] = {
        "fan_in": {
            "raw": fan_in_raw, "normalized": round(_clamp01(fan_in_raw / _FAN_IN_CAP), 4),
            "weight": _WEIGHTS["fan_in"],
            "reason": f"{fan_in_raw} distinct caller(s) reachable within {_FANIN_DEPTH} hops",
        },
        "breadth": {
            "raw": breadth_raw, "normalized": round(_clamp01(breadth_raw / _BREADTH_CAP), 4),
            "weight": _WEIGHTS["breadth"],
            "reason": f"{breadth_raw} distinct file(s) reached by the call graph",
        },
        "test_coverage": {
            "raw": test_raw,
            "normalized": round(_clamp01(1.0 - min(test_raw, _TEST_FILES_CAP) / _TEST_FILES_CAP), 4),
            "weight": _WEIGHTS["test_coverage"],
            "reason": ("no tests reach this change" if test_raw == 0
                       else f"{test_raw} test file(s) reach this change"),
        },
        "churn": {
            "raw": churn_raw, "normalized": round(_clamp01(churn_raw / _CHURN_CAP), 4),
            "weight": _WEIGHTS["churn"],
            "reason": f"{churn_raw} commit(s) touched these file(s) in the last "
                      f"{_CHURN_LOOKBACK_COMMITS} commits",
        },
        "coupling": {
            "raw": coupling_raw, "normalized": round(_clamp01(coupling_raw), 4),
            "weight": _WEIGHTS["coupling"],
            "reason": (f"top co-change: {coupling_top['path']} at confidence "
                       f"{coupling_raw:.2f}") if coupling_top else "no historical co-change coupling found",
        },
        "size": {
            "raw": size_raw, "normalized": round(_clamp01(size_raw / _SIZE_CAP), 4),
            "weight": _WEIGHTS["size"],
            "reason": (f"{size_raw} line(s) added+removed in the diff" if diff_seeded
                       else "not diff-seeded -- change size unknown"),
        },
        "hub": {
            "raw": hub_raw, "normalized": round(_clamp01(hub_raw / _HUB_CAP), 4),
            "weight": _WEIGHTS["hub"],
            "reason": (f"{hub_path} is imported by {hub_raw} other module(s)" if hub_raw
                       else "not imported by other modules (or no module found)"),
        },
    }

    contributions = {name: f["weight"] * f["normalized"] for name, f in factors.items()}
    score = round(100.0 * sum(contributions.values()), 1)
    level = _level(score)

    ranked = sorted(factors.items(), key=lambda kv: -contributions[kv[0]])
    top_reasons = [
        {"factor": name, "contribution": round(contributions[name] * 100, 1),
         "reason": f["reason"]}
        for name, f in ranked[:3] if contributions[name] > 0
    ]

    suggestions: List[str] = []
    py_tests = [t for t in test_files if t.endswith(".py")]
    if py_tests:
        cmd = "python -m pytest -q " + " ".join(py_tests[:30])
        suggestions.append(f"run these tests: {cmd}")
    elif test_files:
        suggestions.append(f"run these tests: {', '.join(test_files[:30])}")
    else:
        top_seed = seed_paths[0] if seed_paths else "this change"
        suggestions.append(f"no tests reach this change -- add one covering {top_seed}")
    if coupling_top:
        suggestions.append(
            f"also review {coupling_top['path']} (historically co-changed, "
            f"confidence {coupling_raw:.2f})")
    if hub_raw >= _HUB_MODULES_THRESHOLD:
        suggestions.append(
            f"{hub_path} is a hub imported by {hub_raw} modules -- changes here have a wide "
            "blast radius, review callers carefully")

    lines = [f"Change risk: {score}/100 ({level})"]
    lines.append(f"seeds: {', '.join(seed_paths) if seed_paths else '(none)'}")
    if unresolved:
        lines.append(f"unresolved: {', '.join(unresolved)}")
    lines.append("top reasons:")
    lines += [f"  {r['factor']} (+{r['contribution']}): {r['reason']}" for r in top_reasons]
    lines.append("suggestions:")
    lines += [f"  - {s}" for s in suggestions]

    return {
        "output": _clip("\n".join(lines), output_chars), "exit_code": 0, "root": root,
        "mode": mode, "base_ref": base_ref, "seeds": seed_paths, "unresolved": unresolved,
        "score": score, "level": level, "factors": factors,
        "top_reasons": top_reasons, "suggestions": suggestions,
    }


def compact_risk(paths_or_symbols: Optional[Sequence[str]] = None, *, workspace: str = "",
                  base_ref: str = "HEAD", project_id: str = "") -> Dict[str, Any]:
    """A small `{score, level, top_reasons}` block for embedding in another
    tool's output (see `impact(include_risk=True)`) without repeating the
    full factor breakdown or a second rendered `output` string."""
    full = change_risk(paths_or_symbols, workspace=workspace, base_ref=base_ref,
                        project_id=project_id)
    if "error" in full:
        return {}
    return {
        "score": full["score"], "level": full["level"],
        "top_reasons": full["top_reasons"], "suggestions": full["suggestions"],
    }
