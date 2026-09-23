"""src/code_graph/flows.py — "what paths run through this, and how critical
are they?"

An execution flow is one entry point (an HTTP route, an agent-tool `execute`
method, an MCP tool handler, a `main`, or a public symbol nothing else in the
workspace calls) followed outward along resolved `calls` edges into a call
tree — the shape a request or a command actually takes through the codebase,
as opposed to `code_graph.communities`' answer to "what modules exist" or
`code_graph.trace_path`'s answer to "how does A reach B".

Entry points, and what is deliberately NOT attempted:

* `kind == "route"` symbols — already extracted by `code_index` from FastAPI-
  style decorators.
* `kind == "tool"` symbols — already extracted from `@tool`/`@register_tool`/
  `@mcp_tool` decorators.
* Agent-tool executors: a method named `execute` on a class whose name ends
  in `Tool` — this repository's own convention (`CodeGraphSearchTool.execute`,
  ...), matched on the qualname `code_index` already records rather than
  needing a new extraction rule.
* `main`-named functions, as a stand-in for "the module run as a script".
* Public roots: a function/method (not `_private`, not itself in a test
  file) with outgoing `calls` edges but **no caller from a non-test file** —
  the request/command has to start somewhere, and a symbol nothing in the
  production code calls is either an unused entry point or one only tests
  reach directly.
* CLI/command-framework decorators (click, typer, argparse subcommands) are
  NOT detected: `code_index`'s Python extractor only special-cases route and
  tool decorator tails (`_ROUTE_DECORATORS`/`_TOOL_DECORATORS`); teaching it
  a third vocabulary is outside this module's file ownership. A CLI handler
  still surfaces here if it also qualifies as a "public root" above.

Given how common "public root" candidates are in any codebase (every
unused-but-exported helper qualifies), routes/tools/executors are processed
in full and public roots are capped (`_MAX_ROOT_ENTRIES`, ranked by their own
fan-out as a cheap proxy for "worth walking") so one call never turns into
thousands of near-duplicate one-hop flows.

Traversal is DFS (a stack, not recursion — a real call graph can be deep
enough to hit Python's recursion limit), preorder, over a fixed, sorted
child order, so the member list is deterministic and its `depth` field alone
is enough to render an indented call tree. A global `visited` set makes it
cycle-safe: a symbol reached once is never re-expanded from a second path,
which turns a call graph with cycles into a spanning tree in exchange for
never finding the SAME node twice at two different depths — depth-capped and
node-capped in the same style as `query.impact`'s BFS.

Criticality (0..1, formula documented on `_CRITICALITY_WEIGHTS`) is *not* a
risk score like `code_graph.risk.change_risk` — it says how central and
consequential this execution path already is, independent of any pending
edit. Persisted the same way `code_graph.communities` is: one row per flow,
keyed by `(workspace, project_id, fingerprint)`, `fingerprint` shared with
`communities` (both derive from the same index status) so cache invalidation
happens together.
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

from .communities import _fingerprint, _where  # shared fingerprint/scope helpers
from .query import DEFAULT_OUTPUT_CHARS, _changed_seeds, _clip, _is_test_path, _resolve_symbol, _root

logger = logging.getLogger(__name__)

ce_store.register_schema("code_flows", (
    """
    CREATE TABLE IF NOT EXISTS code_flows (
        workspace     TEXT NOT NULL DEFAULT '',
        project_id    TEXT NOT NULL DEFAULT '',
        fingerprint   TEXT NOT NULL DEFAULT '',
        id            TEXT NOT NULL DEFAULT '',
        entry_symbol  TEXT NOT NULL DEFAULT '',
        name          TEXT NOT NULL DEFAULT '',
        criticality   REAL NOT NULL DEFAULT 0.0,
        data          TEXT NOT NULL DEFAULT '{}',
        computed_at   TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (workspace, project_id, fingerprint, id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_code_flows_scope "
    "ON code_flows(workspace, project_id, fingerprint)",
    "CREATE INDEX IF NOT EXISTS ix_code_flows_criticality "
    "ON code_flows(workspace, project_id, fingerprint, criticality)",
))

_INDEX_BUDGET_FILES = 20000
_DEFAULT_DEPTH_CAP = 6
_MAX_DEPTH_CAP = 10
_NODE_CAP = 200
_MAX_ROOT_ENTRIES = 300
_MAX_TOTAL_ENTRIES = 1500

