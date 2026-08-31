"""code_refs.py — `path:line` references pasted into the message.

`file_mentions.py` covers the paths a user *picks* in the composer. This module
covers the ones they paste without thinking about it: a Python traceback, a
pytest failure line, a Node stack. That is the most common way a coding request
arrives ("me peta esto" + the paste), and until now it received no treatment at
all — `MENTION_RE` does not even keep the `:42` of an `@src/app.py:42`.

The paste already says which file and which line. Making the model rediscover
that costs a 9B model two or three rounds of grep + read_file (60-90 s), and
sometimes it "fixes" a neighbouring file instead. So the frames are parsed here
and the lines around each one ride along with the turn, exactly like a small
mentioned file does.

Two rules keep this from becoming a leak or a distraction:

  * only frames that resolve INSIDE the workspace are inlined — `site-packages`,
    `node_modules` and the stdlib are named as "not your code" and nothing else
    (they are also the frames that used to look like files the user named and
    could not be found, i.e. a false `target_substituted` round); and
  * the containment, secret and binary rules of `file_mentions.context_text`
    apply unchanged — the helpers are imported, not re-implemented, so a symlink
    that escapes the workspace cannot inline what it points at.

Stdlib only, never raises out of the public helpers.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Tuple, Union

from src.file_mentions import (
    _SECRET_NAME_RE,
    _contained,
    _index,
    _normalise_rel,
    _read_head,
    _sensitive,
    _setting,
)

logger = logging.getLogger(__name__)

DEFAULT_RADIUS = 25
_DEFAULT_BUDGET_CHARS = 4000
_MAX_FILES = 5
_MAX_REFS = 60
# Enough of the file to reach a deep frame; _read_head still sniffs for binary
# and normalises CRLF, and appends its own truncation marker which we drop.
_MAX_SOURCE_CHARS = 400_000
_TRUNCATION_MARK = "\n… (truncated — read_file for the rest)"

# Extensions a frame may point at. Requiring one is what keeps `host:8080` and
# `TODO:42` out: a code reference names a file, and a file has an extension.
_CODE_EXT = (
    "py|pyi|pyw|js|jsx|mjs|cjs|ts|tsx|vue|svelte|go|rs|java|kt|kts|scala|rb|"
    "php|c|h|cc|cpp|cxx|hpp|hh|cs|swift|m|mm|sh|bash|zsh|ps1|sql|html|htm|css|"
    "scss|less|json|yaml|yml|toml|ini|cfg|md|txt|lua|pl|r|dart|ex|exs|erl|tf"
)

# `File "…", line 42` — CPython, and every framework that prints its frames.
_PY_TB_RE = re.compile(
    r"""File\s+["']([^"'\n]{1,400})["']\s*,\s*line\s+(\d{1,7})""", re.I)

# `tests/test_x.py::TestC::test_foo` — a pytest node id has no line number, so
# the window is centred on the named test once the file is on disk.
_PYTEST_NODE_RE = re.compile(
    r"(?<![\w:./\\-])((?:[A-Za-z]:[\\/]|[\\/])?(?:[\w.+-]+[\\/])*[\w.+-]+\.py)"
    r"((?:::[\w\[\].+-]+)+)")

# `path:line[:col]`, the shape shared by pytest failure lines, Node stacks,
# ripgrep/eslint output and everything else. An optional drive letter is part
# of the PATH, never the line: `C:\proj\a.py` must not read as line "\proj…".
_GENERIC_RE = re.compile(
    r"(?<![\w:./\\-])((?:[A-Za-z]:[\\/]|[\\/])?(?:[\w.+-]+[\\/])*[\w.+-]+"
    r"\.(?:" + _CODE_EXT + r")):(\d{1,7})(?::(\d{1,7}))?(?![\d\w])", re.I)

# Masked before scanning so a port never reads as a line number and a path
# inside a URL is never treated as a local file.
_URL_RE = re.compile(r"\b[a-z][a-z0-9+.-]{1,15}://\S+", re.I)

_NODE_EXT_RE = re.compile(r"\.(?:js|jsx|mjs|cjs|ts|tsx|vue|svelte)$", re.I)
_TESTISH_RE = re.compile(
    r"(?:^|[/\\])tests?[/\\]|(?:^|[/\\])(?:test_[^/\\]+|[^/\\]+_test)\.\w+$", re.I)

# Frames that are NOT the user's code: dependencies, virtualenvs, the stdlib,
# and the pseudo-files CPython prints for frozen/eval'd code.
_VENDOR_FRAME_RE = re.compile(
    r"(?:^|[/\\])(?:site-packages|dist-packages|node_modules|\.?venv|\.tox|"
    r"\.nox|vendor|third_party|bower_components)(?:[/\\])"
    r"|(?:^|[/\\])lib[/\\]python\d[\d.]*[/\\]"
    r"|[/\\]python\d[\d.]*[/\\](?:lib|Lib)[/\\]"
    r"|[/\\]Python\d*[/\\]Lib[/\\]"
    r"|(?:^|[/\\])(?:usr|opt)[/\\](?:lib|lib64)[/\\]"
    r"|(?:^|[/\\])importlib[/\\]_bootstrap"
    r"|^<[^>\n]{0,120}>$",
    re.I,
)


class CodeRef(NamedTuple):
    """One frame: where it points, and what printed it.

    `line` is None for a pytest node id (`file.py::test_foo`), where `symbol`
    carries the test name instead and the window is centred on its `def`.
    """
    path: str
    line: Optional[int] = None
    col: Optional[int] = None
    source: str = "generic"          # traceback | pytest | node | generic
    symbol: str = ""


def enabled() -> bool:
    return bool(_setting("agent_code_refs", True))


def is_vendor_frame(path: str) -> bool:
    """True for a dependency / stdlib / frozen frame — never the user's code."""
    p = str(path or "")
    if not p:
        return False
    return bool(_VENDOR_FRAME_RE.search(p.replace("\\", "/"))
                or _VENDOR_FRAME_RE.search(p))


