"""src/code_index.py — find_symbol / callers / tests_for without reading the repo.

The failure this closes is QA-02 (docs/spec/v2/acceptance_scenarios.json):
"Repositorio con vendor, binarios y codigo propio; pedir un bug localizado" ->
"Indice respeta exclusiones y devuelve definicion/callers/tests sin cargar el
repositorio entero." Before this module the only way to answer "where is
`refresh_oauth_token` defined" or "who calls it" was to read files one at a
time and hope, which is exactly what the acceptance text rules out.

What this is, and what it deliberately is not
-----------------------------------------------
This is a small, self-contained incremental index — one file, one read, two
tables (definitions and lexical call sites) — built specifically for the
agent's read-only navigation tools (`find_symbol`, `symbol_references`,
`tests_for`; see src/agent_tools/code_tools.py). It is *not* a second copy of
`src/context_engine/code_index.py`, which stays the authority for the
Context Engine's resolved call graph (AST-level `imports`/`calls`/`tests`
edges with a certainty label, feeding the packet compiler). The two answer
different questions with different cost models: that module trusts only
exact, resolved edges; this one answers "does the text `foo(` appear
anywhere", which is cheaper, language-agnostic and exactly what "callers
lexicos con linea" (Lote 38 spec) asks for. Reusing its private extractors
would have been reasonable if this needed the same shape of answer — it does
not, and the two are kept apart so that changing one's resolution semantics
never quietly changes the other's.

What *is* reused is the storage authority both modules share:
`src.context_engine.store` (one SQLite file under DATA_DIR, WAL, corruption
quarantine, `register_schema()` applied on every connection) — the same
policy `src/context_engine/code_index.py` and half a dozen other Context
Engine modules already build on. Adding two tables here costs nothing that
authority was not already paying for, and avoids inventing a second SQLite
file for the same kind of "derived, rebuildable, delete-and-recompute" data
`context_engine/store.py`'s own docstring describes. Symbol extraction for
languages other than Python reuses `src.repo_map` (`lang_for_path`,
`symbol_lines`) — the one place per-language regexes already live; Python
gets its own compact `ast` pass here because this module needs an accurate
end line per definition, which `repo_map`'s private one-line-per-file
summariser does not compute (it only needs a start line to point a reader
at).

Exclusion policy (the actual point of QA-02)
---------------------------------------------
`iter_candidates()` walks the workspace itself, rather than delegating to
`src.index_walk` (whose default prune list — node_modules/__pycache__/venv —
is deliberately small for the personal-document indexers it serves and does
not cover `vendor/`, `dist/` or `build/`, all three of which QA-02's own
stimulus names). It prunes, in order: dot-directories, `DEFAULT_EXCLUDED_DIRS`,
then `.gitignore` and `.faustusignore` patterns (parsed once per call, at the
workspace root); a candidate file is then dropped by extension
(`BINARY_EXTS`), by size (`MAX_FILE_BYTES`, the lote's 2 MB ceiling) and,
last, by `repo_map.lang_for_path` returning no extractor for it. None of
those checks opens the file — only `os.stat` — so a vendored or binary file
is never read, which is the property tests/qa/test_qa_02_proyecto_grande.py
proves with a spy on `open()`.

`refresh()` is incremental the same way `context_engine/code_index.py` is: a
file whose sha256 has not changed since the last pass is neither reopened
nor reparsed, and the whole candidate list is walked as a generator so a
monorepo's directory tree is never held in memory at once — only the
(bounded, `budget_files`-capped) list of paths still worth indexing.
"""

from __future__ import annotations

import ast
import fnmatch
import hashlib
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional, Sequence, Set, Tuple

from src.context_engine import store
from src.contracts import EvidenceLocator, EvidenceRef, now_iso
from src.repo_map import lang_for_path, symbol_lines

logger = logging.getLogger(__name__)

#: Optional, workspace-root-only ignore file (Lote 38 spec item 1). Same
#: syntax as `.gitignore`, parsed by the same `_parse_ignore_file`.
IGNORE_FILENAME = ".faustusignore"

