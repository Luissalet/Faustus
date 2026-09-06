"""
context_engine/code_index.py — where a symbol is, what it calls, and how sure
we are of the answer.

The failure this exists for is the one §10.1 opens with, and it is easy to
reproduce: ask a vector index "where is the OAuth callback handled" and it
returns three chunks that *talk* about OAuth callbacks.  None of them says
which router registers the endpoint, which module defines the function, which
test covers it, or what breaks if it moves.  The agent then edits the chunk it
was given, because that is what it was shown — and the chunk was a paragraph of
a file, not the definition.

So this is the other index: structural, incremental, and scoped.  For every
symbol it keeps the qualified name, the kind, the signature, the file and the
line range, a hash of its own bytes and a hash of the file it came from; for
every relation it keeps how the relation was established.

Four rules carried over from §10 verbatim, because each one is a way this kind
of index normally goes wrong:

1. **Every edge carries its certainty.**  An import resolved from the AST to a
   file that exists is `exact`; a call resolved by matching a bare name is
   `static_inferred`; anything found by reading text is `lexical`.  An
   inference is never presented as an exact edge — that is the line in §10.3
   and it is the reason `neighbors()` returns the certainty next to every hop.
2. **Incremental by file hash.**  :func:`refresh` reindexes the files whose
   bytes changed, deletes the symbols of the ones that vanished, and stops when
   it has spent `budget_files` — a monorepo gets a truncated answer, never a
   twenty-minute one.
3. **Progressive retrieval.**  :func:`as_candidates` hands over a signature, a
   line range and a short summary — never the file body — with a
   `source_ref` of `symbol:<path>#L<start>-L<end>` so the agent can open it
   with a tool.  The index points; a modification is still made against the
   file as it is on disk right now.
4. **Nothing generated, vendored, binary or secret is indexed.**  The walk
   prunes with `src.index_walk`, the same policy the document indexes use, so
   the two cannot drift.

What is reused, and what could not be.  `src.repo_map` already owns the symbol
extractors — `lang_for_path` and `symbol_lines` are imported here and are the
*only* extractor for JS/TS, Go, Rust, Java, Ruby, PHP, C and Swift.  Its Python
path could not be reused: `repo_map._py_defs` is private and answers a
different question (a name and a line, for a one-line-per-file summary), while
an index needs the signature, the end line, the docstring, the imports and the
calls.  Rather than a second parser this module walks the same stdlib `ast`
that `_py_defs` walks — one parser, two readers — and no tree-sitter is added.
"""

from __future__ import annotations

import ast
import hashlib
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from src.index_walk import is_indexable_file, prune_index_dirs
from src.repo_map import lang_for_path, symbol_lines

from . import store
from .contracts import ContextCandidate, new_id

logger = logging.getLogger(__name__)


# ── closed vocabularies ────────────────────────────────────────────────────

SYMBOL_KINDS: Tuple[str, ...] = (
    "module", "class", "function", "method", "constant", "route", "tool",
)

EDGE_KINDS: Tuple[str, ...] = ("imports", "calls", "defines", "tests", "registers")

#: §10.3.  The whole point of the column: a reader must be able to tell an
#: observation from a guess without knowing how the extractor works.
#:
#: * `exact`            — the AST said so and the target was resolved to a file
#:                        that exists in this workspace.
#: * `static_inferred`  — resolved by name, or the statement is exact but the
#:                        target could not be pinned down (an import of a
#:                        module outside the workspace, a call matched to the
#:                        one symbol in the index with that name).
#: * `lexical`          — found by reading text: the regex extractors, and the
#:                        relations built on top of them.
CERTAINTY: Tuple[str, ...] = ("exact", "static_inferred", "lexical")

#: Same ceiling `repo_map` uses.  A 400 kB source file is generated, minified
#: or a data blob; parsing it costs more than the symbols are worth.
MAX_FILE_BYTES = 400_000

#: §10.4's time budget.  `refresh` stops after this many candidate files and
#: says `truncated`, rather than holding a connection open across a monorepo.
DEFAULT_BUDGET_FILES = 2000

SUMMARY_CHARS = 240
SIGNATURE_CHARS = 300

#: Secrets, on top of `index_walk`'s hidden-file rule (which already covers
#: `.env`, `.npmrc` and everything under a dot-directory).  These names are not
#: hidden and must still never reach the index.  The list mirrors
#: `src.tool_execution._SENSITIVE_FILE_PATTERNS`; it is not imported from there
#: because the check is private to the agent's tool dispatcher and an indexer
#: that imports the dispatcher has the dependency backwards.
_SECRET_NAMES: Tuple[str, ...] = (
    "authorized_keys", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    "known_hosts", "credentials", "secrets", "secring.gpg",
)
_SECRET_SUFFIXES: Tuple[str, ...] = (".pem", ".key", ".pfx", ".p12", ".keystore")

#: A test file, for the `tests` edges.  `repo_map._TEST_HINT_RE` answers a
#: neighbouring question (should this file be ranked down in the map) and is
#: private; this one is narrower on purpose — it decides whether an import
#: becomes a `tests` edge, and a false positive there mislabels the graph.
_TEST_PATH_RE = re.compile(
    r"(?:^|/)(?:tests?|__tests__|spec|specs)(?:/|$)"
    r"|(?:^|/)test_[^/]*$|_test\.\w+$|\.(?:test|spec)\.\w+$", re.I)

#: Decorator tails that register an HTTP route.  Matched on the last dotted
#: segment (`app.get`, `router.post`, `bp.route`), never on a bare name — a
#: local function called `get` is not an endpoint.
_ROUTE_DECORATORS: Tuple[str, ...] = (
    "get", "post", "put", "patch", "delete", "head", "options",
    "route", "websocket", "api_route",
)

#: Decorator tails that register a callable as an agent/MCP tool.
_TOOL_DECORATORS: Tuple[str, ...] = ("tool", "register_tool", "mcp_tool")

_WORD_RE = re.compile(r"[A-Za-z0-9_]+")


# ── schema ─────────────────────────────────────────────────────────────────
#
# Three tables and one rule: every query in this module is scoped by
# `workspace`, and by `project_id` when the caller names one.  Two projects
# indexed into the same store must not be able to see each other's symbols, so
# the scope columns are indexed and the filter is never optional.
#
# Two columns exist on the tables and not on the dataclasses, deliberately:
# `code_symbols.name` (the last segment of the qualname) is what call
# resolution looks up and would otherwise be a `LIKE '%.foo'` scan of the whole
# index; `code_edges.workspace`/`project_id` are what make `drop()` a delete
# instead of a graph walk.  Both are storage concerns, and the wire shapes
# `Symbol` and `Edge` stay the shapes §10.2 describes.

