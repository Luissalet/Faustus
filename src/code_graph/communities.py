"""src/code_graph/communities.py — "what parts is this repo made of?"

`context_engine.code_index` already resolves symbols, calls, imports and
routes; this module answers the one architecture question it does not: group
those symbols into the modules a person would actually name ("the auth
service", "the code graph", "the test suite for X") without reading a single
file.

Design decisions, and why:

* **Nodes are files, not symbols.**  A workspace this index targets has on
  the order of 10-30k symbols but a few thousand files, and clustering at
  file granularity is both cheaper (Louvain in pure Python has to stay a
  few-second operation, not a few-minute one) and more stable: two tiny
  helper functions in the same file that happen to call unrelated things
  should not be pulled into different communities just because a leaf-level
  modularity score says so, and a file is already the unit a human means by
  "part of the codebase". `code_files.symbols` (already stored per file by
  `code_index.refresh`) gives each file node's `size` for free, so nothing
  extra is computed to get it.
* **Edges are weighted by kind and certainty, aggregated per file pair.**
  `calls` edges are the strongest signal of "these two files work together"
  (weight 3.0), `registers` next (a route/tool wiring itself in, 2.0),
  `tests` next (1.5), `imports` weakest (1.0, a dependency without a
  behavioural link). Every edge is additionally scaled by how sure the
  extractor was (`exact` 1.0, `static_inferred` 0.7, `lexical` 0.4) — a
  guessed call should not out-vote an AST-resolved one when they disagree
  about which community a file belongs to.
* **Test files never pull production files into their own cluster.** A test
  file typically imports/exercises a dozen unrelated modules across the
  whole repository (fixtures, the modules under test); if that edge counted
  for clustering, one test file would out-weigh every real coupling signal
  and Louvain would either merge the whole repo into one blob or produce a
  meaningless partition dominated by whichever test file has the most
  imports. So an edge whose two endpoints disagree on "is this a test path"
  (`query._is_test_path`) is dropped **for clustering only** — a test file
  keeps its own node and clusters with other tests it is genuinely coupled
  to (shared fixtures, `conftest.py`), and the coverage a community's tests
  provide is reported separately (`test_files`, computed from the *full*,
  unfiltered edge set) rather than folded into the partition itself.
* **Deterministic two-level Louvain, no dependency.** `_one_level` is the
  textbook Louvain local-moving phase (Blondel et al. 2008): nodes visited
  in a fixed sorted order, each considers moving into the community of a
  neighbour that maximises modularity gain, repeated until no node moves.
  Ties are broken by the lexicographically smallest candidate id and a node
  only moves on a strictly positive gain, so the same graph always produces
  the same partition. Level 0 is the direct output of that phase over the
  file graph; level 1 re-runs the exact same phase over the level-0
  communities aggregated into super-nodes (their mutual edge weight summed,
  self-loops representing internal cohesion) — literally "communities of
  communities", which is what real multi-level Louvain calls its second
  pass. Anything past level 1 is out of scope here (the contract asks for
  0/1 only).
* **A time/size budget, with an honest fallback.** A local-moving pass is
  O(nodes x average degree) per sweep and a pathological graph (a huge
  monorepo, or an unusually dense one) could still take too long in pure
  Python. `_TIME_BUDGET_S` bounds the wall clock spent inside Louvain; if it
  is exceeded, or the file-node count is absurd (`_NODE_CAP`), the whole
  computation is abandoned in favour of directory-based grouping (two path
  segments = level 0, one = level 1) so the tool never hangs a turn — the
  result says `"method": "directory"` so a caller can tell the difference
  from a real clustering.

Persisted in `ce_store` (own tables, `code_communities` / `code_community_files`),
keyed by `(workspace, project_id, fingerprint)` where `fingerprint` is a hash
of the index's own `(last_indexed_at, symbol_count, edge_count)` — cheap to
recompute on every call, and a changed index (different fingerprint) discards
the old rows for that workspace and rebuilds, exactly like `code_index.refresh`
being incremental does not require callers to know when to invalidate.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import sqlite3
import time
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from src.context_engine import code_index
from src.context_engine import store as ce_store

from .query import DEFAULT_OUTPUT_CHARS, _clip, _is_test_path, _resolve_symbol, _root

logger = logging.getLogger(__name__)

ce_store.register_schema("code_communities", (
    """
    CREATE TABLE IF NOT EXISTS code_communities (
        workspace         TEXT NOT NULL DEFAULT '',
        project_id        TEXT NOT NULL DEFAULT '',
        fingerprint       TEXT NOT NULL DEFAULT '',
        level             INTEGER NOT NULL DEFAULT 0,
        id                TEXT NOT NULL DEFAULT '',
        parent            TEXT NOT NULL DEFAULT '',
        method            TEXT NOT NULL DEFAULT 'louvain',
        name              TEXT NOT NULL DEFAULT '',
        purpose           TEXT NOT NULL DEFAULT '',
        summary           TEXT NOT NULL DEFAULT '',
        size              INTEGER NOT NULL DEFAULT 0,
        dominant_language TEXT NOT NULL DEFAULT '',
        cohesion          REAL NOT NULL DEFAULT 0.0,
        data              TEXT NOT NULL DEFAULT '{}',
        computed_at       TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (workspace, project_id, fingerprint, level, id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_code_communities_scope "
    "ON code_communities(workspace, project_id, fingerprint, level)",
    """
    CREATE TABLE IF NOT EXISTS code_community_files (
        workspace     TEXT NOT NULL,
        project_id    TEXT NOT NULL DEFAULT '',
        fingerprint   TEXT NOT NULL DEFAULT '',
        path          TEXT NOT NULL,
        community_id  TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (workspace, project_id, fingerprint, path)
    )
    """,
))