#: The lote's own ceiling: a source file over 2 MB is treated the same as a
#: binary — worth skipping, never worth parsing for a symbol index.
MAX_FILE_BYTES = 2 * 1024 * 1024

#: How many candidate files one `refresh()` will index before it stops and
#: reports `truncated`. A monorepo gets an honest partial answer instead of a
#: refresh that never returns.
DEFAULT_BUDGET_FILES = 20_000

#: Directories `index_walk.EXCLUDED_DIR_NAMES` does not cover and QA-02's own
#: stimulus explicitly names (vendor, binaries "and code of its own" — the
#: exclusions have to survive a project that vendors dependencies or ships
#: build output next to source). Matched case-insensitively, same as
#: `index_walk`, for a vendored dependency directory capitalised on a
#: case-insensitive filesystem.
DEFAULT_EXCLUDED_DIRS: frozenset = frozenset({
    "node_modules", "__pycache__", "venv", ".venv", "env",
    "dist", "build", "vendor", "target", "out",
    ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "site-packages", "egg-info",
})

#: Extensions that are binary regardless of what `repo_map.lang_for_path`
#: would say about a same-named text file — checked before a file is ever
#: opened, which is what keeps the "vendor/binaries" half of QA-02 true.
BINARY_EXTS: frozenset = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".tiff", ".webp", ".svgz",
    ".pdf", ".zip", ".gz", ".tar", ".tgz", ".bz2", ".xz", ".7z", ".rar",
    ".exe", ".dll", ".so", ".dylib", ".bin", ".o", ".a", ".class", ".jar",
    ".pyc", ".pyo", ".pyd", ".whl", ".egg",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".mp3", ".mp4", ".mov", ".avi", ".mkv", ".wav", ".flac", ".ogg", ".webm",
    ".db", ".sqlite", ".sqlite3", ".wasm",
    ".psd", ".ai", ".sketch", ".fig",
})

#: Identifiers a `name(` match should not be reported as calling — control
#: flow and declaration keywords across the languages `repo_map` extracts,
#: the same purpose `repo_map._REGEX_NON_SYMBOLS` serves for definitions.
_NON_CALL_NAMES: frozenset = frozenset({
    "if", "for", "while", "switch", "return", "catch", "function", "else",
    "elif", "except", "with", "def", "class", "lambda", "yield", "try",
    "finally", "raise", "assert", "del", "pass", "break", "continue",
    "import", "from", "as", "new", "typeof", "instanceof", "void",
    "await", "async", "and", "or", "not", "in", "is",
})

_CALL_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")

#: A test file, by path convention — same shape as
#: `context_engine/code_index.py::_TEST_PATH_RE`, duplicated rather than
#: imported because that one is private to a module this one does not import
#: (see module docstring: the two indexes are kept apart on purpose).
_TEST_PATH_RE = re.compile(
    r"(?:^|/)(?:tests?|__tests__|spec|specs)(?:/|$)"
    r"|(?:^|/)test_[^/]*$|_test\.\w+$|\.(?:test|spec)\.\w+$", re.I)