_HIGH_FANIN_THRESHOLD = 5
_SINK_RE = re.compile(r"\b(?:db|commit|write|send|delete|subprocess|requests)\w*", re.I)

#: Entry-point kind, ranked coarsest-first for the default (criticality)
#: sort in `flows()` -- a real HTTP route or tool handler is what a human
#: asking "what are the important paths here?" means, and should never be
#: outranked by an internal "root" (a symbol nothing else happens to call)
#: just because its own call tree is bigger, and especially never by a
#: "vendor_root" (a root candidate inside a detected vendored/third-party
#: directory -- see `_vendor_dirs`), which is often the internal entry
#: point of a whole library the app merely depends on. Ties within a kind
#: still sort by criticality (see `flows()`).
_KIND_PRIORITY: Dict[str, int] = {
    "route": 0, "tool": 1, "tool_executor": 1, "main": 2, "root": 3, "vendor_root": 4,
}

#: A directory's own name matching one of these is a strong structural
#: signal it holds vendored/third-party code, however the vendoring was
#: actually done (a git submodule, a vendoring script, a manual copy) --
#: see `_vendor_dirs`.
_VENDOR_NAME_RE = re.compile(
    r"(?:^|/)(?:vendor|vendored|third[-_]?party|thirdparty|external|contrib)(?:/|$)", re.I)
#: A marker file commonly shipped inside (or naming) a vendored library's
#: own directory -- checked on disk since these are not source files
#: `code_index` indexes as symbols. Deliberately conservative: a bare
#: top-level `LICENSE`/`NOTICE` for the whole project is common and NOT a
#: vendoring signal by itself -- see `_vendor_dirs` for why the project
#: root is excluded from this check.
_VENDOR_MARKER_RE = re.compile(
    r"(?:^|/)(?:VENDORED(?:\.\w+)?|THIRD_PARTY(?:\.\w+)?|NOTICE(?:\.\w+)?|LICEN[CS]E(?:\.\w+)?)$",
    re.I)
_HTTP_VERBS = {"get", "post", "put", "patch", "delete", "head", "options"}
_ROUTE_SIG_RE = re.compile(r"^([\w.]+)\s+(\S.*)$")

# score = sum(weight * normalised); weights sum to 1.0 -- see `_criticality`.
_CRITICALITY_WEIGHTS: Dict[str, float] = {
    "size": 0.20, "files": 0.15, "communities": 0.15,
    "high_fanin": 0.15, "sinks": 0.25, "coverage": 0.10,
}
assert abs(sum(_CRITICALITY_WEIGHTS.values()) - 1.0) < 1e-9
_SIZE_CAP = 30.0
_FILES_CAP = 10.0
_COMMUNITIES_CAP = 5.0
_HIGH_FANIN_CAP = 5.0
_SINKS_CAP = 5.0


def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)


# ── loading ──────────────────────────────────────────────────────────────

class _Sym:
    __slots__ = ("path", "kind", "qualname", "name", "line", "signature")

    def __init__(self, path: str, kind: str, qualname: str, name: str, line: int,
                 signature: str = "") -> None:
        self.path = path
        self.kind = kind
        self.qualname = qualname
        self.name = name
        self.line = line
        self.signature = signature


def _load_symbols(conn: sqlite3.Connection, workspace: str, project_id: str
                   ) -> Dict[str, _Sym]:
    where, params = _where(workspace, project_id)
    out: Dict[str, _Sym] = {}
    for row in conn.execute(
            f"SELECT id, path, kind, qualname, name, start_line, signature FROM code_symbols "
            f"WHERE {where}", params):
        out[str(row["id"])] = _Sym(str(row["path"]), str(row["kind"]), str(row["qualname"]),
                                   str(row["name"]), int(row["start_line"] or 0),
                                   str(row["signature"] or ""))
    return out


def _load_calls(conn: sqlite3.Connection, workspace: str, project_id: str
                 ) -> List[Tuple[str, str, str]]:
    where, params = _where(workspace, project_id, extra="kind = 'calls'")
    return [(str(r["src"]), str(r["dst"]), str(r["certainty"]))
            for r in conn.execute(
                f"SELECT src, dst, certainty FROM code_edges WHERE {where}", params)]


