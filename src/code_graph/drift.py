"""src/code_graph/drift.py — architecture drift: baseline a workspace's
code-graph shape, then say what moved since.

Two calls:

* `snapshot` records a baseline: this workspace's level-0 communities (each
  with its files, purpose, cross-community coupling and an approximate
  "public API" — the union of its routes, entry points and top internal
  fan-in symbols, the same fields `code_graph.communities` already
  computes), its execution flows (`code_graph.flows`) and its current
  hotspots (`code_graph.get_architecture`'s own list — reused, not
  recomputed). Persisted with its own id and timestamp, keyed by
  `(workspace, project_id)`; a workspace can hold many baselines.

* `drift` recomputes the same shape today and compares it against a
  baseline (the most recent one for this workspace by default). It reports:

    - communities that appeared or disappeared,
    - files that moved from one community to another,
    - new coupling between communities (and, more seriously, a new edge
      into a community the baseline showed no coupling into at all),
    - whether a dependency cycle among communities exists now but did not
      in the baseline,
    - flows that gained/lost steps, disappeared, or changed criticality
      by a meaningful amount,
    - a public-API symbol the baseline recorded that no longer resolves in
      the graph, still found by a bounded text search of the current files
      (a plain rename/removal a caller was never updated for),

  each weighted into a single 0..100 drift score, with the highest-severity
  findings surfaced first and explained in one line each.

MATCHING ACROSS SNAPSHOTS. A community's `id` is a hash of its exact file
set (`communities._community_id`) — it changes the instant a single file
moves in or out, which makes id equality useless for "is this the same
community as before". Communities are matched instead by file-set overlap
(Jaccard similarity, `_JACCARD_MATCH_THRESHOLD`): the current community that
shares the most files with a baseline one, above the threshold, is treated
as its successor. A flow's `id` is similarly content-derived
(`flows._flow_id`, hashed from the entry point AND every member reached) —
flows are matched by `entry_symbol` (the qualname), which is stable across
an edit that only changes what the flow reaches.

HONESTY. This is a thin, deterministic layer over `code_graph.communities`/
`code_graph.flows`, the same contract as the rest of this package: nothing
here is a language-server-grade refactor detector, so a finding is phrased
as what was actually observed (file-set overlap crossed a threshold, a
symbol no longer resolves) rather than as a claim about intent ("X was
renamed to Y"). The cycle check is a boolean presence test, not an
enumeration of every cycle — cheap, and the score only needs to know
whether one now exists that did not before. A time-boxed comparison that
runs out of budget stops and marks `"partial": True` on whichever finding
category it was still working through, rather than silently returning less
than it looks like it returned.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import sqlite3
import time
import uuid
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from src.context_engine import code_index
from src.context_engine import store as ce_store

from .query import DEFAULT_OUTPUT_CHARS, _clip, _root, get_architecture
from .communities import communities as _communities
from .flows import flows as _flows

logger = logging.getLogger(__name__)

ce_store.register_schema("code_graph_baselines", (
    """
    CREATE TABLE IF NOT EXISTS code_graph_baselines (
        workspace    TEXT NOT NULL DEFAULT '',
        project_id   TEXT NOT NULL DEFAULT '',
        id           TEXT NOT NULL DEFAULT '',
        label        TEXT NOT NULL DEFAULT '',
        fingerprint  TEXT NOT NULL DEFAULT '',
        data         TEXT NOT NULL DEFAULT '{}',
        created_at   TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (workspace, project_id, id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_code_graph_baselines_scope "
    "ON code_graph_baselines(workspace, project_id, created_at)",
))