store.register_schema("code_index", (
    """
    CREATE TABLE IF NOT EXISTS code_symbols (
        id           TEXT PRIMARY KEY,
        project_id   TEXT NOT NULL DEFAULT '',
        workspace    TEXT NOT NULL DEFAULT '',
        path         TEXT NOT NULL DEFAULT '',
        qualname     TEXT NOT NULL DEFAULT '',
        name         TEXT NOT NULL DEFAULT '',
        kind         TEXT NOT NULL DEFAULT 'function',
        signature    TEXT NOT NULL DEFAULT '',
        summary      TEXT NOT NULL DEFAULT '',
        start_line   INTEGER NOT NULL DEFAULT 0,
        end_line     INTEGER NOT NULL DEFAULT 0,
        content_hash TEXT NOT NULL DEFAULT '',
        file_hash    TEXT NOT NULL DEFAULT '',
        language     TEXT NOT NULL DEFAULT '',
        indexed_at   TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_code_symbols_scope "
    "ON code_symbols(workspace, project_id)",
    "CREATE INDEX IF NOT EXISTS ix_code_symbols_path ON code_symbols(workspace, path)",
    "CREATE INDEX IF NOT EXISTS ix_code_symbols_name ON code_symbols(workspace, name)",
    "CREATE INDEX IF NOT EXISTS ix_code_symbols_qualname "
    "ON code_symbols(workspace, qualname)",
    """
    CREATE TABLE IF NOT EXISTS code_edges (
        src        TEXT NOT NULL,
        dst        TEXT NOT NULL,
        kind       TEXT NOT NULL,
        certainty  TEXT NOT NULL DEFAULT 'lexical',
        detail     TEXT NOT NULL DEFAULT '',
        workspace  TEXT NOT NULL DEFAULT '',
        project_id TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (src, dst, kind)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_code_edges_src ON code_edges(src)",
    "CREATE INDEX IF NOT EXISTS ix_code_edges_dst ON code_edges(dst)",
    "CREATE INDEX IF NOT EXISTS ix_code_edges_scope ON code_edges(workspace, project_id)",
    # The file table is what makes the refresh incremental: one row per indexed
    # file with the hash of its bytes.  A file whose hash is unchanged is not
    # opened, let alone parsed.
    """
    CREATE TABLE IF NOT EXISTS code_files (
        workspace  TEXT NOT NULL,
        path       TEXT NOT NULL,
        project_id TEXT NOT NULL DEFAULT '',
        file_hash  TEXT NOT NULL DEFAULT '',
        language   TEXT NOT NULL DEFAULT '',
        symbols    INTEGER NOT NULL DEFAULT 0,
        indexed_at TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (workspace, path)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_code_files_scope ON code_files(workspace, project_id)",
))


# ── the shapes (§10.2) ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class Symbol:
    """One definition, and enough to find it without reading the file.

    `content_hash` is the symbol's own bytes and `file_hash` is the file's:
    the first says whether *this* definition changed, the second is what makes
    the refresh incremental.  Keeping both is what lets an experience or an
    excerpt be invalidated at the right granularity (§10.4)."""

    id: str = ""
    project_id: str = ""
    workspace: str = ""
    path: str = ""
    qualname: str = ""
    kind: str = "function"
    signature: str = ""
    summary: str = ""
    start_line: int = 0
    end_line: int = 0
    content_hash: str = ""
    file_hash: str = ""
    language: str = ""
    indexed_at: str = ""

    @classmethod
    def parse(cls, raw: Any) -> "Symbol":
        data: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
        kind = _text(data.get("kind"), limit=32)
        return cls(
            id=_text(data.get("id"), limit=64),
            project_id=_text(data.get("project_id"), limit=128),
            workspace=_text(data.get("workspace"), limit=1024),
            path=_text(data.get("path"), limit=1024),
            qualname=_text(data.get("qualname"), limit=512),
            kind=kind if kind in SYMBOL_KINDS else "function",
            signature=_text(data.get("signature"), limit=SIGNATURE_CHARS),
            summary=_text(data.get("summary"), limit=SUMMARY_CHARS),
            start_line=_whole(data.get("start_line")),
            end_line=_whole(data.get("end_line")),
            content_hash=_text(data.get("content_hash"), limit=64),
            file_hash=_text(data.get("file_hash"), limit=64),
            language=_text(data.get("language"), limit=32),
            indexed_at=_text(data.get("indexed_at"), limit=64),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "project_id": self.project_id, "workspace": self.workspace,
            "path": self.path, "qualname": self.qualname, "kind": self.kind,
            "signature": self.signature, "summary": self.summary,
            "start_line": self.start_line, "end_line": self.end_line,
            "content_hash": self.content_hash, "file_hash": self.file_hash,
            "language": self.language, "indexed_at": self.indexed_at,
        }

    def source_ref(self) -> str:
        """§10.5's pointer: enough for a tool to open exactly this range."""
        return f"symbol:{self.path}#L{self.start_line}-L{self.end_line}"

    def name(self) -> str:
        return self.qualname.rsplit(".", 1)[-1]


@dataclass(frozen=True)
class Edge:
    """One relation, and how it was established.

    `certainty` is not optional and has no default that means "probably fine":
    the whole value of this graph is that a reader can tell which hops are
    observations and which are guesses."""

    src: str
    dst: str
    kind: str
    certainty: str
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"src": self.src, "dst": self.dst, "kind": self.kind,
                "certainty": self.certainty, "detail": self.detail}


# ── normalisation and paths ────────────────────────────────────────────────

def _text(value: Any, *, limit: int = 1024) -> str:
    try:
        out = "" if value is None else str(value)
    except Exception:  # noqa: BLE001 - a normaliser never raises
        return ""
    return out.strip()[:limit]


def _whole(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:32]


def _symbol_id(workspace: str, path: str, qualname: str, kind: str) -> str:
    """A stable id: same workspace, path, qualname and kind means the same row.

    Stable on purpose — an edge recorded on Tuesday has to still point at the
    symbol after Wednesday's refresh moved it forty lines down.  Renaming a
    symbol therefore *is* a new id and the old one is deleted, which is exactly
    the behaviour §10.4 asks for."""
    blob = "\x00".join((workspace, path, qualname, kind)).encode("utf-8", "replace")
    return "sym_" + hashlib.sha256(blob).hexdigest()[:24]


def _norm_workspace(value: Any) -> str:
    raw = _text(value, limit=1024)
    if not raw:
        return ""
    try:
        return os.path.normpath(os.path.realpath(os.path.expanduser(raw)))
    except OSError:
        return os.path.normpath(raw)


def _rel(root: str, abs_path: str) -> str:
    """Workspace-relative, forward slashes — the form every caller reads back.

    A path that escapes the workspace returns "" and is dropped: an index that
    can name a file outside the root it was given has lost its isolation."""
    try:
        rel = os.path.relpath(abs_path, root)
    except (OSError, ValueError):
        return ""
    rel = rel.replace("\\", "/")
    if rel.startswith("../") or rel == "..":
        return ""
    return rel


def _is_secret_name(name: str) -> bool:
    low = name.casefold()
    if low in _SECRET_NAMES:
        return True
    return any(low.endswith(suffix) for suffix in _SECRET_SUFFIXES)


def _indexable(rel_path: str) -> bool:
    """`index_walk`'s policy, applied to a path the caller handed us directly.

    :func:`refresh` prunes while it walks, which is cheaper; this is the same
    decision for the `paths=` form, where nobody walked anything and a caller
    could otherwise name `node_modules/react/index.js` and have it indexed."""
    if not rel_path:
        return False
    parts = rel_path.split("/")
    directories, name = parts[:-1], parts[-1]
    probe = list(directories)
    prune_index_dirs(probe)
    if len(probe) != len(directories):
        return False
    if not is_indexable_file(name) or _is_secret_name(name):
        return False
    return lang_for_path(rel_path) != "none"


def _tokens(value: Any) -> Set[str]:
    """Identifier-aware tokens, the same split `experiences` uses: a query for
    "refresh token" has to reach `refresh_oauth_token`."""
    out: Set[str] = set()
    for piece in _WORD_RE.findall(_text(value, limit=4096).lower()):
        if len(piece) < 2:
            continue
        out.add(piece)
        for part in piece.split("_"):
            if len(part) >= 2:
                out.add(part)
    return out


def _coverage(query: Set[str], target: Set[str]) -> float:
    """How much of the query this target accounts for, in [0, 1].  Coverage
    rather than Jaccard: a module with two hundred tokens should not lose to a
    one-token constant just for being a module."""
    if not query or not target:
        return 0.0
    return min(1.0, len(query & target) / float(len(query)))


# ── extraction: Python, through the stdlib AST ─────────────────────────────

def _module_qualname(rel_path: str) -> str:
    stem = rel_path[:-3] if rel_path.endswith(".py") else os.path.splitext(rel_path)[0]
    parts = [part for part in stem.split("/") if part]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) or stem


def _dotted(node: Any) -> str:
    """`app.router.get` from the AST node, or "" when it is not a dotted name."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Call):
        return _dotted(node.func)
    return ""


def _unparse(node: Any, fallback: str = "") -> str:
    try:
        return ast.unparse(node)
    except Exception:  # noqa: BLE001 - an odd node costs the signature, not the file
        return fallback


def _py_signature(node: Any) -> str:
    if isinstance(node, ast.ClassDef):
        bases = ", ".join(part for part in (_unparse(b) for b in node.bases) if part)
        return (f"class {node.name}({bases})" if bases else f"class {node.name}")[:SIGNATURE_CHARS]
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    args = _unparse(node.args, ", ".join(
        a.arg for a in list(node.args.posonlyargs) + list(node.args.args)))
    returns = f" -> {_unparse(node.returns)}" if node.returns else ""
    return f"{prefix} {node.name}({args}){returns}"[:SIGNATURE_CHARS]


def _py_summary(node: Any) -> str:
    try:
        doc = ast.get_docstring(node) or ""
    except Exception:  # noqa: BLE001
        doc = ""
    for line in doc.splitlines():
        if line.strip():
            return line.strip()[:SUMMARY_CHARS]
    return ""


def _decorator_kind(node: Any) -> Tuple[str, str]:
    """`("route", "app.get /users")`, `("tool", "@tool")`, or `("", "")`.

    Read off the decorator list, so the path string is the literal one in the
    source.  The *object* it registers on (`app`, `router`, `bp`) is only a
    name here, which is why the edge this produces is `static_inferred` and not
    `exact`: the decorator is observed, the router it belongs to is inferred."""
    for decorator in getattr(node, "decorator_list", ()) or ():
        call = decorator if isinstance(decorator, ast.Call) else None
        dotted = _dotted(decorator)
        if not dotted:
            continue
        tail = dotted.rsplit(".", 1)[-1].lower()
        if tail in _ROUTE_DECORATORS and "." in dotted:
            route = ""
            if call and call.args and isinstance(call.args[0], ast.Constant) \
                    and isinstance(call.args[0].value, str):
                route = call.args[0].value
            return "route", _text(f"{dotted} {route}".strip(), limit=SIGNATURE_CHARS)
        if tail in _TOOL_DECORATORS:
            return "tool", _text(f"@{dotted}", limit=SIGNATURE_CHARS)
    return "", ""


def _py_imports(tree: ast.AST, module_qual: str, *,
                is_package: bool) -> List[Tuple[str, bool]]:
    """`(dotted module, optional)` for every import in the file.

    `optional` marks a name that is a *guess about shape*, not about existence:
    in `from pkg import util`, `util` may be a submodule or it may be a
    function.  The guess is worth making — resolving it turns a dependency the
    graph would otherwise miss into an `exact` edge — but only when the module
    turns out to exist.  An optional name that resolves to nothing is dropped
    instead of being recorded as an unresolved import, because `module:pkg.util`
    for a function called `util` would be a fact the index invented.

    `ast.walk` rather than `tree.body`: this repository imports lazily inside
    functions all over the place (`from src import prove as prove_mod`), and an
    import graph that misses those is a graph that says two modules are
    unrelated when one calls the other.

    `is_package` is not a detail.  `from . import x` means `a.b.x` in both
    `a/b/c.py` (module `a.b.c`) and `a/b/__init__.py` (module `a.b`), because
    the package of an `__init__` is itself.  Counting levels off the module
    name alone sends every relative import inside a package one level too
    high, so the `__init__` segment is put back before counting."""
    out: List[Tuple[str, bool]] = []
    seen: Set[str] = set()
    package = module_qual.split(".") + (["__init__"] if is_package else [])

    def add(dotted: str, optional: bool) -> None:
        if dotted and dotted not in seen:
            seen.add(dotted)
            out.append((dotted, optional))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                add(alias.name or "", False)
        elif isinstance(node, ast.ImportFrom):
            level = int(node.level or 0)
            if level:
                base = package[:max(0, len(package) - level)]
                dotted = ".".join([*base, node.module] if node.module else base)
            else:
                dotted = node.module or ""
            add(dotted, False)
            for alias in node.names:
                if alias.name and alias.name != "*":
                    add(f"{dotted}.{alias.name}" if dotted else alias.name, True)
    return out


@dataclass
class _Extracted:
    """One file's contribution, before anything is resolved across files.

    `imports` and `calls` are deliberately unresolved here: a call can only be
    matched against symbols that other files defined, so resolution happens
    once, after every changed file has been written (and it is the step that
    decides the edge's certainty)."""

    symbols: List[Symbol]
    edges: List[Edge]
    imports: List[Tuple[str, str, bool]]    # (src symbol id, dotted module, optional)
    calls: List[Tuple[str, str, str]]       # (src symbol id, callee name, detail)


def _slice_hash(lines: Sequence[str], start: int, end: int) -> str:
    body = "\n".join(lines[max(0, start - 1):max(start, end)])
    return _hash(body.encode("utf-8", "replace"))


def _extract_python(*, workspace: str, project_id: str, rel: str, text: str,
                    file_hash: str, indexed_at: str) -> Optional[_Extracted]:
    """Symbols and edges from the stdlib AST, or None when the file does not
    parse (the caller then falls back to the lexical extractor, which is what
    `repo_map` does with the same input)."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return None

    lines = text.split("\n")
    module_qual = _module_qualname(rel)
    is_package = rel.endswith("/__init__.py") or rel == "__init__.py"
    symbols: List[Symbol] = []
    edges: List[Edge] = []
    imports: List[Tuple[str, str, bool]] = []
    calls: List[Tuple[str, str, str]] = []
    bodies: List[Tuple[str, Any]] = []

    def add(qualname: str, kind: str, signature: str, summary: str,
            start: int, end: int) -> str:
        symbol_id = _symbol_id(workspace, rel, qualname, kind)
        symbols.append(Symbol(
            id=symbol_id, project_id=project_id, workspace=workspace, path=rel,
            qualname=qualname, kind=kind, signature=_text(signature, limit=SIGNATURE_CHARS),
            summary=_text(summary, limit=SUMMARY_CHARS), start_line=start, end_line=end,
            content_hash=_slice_hash(lines, start, end), file_hash=file_hash,
            language="py", indexed_at=indexed_at))
        return symbol_id

    module_id = add(module_qual, "module", rel, _py_summary(tree), 1, len(lines))

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            end = int(getattr(node, "end_lineno", node.lineno) or node.lineno)
            class_id = add(node.name, "class", _py_signature(node),
                           _py_summary(node), node.lineno, end)
            edges.append(Edge(module_id, class_id, "defines", "exact", "class"))
            for child in node.body:
                if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                kind, detail = _decorator_kind(child)
                child_end = int(getattr(child, "end_lineno", child.lineno) or child.lineno)
                method_id = add(f"{node.name}.{child.name}", kind or "method",
                                detail or _py_signature(child), _py_summary(child),
                                child.lineno, child_end)
                edges.append(Edge(class_id, method_id, "defines", "exact", "method"))
                if kind:
                    # The decorator is in the source (exact) but the object it
                    # registers on is a bare name we did not resolve — so the
                    # relation is inferred, and says so.  §10.3.
                    edges.append(Edge(module_id, method_id, "registers",
                                      "static_inferred", detail))
                bodies.append((method_id, child))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            kind, detail = _decorator_kind(node)
            end = int(getattr(node, "end_lineno", node.lineno) or node.lineno)
            function_id = add(node.name, kind or "function",
                              detail or _py_signature(node), _py_summary(node),
                              node.lineno, end)
            edges.append(Edge(module_id, function_id, "defines", "exact", kind or "function"))
            if kind:
                edges.append(Edge(module_id, function_id, "registers",
                                  "static_inferred", detail))
            bodies.append((function_id, node))
        else:
            targets = []
            if isinstance(node, ast.Assign):
                targets = [t for t in node.targets if isinstance(t, ast.Name)]
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                targets = [node.target]
            for target in targets:
                name = target.id
                if not name.isupper() or len(name) < 3 or name.startswith("_"):
                    continue
                end = int(getattr(node, "end_lineno", node.lineno) or node.lineno)
                constant_id = add(name, "constant", name, "", node.lineno, end)
                edges.append(Edge(module_id, constant_id, "defines", "exact", "constant"))

    for dotted, optional in _py_imports(tree, module_qual, is_package=is_package):
        imports.append((module_id, dotted, optional))

    for owner_id, node in bodies:
        seen: Set[str] = set()
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            dotted = _dotted(inner.func)
            callee = dotted.rsplit(".", 1)[-1]
            if not callee or callee in seen:
                continue
            seen.add(callee)
            calls.append((owner_id, callee, dotted))

    return _Extracted(symbols=symbols, edges=edges, imports=imports, calls=calls)


# ── extraction: everything else, through repo_map's extractors ─────────────

def _lexical_kind(name: str) -> str:
    """A kind for a name a regex found, and nothing more.

    `repo_map.symbol_lines` returns names, not kinds — its callers (the repo
    map line and read_plan's outline) do not need one.  So this is a naming
    convention read as a heuristic, and every edge built on it is `lexical`
    rather than `static_inferred`: guessing that `UserStore` is a class is not
    the same kind of knowledge as parsing the declaration."""
    if name.isupper() and len(name) > 2:
        return "constant"
    return "class" if name[:1].isupper() else "function"


def _extract_lexical(*, workspace: str, project_id: str, rel: str, text: str,
                     lang: str, file_hash: str, indexed_at: str) -> _Extracted:
    """The other eight languages, and Python files that do not parse.

    Everything here comes from `repo_map.symbol_lines`, which owns the
    per-language regexes.  Two things it cannot give and this therefore
    approximates, marked as approximations: the kind (see above) and the end
    line, which is taken as the line before the next symbol.  A file with one
    symbol reports that symbol as running to the end of the file, which is
    usually true and never claimed to be exact."""
    lines = text.split("\n")
    symbols: List[Symbol] = []
    edges: List[Edge] = []
    module_qual = _module_qualname(rel) if lang == "py" else rel
    module_id = _symbol_id(workspace, rel, module_qual, "module")
    symbols.append(Symbol(
        id=module_id, project_id=project_id, workspace=workspace, path=rel,
        qualname=module_qual, kind="module", signature=rel, summary="",
        start_line=1, end_line=len(lines),
        content_hash=_hash(text.encode("utf-8", "replace")), file_hash=file_hash,
        language=lang, indexed_at=indexed_at))

    found = sorted(
        ((name, line) for name, line in symbol_lines(text, lang)
         if name and 1 <= line <= len(lines)),
        key=lambda pair: (pair[1], pair[0]))
    for position, (name, line) in enumerate(found):
        end = found[position + 1][1] - 1 if position + 1 < len(found) else len(lines)
        end = max(line, end)
        kind = _lexical_kind(name)
        signature = _text(lines[line - 1], limit=SIGNATURE_CHARS) if line <= len(lines) else name
        symbol_id = _symbol_id(workspace, rel, name, kind)
        if any(existing.id == symbol_id for existing in symbols):
            continue
        symbols.append(Symbol(
            id=symbol_id, project_id=project_id, workspace=workspace, path=rel,
            qualname=name, kind=kind, signature=signature or name, summary="",
            start_line=line, end_line=end,
            content_hash=_slice_hash(lines, line, end), file_hash=file_hash,
            language=lang, indexed_at=indexed_at))
        edges.append(Edge(module_id, symbol_id, "defines", "lexical", kind))

    return _Extracted(symbols=symbols, edges=edges, imports=[], calls=[])


def _extract(*, workspace: str, project_id: str, rel: str, text: str, lang: str,
             file_hash: str, indexed_at: str) -> _Extracted:
    if lang == "py":
        parsed = _extract_python(workspace=workspace, project_id=project_id, rel=rel,
                                 text=text, file_hash=file_hash, indexed_at=indexed_at)
        if parsed is not None:
            return parsed
        logger.debug("code_index: %s did not parse; falling back to the lexical pass", rel)
    return _extract_lexical(workspace=workspace, project_id=project_id, rel=rel,
                            text=text, lang=lang, file_hash=file_hash,
                            indexed_at=indexed_at)


# ── writing ────────────────────────────────────────────────────────────────

_SYMBOL_COLUMNS: Tuple[str, ...] = (
    "id", "project_id", "workspace", "path", "qualname", "name", "kind",
    "signature", "summary", "start_line", "end_line", "content_hash",
    "file_hash", "language", "indexed_at",
)


def _read_source(abs_path: str) -> Optional[Tuple[str, str]]:
    """`(text, file_hash)`, or None when the file cannot or should not be read.

    The hash is of the bytes, before decoding: a file whose only change is its
    encoding really did change, and a hash taken after `errors="replace"` would
    say it did not."""
    try:
        with open(abs_path, "rb") as handle:
            raw = handle.read(MAX_FILE_BYTES + 1)
    except OSError:
        return None
    if len(raw) > MAX_FILE_BYTES:
        return None
    if b"\x00" in raw[:4096]:
        # A binary that happens to carry a source extension.  §10.4: binaries
        # are not indexed, and the extension is not what decides that.
        return None
    return raw.decode("utf-8", "replace"), _hash(raw)


def _walk(root: str, budget: int) -> Tuple[List[str], bool]:
    """Indexable files under `root`, pruned by `index_walk`, capped by budget.

    Sorted at every level so that a truncated walk truncates the same way
    twice — a budget that returns a different half of the repository on each
    run is worse than no budget."""
    out: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        prune_index_dirs(dirnames)
        dirnames.sort()
        for name in sorted(filenames):
            if not is_indexable_file(name) or _is_secret_name(name):
                continue
            rel = _rel(root, os.path.join(dirpath, name))
            if not rel or lang_for_path(rel) == "none":
                continue
            out.append(rel)
            if len(out) >= budget:
                return out, True
    return out, False


def _forget_file(conn: sqlite3.Connection, workspace: str, rel: str,
                 keep: Sequence[str] = ()) -> None:
    """Drop a file's symbols and the edges it owns.

    Outgoing edges belong to the file that declared them and always go.
    Incoming edges are dropped only for symbols that did not come back, so a
    function that merely moved keeps the callers that point at it — the id is
    stable across a move by construction."""
    old = [row["id"] for row in conn.execute(
        "SELECT id FROM code_symbols WHERE workspace = ? AND path = ?", (workspace, rel))]
    if not old:
        return
    marks = ",".join("?" * len(old))
    conn.execute(f"DELETE FROM code_edges WHERE src IN ({marks})", old)
    gone = [symbol_id for symbol_id in old if symbol_id not in set(keep)]
    if gone:
        marks = ",".join("?" * len(gone))
        conn.execute(f"DELETE FROM code_edges WHERE dst IN ({marks})", gone)
    conn.execute("DELETE FROM code_symbols WHERE workspace = ? AND path = ?",
                 (workspace, rel))


def _drop_file(conn: sqlite3.Connection, workspace: str, rel: str) -> None:
    _forget_file(conn, workspace, rel)
    conn.execute("DELETE FROM code_files WHERE workspace = ? AND path = ?",
                 (workspace, rel))


def _insert_symbols(conn: sqlite3.Connection, symbols: Sequence[Symbol]) -> None:
    if not symbols:
        return
    columns = ", ".join(_SYMBOL_COLUMNS)
    placeholders = ", ".join("?" * len(_SYMBOL_COLUMNS))
    conn.executemany(
        f"INSERT OR REPLACE INTO code_symbols ({columns}) VALUES ({placeholders})",
        [tuple({**symbol.to_dict(), "name": symbol.name()}[column]
               for column in _SYMBOL_COLUMNS) for symbol in symbols])


def _insert_edges(conn: sqlite3.Connection, workspace: str, project_id: str,
                  edges: Sequence[Edge]) -> int:
    rows = [(edge.src, edge.dst, edge.kind,
             edge.certainty if edge.certainty in CERTAINTY else "lexical",
             _text(edge.detail, limit=512), workspace, project_id)
            for edge in edges
            if edge.src and edge.dst and edge.kind in EDGE_KINDS]
    if not rows:
        return 0
    conn.executemany(
        "INSERT OR REPLACE INTO code_edges "
        "(src, dst, kind, certainty, detail, workspace, project_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    return len(rows)


def _resolve(conn: sqlite3.Connection, workspace: str,
             imports: Sequence[Tuple[str, str, bool]],
             calls: Sequence[Tuple[str, str, str]],
             module_paths: Mapping[str, str]) -> List[Edge]:
    """Turn the unresolved halves into edges, each with the certainty it earned.

    * An import whose dotted name is a module this index holds is `exact`: the
      statement came from the AST and the target is a file that exists.
    * An import of anything else keeps the edge — knowing that a module depends
      on `fastapi` is worth having — with a synthetic `module:<name>` target and
      `static_inferred`, because nothing here resolved it to a definition.  The
      exception is an `optional` name (the `util` of `from pkg import util`),
      which is dropped when it resolves to nothing rather than recorded as a
      module that may never have existed.
    * A call is matched by its bare name.  Exactly one symbol with that name in
      this workspace makes it `static_inferred`; zero or several drop the edge.
      Guessing between two `save()`s and presenting the guess as a graph edge is
      the failure §10.3 names.
    * A test module's import of a workspace module is additionally a `tests`
      edge, at the same certainty as the import it was derived from."""
    edges: List[Edge] = []
    modules = {row["qualname"]: row["id"] for row in conn.execute(
        "SELECT qualname, id FROM code_symbols "
        "WHERE workspace = ? AND kind = 'module'", (workspace,))}

    for src, dotted, optional in imports:
        target = modules.get(dotted)
        if target and target != src:
            edges.append(Edge(src, target, "imports", "exact", dotted))
            if _TEST_PATH_RE.search(module_paths.get(src, "")):
                edges.append(Edge(src, target, "tests", "exact", dotted))
        elif not target and not optional:
            edges.append(Edge(src, f"module:{dotted}", "imports",
                              "static_inferred", dotted))

    wanted = sorted({name for _, name, _ in calls})
    by_name: Dict[str, List[str]] = {}
    for start in range(0, len(wanted), 400):
        chunk = wanted[start:start + 400]
        marks = ",".join("?" * len(chunk))
        for row in conn.execute(
                f"SELECT name, id FROM code_symbols WHERE workspace = ? "
                f"AND name IN ({marks}) AND kind IN "
                "('function', 'method', 'class', 'route', 'tool')",
                [workspace, *chunk]):
            by_name.setdefault(row["name"], []).append(row["id"])

    for src, name, detail in calls:
        found = by_name.get(name) or []
        if len(found) != 1 or found[0] == src:
            continue
        edges.append(Edge(src, found[0], "calls", "static_inferred", detail))
    return edges


# ── the public surface ─────────────────────────────────────────────────────

def refresh(workspace: str, *, project_id: str = "", paths: Optional[Sequence[str]] = None,
            full: bool = False, budget_files: int = DEFAULT_BUDGET_FILES) -> Dict[str, Any]:
    """Bring the index up to date and say what that cost.

    Incremental unless `full`: a file whose hash matches the stored one is not
    opened.  `paths` narrows the work to a few files (what a post-edit hook
    passes) and still applies the same prune policy, so naming a file inside
    `node_modules/` does not get it indexed.  Files that have disappeared are
    removed, together with the symbols only they defined.

    Returns `{"scanned", "reindexed", "removed", "symbols", "edges",
    "elapsed_ms", "truncated"}`.  `symbols` and `edges` are the totals the
    workspace holds afterwards — the same numbers :func:`status` reports — and
    `truncated` is True when `budget_files` ran out before the walk did, which
    is the honest answer for a monorepo and much better than the twenty minutes
    the alternative takes.  Never raises."""
    started = time.time()
    out: Dict[str, Any] = {"scanned": 0, "reindexed": 0, "removed": 0,
                           "symbols": 0, "edges": 0, "elapsed_ms": 0,
                           "truncated": False}
    root = _norm_workspace(workspace)
    if not root or not os.path.isdir(root):
        out["elapsed_ms"] = int((time.time() - started) * 1000)
        return out
    scope = _text(project_id, limit=128)
    budget = max(1, int(budget_files or DEFAULT_BUDGET_FILES))

    try:
        if paths is None:
            candidates, out["truncated"] = _walk(root, budget)
            targeted = False
        else:
            targeted = True
            candidates = []
            for raw in paths:
                rel = _text(raw, limit=1024).replace("\\", "/").strip("/")
                if os.path.isabs(rel):
                    rel = _rel(root, rel)
                if rel and _indexable(rel) and rel not in candidates:
                    candidates.append(rel)
            if len(candidates) > budget:
                candidates, out["truncated"] = candidates[:budget], True
        out["scanned"] = len(candidates)
        _refresh_files(root, scope, candidates, out,
                       full=full, targeted=targeted)
    except (store.ContextStoreError, sqlite3.Error, OSError) as exc:
        logger.warning("code_index.refresh(%s) failed: %s", root, exc)
    out["elapsed_ms"] = int((time.time() - started) * 1000)
    return out


def _reresolve_dangling(conn: sqlite3.Connection, workspace: str,
                        project_id: str) -> int:
    """Promote `module:<name>` imports whose target has since been indexed.

    The cost of resolving incrementally: file A's import of module B is written
    when A is indexed, and if B was not in the index yet the edge is a
    `static_inferred` pointer at a name.  Adding B later does not touch A, so
    without this pass the edge would stay inferred until A itself changed.  A
    dangling edge whose target now exists is rewritten as `exact` — the same
    answer a `full=True` refresh would give, without the full refresh."""
    modules = {row["qualname"]: row["id"] for row in conn.execute(
        "SELECT qualname, id FROM code_symbols "
        "WHERE workspace = ? AND kind = 'module'", (workspace,))}
    if not modules:
        return 0
    paths = {row["id"]: row["path"] for row in conn.execute(
        "SELECT id, path FROM code_symbols "
        "WHERE workspace = ? AND kind = 'module'", (workspace,))}
    promoted: List[Tuple[str, str, str, str]] = []
    for row in conn.execute(
            "SELECT src, dst FROM code_edges WHERE workspace = ? AND kind = 'imports' "
            "AND dst LIKE 'module:%'", (workspace,)):
        dotted = str(row["dst"])[len("module:"):]
        target = modules.get(dotted)
        if target and target != row["src"]:
            promoted.append((row["src"], row["dst"], target, dotted))
    edges: List[Edge] = []
    for src, old, target, dotted in promoted:
        conn.execute("DELETE FROM code_edges WHERE src = ? AND dst = ? AND kind = 'imports'",
                     (src, old))
        edges.append(Edge(src, target, "imports", "exact", dotted))
        if _TEST_PATH_RE.search(paths.get(src, "")):
            edges.append(Edge(src, target, "tests", "exact", dotted))
    _insert_edges(conn, workspace, project_id, edges)
    return len(promoted)


def _refresh_files(root: str, scope: str, candidates: Sequence[str],
                   out: Dict[str, Any], *, full: bool, targeted: bool) -> None:
    """The body of :func:`refresh`, in one transaction.

    Split out so that `refresh` stays the part that decides *what* to look at
    and this stays the part that decides what to do about it.

    One transaction on purpose, and this is what `budget_files` is really
    bounding: `store.db()` is held for the whole pass, so a refresh that walked
    a monorepo would keep a chat turn waiting on the same lock.  Bounded, the
    trade is worth it — the alternative is a crash halfway through leaving an
    index that is half of two different revisions, which no reader can detect."""
    indexed_at = store.now_iso()
    with store.db() as conn:
        known = {row["path"]: row["file_hash"] for row in conn.execute(
            "SELECT path, file_hash FROM code_files WHERE workspace = ?", (root,))}
        pending_imports: List[Tuple[str, str, bool]] = []
        pending_calls: List[Tuple[str, str, str]] = []
        module_paths: Dict[str, str] = {}
        seen: Set[str] = set()

        for rel in candidates:
            source = _read_source(os.path.join(root, *rel.split("/")))
            if source is None:
                # Gone, unreadable, too large or binary — all of which mean the
                # same thing to the index: whatever is stored for it is a lie.
                if rel in known:
                    _drop_file(conn, root, rel)
                    out["removed"] += 1
                continue
            seen.add(rel)
            text, file_hash = source
            if not full and known.get(rel) == file_hash:
                continue
            language = lang_for_path(rel)
            extracted = _extract(workspace=root, project_id=scope, rel=rel, text=text,
                                 lang=language, file_hash=file_hash, indexed_at=indexed_at)
            _forget_file(conn, root, rel, keep=[s.id for s in extracted.symbols])
            _insert_symbols(conn, extracted.symbols)
            _insert_edges(conn, root, scope, extracted.edges)
            conn.execute(
                "INSERT OR REPLACE INTO code_files "
                "(workspace, path, project_id, file_hash, language, symbols, indexed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (root, rel, scope, file_hash, language, len(extracted.symbols), indexed_at))
            out["reindexed"] += 1
            pending_imports.extend(extracted.imports)
            pending_calls.extend(extracted.calls)
            for symbol in extracted.symbols:
                if symbol.kind == "module":
                    module_paths[symbol.id] = rel

        if not targeted and not out["truncated"]:
            # Only a complete walk may conclude that a file is gone.  A
            # truncated one never looked at the rest of the tree, and a
            # targeted one was never told about it.
            for rel in list(known):
                if rel not in seen:
                    _drop_file(conn, root, rel)
                    out["removed"] += 1

        if pending_imports or pending_calls:
            _insert_edges(conn, root, scope,
                          _resolve(conn, root, pending_imports, pending_calls, module_paths))
        if out["reindexed"]:
            _reresolve_dangling(conn, root, scope)

        out["symbols"] = int(conn.execute(
            "SELECT COUNT(*) AS n FROM code_symbols WHERE workspace = ?",
            (root,)).fetchone()["n"])
        out["edges"] = int(conn.execute(
            "SELECT COUNT(*) AS n FROM code_edges WHERE workspace = ?",
            (root,)).fetchone()["n"])


# ── reading ────────────────────────────────────────────────────────────────

def _scope(workspace: Any, project_id: Any, *, alias: str = "") -> Tuple[str, List[Any]]:
    """The isolation clause, written once (rule 7).

    An empty workspace means "every workspace in this store", which is what a
    diagnostic wants; a named one is an exact match on the normalised path, so
    two checkouts of the same repository never see each other's symbols."""
    prefix = f"{alias}." if alias else ""
    where: List[str] = []
    params: List[Any] = []
    root = _norm_workspace(workspace)
    if root:
        where.append(f"{prefix}workspace = ?")
        params.append(root)
    scope = _text(project_id, limit=128)
    if scope:
        where.append(f"{prefix}project_id = ?")
        params.append(scope)
    return (" AND ".join(where) or "1 = 1"), params


def status(workspace: str, *, project_id: str = "") -> Dict[str, Any]:
    """How much of this workspace is indexed, and how old the answer is."""
    out: Dict[str, Any] = {
        "workspace": _norm_workspace(workspace), "project_id": _text(project_id, limit=128),
        "files": 0, "symbols": 0, "edges": 0, "by_kind": {}, "by_certainty": {},
        "languages": {}, "last_indexed_at": "",
    }
    where, params = _scope(workspace, project_id)
    try:
        with store.db() as conn:
            out["files"] = int(conn.execute(
                f"SELECT COUNT(*) AS n FROM code_files WHERE {where}", params
            ).fetchone()["n"])
            out["symbols"] = int(conn.execute(
                f"SELECT COUNT(*) AS n FROM code_symbols WHERE {where}", params
            ).fetchone()["n"])
            out["edges"] = int(conn.execute(
                f"SELECT COUNT(*) AS n FROM code_edges WHERE {where}", params
            ).fetchone()["n"])
            out["by_kind"] = {str(row["kind"]): int(row["n"]) for row in conn.execute(
                f"SELECT kind, COUNT(*) AS n FROM code_symbols WHERE {where} "
                "GROUP BY kind", params)}
            out["by_certainty"] = {str(row["certainty"]): int(row["n"]) for row in conn.execute(
                f"SELECT certainty, COUNT(*) AS n FROM code_edges WHERE {where} "
                "GROUP BY certainty", params)}
            out["languages"] = {str(row["language"]): int(row["n"]) for row in conn.execute(
                f"SELECT language, COUNT(*) AS n FROM code_files WHERE {where} "
                "GROUP BY language", params)}
            row = conn.execute(
                f"SELECT MAX(indexed_at) AS last FROM code_files WHERE {where}",
                params).fetchone()
            out["last_indexed_at"] = _text(row["last"] if row else "", limit=64)
    except (store.ContextStoreError, sqlite3.Error) as exc:
        logger.warning("code_index.status(%s) failed: %s", workspace, exc)
    return out


#: Search weights.  The name dominates because this index exists to answer
#: "where is X" — a question the vector lane already answers badly — and
#: `freshness` is small on purpose: an old row is a reason to re-read the file,
#: not a reason to hide the symbol.
W_NAME = 0.50
W_PATH = 0.20
W_TEXT = 0.25
W_FRESHNESS = 0.05
FRESHNESS_HALF_LIFE_S = 7.0 * 24 * 3600.0

_SEARCH_ROWS = 2000


def search(query: str, *, workspace: str = "", project_id: str = "", k: int = 12,
           kinds: Sequence[str] = (), now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Symbols that match, most likely first.  Never raises.

    Lexical and deliberately so: this is the lane that has to work when the
    embedding store is missing, and an exact identifier is the query people
    actually type.  Each hit is the symbol's `to_dict()` plus `score` and the
    component `scores`, so the manifest can explain the choice later."""
    tokens = _tokens(query)
    wanted = tuple(kind for kind in (kinds or ()) if kind in SYMBOL_KINDS)
    where, params = _scope(workspace, project_id)
    sql = f"SELECT * FROM code_symbols WHERE {where}"
    if wanted:
        sql += " AND kind IN (" + ",".join("?" * len(wanted)) + ")"
        params = [*params, *wanted]
    probes = sorted(tokens)[:8]
    if probes:
        sql += " AND (" + " OR ".join(
            ["name LIKE ?", "qualname LIKE ?", "path LIKE ?", "summary LIKE ?"] * len(probes)
        ) + ")"
        for probe in probes:
            params.extend([f"%{probe}%"] * 4)
    sql += f" LIMIT {_SEARCH_ROWS}"

    try:
        with store.db() as conn:
            rows = store.rows(conn.execute(sql, params))
    except (store.ContextStoreError, sqlite3.Error) as exc:
        logger.warning("code_index.search failed: %s", exc)
        return []

    raw = _text(query, limit=512).lower()
    hits: List[Tuple[float, Dict[str, Any]]] = []
    for row in rows:
        symbol = Symbol.parse(row)
        name = symbol.name().lower()
        if raw and (raw == symbol.qualname.lower() or raw == name):
            name_score = 1.0
        else:
            name_score = _coverage(tokens, _tokens(symbol.qualname))
        path_score = _coverage(tokens, _tokens(symbol.path))
        text_score = _coverage(tokens, _tokens(f"{symbol.signature} {symbol.summary}"))
        age = store.age_seconds(symbol.indexed_at, now=now)
        freshness = 0.5 if age is None else 0.5 ** (age / FRESHNESS_HALF_LIFE_S)
        scores = {
            "name": round(name_score, 4), "path": round(path_score, 4),
            "text": round(text_score, 4), "freshness": round(freshness, 4),
        }
        total = round(W_NAME * name_score + W_PATH * path_score
                      + W_TEXT * text_score + W_FRESHNESS * freshness, 6)
        if total <= 0.0:
            continue
        hits.append((total, {**symbol.to_dict(), "score": total, "scores": scores}))
    hits.sort(key=lambda item: (-item[0], item[1]["path"], item[1]["start_line"]))
    try:
        limit = int(k)
    except (TypeError, ValueError):
        limit = 12
    return [hit for _, hit in hits[:max(0, limit)]]


def symbols_in(path: str, *, workspace: str = "", project_id: str = "") -> List[Symbol]:
    """Everything defined in one file, in the order it appears.

    The outline the agent reads before deciding which range to open — and the
    reason `refresh` stores a line range per symbol rather than a chunk."""
    rel = _text(path, limit=1024).replace("\\", "/").strip("/")
    if not rel:
        return []
    root = _norm_workspace(workspace)
    if root and os.path.isabs(rel):
        rel = _rel(root, rel)
    where, params = _scope(workspace, project_id)
    try:
        with store.db() as conn:
            rows = store.rows(conn.execute(
                f"SELECT * FROM code_symbols WHERE {where} AND path = ? "
                "ORDER BY start_line, qualname", [*params, rel]))
    except (store.ContextStoreError, sqlite3.Error) as exc:
        logger.warning("code_index.symbols_in(%s) failed: %s", rel, exc)
        return []
    return [Symbol.parse(row) for row in rows]


def neighbors(symbol_id: str, *, kinds: Sequence[str] = (), hops: int = 1) -> List[Dict[str, Any]]:
    """What this symbol is connected to, with the certainty of every hop.

    Breadth-first, `hops` deep, both directions.  Each row carries `direction`
    (`out` when this symbol is the source), the edge `kind`, its `certainty`
    and, when the other end is a symbol this index holds, its path and line
    range.  An unresolved target (`module:fastapi`) comes back with
    `resolved: False` and no range — which is the honest answer, and much more
    useful than dropping the edge and implying the dependency is not there."""
    start = _text(symbol_id, limit=64)
    if not start:
        return []
    wanted = tuple(kind for kind in (kinds or ()) if kind in EDGE_KINDS)
    try:
        depth = max(1, int(hops))
    except (TypeError, ValueError):
        depth = 1

    out: List[Dict[str, Any]] = []
    try:
        with store.db() as conn:
            frontier = [start]
            visited = {start}
            reported: Set[Tuple[str, str, str]] = set()
            for hop in range(1, depth + 1):
                if not frontier:
                    break
                marks = ",".join("?" * len(frontier))
                clause = ""
                params: List[Any] = list(frontier)
                if wanted:
                    clause = " AND kind IN (" + ",".join("?" * len(wanted)) + ")"
                    params.extend(wanted)
                rows = store.rows(conn.execute(
                    f"SELECT src, dst, kind, certainty, detail FROM code_edges "
                    f"WHERE src IN ({marks}){clause}", params))
                params = list(frontier) + (list(wanted) if wanted else [])
                rows += [{**row, "_incoming": True} for row in store.rows(conn.execute(
                    f"SELECT src, dst, kind, certainty, detail FROM code_edges "
                    f"WHERE dst IN ({marks}){clause}", params))]

                nxt: List[str] = []
                for row in rows:
                    incoming = bool(row.get("_incoming"))
                    other = str(row["src"] if incoming else row["dst"])
                    seen_key = (other, str(row["kind"]), "in" if incoming else "out")
                    if seen_key in reported:
                        continue
                    reported.add(seen_key)
                    # Expansion is per symbol, reporting is per edge: a module
                    # that both `imports` and `tests` another one has two
                    # relations worth showing and one node worth walking into.
                    if other not in visited:
                        visited.add(other)
                        nxt.append(other)
                    entry = {
                        "symbol_id": other,
                        "direction": "in" if incoming else "out",
                        "edge_kind": str(row["kind"]),
                        "certainty": str(row["certainty"]),
                        "detail": str(row["detail"] or ""),
                        "hops": hop,
                        "resolved": False,
                        "qualname": other, "path": "", "kind": "",
                        "start_line": 0, "end_line": 0,
                    }
                    out.append(entry)
                frontier = nxt

            # One resolution pass at the end rather than one per hop: the same
            # symbol can be reached twice by two different kinds of edge, and
            # only the first of those was ever in a frontier.
            ids = sorted({entry["symbol_id"] for entry in out})
            found: Dict[str, Any] = {}
            for begin in range(0, len(ids), 400):
                chunk = ids[begin:begin + 400]
                marks = ",".join("?" * len(chunk))
                for row in conn.execute(
                        f"SELECT id, qualname, kind, path, start_line, end_line "
                        f"FROM code_symbols WHERE id IN ({marks})", chunk):
                    found[str(row["id"])] = row
            for entry in out:
                row = found.get(entry["symbol_id"])
                if row is None:
                    continue
                entry.update({
                    "resolved": True, "qualname": str(row["qualname"]),
                    "kind": str(row["kind"]), "path": str(row["path"]),
                    "start_line": int(row["start_line"]),
                    "end_line": int(row["end_line"]),
                })
    except (store.ContextStoreError, sqlite3.Error) as exc:
        logger.warning("code_index.neighbors(%s) failed: %s", start, exc)
        return []
    return out


# ── handing them to the compiler (§10.5) ───────────────────────────────────

#: `observed` because the row is a mechanical reading of bytes that were on
#: disk — `observed_at` says when — and `inference` because a static reading of
#: the repository must never win a contradiction against the file as it is
#: right now.  That asymmetry is §10.5's rule made mechanical: the index guides
#: the agent to the range, and the edit is still made against the current file.
_TRUST_CLASS = "observed"
_AUTHORITY = "inference"


def as_candidates(hits: Iterable[Any], *, workspace: str = "") -> List[ContextCandidate]:
    """Search hits as `ContextCandidate`s: signature, range and summary only.

    Never the file body.  A code map that inlines four functions has spent the
    budget the agent needed to read the one function it is about to change, and
    `source_ref` (`symbol:<path>#L<start>-L<end>`) is there so it can open
    exactly that range with a tool when it decides to."""
    root = _norm_workspace(workspace)
    out: List[ContextCandidate] = []
    for hit in hits or ():
        try:
            data = hit.to_dict() if isinstance(hit, Symbol) else dict(hit)
            symbol = Symbol.parse(data)
            if not symbol.path:
                continue
            scores = {str(name): float(value)
                      for name, value in dict(data.get("scores") or {}).items()
                      if isinstance(value, (int, float)) and not isinstance(value, bool)}
            if isinstance(data.get("score"), (int, float)) and not isinstance(data.get("score"), bool):
                scores["total"] = float(data["score"])
            body = symbol.signature
            if symbol.summary:
                body = f"{body}\n{symbol.summary}" if body else symbol.summary
            out.append(ContextCandidate(
                candidate_id=new_id("ctxcand"),
                source_type="symbol",
                source_ref=symbol.source_ref(),
                title=f"{symbol.qualname} ({symbol.kind})"[:512],
                body=body,
                section="code_map",
                lanes=("lexical",),
                scores=scores,
                trust_class=_TRUST_CLASS,
                authority=_AUTHORITY,
                # The file hash is the revision: it is what an excerpt or an
                # experience is invalidated against when the file changes.
                source_revision=symbol.file_hash,
                observed_at=symbol.indexed_at,
                owner="",
                project_id=symbol.project_id,
                degraded=False,
                meta={
                    "kind": symbol.kind, "language": symbol.language,
                    "path": symbol.path, "qualname": symbol.qualname,
                    "start_line": symbol.start_line, "end_line": symbol.end_line,
                    "content_hash": symbol.content_hash,
                    "symbol_id": symbol.id,
                    "workspace": symbol.workspace or root,
                    "retrieval": "signature_only",
                },
            ))
        except Exception as exc:  # noqa: BLE001 - one bad hit costs one candidate
            logger.warning("code_index.as_candidates skipped a hit: %s", exc)
    return out


# ── invalidation ───────────────────────────────────────────────────────────

def invalidate(workspace: str, paths: Sequence[str] = ()) -> int:
    """Forget the stored hashes so the next refresh reindexes those files.

    Deliberately not a delete.  Between "this might be stale" and "the next
    refresh has run", an agent asking where a function lives is better served
    by a slightly old answer with a line range it can check than by nothing at
    all — and the symbols are replaced wholesale the moment the file is read
    again.  With no `paths`, every file in the workspace is invalidated.
    Returns how many file rows were touched."""
    root = _norm_workspace(workspace)
    if not root:
        return 0
    wanted = [rel for rel in
              (_text(p, limit=1024).replace("\\", "/").strip("/") for p in (paths or ()))
              if rel]
    try:
        with store.db() as conn:
            if wanted:
                marks = ",".join("?" * len(wanted))
                cursor = conn.execute(
                    f"UPDATE code_files SET file_hash = '' "
                    f"WHERE workspace = ? AND path IN ({marks})", [root, *wanted])
            else:
                cursor = conn.execute(
                    "UPDATE code_files SET file_hash = '' WHERE workspace = ?", (root,))
            return int(cursor.rowcount or 0)
    except (store.ContextStoreError, sqlite3.Error) as exc:
        logger.warning("code_index.invalidate(%s) failed: %s", root, exc)
        return 0


def drop(workspace: str) -> int:
    """Delete everything indexed for one workspace; returns rows removed.

    The rebuild-not-migrate property the store was chosen for: a workspace that
    was moved, renamed or is simply no longer interesting costs one refresh to
    come back."""
    root = _norm_workspace(workspace)
    if not root:
        return 0
    try:
        with store.db() as conn:
            removed = 0
            for table in ("code_edges", "code_symbols", "code_files"):
                cursor = conn.execute(f"DELETE FROM {table} WHERE workspace = ?", (root,))
                removed += int(cursor.rowcount or 0)
            return removed
    except (store.ContextStoreError, sqlite3.Error) as exc:
        logger.warning("code_index.drop(%s) failed: %s", root, exc)
        return 0


__all__ = [
    "SYMBOL_KINDS", "EDGE_KINDS", "CERTAINTY",
    "MAX_FILE_BYTES", "DEFAULT_BUDGET_FILES",
    "W_NAME", "W_PATH", "W_TEXT", "W_FRESHNESS", "FRESHNESS_HALF_LIFE_S",
    "Symbol", "Edge",
    "refresh", "status", "search", "symbols_in", "neighbors", "as_candidates",
    "invalidate", "drop",
]