# ── parsing ───────────────────────────────────────────────────────────────

def _mask(text: str, start: int, end: int) -> str:
    """Blank a consumed span so later, looser patterns cannot re-read it."""
    return text[:start] + (" " * (end - start)) + text[end:]


def _classify(path: str) -> str:
    """What printed this frame, from the path alone — the `at fn (…)` wrapper of
    a Node stack adds nothing an .js/.ts extension does not already say."""
    if _NODE_EXT_RE.search(path):
        return "node"
    if _TESTISH_RE.search(path):
        return "pytest"
    return "generic"


def _int(raw: Optional[str]) -> Optional[int]:
    try:
        return int(raw) if raw else None
    except (TypeError, ValueError):                        # pragma: no cover
        return None


def extract(text: str) -> List[CodeRef]:
    """Every file/line reference in `text`, in order, de-duplicated.

    Python tracebacks are read first and their spans masked, then pytest node
    ids, then the generic `path:line[:col]` shape — so a frame is classified by
    what actually printed it rather than by whichever pattern ran first.
    """
    if not text:
        return []
    scan = _URL_RE.sub(lambda m: " " * len(m.group(0)), text)
    out: List[CodeRef] = []
    seen = set()

    def add(ref: CodeRef) -> None:
        key = (ref.path.replace("\\", "/").lower(), ref.line, ref.col, ref.symbol)
        if key in seen or len(out) >= _MAX_REFS:
            return
        seen.add(key)
        out.append(ref)

    for m in list(_PY_TB_RE.finditer(scan)):
        add(CodeRef(m.group(1).strip(), _int(m.group(2)), None, "traceback"))
        scan = _mask(scan, m.start(), m.end())

    for m in list(_PYTEST_NODE_RE.finditer(scan)):
        name = m.group(2).strip(":").split("::")[-1].strip()
        add(CodeRef(m.group(1).strip(), None, None, "pytest", name))
        scan = _mask(scan, m.start(), m.end())

    for m in _GENERIC_RE.finditer(scan):
        path = m.group(1).strip()
        add(CodeRef(path, _int(m.group(2)), _int(m.group(3)), _classify(path)))
    return out


# ── resolving against the workspace ───────────────────────────────────────

def _match_rel(path: str, root: str, exact: set, by_lower: Dict[str, str],
               by_base: Dict[str, List[str]]) -> Optional[str]:
    """The workspace-relative path this frame means, or None when it is not ours."""
    p = str(path or "").replace("\\", "/")
    if not p or is_vendor_frame(p):
        return None
    # Absolute and genuinely inside this workspace: trust the filesystem.
    if root and (p.startswith("/") or re.match(r"^[A-Za-z]:/", p)):
        try:
            real = os.path.realpath(p)
        except (OSError, ValueError):                      # pragma: no cover
            real = ""
        if real and os.path.exists(real) and _contained(real, root):
            rel = os.path.relpath(real, root).replace(os.sep, "/")
            if rel in exact or by_lower.get(rel.lower()):
                return by_lower.get(rel.lower(), rel)
            return rel
    rel = _normalise_rel(p)
    if rel in exact:
        return rel
    hit = by_lower.get(rel.lower())
    if hit:
        return hit
    # A traceback is usually absolute and from another checkout ("/home/ci/app/
    # src/a.py" against ~/work/app): the longest indexed path that is a suffix
    # of the frame is the file it means. Only files with the same basename can
    # be that suffix, so this stays O(namesakes) instead of O(whole index).
    low = "/" + rel.lower()
    base = rel.rsplit("/", 1)[-1].lower()
    namesakes = by_base.get(base, [])
    best = ""
    for cand in namesakes:
        cand_low = cand.lower()
        if low.endswith("/" + cand_low) and len(cand_low) > len(best):
            best = cand_low
    if best:
        return by_lower[best]
    if "/" not in rel and len(namesakes) == 1:
        return namesakes[0]
    return None