_INDEX_BUDGET_FILES = 20000
#: Two communities across snapshots are "the same" when they share at least
#: this fraction of their combined file set (Jaccard). A third is the same
#: order of magnitude as the fold thresholds `communities.py` already uses
#: for "is this still basically one group" -- low enough that a handful of
#: files moving in/out does not manufacture a false new/removed community,
#: high enough that two genuinely different, similarly-sized communities
#: never accidentally match.
_JACCARD_MATCH_THRESHOLD = 0.34
#: A repo with fewer production files than this is not worth a drift check
#: at all -- everything "moves" when there are only three files.
DEFAULT_MIN_FILES = 8
#: Wall-clock ceiling for `drift()` end to end, including the bounded text
#: search for removed-but-still-referenced public symbols.
DEFAULT_TIME_BUDGET_S = 10.0
#: A flow's member count changing by at least this many steps is reported.
_FLOW_SIZE_DELTA = 3
#: A flow's criticality changing by at least this much is reported.
_FLOW_CRITICALITY_DELTA = 0.20
#: At most this many missing public symbols get the (more expensive) text
#: search per `drift()` call, ranked by how many communities lost it.
_MAX_SYMBOLS_TEXT_SEARCHED = 20
#: At most this many files scanned per missing symbol before giving up on it.
_MAX_FILES_PER_SYMBOL_SEARCH = 4000
#: At most this many communities' worth of public-API symbols are tracked
#: per snapshot (already capped smaller per-community, this is the total).
_MAX_PUBLIC_API_PER_COMMUNITY = 40

_SOURCE_EXT = (".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")


# ── shaping one snapshot's worth of data ────────────────────────────────

def _public_api_symbols(rec: Dict[str, Any]) -> List[str]:
    """A community's approximate public surface: its routes, its entry
    points and its top internally-fanned-in symbols -- fields
    `code_graph.communities` already computes, not a new graph walk. Not
    exact (a symbol used only by another community's private helper is
    still "public" in the sense that matters for drift -- something outside
    this community's own files depends on it -- and this cannot see that
    without a second pass over cross-community call edges this package does
    not currently retain per-symbol); documented as an approximation, never
    presented as a precise API surface."""
    names: Set[str] = set()
    for r in rec.get("routes") or ():
        q = str(r.get("route") or "").strip()
        if q:
            names.add(q)
    for e in rec.get("entry_points") or ():
        q = str(e.get("symbol") or "").strip()
        if q:
            names.add(q)
    for k in rec.get("key_symbols") or ():
        q = str(k.get("symbol") or "").strip()
        if q:
            names.add(q)
    return sorted(names)[:_MAX_PUBLIC_API_PER_COMMUNITY]


def _shape_communities(records: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for rec in records:
        files = sorted(rec.get("files") or [])
        out.append({
            "id": rec["id"], "name": rec.get("name", ""), "purpose": rec.get("purpose", ""),
            "files": files,
            "coupling": [
                {"to": c.get("community_id"), "weight": c.get("weight")}
                for c in (rec.get("coupling") or []) if c.get("community_id")
            ],
            "public_api": _public_api_symbols(rec),
        })
    return out


def _shape_flows(records: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "id": r.get("id"), "entry_symbol": r.get("entry_symbol"), "name": r.get("name"),
            "criticality": float(r.get("criticality") or 0.0),
            "criticality_level": r.get("criticality_level", ""),
            "size": int(r.get("size") or 0), "files": sorted(r.get("files") or []),
        }
        for r in records
    ]


def _snapshot_payload(resolved: str, project_id: str) -> Optional[Dict[str, Any]]:
    # min_files=0: `communities()` hides communities under `_DEFAULT_MIN_FILES`
    # files by default (a display-time floor for a human list) -- a drift
    # baseline needs every community, including a single-file one, or a real
    # new/removed community at that size would silently disappear.
    comm_result = _communities(resolved, project_id=project_id, level=0, min_files=0)
    if comm_result.get("exit_code") != 0:
        logger.warning("code_graph.drift: communities() failed: %s", comm_result.get("error"))
        return None
    flow_result = _flows(resolved, project_id=project_id, limit=500)
    if flow_result.get("exit_code") != 0:
        logger.warning("code_graph.drift: flows() failed: %s", flow_result.get("error"))
        return None
    arch = {}
    try:
        arch = get_architecture(resolved, project_id=project_id)
    except Exception as exc:  # noqa: BLE001 - hot files are a nice-to-have, not required
        logger.debug("code_graph.drift: get_architecture failed: %s", exc)

    communities_payload = _shape_communities(comm_result.get("communities") or [])
    file_to_community = {f: c["id"] for c in communities_payload for f in c["files"]}
    total_files = sum(len(c["files"]) for c in communities_payload)
    return {
        "communities": communities_payload,
        "flows": _shape_flows(flow_result.get("flows") or []),
        "file_to_community": file_to_community,
        "hot_files": (arch.get("hotspots") or [])[:12],
        "fingerprint": comm_result.get("fingerprint", ""),
        "total_files": total_files,
    }