def _load_test_targets(conn: sqlite3.Connection, workspace: str, project_id: str
                        ) -> Set[str]:
    """File paths any `tests` edge points at -- "this file is exercised by a
    test", independent of any one flow."""
    where, params = _where(workspace, project_id, extra="kind = 'tests'")
    dst_ids = [str(r["dst"]) for r in conn.execute(
        f"SELECT dst FROM code_edges WHERE {where}", params)]
    if not dst_ids:
        return set()
    where2, params2 = _where(workspace, project_id)
    marks = ",".join("?" * len(dst_ids))
    rows = conn.execute(
        f"SELECT DISTINCT path FROM code_symbols WHERE {where2} AND id IN ({marks})",
        [*params2, *dst_ids])
    return {str(r["path"]) for r in rows}


def _load_community_files(conn: sqlite3.Connection, workspace: str, project_id: str,
                           fingerprint: str) -> Dict[str, str]:
    """Best-effort `path -> level-0 community id`, empty when `communities()`
    has not been run for this fingerprint yet -- flows must not force a
    Louvain pass of its own just to compute one criticality factor."""
    try:
        where, params = _where(workspace, project_id, extra="fingerprint = ?")
        params = [*params, fingerprint]
        rows = conn.execute(
            f"SELECT path, community_id FROM code_community_files WHERE {where}", params)
        return {str(r["path"]): str(r["community_id"]) for r in rows}
    except sqlite3.Error:
        return {}


# ── entry point discovery ───────────────────────────────────────────────

def _is_tool_executor(sym: _Sym) -> bool:
    if sym.kind != "method" or sym.name != "execute" or "." not in sym.qualname:
        return False
    owner = sym.qualname.rsplit(".", 1)[0]
    return owner.endswith("Tool")


def _route_label(signature: str, qualname: str) -> str:
    """`"GET /orders"`-style display label for a route entry point, parsed
    from `code_index`'s own recorded `signature` (`_decorator_kind` writes
    it as `"<dotted> <route>"`, e.g. `"router.post /orders"`). Falls back to
    the symbol's qualname when the signature isn't in that shape (a route
    kind from a decorator tail `code_index` doesn't specialise beyond a
    known HTTP verb, or a cached row from before this parsing existed) --
    always returns something displayable, never an empty label."""
    m = _ROUTE_SIG_RE.match(signature or "")
    if m:
        dotted, route = m.group(1), m.group(2).strip()
        verb = dotted.rsplit(".", 1)[-1].lower()
        if verb in _HTTP_VERBS and route.startswith("/"):
            return f"{verb.upper()} {route}"
    return qualname


def _vendor_dirs(symbols: Dict[str, _Sym], calls: Sequence[Tuple[str, str, str]],
                  root: str) -> Set[str]:
    """Directory paths (relative, forward-slash, `""` for the repo root)
    whose ROOT-CANDIDATE entry points get demoted to `"vendor_root"` -- see
    `_entry_points` and the `_KIND_PRIORITY` note. Two independent signals,
    either sufficient on its own:

    - **structural**: the directory's own name matches a vendor/third-party
      naming convention (`_VENDOR_NAME_RE`), or it directly contains a
      vendoring marker file (`_VENDOR_MARKER_RE`) -- checked on disk, since
      these are not source files `code_index` indexes as symbols. The repo
      root itself (`""`) is never flagged this way even if it happens to
      hold a top-level `LICENSE`/`NOTICE` -- that describes the whole
      project, not a vendored dependency inside it.
    - **behavioral**: the directory receives `calls` edges from OTHER
      directories but never itself makes a `calls` edge into another
      directory -- the one-way dependency shape of "the app calls into this
      library, which never calls back out" -- regardless of what the
      directory happens to be named. A directory with no incoming
      cross-directory calls at all is not flagged this way (silence is not
      a signal; only a one-way relationship is).

    Applied per immediate directory of the file, not per top-level path
    segment, so a vendored library nested a few levels deep is caught
    precisely rather than tainting its whole parent tree."""
    dir_of: Dict[str, str] = {}
    for sym in symbols.values():
        if sym.path not in dir_of:
            dir_of[sym.path] = sym.path.rsplit("/", 1)[0] if "/" in sym.path else ""
    all_dirs = set(dir_of.values())

    structural: Set[str] = {d for d in all_dirs if d and _VENDOR_NAME_RE.search(d)}

    try:
        for dirpath, _dirnames, filenames in os.walk(root):
            rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
            if rel_dir == ".":
                continue  # repo root: see docstring, never flagged this way
            if rel_dir not in all_dirs:
                continue
            if any(_VENDOR_MARKER_RE.search(fn) for fn in filenames):
                structural.add(rel_dir)
    except OSError:
        pass

    out_dirs: Dict[str, Set[str]] = {}
    in_dirs: Dict[str, Set[str]] = {}
    for src, dst, _cert in calls:
        src_sym, dst_sym = symbols.get(src), symbols.get(dst)
        if not src_sym or not dst_sym:
            continue
        sd, dd = dir_of.get(src_sym.path, ""), dir_of.get(dst_sym.path, "")
        if sd == dd:
            continue
        out_dirs.setdefault(sd, set()).add(dd)
        in_dirs.setdefault(dd, set()).add(sd)

    behavioral = {d for d, callers in in_dirs.items() if d and callers and not out_dirs.get(d)}
    return structural | behavioral


