"""repo_map.py — a compact map of the workspace (files + top-level symbols) for the model.

Local models invent paths because they never saw the tree. Aider's answer is
a *repository map*: a ranked, budgeted listing of the files that matter most
for the current request, with the symbols each one defines. Injected once per
turn (before the user's message, as reference data), it turns most of the
glob/grep discovery rounds into zero rounds and makes fabricated paths rare.

What it contains
  * a directory tree (compact, capped per directory);
  * for the highest-ranked source files: `path: class Foo(m1, m2), def bar, …`.

Ranking: files whose name the user mentioned first, then conventional source
folders, shallow paths before deep ones, tests and vendored code last. The
whole thing is bounded by `agent_repo_map_tokens` (~4 chars per token).

Symbol extraction: Python via `ast`; JS/TS/Go/Rust/Java/Kotlin/C#/Ruby/PHP via
regexes on line starts. Per-file results are cached by (path, mtime, size).
Stdlib only, never raises.

The extractors are shared, not copied: `symbol_lines(text, lang)` returns the
same symbols with the line each one starts on, which is what `src/read_plan.py`
shows as the index of a file too big to return whole. One extractor per
language, two renderings — a module summary here, a navigable outline there.
"""
from __future__ import annotations

import ast
import logging
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

DEFAULT_TOKENS = 1500
CHARS_PER_TOKEN = 4
_SYMBOL_CACHE: Dict[str, Tuple[float, int, str]] = {}   # abs path → (mtime, size, rendered)
_SYMBOL_CACHE_MAX = 20_000
_MAP_CACHE: Dict[str, Tuple[float, str, str]] = {}      # root → (built_at, key, text)
_LOCK = threading.Lock()
_MAP_TTL_S = 20.0
_MAX_FILE_BYTES = 400_000

SOURCE_EXTS = {
    ".py": "py", ".pyi": "py",
    ".js": "js", ".mjs": "js", ".cjs": "js", ".jsx": "js", ".ts": "js", ".tsx": "js", ".vue": "js", ".svelte": "js",
    ".go": "go", ".rs": "rs", ".java": "java", ".kt": "java", ".kts": "java", ".cs": "java", ".scala": "java",
    ".rb": "rb", ".php": "php", ".swift": "swift", ".c": "c", ".h": "c", ".cc": "c", ".cpp": "c", ".hpp": "c",
    ".sh": "none", ".ps1": "none", ".sql": "none", ".html": "none", ".css": "none", ".scss": "none",
    ".md": "none", ".json": "none", ".yml": "none", ".yaml": "none", ".toml": "none", ".ini": "none", ".cfg": "none",
}
_LOW_VALUE_NAMES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Cargo.lock", "composer.lock", "go.sum"}
_SOURCE_DIR_BONUS = ("src", "app", "lib", "routes", "services", "core", "api", "server", "static/js", "components", "pages", "cmd", "pkg", "internal")
_TEST_HINT_RE = re.compile(r"(?:^|/)(?:tests?|__tests__|spec|specs|e2e|fixtures?)(?:/|$)|(?:^|/)test_[^/]*$|_test\.\w+$|\.(?:test|spec)\.\w+$", re.I)
_VENDOR_HINT_RE = re.compile(r"(?:^|/)(?:vendor|third_party|thirdparty|external|licenses|assets|docs?|website|migrations?|locales?|i18n)(?:/|$)|\.min\.\w+$", re.I)
_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")