#: Wide enough that a real repo's whole file set is seen (mirrors `impact`'s
#: `_IMPACT_INDEX_BUDGET`) -- a partial clustering would be a partial lie.
_INDEX_BUDGET_FILES = 20000
#: Wall-clock ceiling for the Louvain passes; past this the computation is
#: abandoned for the directory fallback rather than hanging a turn.
_TIME_BUDGET_S = 15.0
#: A file-node graph past this size is not attempted at all.
_NODE_CAP = 20000
_MAX_LOCAL_PASSES = 50

_KIND_WEIGHT: Dict[str, float] = {"calls": 3.0, "registers": 2.0, "tests": 1.5, "imports": 1.0}
_CERTAINTY_WEIGHT: Dict[str, float] = {"exact": 1.0, "static_inferred": 0.7, "lexical": 0.4}
_EDGE_KINDS_FOR_GRAPH = tuple(_KIND_WEIGHT)

_TOP_KEY_SYMBOLS = 6
_TOP_COUPLING = 5

#: Bare names common enough (container/string builtins, generic verbs) that
#: `context_engine.code_index`'s bare-name call resolution ("exactly one
#: symbol with that name in this workspace" -- see its `_resolve` docstring)
#: can match a call site that meant `list.append`/`str.strip`/etc to the one
#: unrelated user function or method that happens to share the name, in a
#: codebase large enough to define one. That is a resolution-quality
#: limitation of the shared index, not something this module can fix without
#: touching file ownership outside this lot -- but a name this generic is
#: also never a good "what does this community do" headline even on the rare
#: occasion it IS a real call, so it is excluded from `key_symbols`/naming
#: (never from clustering weight or membership, which is unaffected).
_GENERIC_SYMBOL_NAMES = frozenset({
    "append", "extend", "insert", "remove", "pop", "clear", "index", "count",
    "sort", "reverse", "copy", "update", "keys", "values", "items", "get",
    "setdefault", "strip", "lstrip", "rstrip", "split", "rsplit", "join",
    "replace", "format", "encode", "decode", "startswith", "endswith",
    "find", "match", "search", "sub", "read", "write", "close", "open",
    "run", "execute", "call", "add", "put", "delete", "post", "main",
    "init", "__init__", "str", "repr", "len", "max", "min", "sum", "sorted",
    "filter", "map", "cls", "self", "value", "data", "result", "response",
    "request", "round", "any", "all", "next", "iter", "zip", "enumerate",
    "isinstance", "issubclass", "type", "id", "hash", "vars", "dir",
    "callable", "bool", "int", "float", "list", "dict", "set", "tuple",
    "bytes", "bytearray", "property", "staticmethod", "classmethod",
    "super", "exists",
})


class _BudgetExceeded(Exception):
    """Raised out of `_one_level` when the time budget is spent."""


# ── fingerprint / scope ─────────────────────────────────────────────────

def _fingerprint(root: str, project_id: str) -> str:
    status = code_index.status(root, project_id=project_id)
    raw = f"{status.get('last_indexed_at', '')}|{status.get('symbols', 0)}|{status.get('edges', 0)}"
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:16]


def _where(workspace: str, project_id: str, *, extra: str = "") -> Tuple[str, List[Any]]:
    clauses = ["workspace = ?"]
    params: List[Any] = [workspace]
    if project_id:
        clauses.append("project_id = ?")
        params.append(project_id)
    if extra:
        clauses.append(extra)
    return " AND ".join(clauses), params


# ── loading the raw graph ───────────────────────────────────────────────

class _Inputs:
    __slots__ = ("paths", "path_lang", "path_symbols", "sym_path", "sym_kind",
                 "sym_qual", "sym_name", "sym_line")

    def __init__(self) -> None:
        self.paths: List[str] = []
        self.path_lang: Dict[str, str] = {}
        self.path_symbols: Dict[str, int] = {}
        self.sym_path: Dict[str, str] = {}
        self.sym_kind: Dict[str, str] = {}
        self.sym_qual: Dict[str, str] = {}
        self.sym_name: Dict[str, str] = {}
        self.sym_line: Dict[str, int] = {}


def _load_inputs(conn: sqlite3.Connection, workspace: str, project_id: str) -> _Inputs:
    out = _Inputs()
    where, params = _where(workspace, project_id)
    for row in conn.execute(
            f"SELECT path, language, symbols FROM code_files WHERE {where}", params):
        path = str(row["path"])
        out.paths.append(path)
        out.path_lang[path] = str(row["language"] or "")
        out.path_symbols[path] = int(row["symbols"] or 0)
    out.paths.sort()
    for row in conn.execute(
            f"SELECT id, path, kind, qualname, name, start_line FROM code_symbols "
            f"WHERE {where}", params):
        sid = str(row["id"])
        out.sym_path[sid] = str(row["path"])
        out.sym_kind[sid] = str(row["kind"])
        out.sym_qual[sid] = str(row["qualname"])
        out.sym_name[sid] = str(row["name"])
        out.sym_line[sid] = int(row["start_line"] or 0)
    return out