store.register_schema("code_index_tools", (
    """
    CREATE TABLE IF NOT EXISTS ci_files (
        workspace  TEXT NOT NULL,
        project_id TEXT NOT NULL DEFAULT '',
        path       TEXT NOT NULL,
        file_hash  TEXT NOT NULL DEFAULT '',
        language   TEXT NOT NULL DEFAULT '',
        indexed_at TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (workspace, project_id, path)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_ci_files_scope ON ci_files(workspace, project_id)",
    """
    CREATE TABLE IF NOT EXISTS ci_symbols (
        id         TEXT PRIMARY KEY,
        workspace  TEXT NOT NULL,
        project_id TEXT NOT NULL DEFAULT '',
        path       TEXT NOT NULL,
        name       TEXT NOT NULL,
        qualname   TEXT NOT NULL,
        kind       TEXT NOT NULL DEFAULT 'function',
        start_line INTEGER NOT NULL DEFAULT 0,
        end_line   INTEGER NOT NULL DEFAULT 0,
        file_hash  TEXT NOT NULL DEFAULT '',
        language   TEXT NOT NULL DEFAULT '',
        indexed_at TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_ci_symbols_scope ON ci_symbols(workspace, project_id)",
    "CREATE INDEX IF NOT EXISTS ix_ci_symbols_name ON ci_symbols(workspace, project_id, name)",
    "CREATE INDEX IF NOT EXISTS ix_ci_symbols_path ON ci_symbols(workspace, project_id, path)",
    """
    CREATE TABLE IF NOT EXISTS ci_refs (
        workspace  TEXT NOT NULL,
        project_id TEXT NOT NULL DEFAULT '',
        path       TEXT NOT NULL,
        name       TEXT NOT NULL,
        line       INTEGER NOT NULL DEFAULT 0,
        file_hash  TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_ci_refs_scope ON ci_refs(workspace, project_id, name)",
    "CREATE INDEX IF NOT EXISTS ix_ci_refs_path ON ci_refs(workspace, project_id, path)",
))


# ── normalisation ────────────────────────────────────────────────────────

def _text(value: Any, *, limit: int = 1024) -> str:
    try:
        out = "" if value is None else str(value)
    except Exception:  # noqa: BLE001 - a normaliser never raises
        return ""
    return out.strip()[:limit]


def _norm_workspace(value: Any) -> str:
    raw = _text(value, limit=1024)
    if not raw:
        return ""
    try:
        return os.path.normpath(os.path.realpath(os.path.expanduser(raw)))
    except OSError:
        return os.path.normpath(raw)


def _rel(root: str, abs_path: str) -> str:
    try:
        rel = os.path.relpath(abs_path, root)
    except (OSError, ValueError):
        return ""
    rel = rel.replace("\\", "/")
    if rel.startswith("../") or rel == "..":
        return ""
    return rel


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:32]


def _symbol_id(workspace: str, project_id: str, path: str, qualname: str, kind: str) -> str:
    blob = "\x00".join((workspace, project_id, path, qualname, kind)).encode("utf-8", "replace")
    return "cisym_" + hashlib.sha256(blob).hexdigest()[:24]


# ── exclusion policy: .gitignore / .faustusignore ──────────────────────────

@dataclass(frozen=True)
class _IgnoreRule:
    pattern: str
    negate: bool
    dir_only: bool
    anchored: bool


def _parse_ignore_file(path: str) -> List[_IgnoreRule]:
    """One `.gitignore`/`.faustusignore` file's rules, in file order.

    A minimal but faithful-enough subset: comments, blank lines, `!negation`,
    a trailing `/` for directory-only, a leading `/` to anchor at the
    workspace root. Good enough for the patterns real repos actually write
    (`*.log`, `/build`, `dist/`, `**/*.min.js`) without adding a dependency
    this repo does not already have."""
    rules: List[_IgnoreRule] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return rules
    for raw in lines:
        line = raw.strip("\n")
        if not line.strip() or line.strip().startswith("#"):
            continue
        negate = line.startswith("!")
        if negate:
            line = line[1:]
        dir_only = line.endswith("/") and not line.endswith("\\/")
        if dir_only:
            line = line[:-1]
        anchored = line.startswith("/")
        if anchored:
            line = line[1:]
        if not line:
            continue
        rules.append(_IgnoreRule(pattern=line, negate=negate, dir_only=dir_only, anchored=anchored))
    return rules


def _load_ignore_rules(root: str) -> List[_IgnoreRule]:
    rules = _parse_ignore_file(os.path.join(root, ".gitignore"))
    rules += _parse_ignore_file(os.path.join(root, IGNORE_FILENAME))
    return rules


def _rule_matches(rule: _IgnoreRule, rel_path: str, is_dir: bool) -> bool:
    if rule.dir_only and not is_dir:
        return False
    name = rel_path.rsplit("/", 1)[-1]
    if rule.anchored:
        return fnmatch.fnmatch(rel_path, rule.pattern)
    if "/" in rule.pattern:
        return fnmatch.fnmatch(rel_path, rule.pattern) or fnmatch.fnmatch(rel_path, "*/" + rule.pattern)
    return fnmatch.fnmatch(name, rule.pattern)


def _ignored(rules: Sequence[_IgnoreRule], rel_path: str, is_dir: bool) -> bool:
    """Last matching rule wins (git's own semantics) — a later `!keep.log`
    un-ignores an earlier `*.log`."""
    verdict = False
    for rule in rules:
        if _rule_matches(rule, rel_path, is_dir):
            verdict = not rule.negate
    return verdict


# ── the walk: candidates only, nothing opened yet ───────────────────────────

def iter_candidates(root: str) -> Iterator[str]:
    """Workspace-relative paths worth indexing, streamed one at a time.

    Every exclusion here is decided from the path and, at most, `os.stat` —
    never from the file's content, so a vendored or oversized file is never
    opened. Directories are pruned before `os.walk` descends into them, so a
    `vendor/` with a thousand files costs one `os.walk` skip, not a thousand
    stats."""
    rules = _load_ignore_rules(root)
    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        rel_dir = "" if dirpath == root else _rel(root, dirpath)
        kept: List[str] = []
        for d in sorted(dirnames):
            if d.startswith("."):
                continue
            if d.lower() in DEFAULT_EXCLUDED_DIRS:
                continue
            child_rel = f"{rel_dir}/{d}" if rel_dir else d
            if _ignored(rules, child_rel, True):
                continue
            kept.append(d)
        dirnames[:] = kept
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext in BINARY_EXTS:
                continue
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if _ignored(rules, rel, False):
                continue
            if lang_for_path(rel) == "none":
                continue
            try:
                size = os.stat(os.path.join(dirpath, name)).st_size
            except OSError:
                continue
            if size > MAX_FILE_BYTES:
                continue
            yield rel


# ── extraction: Python via ast, everything else via repo_map ───────────────

# (name, qualname, kind, start_line, end_line)
_Definition = Tuple[str, str, str, int, int]


def _py_definitions(text: str) -> Optional[List[_Definition]]:
    """Top-level classes/functions/methods/constants, with an accurate end
    line for each — the one thing `repo_map`'s private extractor does not
    compute, because a one-line-per-file summary never needed it. Returns
    None on a parse failure so the caller falls back to `repo_map.symbol_lines`,
    the same fallback `repo_map` itself uses."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return None
    out: List[_Definition] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            end = int(getattr(node, "end_lineno", node.lineno) or node.lineno)
            out.append((node.name, node.name, "class", node.lineno, end))
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    cend = int(getattr(child, "end_lineno", child.lineno) or child.lineno)
                    out.append((child.name, f"{node.name}.{child.name}", "method",
                                child.lineno, cend))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = int(getattr(node, "end_lineno", node.lineno) or node.lineno)
            out.append((node.name, node.name, "function", node.lineno, end))
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.isupper() and len(target.id) > 1:
                    end = int(getattr(node, "end_lineno", node.lineno) or node.lineno)
                    out.append((target.id, target.id, "constant", node.lineno, end))
    return out


def _lexical_kind(name: str) -> str:
    if name.isupper() and len(name) > 1:
        return "constant"
    return "class" if name[:1].isupper() else "function"


def _lexical_definitions(text: str, lang: str) -> List[_Definition]:
    """The other languages, through `repo_map.symbol_lines` — the one place
    per-language regexes already live (see module docstring). It gives a
    name and a start line; the end line is approximated as the line before
    the next symbol, or end of file for the last one."""
    lines = text.split("\n")
    found = sorted(
        ((name, line) for name, line in symbol_lines(text, lang)
         if name and 1 <= line <= len(lines)),
        key=lambda pair: (pair[1], pair[0]))
    out: List[_Definition] = []
    for position, (name, line) in enumerate(found):
        end = found[position + 1][1] - 1 if position + 1 < len(found) else len(lines)
        end = max(line, end)
        out.append((name, name, _lexical_kind(name), line, end))
    return out


def _lexical_refs(text: str) -> List[Tuple[str, int]]:
    """Every `name(` call-site in the text, with its 1-based line — the
    "callers lexicos con linea" the lote spec asks for. A dotted call like
    `self.refresh_oauth_token(...)` is captured by its bare method name,
    which is what a caller-by-name lookup wants and what
    `context_engine/code_index.py::_resolve` also matches calls by."""
    out: List[Tuple[str, int]] = []
    for lineno, line in enumerate(text.split("\n"), start=1):
        for match in _CALL_RE.finditer(line):
            name = match.group(1)
            if name in _NON_CALL_NAMES:
                continue
            out.append((name, lineno))
    return out


def _extract(text: str, lang: str) -> Tuple[List[_Definition], List[Tuple[str, int]]]:
    refs = _lexical_refs(text)
    if lang == "py":
        defs = _py_definitions(text)
        if defs is not None:
            return defs, refs
        logger.debug("code_index: python source did not parse; using the lexical extractor")
    return _lexical_definitions(text, lang), refs


# ── reading, one file at a time ─────────────────────────────────────────────

def _read_hashed(abs_path: str) -> Optional[Tuple[str, str]]:
    """`(text, file_hash)`, or None when the file cannot/should not be read.

    The hash covers the raw bytes, before decoding, for the same reason
    `context_engine/code_index.py` hashes before decoding: a file whose only
    change is its encoding really did change."""
    try:
        with open(abs_path, "rb") as handle:
            raw = handle.read(MAX_FILE_BYTES + 1)
    except OSError:
        return None
    if len(raw) > MAX_FILE_BYTES:
        return None
    if b"\x00" in raw[:4096]:
        # A binary that slipped past the extension check (a renamed binary,
        # or one this module's BINARY_EXTS does not list yet).
        return None
    return raw.decode("utf-8", "replace"), _hash(raw)


def _drop_file(conn: sqlite3.Connection, workspace: str, project_id: str, rel: str) -> None:
    conn.execute("DELETE FROM ci_symbols WHERE workspace = ? AND project_id = ? AND path = ?",
                 (workspace, project_id, rel))
    conn.execute("DELETE FROM ci_refs WHERE workspace = ? AND project_id = ? AND path = ?",
                 (workspace, project_id, rel))
    conn.execute("DELETE FROM ci_files WHERE workspace = ? AND project_id = ? AND path = ?",
                 (workspace, project_id, rel))


def _write_file(conn: sqlite3.Connection, workspace: str, project_id: str, rel: str,
                file_hash: str, lang: str, defs: Sequence[_Definition],
                refs: Sequence[Tuple[str, int]], indexed_at: str) -> None:
    conn.execute("DELETE FROM ci_symbols WHERE workspace = ? AND project_id = ? AND path = ?",
                 (workspace, project_id, rel))
    conn.execute("DELETE FROM ci_refs WHERE workspace = ? AND project_id = ? AND path = ?",
                 (workspace, project_id, rel))
    for name, qualname, kind, start, end in defs:
        symbol_id = _symbol_id(workspace, project_id, rel, qualname, kind)
        conn.execute(
            "INSERT OR REPLACE INTO ci_symbols "
            "(id, workspace, project_id, path, name, qualname, kind, start_line, end_line, "
            "file_hash, language, indexed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (symbol_id, workspace, project_id, rel, name, qualname, kind, start, end,
             file_hash, lang, indexed_at))
    if refs:
        conn.executemany(
            "INSERT INTO ci_refs (workspace, project_id, path, name, line, file_hash) "
            "VALUES (?,?,?,?,?,?)",
            [(workspace, project_id, rel, name, line, file_hash) for name, line in refs])
    conn.execute(
        "INSERT OR REPLACE INTO ci_files (workspace, project_id, path, file_hash, language, indexed_at) "
        "VALUES (?,?,?,?,?,?)", (workspace, project_id, rel, file_hash, lang, indexed_at))


# ── the public surface ──────────────────────────────────────────────────────

def refresh(workspace: str, *, project_id: str = "", full: bool = False,
           budget_files: int = DEFAULT_BUDGET_FILES) -> Dict[str, Any]:
    """Bring the index up to date and say what that cost.

    Incremental unless `full`: a file whose hash matches the one already
    stored is neither opened nor reparsed. Returns
    `{"scanned", "reindexed", "removed", "symbols", "refs", "elapsed_ms",
    "truncated"}`. Never raises — a store error is logged and answered with
    zeros, the same degrade-not-break contract `context_engine` modules use
    on the turn path."""
    started = time.time()
    out: Dict[str, Any] = {"scanned": 0, "reindexed": 0, "removed": 0,
                           "symbols": 0, "refs": 0, "elapsed_ms": 0, "truncated": False}
    root = _norm_workspace(workspace)
    if not root or not os.path.isdir(root):
        out["elapsed_ms"] = int((time.time() - started) * 1000)
        return out
    scope = _text(project_id, limit=128)
    budget = max(1, int(budget_files or DEFAULT_BUDGET_FILES))

    candidates: List[str] = []
    truncated = False
    for rel in iter_candidates(root):
        candidates.append(rel)
        if len(candidates) >= budget:
            truncated = True
            break
    out["scanned"] = len(candidates)
    out["truncated"] = truncated
    indexed_at = now_iso()

    try:
        with store.db() as conn:
            known = {row["path"]: row["file_hash"] for row in conn.execute(
                "SELECT path, file_hash FROM ci_files WHERE workspace = ? AND project_id = ?",
                (root, scope))}
            seen: Set[str] = set()
            for rel in candidates:
                abs_path = os.path.join(root, *rel.split("/"))
                source = _read_hashed(abs_path)
                if source is None:
                    if rel in known:
                        _drop_file(conn, root, scope, rel)
                        out["removed"] += 1
                    continue
                seen.add(rel)
                text, file_hash = source
                if not full and known.get(rel) == file_hash:
                    continue
                lang = lang_for_path(rel)
                defs, refs = _extract(text, lang)
                _write_file(conn, root, scope, rel, file_hash, lang, defs, refs, indexed_at)
                out["reindexed"] += 1
            if not truncated:
                # A truncated walk never looked at the rest of the tree, so
                # only a complete one may conclude a known file is gone.
                for rel in list(known):
                    if rel not in seen:
                        _drop_file(conn, root, scope, rel)
                        out["removed"] += 1
            out["symbols"] = int(conn.execute(
                "SELECT COUNT(*) AS n FROM ci_symbols WHERE workspace = ? AND project_id = ?",
                (root, scope)).fetchone()["n"])
            out["refs"] = int(conn.execute(
                "SELECT COUNT(*) AS n FROM ci_refs WHERE workspace = ? AND project_id = ?",
                (root, scope)).fetchone()["n"])
    except (store.ContextStoreError, sqlite3.Error) as exc:
        logger.warning("code_index.refresh(%s) failed: %s", root, exc)
    out["elapsed_ms"] = int((time.time() - started) * 1000)
    return out


def status(workspace: str, *, project_id: str = "") -> Dict[str, Any]:
    root = _norm_workspace(workspace)
    scope = _text(project_id, limit=128)
    out: Dict[str, Any] = {"workspace": root, "project_id": scope,
                           "files": 0, "symbols": 0, "refs": 0, "last_indexed_at": ""}
    try:
        with store.db() as conn:
            out["files"] = int(conn.execute(
                "SELECT COUNT(*) AS n FROM ci_files WHERE workspace = ? AND project_id = ?",
                (root, scope)).fetchone()["n"])
            out["symbols"] = int(conn.execute(
                "SELECT COUNT(*) AS n FROM ci_symbols WHERE workspace = ? AND project_id = ?",
                (root, scope)).fetchone()["n"])
            out["refs"] = int(conn.execute(
                "SELECT COUNT(*) AS n FROM ci_refs WHERE workspace = ? AND project_id = ?",
                (root, scope)).fetchone()["n"])
            row = conn.execute(
                "SELECT MAX(indexed_at) AS last FROM ci_files WHERE workspace = ? AND project_id = ?",
                (root, scope)).fetchone()
            out["last_indexed_at"] = _text(row["last"] if row else "", limit=64)
    except (store.ContextStoreError, sqlite3.Error) as exc:
        logger.warning("code_index.status(%s) failed: %s", root, exc)
    return out


def _evidence(path: str, start_line: int, end_line: int, file_hash: str, *,
             project_id: str = "") -> EvidenceRef:
    """§10.5's pointer, in the typed shape `ToolResult`/acceptance criteria
    attach evidence by id with (src/contracts/tool.py::EvidenceRef) — file,
    line range, hash, never the file body."""
    digest = _text(file_hash, limit=64)
    evidence_id = "evi_ci_" + hashlib.sha256(
        f"{path}:{start_line}:{end_line}:{digest}".encode("utf-8", "replace")).hexdigest()[:24]
    return EvidenceRef(
        evidence_id=evidence_id,
        owner_id="code_index",
        project_id=project_id or None,
        source_type="file",
        source_ref=path[:512],
        source_revision=digest,
        content_sha256=None,
        captured_at=now_iso(),
        locator=EvidenceLocator(kind="lines", value=f"{start_line}-{end_line}"),
    )


def find_definition(name: str, *, workspace: str = "", project_id: str = "",
                    kind: str = "") -> List[Dict[str, Any]]:
    """Definition site(s) for an exact bare name or dotted qualname.
    Never raises; an empty list means "not indexed" or "not found"."""
    root = _norm_workspace(workspace)
    scope = _text(project_id, limit=128)
    wanted = _text(name, limit=512)
    if not wanted:
        return []
    sql = ("SELECT * FROM ci_symbols WHERE workspace = ? AND project_id = ? "
           "AND (name = ? OR qualname = ?)")
    params: List[Any] = [root, scope, wanted, wanted]
    wanted_kind = _text(kind, limit=32)
    if wanted_kind:
        sql += " AND kind = ?"
        params.append(wanted_kind)
    sql += " ORDER BY path, start_line"
    try:
        with store.db() as conn:
            rows = store.rows(conn.execute(sql, params))
    except (store.ContextStoreError, sqlite3.Error) as exc:
        logger.warning("code_index.find_definition(%s) failed: %s", wanted, exc)
        return []
    out: List[Dict[str, Any]] = []
    for row in rows:
        path, start, end = str(row["path"]), int(row["start_line"]), int(row["end_line"])
        file_hash = str(row["file_hash"] or "")
        out.append({
            "name": str(row["name"]), "qualname": str(row["qualname"]), "kind": str(row["kind"]),
            "path": path, "start_line": start, "end_line": end,
            "language": str(row["language"]), "file_hash": file_hash,
            "evidence": _evidence(path, start, end, file_hash, project_id=scope).to_mapping(),
        })
    return out


def find_callers(name: str, *, workspace: str = "", project_id: str = "",
                 limit: int = 200) -> List[Dict[str, Any]]:
    """Lexical call sites for `name` (file + line), excluding the definition's
    own line so a symbol is never reported as its own caller."""
    root = _norm_workspace(workspace)
    scope = _text(project_id, limit=128)
    wanted = _text(name, limit=512)
    if not wanted:
        return []
    try:
        cap = max(1, int(limit))
    except (TypeError, ValueError):
        cap = 200
    try:
        with store.db() as conn:
            def_lines = {(str(r["path"]), int(r["start_line"])) for r in conn.execute(
                "SELECT path, start_line FROM ci_symbols "
                "WHERE workspace = ? AND project_id = ? AND name = ?", (root, scope, wanted))}
            rows = store.rows(conn.execute(
                "SELECT path, line, file_hash FROM ci_refs "
                "WHERE workspace = ? AND project_id = ? AND name = ? ORDER BY path, line",
                (root, scope, wanted)))
    except (store.ContextStoreError, sqlite3.Error) as exc:
        logger.warning("code_index.find_callers(%s) failed: %s", wanted, exc)
        return []
    out: List[Dict[str, Any]] = []
    for row in rows:
        path, line = str(row["path"]), int(row["line"])
        if (path, line) in def_lines:
            continue
        file_hash = str(row["file_hash"] or "")
        out.append({
            "path": path, "line": line,
            "evidence": _evidence(path, line, line, file_hash, project_id=scope).to_mapping(),
        })
        if len(out) >= cap:
            break
    return out


def tests_for(symbol_or_path: str, *, workspace: str = "", project_id: str = "") -> List[Dict[str, Any]]:
    """Candidate test files for a symbol name or a file path, by convention:
    `test_<module>` / `tests/**/test_*` that names the module in its own
    filename, or mentions the bare symbol name anywhere in its text (found
    through the same `ci_refs` lexical index `find_callers` reads)."""
    root = _norm_workspace(workspace)
    scope = _text(project_id, limit=128)
    target = _text(symbol_or_path, limit=1024).replace("\\", "/").strip()
    if not target:
        return []

    stems: Set[str] = set()
    symbol_name = ""
    if "/" in target or os.path.splitext(target)[1]:
        rel = target
        if root and os.path.isabs(rel):
            rel = _rel(root, rel)
        if rel:
            stems.add(os.path.splitext(rel.rsplit("/", 1)[-1])[0])
    else:
        symbol_name = target
        stems.add(target)
        for hit in find_definition(target, workspace=workspace, project_id=project_id):
            stems.add(os.path.splitext(hit["path"].rsplit("/", 1)[-1])[0])

    try:
        with store.db() as conn:
            files = conn.execute(
                "SELECT path, file_hash FROM ci_files WHERE workspace = ? AND project_id = ?",
                (root, scope)).fetchall()
            out: List[Dict[str, Any]] = []
            for row in files:
                path = str(row["path"])
                if not _TEST_PATH_RE.search(path):
                    continue
                base = os.path.splitext(path.rsplit("/", 1)[-1])[0].lower()
                name_match = any(stem and stem.lower() in base for stem in stems)
                mention_line: Optional[int] = None
                if not name_match and symbol_name:
                    mention = conn.execute(
                        "SELECT line FROM ci_refs WHERE workspace = ? AND project_id = ? "
                        "AND path = ? AND name = ? ORDER BY line LIMIT 1",
                        (root, scope, path, symbol_name)).fetchone()
                    if mention is not None:
                        mention_line = int(mention["line"])
                if not name_match and mention_line is None:
                    continue
                line = mention_line or 1
                file_hash = str(row["file_hash"] or "")
                out.append({
                    "path": path, "line": line,
                    "reason": "name_convention" if name_match else "mentions_symbol",
                    "evidence": _evidence(path, line, line, file_hash, project_id=scope).to_mapping(),
                })
            out.sort(key=lambda hit: (hit["path"], hit["line"]))
            return out
    except (store.ContextStoreError, sqlite3.Error) as exc:
        logger.warning("code_index.tests_for(%s) failed: %s", target, exc)
        return []


__all__ = [
    "IGNORE_FILENAME", "MAX_FILE_BYTES", "DEFAULT_BUDGET_FILES",
    "DEFAULT_EXCLUDED_DIRS", "BINARY_EXTS",
    "iter_candidates", "refresh", "status",
    "find_definition", "find_callers", "tests_for",
]