# ── persistence ──────────────────────────────────────────────────────────

def _baseline_id() -> str:
    return "bl_" + uuid.uuid4().hex[:16]


def _persist_baseline(root: str, project_id: str, baseline_id: str, label: str,
                       fingerprint: str, data: Dict[str, Any], created_at: str) -> None:
    try:
        with ce_store.db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO code_graph_baselines "
                "(workspace, project_id, id, label, fingerprint, data, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (root, project_id, baseline_id, label, fingerprint, ce_store.dumps(data), created_at),
            )
    except (ce_store.ContextStoreError, sqlite3.Error) as exc:
        logger.warning("code_graph.drift: persist_baseline failed: %s", exc)


def _load_baseline(root: str, project_id: str, baseline_id: str) -> Optional[Dict[str, Any]]:
    try:
        with ce_store.db() as conn:
            row = conn.execute(
                "SELECT * FROM code_graph_baselines WHERE workspace = ? AND project_id = ? AND id = ?",
                (root, project_id, baseline_id),
            ).fetchone()
    except (ce_store.ContextStoreError, sqlite3.Error) as exc:
        logger.warning("code_graph.drift: load_baseline failed: %s", exc)
        return None
    if row is None:
        return None
    data = ce_store.loads_dict(row["data"])
    data["id"] = row["id"]
    data["label"] = row["label"]
    data["created_at"] = row["created_at"]
    data["fingerprint"] = row["fingerprint"]
    return data


def _latest_baseline_id(root: str, project_id: str) -> str:
    try:
        with ce_store.db() as conn:
            row = conn.execute(
                "SELECT id FROM code_graph_baselines WHERE workspace = ? AND project_id = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (root, project_id),
            ).fetchone()
    except (ce_store.ContextStoreError, sqlite3.Error) as exc:
        logger.warning("code_graph.drift: latest_baseline lookup failed: %s", exc)
        return ""
    return str(row["id"]) if row else ""


def list_baselines(root: str = "", *, project_id: str = "", limit: int = 20) -> Dict[str, Any]:
    """Baselines recorded for this workspace, newest first."""
    try:
        resolved = _root(root)
    except ValueError as exc:
        return {"error": str(exc), "exit_code": 1}
    cap = max(1, min(int(limit or 20), 200))
    try:
        with ce_store.db() as conn:
            rows = ce_store.rows(conn.execute(
                "SELECT id, label, fingerprint, created_at FROM code_graph_baselines "
                "WHERE workspace = ? AND project_id = ? ORDER BY created_at DESC LIMIT ?",
                (resolved, project_id, cap),
            ))
    except (ce_store.ContextStoreError, sqlite3.Error) as exc:
        logger.warning("code_graph.drift: list_baselines failed: %s", exc)
        rows = []
    lines = [f"{len(rows)} baseline(s):"] + [
        f"  {r['id']}  {r['created_at']}" + (f"  ({r['label']})" if r.get("label") else "")
        for r in rows
    ]
    return {"output": "\n".join(lines), "exit_code": 0, "root": resolved, "baselines": rows}


# ── snapshot ─────────────────────────────────────────────────────────────