def _load_call_edges(conn: sqlite3.Connection, workspace: str, project_id: str
                      ) -> List[Tuple[str, str]]:
    """`(src_id, dst_id)` for every resolved `calls` edge -- used for
    key-symbol fan-in, kept separate from the weighted clustering graph
    because fan-in should count every call regardless of certainty."""
    where, params = _where(workspace, project_id, extra="kind = 'calls'")
    return [(str(r["src"]), str(r["dst"]))
            for r in conn.execute(f"SELECT src, dst FROM code_edges WHERE {where}", params)]


def _load_test_edges(conn: sqlite3.Connection, workspace: str, project_id: str
                      ) -> List[Tuple[str, str]]:
    """`(src_id, dst_id)` for every edge a test file makes into anything --
    unfiltered, used only to report which tests exercise a community, never
    for clustering."""
    where, params = _where(workspace, project_id,
                           extra="kind IN ('calls', 'imports', 'tests')")
    return [(str(r["src"]), str(r["dst"]))
            for r in conn.execute(f"SELECT src, dst FROM code_edges WHERE {where}", params)]


def _build_file_graph(conn: sqlite3.Connection, workspace: str, project_id: str,
                       inputs: _Inputs) -> Dict[Tuple[str, str], float]:
    """One weighted, undirected entry per unordered file pair with at least
    one qualifying edge between them -- see the module docstring for the
    kind/certainty weights and the test/production exclusion rule."""
    marks = ",".join("?" * len(_EDGE_KINDS_FOR_GRAPH))
    clauses = ["workspace = ?"]
    params = [workspace]
    if project_id:
        clauses.append("project_id = ?")
        params.append(project_id)
    clauses.append(f"kind IN ({marks})")
    params.extend(_EDGE_KINDS_FOR_GRAPH)
    sql = f"SELECT src, dst, kind, certainty FROM code_edges WHERE {' AND '.join(clauses)}"

    pair_weight: Dict[Tuple[str, str], float] = {}
    for row in conn.execute(sql, params):
        src_path = inputs.sym_path.get(str(row["src"]))
        dst_path = inputs.sym_path.get(str(row["dst"]))
        if not src_path or not dst_path or src_path == dst_path:
            continue
        if _is_test_path(src_path) != _is_test_path(dst_path):
            continue  # cross test/production edge: never used for clustering
        weight = _KIND_WEIGHT.get(str(row["kind"]), 0.0) * \
            _CERTAINTY_WEIGHT.get(str(row["certainty"]), 0.3)
        if weight <= 0:
            continue
        key = (src_path, dst_path) if src_path < dst_path else (dst_path, src_path)
        pair_weight[key] = pair_weight.get(key, 0.0) + weight
    return pair_weight


# ── deterministic two-level Louvain ─────────────────────────────────────

def _one_level(adj: Dict[str, Dict[str, float]], order: Sequence[str],
                deadline: float) -> Dict[str, str]:
    """One Louvain local-moving phase: fixed node order, ties broken by the
    smallest candidate id, a move only on a strictly positive gain -- the
    same graph and order always produce the same partition."""
    comm: Dict[str, str] = {n: n for n in order}
    if not order:
        return comm
    k = {n: sum(adj.get(n, {}).values()) for n in order}
    m2 = sum(k.values())
    if m2 <= 0:
        return comm
    sigma_tot = dict(k)
    changed = True
    passes = 0
    while changed and passes < _MAX_LOCAL_PASSES:
        if time.monotonic() > deadline:
            raise _BudgetExceeded()
        changed = False
        passes += 1
        for n in order:
            cur = comm[n]
            kn = k[n]
            sigma_tot[cur] = sigma_tot.get(cur, 0.0) - kn
            neigh_w: Dict[str, float] = {}
            for nb, w in adj.get(n, {}).items():
                if nb == n:
                    continue
                c = comm[nb]
                neigh_w[c] = neigh_w.get(c, 0.0) + w
            best_c = cur
            best_gain = neigh_w.get(cur, 0.0) - sigma_tot.get(cur, 0.0) * kn / m2
            for c in sorted(neigh_w):
                if c == cur:
                    continue
                gain = neigh_w[c] - sigma_tot.get(c, 0.0) * kn / m2
                if gain > best_gain + 1e-12:
                    best_gain = gain
                    best_c = c
            sigma_tot[best_c] = sigma_tot.get(best_c, 0.0) + kn
            if best_c != cur:
                changed = True
            comm[n] = best_c
    return comm


def _aggregate(adj: Dict[str, Dict[str, float]], membership: Dict[str, str]
               ) -> Dict[str, Dict[str, float]]:
    """Super-node graph: communities become nodes, mutual weight summed.
    Iterating every original (n, neighbour) pair from both sides naturally
    doubles an internal pair's contribution into the community's own
    self-loop -- exactly the convention multi-level Louvain relies on: a
    self-loop weight already represents twice the enclosed edge weight, and
    contributes to the super-node's degree with no further adjustment."""
    agg: Dict[str, Dict[str, float]] = {c: {} for c in set(membership.values())}
    for n, neighbours in adj.items():
        cn = membership[n]
        for nb, w in neighbours.items():
            cnb = membership[nb]
            agg[cn][cnb] = agg[cn].get(cnb, 0.0) + w
    return agg


def _group_by(membership: Dict[str, str]) -> Dict[str, List[str]]:
    groups: Dict[str, List[str]] = {}
    for node, label in membership.items():
        groups.setdefault(label, []).append(node)
    for members in groups.values():
        members.sort()
    return groups