def _entry_points(symbols: Dict[str, _Sym], calls: Sequence[Tuple[str, str, str]],
                   vendor_dirs: Set[str]) -> List[Tuple[str, str]]:
    """`[(symbol_id, reason)]`, routes/tools/executors first (uncapped),
    public roots last (capped and ranked by fan-out, non-vendored roots
    ranked ahead of vendored ones regardless of fan-out) -- see the module
    docstring, `_KIND_PRIORITY`, and `_vendor_dirs`."""
    fanout: Dict[str, int] = {}
    caller_is_nontest: Set[str] = set()
    for src, dst, _cert in calls:
        fanout[src] = fanout.get(src, 0) + 1
        src_sym = symbols.get(src)
        if src_sym and not _is_test_path(src_sym.path):
            caller_is_nontest.add(dst)

    def _is_vendor(sid: str) -> bool:
        path = symbols[sid].path
        d = path.rsplit("/", 1)[0] if "/" in path else ""
        return d in vendor_dirs

    routes: List[Tuple[str, str]] = []
    tools: List[Tuple[str, str]] = []
    executors: List[Tuple[str, str]] = []
    roots: List[Tuple[str, int]] = []
    for sid, sym in symbols.items():
        if _is_test_path(sym.path):
            continue  # tests are never entry points
        if sym.kind == "route":
            routes.append((sid, "route"))
        elif sym.kind == "tool":
            tools.append((sid, "tool"))
        elif _is_tool_executor(sym):
            executors.append((sid, "tool_executor"))
        elif sym.kind == "function" and sym.name == "main":
            executors.append((sid, "main"))
        elif sym.kind in ("function", "method") and not sym.name.startswith("_") \
                and sid in fanout and sid not in caller_is_nontest:
            roots.append((sid, fanout[sid]))

    routes.sort(key=lambda p: (symbols[p[0]].path, symbols[p[0]].line))
    tools.sort(key=lambda p: (symbols[p[0]].path, symbols[p[0]].line))
    executors.sort(key=lambda p: (symbols[p[0]].path, symbols[p[0]].line))
    roots.sort(key=lambda p: (_is_vendor(p[0]), -p[1], symbols[p[0]].path, symbols[p[0]].line))
    root_entries = [(sid, "vendor_root" if _is_vendor(sid) else "root")
                    for sid, _fanout in roots[:_MAX_ROOT_ENTRIES]]

    picked = routes + tools + executors + root_entries
    return picked[:_MAX_TOTAL_ENTRIES]


# ── traversal ────────────────────────────────────────────────────────────