def snapshot(root: str = "", *, project_id: str = "", label: str = "") -> Dict[str, Any]:
    """Record an architecture baseline of this workspace: level-0
    communities (files, purpose, coupling, approximate public API),
    execution flows and current hotspots -- everything `drift` later
    compares against. Returns the new baseline's id."""
    try:
        resolved = _root(root)
    except ValueError as exc:
        return {"error": str(exc), "exit_code": 1}
    try:
        code_index.refresh(resolved, project_id=project_id, budget_files=_INDEX_BUDGET_FILES)
    except Exception as exc:  # noqa: BLE001
        logger.warning("code_graph.drift: index refresh failed: %s", exc)

    payload = _snapshot_payload(resolved, project_id)
    if payload is None:
        return {"error": "could not compute the current graph", "exit_code": 1, "root": resolved}

    baseline_id = _baseline_id()
    created_at = ce_store.now_iso()
    _persist_baseline(resolved, project_id, baseline_id, str(label or ""),
                      payload["fingerprint"], payload, created_at)
    n_comm, n_flows = len(payload["communities"]), len(payload["flows"])
    return {
        "output": (
            f"Baseline {baseline_id} recorded ({created_at}): {n_comm} "
            f"communit{'y' if n_comm == 1 else 'ies'}, {n_flows} flow(s), "
            f"{payload['total_files']} file(s)."
        ),
        "exit_code": 0, "root": resolved, "baseline_id": baseline_id, "created_at": created_at,
        "communities": n_comm, "flows": n_flows, "files": payload["total_files"],
    }


# ── matching across snapshots ───────────────────────────────────────────

def _match_communities(base: Sequence[Dict[str, Any]], cur: Sequence[Dict[str, Any]]
                        ) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Best-overlap (Jaccard) match, baseline id -> current id and back.
    O(n*m) over community counts, which stay small (tens, not thousands) for
    any repo this package's own budgets would let through."""
    base_to_cur: Dict[str, str] = {}
    cur_to_base: Dict[str, str] = {}
    cur_file_sets = {c["id"]: set(c["files"]) for c in cur}
    for b in base:
        b_files = set(b["files"])
        if not b_files:
            continue
        best_id, best_score = None, 0.0
        for cid, c_files in cur_file_sets.items():
            if not c_files:
                continue
            inter = len(b_files & c_files)
            if not inter:
                continue
            score = inter / len(b_files | c_files)
            if score > best_score:
                best_id, best_score = cid, score
        if best_id is not None and best_score >= _JACCARD_MATCH_THRESHOLD:
            base_to_cur[b["id"]] = best_id
            cur_to_base.setdefault(best_id, b["id"])
    return base_to_cur, cur_to_base


def _has_cycle(edges: Dict[str, Set[str]]) -> bool:
    WHITE, GRAY, BLACK = 0, 1, 2
    color: Dict[str, int] = {n: WHITE for n in edges}
    for target_set in edges.values():
        for t in target_set:
            color.setdefault(t, WHITE)

    def visit(node: str, stack: Set[str]) -> bool:
        color[node] = GRAY
        for nxt in edges.get(node, ()):
            if color.get(nxt, WHITE) == GRAY:
                return True
            if color.get(nxt, WHITE) == WHITE and visit(nxt, stack):
                return True
        color[node] = BLACK
        return False

    for node in list(color):
        if color[node] == WHITE:
            if visit(node, set()):
                return True
    return False


def _coupling_edges(comms: Sequence[Dict[str, Any]]) -> Dict[str, Set[str]]:
    return {c["id"]: {e["to"] for e in c.get("coupling") or [] if e.get("to")} for c in comms}


_WORD_RE_CACHE: Dict[str, "re.Pattern[str]"] = {}


def _bare_name(qualname: str) -> str:
    return str(qualname or "").rsplit(".", 1)[-1].strip()


