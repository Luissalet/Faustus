"""src/code_graph/communities.py — "what parts is this repo made of?"

`context_engine.code_index` already resolves symbols, calls, imports and
routes; this module answers the one architecture question it does not: group
those symbols into the modules a person would actually name ("the auth
service", "the code graph", "the studio brain screen") without reading a
single file.

Design decisions, and why:

* **Nodes are production files, not symbols, and not test files either.**
  Test files are never graph nodes for clustering: a test file typically
  imports/exercises a dozen unrelated modules across the whole repository,
  and if it clustered like any other node it would either drag unrelated
  production files together or form its own meaningless "tests" community
  (which read as noise, not architecture, to a human skimming the top-level
  list). Instead every test file is attached, after clustering, to the one
  production community it exercises most (by the summed weight of its
  `calls`/`imports`/`tests`/`registers` edges into that community's files) —
  reported as that community's `test_files`, never as its own community. A
  test file with no such edge anywhere lands in one shared "Unattached
  tests" bucket per level, excluded from the default listing.
* **A community must earn its place: no singletons, no giant blobs.** Two
  post-processing passes run after clustering, both keyed off actual graph
  signal rather than an arbitrary count alone:
  - `_fold_tiny_groups` merges any group under a repo-size-scaled minimum
    file count (`_fold_min_files_for`) into, in order, (a) the other group
    it has the strongest real edge weight to, (b) the group holding a
    majority of the *other* files in the same directory, or (c) a
    per-top-level-directory "loose files" bucket if neither exists — so an
    isolated or near-isolated file never becomes its own one-file
    "community". The threshold scales with repo size (`_fold_min_files_for`)
    so a 60-file app isn't held to the same absolute floor as a 2,500-file
    monorepo.
  - `_split_catchalls` breaks up the opposite failure mode: a group past a
    repo-size-scaled file count (`_catchall_min_files_for`) that is either
    low-cohesion (`<0.30`, a loosely-coupled "glue" region — routing setup,
    `__init__` re-exports, ...) or simply dominates the repo
    (`_CATCHALL_DOMINANCE_FRACTION` of all production files, even at high
    cohesion — one small, tightly-coupled package Louvain never had a reason
    to split). Rather than present that as one unreadable blob, it is split
    into real sub-communities: first by re-running Louvain on the blob's own
    internal subgraph at a higher resolution (`split_by: "call_graph"`),
    falling back to a directory split (`split_by: "directory"`) only when
    that doesn't yield usable sub-groups.
* **Edges are weighted by kind and certainty, aggregated per file pair.**
  `calls` edges are the strongest signal of "these two files work together"
  (weight 3.0), `registers` next (a route/tool wiring itself in, 2.0),
  `tests` next (1.5), `imports` weakest (1.0, a dependency without a
  behavioural link). Every edge is additionally scaled by how sure the
  extractor was (`exact` 1.0, `static_inferred` 0.7, `lexical` 0.4).
* **Deterministic two-level Louvain, no dependency, with a resolution
  parameter.** `_one_level` is the textbook Louvain local-moving phase
  (Blondel et al. 2008): nodes visited in a fixed sorted order, each
  considers moving into the community of a neighbour that maximises
  modularity gain, repeated until no node moves. Ties are broken by the
  lexicographically smallest candidate id and a node only moves on a
  strictly positive gain, so the same graph always produces the same
  partition. A resolution parameter (Reichardt & Bornholdt 2006) scales the
  null-model term of the gain; `_RESOLUTION_LEVEL0/1 < 1` deliberately biases
  toward fewer, larger communities than the textbook default, because the
  contract this module serves is "a human orienting in an unfamiliar repo",
  and a few dozen readable groups beat a few thousand technically-optimal
  but illegible ones. Level 0 comes from that phase over the (fold/split
  finalised) production file graph; level 1 re-runs it over those level-0
  communities aggregated into super-nodes.
* **Names come from paths, not from a symbol.** A community's name is the
  one or two directories (by symbol count, not just file count) that hold
  most of it, e.g. `"src/brain"` or `"src/brain + routes/brain_routes"`. If
  a community only touches a small slice of a directory (most of that
  directory lives in other communities), that piece is named by its
  standout file's own stem instead of the whole directory, so the name
  never implies ownership of files it does not have. Level-1 names use only
  the top-level directory segment ("dominant top-level areas"). When two
  sibling communities land on the exact same base name, a short
  TF-IDF-over-file-stem-tokens tag disambiguates them. `key_symbols` (the
  old, still useful "what's the most-called thing in here" signal) remains
  a separate field on every record.
* **A time/size budget, with an honest fallback.** `_TIME_BUDGET_S` bounds
  the wall clock spent inside Louvain; if exceeded, or the file-node count
  is absurd (`_NODE_CAP`), the whole computation is abandoned in favour of
  directory-based grouping (two path segments = level 0, one = level 1) so
  the tool never hangs a turn — the result says `"method": "directory"`.

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
import math
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

#: Resolution parameter (Reichardt & Bornholdt 2006) applied to the
#: null-model term of the Louvain modularity gain. 1.0 is the textbook
#: value. Empirically (see REPORT_CG.md) the textbook value already gives
#: this repo's ~2,500 production files a healthy level-0 spread (no
#: hundred-plus-file mega-blob); it is `_fold_min_files_for` below, not the
#: resolution, that does the real coarsening work needed to land in the
#: ~20-120 / ~6-25 community ranges a first-time reader can actually use.
_RESOLUTION_LEVEL0 = 1.0
_RESOLUTION_LEVEL1 = 1.0

_TOP_KEY_SYMBOLS = 6
_TOP_COUPLING = 5

#: A group under this many files never stands alone at BUILD time -- see
#: `_fold_tiny_groups`. This is what actually keeps the *count* of level-0/1
#: communities in a range a human can scan (real-repo numbers in
#: REPORT_CG.md) -- the Louvain resolution alone was not enough. **Scaled to
#: repo size** (`_fold_min_files_for`): a fixed value tuned on a ~2,500-file
#: repo folded away almost everything on a ~60-file one (nothing there ever
#: reached it) while doing nothing useful on a much bigger one. Kept
#: conceptually separate from `_DEFAULT_MIN_FILES` below (the *display*-time
#: floor `communities()`'s own `min_files` parameter defaults to, NOT
#: scaled) because a small workspace legitimately has communities under the
#: scaled build-time threshold with nothing sensible to fold them into --
#: folding backs off in that case (see `_fold_tiny_groups`) -- while the
#: display floor is the contract's literal "at least 3 files", independent
#: of repo size.
_FOLD_MIN_FILES_DIVISOR = 80.0
_FOLD_MIN_FILES_MIN = 2
_FOLD_MIN_FILES_MAX = 8
#: `communities()`'s default `min_files` display parameter (not scaled).
_DEFAULT_MIN_FILES = 3
#: A group at or past this many files, with cohesion below
#: `_CATCHALL_MAX_COHESION` OR holding more than `_CATCHALL_DOMINANCE_FRACTION`
#: of the whole repo's production files, is a catch-all -- see
#: `_split_catchalls`. The file-count floor is **scaled to repo size**
#: (`_catchall_min_files_for`) the same way and for the same reason as the
#: fold threshold above: a fixed 60-file floor can never fire on a 60-file
#: repo (nothing there can even reach it), where the actual problem this
#: round found was a single community holding *most* of the app (39 of 67
#: production files, cohesion 0.96 -- too internally consistent to be
#: "low-cohesion", but far too large a fraction of the repo to be one
#: module) -- hence the size-relative dominance trigger, independent of
#: cohesion, alongside the existing absolute-cohesion one.
_CATCHALL_MIN_FILES_FRACTION = 0.12
_CATCHALL_MIN_FILES_FLOOR = 15
_CATCHALL_MIN_FILES_CEIL = 60
_CATCHALL_MAX_COHESION = 0.30
_CATCHALL_DOMINANCE_FRACTION = 0.30
#: Resolution used to re-cluster a catch-all's own internal call/import
#: subgraph before ever falling back to a directory split (see
#: `_split_one_catchall`) -- deliberately far above `_RESOLUTION_LEVEL0` so
#: a package Louvain judged "one dense community" at the repo's own
#: resolution still gets pulled apart by its *internal* structure (the
#: files that call each other most within the blob, not across it).
_CATCHALL_INTERNAL_RESOLUTION = 4.0


def _fold_min_files_for(n_production_files: int) -> int:
    if n_production_files <= 0:
        return _FOLD_MIN_FILES_MIN
    return max(_FOLD_MIN_FILES_MIN, min(_FOLD_MIN_FILES_MAX,
               round(n_production_files / _FOLD_MIN_FILES_DIVISOR)))


def _catchall_min_files_for(n_production_files: int) -> int:
    return max(_CATCHALL_MIN_FILES_FLOOR, min(_CATCHALL_MIN_FILES_CEIL,
               round(n_production_files * _CATCHALL_MIN_FILES_FRACTION)))

_BUCKET_UNATTACHED_TESTS = "unattached_tests"
_BUCKET_LOOSE_FILES = "loose_files"

#: Bare names common enough (container/string builtins, generic verbs) that
#: `context_engine.code_index`'s bare-name call resolution ("exactly one
#: symbol with that name in this workspace" -- see its `_resolve` docstring)
#: can match a call site that meant `list.append`/`str.strip`/`round`/etc to
#: the one unrelated user function or method that happens to share the name,
#: in a codebase large enough to define one (verified on the real repo: a
#: TypeScript `round`/`any` under `studio/src/lib/**` was misresolved this
#: way). That is a resolution-quality limitation of the shared index, not
#: something this module can fix without touching file ownership outside
#: this lot -- but a name this generic is also never a good "what does this
#: community do" headline even on the rare occasion it IS a real call, so it
#: is excluded from `key_symbols` (never from clustering weight or
#: membership, which is unaffected; and no longer from naming at all, since
#: names are now path-based -- see the module docstring).
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

#: Tokens too generic (either as English filler or as code-file boilerplate)
#: to usefully distinguish two same-named sibling communities from each
#: other -- excluded from `_tfidf_tag` candidates.
_GENERIC_PATH_TOKENS = frozenset({
    "test", "tests", "util", "utils", "helper", "helpers", "service",
    "services", "index", "main", "init", "common", "base", "core", "mod",
    "module", "src", "lib", "app", "routes", "route", "api", "v1", "v2",
    # Generic UI/CRUD/status words: real English (or file-naming) filler
    # that shows up across unrelated screens/handlers and never actually
    # tells two communities apart -- e.g. every page in a client app has a
    # file named "*Page", every handler module has "handler" in its name.
    "page", "pages", "error", "errors", "check", "checks", "issue", "issues",
    "whats", "preview", "previews", "handler", "handlers",
})


class _BudgetExceeded(Exception):
    """Raised out of `_one_level` when the time budget is spent."""


# ── fingerprint / scope ─────────────────────────────────────────────────

#: Bumped whenever clustering or naming changes, so a cached result built by
#: older code is rebuilt instead of served (the index fingerprint alone does
#: not change when only this module does).
ALGO_VERSION = "4"


def _fingerprint(root: str, project_id: str) -> str:
    status = code_index.status(root, project_id=project_id)
    raw = (f"{ALGO_VERSION}|{status.get('last_indexed_at', '')}|"
           f"{status.get('symbols', 0)}|{status.get('edges', 0)}")
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


def _load_test_weighted_edges(conn: sqlite3.Connection, workspace: str, project_id: str,
                               inputs: _Inputs) -> Dict[str, Dict[str, float]]:
    """`test_path -> {production_path: summed_weight}` for every edge a test
    file makes into a production file -- never used for clustering, only to
    decide which single production community a test file is attached to
    (see the module docstring's test-attachment rule)."""
    marks = ",".join("?" * len(_EDGE_KINDS_FOR_GRAPH))
    clauses = ["workspace = ?"]
    params: List[Any] = [workspace]
    if project_id:
        clauses.append("project_id = ?")
        params.append(project_id)
    clauses.append(f"kind IN ({marks})")
    params.extend(_EDGE_KINDS_FOR_GRAPH)
    sql = f"SELECT src, dst, kind, certainty FROM code_edges WHERE {' AND '.join(clauses)}"

    out: Dict[str, Dict[str, float]] = {}
    for row in conn.execute(sql, params):
        src_path = inputs.sym_path.get(str(row["src"]))
        dst_path = inputs.sym_path.get(str(row["dst"]))
        if not src_path or not dst_path or src_path == dst_path:
            continue
        if not _is_test_path(src_path) or _is_test_path(dst_path):
            continue  # only test -> production edges decide attachment
        weight = _KIND_WEIGHT.get(str(row["kind"]), 0.0) * \
            _CERTAINTY_WEIGHT.get(str(row["certainty"]), 0.3)
        if weight <= 0:
            continue
        bucket = out.setdefault(src_path, {})
        bucket[dst_path] = bucket.get(dst_path, 0.0) + weight
    return out


def _build_file_graph(conn: sqlite3.Connection, workspace: str, project_id: str,
                       inputs: _Inputs) -> Dict[Tuple[str, str], float]:
    """One weighted, undirected entry per unordered *production* file pair
    with at least one qualifying edge between them -- test files are never
    nodes in this graph at all (see the module docstring); their edges are
    loaded separately by `_load_test_weighted_edges` for post-hoc
    attachment instead."""
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
        if _is_test_path(src_path) or _is_test_path(dst_path):
            continue  # test files are not clustering nodes
        weight = _KIND_WEIGHT.get(str(row["kind"]), 0.0) * \
            _CERTAINTY_WEIGHT.get(str(row["certainty"]), 0.3)
        if weight <= 0:
            continue
        key = (src_path, dst_path) if src_path < dst_path else (dst_path, src_path)
        pair_weight[key] = pair_weight.get(key, 0.0) + weight
    return pair_weight


def _build_adj(pair_weight: Dict[Tuple[str, str], float],
                nodes: Sequence[str]) -> Dict[str, Dict[str, float]]:
    adj: Dict[str, Dict[str, float]] = {p: {} for p in nodes}
    for (a, b), w in pair_weight.items():
        adj.setdefault(a, {})
        adj.setdefault(b, {})
        adj[a][b] = adj[a].get(b, 0.0) + w
        adj[b][a] = adj[b].get(a, 0.0) + w
    return adj


# ── JS/TS relative-import edges (code_index does not extract these) ────

#: `code_index._extract_lexical` (used for every non-Python language) never
#: emits `imports`/`calls` edges for JS/JSX/TS/TSX -- only `defines` edges
#: from a module to its own lexically-found symbols (see that function's
#: docstring in `context_engine/code_index.py`). For a client app this means
#: the file-level clustering graph normally has ZERO real edges between its
#: own files, so every JS/TS community lands at cohesion 0.0 regardless of
#: how tightly the screens/components actually depend on each other. Rather
#: than touch `code_index`/`code_edges` (out of this module's scope, and a
#: much bigger change), `communities.py` derives its own best-effort,
#: file-level "imports" edge: a cheap regex over each file's own
#: `import ... from './x'` / `export ... from './x'` / `require('./x')`
#: relative specifiers, resolved to another production file the same way
#: Node/bundler resolution would (trying the usual JS/TS extensions and
#: `index.<ext>`). This is intentionally NOT a real module resolver -- no
#: tsconfig paths/aliases, no node_modules, no bare/package specifiers (only
#: `./x` / `../x` are ever considered local) -- just enough real signal for
#: client code to cluster by screens/components/lib instead of by nothing.
#: It contributes to THIS module's clustering graph only; it is never
#: written back to the database and never affects `impact()`, `flows()`, or
#: any other consumer of the real code graph.
_JS_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
_JS_RESOLVE_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".json")
_JS_MAX_FILE_BYTES = 512_000
_JS_IMPORT_RE = re.compile(r"""(?:from\s+|require\(\s*)['"](\.\.?/[^'"]*)['"]""")


