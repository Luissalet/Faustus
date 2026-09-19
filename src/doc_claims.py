"""src/doc_claims.py — deterministic doc-claim drift checker.

Docs (FAUSTUS.md, README.md, docs/**/*.md) cite code in backticks: file
paths (`src/engine_swap.py`), dotted symbols (`engine_swap.ensure_ready`),
settings keys (`engine_idle_ttl_minutes`), API routes (`/api/engines/swap
/status`) and tool names (`code_graph_impact`). Code moves; docs don't
always follow. This module extracts those claims and grounds each one in
code evidence:

  - **broken**: the thing a claim points at no longer exists (missing file,
    unresolved symbol, unknown settings key, unregistered route, unknown
    tool name).
  - **stale**: the claim still resolves, but the code it references has
    been touched by a commit *after* the doc section that cites it was
    last edited — the doc may no longer describe current behavior.

Precision rules (what keeps this signal usable instead of noisy):

  - A bare snake_case identifier is only a **settings** claim when it has
    >=2 underscores AND its first token is a prefix shared by >=3 real
    DEFAULT_SETTINGS keys (`agent_`, `engine_`, `llm_`, ...) — this is what
    keeps model/Ollama options like `top_k`/`num_ctx`/`repeat_penalty` out
    (1 underscore, excluded outright) and prevents any one-off snake_case
    word from masquerading as a settings key.
  - A dotted **symbol** claim is only checked when its leading module part
    resolves to an actual file under `src/`, `routes/`, `services/` or
    `core/` (an exact dotted path under one of those, or a bare module
    stem found anywhere under `src/`). `os.walk`, `json.dumps`,
    `window.open` and other stdlib/JS APIs never resolve there, so they're
    silently ignored instead of reported broken.
  - A **path** claim is ignored (not checked) when it's gitignored, or
    looks like runtime data (`data/...`, `*-dev-data/...`, `.faustus/...`,
    `%APPDATA%...`, an absolute Windows path) rather than tracked source,
    or its top-level directory doesn't exist in the workspace at all.
  - **FAUSTUS.md** is a dated running log, not living reference docs: a
    finding in a section whose last edit (blame time) is older than
    `since_days` (default 30) is reported separately as **historical** and
    excluded from the main broken/stale counts. README*.md and
    docs/**/*.md are always "current" — they're meant to describe the
    present, not a specific day.
  - **stale** only fires for a referenced *source* file (never the doc
    itself or another `.md` doc) whose newest touching commit is both
    after the section's last edit AND within the last 90 days. Line-level
    precision (`git log -L :<symbol>:<file>`) would be the "exact" option,
    but it forks git once per (symbol, file) pair; over a doc with
    thousands of claims that's minutes, not seconds (this module already
    spends ~1s/file on the file-level `git log -1`/`git blame` calls it
    does use). The 90-day recency gate is the cheap option that still buys
    most of the precision: it only suppresses stale findings for drift
    that's old enough the doc has almost certainly already caught up with
    it in some other section, or the "staleness" is ancient history rather
    than something worth re-reading the doc for today.

Two-phase design:

  `extract_claims(md_text, doc_path)` classifies each backticked span by
  shape alone, using the *installed* `src.tool_schemas`/`src.settings` as
  classification hints only (doc_claims.py ships beside the code it
  documents, so those hints are always the running package's own — this is
  what keeps extraction deterministic and workspace-independent).

  `check(claims, workspace)` grounds each claim against an arbitrary
  `workspace` root (a directory, not necessarily the running package) by
  reading its files directly: `workspace/<path>` for paths, an AST scan for
  symbols, `workspace/src/settings.py`'s DEFAULT_SETTINGS dict for settings
  keys, `workspace/routes/*.py` + `workspace/app.py` for routes, and
  `workspace/src/tool_schemas.py`'s FUNCTION_TOOL_SCHEMAS for tools. This
  split is what lets tests point `check()` at a throwaway temp repo while
  `extract_claims()` stays a pure function of the doc text.

  `report(workspace, docs=None, since_days=30)` reads each doc from
  `workspace`, extracts, checks and adds DRIFT findings, splits them into
  current vs. historical (see above), and returns a dict + renders a
  grouped text report.

CLI: `python -m src.doc_claims [--workspace ROOT] [--docs A.md B.md]
[--since-days N] [--all] [--json]`. `--all` shows historical findings too
(they're always in the returned data either way).
"""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

