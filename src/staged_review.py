"""staged_review.py — a staged code review of a working tree.

A model review is the most expensive and least reliable stage of a code
review: it can hallucinate a defect, it can miss one, and every token of its
prompt costs time and money. Most of what a review needs to say is already
knowable without a model at all — a linter finds undefined names in
0.2 seconds, and the project's own tests already say whether the change
broke something. This module runs those cheap, deterministic stages FIRST
and hands their verdicts to the model as tool-confirmed facts, so the model
never re-derives what a linter already proved and can spend its whole
attention budget on the one thing it is actually good for: logic that
contradicts the request, and behaviour the diff quietly deleted.

Stages, in order, each individually skippable and time-bounded:
  1. `collect_diff`       — the unified diff of the working tree against a
                            base ref (tracked changes + untracked new files).
  2. `run_static`         — `src.static_checks` over the changed files,
                            findings kept only on lines the diff ADDED.
  3. `run_affected_tests` — `src.project_tests.related_test_files` run (never
                            the whole suite).
  4. the model stage      — only when an endpoint/model is given and there is
                            a diff to review; reuses `src.auto_review`'s
                            reviewer call and grounding.

One stage failing never prevents the next (the diff stage is the only
exception: no diff means nothing to review). Every function here is safe to
call from a request handler via `asyncio.to_thread` for the blocking parts;
`review_worktree` itself is the async orchestrator.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from src import auto_review
from src import git_panel
from src import project_tests
from src import static_checks

_GIT_TIMEOUT_S = 60.0


def _run_git(workspace: str, *args: str, timeout: float = _GIT_TIMEOUT_S) -> "subprocess.CompletedProcess":
    """`git_panel`'s hardened invocation: the repo being reviewed must not
    choose what runs (`-c core.fsmonitor=`, `-c diff.external=`, no pager,
    the stripped environment). Every `diff` here also passes `--no-ext-diff
    --no-textconv`: with an empty `diff.external` git 2.43 otherwise tries to
    exec an empty command ("external diff died")."""
    if not git_panel.git_available():
        raise git_panel.GitNotFoundError()
    return subprocess.run(
        ["git", *git_panel._HARDENING, *args], cwd=workspace, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout, env=git_panel._git_env(),
    )


logger = logging.getLogger(__name__)

DEFAULT_STATIC_TIMEOUT_S = 60.0
DEFAULT_TESTS_TIMEOUT_S = 300.0
DEFAULT_MODEL_TIMEOUT_S = float(auto_review.DEFAULT_TIMEOUT_S)
MAX_UNTRACKED_FILE_BYTES = 200_000
MAX_FILES_CONSIDERED = 200

STAGES = ("diff", "static", "tests", "model")

# A safe git "committish": refs, SHAs, `HEAD~3`, `origin/main`, `HEAD^`, tags.
# Must start with an alphanumeric (rules out `-x`/`--flag` option injection)
# and contain only characters real refs/SHAs/relative expressions use — never
# whitespace, quotes or shell metacharacters, even though we never invoke a
# shell: a base that git itself would treat as an option is still a bug.
_SAFE_BASE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-/@{}~^:+]*$")

# Codes that mean the code cannot run at all (undefined name, syntax error) —
# everything else `static_checks` reports (unused import, redefinition, an
# f-string with no placeholder, …) is real but not fatal.
_ERROR_CODES = {"F821", "F405", "F403", "E999"}


def _valid_base(base: str) -> bool:
    b = (base or "").strip()
    if not b or b.startswith("-"):
        return False
    return bool(_SAFE_BASE_RE.match(b))


def _is_error_static_code(code: str) -> bool:
    c = (code or "").strip().upper()
    return c in _ERROR_CODES or c.startswith("E9")


def _covers(rel: str, checker: Mapping[str, Any]) -> bool:
    exts = checker.get("exts") or ()
    if "*" in exts:
        return True
    return os.path.splitext(rel)[1].lower() in exts


# ---------------------------------------------------------------------------
# Stage 1 — the diff
# ---------------------------------------------------------------------------

def collect_diff(workspace: str, base: str = "HEAD", paths: Optional[Sequence[str]] = None,
                 max_chars: int = auto_review.MAX_DIFF_CHARS) -> Dict[str, Any]:
    """Unified diff of `workspace` against `base`, plus untracked new files.

    Returns {"base", "files": [rel paths], "diff": str, "truncated": bool,
    "added_lines": {rel: [line numbers]}, "error": optional}. Never raises;
    a workspace that is not a git repository is reported via "error", not an
    exception.
    """
    result: Dict[str, Any] = {
        "base": base, "files": [], "diff": "", "truncated": False, "added_lines": {},
    }
    if not workspace or not os.path.isdir(workspace):
        result["error"] = "workspace is not a directory"
        return result
    if not _valid_base(base):
        result["error"] = f"unsafe or invalid base ref: {base!r}"
        return result
    rel_paths = [p for p in (paths or []) if p]

    if not git_panel.git_available():
        result["error"] = "git is not installed or not on PATH"
        return result

    try:
        probe = _run_git(workspace, "rev-parse", "--is-inside-work-tree")
    except git_panel.GitNotFoundError:
        result["error"] = "git is not installed or not on PATH"
        return result
    except Exception as e:  # noqa: BLE001 - never raise out of a review stage
        result["error"] = f"could not inspect {workspace!r}: {e}"[:300]
        return result
    if probe.returncode != 0 or "true" not in (probe.stdout or "").strip().lower():
        result["error"] = "not a git repository: " + git_panel.stderr_snippet(probe.stderr or probe.stdout or "")
        return result

    try:
        diff_args = ["diff", "--no-ext-diff", "--no-textconv", "--no-color", "--unified=3", base]
        if rel_paths:
            diff_args += ["--", *rel_paths]
        diff_proc = _run_git(workspace, *diff_args)
    except git_panel.GitNotFoundError:
        result["error"] = "git is not installed or not on PATH"
        return result
    except Exception as e:  # noqa: BLE001
        result["error"] = f"git diff failed: {e}"[:300]
        return result
    if diff_proc.returncode not in (0, 1):
        result["error"] = "git diff failed: " + git_panel.stderr_snippet(diff_proc.stderr or diff_proc.stdout or "")
        return result
    tracked_diff = diff_proc.stdout or ""

    # Per-file added lines for the tracked diff: split on each file's own
    # `diff --git a/x b/y` header (still present after the split, minus the
    # literal prefix) so we never re-run git once per file just to get this.
    added_lines: Dict[str, List[int]] = {}
    parts = re.split(r"^diff --git ", tracked_diff, flags=re.M)
    for part in parts[1:]:
        header, _, rest = part.partition("\n")
        m = re.match(r"a/(.+?) b/(.+)$", header.strip())
        if not m:
            continue
        path = m.group(2).strip()
        lines = static_checks.parse_hunks(rest)
        if lines:
            added_lines[path] = sorted(lines)

    # Untracked files, rendered as new-file diffs (skip binaries and large files).
    untracked_chunks: List[str] = []
    try:
        ls_args = ["ls-files", "--others", "--exclude-standard"]
        if rel_paths:
            ls_args += ["--", *rel_paths]
        ls_proc = _run_git(workspace, *ls_args)
    except git_panel.GitNotFoundError:
        ls_proc = None
    except Exception as e:  # noqa: BLE001
        logger.debug("[staged_review] ls-files failed: %s", e)
        ls_proc = None

    untracked_names: List[str] = []
    if ls_proc is not None and ls_proc.returncode == 0:
        untracked_names = [ln.strip() for ln in (ls_proc.stdout or "").splitlines() if ln.strip()]

    for rel in untracked_names[:MAX_FILES_CONSIDERED]:
        abs_p = os.path.join(workspace, *rel.split("/"))
        try:
            if not os.path.isfile(abs_p) or os.path.getsize(abs_p) > MAX_UNTRACKED_FILE_BYTES:
                continue
            with open(abs_p, "rb") as f:
                head = f.read(8192)
            if b"\x00" in head:
                continue  # binary
        except OSError:
            continue
        try:
            d = _run_git(workspace, "diff", "--no-ext-diff", "--no-textconv", "--no-color", "--no-index", "--", "/dev/null", rel)
        except git_panel.GitNotFoundError:
            continue
        except Exception as e:  # noqa: BLE001
            logger.debug("[staged_review] untracked diff for %s failed: %s", rel, e)
            continue
        chunk = d.stdout or ""
        if not chunk:
            continue
        untracked_chunks.append(chunk)
        lines = static_checks.parse_hunks(chunk)
        if lines:
            added_lines[rel] = sorted(lines)

    full_diff = tracked_diff
    if untracked_chunks:
        full_diff = (full_diff + ("\n" if full_diff and not full_diff.endswith("\n") else "")
                    + "\n".join(untracked_chunks))

    truncated = False
    if len(full_diff) > max_chars:
        full_diff = full_diff[:max_chars] + "\n… diff truncated"
        truncated = True

    files = sorted(set(added_lines.keys()) | set(untracked_names))
    # Files whose diff produced no added lines (pure deletions, mode-only
    # changes) still belong in `files` when they were touched.
    for path in re.finditer(r"^diff --git a/(.+?) b/(.+)$", tracked_diff, re.M):
        files.append(path.group(2).strip())
    files = sorted(set(files))

    result.update(diff=full_diff, truncated=truncated, files=files, added_lines=added_lines)
    return result


# ---------------------------------------------------------------------------
# Stage 2 — static analysis, filtered to added lines
# ---------------------------------------------------------------------------

def run_static(workspace: str, files: Sequence[str], added_lines: Mapping[str, Sequence[int]],
               timeout: Optional[float] = None) -> Dict[str, Any]:
    """`static_checks` over `files`, findings kept only on lines `added_lines`
    says this diff added — the same rule `static_checks.run_for_turn` applies
    to an agent turn. Never raises."""
    t0 = time.time()
    res: Dict[str, Any] = {
        "ran": False, "ok": None, "available": False, "tools": [], "findings": [],
        "ignored": 0, "summary": "", "duration_s": 0.0,
    }
    files = [f for f in files if f]
    if not workspace or not files:
        res["summary"] = "no file to analyse"
        res["duration_s"] = round(time.time() - t0, 2)
        return res

    try:
        checkers = static_checks.detect_checkers(workspace)
    except Exception as e:  # noqa: BLE001
        res["summary"] = f"static analysis unavailable: {e}"[:300]
        res["duration_s"] = round(time.time() - t0, 2)
        return res

    covered = [f for f in files if any(_covers(f, c) for c in checkers)]
    if not checkers or not covered:
        res["ok"] = True
        res["summary"] = "no static analysis tool covers the changed files" if checkers else \
            "no static analysis tool available for this workspace"
        res["duration_s"] = round(time.time() - t0, 2)
        return res

    res["available"] = True
    try:
        timeout_f = float(timeout if timeout is not None else DEFAULT_STATIC_TIMEOUT_S)
    except (TypeError, ValueError):
        timeout_f = DEFAULT_STATIC_TIMEOUT_S
    try:
        entries = static_checks.run_for_files(workspace, covered, timeout_f, checkers=checkers)
    except Exception as e:  # noqa: BLE001
        res["summary"] = f"static analysis failed to run: {e}"[:300]
        res["duration_s"] = round(time.time() - t0, 2)
        return res

    res["ran"] = bool(entries)
    res["tools"] = list(dict.fromkeys(e.get("tool") for e in entries if e.get("tool")))
    findings: List[Dict[str, Any]] = []
    ignored = 0
    decided = False
    for entry in entries:
        if entry.get("ok") is None:
            continue  # could not be interpreted for this file: not "clean", not blamed either
        decided = True
        owned: Set[int] = set(added_lines.get(entry["path"]) or ())
        for e in entry.get("errors") or []:
            code = str(e.get("code") or "")
            if e.get("line") in owned:
                findings.append({
                    "source": "static",
                    "severity": "error" if _is_error_static_code(code) else "warning",
                    "file": entry["path"], "line": e.get("line"), "code": code,
                    "issue": ((code + " ") if code else "") + str(e.get("msg") or ""),
                })
            else:
                ignored += 1

    res["findings"] = findings
    res["ignored"] = ignored
    res["ok"] = (not findings) if decided else None
    n_files = len({f["file"] for f in findings})
    tools = ", ".join(t for t in res["tools"] if t) or "static analysis"
    if not decided:
        res["summary"] = f"{tools} could not be interpreted"
    elif findings:
        res["summary"] = f"{len(findings)} finding(s) in {n_files} file(s) ({tools})"
    else:
        res["summary"] = f"clean ({tools})" + (f", {ignored} pre-existing ignored" if ignored else "")
    res["duration_s"] = round(time.time() - t0, 2)
    return res


# ---------------------------------------------------------------------------
# Stage 3 — only the tests related to what changed
# ---------------------------------------------------------------------------

def run_affected_tests(workspace: str, files: Sequence[str], timeout: Optional[float] = None) -> Dict[str, Any]:
    """Run only the test files `project_tests.related_test_files` ties to
    `files` — never the whole suite. `{"ran": False, "reason": "no related
    tests"}` (among other fields) when nothing relates. Never raises."""
    t0 = time.time()
    res: Dict[str, Any] = {"ran": False, "ok": None, "findings": [], "summary": "", "duration_s": 0.0}
    files = [f for f in files if f]
    if not workspace or not files:
        res["reason"] = "no changed file"
        res["summary"] = res["reason"]
        res["duration_s"] = round(time.time() - t0, 2)
        return res

    try:
        related = project_tests.related_test_files(workspace, files)
    except Exception as e:  # noqa: BLE001
        res["reason"] = f"could not resolve related tests: {e}"[:300]
        res["summary"] = res["reason"]
        res["duration_s"] = round(time.time() - t0, 2)
        return res
    if not related:
        res["ran"] = False
        res["ok"] = True
        res["reason"] = "no related tests"
        res["summary"] = res["reason"]
        res["duration_s"] = round(time.time() - t0, 2)
        return res
    res["related_files"] = related

    py_related = [f for f in related if f.endswith(".py")]
    node_related = [f for f in related if project_tests._is_node_test_file(f)]
    notes: List[str] = []
    sub_results: List[Dict[str, Any]] = []

    if py_related:
        try:
            spec = project_tests.detect_test_command(workspace)
        except Exception as e:  # noqa: BLE001
            spec = None
            notes.append(f"test command detection failed: {e}"[:200])
        if spec and spec.get("kind") == "pytest":
            try:
                sub_results.append(project_tests.run_tests(workspace, spec, test_files=py_related, timeout_s=timeout))
            except Exception as e:  # noqa: BLE001
                notes.append(f"pytest run failed: {e}"[:200])
        else:
            notes.append("python test files relate to this change but no pytest runner was detected")

    if node_related:
        try:
            sub_results.append(project_tests.run_node_tests(workspace, node_related, timeout_s=timeout))
        except Exception as e:  # noqa: BLE001
            notes.append(f"node test run failed: {e}"[:200])

    if notes:
        res["notes"] = notes
    if not sub_results:
        res["ran"] = False
        res["ok"] = None
        res["reason"] = "no related tests could be run"
        res["summary"] = "; ".join(notes) or res["reason"]
        res["duration_s"] = round(time.time() - t0, 2)
        return res

    all_failures: List[str] = []
    ran_any = False
    all_ok = True
    any_inconclusive = False
    summaries: List[str] = []
    for r in sub_results:
        if r.get("ran"):
            ran_any = True
            all_ok = all_ok and bool(r.get("ok")) and not r.get("inconclusive")
        if r.get("inconclusive"):
            any_inconclusive = True
        all_failures.extend(r.get("failures") or [])
        if r.get("summary"):
            summaries.append(f"{r.get('kind') or 'tests'}: {r.get('summary')}")

    res["ran"] = ran_any
    res["ok"] = None if (not ran_any or any_inconclusive) else all_ok
    res["inconclusive"] = any_inconclusive
    res["failing"] = [project_tests._failure_id(f) for f in all_failures]
    res["summary"] = "; ".join(summaries) or ("passed" if all_ok else "failed")

    findings: List[Dict[str, Any]] = []
    for item in all_failures:
        fid = project_tests._failure_id(item)
        file_part = fid.split("::", 1)[0] if "::" in fid else fid
        findings.append({"source": "tests", "severity": "error", "file": file_part, "line": None, "issue": item})
    res["findings"] = findings
    res["duration_s"] = round(time.time() - t0, 2)
    return res


# ---------------------------------------------------------------------------
# Stage 4 — the model, told what the tools already proved
# ---------------------------------------------------------------------------

def _tool_confirmed_facts(static_res: Optional[Dict[str, Any]], tests_res: Optional[Dict[str, Any]]) -> str:
    """A short block the model reads before the diff: what the linter and the
    tests already found, so it is not re-derived and the model's attention
    goes to logic, the request, and deleted behaviour instead."""
    lines: List[str] = []
    has_any = False
    if static_res is not None:
        lines.append("Static analysis (tool-confirmed; do not re-derive these):")
        findings = static_res.get("findings") or []
        if findings:
            has_any = True
            for f in findings[:25]:
                lines.append(f"- {f.get('file')}:{f.get('line')}: {f.get('issue')}")
        elif static_res.get("ran"):
            lines.append(f"- clean ({static_res.get('summary') or 'no findings'})")
        else:
            lines.append(f"- not run ({static_res.get('summary') or 'unavailable'})")
    if tests_res is not None:
        lines.append("Related tests (tool-confirmed; do not re-derive these):")
        if tests_res.get("ran"):
            has_any = True
            lines.append(f"- {tests_res.get('summary') or ''}")
            for fid in (tests_res.get("failing") or [])[:25]:
                lines.append(f"  FAILING: {fid}")
        else:
            lines.append(f"- not run ({tests_res.get('reason') or tests_res.get('summary') or 'skipped'})")
    if not has_any:
        return ""
    lines.append(
        "The lines above came from running real tools, not from reading the diff — treat them as "
        "established fact. Focus your review on logic that contradicts the request, and behaviour "
        "the diff deleted or changed silently; do not repeat what is already listed above."
    )
    return "\n".join(lines)


#: A /review of the working tree usually comes with no stated goal; an empty
#: request made a small reviewer answer "this aligns with the user's request".
NO_REQUEST = ("(No request was given. Review the change on its own merits: correctness, edge cases such "
              "as empty or missing inputs, and behaviour it removed. Do not judge it against a request.)")


async def _run_model_stage(workspace: str, diff_res: Dict[str, Any], static_res: Optional[Dict[str, Any]],
                           tests_res: Optional[Dict[str, Any]], request_text: str, endpoint_url: str,
                           model: str, headers: Optional[Dict[str, str]], timeout: float) -> Dict[str, Any]:
    result: Dict[str, Any] = {"ran": False, "ok": None, "findings": [], "summary": "", "model": model}
    diff_text = diff_res.get("diff") or ""
    files = list(diff_res.get("files") or [])
    facts = _tool_confirmed_facts(static_res, tests_res)
    augmented_diff = (facts + "\n\n" + diff_text) if facts else diff_text
    try:
        parsed, err = await auto_review._call_reviewer(
            diff=augmented_diff, files=files, user_text=(request_text or NO_REQUEST), endpoint_url=endpoint_url,
            reviewer=model, headers=headers, tests=tests_res, timeout=timeout, workload="foreground",
        )
    except Exception as e:  # noqa: BLE001 - a review stage never raises
        result["error"] = f"{type(e).__name__}: {e}"[:300]
        result["summary"] = result["error"]
        return result
    if err:
        result["error"] = err
        result["summary"] = err
        return result
    result["ran"] = True
    result["ok"] = parsed.get("verdict") != "issues"
    result["summary"] = parsed.get("summary") or ""
    findings: List[Dict[str, Any]] = []
    for f in parsed.get("findings") or []:
        findings.append({
            "source": "model", "severity": f.get("severity") or "warning",
            "file": f.get("file") or "", "line": f.get("line"), "issue": f.get("issue") or "",
            "evidence": f.get("evidence") or "", "grounded": bool(f.get("grounded")),
        })
    result["findings"] = findings
    return result


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _dedup_and_sort(findings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    deduped = auto_review._dedup_findings(findings)
    deduped.sort(key=lambda f: (0 if f.get("severity") == "error" else 1, f.get("file") or "", f.get("line") or 0))
    return deduped


def _markdown(result: Dict[str, Any]) -> str:
    lines = [f"# Staged review — base `{result.get('base')}`", ""]
    for name in STAGES:
        st = (result.get("stages") or {}).get(name)
        if not st:
            continue
        if not st.get("ran"):
            status = "skipped"
        elif st.get("ok") is True:
            status = "ok"
        elif st.get("ok") is False:
            status = "issues"
        else:
            status = "inconclusive"
        dur = st.get("duration_s")
        dur_str = f"{dur:.2f}s" if isinstance(dur, (int, float)) else "?"
        detail = st.get("summary") or st.get("reason") or st.get("error") or ""
        lines.append(f"- **{name}**: {status} ({dur_str})" + (f" — {detail}" if detail else ""))
    lines.append("")
    lines.append(f"**Verdict:** {result.get('verdict')} — {result.get('summary')}")
    findings = result.get("findings") or []
    if findings:
        lines.append("")
        lines.append("## Findings")
        by_file: Dict[str, List[Dict[str, Any]]] = {}
        for f in findings:
            by_file.setdefault(f.get("file") or "(unknown)", []).append(f)
        for file in sorted(by_file):
            lines.append(f"### {file}")
            for f in by_file[file]:
                loc = f":{f['line']}" if f.get("line") else ""
                lines.append(f"- [{f.get('severity')}] ({f.get('source')}) {file}{loc}: {f.get('issue')}")
    return "\n".join(lines)


async def review_worktree(
    workspace: str, *, base: str = "HEAD", paths: Optional[Sequence[str]] = None, request: str = "",
    endpoint_url: Optional[str] = None, model: Optional[str] = None, headers: Optional[Dict[str, str]] = None,
    stages: Sequence[str] = STAGES, timeout_s: Optional[float] = None,
) -> Dict[str, Any]:
    """Run the staged review. Always returns a dict; never raises.

    {"base", "files", "stages": {name: {"ran","ok","duration_s", ...}},
    "findings": [...], "verdict": "ok"|"issues"|"error", "summary", "markdown"}.
    """
    import asyncio

    t0 = time.time()
    wanted = {s for s in (stages or ()) if s in STAGES}
    result: Dict[str, Any] = {
        "base": base, "files": [], "stages": {}, "findings": [], "verdict": "ok", "summary": "",
    }

    if "diff" not in wanted:
        result["stages"]["diff"] = {"ran": False, "ok": None, "duration_s": 0.0, "reason": "stage skipped"}
        result["verdict"] = "error"
        result["summary"] = "the diff stage is required and was skipped"
        result["duration_s"] = round(time.time() - t0, 2)
        result["markdown"] = _markdown(result)
        return result

    t1 = time.time()
    try:
        diff_res = await asyncio.to_thread(collect_diff, workspace, base, paths, auto_review.MAX_DIFF_CHARS)
    except Exception as e:  # noqa: BLE001
        diff_res = {"error": f"{type(e).__name__}: {e}"[:300]}
    diff_dur = round(time.time() - t1, 2)
    result["base"] = diff_res.get("base", base)
    result["files"] = diff_res.get("files") or []

    if diff_res.get("error"):
        result["stages"]["diff"] = {"ran": True, "ok": False, "duration_s": diff_dur, "error": diff_res["error"]}
        result["verdict"] = "error"
        result["summary"] = diff_res["error"]
        result["duration_s"] = round(time.time() - t0, 2)
        result["markdown"] = _markdown(result)
        return result

    has_diff = bool((diff_res.get("diff") or "").strip())
    result["stages"]["diff"] = {
        "ran": True, "ok": True, "duration_s": diff_dur,
        "files": len(result["files"]), "truncated": bool(diff_res.get("truncated")),
        "summary": (f"{len(result['files'])} file(s) changed" if has_diff else "no changes"),
    }
    if not has_diff:
        result["verdict"] = "ok"
        result["summary"] = "no changes to review"
        result["duration_s"] = round(time.time() - t0, 2)
        result["markdown"] = _markdown(result)
        return result

    findings: List[Dict[str, Any]] = []

    static_res: Optional[Dict[str, Any]] = None
    if "static" in wanted:
        static_timeout = min(float(timeout_s), DEFAULT_STATIC_TIMEOUT_S) if timeout_s else DEFAULT_STATIC_TIMEOUT_S
        try:
            static_res = await asyncio.to_thread(
                run_static, workspace, result["files"], diff_res.get("added_lines") or {}, static_timeout)
        except Exception as e:  # noqa: BLE001
            static_res = {"ran": False, "ok": None, "duration_s": 0.0, "findings": [],
                          "error": f"{type(e).__name__}: {e}"[:300]}
        result["stages"]["static"] = static_res
        findings.extend(static_res.get("findings") or [])
    else:
        result["stages"]["static"] = {"ran": False, "ok": None, "duration_s": 0.0, "reason": "stage skipped"}

    tests_res: Optional[Dict[str, Any]] = None
    if "tests" in wanted:
        tests_timeout = min(float(timeout_s), DEFAULT_TESTS_TIMEOUT_S) if timeout_s else DEFAULT_TESTS_TIMEOUT_S
        try:
            tests_res = await asyncio.to_thread(run_affected_tests, workspace, result["files"], tests_timeout)
        except Exception as e:  # noqa: BLE001
            tests_res = {"ran": False, "ok": None, "duration_s": 0.0, "findings": [],
                        "error": f"{type(e).__name__}: {e}"[:300]}
        result["stages"]["tests"] = tests_res
        findings.extend(tests_res.get("findings") or [])
    else:
        result["stages"]["tests"] = {"ran": False, "ok": None, "duration_s": 0.0, "reason": "stage skipped"}

    if "model" in wanted and endpoint_url and model:
        model_timeout = min(float(timeout_s), DEFAULT_MODEL_TIMEOUT_S) if timeout_s else DEFAULT_MODEL_TIMEOUT_S
        t3 = time.time()
        try:
            model_res = await _run_model_stage(
                workspace, diff_res, static_res, tests_res, request, endpoint_url, model, headers, model_timeout)
        except Exception as e:  # noqa: BLE001
            model_res = {"ran": False, "ok": None, "findings": [], "error": f"{type(e).__name__}: {e}"[:300]}
        model_res["duration_s"] = round(time.time() - t3, 2)
        result["stages"]["model"] = model_res
        findings.extend(model_res.get("findings") or [])
    else:
        reason = "stage skipped" if "model" not in wanted else "no model/endpoint configured"
        result["stages"]["model"] = {"ran": False, "ok": None, "duration_s": 0.0, "reason": reason}

    result["findings"] = _dedup_and_sort(findings)
    n_err = sum(1 for f in result["findings"] if f.get("severity") == "error")
    n_warn = len(result["findings"]) - n_err
    result["verdict"] = "issues" if result["findings"] else "ok"
    result["summary"] = (
        f"{n_err} error(s), {n_warn} warning(s) across {len(result['files'])} file(s)"
        if result["findings"] else f"clean across {len(result['files'])} file(s)"
    )
    result["duration_s"] = round(time.time() - t0, 2)
    result["markdown"] = _markdown(result)
    logger.info("[staged_review] %s: verdict=%s findings=%d in %ss", workspace, result["verdict"],
                len(result["findings"]), result["duration_s"])
    return result