def _js_relative_imports(source: str) -> List[str]:
    """Every relative import/require specifier (`./x`, `../y/z`) found in
    `source`. Deliberately simple: only specifiers starting with `.` or
    `..` are considered -- bare/package specifiers (`react`, `@scope/pkg`)
    are never local files and would only add noise or false edges to
    unrelated files that happen to share a resolved name."""
    return _JS_IMPORT_RE.findall(source)


def _resolve_js_specifier(from_file: str, spec: str, known: Set[str]) -> Optional[str]:
    """Resolve a relative specifier written inside `from_file` to one of
    `known` (production file paths, forward-slash, relative to the repo
    root) -- the same three things Node/bundler resolution tries: the exact
    joined path, that path plus each known extension, and `index.<ext>`
    inside it as a directory. Returns None (no match, e.g. the target is
    outside the indexed/production set, or resolution genuinely can't find
    it) rather than guessing."""
    base_dir = from_file.rsplit("/", 1)[0] if "/" in from_file else ""
    parts = (base_dir.split("/") if base_dir else []) + spec.split("/")
    resolved: List[str] = []
    for part in parts:
        if part in ("", "."):
            continue
        if part == "..":
            if resolved:
                resolved.pop()
            continue
        resolved.append(part)
    candidate = "/".join(resolved)
    if candidate in known:
        return candidate
    for ext in _JS_RESOLVE_EXTENSIONS:
        if candidate + ext in known:
            return candidate + ext
    for ext in _JS_RESOLVE_EXTENSIONS:
        idx = f"{candidate}/index{ext}" if candidate else f"index{ext}"
        if idx in known:
            return idx
    return None