def _community_id(files: Sequence[str]) -> str:
    blob = "\x00".join(sorted(files)).encode("utf-8", "replace")
    return "comm_" + hashlib.sha256(blob).hexdigest()[:16]


def _louvain_partition(pair_weight: Dict[Tuple[str, str], float], all_files: Sequence[str],
                        *, deadline: float
                        ) -> Tuple[Dict[str, str], Dict[str, str], str]:
    """`(file -> community_id0, community_id0 -> community_id1, method)`.

    Falls back to directory-based grouping (`method="directory"`) when the
    file-node count is past `_NODE_CAP` or the Louvain passes exceed the
    time budget -- never a partial/mixed result, always one method or the
    other, so a caller never has to reconcile two clustering styles."""
    if len(all_files) > _NODE_CAP:
        return _directory_fallback(all_files)

    adj: Dict[str, Dict[str, float]] = {p: {} for p in all_files}
    for (a, b), w in pair_weight.items():
        adj[a][b] = adj[a].get(b, 0.0) + w
        adj[b][a] = adj[b].get(a, 0.0) + w

    order = sorted(all_files)
    try:
        level0_raw = _one_level(adj, order, deadline)
        agg = _aggregate(adj, level0_raw)
        comm_order = sorted(agg)
        level1_raw = _one_level(agg, comm_order, deadline)
    except _BudgetExceeded:
        logger.info("code_graph.communities: Louvain exceeded its time budget "
                    "(%d files) -- falling back to directory grouping", len(all_files))
        return _directory_fallback(all_files)

    groups0 = _group_by(level0_raw)
    label_to_id0 = {label: _community_id(members) for label, members in groups0.items()}
    id0_to_label0 = {v: k for k, v in label_to_id0.items()}
    file_to_id0 = {f: label_to_id0[level0_raw[f]] for f in all_files}

    groups1_labels: Dict[str, List[str]] = {}  # raw level1 label -> [id0, ...]
    for label0, id0 in label_to_id0.items():
        label1 = level1_raw.get(label0, label0)
        groups1_labels.setdefault(label1, []).append(id0)
    id0_to_id1: Dict[str, str] = {}
    for label1, id0_list in sorted(groups1_labels.items()):
        files1 = sorted({f for id0 in id0_list for f in groups0[id0_to_label0[id0]]})
        id1 = _community_id(files1)
        for id0 in id0_list:
            id0_to_id1[id0] = id1

    return file_to_id0, id0_to_id1, "louvain"


def _directory_fallback(all_files: Sequence[str], *, level0_depth: int = 2,
                        level1_depth: int = 1) -> Tuple[Dict[str, str], Dict[str, str], str]:
    def _key(path: str, depth: int) -> str:
        parts = path.split("/")[:-1][:depth]
        return "/".join(parts) or "."

    file_to_id0: Dict[str, str] = {}
    groups0: Dict[str, List[str]] = {}
    for f in all_files:
        key = _key(f, level0_depth)
        groups0.setdefault(key, []).append(f)
    label_to_id0 = {key: _community_id(members) for key, members in groups0.items()}
    for key, members in groups0.items():
        for f in members:
            file_to_id0[f] = label_to_id0[key]

    groups1: Dict[str, List[str]] = {}
    id0_to_key1: Dict[str, str] = {}
    for key0, members in groups0.items():
        key1 = _key(members[0], level1_depth) if level1_depth < level0_depth else key0
        groups1.setdefault(key1, []).extend(members)
        id0_to_key1[label_to_id0[key0]] = key1
    label_to_id1 = {key1: _community_id(members) for key1, members in groups1.items()}
    id0_to_id1 = {id0: label_to_id1[key1] for id0, key1 in id0_to_key1.items()}
    return file_to_id0, id0_to_id1, "directory"


# ── per-community detail (shared by both levels) ────────────────────────

_ENTRY_ROUTE_KINDS = ("route",)
_ENTRY_TOOL_KINDS = ("tool",)


def _is_entry_symbol(kind: str, qualname: str, name: str) -> bool:
    if kind in _ENTRY_ROUTE_KINDS or kind in _ENTRY_TOOL_KINDS:
        return True
    if kind == "function" and name == "main":
        return True
    if kind == "method" and name == "execute" and "." in qualname:
        owner = qualname.rsplit(".", 1)[0]
        if owner.endswith("Tool"):
            return True
    return False


def _dominant_language(files: Sequence[str], path_lang: Dict[str, str]) -> str:
    counts: Dict[str, int] = {}
    for f in files:
        lang = path_lang.get(f, "")
        if lang:
            counts[lang] = counts.get(lang, 0) + 1
    if not counts:
        return ""
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def _common_dir_prefix(files: Sequence[str]) -> str:
    dirs = [f.rsplit("/", 1)[0].split("/") for f in files if "/" in f]
    if not dirs:
        return ""
    shortest = min(len(d) for d in dirs)
    prefix: List[str] = []
    for i in range(shortest):
        seg = dirs[0][i]
        if all(d[i] == seg for d in dirs):
            prefix.append(seg)
        else:
            break
    return "/".join(prefix)


def _name_for(files: Sequence[str], key_symbols: Sequence[Dict[str, Any]]) -> str:
    prefix = _common_dir_prefix(files)
    top = key_symbols[0]["symbol"].rsplit(".", 1)[-1] if key_symbols else ""
    if prefix and top:
        return f"{prefix} — {top}"
    if prefix:
        return prefix
    base = files[0].rsplit("/", 1)[-1] if files else "community"
    return f"{base} — {top}" if top else base


