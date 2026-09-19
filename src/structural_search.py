"""src/structural_search.py — structural (AST-pattern) code search & rewrite.

Text grep matches characters; the code graph (`src/code_graph/`) matches
*named symbols*. Neither can express "every `except Exception:` whose body
has no logging call" or "replace `foo($A, None)` with `foo($A)` everywhere,
but leave `foo(x, y)` alone" — those are questions about *shape*: a
tree-sitter AST pattern with metavariables (`$VAR` binds one node,
`$$$ARGS` binds zero-or-more). This module answers them by shelling out to
the `ast-grep` CLI (https://ast-grep.github.io/), the same tool VS
Code/Neovim structural-search plugins wrap.

Three entry points, mirroring `src/code_graph/query.py`'s conventions:

  available()               -- is the binary on this machine, and what version
  search(...)                -- run a pattern, return hits with metavariables
  rewrite_preview(...)       -- compute a unified diff, WITHOUT touching disk
  rewrite_apply(...)         -- write the diff's result to disk, workspace-confined

Binary discovery (never the ambiguous `sg`, which collides with other tools
on PATH on several platforms):
  1. The running interpreter's own scripts directory (`Scripts/` on Windows,
     `bin/` elsewhere — i.e. `dirname(sys.executable)`, since that is where a
     venv/`pip install ast-grep-cli` puts `ast-grep(.exe)` right next to
     `python(.exe)`).
  2. `ast-grep` resolved from PATH via `shutil.which("ast-grep")` (explicitly
     that name, never `"sg"`).

Path confinement uses the same helper every other file/search tool in this
codebase uses (`src.tool_execution._resolve_search_root` for reads,
`_resolve_tool_path_in_roots`-backed resolution for writes) so a pattern
can never search or rewrite outside the active workspace.

`rewrite_preview`/`rewrite_apply` do NOT rely on ast-grep's own
`--update-all` file-writing: they ask ast-grep for `--json` output that
includes, per hit, a `replacement` string and `replacementOffsets` (byte
range in the ORIGINAL file), then apply those byte-range substitutions to
the file content this module read itself. That keeps the write path under
the codebase's own path-confinement and lets `rewrite_preview` compute the
exact would-be diff without ever writing a byte — one code path computes
the new content, `preview` reads it back as a diff, `apply` writes it to
the already-confined absolute path.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import difflib
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_MAX_RESULTS = 200
DEFAULT_TIMEOUT = 30
DEFAULT_OUTPUT_CHARS = 20000

# Languages ast-grep's CLI accepts (passed straight through to --lang; this
# is an allowlist for a clear error message, not an enforcement mechanism --
# ast-grep itself rejects anything else).
SUPPORTED_LANGS = {
    "python", "javascript", "typescript", "tsx", "json", "css", "html",
    "rust", "go", "java", "c", "cpp", "csharp", "bash", "yaml",
}

_INSTALL_HINT = "ast-grep binary not found -- install it with: pip install ast-grep-cli"

_cached_binary: Optional[str] = None
_cached_version: Optional[str] = None
_lookup_done = False


def _binary_name() -> str:
    return "ast-grep.exe" if os.name == "nt" else "ast-grep"


def _find_binary() -> Optional[str]:
    """Locate the `ast-grep` executable. Interpreter scripts dir first (a
    venv `pip install ast-grep-cli` puts the binary there), then PATH --
    always the exact name `ast-grep`, never the ambiguous `sg`."""
    name = _binary_name()
    scripts_dir = os.path.dirname(os.path.abspath(sys.executable))
    candidate = os.path.join(scripts_dir, name)
    if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        return candidate
    on_path = shutil.which("ast-grep")
    if on_path:
        return on_path
    return None


def _root(raw: str):
    """Confine a search path to the active workspace (read), same guard
    `grep`/`glob`/`code_graph_*` use."""
    from src.tool_execution import _resolve_search_root
    return _resolve_search_root(raw or "")


def _write_root(raw: str):
    """Confine a rewrite target path (must resolve to an existing file or
    directory the active workspace already permits writing under)."""
    from src.tool_execution import _resolve_tool_path
    return _resolve_tool_path(raw)


def available() -> Dict[str, Any]:
    """`{"available": bool, "version": str|None, "path": str|None, "error": str|None}`."""
    global _cached_binary, _cached_version, _lookup_done
    if not _lookup_done:
        _lookup_done = True
        _cached_binary = _find_binary()
        if _cached_binary:
            try:
                proc = subprocess.run(
                    [_cached_binary, "--version"], capture_output=True, text=True,
                    timeout=10, shell=False)
                _cached_version = (proc.stdout or proc.stderr or "").strip() or None
            except (OSError, subprocess.TimeoutExpired):
                _cached_version = None
    if not _cached_binary:
        return {"available": False, "version": None, "path": None, "error": _INSTALL_HINT}
    return {"available": True, "version": _cached_version, "path": _cached_binary, "error": None}


def _clip(text: str, limit: Optional[int]) -> str:
    text = text or ""
    cap = int(limit) if limit else DEFAULT_OUTPUT_CHARS
    if cap <= 0 or len(text) <= cap:
        return text
    return text[:cap] + f"\n… truncated ({len(text)} chars total, limit {cap})"


def _check_lang(lang: str) -> Optional[str]:
    lang = (lang or "").strip().lower()
    if not lang:
        return "lang is required (one of: " + ", ".join(sorted(SUPPORTED_LANGS)) + ")"
    if lang not in SUPPORTED_LANGS:
        return (f"unsupported lang {lang!r} -- one of: " + ", ".join(sorted(SUPPORTED_LANGS)))
    return None


def _run_json(binary: str, args: List[str], *, cwd: str, timeout: int) -> Tuple[Optional[list], Optional[str]]:
    """Run `ast-grep run ... --json=compact ...`, return (hits, error).

    ast-grep's own exit code is 0 when matches were found and 1 when the
    scan ran cleanly with zero matches -- neither is a failure here. Any
    other exit code (parse error, bad pattern/lang, missing path) is.
    """
    try:
        proc = subprocess.run(
            [binary, "run", *args], cwd=cwd, capture_output=True, text=True,
            timeout=timeout, shell=False)
    except subprocess.TimeoutExpired:
        return None, f"ast-grep timed out after {timeout}s"
    except OSError as exc:
        return None, f"failed to run ast-grep: {exc}"
    if proc.returncode not in (0, 1):
        msg = (proc.stderr or proc.stdout or "ast-grep failed").strip()
        return None, _clip(msg, 2000)
    raw = (proc.stdout or "").strip()
    if not raw:
        return [], None
    try:
        hits = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"could not parse ast-grep JSON output: {exc}"
    return hits if isinstance(hits, list) else [], None


def search(pattern: str, lang: str, path: str = "", *,
           max_results: int = DEFAULT_MAX_RESULTS, context: int = 0,
           output_chars: int = DEFAULT_OUTPUT_CHARS) -> Dict[str, Any]:
    """Run an ast-grep structural pattern over `path` (default: the active
    workspace). `$VAR` in `pattern` binds a single AST node, `$$$VAR` binds
    zero or more (e.g. `except Exception:\\n    $$$BODY` captures the whole
    body of a bare except). Returns hits with file, line, column, the
    matched text and any metavariable bindings, capped at `max_results`.
    """
    info = available()
    if not info["available"]:
        return {"error": info["error"], "exit_code": 1}
    pattern = (pattern or "").strip()
    if not pattern:
        return {"error": "structural_search: pattern is required", "exit_code": 1}
    lang_err = _check_lang(lang)
    if lang_err:
        return {"error": f"structural_search: {lang_err}", "exit_code": 1}
    try:
        resolved = _root(path)
    except ValueError as exc:
        return {"error": f"structural_search: {exc}", "exit_code": 1}

    args = ["--pattern", pattern, "--lang", lang.strip().lower(), "--json=compact"]
    ctx = max(0, int(context or 0))
    if ctx:
        args += ["--context", str(ctx)]
    args.append(resolved)

    cwd = resolved if os.path.isdir(resolved) else os.path.dirname(resolved)
    hits, err = _run_json(info["path"], args, cwd=cwd, timeout=DEFAULT_TIMEOUT)
    if err is not None:
        return {"error": f"structural_search: {err}", "exit_code": 1}

    limit = max(1, int(max_results or DEFAULT_MAX_RESULTS))
    truncated = len(hits) > limit
    hits = hits[:limit]

    results = []
    for h in hits:
        rng = h.get("range") or {}
        start = rng.get("start") or {}
        results.append({
            "file": h.get("file", ""),
            "line": start.get("line"),
            "column": start.get("column"),
            "text": h.get("text", ""),
            "lines": h.get("lines", h.get("text", "")),
            "metaVariables": h.get("metaVariables") or {},
        })

    lines = [f"{r['file']}:{r['line']}:{r['column']}  {r['text'].splitlines()[0] if r['text'] else ''}"
             for r in results]
    if truncated:
        lines.append(f"… truncated at {limit} results")
    text = "\n".join(lines) if lines else "no matches"
    return {
        "output": _clip(text, output_chars), "exit_code": 0,
        "hits": results, "count": len(results), "truncated": truncated,
        "root": resolved, "pattern": pattern, "lang": lang.strip().lower(),
    }


def _hits_with_replacement(binary: str, pattern: str, rewrite: str, lang: str,
                            resolved: str, *, cwd: str) -> Tuple[Optional[List[dict]], Optional[str]]:
    args = ["--pattern", pattern, "--rewrite", rewrite, "--lang", lang, "--json=compact", resolved]
    return _run_json(binary, args, cwd=cwd, timeout=DEFAULT_TIMEOUT)


def _apply_replacements(original: str, hits: List[dict]) -> str:
    """Splice each hit's `replacement` into `original` at its
    `replacementOffsets` (byte offsets in the original file), from last to
    first so earlier offsets stay valid as later ones are applied."""
    data = original.encode("utf-8")
    ordered = sorted(
        (h for h in hits if h.get("replacement") is not None and h.get("replacementOffsets")),
        key=lambda h: h["replacementOffsets"]["start"], reverse=True,
    )
    for h in ordered:
        off = h["replacementOffsets"]
        start, end = int(off["start"]), int(off["end"])
        repl = h["replacement"].encode("utf-8")
        data = data[:start] + repl + data[end:]
    return data.decode("utf-8")


def _compute_rewrite(pattern: str, rewrite: str, lang: str, path: str,
                      *, for_write: bool) -> Dict[str, Any]:
    info = available()
    if not info["available"]:
        return {"error": info["error"], "exit_code": 1}
    pattern = (pattern or "").strip()
    if not pattern:
        return {"error": "structural_rewrite: pattern is required", "exit_code": 1}
    if rewrite is None:
        return {"error": "structural_rewrite: rewrite is required", "exit_code": 1}
    lang_err = _check_lang(lang)
    if lang_err:
        return {"error": f"structural_rewrite: {lang_err}", "exit_code": 1}
    if for_write and not (path or "").strip():
        return {"error": "structural_rewrite: path is required when apply=true "
                          "(rewriting the whole workspace by default would be too risky -- "
                          "name the file or directory to rewrite)", "exit_code": 1}
    try:
        resolved = _write_root(path) if for_write else _root(path)
    except ValueError as exc:
        return {"error": f"structural_rewrite: {exc}", "exit_code": 1}
    lang_norm = lang.strip().lower()

    cwd = resolved if os.path.isdir(resolved) else os.path.dirname(resolved)
    hits, err = _hits_with_replacement(info["path"], pattern, rewrite, lang_norm, resolved, cwd=cwd)
    if err is not None:
        return {"error": f"structural_rewrite: {err}", "exit_code": 1}

    by_file: Dict[str, List[dict]] = {}
    for h in hits:
        by_file.setdefault(h.get("file", ""), []).append(h)

    diffs: Dict[str, str] = {}
    new_contents: Dict[str, str] = {}
    abs_paths: Dict[str, str] = {}
    for rel_or_abs, file_hits in by_file.items():
        file_path = rel_or_abs if os.path.isabs(rel_or_abs) else os.path.join(resolved, rel_or_abs)
        try:
            with open(file_path, "r", encoding="utf-8") as fh:
                original = fh.read()
        except OSError as exc:
            logger.warning("structural_rewrite: could not read %s: %s", file_path, exc)
            continue
        updated = _apply_replacements(original, file_hits)
        if updated == original:
            continue
        diff = "".join(difflib.unified_diff(
            original.splitlines(keepends=True), updated.splitlines(keepends=True),
            fromfile=rel_or_abs, tofile=rel_or_abs,
        ))
        diffs[rel_or_abs] = diff
        new_contents[rel_or_abs] = updated
        abs_paths[rel_or_abs] = file_path

    return {
        "exit_code": 0, "root": resolved, "pattern": pattern, "rewrite": rewrite,
        "lang": lang_norm, "diffs": diffs, "_new_contents": new_contents,
        "_abs_paths": abs_paths, "match_count": len(hits), "files_changed": sorted(diffs.keys()),
    }


def rewrite_preview(pattern: str, rewrite: str, lang: str, path: str = "") -> Dict[str, Any]:
    """Compute what `structural_rewrite` would change, as a unified diff per
    file, WITHOUT writing anything to disk."""
    result = _compute_rewrite(pattern, rewrite, lang, path, for_write=False)
    if "error" in result:
        return result
    result.pop("_new_contents", None)
    result.pop("_abs_paths", None)
    diff_text = "\n".join(result["diffs"].values()) or "no matches -- nothing to rewrite"
    result["output"] = _clip(diff_text, DEFAULT_OUTPUT_CHARS)
    return result


def rewrite_apply(pattern: str, rewrite: str, lang: str, path: str = "") -> Dict[str, Any]:
    """Compute the rewrite (same as `rewrite_preview`) and write the result
    to each affected file, confined to the active workspace."""
    result = _compute_rewrite(pattern, rewrite, lang, path, for_write=True)
    if "error" in result:
        return result
    new_contents = result.pop("_new_contents", {})
    abs_paths = result.pop("_abs_paths", {})
    written: List[str] = []
    errors: List[str] = []
    for rel_or_abs in result["files_changed"]:
        abs_path = abs_paths[rel_or_abs]
        try:
            with open(abs_path, "w", encoding="utf-8", newline="") as fh:
                fh.write(new_contents[rel_or_abs])
            written.append(abs_path)
        except OSError as exc:
            errors.append(f"{rel_or_abs}: {exc}")
    diff_text = "\n".join(result["diffs"].values()) or "no matches -- nothing to rewrite"
    result["output"] = _clip(diff_text, DEFAULT_OUTPUT_CHARS)
    result["files_written"] = written
    if errors:
        result["write_errors"] = errors
    return result