def _dfs_flow(entry_id: str, symbols: Dict[str, _Sym],
              calls_out: Dict[str, List[Tuple[str, str, str]]],
              *, depth_cap: int, node_cap: int) -> List[Dict[str, Any]]:
    """Preorder DFS member list (excludes the entry itself), each carrying
    the edge it was reached by. Deterministic: children of a node are always
    visited in `(path, line)` order."""
    visited = {entry_id}
    members: List[Dict[str, Any]] = []
    # stack entries: (symbol_id, depth, via_qualname, via_certainty)
    stack: List[Tuple[str, int, str, str]] = [(entry_id, 0, "", "")]
    while stack and len(members) < node_cap:
        sid, depth, via, certainty = stack.pop()
        if sid != entry_id:
            sym = symbols.get(sid)
            if sym is None:
                continue
            members.append({
                "symbol_id": sid, "symbol": sym.qualname, "kind": sym.kind,
                "path": sym.path, "line": sym.line, "depth": depth,
                "certainty": certainty, "via": via,
            })
        if depth >= depth_cap:
            continue
        children = sorted(
            calls_out.get(sid, ()),
            key=lambda c: (symbols[c[1]].path, symbols[c[1]].line) if c[1] in symbols else ("", 0),
            reverse=True,
        )
        for _src, dst_id, cert in children:
            if dst_id in visited or dst_id not in symbols:
                continue
            visited.add(dst_id)
            stack.append((dst_id, depth + 1, symbols[sid].qualname if sid in symbols
                         else "", cert))
    return members


# ── criticality ──────────────────────────────────────────────────────────

def _criticality(entry_sym: _Sym, members: List[Dict[str, Any]],
                  fanin_count: Dict[str, int], test_targets: Set[str],
                  community_of_path: Dict[str, str]) -> Dict[str, Any]:
    """0..1 score from six normalised, capped factors (weights above, sum
    1.0): `size` (member count), `files` (distinct files spanned),
    `communities` (distinct level-0 communities crossed, 1 when
    `communities()` has never run so the factor cannot be computed),
    `high_fanin` (members with >= `_HIGH_FANIN_THRESHOLD` distinct callers
    anywhere in the workspace), `sinks` (members whose name suggests a
    side-effect -- db/commit/write/send/delete/subprocess/requests, the
    heaviest weight because a path that mutates state or talks to the
    network matters more than one that only reads), and `coverage` (the
    INVERSE of the fraction of members whose file a test exercises -- an
    important, untested path is more critical, not less)."""
    paths = {entry_sym.path} | {m["path"] for m in members}
    files_spanned = len(paths)
    size = len(members) + 1  # entry counts as a member of its own flow

    communities = {community_of_path[p] for p in paths if p in community_of_path}
    communities_crossed = len(communities) if communities else 1

    high_fanin = sum(1 for m in members if fanin_count.get(m["symbol_id"], 0) >= _HIGH_FANIN_THRESHOLD)
    sinks = sum(1 for m in members if _SINK_RE.search(m["symbol"]))
    covered = sum(1 for p in paths if p in test_targets)
    coverage_fraction = covered / max(1, len(paths))

    factors = {
        "size": {"raw": size, "normalized": round(_clamp01(size / _SIZE_CAP), 4)},
        "files": {"raw": files_spanned, "normalized": round(_clamp01(files_spanned / _FILES_CAP), 4)},
        "communities": {"raw": communities_crossed,
                        "normalized": round(_clamp01(communities_crossed / _COMMUNITIES_CAP), 4)},
        "high_fanin": {"raw": high_fanin, "normalized": round(_clamp01(high_fanin / _HIGH_FANIN_CAP), 4)},
        "sinks": {"raw": sinks, "normalized": round(_clamp01(sinks / _SINKS_CAP), 4)},
        "coverage": {"raw": round(coverage_fraction, 4),
                     "normalized": round(_clamp01(1.0 - coverage_fraction), 4)},
    }
    score = round(sum(_CRITICALITY_WEIGHTS[k] * f["normalized"] for k, f in factors.items()), 4)
    for k, f in factors.items():
        f["weight"] = _CRITICALITY_WEIGHTS[k]
        f["contribution"] = round(_CRITICALITY_WEIGHTS[k] * f["normalized"], 4)
    return {
        "score": score, "factors": factors,
        "test_coverage_fraction": round(coverage_fraction, 4),
        "files_spanned": files_spanned, "communities_crossed": communities_crossed,
    }


# ── build ────────────────────────────────────────────────────────────────

def _flow_id(entry_id: str, member_ids: Sequence[str]) -> str:
    blob = "\x00".join([entry_id, *sorted(member_ids)]).encode("utf-8", "replace")
    return "flow_" + hashlib.sha256(blob).hexdigest()[:16]