def _purpose_for(files: Sequence[str], routes: Sequence[Dict[str, Any]],
                  entry_points: Sequence[Dict[str, Any]],
                  key_symbols: Sequence[Dict[str, Any]], languages: Sequence[str]) -> str:
    bits: List[str] = [
        f"{len(files)} file(s)" + (f" ({', '.join(sorted(set(languages))[:3])})"
                                    if languages else "") + "."
    ]
    if key_symbols:
        names = ", ".join(s["symbol"].rsplit(".", 1)[-1] for s in key_symbols[:4])
        bits.append(f"Most-used internally: {names}.")
    if routes:
        rs = ", ".join(r["route"] for r in routes[:3])
        bits.append(f"Routes: {rs}.")
    if entry_points and not routes:
        eps = ", ".join(e["symbol"] for e in entry_points[:3])
        bits.append(f"Entry points: {eps}.")
    return " ".join(bits)


def _build_records(level: int, groups: Dict[str, List[str]], parents: Dict[str, str],
                    inputs: _Inputs, pair_weight: Dict[Tuple[str, str], float],
                    calls_edges: Sequence[Tuple[str, str]],
                    test_targets: Dict[str, Set[str]], method: str,
                    computed_at: str) -> List[Dict[str, Any]]:
    """Every field the contract asks for, for one level's groups.

    `groups`: community id -> sorted member files. `parents`: id -> parent id
    (empty for level 1). `test_targets`: file -> set of test files that call
    or import into it, from the *unfiltered* edge set (never the clustering
    graph)."""
    files_of_community: Dict[str, str] = {f: cid for cid, files in groups.items() for f in files}

    # internal vs cross weight, one pass over the clustering graph.
    internal: Dict[str, float] = {}
    cross: Dict[str, Dict[str, float]] = {}
    for (a, b), w in pair_weight.items():
        ca, cb = files_of_community.get(a), files_of_community.get(b)
        if ca is None or cb is None:
            continue
        if ca == cb:
            internal[ca] = internal.get(ca, 0.0) + w
        else:
            cross.setdefault(ca, {})[cb] = cross.setdefault(ca, {}).get(cb, 0.0) + w
            cross.setdefault(cb, {})[ca] = cross.setdefault(cb, {}).get(ca, 0.0) + w

    # key symbols: internal call fan-in, one pass over resolved `calls` edges.
    fanin: Dict[str, int] = {}
    for src, dst in calls_edges:
        src_path, dst_path = inputs.sym_path.get(src), inputs.sym_path.get(dst)
        if not src_path or not dst_path:
            continue
        if files_of_community.get(src_path) == files_of_community.get(dst_path) \
                and files_of_community.get(dst_path) is not None:
            fanin[dst] = fanin.get(dst, 0) + 1

    # Symbol ids grouped by file, built ONCE: with thousands of communities
    # (one repo file each, in the common case) a naive per-community scan
    # of the whole symbol table (`sid for sid, p in inputs.sym_path.items()
    # if p in file_set`, repeated for key_symbols/routes/entry_points) is
    # O(communities x total_symbols) -- tens of seconds on a real repo. This
    # index makes every community's own lookup O(its own member count).
    symbols_by_file: Dict[str, List[str]] = {}
    for sid, p in inputs.sym_path.items():
        symbols_by_file.setdefault(p, []).append(sid)

    records: List[Dict[str, Any]] = []
    for cid, files in sorted(groups.items()):
        size = sum(inputs.path_symbols.get(f, 0) for f in files)
        dominant_language = _dominant_language(files, inputs.path_lang)
        member_ids = [sid for f in files for sid in symbols_by_file.get(f, ())]

        candidates = sorted(
            (sid for sid in member_ids if inputs.sym_name.get(sid, "") not in _GENERIC_SYMBOL_NAMES),
            key=lambda sid: (-fanin.get(sid, 0), inputs.sym_path[sid], inputs.sym_line[sid]))
        key_symbols = [
            {"symbol": inputs.sym_qual[sid], "path": inputs.sym_path[sid],
             "line": inputs.sym_line[sid], "fan_in": fanin.get(sid, 0)}
            for sid in candidates if fanin.get(sid, 0) > 0
        ][:_TOP_KEY_SYMBOLS]

        routes = sorted((
            {"route": inputs.sym_qual[sid], "path": inputs.sym_path[sid],
             "line": inputs.sym_line[sid]}
            for sid in member_ids if inputs.sym_kind.get(sid) == "route"
        ), key=lambda r: (r["path"], r["line"]))

        entry_points = sorted((
            {"symbol": inputs.sym_qual[sid], "kind": inputs.sym_kind[sid],
             "path": inputs.sym_path[sid], "line": inputs.sym_line[sid]}
            for sid in member_ids if _is_entry_symbol(
                inputs.sym_kind.get(sid, ""), inputs.sym_qual.get(sid, ""),
                inputs.sym_name.get(sid, ""))
        ), key=lambda e: (e["path"], e["line"]))

        test_files = sorted({t for f in files for t in test_targets.get(f, ())})

        own_internal = internal.get(cid, 0.0)
        own_cross = sum(cross.get(cid, {}).values())
        cohesion = round(own_internal / (own_internal + own_cross), 4) \
            if (own_internal + own_cross) > 0 else 0.0

        coupling = [
            {"community_id": other, "weight": round(w, 4)}
            for other, w in sorted(cross.get(cid, {}).items(), key=lambda kv: (-kv[1], kv[0]))
        ][:_TOP_COUPLING]

        name = _name_for(files, key_symbols)
        purpose = _purpose_for(files, routes, entry_points, key_symbols,
                               [inputs.path_lang.get(f, "") for f in files])

        records.append({
            "id": cid, "level": level, "parent": parents.get(cid, ""),
            "method": method, "name": name, "purpose": purpose, "summary": "",
            "size": size, "files": files, "dominant_language": dominant_language,
            "cohesion": cohesion, "key_symbols": key_symbols, "routes": routes,
            "entry_points": entry_points, "test_files": test_files,
            "coupling": coupling, "computed_at": computed_at,
        })
    return records


