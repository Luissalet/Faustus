"""src/doc_lint.py -- report-only Markdown linter with checkable rules.

It never rewrites a document. Every finding carries a rule id, a severity
(error / warning / info), a 1-based line, a message and a fix hint.

Rules (ids), grouped:

* headings   -- ``heading-skip`` (a level jumps more than one deeper),
  ``heading-multiple-h1``, ``heading-missing-h1``, ``heading-duplicate``
  (same text, same level, same parent section).
* numbering  -- numbered sections ("## 12. Title", "### 3.4 Title",
  "## <section sign>12 Title"): ``numbering-gap``, ``numbering-duplicate``,
  ``numbering-order``, ``numbering-orphan`` (3.2 with no section 3).
* links      -- ``link-broken`` (relative Markdown link whose target does not
  exist), ``path-missing`` (a backticked repo path that does not exist).
  Both resolve against the document's repo root (nearest ancestor with a
  ``.git``); a target that would leave the repo root is reported, never probed.
* dates      -- ``date-impossible`` (31-02-2026, month 13) and
  ``date-malformed`` (not zero-padded) for DD-MM-YYYY and YYYY-MM-DD.
* sections   -- ``required-section``: headings a document must contain,
  configured per document name.
* forbidden  -- ``forbidden-content``: regex patterns and banned names from the
  config (defaults: commit trailers; the names list is empty).
* whitespace -- ``trailing-whitespace`` and ``tabs`` (off by default).

Fenced code blocks are skipped by every rule except ``forbidden-content``,
``trailing-whitespace`` and ``tabs``.

Config (JSON) is merged over the built-in defaults from, in order,
``<repo>/config/doc_lint.json`` and ``<repo>/.doc_lint.json``::

    {"disabled_rules": [], "whitespace": false,
     "documents": {"README.md": {"required_sections": ["Quick start"]}},
     "forbidden": {"patterns": ["Co-Authored-By\\\\s*:"], "names": []},
     "date_year_range": [1990, 2100], "path_ignore": ["data/*"]}

CLI: ``python -m src.doc_lint FILE... [--json] [--rules a,b] [--config F]
[--fail-on error|warning|info|never]``.
"""
from __future__ import annotations

import argparse
import copy
import fnmatch
import datetime as _dt
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import unquote

SEVERITIES = ("error", "warning", "info")
_SEV_RANK = {"error": 0, "warning": 1, "info": 2}

RULE_GROUPS: Dict[str, Tuple[str, ...]] = {
    "headings": ("heading-skip", "heading-multiple-h1", "heading-missing-h1", "heading-duplicate"),
    "numbering": ("numbering-gap", "numbering-duplicate", "numbering-order", "numbering-orphan"),
    "links": ("link-broken", "path-missing"),
    "dates": ("date-impossible", "date-malformed"),
    "sections": ("required-section",),
    "forbidden": ("forbidden-content",),
    "whitespace": ("trailing-whitespace", "tabs"),
}
ALL_RULES: Tuple[str, ...] = tuple(r for g in RULE_GROUPS.values() for r in g)
DEFAULT_OFF_GROUPS = ("whitespace",)
MAX_BYTES = 5 * 1024 * 1024
MARKDOWN_SUFFIXES = (".md", ".markdown", ".mdx")

DEFAULT_CONFIG: Dict[str, Any] = {
    "disabled_rules": [],
    "whitespace": False,
    "date_year_range": [1990, 2100],
    "path_ignore": ["data/*", "logs/*", "venv/*", "node_modules/*", "dist/*", "build/*"],
    "documents": {
        "README.md": {
            "required_sections": [
                "What Faustus is",
                "Quick start",
                "Architecture",
                "Security and data",
                "Credits and licence",
            ]
        },
        "FAUSTUS.md": {
            "required_sections": [
                "Proyectos",
                "Arn\u00e9s de fiabilidad del agente",
            ]
        },
    },
    "forbidden": {
        "patterns": [r"Co-Authored-By\s*:", r"Claude-Session\s*:"],
        "names": [],
    },
}