def _derive_js_import_edges(root: str, production_files: Sequence[str]
                             ) -> Dict[Tuple[str, str], float]:
    """Best-effort file-level `pair_weight` contributions for JS/TS relative
    imports -- see the module note above `_JS_EXTENSIONS`. Reads each JS/TS
    production file directly off disk (capped at `_JS_MAX_FILE_BYTES`); a
    file that cannot be read (missing, permissions, too large) is silently
    skipped, same as a missing edge from the real graph would be. Weight
    reuses the existing `imports` x `lexical` formula (0.4) -- the same
    confidence code_index itself would assign a lexically-found, unresolved
    import, since that is exactly what this is."""
    known = set(production_files)
    js_files = [f for f in production_files if f.endswith(_JS_EXTENSIONS)]
    if not js_files:
        return {}
    weight = _KIND_WEIGHT["imports"] * _CERTAINTY_WEIGHT["lexical"]
    out: Dict[Tuple[str, str], float] = {}
    for f in js_files:
        abs_path = os.path.join(root, *f.split("/"))
        try:
            with open(abs_path, "rb") as fh:
                raw = fh.read(_JS_MAX_FILE_BYTES + 1)
        except OSError:
            continue
        if len(raw) > _JS_MAX_FILE_BYTES:
            continue
        source = raw.decode("utf-8", "replace")
        for spec in _js_relative_imports(source):
            target = _resolve_js_specifier(f, spec, known)
            if target is None or target == f:
                continue
            key = (f, target) if f < target else (target, f)
            out[key] = out.get(key, 0.0) + weight
    return out


# ── deterministic Louvain (one level) ───────────────────────────────────

