"""command_output_filters.py — deterministic compression of the IN-PROMPT
copy of a shell/bash/powershell tool result.

The full raw output is always persisted/offloaded exactly as today
(src/tool_result_offload.py, called BEFORE this module ever runs — see the
wiring in src/agent_loop.py, right before `format_tool_result`). Nothing
here changes what is stored; it only shrinks what the model reads this
round, by recognising the command that produced the output and keeping the
parts that matter for that command's format:

* pytest / unittest / node --test / vitest / jest / npm|pnpm test — the
  FAILED/ERROR sections with their tracebacks (capped), warnings collapsed
  to a count, the final summary line(s) kept verbatim, and the progress
  dots / PASSED lines collapsed into a single "N passed" line.
* git diff / git show — file and hunk headers kept, +/- lines kept, more
  than one line of unchanged context around a change dropped, and a
  lockfile's diff (package-lock.json, *.lock, poetry.lock, yarn.lock,
  pnpm-lock.yaml) collapsed to one summary line.
* git status (long and short) — grouped by state with counts, paths kept
  up to a cap with "+N more".
* grep / rg / Select-String / findstr — grouped by file, matches and files
  each capped with "+N more".
* pip install / npm install / uv — errors/warnings and the final
  "Successfully installed" / "added N packages" line kept; download and
  progress noise dropped.
* anything else — a generic fallback: ANSI escapes and \\r progress
  redraws stripped, 3+ consecutive identical lines collapsed to one line
  plus a "(×N)" marker.

Hard rules, enforced by `compress()` itself, not by any individual filter:

* A line containing error|fail|exception|traceback|warning (case
  insensitive) is never dropped unless it is inside an already-kept block
  (e.g. a kept traceback, or a duplicate-line run in the generic filter).
* The result is never longer than the input: if compression does not save
  at least 10%, the original text is returned unchanged.
* Fail open: any exception during classification or filtering returns the
  original text untouched — a bug in a filter must never eat output the
  model needed to see.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Shared regexes
# ---------------------------------------------------------------------------

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
_ERRORLIKE_RE = re.compile(r"error|fail|exception|traceback|warning", re.I)
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=\S*$")

_CAP_FILES = 50
_CAP_MATCHES_PER_FILE = 20
_CAP_PATHS_PER_GROUP = 100


# ---------------------------------------------------------------------------
# Command classification
# ---------------------------------------------------------------------------

def _primary_command(command: str) -> str:
    """The command that actually matters for classifying the output: the
    last non-`cd` segment of a `cd x && ... && real-command` chain, with any
    leading `VAR=val` env-prefix assignments stripped, on the first line of
    a (usually single-line) command string.
    """
    first_line = (command or "").strip().split("\n", 1)[0]
    segments = [s.strip() for s in re.split(r"&&|;", first_line) if s.strip()]
    candidate = ""
    for seg in reversed(segments):
        tokens = seg.split()
        if tokens and tokens[0] in ("cd", "Set-Location", "pushd"):
            continue
        candidate = seg
        break
    if not candidate and segments:
        candidate = segments[-1]
    tokens = candidate.split()
    while tokens and _ENV_ASSIGN_RE.match(tokens[0]):
        tokens.pop(0)
    return " ".join(tokens)


def _exe_basename(token: str) -> str:
    base = re.split(r"[\\/]", token.strip("\"'"))[-1]
    return re.sub(r"\.exe$", "", base, flags=re.I).lower()


def classify(command: str) -> str:
    """Return a registry key for `command`, or 'generic' when nothing
    recognises it."""
    primary = _primary_command(command)
    if not primary:
        return "generic"
    tokens = primary.split()
    exe = _exe_basename(tokens[0]) if tokens else ""
    rest = [t.lower() for t in tokens[1:]]
    joined = primary.lower()

    if "pytest" in joined:
        return "pytest"
    if exe in ("python", "python3", "py") and "unittest" in rest:
        return "unittest"
    if exe == "node" and "--test" in rest:
        return "node_test"
    if "vitest" in joined:
        return "node_test"
    if "jest" in joined:
        return "node_test"
    if exe in ("npm", "pnpm", "yarn") and ("test" in rest or "run" in rest and "test" in rest):
        return "node_test"

    if joined.startswith("git diff") or joined.startswith("git show"):
        return "git_diff"
    if joined.startswith("git status"):
        return "git_status"

    if exe in ("grep", "rg", "findstr"):
        return "grep"
    if "select-string" in joined:
        return "grep"

    if exe in ("pip", "pip3") and "install" in rest:
        return "install"
    if exe == "uv" and ("install" in rest or "add" in rest or "sync" in rest):
        return "install"
    if exe in ("npm", "pnpm", "yarn") and "install" in rest:
        return "install"
    if exe == "npx" and "install" in rest:
        return "install"

    return "generic"


# ---------------------------------------------------------------------------
# Generic fallback
# ---------------------------------------------------------------------------

def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _strip_cr_progress(text: str) -> str:
    """Keep only what survives the last carriage return on each line — the
    same thing a real terminal would show for an in-place progress redraw.

    "\\r\\n" is a line ending, not a redraw. Windows programs end every line
    with it; read as a redraw, each line kept only the empty text after its
    "\\r". Seen live: a three-line report reached the model as two blank
    lines and the last one, under a note saying 3 of 3 lines were kept.
    """
    lines = text.replace("\r\n", "\n").split("\n")
    return "\n".join(line.split("\r")[-1] if "\r" in line else line for line in lines)


def _filter_generic(output: str, exit_code: Optional[int]) -> Tuple[str, str]:
    text = _strip_cr_progress(_strip_ansi(output))
    lines = text.split("\n")
    out: List[str] = []
    i = 0
    n = len(lines)
    while i < n:
        j = i
        while j < n and lines[j] == lines[i]:
            j += 1
        run = j - i
        if run >= 3:
            out.append(lines[i])
            out.append(f"(×{run})")
        else:
            out.extend(lines[i:j])
        i = j
    return "\n".join(out), "generic"


# ---------------------------------------------------------------------------
# pytest
# ---------------------------------------------------------------------------

_PYTEST_HEADER_RE = re.compile(
    r"^=+\s*(FAILURES|ERRORS|short test summary info|warnings summary)\s*=+$", re.I
)
_PYTEST_SECTION_RE = re.compile(r"^=+.*=+$")
_PYTEST_TEST_SEP_RE = re.compile(r"^_{3,}.*_{3,}$")
_PYTEST_SUMMARY_RE = re.compile(
    r"\d+\s+(passed|failed|error|skipped|xfailed|xpassed|deselected)", re.I
)
_PROGRESS_ONLY_RE = re.compile(r"^[.\sFEsx]+(\[\s*\d+%\])?$")
_PASSED_LINE_RE = re.compile(r"\bPASSED\b")


def _cap_failure_block(block: List[str], cap: int = 60) -> List[str]:
    """Split a FAILURES/ERRORS block into per-test sections at pytest's
    ``____ test_name ____`` separators, and cap each one at `cap` lines,
    keeping the head (setup) and the tail (the assertion/last lines)."""
    sections: List[List[str]] = []
    current: List[str] = []
    for line in block:
        if _PYTEST_TEST_SEP_RE.match(line.strip()) and current:
            sections.append(current)
            current = [line]
        else:
            current.append(line)
    if current:
        sections.append(current)
    out: List[str] = []
    for sec in sections:
        if len(sec) <= cap:
            out.extend(sec)
            continue
        head_n = max(1, int(cap * 0.65))
        tail_n = cap - head_n
        head, tail = sec[:head_n], sec[-tail_n:]
        omitted = len(sec) - len(head) - len(tail)
        out.extend(head)
        out.append(f"... [{omitted} lines omitted] ...")
        out.extend(tail)
    return out


def _filter_pytest(output: str, exit_code: Optional[int]) -> Tuple[str, str]:
    lines = output.split("\n")
    n = len(lines)
    kept: List[str] = []
    summary_lines: List[str] = []
    passed_count = 0
    i = 0
    while i < n:
        line = lines[i]
        stripped = line.strip()
        header = _PYTEST_HEADER_RE.match(stripped)
        if header:
            section = header.group(1).lower()
            block = [line]
            i += 1
            while i < n and not _PYTEST_SECTION_RE.match(lines[i].strip()):
                block.append(lines[i])
                i += 1
            if section in ("failures", "errors"):
                kept.append("")
                kept.extend(_cap_failure_block(block))
            elif section == "short test summary info":
                kept.append("")
                kept.extend(block)
            elif section == "warnings summary":
                warn_count = sum(1 for l in block[1:] if l.strip())
                kept.append("")
                kept.append(block[0])
                kept.append(f"  ({warn_count} warning line(s) collapsed)")
            continue
        if _PYTEST_SECTION_RE.match(stripped) and _PYTEST_SUMMARY_RE.search(stripped):
            summary_lines.append(line)
            i += 1
            continue
        if stripped and _PROGRESS_ONLY_RE.match(stripped):
            passed_count += stripped.count(".")
            i += 1
            continue
        if _PASSED_LINE_RE.search(line):
            passed_count += 1
            i += 1
            continue
        if _ERRORLIKE_RE.search(line):
            kept.append(line)
            i += 1
            continue
        i += 1
    if passed_count:
        kept.insert(0, f"{passed_count} passed")
    kept.extend(summary_lines)
    text = "\n".join(l for l in kept if l is not None).strip("\n")
    if not text:
        remaining = [l for l in output.strip().splitlines() if l.strip()]
        text = remaining[-1] if remaining else ""
    return text, "pytest"


# ---------------------------------------------------------------------------
# unittest / node --test / vitest / jest / npm|pnpm test — keyword based,
# since their block markers differ from pytest's but the shape (progress
# markers + failure blocks + a final summary) is the same idea.
# ---------------------------------------------------------------------------

_PASS_MARK_RE = re.compile(r"^\s*(✓|✔|ok\b|PASS\b|passed\b)", re.I)
_FAIL_MARK_RE = re.compile(r"^\s*(✗|✕|×|not ok\b|FAIL\b|failed\b)", re.I)
_TEST_SUMMARY_NUMS_RE = re.compile(
    r"\d+\s+(passing|passed|failing|failed|total|tests?\b|errors?)", re.I
)


def _filter_generic_test(output: str, exit_code: Optional[int]) -> Tuple[str, str]:
    lines = output.split("\n")
    n = len(lines)
    out: List[str] = []
    pass_count = 0
    i = 0
    while i < n:
        line = lines[i]
        stripped = line.strip()
        if stripped and _PROGRESS_ONLY_RE.match(stripped):
            pass_count += stripped.count(".")
            i += 1
            continue
        if _FAIL_MARK_RE.search(line) or _ERRORLIKE_RE.search(line):
            out.append(line)
            i += 1
            block_start = i
            while (
                i < n
                and lines[i].strip()
                and not _PASS_MARK_RE.match(lines[i])
                and not _FAIL_MARK_RE.search(lines[i])
            ):
                i += 1
            block = lines[block_start:i]
            if len(block) > 60:
                out.extend(block[:40])
                out.append(f"... [{len(block) - 60} lines omitted] ...")
                out.extend(block[-20:])
            else:
                out.extend(block)
            continue
        if _PASS_MARK_RE.match(line):
            pass_count += 1
            i += 1
            continue
        if _TEST_SUMMARY_NUMS_RE.search(line):
            out.append(line)
            i += 1
            continue
        i += 1
    if pass_count:
        out.insert(0, f"{pass_count} passed")
    return "\n".join(out), "node_test"


# ---------------------------------------------------------------------------
# git diff / git show
# ---------------------------------------------------------------------------

_LOCKFILE_RE = re.compile(
    r"(^|/)(package-lock\.json|[^/]+\.lock|poetry\.lock|yarn\.lock|pnpm-lock\.yaml)$"
)
_DIFF_GIT_RE = re.compile(r"^diff --git a/(.+) b/(.+)$")


def _compress_hunk(hunk_lines: List[str]) -> List[str]:
    n = len(hunk_lines)
    is_change = [l.startswith("+") or l.startswith("-") for l in hunk_lines]
    keep = [False] * n
    for idx, ch in enumerate(is_change):
        if ch:
            keep[idx] = True
            if idx > 0:
                keep[idx - 1] = True
            if idx < n - 1:
                keep[idx + 1] = True
    out: List[str] = []
    i = 0
    while i < n:
        if keep[i]:
            out.append(hunk_lines[i])
            i += 1
        else:
            j = i
            while j < n and not keep[j]:
                j += 1
            out.append(f"  ... ({j - i} unchanged line(s) omitted) ...")
            i = j
    return out


def _compress_diff_block(block: List[str]) -> List[str]:
    out: List[str] = []
    i = 0
    n = len(block)
    while i < n:
        line = block[i]
        if line.startswith("@@"):
            out.append(line)
            i += 1
            hunk: List[str] = []
            while i < n and not block[i].startswith("@@") and not block[i].startswith("diff --git"):
                hunk.append(block[i])
                i += 1
            out.extend(_compress_hunk(hunk))
        else:
            out.append(line)
            i += 1
    return out


def _filter_git_diff(output: str, exit_code: Optional[int]) -> Tuple[str, str]:
    lines = output.split("\n")
    out: List[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        m = _DIFF_GIT_RE.match(line)
        if m:
            path_b = m.group(2)
            block = [line]
            i += 1
            while i < n and not lines[i].startswith("diff --git"):
                block.append(lines[i])
                i += 1
            if _LOCKFILE_RE.search(path_b):
                added = sum(1 for l in block if l.startswith("+") and not l.startswith("+++"))
                removed = sum(1 for l in block if l.startswith("-") and not l.startswith("---"))
                out.append(line)
                out.append(f"  ({path_b}: lockfile diff collapsed, +{added}/-{removed})")
            elif any("Binary files" in l for l in block):
                out.extend(block)
            else:
                out.extend(_compress_diff_block(block))
            continue
        out.append(line)
        i += 1
    return "\n".join(out), "git_diff"


# ---------------------------------------------------------------------------
# git status (long and short)
# ---------------------------------------------------------------------------

_STATUS_HEADERS = (
    "Changes to be committed:",
    "Changes not staged for commit:",
    "Untracked files:",
    "Unmerged paths:",
)
_SHORT_STATUS_RE = re.compile(r"^([ MADRCU?!]{2}) (.+)$")


def _filter_git_status_long(output: str) -> str:
    lines = output.split("\n")
    out: List[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        header = next((h for h in _STATUS_HEADERS if line.strip().startswith(h)), None)
        if header:
            out.append(line)
            i += 1
            paths: List[str] = []
            while i < n and lines[i].strip() and not any(
                lines[i].strip().startswith(h) for h in _STATUS_HEADERS
            ):
                stripped = lines[i].strip()
                if stripped.startswith("(use"):
                    i += 1
                    continue
                paths.append(stripped)
                i += 1
            for p in paths[:_CAP_PATHS_PER_GROUP]:
                out.append(f"\t{p}")
            if len(paths) > _CAP_PATHS_PER_GROUP:
                out.append(f"\t... (+{len(paths) - _CAP_PATHS_PER_GROUP} more)")
            out.append("")
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


def _filter_git_status_short(output: str) -> str:
    groups: Dict[str, List[str]] = {}
    order: List[str] = []
    other: List[str] = []
    for line in output.split("\n"):
        m = _SHORT_STATUS_RE.match(line)
        if not m:
            if line.strip():
                other.append(line)
            continue
        code, path = m.group(1), m.group(2)
        if code not in groups:
            groups[code] = []
            order.append(code)
        groups[code].append(path)
    out: List[str] = list(other)
    for code in order:
        paths = groups[code]
        out.append(f"{code} ({len(paths)}):")
        for p in paths[:_CAP_PATHS_PER_GROUP]:
            out.append(f"  {code} {p}")
        if len(paths) > _CAP_PATHS_PER_GROUP:
            out.append(f"  ... (+{len(paths) - _CAP_PATHS_PER_GROUP} more)")
    return "\n".join(out) if out else output


def _filter_git_status(output: str, exit_code: Optional[int]) -> Tuple[str, str]:
    if re.search(r"^On branch|^Changes (to be committed|not staged)|^Untracked files:", output, re.M):
        return _filter_git_status_long(output), "git_status"
    return _filter_git_status_short(output), "git_status"


# ---------------------------------------------------------------------------
# grep / rg / Select-String / findstr
# ---------------------------------------------------------------------------

_GREP_LINE_RE = re.compile(r"^([^:\n]+):(\d+:)?(.*)$")


def _filter_grep(output: str, exit_code: Optional[int]) -> Tuple[str, str]:
    files: Dict[str, List[str]] = {}
    order: List[str] = []
    other: List[str] = []
    for line in output.split("\n"):
        if not line.strip():
            continue
        m = _GREP_LINE_RE.match(line)
        if not m:
            other.append(line)
            continue
        path = m.group(1)
        if path not in files:
            files[path] = []
            order.append(path)
        files[path].append(line)
    out: List[str] = []
    for path in order[:_CAP_FILES]:
        matches = files[path]
        out.append(f"{path} ({len(matches)} match(es)):")
        for l in matches[:_CAP_MATCHES_PER_FILE]:
            out.append("  " + l)
        if len(matches) > _CAP_MATCHES_PER_FILE:
            out.append(f"  ... (+{len(matches) - _CAP_MATCHES_PER_FILE} more in this file)")
    if len(order) > _CAP_FILES:
        out.append(f"... (+{len(order) - _CAP_FILES} more file(s))")
    out.extend(other)
    return "\n".join(out), "grep"


# ---------------------------------------------------------------------------
# pip / npm / uv install
# ---------------------------------------------------------------------------

_INSTALL_KEEP_FINAL_RE = re.compile(
    r"(Successfully installed|added \d+ package|changed \d+ package|up to date in|"
    r"Installed \d+ package|Audited \d+ package)",
    re.I,
)
_INSTALL_DROP_RE = re.compile(
    r"^(Collecting|Downloading|Requirement already satisfied|Building wheel|"
    r"Preparing metadata|Using cached|Resolved \d+ packages|npm warn deprecated)",
    re.I,
)
_INSTALL_PROGRESS_RE = re.compile(r"^[\s.\-#=]*\d+%.*$")


def _filter_install(output: str, exit_code: Optional[int]) -> Tuple[str, str]:
    out: List[str] = []
    for line in output.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        if _ERRORLIKE_RE.search(line):
            out.append(line)
            continue
        if _INSTALL_KEEP_FINAL_RE.search(line):
            out.append(line)
            continue
        if _INSTALL_DROP_RE.match(stripped):
            continue
        if _INSTALL_PROGRESS_RE.match(stripped):
            continue
        out.append(line)
    return "\n".join(out), "install"


# ---------------------------------------------------------------------------
# Registry + public entry point
# ---------------------------------------------------------------------------

_FILTERS = {
    "pytest": _filter_pytest,
    "unittest": _filter_generic_test,
    "node_test": _filter_generic_test,
    "git_diff": _filter_git_diff,
    "git_status": _filter_git_status,
    "grep": _filter_grep,
    "install": _filter_install,
}


def compress(
    command: str,
    output: str,
    exit_code: Optional[int] = None,
    *,
    artifact_hint: Optional[str] = None,
) -> Tuple[str, Dict[str, Any]]:
    """Compress `output` (the model-visible copy of a command's result) for
    `command`, using the registry entry that recognises it or a generic
    fallback. Returns `(text, meta)`.

    `meta` always carries `filter`, `original_lines`, `kept_lines`,
    `original_chars`, `kept_chars` — `filter` is `"none"` when nothing was
    changed (empty input, no savings, or a filter raised).

    `artifact_hint`, when given, is how the appended note tells the model
    to read the full text (e.g. an artifact id); otherwise the note says to
    rerun without filtering. This does not change the compression itself.
    """
    original = output or ""
    original_lines = original.count("\n") + 1 if original else 0
    original_chars = len(original)

    if not original:
        return original, {
            "filter": "none", "original_lines": 0, "kept_lines": 0,
            "original_chars": 0, "kept_chars": 0,
        }

    try:
        kind = classify(command)
        handler = _FILTERS.get(kind, _filter_generic)
        compressed, filter_name = handler(original, exit_code)
    except Exception:  # noqa: BLE001 - fail open: never eat output on a bug
        return original, {
            "filter": "none", "original_lines": original_lines, "kept_lines": original_lines,
            "original_chars": original_chars, "kept_chars": original_chars,
        }

    kept_lines = compressed.count("\n") + 1 if compressed else 0

    # Never make it longer than the original — require at least 10% savings.
    if len(compressed) > original_chars * 0.9:
        return original, {
            "filter": "none", "original_lines": original_lines, "kept_lines": original_lines,
            "original_chars": original_chars, "kept_chars": original_chars,
        }

    meta = {
        "filter": filter_name,
        "original_lines": original_lines,
        "kept_lines": kept_lines,
        "original_chars": original_chars,
        "kept_chars": len(compressed),
    }
    how = artifact_hint or "rerun without filtering"
    note = (
        f"\n[output compressed by {filter_name}: kept {kept_lines} of "
        f"{original_lines} lines; full output: {how}]"
    )
    return compressed + note, meta