def _build(root: str, project_id: str, fingerprint: str) -> List[Dict[str, Any]]:
    started = time.monotonic()
    with ce_store.db() as conn:
        symbols = _load_symbols(conn, root, project_id)
        calls = _load_calls(conn, root, project_id)
        test_targets = _load_test_targets(conn, root, project_id)
        community_of_path = _load_community_files(conn, root, project_id, fingerprint)

    calls_out: Dict[str, List[Tuple[str, str, str]]] = {}
    fanin_count: Dict[str, int] = {}
    for src, dst, cert in calls:
        if src not in symbols or dst not in symbols:
            continue
        calls_out.setdefault(src, []).append((src, dst, cert))
        fanin_count[dst] = fanin_count.get(dst, 0) + 1

    vendor_dirs = _vendor_dirs(symbols, calls, root)
    entries = _entry_points(symbols, calls, vendor_dirs)
    computed_at = ce_store.now_iso()
    records: List[Dict[str, Any]] = []
    for entry_id, reason in entries:
        entry_sym = symbols.get(entry_id)
        if entry_sym is None:
            continue
        members = _dfs_flow(entry_id, symbols, calls_out,
                            depth_cap=_DEFAULT_DEPTH_CAP, node_cap=_NODE_CAP)
        crit = _criticality(entry_sym, members, fanin_count, test_targets, community_of_path)
        name = _route_label(entry_sym.signature, entry_sym.qualname) if reason == "route" \
            else entry_sym.qualname
        fid = _flow_id(entry_id, [m["symbol_id"] for m in members])
        records.append({
            "id": fid, "entry_symbol": entry_sym.qualname, "entry_reason": reason,
            "name": name,
            "entry": {"symbol": entry_sym.qualname, "kind": entry_sym.kind,
                      "path": entry_sym.path, "line": entry_sym.line, "symbol_id": entry_id},
            "members": members, "files": sorted({entry_sym.path} | {m["path"] for m in members}),
            "criticality": crit["score"], "criticality_factors": crit["factors"],
            "test_coverage_fraction": crit["test_coverage_fraction"],
            "files_spanned": crit["files_spanned"], "communities_crossed": crit["communities_crossed"],
            "computed_at": computed_at,
        })

    _persist(root, project_id, fingerprint, records)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    logger.info("code_graph.flows: built %d flow(s) from %d entry point(s) in %dms",
               len(records), len(entries), elapsed_ms)
    return records


# ── persistence ──────────────────────────────────────────────────────────

_DATA_KEYS = ("entry_reason", "entry", "members", "files", "criticality_factors",
              "test_coverage_fraction", "files_spanned", "communities_crossed")


def _persist(root: str, project_id: str, fingerprint: str, records: List[Dict[str, Any]]) -> None:
    try:
        with ce_store.db() as conn:
            conn.execute(
                "DELETE FROM code_flows WHERE workspace = ? AND project_id = ? "
                "AND fingerprint != ?", (root, project_id, fingerprint))
            rows = [(
                root, project_id, fingerprint, r["id"], r["entry_symbol"], r["name"],
                r["criticality"], ce_store.dumps({k: r[k] for k in _DATA_KEYS}), r["computed_at"],
            ) for r in records]
            conn.executemany(
                "INSERT OR REPLACE INTO code_flows "
                "(workspace, project_id, fingerprint, id, entry_symbol, name, criticality, "
                "data, computed_at) VALUES (?,?,?,?,?,?,?,?,?)", rows)
    except (ce_store.ContextStoreError, sqlite3.Error) as exc:
        logger.warning("code_graph.flows: persist failed: %s", exc)


def _row_to_record(row: Dict[str, Any]) -> Dict[str, Any]:
    data = ce_store.loads_dict(row.get("data"))
    rec = {
        "id": row["id"], "entry_symbol": row["entry_symbol"], "name": row["name"],
        "criticality": float(row["criticality"]), "computed_at": row["computed_at"],
    }
    for key in _DATA_KEYS:
        if key in ("members", "files"):
            default: Any = []
        elif key in ("entry", "criticality_factors"):
            default = {}
        elif key == "entry_reason":
            default = ""
        else:
            default = 0
        rec[key] = data.get(key, default)
    return rec