def _text_references(root: str, name: str, *, deadline: float,
                      max_files: int = _MAX_FILES_PER_SYMBOL_SEARCH) -> Tuple[List[str], bool]:
    """Bounded, best-effort text search for `name` (word-boundary) across
    the workspace's source files. Returns (matching file paths, capped),
    where `capped` is True when the time/file budget ran out before every
    file was checked -- the caller must not read an empty result as "not
    referenced anywhere" when this is True."""
    if not name:
        return [], False
    pattern = _WORD_RE_CACHE.get(name)
    if pattern is None:
        pattern = re.compile(r"(?<![\w.])" + re.escape(name) + r"(?![\w])")
        if len(_WORD_RE_CACHE) < 4096:
            _WORD_RE_CACHE[name] = pattern
    hits: List[str] = []
    checked = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (
            ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build")]
        for fname in filenames:
            if not fname.endswith(_SOURCE_EXT):
                continue
            if time.monotonic() >= deadline or checked >= max_files:
                return hits, True
            checked += 1
            path = os.path.join(dirpath, fname)
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as handle:
                    text = handle.read()
            except OSError:
                continue
            if pattern.search(text):
                hits.append(os.path.relpath(path, root))
                if len(hits) >= 5:
                    return hits, False
    return hits, False


# ── scoring ──────────────────────────────────────────────────────────────

_WEIGHTS: Dict[str, int] = {
    "new_community": 3,
    "removed_community": 8,
    "file_moved_community": 1,
    "new_cross_community_edge": 2,
    "new_edge_into_isolated_community": 10,
    "cycle_introduced": 15,
    "flow_removed": 6,
    "new_flow": 2,
    "flow_size_changed": 4,
    "flow_criticality_changed": 3,
    "removed_public_symbol_still_referenced": 12,
}
#: Per-category point cap, so one noisy category (forty files moved in an
#: intentional reorg) cannot alone saturate the score.
_CATEGORY_CAP: Dict[str, int] = {
    "new_community": 12, "removed_community": 24, "file_moved_community": 15,
    "new_cross_community_edge": 10, "new_edge_into_isolated_community": 30,
    "cycle_introduced": 15, "flow_removed": 24, "new_flow": 8,
    "flow_size_changed": 16, "flow_criticality_changed": 12,
    "removed_public_symbol_still_referenced": 36,
}


def drift(root: str = "", *, project_id: str = "", baseline_id: str = "",
          refresh: bool = False, time_budget_s: float = DEFAULT_TIME_BUDGET_S,
          output_chars: int = DEFAULT_OUTPUT_CHARS) -> Dict[str, Any]:
    """Compare the current code graph against a baseline (the most recent
    one for this workspace, or `baseline_id`). See the module docstring for
    what is compared and how communities/flows are matched across the two
    snapshots. Time-boxed (`time_budget_s`): a comparison that runs past it
    stops the (only) expensive part -- the removed-public-symbol text search
    -- and marks the result `"partial": True` rather than hanging."""
    try:
        resolved = _root(root)
    except ValueError as exc:
        return {"error": str(exc), "exit_code": 1}
    started = time.monotonic()
    deadline = started + max(1.0, float(time_budget_s or DEFAULT_TIME_BUDGET_S))

    bl_id = str(baseline_id or "").strip() or _latest_baseline_id(resolved, project_id)
    if not bl_id:
        return {
            "error": "no baseline recorded for this workspace -- run code_graph_drift "
                     "with action=snapshot first",
            "exit_code": 1, "root": resolved,
        }
    baseline = _load_baseline(resolved, project_id, bl_id)
    if baseline is None:
        return {"error": f"baseline not found: {bl_id}", "exit_code": 1, "root": resolved}

    try:
        code_index.refresh(resolved, project_id=project_id, budget_files=_INDEX_BUDGET_FILES)
    except Exception as exc:  # noqa: BLE001
        logger.warning("code_graph.drift: index refresh failed: %s", exc)
    if refresh:
        try:
            _communities(resolved, project_id=project_id, level=0, refresh=True)
            _flows(resolved, project_id=project_id, refresh=True)
        except Exception as exc:  # noqa: BLE001
            logger.debug("code_graph.drift: forced rebuild failed: %s", exc)

    current = _snapshot_payload(resolved, project_id)
    if current is None:
        return {"error": "could not compute the current graph", "exit_code": 1, "root": resolved}

    base_comms = baseline.get("communities") or []
    cur_comms = current["communities"]
    base_flows = baseline.get("flows") or []
    cur_flows = current["flows"]

    findings: List[Dict[str, Any]] = []
    partial = False

    base_to_cur, cur_to_base = _match_communities(base_comms, cur_comms)
    base_by_id = {c["id"]: c for c in base_comms}
    cur_by_id = {c["id"]: c for c in cur_comms}

    for bid, b in base_by_id.items():
        if bid not in base_to_cur:
            findings.append({
                "type": "removed_community", "severity": _WEIGHTS["removed_community"],
                "community": b["name"], "files": len(b["files"]),
                "explanation": f"community {b['name']!r} ({len(b['files'])} files) no longer matches "
                                "any current community by file overlap.",
            })
    for cid, c in cur_by_id.items():
        if cid not in cur_to_base:
            findings.append({
                "type": "new_community", "severity": _WEIGHTS["new_community"],
                "community": c["name"], "files": len(c["files"]),
                "explanation": f"community {c['name']!r} ({len(c['files'])} files) has no match in "
                                "the baseline.",
            })

    # Files that moved from their matched community into a different one.
    base_file_to_comm = baseline.get("file_to_community") or {}
    cur_file_to_comm = current["file_to_community"]
    moved: List[Tuple[str, str, str]] = []
    for f, bid in base_file_to_comm.items():
        cid_actual = cur_file_to_comm.get(f)
        if cid_actual is None:
            continue  # file no longer exists -- not a move
        cid_expected = base_to_cur.get(bid)
        if cid_expected is not None and cid_actual != cid_expected:
            moved.append((f, base_by_id.get(bid, {}).get("name", bid),
                         cur_by_id.get(cid_actual, {}).get("name", cid_actual)))
    if moved:
        shown = moved[:10]
        findings.append({
            "type": "file_moved_community", "severity": min(
                _CATEGORY_CAP["file_moved_community"],
                _WEIGHTS["file_moved_community"] * len(moved)),
            "count": len(moved), "examples": [
                {"file": f, "from": frm, "to": to} for f, frm, to in shown
            ],
            "explanation": f"{len(moved)} file(s) moved to a different community, e.g. "
                            + "; ".join(f"{f} ({frm} -> {to})" for f, frm, to in shown[:3]),
        })

    # New coupling between communities (mapped into current id-space).
    isolated_before = {
        base_to_cur[bid] for bid, b in base_by_id.items()
        if bid in base_to_cur and not (b.get("coupling") or [])
    }
    for cid, c in cur_by_id.items():
        bid = cur_to_base.get(cid)
        cur_targets = {e["to"] for e in c.get("coupling") or [] if e.get("to")}
        if bid is None:
            continue  # a brand-new community's edges are covered by "new_community"
        base_target_ids = {
            base_to_cur.get(e["to"]) for e in (base_by_id.get(bid, {}).get("coupling") or [])
            if e.get("to") and base_to_cur.get(e["to"])
        }
        new_targets = cur_targets - base_target_ids
        if not new_targets:
            continue
        if cid in isolated_before:
            findings.append({
                "type": "new_edge_into_isolated_community",
                "severity": min(_CATEGORY_CAP["new_edge_into_isolated_community"],
                                _WEIGHTS["new_edge_into_isolated_community"] * len(new_targets)),
                "community": c["name"], "new_dependencies": len(new_targets),
                "explanation": f"community {c['name']!r} had no cross-community coupling in the "
                                f"baseline and now depends on {len(new_targets)} other "
                                "community/ies.",
            })
        else:
            findings.append({
                "type": "new_cross_community_edge",
                "severity": min(_CATEGORY_CAP["new_cross_community_edge"],
                                _WEIGHTS["new_cross_community_edge"] * len(new_targets)),
                "community": c["name"], "new_dependencies": len(new_targets),
                "explanation": f"community {c['name']!r} gained {len(new_targets)} new "
                                "cross-community dependency/ies.",
            })

    # Cycle among communities, present now but not in the baseline.
    try:
        if _has_cycle(_coupling_edges(cur_comms)) and not _has_cycle(_coupling_edges(base_comms)):
            findings.append({
                "type": "cycle_introduced", "severity": _WEIGHTS["cycle_introduced"],
                "explanation": "a dependency cycle exists among communities now that did not exist "
                                "in the baseline.",
            })
    except RecursionError:
        logger.debug("code_graph.drift: cycle check skipped (recursion depth)")
        partial = True

    # Flows: matched by entry_symbol (stable), not by id (content-derived).
    base_flow_by_entry = {f["entry_symbol"]: f for f in base_flows if f.get("entry_symbol")}
    cur_flow_by_entry = {f["entry_symbol"]: f for f in cur_flows if f.get("entry_symbol")}
    for entry, bf in base_flow_by_entry.items():
        cf = cur_flow_by_entry.get(entry)
        if cf is None:
            findings.append({
                "type": "flow_removed", "severity": _WEIGHTS["flow_removed"],
                "entry_symbol": entry,
                "explanation": f"the flow from {entry} no longer appears (removed, or the entry "
                                "point no longer qualifies as one).",
            })
            continue
        size_delta = cf["size"] - bf["size"]
        if abs(size_delta) >= _FLOW_SIZE_DELTA:
            findings.append({
                "type": "flow_size_changed", "severity": _WEIGHTS["flow_size_changed"],
                "entry_symbol": entry, "from_size": bf["size"], "to_size": cf["size"],
                "explanation": f"the flow from {entry} changed from {bf['size']} to {cf['size']} "
                                "step(s).",
            })
        crit_delta = cf["criticality"] - bf["criticality"]
        if abs(crit_delta) >= _FLOW_CRITICALITY_DELTA:
            findings.append({
                "type": "flow_criticality_changed", "severity": _WEIGHTS["flow_criticality_changed"],
                "entry_symbol": entry, "from": bf["criticality"], "to": cf["criticality"],
                "explanation": f"the flow from {entry} criticality moved from "
                                f"{bf['criticality']:.2f} to {cf['criticality']:.2f}.",
            })
    new_flows = [e for e in cur_flow_by_entry if e not in base_flow_by_entry]
    if new_flows:
        shown = sorted(new_flows)[:5]
        findings.append({
            "type": "new_flow", "severity": min(_CATEGORY_CAP["new_flow"],
                                                _WEIGHTS["new_flow"] * len(new_flows)),
            "count": len(new_flows), "examples": shown,
            "explanation": f"{len(new_flows)} new flow(s), e.g. " + ", ".join(shown),
        })

    # Removed public API symbols, checked for continued textual reference.
    cur_public = {q for c in cur_comms for q in c.get("public_api") or []}
    missing: List[Tuple[str, str]] = []  # (qualname, community_name)
    for b in base_comms:
        for q in b.get("public_api") or []:
            if q not in cur_public:
                missing.append((q, b["name"]))
    if missing:
        checked = 0
        for qualname, comm_name in missing:
            if checked >= _MAX_SYMBOLS_TEXT_SEARCHED or time.monotonic() >= deadline:
                partial = True
                break
            checked += 1
            hits, capped = _text_references(resolved, _bare_name(qualname), deadline=deadline)
            partial = partial or capped
            if hits:
                findings.append({
                    "type": "removed_public_symbol_still_referenced",
                    "severity": _WEIGHTS["removed_public_symbol_still_referenced"],
                    "symbol": qualname, "community": comm_name, "referenced_in": hits,
                    "explanation": f"{qualname} (public in {comm_name!r}) no longer resolves in the "
                                    f"graph but its name still appears in {len(hits)} file(s), e.g. "
                                    f"{hits[0]}.",
                })

    findings.sort(key=lambda f: -int(f.get("severity") or 0))
    score = 0
    per_category: Dict[str, int] = {}
    for f in findings:
        cat = f["type"]
        cap = _CATEGORY_CAP.get(cat, f.get("severity") or 0)
        per_category[cat] = min(cap, per_category.get(cat, 0) + int(f.get("severity") or 0))
    score = min(100, sum(per_category.values()))

    top = findings[:8]
    lines = [f"Drift vs baseline {bl_id}: score {score}/100 ({len(findings)} finding(s))."]
    lines += [f"  [{f['severity']}] {f['explanation']}" for f in top]
    if partial:
        lines.append("  (partial: the comparison ran past its time budget)")

    return {
        "output": _clip("\n".join(lines), output_chars), "exit_code": 0, "root": resolved,
        "baseline_id": bl_id, "score": score, "findings": findings, "top_findings": top,
        "partial": partial, "elapsed_ms": int((time.monotonic() - started) * 1000),
    }


__all__ = ["snapshot", "drift", "list_baselines"]