def _one_level(adj: Dict[str, Dict[str, float]], order: Sequence[str],
                deadline: float, *, resolution: float = 1.0) -> Dict[str, str]:
    """One Louvain local-moving phase: fixed node order, ties broken by the
    smallest candidate id, a move only on a strictly positive gain -- the
    same graph and order always produce the same partition. `resolution`
    scales the null-model term (see the module docstring)."""
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
            best_gain = neigh_w.get(cur, 0.0) - resolution * sigma_tot.get(cur, 0.0) * kn / m2
            for c in sorted(neigh_w):
                if c == cur:
                    continue
                gain = neigh_w[c] - resolution * sigma_tot.get(c, 0.0) * kn / m2
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
    self-loop -- exactly the convention multi-level Louvain relies on."""
    agg: Dict[str, Dict[str, float]] = {c: {} for c in set(membership.values())}
    for n, neighbours in adj.items():
        cn = membership.get(n)
        if cn is None:
            continue
        for nb, w in neighbours.items():
            cnb = membership.get(nb)
            if cnb is None:
                continue
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


def _internal_cross_weights(groups: Dict[str, List[str]],
                             pair_weight: Dict[Tuple[str, str], float]
                             ) -> Tuple[Dict[str, float], Dict[str, Dict[str, float]]]:
    file_to_group = {f: gid for gid, files in groups.items() for f in files}
    internal: Dict[str, float] = {}
    cross: Dict[str, Dict[str, float]] = {}
    for (a, b), w in pair_weight.items():
        ga, gb = file_to_group.get(a), file_to_group.get(b)
        if ga is None or gb is None:
            continue
        if ga == gb:
            internal[ga] = internal.get(ga, 0.0) + w
        else:
            cross.setdefault(ga, {})[gb] = cross.setdefault(ga, {}).get(gb, 0.0) + w
            cross.setdefault(gb, {})[ga] = cross.setdefault(gb, {}).get(ga, 0.0) + w
    return internal, cross


def _cohesion_of(gid: str, internal: Dict[str, float], cross: Dict[str, Dict[str, float]]) -> float:
    own_internal = internal.get(gid, 0.0)
    own_cross = sum(cross.get(gid, {}).values())
    total = own_internal + own_cross
    return (own_internal / total) if total > 0 else 0.0


# ── fold tiny groups / split catch-alls ─────────────────────────────────

def _dir_key(path: str, depth: Optional[int] = None) -> str:
    parts = path.rsplit("/", 1)[0].split("/") if "/" in path else []
    if depth is not None:
        parts = parts[:depth]
    return "/".join(parts) or "."


def _fold_tiny_groups(groups: Dict[str, List[str]], pair_weight: Dict[Tuple[str, str], float],
                       *, min_files: int) -> Dict[str, List[str]]:
    """A group under `min_files` files never stands alone. It merges into,
    in order: (a) the other group it has the strongest real edge weight to,
    (b) the group holding a majority of the *other* files in the same
    directory (using the ORIGINAL, pre-fold assignment, snapshotted once so
    folding decisions never chain/order-depend), or (c) a per-top-level-
    directory "loose files" bucket. If nothing in the repo reaches
    `min_files` at all (a tiny fixture/workspace), folding is skipped
    entirely rather than dumping everything into "loose" buckets."""
    big = {gid for gid, files in groups.items() if len(files) >= min_files}
    if not big:
        return {gid: list(files) for gid, files in groups.items()}
    tiny = sorted(gid for gid in groups if gid not in big)
    if not tiny:
        return {gid: list(files) for gid, files in groups.items()}

    file_to_group = {f: gid for gid, files in groups.items() for f in files}
    _, cross = _internal_cross_weights(groups, pair_weight)

    dir_counts: Dict[str, Dict[str, int]] = {}
    for f, gid in file_to_group.items():
        d = _dir_key(f)
        dir_counts.setdefault(d, {})[gid] = dir_counts.setdefault(d, {}).get(gid, 0) + 1

    new_groups: Dict[str, List[str]] = {gid: list(files) for gid, files in groups.items() if gid in big}
    loose: Dict[str, List[str]] = {}

    for gid in tiny:
        files = groups[gid]
        candidates = {g: w for g, w in cross.get(gid, {}).items() if g in big}
        target = sorted(candidates.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] if candidates else None
        if target is None:
            dir_votes: Dict[str, int] = {}
            for f in files:
                d = _dir_key(f)
                for g, cnt in dir_counts.get(d, {}).items():
                    if g == gid or g not in big:
                        continue
                    dir_votes[g] = dir_votes.get(g, 0) + cnt
            if dir_votes:
                target = sorted(dir_votes.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        if target is not None:
            new_groups[target].extend(files)
            continue
        for f in files:
            top = f.split("/", 1)[0] if "/" in f else "."
            loose.setdefault(top, []).append(f)

    for top, files in loose.items():
        new_groups[f"__loose__{top}"] = files
    for files in new_groups.values():
        files.sort()
    return new_groups


def _fold_siblings(sub: Dict[str, List[str]], *, min_files: int) -> Dict[str, List[str]]:
    """Same idea as `_fold_tiny_groups`, scoped to one catch-all's own
    directory-split sub-groups: a too-small sub-group merges into a sibling
    sharing its top-level segment, else the largest sibling overall."""
    big = {k: v for k, v in sub.items() if len(v) >= min_files}
    if not big:
        return {k: list(v) for k, v in sub.items()}
    result: Dict[str, List[str]] = {k: list(v) for k, v in big.items()}
    for k, files in sub.items():
        if k in big:
            continue
        top = k.split("/", 1)[0]
        target = next((bk for bk in sorted(big) if bk == top or bk.startswith(top + "/")), None)
        if target is None:
            target = sorted(big.items(), key=lambda kv: (-len(kv[1]), kv[0]))[0][0]
        result[target].extend(files)
    for v in result.values():
        v.sort()
    return result


def _split_one_catchall(files: List[str], pair_weight: Dict[Tuple[str, str], float],
                         *, min_files: int, deadline: float
                         ) -> Tuple[Optional[Dict[str, List[str]]], str]:
    """Split one catch-all's file list into sub-groups, preferring the blob's
    own internal structure over its directory layout. A dense, cohesive blob
    (e.g. one vendored library or one tightly-coupled package) usually has
    real substructure in its OWN call graph -- distinct file clusters that
    Louvain's top-level resolution was too coarse to tell apart from the rest
    of the repo but that a second Louvain pass, run only on this blob's
    induced subgraph at a much higher resolution (`_CATCHALL_INTERNAL_RESOLUTION`),
    can separate. That is tried first (`split_by="call_graph"`); only when it
    fails to produce at least two usable sub-groups (every internal edge is
    genuinely uniform, or the blob has no internal edges at all) do we fall
    back to splitting by each file's own immediate directory
    (`split_by="directory"`) -- a fixed absolute depth would collapse a
    deeply-nested blob like `studio/src/screens/**` right back to
    `studio/src` for every file, so splitting on the file's own full
    directory keeps whatever structure the blob actually has, however deep
    it starts. Sub-groups that come out too small are folded into a sibling,
    same rule as `_fold_tiny_groups`. Returns `(None, "")` when neither
    approach yields at least two sub-groups meeting `min_files` -- e.g. every
    file in the blob is a direct sibling in one flat directory with no
    internal edges and no subdirectory structure, so splitting genuinely
    cannot help and the caller should leave the blob as one community."""
    file_set = set(files)
    internal_pw = {(a, b): w for (a, b), w in pair_weight.items()
                   if a in file_set and b in file_set}
    if internal_pw:
        adj = _build_adj(internal_pw, files)
        try:
            membership = _one_level(adj, sorted(files), deadline,
                                     resolution=_CATCHALL_INTERNAL_RESOLUTION)
        except _BudgetExceeded:
            membership = None
        if membership is not None:
            sub = _group_by(membership)
            if len(sub) > 1:
                sub = _fold_siblings_by_weight(sub, internal_pw, min_files=min_files)
                if len(sub) > 1:
                    return sub, "call_graph"

    sub = {}
    for f in files:
        sub.setdefault(_dir_key(f), []).append(f)
    sub = _fold_siblings(sub, min_files=min_files)
    if len(sub) > 1:
        return sub, "directory"
    return None, ""


def _fold_siblings_by_weight(sub: Dict[str, List[str]], pair_weight: Dict[Tuple[str, str], float],
                              *, min_files: int) -> Dict[str, List[str]]:
    """Same idea as `_fold_siblings`, but for call-graph sub-groups (which
    have no directory-sibling relationship to lean on): a too-small sub-group
    merges into whichever other sub-group it has the strongest combined edge
    weight to, else the largest sub-group overall."""
    big = {k: v for k, v in sub.items() if len(v) >= min_files}
    if not big:
        return {k: list(v) for k, v in sub.items()}
    result: Dict[str, List[str]] = {k: list(v) for k, v in big.items()}
    _, cross = _internal_cross_weights(sub, pair_weight)
    for k, files in sub.items():
        if k in big:
            continue
        candidates = {g: w for g, w in cross.get(k, {}).items() if g in big}
        target = sorted(candidates.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] if candidates \
            else sorted(big.items(), key=lambda kv: (-len(kv[1]), kv[0]))[0][0]
        result[target].extend(files)
    for v in result.values():
        v.sort()
    return result


def _split_catchalls(groups: Dict[str, List[str]], pair_weight: Dict[Tuple[str, str], float],
                      *, min_files: int, catchall_min_files: int, total_files: int,
                      deadline: float) -> Tuple[Dict[str, List[str]], Dict[str, str]]:
    """A group past `catchall_min_files` files is Louvain's honest answer for
    a loosely-coupled "glue" region or an outsized dense blob, not a
    reasonably-sized single module -- split it into real sub-communities
    instead of presenting one illegible catch-all. Two independent triggers,
    either sufficient on its own:
      - cohesion below `_CATCHALL_MAX_COHESION` (the classic "glue" case: a
        big loosely-coupled group whose internal edges barely outweigh its
        cross edges), or
      - the group holds more than `_CATCHALL_DOMINANCE_FRACTION` of the
        WHOLE repo's production files (the "one dense blob" case a small
        cohesive package produces: cohesion can be very high, ~1.0, yet the
        group is still most of the repo and tells a human nothing about its
        internal structure).
    See `_split_one_catchall` for how the split itself is attempted (call
    graph first, directory fallback)."""
    internal, cross = _internal_cross_weights(groups, pair_weight)
    final: Dict[str, List[str]] = {}
    split_of: Dict[str, str] = {}
    for gid, files in groups.items():
        cohesion = _cohesion_of(gid, internal, cross)
        dominant = total_files > 0 and (len(files) / total_files) > _CATCHALL_DOMINANCE_FRACTION
        if len(files) > catchall_min_files and (cohesion < _CATCHALL_MAX_COHESION or dominant):
            sub, split_by = _split_one_catchall(files, pair_weight, min_files=min_files,
                                                 deadline=deadline)
            if sub is None:
                final[gid] = files  # splitting would not actually help
                continue
            for skey, sfiles in sub.items():
                label = f"{gid}::{skey}"
                final[label] = sfiles
                split_of[label] = split_by
        else:
            final[gid] = files
    return final, split_of


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


# ── test attachment ─────────────────────────────────────────────────────

def _assign_tests(test_weighted: Dict[str, Dict[str, float]], file_to_id0: Dict[str, str],
                   test_paths: Sequence[str]) -> Tuple[Dict[str, Set[str]], List[str]]:
    """Every test file is attached to the *one* production community
    (post fold/split, so ids are final) it exercises most, by summed edge
    weight; a test file with no production edge at all is unattached."""
    test_files_by_id0: Dict[str, Set[str]] = {}
    unattached: List[str] = []
    for tpath in sorted(test_paths):
        group_weight: Dict[str, float] = {}
        for prod_path, w in test_weighted.get(tpath, {}).items():
            gid = file_to_id0.get(prod_path)
            if not gid:
                continue
            group_weight[gid] = group_weight.get(gid, 0.0) + w
        if not group_weight:
            unattached.append(tpath)
            continue
        best_gid = sorted(group_weight.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        test_files_by_id0.setdefault(best_gid, set()).add(tpath)
    return test_files_by_id0, unattached


# ── naming: paths, not symbols ───────────────────────────────────────────

_TOKEN_SPLIT_RE = re.compile(r"[_\-\s]+")
_CAMEL_RE = re.compile(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])")
_EXT_RE = re.compile(r"\.(py|ts|tsx|js|jsx|mjs|go|rs|java|rb)$")


def _tokenize_stem(path: str) -> List[str]:
    stem = path.rsplit("/", 1)[-1]
    stem = _EXT_RE.sub("", stem)
    tokens: List[str] = []
    for part in _TOKEN_SPLIT_RE.split(stem):
        if not part:
            continue
        tokens.extend(m.lower() for m in _CAMEL_RE.findall(part))
    return [t for t in tokens if len(t) > 1]


class _NameCtx:
    """Precomputed, repo-wide inputs the naming/tagging functions need --
    built once per `_build` call, never per community (that would be
    O(communities x files) again)."""
    __slots__ = ("path_symbols", "dir_file_totals", "file_tokens", "doc_freq", "n_docs",
                 "level1_depth")

    def __init__(self, path_symbols: Dict[str, int], production_files: Sequence[str]) -> None:
        self.path_symbols = path_symbols
        dir_file_totals: Dict[str, int] = {}
        file_tokens: Dict[str, Set[str]] = {}
        doc_freq: Dict[str, int] = {}
        for f in production_files:
            dir_file_totals[_dir_key(f)] = dir_file_totals.get(_dir_key(f), 0) + 1
            toks = set(_tokenize_stem(f))
            file_tokens[f] = toks
            for t in toks:
                doc_freq[t] = doc_freq.get(t, 0) + 1
        self.dir_file_totals = dir_file_totals
        self.file_tokens = file_tokens
        self.doc_freq = doc_freq
        self.n_docs = len(production_files) or 1
        self.level1_depth = _level1_depth(production_files)


def _level1_depth(production_files: Sequence[str]) -> int:
    """Directory depth level-1 names are cut at. Normally the top-level area
    (`src`, `studio`, `routes`). A repo whose code lives almost entirely
    under one top-level package (`myapp/...`) would then name every level-1
    community after that same package -- seen on a ~60-file app: "myapp
    (ask, backend)", "myapp (comfy, faustus)" -- so the cut moves one level
    down while a single directory still holds 80% of the files and the next
    level down actually tells directories apart."""
    depth = 1
    n = len(production_files)
    while n and depth < 4:
        counts: Dict[str, int] = {}
        for f in production_files:
            key = _dir_key(f, depth)
            counts[key] = counts.get(key, 0) + 1
        if max(counts.values()) < 0.8 * n:
            break
        if len({_dir_key(f, depth + 1) for f in production_files}) <= len(counts):
            break
        depth += 1
    return depth


def _name_for(files: Sequence[str], ctx: _NameCtx, *, level1: bool = False) -> str:
    depth = ctx.level1_depth if level1 else None
    dir_symbols: Dict[str, int] = {}
    dir_files: Dict[str, List[str]] = {}
    for f in files:
        key = _dir_key(f, depth)
        dir_symbols[key] = dir_symbols.get(key, 0) + ctx.path_symbols.get(f, 0)
        dir_files.setdefault(key, []).append(f)
    total = sum(dir_symbols.values()) or 1
    ranked = sorted(dir_symbols.items(), key=lambda kv: (-kv[1], kv[0]))

    pieces: List[str] = []
    covered = 0.0
    for key, weight in ranked:
        if len(pieces) >= 2:
            break
        frac = weight / total
        if pieces and frac < 0.12:
            break
        piece = key
        if not level1:
            repo_total = ctx.dir_file_totals.get(key, len(dir_files[key]))
            here = len(dir_files[key])
            frac_of_dir = (here / repo_total) if repo_total else 1.0
            # A "flat" directory key (no subdirectory segment of its own,
            # e.g. "src", "routes") holding hundreds of loose files is not
            # a meaningful name by itself even when this community owns a
            # few dozen of them -- so the minority-of-directory threshold is
            # more forgiving there than for a real, named subdirectory.
            is_flat = "/" not in key
            minority = frac_of_dir < (0.5 if is_flat else 0.34)
            if repo_total > here and minority:
                top_files = sorted(dir_files[key],
                                   key=lambda f: (-ctx.path_symbols.get(f, 0), f))[:2]
                stems: List[str] = []
                for bf in top_files:
                    stem = _EXT_RE.sub("", bf.rsplit("/", 1)[-1])
                    file_dir = bf.rsplit("/", 1)[0] if "/" in bf else ""
                    piece_str = f"{file_dir}/{stem}" if file_dir else stem
                    if piece_str not in stems:
                        stems.append(piece_str)
                piece = " · ".join(stems)
        pieces.append(piece)
        covered += frac
        if covered >= 0.75:
            break
    if not pieces:
        return files[0].rsplit("/", 1)[-1] if files else "community"
    return " + ".join(pieces)


def _tfidf_tag(files: Sequence[str], ctx: _NameCtx, exclude: Set[str]) -> str:
    """Short disambiguator for two same-named sibling communities: the
    file-stem tokens most distinctive to this community's files versus the
    whole repo (classic TF-IDF), skipping generic/boilerplate tokens and
    anything already present in the name itself.

    A token that appears in only ONE member file's stem is a weak
    disambiguator in practice -- it usually reads as noise picked off a
    single filename (e.g. "handler" from one `error_handler.py`) rather than
    a real theme shared across the community. So tokens with a
    within-community document frequency of at least 2 (the token's stem
    appears in >=2 of this community's own files) are preferred outright;
    count-1 tokens are only used as a fallback when no token clears that
    bar, so a genuinely small/singleton-flavoured community still gets a
    tag rather than none at all."""
    tf: Dict[str, int] = {}
    for f in files:
        for t in ctx.file_tokens.get(f, ()):
            tf[t] = tf.get(t, 0) + 1
    n_files = len(files) or 1
    scored: List[Tuple[float, str]] = []
    for t, c in tf.items():
        if t in exclude or t in _GENERIC_PATH_TOKENS:
            continue
        idf = math.log((ctx.n_docs + 1) / (ctx.doc_freq.get(t, 0) + 1)) + 1.0
        score = (c / n_files) * idf
        scored.append((score, t, c))
    scored.sort(key=lambda x: (-x[0], x[1]))
    qualified = [(s, t) for s, t, c in scored if c >= 2]
    chosen = qualified if qualified else [(s, t) for s, t, c in scored]
    return ", ".join(t for _, t in chosen[:2])


def _disambiguate_names(records: List[Dict[str, Any]], ctx: _NameCtx) -> None:
    """Sibling communities (same level) that landed on the exact same
    base name get a short TF-IDF tag appended, in place."""
    by_name: Dict[str, List[Dict[str, Any]]] = {}
    for r in records:
        by_name.setdefault(r["name"], []).append(r)
    for name, group in by_name.items():
        if len(group) < 2:
            continue
        # A coarse (level-1) name cut at the top directory collides whenever
        # the code lives under one package. The full-depth path names tell
        # such siblings apart better than file-stem tags ("app/hoard_link"
        # beats "app (comfy, faustus)"); tags remain for what paths can't.
        deeper = [_name_for(r["files"], ctx, level1=False) for r in group]
        if len(set(deeper)) == len(group) and all(d != name for d in deeper):
            for r, d in zip(group, deeper):
                r["name"] = d
            continue
        exclude = set(re.split(r"[\s+/·]+", name.lower()))
        for r in group:
            tag = _tfidf_tag(r["files"], ctx, exclude)
            if tag:
                r["name"] = f"{name} ({tag})"


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
    """Still used by `_maybe_summarize`'s prompt (a rough one-line location
    hint for the model) -- naming itself no longer uses this, see
    `_name_for`."""
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
                    test_files_map: Dict[str, Set[str]], method: str,
                    computed_at: str, split_of: Dict[str, str], ctx: _NameCtx
                    ) -> List[Dict[str, Any]]:
    """Every field the contract asks for, for one level's groups.

    `groups`: community id -> sorted member (production) files. `parents`:
    id -> parent id (empty for level 1). `test_files_map`: community id ->
    the test files attached to it (see `_assign_tests`). `split_of`:
    community id -> `"directory"` when it is a catch-all split's
    sub-community, else absent/empty."""
    files_of_community: Dict[str, str] = {f: cid for cid, files in groups.items() for f in files}
    internal, cross = _internal_cross_weights(groups, pair_weight)

    # key symbols: internal call fan-in, one pass over resolved `calls` edges.
    fanin: Dict[str, int] = {}
    for src, dst in calls_edges:
        src_path, dst_path = inputs.sym_path.get(src), inputs.sym_path.get(dst)
        if not src_path or not dst_path:
            continue
        if files_of_community.get(src_path) == files_of_community.get(dst_path) \
                and files_of_community.get(dst_path) is not None:
            fanin[dst] = fanin.get(dst, 0) + 1

    # Symbol ids grouped by file, built ONCE -- see the perf note in git
    # history: a per-community scan of the whole symbol table is
    # O(communities x total_symbols), tens of seconds on a real repo.
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

        test_files = sorted(test_files_map.get(cid, ()))

        cohesion = round(_cohesion_of(cid, internal, cross), 4)

        coupling = [
            {"community_id": other, "weight": round(w, 4)}
            for other, w in sorted(cross.get(cid, {}).items(), key=lambda kv: (-kv[1], kv[0]))
        ][:_TOP_COUPLING]

        name = _name_for(files, ctx, level1=(level >= 1))
        purpose = _purpose_for(files, routes, entry_points, key_symbols,
                               [inputs.path_lang.get(f, "") for f in files])

        records.append({
            "id": cid, "level": level, "parent": parents.get(cid, ""),
            "method": method, "name": name, "purpose": purpose, "summary": "",
            "size": size, "files": files, "dominant_language": dominant_language,
            "cohesion": cohesion, "key_symbols": key_symbols, "routes": routes,
            "entry_points": entry_points, "test_files": test_files,
            "coupling": coupling, "computed_at": computed_at,
            "split_by": split_of.get(cid, ""), "bucket": "",
        })

    _disambiguate_names(records, ctx)
    return records


def _unattached_tests_record(level: int, files: Sequence[str], computed_at: str,
                             method: str) -> Dict[str, Any]:
    cid = _community_id(list(files)) + f"_lvl{level}"
    return {
        "id": cid, "level": level, "parent": cid, "method": method,
        "name": "Unattached tests", "purpose": f"{len(files)} test file(s) with no "
        "resolved edge into any production file.", "summary": "", "size": 0,
        "files": sorted(files), "dominant_language": "", "cohesion": 0.0,
        "key_symbols": [], "routes": [], "entry_points": [], "test_files": [],
        "coupling": [], "computed_at": computed_at, "split_by": "",
        "bucket": _BUCKET_UNATTACHED_TESTS,
    }


# ── persistence ──────────────────────────────────────────────────────────

_RECORD_KEYS = ("files", "key_symbols", "routes", "entry_points", "test_files", "coupling",
               "split_by", "bucket")
_RECORD_DEFAULTS: Dict[str, Any] = {
    "files": [], "key_symbols": [], "routes": [], "entry_points": [], "test_files": [],
    "coupling": [], "split_by": "", "bucket": "",
}


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
                    for f in rec.get("test_files", ()):
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
        rec[key] = data.get(key, _RECORD_DEFAULTS[key])
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
        test_weighted = _load_test_weighted_edges(conn, root, project_id, inputs)

    production_files = [f for f in inputs.paths if not _is_test_path(f)]
    test_paths = [f for f in inputs.paths if _is_test_path(f)]
    deadline = started + _TIME_BUDGET_S
    fold_min = _fold_min_files_for(len(production_files))
    catchall_min = _catchall_min_files_for(len(production_files))

    js_edges = _derive_js_import_edges(root, production_files)
    for key, w in js_edges.items():
        pair_weight[key] = pair_weight.get(key, 0.0) + w

    if len(production_files) > _NODE_CAP:
        file_to_label0, label0_to_label1_dir, method = _directory_fallback(production_files)
        groups0_final = _group_by(file_to_label0)
        split_of: Dict[str, str] = {}
        groups1_final = {}
        for id0, files in groups0_final.items():
            id1 = label0_to_label1_dir.get(id0, id0)
            groups1_final.setdefault(id1, []).extend(files)
        for v in groups1_final.values():
            v.sort()
        label_to_id0 = {gid: gid for gid in groups0_final}  # already stable ids
        label_to_id1 = {gid: gid for gid in groups1_final}
        parents = {id0: label0_to_label1_dir.get(id0, id0) for id0 in groups0_final}
    else:
        adj = _build_adj(pair_weight, production_files)
        order = sorted(production_files)
        try:
            labels0 = _one_level(adj, order, deadline, resolution=_RESOLUTION_LEVEL0)
            method = "louvain"
        except _BudgetExceeded:
            logger.info("code_graph.communities: Louvain exceeded its time budget "
                       "(%d files) -- falling back to directory grouping", len(production_files))
            file_to_label0, label0_to_label1_dir, method = _directory_fallback(production_files)
            labels0 = file_to_label0

        if method == "directory":
            groups0_final = _group_by(labels0)
            split_of = {}
            groups1_final = {}
            for id0, files in groups0_final.items():
                id1 = label0_to_label1_dir.get(id0, id0)
                groups1_final.setdefault(id1, []).extend(files)
            for v in groups1_final.values():
                v.sort()
            label_to_id0 = {gid: gid for gid in groups0_final}
            label_to_id1 = {gid: gid for gid in groups1_final}
            parents = {id0: label0_to_label1_dir.get(id0, id0) for id0 in groups0_final}
        else:
            groups0_raw = _group_by(labels0)
            groups0_folded = _fold_tiny_groups(groups0_raw, pair_weight, min_files=fold_min)
            groups0_labelled, split_of = _split_catchalls(
                groups0_folded, pair_weight, min_files=fold_min,
                catchall_min_files=catchall_min, total_files=len(production_files),
                deadline=deadline)
            file_to_label0 = {f: lbl for lbl, files in groups0_labelled.items() for f in files}

            try:
                agg = _aggregate(adj, file_to_label0)
                label1_of_label0 = _one_level(agg, sorted(agg), deadline,
                                              resolution=_RESOLUTION_LEVEL1)
            except _BudgetExceeded:
                label1_of_label0 = {lbl: lbl for lbl in groups0_labelled}

            groups1_raw: Dict[str, List[str]] = {}
            for lbl0, files in groups0_labelled.items():
                lbl1 = label1_of_label0.get(lbl0, lbl0)
                groups1_raw.setdefault(lbl1, []).extend(files)
            for v in groups1_raw.values():
                v.sort()
            groups1_labelled = _fold_tiny_groups(groups1_raw, pair_weight, min_files=fold_min)

            label_to_id0 = {lbl: _community_id(files) for lbl, files in groups0_labelled.items()}
            label_to_id1 = {lbl: _community_id(files) for lbl, files in groups1_labelled.items()}

            groups0_final = {label_to_id0[lbl]: files for lbl, files in groups0_labelled.items()}
            groups1_final = {label_to_id1[lbl]: files for lbl, files in groups1_labelled.items()}
            split_of = {label_to_id0[lbl]: flag for lbl, flag in split_of.items()}

            id1_of_file = {f: label_to_id1[lbl1] for lbl1, files in groups1_labelled.items()
                          for f in files}
            parents = {}
            for id0, files in groups0_final.items():
                votes: Dict[str, int] = {}
                for f in files:
                    v = id1_of_file.get(f)
                    if v:
                        votes[v] = votes.get(v, 0) + 1
                parents[id0] = sorted(votes.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] \
                    if votes else ""

    file_to_id0 = {f: gid for gid, files in groups0_final.items() for f in files}
    test_files_by_id0, unattached = _assign_tests(test_weighted, file_to_id0, test_paths)

    # test_files at level 1 = union of the test_files of its level-0 children.
    test_files_by_id1: Dict[str, Set[str]] = {}
    for id0, tests in test_files_by_id0.items():
        id1 = parents.get(id0, "")
        if id1:
            test_files_by_id1.setdefault(id1, set()).update(tests)

    ctx = _NameCtx(inputs.path_symbols, production_files)
    computed_at = ce_store.now_iso()
    records0 = _build_records(0, groups0_final, parents, inputs, pair_weight, calls_edges,
                              test_files_by_id0, method, computed_at, split_of, ctx)
    records1 = _build_records(1, groups1_final, {}, inputs, pair_weight, calls_edges,
                              test_files_by_id1, method, computed_at, {}, ctx)

    if unattached:
        records0.append(_unattached_tests_record(0, unattached, computed_at, method))
        records1.append(_unattached_tests_record(1, unattached, computed_at, method))

    all_records = records0 + records1
    _persist(root, project_id, fingerprint, all_records)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    logger.info("code_graph.communities: built %d level-0 / %d level-1 communities "
               "(%s, %d production files, %d test files, %d unattached) in %dms",
               len(records0), len(records1), method, len(production_files),
               len(test_paths), len(unattached), elapsed_ms)
    for rec in all_records:
        rec["_elapsed_ms"] = elapsed_ms
    return all_records


# ── rendering ────────────────────────────────────────────────────────────

def _render_list(records: List[Dict[str, Any]], output_chars: Optional[int], *,
                  hidden_count: int = 0, unattached_count: int = 0) -> str:
    lines = []
    for r in sorted(records, key=lambda x: (-x["size"], x["id"])):
        tag = "  [split_by=directory]" if r.get("split_by") else ""
        lines.append(
            f"{r['id']}  {r['name']}  ({r['size']} symbols, {len(r['files'])} files, "
            f"cohesion={r['cohesion']:.2f}){tag}")
    if hidden_count:
        lines.append(f"... {hidden_count} smaller communit{'y' if hidden_count == 1 else 'ies'} "
                     f"folded/hidden below the file-count floor")
    if unattached_count:
        lines.append(f"({unattached_count} test file(s) with no production edge -- "
                     f"see communities(include_unattached=True))")
    return _clip("\n".join(lines) or "(no communities)", output_chars)


def _render_detail(r: Dict[str, Any], output_chars: Optional[int]) -> str:
    lines = [
        f"{r['id']}  {r['name']}  [level {r['level']}]"
        + ("  [split_by=directory]" if r.get("split_by") else ""),
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
                min_files: int = _DEFAULT_MIN_FILES, include_unattached: bool = False,
                output_chars: int = DEFAULT_OUTPUT_CHARS) -> Dict[str, Any]:
    """What parts this repo is made of, at `level` 0 (fine, per-module) or
    1 (coarse groups of those modules).

    Cached per `(workspace, project_id)` and the index's own fingerprint --
    a second call with an unchanged index is a cache read, a changed one
    rebuilds. `refresh=True` forces a rebuild even when the fingerprint
    matches. The default listing hides communities under `min_files` files
    (folding already merged most of these at build time; this is a display-
    time floor on top of that) and always hides the "Unattached tests"
    bucket unless `include_unattached=True` -- both counts are still
    reported (`hidden_small_count`, `unattached_tests_count`) so nothing
    silently disappears. `summarize=True` additionally tries a one-sentence
    model summary per community (see `_maybe_summarize`); it never blocks on
    a model that would have to be loaded and never changes the deterministic
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
    try:
        min_files_i = max(0, int(min_files or 0))
    except (TypeError, ValueError):
        min_files_i = _DEFAULT_MIN_FILES

    fp = _fingerprint(resolved, project_id)
    all_records = None if refresh else _load_cached(resolved, project_id, fp)
    if all_records is None:
        all_records = _build(resolved, project_id, fp)

    if summarize:
        _maybe_summarize(resolved, project_id, fp, all_records)

    picked_all = [r for r in all_records if r["level"] == lvl]
    method = picked_all[0]["method"] if picked_all else "louvain"
    unattached_bucket = next((r for r in picked_all if r.get("bucket") == _BUCKET_UNATTACHED_TESTS), None)
    pool = picked_all if include_unattached else \
        [r for r in picked_all if r.get("bucket") != _BUCKET_UNATTACHED_TESTS]
    shown = [r for r in pool if len(r["files"]) >= min_files_i]
    hidden_count = len(pool) - len(shown)
    unattached_count = len(unattached_bucket["files"]) if unattached_bucket else 0

    return {
        "output": _render_list(shown, output_chars, hidden_count=hidden_count,
                               unattached_count=0 if include_unattached else unattached_count),
        "exit_code": 0, "root": resolved,
        "level": lvl, "method": method, "fingerprint": fp, "min_files": min_files_i,
        "hidden_small_count": hidden_count, "unattached_tests_count": unattached_count,
        "communities": [{k: v for k, v in r.items() if not k.startswith("_")} for r in shown],
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
    """The level-0 community id/name a symbol's file belongs to (a
    production file's own community, or -- for a symbol defined in a test
    file -- whichever community that test file was attached to, or the
    "Unattached tests" bucket)."""
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
    to_summarize = [r for r in records if not r.get("summary") and not r.get("bucket")]
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