def resolve(workspace: str, refs: Sequence[CodeRef]) -> Tuple[List[CodeRef], List[CodeRef]]:
    """Split `refs` into (inside, outside) the workspace.

    `inside` refs come back with `path` rewritten to the workspace-relative form
    used everywhere else (forward slashes). `outside` keeps the frame exactly as
    pasted: dependency, stdlib and unknown paths, which the block names but
    never inlines.
    """
    inside: List[CodeRef] = []
    outside: List[CodeRef] = []
    if not refs:
        return inside, outside
    files = _index(workspace) if workspace else []
    try:
        root = os.path.realpath(os.path.expanduser(workspace)) if workspace else ""
    except (OSError, ValueError):                          # pragma: no cover
        root = ""
    exact = set(files)
    by_lower = {f.lower(): f for f in files}
    by_base: Dict[str, List[str]] = {}
    for f in files:
        by_base.setdefault(f.rsplit("/", 1)[-1].lower(), []).append(f)
    for ref in refs:
        rel = _match_rel(ref.path, root, exact, by_lower, by_base)
        (inside if rel else outside).append(ref._replace(path=rel) if rel else ref)
    return inside, outside


# ── the window ────────────────────────────────────────────────────────────

def _source_lines(abs_path: str) -> Optional[List[str]]:
    body = _read_head(abs_path, _MAX_SOURCE_CHARS)
    if body is None:
        return None
    if body.endswith(_TRUNCATION_MARK):
        body = body[: -len(_TRUNCATION_MARK)]
    return body.split("\n")


def window(abs_path: str, line: Union[int, Sequence[int]], radius: int = DEFAULT_RADIUS,
           *, budget_chars: Optional[int] = None) -> str:
    """The lines around `line` (or around every line in a sequence), numbered.

    One window covers several frames in the same file: each pointed line is
    marked with `>` and the rest is context, so the model sees the call site and
    the assertion in one block instead of two overlapping ones. Returns '' when
    the file cannot be read or the lines are past its end (a stale paste).
    """
    wanted = sorted({int(n) for n in (line if isinstance(line, (list, tuple, set))
                                      else [line]) if n and int(n) > 0})
    if not wanted:
        return ""
    src = _source_lines(abs_path)
    if not src:
        return ""
    if src and src[-1] == "":
        src = src[:-1]
    total = len(src)
    wanted = [n for n in wanted if n <= total]
    if not wanted or total == 0:
        return ""
    r = max(0, int(radius))
    while True:
        start = max(1, min(wanted) - r)
        end = min(total, max(wanted) + r)
        width = len(str(end))
        marks = set(wanted)
        out = [("> " if n in marks else "  ") + str(n).rjust(width) + " | " + src[n - 1]
               for n in range(start, end + 1)]
        text = "\n".join(out)
        if budget_chars is None or len(text) <= budget_chars or r == 0:
            break
        r = 0 if r <= 2 else r // 2
    if budget_chars is not None and len(text) > budget_chars:
        # The marker is part of the budget: a window that overruns by its own
        # "truncated" note would push the next file's window over the total.
        tail = "\n  … (truncated)"
        keep = budget_chars - len(tail)
        if keep <= 0:
            return ""
        text = text[:keep].rsplit("\n", 1)[0] + tail
    return text


def _symbol_line(src: Sequence[str], symbol: str) -> Optional[int]:
    """Where `test_foo` is defined, for a pytest node id that carries no line."""
    if not symbol:
        return None
    name = symbol.split("[", 1)[0].strip()     # test_x[param-1] is defined as test_x
    if not name:
        return None
    pat = re.compile(r"^\s*(?:async\s+def|def|class)\s+" + re.escape(name) + r"\b")
    for i, ln in enumerate(src, 1):
        if pat.match(ln):
            return i
    return None


# ── the block the agent loop injects ──────────────────────────────────────