@dataclass
class Finding:
    rule: str
    severity: str
    line: int
    message: str
    fix: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _merge(base: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for key, val in (extra or {}).items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], val)
        else:
            out[key] = copy.deepcopy(val)
    return out


def find_repo_root(path: str) -> str:
    """Nearest ancestor of `path` holding a `.git` (dir or worktree file);
    the document's own directory when there is none."""
    start = os.path.dirname(os.path.realpath(path)) if not os.path.isdir(path) else os.path.realpath(path)
    cur = start
    while True:
        if os.path.exists(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return start
        cur = parent


def load_config(repo_root: str, config_path: Optional[str] = None) -> Dict[str, Any]:
    """Built-in defaults merged with the repo config files (or `config_path`).
    An unreadable or malformed config file is ignored, never fatal."""
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    candidates = [config_path] if config_path else [
        os.path.join(repo_root, "config", "doc_lint.json"),
        os.path.join(repo_root, ".doc_lint.json"),
    ]
    for cand in candidates:
        if not cand or not os.path.isfile(cand):
            continue
        try:
            with open(cand, "r", encoding="utf-8-sig") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            cfg = _merge(cfg, data)
    return cfg


def resolve_rules(rules: Optional[Iterable[str]], config: Dict[str, Any]) -> List[str]:
    """Rule ids to run. `rules` may mix ids and group names; None means the
    config default (everything except disabled rules and the whitespace group
    unless the config switches it on). Unknown names raise ValueError."""
    if rules:
        chosen: List[str] = []
        for name in rules:
            name = str(name).strip().lower()
            if not name:
                continue
            if name in RULE_GROUPS:
                chosen.extend(RULE_GROUPS[name])
            elif name in ALL_RULES:
                chosen.append(name)
            else:
                raise ValueError(f"unknown rule {name!r}; known rules: {', '.join(ALL_RULES)}; "
                                 f"groups: {', '.join(RULE_GROUPS)}")
        seen = []
        for r in chosen:
            if r not in seen:
                seen.append(r)
        return seen
    disabled = {str(r).lower() for r in (config.get("disabled_rules") or [])}
    out = []
    for group, ids in RULE_GROUPS.items():
        if group in DEFAULT_OFF_GROUPS and not config.get(group):
            continue
        out.extend(r for r in ids if r not in disabled and group not in disabled)
    return out


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$")
_NUM_RE = re.compile(r"^(?:\u00a7\s*)?(\d{1,4}(?:\.\d{1,4})*)\.?(?=\s|$)")
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_LINK_RE = re.compile(r"!?\[[^\]\n]*\]\(\s*(<[^>\n]+>|[^)\s]+)(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)")
_REFDEF_RE = re.compile(r"^ {0,3}\[[^\]\n]+\]:\s*(<[^>\n]+>|\S+)")
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")
_DMY_RE = re.compile(r"(?<![\d\-/.])(\d{1,2})-(\d{1,2})-(\d{4})(?![\d\-])")
_YMD_RE = re.compile(r"(?<![\d\-/.])(\d{4})-(\d{1,2})-(\d{1,2})(?![\d\-])")
_PATH_EXT_RE = re.compile(r"\.[A-Za-z0-9]{1,8}$")


def _fence_flags(lines: Sequence[str]) -> List[bool]:
    """True for every line inside (or on the delimiter of) a fenced block."""
    flags: List[bool] = []
    marker: Optional[str] = None
    for ln in lines:
        m = _FENCE_RE.match(ln)
        if marker is None:
            if m:
                marker = m.group(1)[0] * len(m.group(1))
                flags.append(True)
            else:
                flags.append(False)
        else:
            flags.append(True)
            if m and m.group(1)[0] == marker[0] and len(m.group(1)) >= len(marker) \
                    and ln.strip().strip(marker[0]) == "":
                marker = None
    return flags


def _strip_inline_code(line: str) -> str:
    return _INLINE_CODE_RE.sub(lambda m: " " * len(m.group(0)), line)


def _norm(text: str) -> str:
    text = re.sub(r"[`*_]", "", text)
    return re.sub(r"\s+", " ", text).strip().casefold()


@dataclass
class _Heading:
    line: int
    level: int
    text: str


def _headings(lines: Sequence[str], fenced: Sequence[bool]) -> List[_Heading]:
    out = []
    for i, ln in enumerate(lines):
        if fenced[i]:
            continue
        m = _HEADING_RE.match(ln)
        if m:
            out.append(_Heading(i + 1, len(m.group(1)), m.group(2).strip()))
    return out


# ---------------------------------------------------------------------------
# Rules. Each takes a context and appends findings.
# ---------------------------------------------------------------------------

class _Ctx:
    def __init__(self, text: str, path: str, repo_root: str, config: Dict[str, Any], enabled: Sequence[str]):
        self.text = text
        self.lines = text.split("\n")
        self.path = path
        self.repo_root = repo_root
        self.config = config
        self.enabled = set(enabled)
        self.fenced = _fence_flags(self.lines)
        self.headings = _headings(self.lines, self.fenced)
        self.findings: List[Finding] = []

    def add(self, rule: str, severity: str, line: int, message: str, fix: str) -> None:
        if rule in self.enabled:
            self.findings.append(Finding(rule, severity, line, message, fix))


def _rule_headings(c: _Ctx) -> None:
    hs = c.headings
    if not hs:
        c.add("heading-missing-h1", "info", 1, "the document has no headings at all",
              "start with a single '# Title' heading")
        return
    h1s = [h for h in hs if h.level == 1]
    if not h1s:
        c.add("heading-missing-h1", "info", hs[0].line, "no H1 heading",
              "add one '# Title' heading at the top (or ignore if the title is rendered from HTML)")
    for extra in h1s[1:]:
        c.add("heading-multiple-h1", "warning", extra.line,
              f"second H1 '{extra.text}' (first H1 is on line {h1s[0].line})",
              "keep a single H1; demote this one to '##'")
    prev = hs[0]
    for h in hs[1:]:
        if h.level > prev.level + 1:
            c.add("heading-skip", "warning", h.line,
                  f"heading level jumps from H{prev.level} (line {prev.line}) to H{h.level} '{h.text}'",
                  f"use H{prev.level + 1} here or add the missing intermediate heading")
        prev = h
    # duplicates: same normalised text, same level, same parent heading
    stack: List[_Heading] = []
    seen: Dict[Tuple[int, int, str], int] = {}
    for h in hs:
        while stack and stack[-1].level >= h.level:
            stack.pop()
        parent_line = stack[-1].line if stack else 0
        key = (parent_line, h.level, _norm(h.text))
        if key in seen:
            c.add("heading-duplicate", "warning", h.line,
                  f"duplicate heading '{h.text}' (same level and parent as line {seen[key]})",
                  "rename one of them; duplicate headings get colliding anchors")
        else:
            seen[key] = h.line
        stack.append(h)


def _parse_number(text: str) -> Optional[Tuple[int, ...]]:
    m = _NUM_RE.match(text)
    if not m:
        return None
    return tuple(int(p) for p in m.group(1).split("."))


def _rule_numbering(c: _Ctx) -> None:
    last: Dict[Tuple[int, ...], int] = {}
    seen: Dict[Tuple[int, ...], Dict[int, int]] = {}
    known: set = set()
    for h in c.headings:
        parts = _parse_number(h.text)
        if parts is None:
            continue
        depth = len(parts)
        prefix, n = parts[:-1], parts[-1]
        label = ".".join(str(p) for p in parts)
        if depth > 1 and prefix not in known:
            c.add("numbering-orphan", "warning", h.line,
                  f"section {label} has no parent section {'.'.join(str(p) for p in prefix)} before it",
                  "add or renumber the parent section")
        series_seen = seen.setdefault(prefix, {})
        if n in series_seen:
            c.add("numbering-duplicate", "error", h.line,
                  f"section number {label} is already used on line {series_seen[n]}",
                  "renumber one of them")
        else:
            prev = last.get(prefix)
            if prev is not None and n < prev:
                c.add("numbering-order", "error", h.line,
                      f"section {label} comes after {'.'.join(str(p) for p in prefix + (prev,))} (out of order)",
                      "move the section or renumber it")
            elif prev is not None and n > prev + 1:
                missing = ", ".join(".".join(str(p) for p in prefix + (k,)) for k in range(prev + 1, min(n, prev + 4)))
                more = "..." if n - prev > 4 else ""
                c.add("numbering-gap", "warning", h.line,
                      f"section {label} follows {'.'.join(str(p) for p in prefix + (prev,))}: missing {missing}{more}",
                      "renumber to close the gap, or confirm the sections were removed on purpose")
            series_seen[n] = h.line
            last[prefix] = n if prev is None else max(prev, n)
        known.add(parts)
        # a (re)started section resets the numbering of its children
        for table in (last, seen):
            for key in [k for k in table if len(k) >= depth and k[:depth] == parts]:
                del table[key]
        known = {k for k in known if not (len(k) > depth and k[:depth] == parts)}


def _clean_target(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("<") and raw.endswith(">"):
        raw = raw[1:-1]
    return raw


def _within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([os.path.realpath(path), os.path.realpath(root)]) == os.path.realpath(root)
    except ValueError:
        return False


def _rule_links(c: _Ctx) -> None:
    doc_dir = os.path.dirname(os.path.realpath(c.path)) if c.path else c.repo_root
    ignore = [str(p) for p in (c.config.get("path_ignore") or [])]
    for i, ln in enumerate(c.lines):
        if c.fenced[i]:
            continue
        bare = _strip_inline_code(ln)
        targets = [_clean_target(m.group(1)) for m in _LINK_RE.finditer(bare)]
        rd = _REFDEF_RE.match(bare)
        if rd:
            targets.append(_clean_target(rd.group(1)))
        for tgt in targets:
            if not tgt or tgt.startswith("#") or tgt.startswith("//") or _SCHEME_RE.match(tgt):
                continue
            rel = unquote(re.split(r"[#?]", tgt, maxsplit=1)[0])
            if not rel:
                continue
            base = c.repo_root if rel.startswith("/") else doc_dir
            full = os.path.normpath(os.path.join(base, rel.lstrip("/")))
            if not _within(full, c.repo_root):
                c.add("link-broken", "warning", i + 1,
                      f"link '{tgt}' points outside the repository root",
                      "link to a file inside the repo or use an absolute URL")
            elif not os.path.exists(full):
                c.add("link-broken", "error", i + 1,
                      f"link target '{tgt}' does not exist",
                      "fix the path, restore the file, or remove the link")
        for m in _INLINE_CODE_RE.finditer(ln):
            cand = _path_candidate(m.group(1), c.repo_root, ignore)
            if cand is None:
                continue
            full = os.path.normpath(os.path.join(c.repo_root, cand))
            alt = os.path.normpath(os.path.join(doc_dir, cand))
            if not _within(full, c.repo_root):
                continue
            if os.path.exists(full) or (_within(alt, c.repo_root) and os.path.exists(alt)):
                continue
            c.add("path-missing", "warning", i + 1,
                  f"`{m.group(1)}` looks like a repo path but '{cand}' does not exist",
                  "update the path, or drop the backticks if it is not a repo file")


def _path_candidate(code: str, repo_root: str, ignore: Sequence[str] = ()) -> Optional[str]:
    """The repo-relative path `code` claims, or None when it is not clearly
    one: it must have a slash, ASCII only, no prose/glob characters, a first
    segment that is a real top-level entry of the repo, and either a file
    extension or a trailing slash (a directory claim)."""
    s = code.strip()
    if not s or " " in s or "\t" in s or len(s) > 200 or not s.isascii():
        return None
    if _SCHEME_RE.match(s) or re.match(r"^[A-Za-z]:[\\/]", s) or "\\" in s:
        return None
    if re.search(r"[*?<>{}|$()\[\]=,;!\"'#@%~]", s):
        return None
    s = re.sub(r"((::[\w.]+)+|:\d+(?:[:\-]\d+)*)$", "", s)
    is_dir = s.endswith("/")
    s = s.rstrip("/")
    if s.startswith("./"):
        s = s[2:]
    if "/" not in s or s.startswith("/") or s.startswith("."):
        return None
    parts = s.split("/")
    if ".." in parts or "" in parts:
        return None
    if not (_PATH_EXT_RE.search(parts[-1]) or is_dir):
        return None
    if not os.path.exists(os.path.join(repo_root, parts[0])):
        return None
    if any(fnmatch.fnmatch(s, pat) for pat in ignore):
        return None
    return s


def _date_fmt(y: int, mo: int, d: int, c: _Ctx, line: int, raw: str, padded: bool) -> None:
    lo, hi = (c.config.get("date_year_range") or [1990, 2100])[:2]
    try:
        _dt.date(y, mo, d)
        valid = lo <= y <= hi
    except ValueError:
        valid = False
    if not valid:
        c.add("date-impossible", "error", line, f"'{raw}' is not a valid calendar date",
              "correct the day, month or year")
    elif not padded:
        c.add("date-malformed", "warning", line, f"'{raw}' is not zero-padded",
              "write two-digit day and month (DD-MM-YYYY or YYYY-MM-DD)")


def _rule_dates(c: _Ctx) -> None:
    for i, ln in enumerate(c.lines):
        if c.fenced[i]:
            continue
        bare = _strip_inline_code(ln)
        bare = re.sub(r"\]\([^)]*\)", "]()", bare)  # URLs inside links
        bare = re.sub(r"https?://\S+", " ", bare)
        for m in _DMY_RE.finditer(bare):
            d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            _date_fmt(y, mo, d, c, i + 1, m.group(0), len(m.group(1)) == 2 and len(m.group(2)) == 2)
        for m in _YMD_RE.finditer(bare):
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            _date_fmt(y, mo, d, c, i + 1, m.group(0), len(m.group(2)) == 2 and len(m.group(3)) == 2)


def _doc_config(c: _Ctx) -> Dict[str, Any]:
    docs = c.config.get("documents") or {}
    name = os.path.basename(c.path) if c.path else ""
    try:
        rel = os.path.relpath(os.path.realpath(c.path), c.repo_root).replace("\\", "/")
    except ValueError:
        rel = name
    for key in (rel, name):
        for k, v in docs.items():
            if k.replace("\\", "/").casefold() == key.casefold() and isinstance(v, dict):
                return v
    return {}


def _rule_sections(c: _Ctx) -> None:
    required = _doc_config(c).get("required_sections") or []
    titles = [_norm(_NUM_RE.sub("", h.text, count=1)) for h in c.headings]
    for want in required:
        w = _norm(str(want))
        if not any(w in t for t in titles):
            c.add("required-section", "error", 1, f"required section '{want}' is missing",
                  f"add a heading containing '{want}'")


def _rule_forbidden(c: _Ctx) -> None:
    cfg = c.config.get("forbidden") or {}
    patterns = []
    for p in cfg.get("patterns") or []:
        try:
            patterns.append((str(p), re.compile(str(p), re.IGNORECASE)))
        except re.error:
            c.add("forbidden-content", "warning", 1, f"invalid forbidden pattern {p!r} in the config",
                  "fix the regular expression in the doc-lint config")
    for name in cfg.get("names") or []:
        n = str(name).strip()
        if n:
            patterns.append((n, re.compile(r"(?<!\w)" + re.escape(n) + r"(?!\w)", re.IGNORECASE)))
    if not patterns:
        return
    for i, ln in enumerate(c.lines):
        for label, rx in patterns:
            m = rx.search(ln)
            if m:
                c.add("forbidden-content", "error", i + 1,
                      f"forbidden content '{m.group(0)}' (rule {label})",
                      "remove or reword it")


def _rule_whitespace(c: _Ctx) -> None:
    for i, ln in enumerate(c.lines):
        body = ln.rstrip("\r")
        if body != body.rstrip(" \t"):
            trailing = body[len(body.rstrip(" \t")):]
            hard_break = trailing == "  " and body.strip() and not c.fenced[i]
            if not hard_break:
                c.add("trailing-whitespace", "info", i + 1, "trailing whitespace",
                      "delete the trailing spaces/tabs")
        if "\t" in body and not c.fenced[i]:
            c.add("tabs", "info", i + 1, "tab character in Markdown text",
                  "use spaces for indentation")


_RULE_FUNCS: List[Tuple[Tuple[str, ...], Callable[[_Ctx], None]]] = [
    (RULE_GROUPS["headings"], _rule_headings),
    (RULE_GROUPS["numbering"], _rule_numbering),
    (RULE_GROUPS["links"], _rule_links),
    (RULE_GROUPS["dates"], _rule_dates),
    (RULE_GROUPS["sections"], _rule_sections),
    (RULE_GROUPS["forbidden"], _rule_forbidden),
    (RULE_GROUPS["whitespace"], _rule_whitespace),
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def lint_text(text: str, path: str = "", repo_root: str = "", config: Optional[Dict[str, Any]] = None,
              rules: Optional[Iterable[str]] = None) -> List[Finding]:
    """Lint Markdown `text`. `path` names the document (link/path resolution
    and per-document config); `repo_root` defaults to the nearest `.git`."""
    root = repo_root or (find_repo_root(path) if path else os.getcwd())
    cfg = config if config is not None else load_config(root)
    enabled = resolve_rules(rules, cfg)
    ctx = _Ctx(text.lstrip("﻿").replace("\r\n", "\n"), path, root, cfg, enabled)
    for ids, fn in _RULE_FUNCS:
        if any(r in ctx.enabled for r in ids):
            fn(ctx)
    ctx.findings.sort(key=lambda f: (f.line, _SEV_RANK.get(f.severity, 9), f.rule))
    return ctx.findings


def lint_file(path: str, rules: Optional[Iterable[str]] = None, config: Optional[Dict[str, Any]] = None,
              repo_root: str = "") -> Dict[str, Any]:
    """Lint one file. Returns {path, findings, counts} or {path, error}."""
    if not path.lower().endswith(MARKDOWN_SUFFIXES):
        return {"path": path, "error": "not a Markdown file (.md, .markdown, .mdx)"}
    try:
        if os.path.getsize(path) > MAX_BYTES:
            return {"path": path, "error": f"file larger than {MAX_BYTES // (1024 * 1024)} MB"}
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as exc:
        return {"path": path, "error": f"cannot read: {exc.strerror or exc}"}
    findings = lint_text(text, path, repo_root, config, rules)
    return {"path": path, "findings": [f.to_dict() for f in findings], "counts": _counts(findings)}


def _counts(findings: Iterable[Any]) -> Dict[str, int]:
    out = {s: 0 for s in SEVERITIES}
    for f in findings:
        sev = f.severity if isinstance(f, Finding) else f.get("severity")
        if sev in out:
            out[sev] += 1
    out["total"] = sum(out.values())
    return out


def lint_paths(paths: Sequence[str], rules: Optional[Iterable[str]] = None,
               resolver: Optional[Callable[[str], str]] = None,
               config: Optional[Dict[str, Any]] = None, config_path: Optional[str] = None) -> Dict[str, Any]:
    """Lint several documents. `resolver` maps a requested path to a real one
    (raising ValueError when it is not allowed); a refused path becomes an
    error entry and nothing about it is read."""
    rule_list = list(rules) if rules else None
    if rule_list:
        resolve_rules(rule_list, DEFAULT_CONFIG)  # validate early: raises ValueError
    files: List[Dict[str, Any]] = []
    for raw in paths:
        raw = str(raw)
        try:
            real = resolver(raw) if resolver else raw
        except ValueError as exc:
            files.append({"path": raw, "error": f"refused: {exc}"})
            continue
        if not os.path.isfile(real):
            files.append({"path": raw, "error": "no such file"})
            continue
        root = find_repo_root(real)
        cfg = config if config is not None else load_config(root, config_path)
        res = lint_file(real, rule_list, cfg, root)
        res["path"] = raw
        files.append(res)
    total = {s: 0 for s in SEVERITIES}
    for f in files:
        for s in SEVERITIES:
            total[s] += (f.get("counts") or {}).get(s, 0)
    total["total"] = sum(total[s] for s in SEVERITIES)
    total["files"] = len(files)
    total["errors_reading"] = sum(1 for f in files if "error" in f)
    return {"files": files, "summary": total,
            "rules": resolve_rules(rule_list, config or DEFAULT_CONFIG)}


def render_report(report: Dict[str, Any]) -> str:
    lines: List[str] = []
    for f in report.get("files", []):
        if "error" in f:
            lines.append(f"{f['path']}: ERROR {f['error']}")
            continue
        counts = f.get("counts", {})
        lines.append(f"{f['path']}: {counts.get('error', 0)} error(s), {counts.get('warning', 0)} warning(s), "
                     f"{counts.get('info', 0)} info")
        for x in f.get("findings", []):
            lines.append(f"  {f['path']}:{x['line']}  {x['severity']:<7} {x['rule']}  {x['message']}")
            lines.append(f"      fix: {x['fix']}")
    s = report.get("summary", {})
    lines.append(f"total: {s.get('error', 0)} error(s), {s.get('warning', 0)} warning(s), "
                 f"{s.get('info', 0)} info in {s.get('files', 0)} file(s)")
    return "\n".join(lines)


def rules_catalogue() -> Dict[str, Any]:
    return {"groups": {g: list(ids) for g, ids in RULE_GROUPS.items()}, "rules": list(ALL_RULES),
            "off_by_default": list(DEFAULT_OFF_GROUPS)}


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m src.doc_lint",
                                 description="Report-only Markdown linter (never rewrites).")
    ap.add_argument("files", nargs="*", help="Markdown files")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--rules", default="", help="comma-separated rule ids or groups (default: config)")
    ap.add_argument("--config", default="", help="JSON config file (default: repo config/doc_lint.json)")
    ap.add_argument("--fail-on", default="error", choices=["error", "warning", "info", "never"],
                    help="exit 1 when a finding of at least this severity exists (default error)")
    ap.add_argument("--list-rules", action="store_true", help="print the rule catalogue and exit")
    args = ap.parse_args(argv)
    if args.list_rules:
        print(json.dumps(rules_catalogue(), indent=2))
        return 0
    if not args.files:
        ap.error("no files given")
    rules = [r for r in args.rules.split(",") if r.strip()] or None
    try:
        report = lint_paths(args.files, rules, config_path=args.config or None)
    except ValueError as exc:
        print(f"doc_lint: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(render_report(report))
    if any("error" in f for f in report["files"]):
        return 2
    if args.fail_on == "never":
        return 0
    limit = _SEV_RANK[args.fail_on]
    s = report["summary"]
    return 1 if any(s[sev] for sev in SEVERITIES if _SEV_RANK[sev] <= limit) else 0


if __name__ == "__main__":
    sys.exit(main())