_JS_SYMBOL_RES = (
    re.compile(r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)", re.M),
    re.compile(r"^(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)", re.M),
    re.compile(r"^(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>", re.M),
    re.compile(r"^(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?function\b", re.M),
    re.compile(r"^export\s+(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=", re.M),
    re.compile(r"^(?:export\s+)?(?:interface|type|enum)\s+([A-Za-z_$][\w$]*)", re.M),
)
_GO_RES = (
    re.compile(r"^func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*\(", re.M),
    re.compile(r"^type\s+([A-Za-z_]\w*)\s+(?:struct|interface)", re.M),
)
_RS_RES = (
    re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+([A-Za-z_]\w*)", re.M),
    re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait|type)\s+([A-Za-z_]\w*)", re.M),
    re.compile(r"^impl(?:<[^>]*>)?\s+(?:[A-Za-z_][\w:]*\s+for\s+)?([A-Za-z_]\w*)", re.M),
)
_JAVA_RES = (
    re.compile(r"^\s*(?:public|private|protected|internal)?\s*(?:static\s+)?(?:abstract\s+|final\s+|sealed\s+|data\s+|open\s+)?(?:class|interface|enum|record|object|struct)\s+([A-Za-z_]\w*)", re.M),
    re.compile(r"^\s{1,8}(?:public|private|protected|internal|override|static|fun|def|suspend|async|virtual)\b[^=;(]*?\b([A-Za-z_]\w*)\s*\(", re.M),
)
_RB_RES = (
    re.compile(r"^\s*(?:class|module)\s+([A-Za-z_][\w:]*)", re.M),
    re.compile(r"^\s*def\s+(?:self\.)?([A-Za-z_]\w*[?!=]?)", re.M),
)
_PHP_RES = (
    re.compile(r"^\s*(?:abstract\s+|final\s+)?(?:class|interface|trait|enum)\s+([A-Za-z_]\w*)", re.M),
    re.compile(r"^\s*(?:public|private|protected|static|\s)*function\s+&?([A-Za-z_]\w*)", re.M),
)
_C_RES = (
    re.compile(r"^(?:[A-Za-z_][\w\s\*&:<>,]*?)\b([A-Za-z_]\w*)\s*\([^;]*\)\s*(?:const\s*)?\{", re.M),
    re.compile(r"^\s*(?:typedef\s+)?(?:struct|class|enum|union)\s+([A-Za-z_]\w*)", re.M),
)
_SWIFT_RES = (
    re.compile(r"^\s*(?:public\s+|private\s+|internal\s+|open\s+|fileprivate\s+)?(?:final\s+)?(?:class|struct|enum|protocol|extension)\s+([A-Za-z_]\w*)", re.M),
    re.compile(r"^\s*(?:public\s+|private\s+|internal\s+|open\s+|static\s+|override\s+)*func\s+([A-Za-z_]\w*)", re.M),
)
_LANG_RES = {"js": _JS_SYMBOL_RES, "go": _GO_RES, "rs": _RS_RES, "java": _JAVA_RES, "rb": _RB_RES,
             "php": _PHP_RES, "c": _C_RES, "swift": _SWIFT_RES}


def _setting(key: str, default: Any) -> Any:
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:
        return default


# ---------------------------------------------------------------------------
# Symbols
# ---------------------------------------------------------------------------

_PY_FALLBACK_RES = (
    re.compile(r"^class\s+([A-Za-z_]\w*)", re.M),
    re.compile(r"^(?:async\s+)?def\s+([A-Za-z_]\w*)", re.M),
)
_REGEX_NON_SYMBOLS = ("if", "for", "while", "switch", "return", "catch", "function", "else", "constructor")
_REGEX_LIMIT = 40