def _load_cached(root: str, project_id: str, fingerprint: str) -> Optional[List[Dict[str, Any]]]:
    try:
        with ce_store.db() as conn:
            where, params = _where(root, project_id, extra="fingerprint = ?")
            params = [*params, fingerprint]
            rows = ce_store.rows(conn.execute(f"SELECT * FROM code_flows WHERE {where}", params))
    except (ce_store.ContextStoreError, sqlite3.Error) as exc:
        logger.warning("code_graph.flows: cache read failed: %s", exc)
        return None
    if not rows:
        return None
    return [_row_to_record(r) for r in rows]


def _criticality_level(score: float) -> str:
    if score < 0.3:
        return "low"
    if score < 0.6:
        return "medium"
    return "high"


# ── rendering ────────────────────────────────────────────────────────────

def _render_list(records: List[Dict[str, Any]], output_chars: Optional[int]) -> str:
    lines = []
    for r in records:
        lines.append(f"{r['id']}  [{r.get('entry_reason', '')}] {r['name']}  "
                    f"criticality={r['criticality']:.2f} "
                    f"({_criticality_level(r['criticality'])})  "
                    f"{len(r['members']) + 1} members, {len(r['files'])} files")
    return _clip("\n".join(lines) or "(no flows found)", output_chars)


def _render_tree(r: Dict[str, Any], output_chars: Optional[int]) -> str:
    entry = r["entry"]
    lines = [f"{r['id']}  [{r.get('entry_reason', '')}] {r['name']}  "
            f"criticality={r['criticality']:.2f} "
            f"({_criticality_level(r['criticality'])})",
            f"{entry['symbol']} ({entry['kind']})  {entry['path']}:{entry['line']}"]
    for m in r["members"]:
        indent = "  " * m["depth"]
        lines.append(f"{indent}└─ {m['symbol']} ({m['kind']})  "
                    f"{m['path']}:{m['line']}  [{m['certainty']}]")
    if r.get("criticality_factors"):
        lines.append("Criticality factors:")
        for name, f in sorted(r["criticality_factors"].items(),
                              key=lambda kv: -kv[1]["contribution"]):
            lines.append(f"  {name}: raw={f['raw']} contribution=+{f['contribution']}")
    return _clip("\n".join(lines), output_chars)


# ── public API ───────────────────────────────────────────────────────────

def _ensure_built(root: str, project_id: str, *, refresh: bool = False
                   ) -> Tuple[List[Dict[str, Any]], str]:
    fp = _fingerprint(root, project_id)
    records = None if refresh else _load_cached(root, project_id, fp)
    if records is None:
        records = _build(root, project_id, fp)
    return records, fp


def flows(root: str = "", *, project_id: str = "", limit: int = 20,
          sort: str = "criticality", entry: Optional[str] = None,
          refresh: bool = False, output_chars: int = DEFAULT_OUTPUT_CHARS) -> Dict[str, Any]:
    """Execution flows in this workspace, ranked by `sort` (`criticality` the
    default, or `size`). `entry` narrows to flows whose entry point's
    qualname/name matches (substring, case-insensitive)."""
    try:
        resolved = _root(root)
    except ValueError as exc:
        return {"error": str(exc), "exit_code": 1}
    try:
        code_index.refresh(resolved, project_id=project_id, budget_files=_INDEX_BUDGET_FILES)
    except Exception as exc:  # noqa: BLE001
        logger.warning("code_graph.flows: index refresh failed: %s", exc)

    records, fp = _ensure_built(resolved, project_id, refresh=refresh)
    if entry:
        low = str(entry).lower()
        records = [r for r in records if low in r["entry_symbol"].lower()]

    if sort == "size":
        records = sorted(records, key=lambda r: (-(len(r["members"]) + 1), r["id"]))
    else:
        records = sorted(records, key=lambda r: (
            _KIND_PRIORITY.get(r["entry_reason"], 3), -r["criticality"], r["id"]))

    try:
        cap = max(1, min(int(limit or 20), 500))
    except (TypeError, ValueError):
        cap = 20
    picked = records[:cap]
    return {
        "output": _render_list(picked, output_chars), "exit_code": 0, "root": resolved,
        "fingerprint": fp, "total": len(records),
        "flows": [{"id": r["id"], "name": r["name"], "entry_symbol": r["entry_symbol"],
                  "entry_reason": r["entry_reason"], "criticality": r["criticality"],
                  "criticality_level": _criticality_level(r["criticality"]),
                  "size": len(r["members"]) + 1, "files": r["files"]}
                 for r in picked],
    }