DEFAULT_DOCS = ("FAUSTUS.md", "README.md")
DEFAULT_SINCE_DAYS = 14
_MAX_DRIFT_COMMITS = 20
_STALE_MAX_AGE_DAYS = 90
_MIN_SETTING_PREFIX_COUNT = 3
_HEADING_RE = re.compile(r"^##(?!#)\s*(.+?)\s*$")
_BACKTICK_RE = re.compile(r"`([^`\n]+)`")

# Same source/config extension list src/agent_harness.py's PATH_TOKEN_RE
# uses for doc/prose file-mention detection — reused here so "path" claims
# agree with the rest of the codebase about what counts as a file.
try:
    from src.agent_harness import _PATH_EXTS as _PATH_EXTS  # type: ignore
except Exception:  # noqa: BLE001 - keep doc_claims usable standalone
    _PATH_EXTS = (
        "vue|jsx?|tsx?|mjs|cjs|py|pyi|css|scss|sass|less|html?|json|jsonc|md|mdx|"
        r"ya?ml|toml|ini|cfg|conf|env|txt|rst|go|rs|java|kt|kts|swift|cs|cpp|cc|c|h|hpp|"
        r"rb|php|sh|bash|zsh|ps1|psm1|bat|cmd|sql|graphql|gql|proto|xml|svg|lock|"
        r"dockerfile|makefile|gradle|properties|csv|ipynb|dart|lua|r|m|mm|ex|exs|erl|"
        r"hs|scala|clj|cljs|elm|vim|tf|tfvars|hcl|nix|zig|v|sol|wasm"
    )

_PATH_CLAIM_RE = re.compile(
    r"^(?:\.{0,2}[\w@.-]+[/\\])*[\w@.-]*[A-Za-z_][\w@.-]*\.(?:" + _PATH_EXTS + r")$",
    re.IGNORECASE,
)
_SYMBOL_CLAIM_RE = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+$")
# Shape gate only: >=2 underscores (3+ tokens). The prefix-frequency gate
# (see _setting_prefix_hints) is what actually decides "setting" vs.
# "ignore" for anything that passes this shape.
_SETTING_SHAPE_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+){2,}$")

# Dotted-symbol resolution is only attempted when the leading module part
# looks like it could live in one of these top-level source dirs (either
# as an exact `top/....py` path, or — for a bare module stem with no dir
# prefix — anywhere under `src/`). Keeps stdlib/JS dotted calls
# (`os.walk`, `json.dumps`, `window.open`) from ever being "checked".
_SYMBOL_MODULE_DIRS = ("src", "routes", "services", "core")
_WALK_IGNORED_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
}

# Runtime-data / non-tracked path patterns: never worth checking against
# the workspace (they're either gitignored by construction, per-machine,
# or platform-specific paths a Markdown doc can legitimately show as an
# example without it being a real file in this repo).
_RUNTIME_PATH_RE = re.compile(
    r"(^|/)data/"
    r"|[^/\\]*-dev-data(?:/|$)"
    r"|(^|/)\.faustus/"
    r"|%APPDATA%"
    r"|^[A-Za-z]:\\",
)


# ── data types ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Claim:
    kind: str  # "path" | "symbol" | "setting" | "route" | "tool"
    text: str
    doc: str
    line: int
    section: str