def _py_defs(text: str) -> Optional[List[Dict[str, Any]]]:
    """Every top-level definition in a Python source, with its line.

    The single Python extractor: one `ast` pass, two renderers on top of it —
    `_py_symbols` (the repo map's one-line-per-file summary) and
    `_py_symbol_lines` (read_plan's per-file outline). Returns None when the
    source does not parse, so callers fall back to the regex path.

    Records: {"kind": class|def|const, "name", "line"} plus, for a class, its
    "methods" and, for a function, its directly "nested" defs — one level down,
    which is where a module that is mostly one long function keeps its structure
    (src/agent_loop.py holds 4113 of its 7691 lines inside `stream_agent_loop`).
    Nothing is filtered here — each renderer decides what it wants to show.
    """
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return None
    defs: List[Dict[str, Any]] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            methods = [(n.name, n.lineno) for n in node.body
                       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
            defs.append({"kind": "class", "name": node.name, "line": node.lineno, "methods": methods})
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            nested = [(n.name, n.lineno) for n in node.body
                      if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
            defs.append({"kind": "def", "name": node.name, "line": node.lineno,
                         "async": isinstance(node, ast.AsyncFunctionDef), "nested": nested})
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    defs.append({"kind": "const", "name": t.id, "line": node.lineno})
    return defs


def _py_symbols(text: str) -> List[str]:
    """Repo-map rendering: public API of a module, classes with their methods."""
    defs = _py_defs(text)
    if defs is None:
        return _regex_symbols(text, _PY_FALLBACK_RES)
    out: List[str] = []
    consts: List[str] = []
    for d in defs:
        if d["kind"] == "class":
            methods = [n for n, _ in d["methods"] if not n.startswith("__")]
            shown = ", ".join(methods[:8]) + (f", +{len(methods) - 8}" if len(methods) > 8 else "")
            out.append(f"class {d['name']}({shown})" if methods else f"class {d['name']}")
        elif d["kind"] == "def":
            if not d["name"].startswith("_") or d["name"] in ("__init__",):
                out.append(f"def {d['name']}")
        elif d["kind"] == "const":
            name = d["name"]
            if name.isupper() and len(name) > 2 and not name.startswith("_"):
                consts.append(name)
    return out + consts[:6]


def _py_symbol_lines(text: str) -> List[Tuple[str, int]]:
    """Outline rendering: every definition in the file with the line it starts on.

    Same extraction as `_py_symbols`, different question. Navigating *inside* one
    file is not the same job as summarising a module for the repo map: private
    helpers are most of a large module (7 of 8 top-level defs in
    src/agent_loop.py start with "_"), and methods are where the work lives, so
    both are kept here and neither is kept there.
    """
    defs = _py_defs(text)
    if defs is None:
        return _regex_symbol_lines(text, _PY_FALLBACK_RES)
    out: List[Tuple[str, int]] = []
    for d in defs:
        if d["kind"] == "class":
            out.append((f"class {d['name']}", d["line"]))
            for name, line in d["methods"]:
                out.append((f"{d['name']}.{name}", line))
        elif d["kind"] == "def":
            out.append((f"{'async def' if d.get('async') else 'def'} {d['name']}", d["line"]))
            for name, line in d.get("nested", ()):
                out.append((f"{d['name']}.{name}", line))
        elif d["kind"] == "const":
            name = d["name"]
            if name.isupper() and len(name) > 2:
                out.append((name, d["line"]))
    return out


def _regex_matches(text: str, patterns, *, limit: int, include_private: bool) -> List[Tuple[str, int]]:
    """The single regex extractor: (name, 1-based line) for each pattern hit.

    First-wins dedupe by name, patterns applied in order — the order
    `_regex_symbols` has always produced.
    """
    out: List[Tuple[str, int]] = []
    seen: Set[str] = set()
    for pat in patterns:
        for m in pat.finditer(text):
            name = m.group(1)
            if not name or name in seen or (not include_private and name.startswith("_") and name != "__init__"):
                continue
            if name in _REGEX_NON_SYMBOLS:
                continue
            seen.add(name)
            out.append((name, text.count("\n", 0, m.start()) + 1))
            if len(out) >= limit:
                return out
    return out


def _regex_symbols(text: str, patterns) -> List[str]:
    return [name for name, _ in _regex_matches(text, patterns, limit=_REGEX_LIMIT, include_private=False)]


def _dedent_lines(text: str) -> str:
    """The same text with per-line indentation removed; line numbers preserved.

    The language regexes anchor on `^` because the repo map wants *top-level*
    symbols only. A file outline wants the ones inside a wrapper too — 103 of
    static/js/chat.js's 104 function declarations are indented inside the module
    body, so the anchored patterns find one. Running the *same* patterns over a
    dedented view finds them without a second set of regexes; stripping only
    leading whitespace keeps every line at its original index.
    """
    return "\n".join(line.lstrip() for line in text.split("\n"))


def _regex_symbol_lines(text: str, patterns, *, limit: int = 400) -> List[Tuple[str, int]]:
    """Outline rendering for the regex languages: top-level hits plus indented ones."""
    found = _regex_matches(text, patterns, limit=limit, include_private=True)
    seen = {name for name, _ in found}
    for name, line in _regex_matches(_dedent_lines(text), patterns, limit=limit, include_private=True):
        if name not in seen:
            seen.add(name)
            found.append((name, line))
    return found[:limit]


def symbol_lines(text: str, lang: str) -> List[Tuple[str, int]]:
    """(symbol, line) pairs for one source text — the outline `read_plan` shows.

    Pure function on text the caller already has; `lang` is a `SOURCE_EXTS`
    value. Returns [] for "none"/unknown languages (a .log, a .csv, a binary),
    which is what tells read_plan there is no index to give.
    """
    if not text or lang in (None, "", "none"):
        return []
    if lang == "py":
        return _py_symbol_lines(text)
    patterns = _LANG_RES.get(lang)
    if not patterns:
        return []
    return _regex_symbol_lines(text, patterns)


def lang_for_path(path: str) -> str:
    """`SOURCE_EXTS` language for a path ('none' when it has no symbol extractor)."""
    return SOURCE_EXTS.get(os.path.splitext(path)[1].lower(), "none")


def file_symbols(abs_path: str, lang: str) -> str:
    """Rendered symbol list for one file ('' when none / not applicable)."""
    if lang == "none":
        return ""
    try:
        st = os.stat(abs_path)
    except OSError:
        return ""
    key = abs_path
    cached = _SYMBOL_CACHE.get(key)
    if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
        return cached[2]
    if st.st_size > _MAX_FILE_BYTES:
        rendered = ""
    else:
        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read(_MAX_FILE_BYTES)
        except OSError:
            return ""
        if lang == "py":
            syms = _py_symbols(text)
        else:
            syms = _regex_symbols(text, _LANG_RES.get(lang, ()))
        rendered = ", ".join(syms[:30]) + (f", +{len(syms) - 30} more" if len(syms) > 30 else "")
    if len(_SYMBOL_CACHE) >= _SYMBOL_CACHE_MAX:
        _SYMBOL_CACHE.clear()
    _SYMBOL_CACHE[key] = (st.st_mtime, st.st_size, rendered)
    return rendered


# ---------------------------------------------------------------------------
# Ranking + rendering
# ---------------------------------------------------------------------------

def _mentioned_terms(user_text: str) -> Set[str]:
    terms: Set[str] = set()
    for w in _WORD_RE.findall(user_text or ""):
        if len(w) >= 3:
            terms.add(w.lower())
    return terms


def _score(rel: str, terms: Set[str]) -> float:
    low = rel.lower()
    base = low.rsplit("/", 1)[-1]
    stem = base.rsplit(".", 1)[0]
    depth = low.count("/")
    s = 2.0 - min(depth, 6) * 0.35
    if stem in terms or base in terms:
        s += 6.0
    elif any(t in stem for t in terms if len(t) >= 4):
        s += 2.5
    if any(low.startswith(d + "/") or low == d for d in _SOURCE_DIR_BONUS):
        s += 1.0
    if base in ("readme.md", "pyproject.toml", "package.json", "setup.py", "cargo.toml", "go.mod", "app.py", "main.py", "index.js", "index.ts", "server.py", "manage.py"):
        s += 1.5
    if _TEST_HINT_RE.search(low):
        s -= 1.2
    if _VENDOR_HINT_RE.search(low):
        s -= 1.5
    ext = os.path.splitext(base)[1]
    if SOURCE_EXTS.get(ext) == "none":
        s -= 0.8
    if base in _LOW_VALUE_NAMES:
        s -= 5
    return s


def _render_tree(files: List[str], budget_chars: int, max_per_dir: int = 25, max_depth: int = 5) -> str:
    """Directory-first compact tree, capped per directory and in total."""
    tree: Dict[str, Any] = {}
    for rel in files:
        parts = rel.split("/")
        node = tree
        for p in parts[:-1]:
            node = node.setdefault(p + "/", {})
        node[parts[-1]] = None
    lines: List[str] = []
    used = [0]

    def walk(node: Dict[str, Any], indent: str, depth: int) -> None:
        dirs = sorted(k for k, v in node.items() if isinstance(v, dict))
        leafs = sorted(k for k, v in node.items() if v is None)
        entries = dirs + leafs
        shown = 0
        for k in entries:
            if used[0] > budget_chars:
                return
            if shown >= max_per_dir:
                extra = len(entries) - shown
                lines.append(f"{indent}… +{extra} more")
                used[0] += len(indent) + 12
                return
            lines.append(f"{indent}{k}")
            used[0] += len(indent) + len(k) + 1
            shown += 1
            if isinstance(node[k], dict) and depth < max_depth:
                walk(node[k], indent + "  ", depth + 1)

    walk(tree, "", 0)
    return "\n".join(lines)


def build(workspace: str, user_text: str = "", *, max_tokens: Optional[int] = None, max_files: int = 4000) -> str:
    """The map text ('' when disabled / empty). Cached ~20 s per (workspace, request terms)."""
    if not workspace or not bool(_setting("agent_repo_map", True)):
        return ""
    root = os.path.realpath(os.path.expanduser(workspace))
    if not os.path.isdir(root):
        return ""
    try:
        budget_tokens = int(max_tokens or _setting("agent_repo_map_tokens", DEFAULT_TOKENS) or DEFAULT_TOKENS)
    except (TypeError, ValueError):
        budget_tokens = DEFAULT_TOKENS
    budget = max(300, min(budget_tokens, 12000)) * CHARS_PER_TOKEN
    terms = _mentioned_terms(user_text)
    cache_key = f"{budget}|{'|'.join(sorted(terms))[:400]}"
    now = time.time()
    with _LOCK:
        cached = _MAP_CACHE.get(root)
    if cached and cached[1] == cache_key and now - cached[0] < _MAP_TTL_S:
        return cached[2]
    t0 = time.time()
    try:
        from src.agent_harness import workspace_file_index
        files = workspace_file_index(root)
    except Exception as e:
        logger.debug("[repo-map] index failed: %s", e)
        return ""
    if not files:
        return ""
    total = len(files)
    files = [f for f in files if os.path.splitext(f)[1].lower() in SOURCE_EXTS or "/" not in f][:max_files * 4]
    ranked = sorted(files, key=lambda r: -_score(r, terms))
    tree_files = ranked[:max_files]
    tree_budget = int(budget * 0.3)
    tree = _render_tree(sorted(tree_files), tree_budget, max_per_dir=18 if total < 400 else 10,
                        max_depth=5 if total < 400 else 3)
    used = len(tree)
    sym_lines: List[str] = []
    for rel in ranked:
        if used >= budget:
            break
        ext = os.path.splitext(rel)[1].lower()
        lang = SOURCE_EXTS.get(ext)
        if not lang or lang == "none":
            continue
        syms = file_symbols(os.path.join(root, *rel.split("/")), lang)
        if not syms:
            continue
        line = f"{rel}: {syms}"
        if len(line) > 260:
            line = line[:257] + "…"
        sym_lines.append(line)
        used += len(line) + 1
    header = (
        f"Repository map of the workspace ({total} files indexed; vendored/build folders skipped). "
        "Paths are relative to the workspace root. Only files listed here (or returned by ls/glob/grep) exist; "
        "use read_file on a listed path before editing it."
    )
    parts = [header, "", "## Tree", tree]
    if sym_lines:
        parts += ["", "## Symbols (top-level, most relevant files first)", *sym_lines]
    text = "\n".join(parts)
    with _LOCK:
        if len(_MAP_CACHE) > 64:
            _MAP_CACHE.clear()
        _MAP_CACHE[root] = (now, cache_key, text)
    logger.debug("[repo-map] %s: %d files, %d symbol lines, %d chars in %d ms", root, total, len(sym_lines),
                 len(text), int((time.time() - t0) * 1000))
    return text


def invalidate(workspace: Optional[str] = None) -> None:
    with _LOCK:
        if workspace:
            _MAP_CACHE.pop(os.path.realpath(os.path.expanduser(workspace)), None)
        else:
            _MAP_CACHE.clear()