def flow(root: str, id_or_entry: str, *, project_id: str = "",
         output_chars: int = DEFAULT_OUTPUT_CHARS) -> Dict[str, Any]:
    """One flow's full call tree, by its id or its entry point's name."""
    try:
        resolved = _root(root)
    except ValueError as exc:
        return {"error": str(exc), "exit_code": 1}
    query = str(id_or_entry or "").strip()
    if not query:
        return {"error": "flow: an id or entry symbol is required", "exit_code": 1}
    try:
        code_index.refresh(resolved, project_id=project_id, budget_files=_INDEX_BUDGET_FILES)
    except Exception as exc:  # noqa: BLE001
        logger.warning("code_graph.flows: index refresh failed: %s", exc)
    records, _fp = _ensure_built(resolved, project_id)

    hit = next((r for r in records if r["id"] == query), None)
    if hit is None:
        low = query.lower()
        candidates = [r for r in records if low == r["entry_symbol"].lower()
                     or low in r["entry_symbol"].lower()]
        if candidates:
            candidates.sort(key=lambda r: -r["criticality"])
            hit = candidates[0]
    if hit is None:
        return {"output": f"no flow matches {query!r}", "exit_code": 1, "root": resolved}
    return {"output": _render_tree(hit, output_chars), "exit_code": 0, "root": resolved,
            "flow": hit}


def affected_flows(symbol: str = "", *, workspace: str = "", project_id: str = "",
                   base_ref: str = "HEAD", limit: int = 20,
                   output_chars: int = DEFAULT_OUTPUT_CHARS) -> Dict[str, Any]:
    """Flows that pass through `symbol` (its entry point or any member), or
    -- with no symbol -- through any symbol the current git diff touches.
    Sorted by criticality, so the most important broken path is first."""
    try:
        resolved = _root(workspace)
    except ValueError as exc:
        return {"error": str(exc), "exit_code": 1}
    try:
        code_index.refresh(resolved, project_id=project_id, budget_files=_INDEX_BUDGET_FILES)
    except Exception as exc:  # noqa: BLE001
        logger.warning("code_graph.flows: index refresh failed: %s", exc)
    records, fp = _ensure_built(resolved, project_id)

    target_ids: Set[str] = set()
    mode = "symbol"
    name = str(symbol or "").strip()
    if name:
        sym = _resolve_symbol(name, root=resolved, project_id=project_id)
        if not sym:
            return {"output": f"symbol not found: {name!r}", "exit_code": 1, "root": resolved}
        target_ids.add(str(sym["id"]))
    else:
        mode = "diff"
        try:
            seeds = _changed_seeds(resolved, base_ref=base_ref, project_id=project_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("code_graph.flows: diff seeding failed: %s", exc)
            return {"error": f"affected_flows: {exc}", "exit_code": 1, "root": resolved}
        target_ids = {s["id"] for s in seeds}
        if not target_ids:
            return {"output": f"no changed symbols vs {base_ref}", "exit_code": 0,
                    "root": resolved, "mode": mode, "flows": []}

    matched: List[Dict[str, Any]] = []
    for r in records:
        member_ids = {m["symbol_id"] for m in r["members"]} | {r["entry"].get("symbol_id", "")}
        if member_ids & target_ids:
            matched.append(r)
    matched.sort(key=lambda r: (-r["criticality"], r["id"]))

    try:
        cap = max(1, min(int(limit or 20), 500))
    except (TypeError, ValueError):
        cap = 20
    picked = matched[:cap]
    lines = [f"{len(matched)} flow(s) touch the given {'symbol' if mode == 'symbol' else 'diff'}:"]
    for r in picked:
        lines.append(f"  {r['id']}  {r['name']}  criticality={r['criticality']:.2f}")
    return {
        "output": _clip("\n".join(lines), output_chars), "exit_code": 0, "root": resolved,
        "mode": mode, "fingerprint": fp, "total": len(matched),
        "flows": [{"id": r["id"], "name": r["name"], "entry_symbol": r["entry_symbol"],
                  "criticality": r["criticality"],
                  "criticality_level": _criticality_level(r["criticality"]), "files": r["files"]}
                 for r in picked],
    }


__all__ = ["flows", "flow", "affected_flows"]