# ── persistence ──────────────────────────────────────────────────────────

_RECORD_KEYS = ("files", "key_symbols", "routes", "entry_points", "test_files", "coupling")


def _persist(root: str, project_id: str, fingerprint: str,
             records: List[Dict[str, Any]]) -> None:
    try:
        with ce_store.db() as conn:
            conn.execute(
                "DELETE FROM code_communities WHERE workspace = ? AND project_id = ? "
                "AND fingerprint != ?", (root, project_id, fingerprint))
            conn.execute(
                "DELETE FROM code_community_files WHERE workspace = ? AND project_id = ? "
                "AND fingerprint != ?", (root, project_id, fingerprint))
            rows = []
            file_rows = []
            for rec in records:
                data = {k: rec[k] for k in _RECORD_KEYS}
                rows.append((
                    root, project_id, fingerprint, rec["level"], rec["id"], rec["parent"],
                    rec["method"], rec["name"], rec["purpose"], rec.get("summary", ""),
                    rec["size"], rec["dominant_language"], rec["cohesion"],
                    ce_store.dumps(data), rec["computed_at"],
                ))
                if rec["level"] == 0:
                    for f in rec["files"]:
                        file_rows.append((root, project_id, fingerprint, f, rec["id"]))
            conn.executemany(
                "INSERT OR REPLACE INTO code_communities "
                "(workspace, project_id, fingerprint, level, id, parent, method, name, "
                "purpose, summary, size, dominant_language, cohesion, data, computed_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            if file_rows:
                conn.executemany(
                    "INSERT OR REPLACE INTO code_community_files "
                    "(workspace, project_id, fingerprint, path, community_id) "
                    "VALUES (?,?,?,?,?)", file_rows)
    except (ce_store.ContextStoreError, sqlite3.Error) as exc:
        logger.warning("code_graph.communities: persist failed: %s", exc)


def _row_to_record(row: Dict[str, Any]) -> Dict[str, Any]:
    data = ce_store.loads_dict(row.get("data"))
    rec = {
        "id": row["id"], "level": int(row["level"]), "parent": row["parent"],
        "method": row["method"], "name": row["name"], "purpose": row["purpose"],
        "summary": row.get("summary", ""), "size": int(row["size"]),
        "dominant_language": row["dominant_language"], "cohesion": float(row["cohesion"]),
        "computed_at": row["computed_at"],
    }
    for key in _RECORD_KEYS:
        rec[key] = data.get(key, [] if key != "files" else [])
    return rec


def _load_cached(root: str, project_id: str, fingerprint: str) -> Optional[List[Dict[str, Any]]]:
    try:
        with ce_store.db() as conn:
            where, params = _where(root, project_id, extra="fingerprint = ?")
            params = [*params, fingerprint]
            rows = ce_store.rows(conn.execute(
                f"SELECT * FROM code_communities WHERE {where}", params))
    except (ce_store.ContextStoreError, sqlite3.Error) as exc:
        logger.warning("code_graph.communities: cache read failed: %s", exc)
        return None
    if not rows:
        return None
    return [_row_to_record(r) for r in rows]


# ── build ────────────────────────────────────────────────────────────────

def _build(root: str, project_id: str, fingerprint: str) -> List[Dict[str, Any]]:
    started = time.monotonic()
    with ce_store.db() as conn:
        inputs = _load_inputs(conn, root, project_id)
        pair_weight = _build_file_graph(conn, root, project_id, inputs)
        calls_edges = _load_call_edges(conn, root, project_id)
        test_edges = _load_test_edges(conn, root, project_id)

    test_targets: Dict[str, Set[str]] = {}
    for src, dst in test_edges:
        src_path, dst_path = inputs.sym_path.get(src), inputs.sym_path.get(dst)
        if not src_path or not dst_path or not _is_test_path(src_path) or src_path == dst_path:
            continue
        test_targets.setdefault(dst_path, set()).add(src_path)

    deadline = started + _TIME_BUDGET_S
    file_to_id0, id0_to_id1, method = _louvain_partition(pair_weight, inputs.paths,
                                                          deadline=deadline)

    groups0 = _group_by(file_to_id0)
    computed_at = ce_store.now_iso()
    records0 = _build_records(0, groups0, {}, inputs, pair_weight, calls_edges,
                              test_targets, method, computed_at)
    for rec in records0:
        rec["parent"] = id0_to_id1.get(rec["id"], "")

    groups1: Dict[str, List[str]] = {}
    for id0, files in groups0.items():
        id1 = id0_to_id1.get(id0, id0)
        groups1.setdefault(id1, []).extend(files)
    for files in groups1.values():
        files.sort()
    records1 = _build_records(1, groups1, {}, inputs, pair_weight, calls_edges,
                              test_targets, method, computed_at)

    all_records = records0 + records1
    _persist(root, project_id, fingerprint, all_records)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    logger.info("code_graph.communities: built %d level-0 / %d level-1 communities "
               "(%s, %d files) in %dms", len(records0), len(records1), method,
               len(inputs.paths), elapsed_ms)
    for rec in all_records:
        rec["_elapsed_ms"] = elapsed_ms
    return all_records


# ── rendering ────────────────────────────────────────────────────────────

def _render_list(records: List[Dict[str, Any]], output_chars: Optional[int]) -> str:
    lines = []
    for r in sorted(records, key=lambda x: (-x["size"], x["id"])):
        lines.append(
            f"{r['id']}  {r['name']}  ({r['size']} symbols, {len(r['files'])} files, "
            f"cohesion={r['cohesion']:.2f})")
    return _clip("\n".join(lines) or "(no communities)", output_chars)


def _render_detail(r: Dict[str, Any], output_chars: Optional[int]) -> str:
    lines = [
        f"{r['id']}  {r['name']}  [level {r['level']}]",
        r["purpose"],
        f"{r['size']} symbols across {len(r['files'])} file(s), "
        f"language={r['dominant_language'] or '?'}, cohesion={r['cohesion']:.2f}",
    ]
    if r["key_symbols"]:
        lines.append("Key symbols (top internal fan-in):")
        lines += [f"  {s['symbol']}  {s['path']}:{s['line']}  x{s['fan_in']}"
                 for s in r["key_symbols"]]
    if r["routes"]:
        lines.append("Routes:")
        lines += [f"  {x['route']}  {x['path']}:{x['line']}" for x in r["routes"]]
    if r["entry_points"]:
        lines.append("Entry points:")
        lines += [f"  {x['symbol']} ({x['kind']})  {x['path']}:{x['line']}"
                 for x in r["entry_points"]]
    if r["test_files"]:
        lines.append(f"Test files exercising this community ({len(r['test_files'])}):")
        lines += [f"  {t}" for t in r["test_files"][:20]]
    if r["coupling"]:
        lines.append("Coupled with (by cross-edge weight):")
        lines += [f"  {c['community_id']}  weight={c['weight']}" for c in r["coupling"]]
    if r["summary"]:
        lines.append(f"Summary: {r['summary']}")
    return _clip("\n".join(lines), output_chars)


# ── public API ───────────────────────────────────────────────────────────

def communities(root: str = "", *, project_id: str = "", level: int = 0,
                refresh: bool = False, summarize: bool = False,
                output_chars: int = DEFAULT_OUTPUT_CHARS) -> Dict[str, Any]:
    """What parts this repo is made of, at `level` 0 (fine, per-module) or
    1 (coarse groups of those modules).

    Cached per `(workspace, project_id)` and the index's own fingerprint --
    a second call with an unchanged index is a cache read, a changed one
    rebuilds. `refresh=True` forces a rebuild even when the fingerprint
    matches. `summarize=True` additionally tries a one-sentence model
    summary per community (see `_maybe_summarize`); it never blocks on a
    model that would have to be loaded and never changes the deterministic
    fields."""
    try:
        resolved = _root(root)
    except ValueError as exc:
        return {"error": str(exc), "exit_code": 1}
    try:
        code_index.refresh(resolved, project_id=project_id, budget_files=_INDEX_BUDGET_FILES)
    except Exception as exc:  # noqa: BLE001
        logger.warning("code_graph.communities: index refresh failed: %s", exc)

    try:
        lvl = 1 if int(level or 0) >= 1 else 0
    except (TypeError, ValueError):
        lvl = 0

    fp = _fingerprint(resolved, project_id)
    all_records = None if refresh else _load_cached(resolved, project_id, fp)
    if all_records is None:
        all_records = _build(resolved, project_id, fp)

    if summarize:
        _maybe_summarize(resolved, project_id, fp, all_records)

    picked = [r for r in all_records if r["level"] == lvl]
    method = picked[0]["method"] if picked else "louvain"
    return {
        "output": _render_list(picked, output_chars), "exit_code": 0, "root": resolved,
        "level": lvl, "method": method, "fingerprint": fp,
        "communities": [{k: v for k, v in r.items() if not k.startswith("_")} for r in picked],
    }


def community(root: str, id_or_name_or_symbol: str, *, project_id: str = "",
             output_chars: int = DEFAULT_OUTPUT_CHARS) -> Dict[str, Any]:
    """One community's full detail, by its id, a name substring, or a
    symbol name/qualname it contains (resolved via `community_of`)."""
    try:
        resolved = _root(root)
    except ValueError as exc:
        return {"error": str(exc), "exit_code": 1}
    query = str(id_or_name_or_symbol or "").strip()
    if not query:
        return {"error": "community: an id, name or symbol is required", "exit_code": 1}
    try:
        code_index.refresh(resolved, project_id=project_id, budget_files=_INDEX_BUDGET_FILES)
    except Exception as exc:  # noqa: BLE001
        logger.warning("code_graph.communities: index refresh failed: %s", exc)
    fp = _fingerprint(resolved, project_id)
    all_records = _load_cached(resolved, project_id, fp)
    if all_records is None:
        all_records = _build(resolved, project_id, fp)

    hit = next((r for r in all_records if r["id"] == query), None)
    if hit is None:
        low = query.lower()
        named = [r for r in all_records if low in r["name"].lower()]
        if named:
            named.sort(key=lambda r: (r["level"], -r["size"]))
            hit = named[0]
    if hit is None:
        found = community_of(resolved, query, project_id=project_id)
        if found.get("exit_code") == 0 and found.get("community_id"):
            hit = next((r for r in all_records
                       if r["id"] == found["community_id"] and r["level"] == 0), None)
    if hit is None:
        return {"output": f"no community matches {query!r}", "exit_code": 1, "root": resolved}
    return {"output": _render_detail(hit, output_chars), "exit_code": 0, "root": resolved,
            "community": {k: v for k, v in hit.items() if not k.startswith("_")}}


def community_of(root: str, symbol: str, *, project_id: str = "") -> Dict[str, Any]:
    """The level-0 community id/name a symbol's file belongs to."""
    try:
        resolved = _root(root)
    except ValueError as exc:
        return {"error": str(exc), "exit_code": 1}
    name = str(symbol or "").strip()
    if not name:
        return {"error": "community_of: symbol is required", "exit_code": 1}
    try:
        code_index.refresh(resolved, project_id=project_id, budget_files=_INDEX_BUDGET_FILES)
    except Exception as exc:  # noqa: BLE001
        logger.warning("code_graph.communities: index refresh failed: %s", exc)
    sym = _resolve_symbol(name, root=resolved, project_id=project_id)
    if not sym:
        return {"output": f"symbol not found: {name!r}", "exit_code": 1, "root": resolved}
    path = str(sym.get("path") or "")
    fp = _fingerprint(resolved, project_id)
    try:
        with ce_store.db() as conn:
            where, params = _where(resolved, project_id, extra="fingerprint = ? AND path = ?")
            params = [*params, fp, path]
            row = conn.execute(
                f"SELECT community_id FROM code_community_files WHERE {where}", params
            ).fetchone()
    except (ce_store.ContextStoreError, sqlite3.Error) as exc:
        logger.warning("code_graph.communities: community_of lookup failed: %s", exc)
        row = None
    if row is None:
        _build(resolved, project_id, fp)
        try:
            with ce_store.db() as conn:
                where, params = _where(resolved, project_id, extra="fingerprint = ? AND path = ?")
                params = [*params, fp, path]
                row = conn.execute(
                    f"SELECT community_id FROM code_community_files WHERE {where}", params
                ).fetchone()
        except (ce_store.ContextStoreError, sqlite3.Error):
            row = None
    if row is None:
        return {"output": f"no community found for {sym['qualname']}", "exit_code": 1,
                "root": resolved}
    community_id = str(row["community_id"])
    return {"output": f"{sym['qualname']} ({path}) is in {community_id}", "exit_code": 0,
            "root": resolved, "symbol": sym["qualname"], "path": path,
            "community_id": community_id}


# ── optional model summaries ─────────────────────────────────────────────

def _maybe_summarize(root: str, project_id: str, fingerprint: str,
                     records: List[Dict[str, Any]]) -> None:
    """Best-effort one-sentence summary per community, via the utility model,
    ONLY when it is already resident and idle -- never inside a chat turn,
    never loading or evicting a model. Off entirely unless the
    `code_graph_community_summaries` setting is on. Mirrors
    `src.brain.wiki.refresh_entity`'s residency check
    (`src.brain.extract.background_llm_gate`), the same pattern the harness
    contract points at."""
    try:
        from src.settings import get_setting
        if not bool(get_setting("code_graph_community_summaries", False)):
            return
    except Exception:  # noqa: BLE001
        return
    to_summarize = [r for r in records if not r.get("summary")]
    if not to_summarize:
        return
    try:
        from src.endpoint_resolver import resolve_endpoint
        url, model, headers = resolve_endpoint("utility", owner="")
    except Exception:  # noqa: BLE001
        return
    if not url or not model:
        return
    try:
        from src.brain.extract import background_llm_gate
        reason = background_llm_gate(url, model)
    except Exception:  # noqa: BLE001
        reason = "residency_unknown"
    if reason:
        logger.debug("code_graph.communities: summaries deferred (%s)", reason)
        return

    import asyncio
    from src.llm_core import llm_call_async

    async def _one(rec: Dict[str, Any]) -> None:
        prompt = (
            "In one plain sentence, describe what this code module is for. "
            "Do not invent anything not implied by these names.\n"
            f"Path prefix: {_common_dir_prefix(rec['files'])}\n"
            f"Key symbols: {', '.join(s['symbol'] for s in rec['key_symbols'][:6])}\n"
            f"Routes: {', '.join(r['route'] for r in rec['routes'][:3])}\n"
        )
        try:
            raw = await llm_call_async(
                url=url, model=model, messages=[{"role": "user", "content": prompt}],
                headers=headers, temperature=0.2, max_tokens=120, timeout=30,
                max_retries=1, workload="background")
        except Exception as exc:  # noqa: BLE001
            logger.debug("code_graph.communities: summary call failed for %s: %s",
                        rec["id"], exc)
            return
        if isinstance(raw, tuple):
            raw = raw[0]
        text = str(raw or "").strip()
        if text:
            rec["summary"] = text[:400]

    async def _all() -> None:
        for rec in to_summarize:
            await _one(rec)

    try:
        asyncio.run(_all())
    except Exception as exc:  # noqa: BLE001
        logger.debug("code_graph.communities: summarize pass failed: %s", exc)
        return
    _persist(root, project_id, fingerprint, records)


__all__ = ["communities", "community", "community_of"]