@dataclass
class Finding:
    severity: str  # "broken" | "stale"
    kind: str
    message: str
    doc: str
    line: int
    section: str
    claim: str
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── classification hints (this package's own tool/settings names) ────────

def _tool_name_hints() -> set:
    try:
        # src.tool_schemas <-> src.agent_tools is a pre-existing circular
        # import in this codebase; importing agent_tools first (the module
        # every real entrypoint loads before tool_schemas) resolves it the
        # same way app.py's own import order already does.
        import src.agent_tools  # noqa: F401
        from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
        return {
            str((row.get("function") or {}).get("name") or "")
            for row in FUNCTION_TOOL_SCHEMAS
            if isinstance(row, dict)
        } - {""}
    except Exception:  # noqa: BLE001
        return set()


def _setting_key_hints() -> set:
    try:
        from src.settings import DEFAULT_SETTINGS
        return set(DEFAULT_SETTINGS.keys())
    except Exception:  # noqa: BLE001
        return set()


def _setting_prefix_hints(setting_hints: set) -> Set[str]:
    """First-token prefixes (before the first underscore) shared by >=3
    real DEFAULT_SETTINGS keys, e.g. {'agent', 'engine', 'llm', ...}. Used
    to keep one-off snake_case words (model params like `top_k`, or
    prose) from being classified as settings claims."""
    counts = Counter(k.split("_", 1)[0] for k in setting_hints if "_" in k)
    return {p for p, n in counts.items() if n >= _MIN_SETTING_PREFIX_COUNT}


# ── extraction ─────────────────────────────────────────────────────────

def _classify(text: str, tool_hints: set, setting_prefixes: Set[str]) -> Optional[str]:
    text = text.strip()
    if not text or " " in text:
        return None
    if _PATH_CLAIM_RE.match(text):
        return "path"
    if text.startswith("/"):
        # `/temp`, `/maxtokens`: composer slash-commands, not HTTP routes.
        return "route" if text.startswith("/api/") else None
    if "." not in text and "/" not in text and "\\" not in text:
        if text in tool_hints:
            return "tool"
        if _SETTING_SHAPE_RE.match(text) and text.split("_", 1)[0] in setting_prefixes:
            return "setting"
        return None
    if "/" not in text and "\\" not in text and _SYMBOL_CLAIM_RE.match(text):
        return "symbol"
    return None


def extract_claims(md_text: str, doc_path: str) -> List[Claim]:
    """Extract backticked file/symbol/setting/route/tool claims from
    Markdown text, each tagged with its line number and nearest `##`
    section heading. Pure function of `md_text` — classification hints come
    from this package's own `src.tool_schemas`/`src.settings`, not from any
    external workspace (see module docstring)."""
    tool_hints = _tool_name_hints()
    setting_prefixes = _setting_prefix_hints(_setting_key_hints())
    claims: List[Claim] = []
    section = ""
    for lineno, line in enumerate((md_text or "").splitlines(), start=1):
        heading = _HEADING_RE.match(line)
        if heading:
            section = heading.group(1)
            continue
        for m in _BACKTICK_RE.finditer(line):
            span = m.group(1)
            kind = _classify(span, tool_hints, setting_prefixes)
            if kind is None:
                continue
            claims.append(Claim(kind=kind, text=span.strip(), doc=doc_path,
                                 line=lineno, section=section))
    return claims


# ── grounding (check) ─────────────────────────────────────────────────

def _read_text(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def _workspace_default_settings(workspace: str) -> Optional[set]:
    text = _read_text(os.path.join(workspace, "src", "settings.py"))
    if text is None:
        return None
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "DEFAULT_SETTINGS" in names:
                keys = set()
                for k in node.value.keys:
                    if isinstance(k, ast.Constant) and isinstance(k.value, str):
                        keys.add(k.value)
                return keys
    return None


def _workspace_tool_names(workspace: str) -> Optional[set]:
    text = _read_text(os.path.join(workspace, "src", "tool_schemas.py"))
    if text is None:
        return None
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.List):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "FUNCTION_TOOL_SCHEMAS" not in names:
                continue
            tools = set()
            for elt in node.value.elts:
                if not isinstance(elt, ast.Dict):
                    continue
                for k, v in zip(elt.keys, elt.values):
                    if (isinstance(k, ast.Constant) and k.value == "function"
                            and isinstance(v, ast.Dict)):
                        for fk, fv in zip(v.keys, v.values):
                            if (isinstance(fk, ast.Constant) and fk.value == "name"
                                    and isinstance(fv, ast.Constant)
                                    and isinstance(fv.value, str)):
                                tools.add(fv.value)
            return tools
    return None


def _ast_has_symbol(path: str, name: str) -> bool:
    text = _read_text(path)
    if text is None:
        return False
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == name:
                return True
    return False


def _locate_symbol_candidates(workspace: str, module_parts: Sequence[str]) -> List[str]:
    """Candidate files the leading module part of a dotted claim could
    resolve to, restricted to `_SYMBOL_MODULE_DIRS`: an exact
    `top/.../mod.py` path when the first token names one of those dirs, or
    (for a bare module stem, no dir prefix at all) any file with that stem
    found under `src/`. Empty result means "not applicable" -- the caller
    should ignore the claim rather than report it broken."""
    if not module_parts:
        return []
    candidates: List[str] = []
    if module_parts[0] in _SYMBOL_MODULE_DIRS:
        rel = os.path.join(*module_parts) + ".py"
        full = os.path.join(workspace, rel)
        if os.path.isfile(full):
            candidates.append(full)
    src_dir = os.path.join(workspace, "src")
    if os.path.isdir(src_dir):
        bare = module_parts[-1] + ".py"
        hits = []
        for root, dirs, files in os.walk(src_dir):
            dirs[:] = sorted(d for d in dirs if d not in _WALK_IGNORED_DIRS)
            if bare in files:
                hits.append(os.path.join(root, bare))
        candidates.extend(sorted(hits))
    # de-dupe, keep first-seen order (exact match first, then bare hits)
    seen = set()
    ordered = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            ordered.append(c)
    return ordered


def _resolve_symbol_claim(workspace: str, dotted: str) -> Optional[str]:
    """Resolve a `module.func`/`Class.method` dotted claim.

    Returns: a file path (str) when the symbol was found -- the claim is
    fine; "" (empty string, falsy but not None) when a candidate module
    was located but the symbol wasn't found in it -- the claim is broken;
    None when no candidate module could be located under the allowed
    source dirs at all -- the claim isn't applicable and is ignored
    (this is what keeps `os.walk`/`json.dumps`/`window.open` out of the
    broken count).
    """
    parts = dotted.split(".")
    # `core.database`, `services.hwfit`: the dotted name is itself a module
    # or package in the workspace, so the claim holds.
    for base in ("", "src"):
        rel = os.path.join(workspace, base, *parts)
        if os.path.isfile(rel + ".py") or os.path.isfile(os.path.join(rel, "__init__.py")):
            return rel
    symbol = parts[-1]
    module_parts = parts[:-1]
    candidates = _locate_symbol_candidates(workspace, module_parts)
    if not candidates:
        return None
    for cand in candidates:
        if _ast_has_symbol(cand, symbol):
            return cand
    return ""


_ROUTE_PREFIX_RE = re.compile(r'APIRouter\(\s*prefix\s*=\s*["\']([^"\']*)["\']')
_ROUTE_PATH_RE = re.compile(
    r'router\.(?:get|post|put|patch|delete)\(\s*["\']([^"\']*)["\']'
)
_APP_ROUTE_RE = re.compile(
    r'app\.(?:get|post|put|patch|delete)\(\s*["\']([^"\']*)["\']'
)


def _workspace_routes(workspace: str) -> Optional[set]:
    routes_dir = os.path.join(workspace, "routes")
    if not os.path.isdir(routes_dir):
        return None
    found = set()
    for name in os.listdir(routes_dir):
        if not name.endswith(".py"):
            continue
        text = _read_text(os.path.join(routes_dir, name))
        if text is None:
            continue
        prefix_m = _ROUTE_PREFIX_RE.search(text)
        prefix = prefix_m.group(1) if prefix_m else ""
        for pm in _ROUTE_PATH_RE.finditer(text):
            found.add((prefix.rstrip("/") + "/" + pm.group(1).lstrip("/")).replace("//", "/"))
    app_text = _read_text(os.path.join(workspace, "app.py"))
    if app_text:
        for pm in _APP_ROUTE_RE.finditer(app_text):
            found.add(pm.group(1))
    return found


def _route_registered(claim_text: str, routes: Optional[set], workspace: str) -> bool:
    if routes and claim_text in routes:
        return True
    # Deterministic fallback: the literal route string appears verbatim as
    # a quoted string anywhere under routes/ — covers dynamic registration
    # styles the structured prefix/path scan above doesn't parse.
    routes_dir = os.path.join(workspace, "routes")
    if not os.path.isdir(routes_dir):
        return False
    needle_variants = (f'"{claim_text}"', f"'{claim_text}'")
    for root, dirs, files in os.walk(routes_dir):
        dirs[:] = sorted(d for d in dirs if d not in ("__pycache__",))
        for name in files:
            if not name.endswith(".py"):
                continue
            text = _read_text(os.path.join(root, name))
            if text and any(v in text for v in needle_variants):
                return True
    return False


def _gitignored_set(workspace: str, paths: Sequence[str]) -> Set[str]:
    """Batched `git check-ignore --stdin` over every path-kind claim at
    once (one process, not one per claim)."""
    paths = list(dict.fromkeys(paths))  # de-dupe, keep order
    if not paths:
        return set()
    try:
        out = subprocess.run(
            ["git", "check-ignore", "--stdin"], cwd=workspace,
            input="\n".join(paths), capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    if out.returncode not in (0, 1):  # 128 etc. -- not a git repo/error
        return set()
    return {line for line in out.stdout.splitlines() if line}


def _path_top_dir_exists(workspace: str, rel_path: str) -> bool:
    first = rel_path.replace("\\", "/").split("/")[0]
    if first == rel_path.replace("\\", "/"):
        return True  # bare filename, no directory component to gate on
    return os.path.isdir(os.path.join(workspace, first))


def _skippable_paths(workspace: str, path_claims: Sequence[Claim]) -> Set[str]:
    """Path-claim texts that should NOT be checked at all: runtime-data
    patterns, gitignored paths (one batched `git check-ignore` call), and
    paths whose top-level directory doesn't exist in the workspace."""
    texts = [c.text for c in path_claims]
    runtime = {t for t in texts if _RUNTIME_PATH_RE.search(t)}
    remaining = [t for t in texts if t not in runtime]
    ignored = _gitignored_set(workspace, remaining)
    no_top_dir = {
        t for t in remaining
        if t not in ignored and not _path_top_dir_exists(workspace, t)
    }
    return runtime | ignored | no_top_dir


def _tracked_files(workspace: str) -> List[str]:
    """Repo-relative tracked files (forward slashes); empty when not a git repo."""
    try:
        out = subprocess.run(
            ["git", "ls-files"], cwd=workspace, capture_output=True,
            text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    return [line for line in out.stdout.splitlines() if line]


def _path_resolves(workspace: str, rel: str, tracked: Sequence[str]) -> bool:
    """A doc path resolves when it exists from the repo root, or -- since
    docs often cite files by basename (`store.py`) or by a path relative to
    a sub-project (`screens/Chat.tsx`) -- when some tracked file ends with it."""
    if os.path.isfile(os.path.join(workspace, rel)):
        return True
    norm = rel.replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    if not norm:
        return False
    suffix = "/" + norm
    return any(t == norm or t.endswith(suffix) for t in tracked)


def check(claims: Sequence[Claim], workspace: str) -> List[Finding]:
    """Ground each claim against `workspace`'s files. Returns only
    "broken" findings; stale/DRIFT findings come from `report()`, which
    also needs a git history and doc source to compute them."""
    findings: List[Finding] = []
    ws_settings = _workspace_default_settings(workspace)
    ws_tools = _workspace_tool_names(workspace)
    ws_routes = _workspace_routes(workspace)
    path_claims = [c for c in claims if c.kind == "path"]
    skip_paths = _skippable_paths(workspace, path_claims)
    tracked = _tracked_files(workspace) if path_claims else []
    for claim in claims:
        if claim.kind == "path":
            if claim.text in skip_paths:
                continue
            if not _path_resolves(workspace, claim.text, tracked):
                findings.append(Finding(
                    severity="broken", kind="missing_path",
                    message=f"file not found: {claim.text}",
                    doc=claim.doc, line=claim.line, section=claim.section,
                    claim=claim.text,
                ))
        elif claim.kind == "symbol":
            resolved = _resolve_symbol_claim(workspace, claim.text)
            if resolved == "":  # applicable module found, symbol missing
                findings.append(Finding(
                    severity="broken", kind="missing_symbol",
                    message=f"symbol not found: {claim.text}",
                    doc=claim.doc, line=claim.line, section=claim.section,
                    claim=claim.text,
                ))
            # resolved is None -> not applicable (stdlib/JS/etc.), ignored
        elif claim.kind == "setting":
            if ws_settings is not None and claim.text not in ws_settings:
                findings.append(Finding(
                    severity="broken", kind="missing_setting",
                    message=f"settings key not in DEFAULT_SETTINGS: {claim.text}",
                    doc=claim.doc, line=claim.line, section=claim.section,
                    claim=claim.text,
                ))
        elif claim.kind == "route":
            if not _route_registered(claim.text, ws_routes, workspace):
                findings.append(Finding(
                    severity="broken", kind="missing_route",
                    message=f"route not registered: {claim.text}",
                    doc=claim.doc, line=claim.line, section=claim.section,
                    claim=claim.text,
                ))
        elif claim.kind == "tool":
            if ws_tools is not None and claim.text not in ws_tools:
                findings.append(Finding(
                    severity="broken", kind="missing_tool",
                    message=f"tool not in FUNCTION_TOOL_SCHEMAS: {claim.text}",
                    doc=claim.doc, line=claim.line, section=claim.section,
                    claim=claim.text,
                ))
    return findings


# ── drift (stale sections) ────────────────────────────────────────────

def _git(workspace: str, *args: str, timeout: int = 10) -> Optional[str]:
    try:
        out = subprocess.run(
            ["git", *args], cwd=workspace, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout


class _GitHistory:
    """Whole-history index built from ONE `git log` call, so drift checks
    don't spawn a git process per file (hundreds of spawns took >60s on
    Windows). Commits are newest first."""

    def __init__(self, workspace: str):
        self.commits: List[Tuple[int, str, str, Set[str]]] = []
        self.last_touch: Dict[str, int] = {}
        out = _git(workspace, "log", "--no-renames",
                   "--format=%x1e%ct%x1f%h%x1f%s", "--name-only", timeout=60)
        if not out:
            return
        for block in out.split("\x1e"):
            block = block.strip("\n")
            if not block:
                continue
            head, _, rest = block.partition("\n")
            parts = head.split("\x1f", 2)
            if len(parts) != 3:
                continue
            try:
                ts = int(parts[0])
            except ValueError:
                continue
            files = {ln.strip() for ln in rest.splitlines() if ln.strip()}
            self.commits.append((ts, parts[1], parts[2], files))
            for f in files:
                if f not in self.last_touch:
                    self.last_touch[f] = ts

    def last_commit_time(self, rel_path: str) -> Optional[int]:
        return self.last_touch.get(_norm_rel(rel_path))

    def commits_since(self, ts: int, rel_paths: Sequence[str], limit: int) -> List[Dict[str, str]]:
        wanted = {_norm_rel(p) for p in rel_paths}
        found: List[Dict[str, str]] = []
        for c_ts, sha, subject, files in self.commits:
            if c_ts < ts:
                break
            if files & wanted:
                found.append({"sha": sha, "subject": subject})
                if len(found) >= limit:
                    break
        return found


def _norm_rel(rel_path: str) -> str:
    norm = rel_path.replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    return norm


_BLAME_CACHE: Dict[Tuple[str, str, str], List[Optional[int]]] = {}


def _doc_line_times(workspace: str, doc_rel: str) -> List[Optional[int]]:
    """committer-time per line of `doc_rel` (index 0 = line 1), from one
    `git blame` of the whole file, cached per (workspace, doc, HEAD)."""
    head = (_git(workspace, "rev-parse", "HEAD") or "").strip()
    key = (workspace, doc_rel, head)
    if key in _BLAME_CACHE:
        return _BLAME_CACHE[key]
    out = _git(workspace, "blame", "--line-porcelain", "--", doc_rel, timeout=60)
    times: List[Optional[int]] = []
    current: Optional[int] = None
    for line in (out or "").splitlines():
        if line.startswith("committer-time "):
            try:
                current = int(line.split(" ", 1)[1])
            except (ValueError, IndexError):
                current = None
        elif line.startswith("\t"):
            times.append(current)
    if len(_BLAME_CACHE) > 16:
        _BLAME_CACHE.clear()
    _BLAME_CACHE[key] = times
    return times


def _section_line_range(doc_text: str, section: str) -> Optional[Tuple[int, int]]:
    lines = doc_text.splitlines()
    start = None
    end = len(lines)
    for i, line in enumerate(lines, start=1):
        heading = _HEADING_RE.match(line)
        if heading:
            if start is not None:
                end = i - 1
                break
            if heading.group(1) == section:
                start = i
        elif start is None and section == "":
            start = 1
    if start is None:
        return None
    return (start, max(start, end))


def _section_last_edit_time(workspace: str, doc_rel: str, doc_text: str, section: str) -> Optional[int]:
    rng = _section_line_range(doc_text, section)
    if rng is None:
        return None
    a, b = rng
    times = _doc_line_times(workspace, doc_rel)
    window = [t for t in times[a - 1:b] if t is not None]
    return max(window) if window else None


def _referenced_files_for_section(workspace: str, section_claims: Sequence[Claim]) -> List[str]:
    """Source files a section's claims resolve to -- never the doc itself
    or another Markdown doc (drift is about *code* moving under the doc,
    not docs referencing each other)."""
    files: List[str] = []
    seen = set()
    for claim in section_claims:
        rel = None
        if claim.kind == "path":
            if not claim.text.lower().endswith(".md"):
                rel = claim.text
        elif claim.kind == "symbol":
            found = _resolve_symbol_claim(workspace, claim.text)
            if found:  # a real path string, not None/""
                rel = os.path.relpath(found, workspace)
        elif claim.kind == "setting":
            rel = os.path.join("src", "settings.py")
        elif claim.kind == "tool":
            rel = os.path.join("src", "tool_schemas.py")
        elif claim.kind == "route":
            rel = None  # route resolution doesn't pin one file deterministically
        if rel and rel not in seen and os.path.isfile(os.path.join(workspace, rel)):
            seen.add(rel)
            files.append(rel)
    return files


def _drift_findings(workspace: str, doc_rel: str, doc_text: str,
                     claims: Sequence[Claim]) -> List[Finding]:
    findings: List[Finding] = []
    if _git(workspace, "rev-parse", "--git-dir") is None:
        return findings  # not a git repo (or git unavailable) -- no drift signal
    now = time.time()
    stale_cutoff = now - _STALE_MAX_AGE_DAYS * 86400
    history = _GitHistory(workspace)
    by_section: Dict[str, List[Claim]] = {}
    for c in claims:
        by_section.setdefault(c.section, []).append(c)
    for section, section_claims in by_section.items():
        files = _referenced_files_for_section(workspace, section_claims)
        if not files:
            continue
        doc_time = _section_last_edit_time(workspace, doc_rel, doc_text, section)
        if doc_time is None:
            continue
        newest_code_time = None
        newest_file = None
        for f in files:
            t = history.last_commit_time(f)
            if t is not None and (newest_code_time is None or t > newest_code_time):
                newest_code_time, newest_file = t, f
        if newest_code_time is None or newest_code_time <= doc_time:
            continue
        # Precision gate: only recent drift is worth flagging (see module
        # docstring for why file-level + recency beats `git log -L` here).
        if newest_code_time < stale_cutoff:
            continue
        # Section is stale: list the commits that touched referenced files
        # after this doc section was last edited.
        commits = history.commits_since(doc_time, files, _MAX_DRIFT_COMMITS)
        first_line = section_claims[0].line
        findings.append(Finding(
            severity="stale", kind="drift",
            message=(
                f"section '{section or '(intro)'}' was last edited before code it "
                f"references changed (newest: {newest_file})"
            ),
            doc=doc_rel, line=first_line, section=section, claim=newest_file or "",
            extra={"files": files, "commits": commits,
                   "doc_edit_ts": doc_time, "code_change_ts": newest_code_time},
        ))
    return findings


# ── historical vs. current ────────────────────────────────────────────

def _is_dated_log_doc(doc_rel: str) -> bool:
    """Only FAUSTUS.md is treated as a dated running log whose old
    sections get a historical pass; README*.md and docs/**/*.md always
    describe the present, so they're always "current"."""
    return os.path.basename(doc_rel) == "FAUSTUS.md"


# ── report ────────────────────────────────────────────────────────────

def report(workspace: str, docs: Optional[Sequence[str]] = None,
           since_days: int = DEFAULT_SINCE_DAYS) -> Dict[str, Any]:
    docs = list(docs) if docs else list(DEFAULT_DOCS)
    cutoff = time.time() - since_days * 86400
    current: List[Finding] = []
    historical: List[Finding] = []
    claim_count = 0
    per_doc: List[Dict[str, Any]] = []
    for doc_rel in docs:
        full = os.path.join(workspace, doc_rel)
        text = _read_text(full)
        if text is None:
            per_doc.append({"doc": doc_rel, "error": "not found", "claims": 0})
            continue
        claims = extract_claims(text, doc_rel)
        claim_count += len(claims)
        broken = check(claims, workspace)
        stale = _drift_findings(workspace, doc_rel, text, claims)
        dated_log = _is_dated_log_doc(doc_rel)
        section_time_cache: Dict[str, Optional[int]] = {}

        def is_historical(section: str) -> bool:
            if not dated_log:
                return False
            if section not in section_time_cache:
                section_time_cache[section] = _section_last_edit_time(
                    workspace, doc_rel, text, section)
            t = section_time_cache[section]
            return t is not None and t < cutoff

        doc_current = 0
        doc_historical = 0
        for f in (*broken, *stale):
            if is_historical(f.section):
                historical.append(f)
                doc_historical += 1
            else:
                current.append(f)
                doc_current += 1
        per_doc.append({"doc": doc_rel, "claims": len(claims),
                         "current_findings": doc_current,
                         "historical_findings": doc_historical})
    broken_count = sum(1 for f in current if f.severity == "broken")
    stale_count = sum(1 for f in current if f.severity == "stale")
    historical_broken_count = sum(1 for f in historical if f.severity == "broken")
    historical_stale_count = sum(1 for f in historical if f.severity == "stale")
    return {
        "workspace": workspace,
        "since_days": since_days,
        "docs": per_doc,
        "claims_total": claim_count,
        "broken_count": broken_count,
        "stale_count": stale_count,
        "historical_count": historical_broken_count,
        "historical_stale_count": historical_stale_count,
        "findings": [f.to_dict() for f in current],
        "historical_findings": [f.to_dict() for f in historical],
    }


def _render_findings(lines: List[str], docs_info: List[Dict[str, Any]],
                      findings: List[Dict[str, Any]]) -> None:
    by_doc: Dict[str, List[Dict[str, Any]]] = {}
    for f in findings:
        by_doc.setdefault(f["doc"], []).append(f)
    for doc_info in docs_info:
        doc = doc_info["doc"]
        doc_findings = by_doc.get(doc, [])
        if not doc_findings:
            continue
        lines.append(f"\n== {doc} ==")
        by_section: Dict[str, List[Dict[str, Any]]] = {}
        for f in doc_findings:
            by_section.setdefault(f["section"] or "(intro)", []).append(f)
        for section, sec_findings in by_section.items():
            lines.append(f"  -- {section} --")
            for f in sec_findings:
                tag = "BROKEN" if f["severity"] == "broken" else "STALE "
                lines.append(f"    [{tag}] L{f['line']} {f['kind']}: {f['message']}")
                if f["severity"] == "stale" and f.get("extra", {}).get("commits"):
                    for c in f["extra"]["commits"][:5]:
                        lines.append(f"        {c['sha']}  {c['subject']}")


def render_report(data: Dict[str, Any], include_historical: bool = False) -> str:
    lines = [
        f"doc-claims report — {data['claims_total']} claims, "
        f"{data['broken_count']} broken, {data['stale_count']} stale"
        f" (+ {data.get('historical_count', 0)} historical broken, "
        f"{data.get('historical_stale_count', 0)} historical stale, "
        f"since_days={data.get('since_days', DEFAULT_SINCE_DAYS)})",
    ]
    empty_docs = [d for d in data["docs"] if not d.get("error")
                  and not d.get("current_findings") and not d.get("historical_findings")]
    _render_findings(lines, data["docs"], data["findings"])
    if include_historical and data.get("historical_findings"):
        lines.append("\n== HISTORICAL (older than since_days) ==")
        _render_findings(lines, data["docs"], data["historical_findings"])
    for doc_info in data["docs"]:
        if doc_info.get("error"):
            lines.append(f"\n== {doc_info['doc']} ==\n  {doc_info['error']}")
    for doc_info in empty_docs:
        lines.append(f"\n== {doc_info['doc']} ==\n  ok ({doc_info['claims']} claims, no findings)")
    return "\n".join(lines)


# ── CLI ───────────────────────────────────────────────────────────────

def _main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Doc-claim drift checker")
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--docs", nargs="*", default=None)
    parser.add_argument("--since-days", type=int, default=DEFAULT_SINCE_DAYS)
    parser.add_argument("--all", action="store_true",
                         help="also show historical findings (older FAUSTUS.md sections)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    data = report(os.path.abspath(args.workspace), args.docs, since_days=args.since_days)
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print(render_report(data, include_historical=args.all))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