def _merge(lines: List[int], radius: int) -> List[List[int]]:
    """Frames closer than 2·radius share one window instead of two overlapping."""
    groups: List[List[int]] = []
    for n in sorted(set(lines)):
        if groups and n - groups[-1][-1] <= 2 * radius:
            groups[-1].append(n)
        else:
            groups.append([n])
    return groups


_SOURCE_LABEL = {
    "traceback": "python traceback",
    "pytest": "pytest",
    "node": "node stack",
    "generic": "the pasted text",
}


def turn_context(workspace: str, user_text: str, *, budget_chars: Optional[int] = None,
                 exclude: Optional[Sequence[str]] = None,
                 radius: int = DEFAULT_RADIUS) -> Optional[Dict[str, Any]]:
    """{"text", "refs", "outside"} for one turn, or None when there is nothing.

    `exclude` are workspace-relative paths already inlined by another feature
    (the `@` mentions inline whole files): a window of a file the model is
    holding in full is pure duplication.
    """
    if not enabled() or not workspace or not user_text:
        return None
    try:
        refs = extract(user_text)
    except Exception as e:                                 # pragma: no cover
        logger.debug("[code-refs] extract failed: %s", e)
        return None
    if not refs:
        return None
    if budget_chars is None:
        try:
            budget_chars = int(_setting("agent_code_ref_chars", _DEFAULT_BUDGET_CHARS))
        except (TypeError, ValueError):                    # pragma: no cover
            budget_chars = _DEFAULT_BUDGET_CHARS
    budget = max(0, min(int(budget_chars), 60000))
    try:
        inside, outside = resolve(workspace, refs)
    except Exception as e:                                 # pragma: no cover
        logger.debug("[code-refs] resolve failed: %s", e)
        return None
    skip = {_normalise_rel(p).lower() for p in (exclude or [])}
    try:
        root = os.path.realpath(os.path.expanduser(workspace))
    except (OSError, ValueError):                          # pragma: no cover
        root = ""

    per_file: "Dict[str, List[CodeRef]]" = {}
    for ref in inside:
        if ref.path.lower() in skip:
            continue
        per_file.setdefault(ref.path, []).append(ref)

    blocks: List[str] = []
    used: List[CodeRef] = []
    for rel, group in list(per_file.items())[:_MAX_FILES]:
        if budget <= 0:
            break
        abs_path = os.path.join(root, *rel.split("/")) if root else rel
        try:
            real_path = os.path.realpath(abs_path)
        except (OSError, ValueError):                      # pragma: no cover
            real_path = ""
        # Same invariant as file_mentions.context_text: resolve BEFORE reading.
        # The index reports a symlink as an ordinary file, so a frame naming
        # `notas.py` could otherwise paste ~/.aws/credentials into the prompt.
        if not real_path or (root and not _contained(real_path, root)):
            continue
        if _SECRET_NAME_RE.search(rel) or _sensitive(real_path):
            continue
        src = _source_lines(real_path)
        if not src:
            continue
        lines = [r.line for r in group if r.line]
        for r in group:
            if not r.line and r.symbol:
                at = _symbol_line(src, r.symbol)
                if at:
                    lines.append(at)
        if not lines:
            lines = [1]
        for chunk in _merge(lines, radius):
            if budget <= 0:
                break
            body = window(real_path, chunk, radius, budget_chars=budget)
            if not body:
                continue
            budget -= len(body)
            src_label = _SOURCE_LABEL.get(group[0].source, "the pasted text")
            where = ", ".join(f"line {n}" for n in chunk)
            lang = os.path.splitext(rel)[1].lstrip(".") or ""
            blocks.append(f"{rel} — {where} (from {src_label})\n"
                          f"```{lang}\n{body}\n```")
        used.extend(group)

    outside_paths: List[str] = []
    for ref in outside:
        label = ref.path if ref.line is None else f"{ref.path}:{ref.line}"
        if label not in outside_paths:
            outside_paths.append(label)
    if not blocks and not outside_paths:
        return None

    lines_out: List[str] = []
    if blocks:
        lines_out += [
            "The message below pastes an error that points at these workspace "
            "files and lines. This is the code as it is on disk right now, with "
            "real line numbers; the `>` marks the exact line the paste names. "
            "Start here — you do not need to grep or read_file to find it.",
            "",
        ]
        lines_out.append("\n\n".join(blocks))
    if outside_paths:
        if blocks:
            lines_out.append("")
        lines_out.append(
            "These frames are NOT your code — they are dependencies, the "
            "standard library or files outside the workspace. They only show "
            "how execution got there: do not edit them, and do not report them "
            "as missing files: " + ", ".join(outside_paths[:10])
            + (" …" if len(outside_paths) > 10 else ""))
    return {"text": "\n".join(lines_out), "refs": used, "outside": outside_paths}
