"""auto_review.py — a second, tool-less pass over the diff of an agent turn.

After a turn that changed files (and after the syntax / project-test checks),
a fresh-context model reads ONLY the user's request and the unified diff of
what changed this turn, and lists obvious defects: broken references, missing
imports, wrong names, logic that contradicts the request, deleted behaviour,
leftover debug code. Cheap (one completion, no tools) and it catches a useful
share of the mistakes local coding models make.

The reviewer can be the same model or another one (setting `agent_auto_review`:
"off" | "same" | "<model name served by the same endpoint>"; a project can
override it with `review_model`). Findings are shown in the chat as a card and
persisted with the turn; `error`-severity findings may trigger ONE bounded fix
round (`agent_auto_review_fix_round`).

Never raises: any failure yields a result with an "error" field and the turn
goes on.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 180
MAX_DIFF_CHARS = 24_000
_THINK_RE = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.S | re.I)
_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.S)
_SEVERITIES = ("error", "warning", "info")


def _setting(key: str, default: Any) -> Any:
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:
        return default


def resolve_reviewer(model: str, project_override: Optional[str] = None) -> Optional[str]:
    """The model to review with, or None when auto-review is off."""
    raw = (project_override or "").strip() or str(_setting("agent_auto_review", "off") or "off").strip()
    low = raw.lower()
    if low in ("", "off", "false", "0", "none", "no"):
        return None
    if low in ("same", "self", "true", "1", "on", "yes"):
        return model
    return raw


# ---------------------------------------------------------------------------
# Diff of the turn
# ---------------------------------------------------------------------------

def _user_git_diff(workspace: str, paths: List[str], max_chars: int) -> str:
    """Fallback without a checkpoint: the user's own git diff for the paths
    (untracked files rendered as additions)."""
    try:
        probe = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=workspace, capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=8)
    except (OSError, subprocess.SubprocessError):
        return ""
    if probe.returncode != 0:
        return ""
    top = os.path.realpath(probe.stdout.strip())
    parts: List[str] = []
    for p in paths:
        abs_p = p if os.path.isabs(p) else os.path.join(workspace, p)
        try:
            rel = os.path.relpath(os.path.realpath(abs_p), top).replace(os.sep, "/")
        except ValueError:
            continue
        if rel.startswith("../"):
            continue
        try:
            st = subprocess.run(["git", "status", "--porcelain", "--", rel], cwd=top, capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=8)
            code = (st.stdout or "")[:2].strip()
            if code.startswith("?"):
                null = "NUL" if os.name == "nt" else "/dev/null"
                d = subprocess.run(["git", "diff", "--no-index", "--no-color", "--", null, rel], cwd=top,
                                   capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
            else:
                d = subprocess.run(["git", "diff", "--no-color", "HEAD", "--", rel], cwd=top, capture_output=True,
                                   text=True, encoding="utf-8", errors="replace", timeout=15)
            if d.stdout:
                parts.append(d.stdout)
        except (OSError, subprocess.SubprocessError):
            continue
        if sum(len(x) for x in parts) > max_chars:
            break
    return "\n".join(parts)


def turn_diff(workspace: str, changed: Iterable[str], checkpoint_sha: Optional[str], max_chars: int = MAX_DIFF_CHARS) -> Dict[str, Any]:
    """Unified diff of this turn's changes: from the shadow checkpoint when
    there is one, else from the user's git. Returns {"diff", "truncated", "source"}."""
    paths = [p for p in changed if p]
    text = ""
    source = "none"
    if checkpoint_sha and workspace:
        try:
            from src import workspace_checkpoints as wc
            chunks: List[str] = []
            for p in paths[:40]:
                d = wc.diff_since(workspace, checkpoint_sha, p, max_chars=max_chars)
                if d:
                    chunks.append(d)
                if sum(len(c) for c in chunks) > max_chars:
                    break
            text = "\n".join(chunks)
            source = "checkpoint"
        except Exception as e:
            logger.debug("[review] checkpoint diff failed: %s", e)
            text = ""
    if not text and workspace:
        text = _user_git_diff(workspace, paths, max_chars)
        source = "git" if text else "none"
    truncated = False
    if len(text) > max_chars:
        text = text[:max_chars] + "\n… diff truncated for review"
        truncated = True
    return {"diff": text, "truncated": truncated, "source": source, "files": paths}


# ---------------------------------------------------------------------------
# The review call
# ---------------------------------------------------------------------------

def _prompt(user_text: str, diff: str, files: List[str], tests: Optional[Dict[str, Any]]) -> str:
    test_line = ""
    if tests and tests.get("ran"):
        test_line = f"\nProject tests after the change: {'PASSED' if tests.get('ok') else 'FAILED'} ({tests.get('summary') or ''})."
    return (
        "You are a strict but practical code reviewer. An AI coding agent just changed files in a "
        "repository to satisfy the user's request below. You see ONLY the request and the unified diff. "
        "Find OBVIOUS defects a careful engineer would flag before merging:\n"
        "- references to names/functions/files that the diff does not define and that likely do not exist\n"
        "- missing or wrong imports, wrong signatures, wrong argument order\n"
        "- logic that contradicts the request, or does only part of it\n"
        "- behaviour that was deleted or overwritten (removed lines that were still needed)\n"
        "- unhandled None/undefined, off-by-one, wrong operators, swapped branches\n"
        "- leftover debug prints, TODO stubs presented as done, hard-coded test values\n"
        "Do NOT comment on style, naming or formatting. Do not invent problems: if the diff looks "
        "correct, say so. Line numbers refer to the NEW file when you can tell.\n\n"
        f"<user_request>\n{(user_text or '')[:3000]}\n</user_request>\n\n"
        f"<changed_files>\n{', '.join(files[:40])}\n</changed_files>{test_line}\n\n"
        f"<diff>\n{diff}\n</diff>\n\n"
        "Answer with ONLY a JSON object, no prose before or after:\n"
        '{"verdict": "ok" | "issues", "summary": "<one sentence>", '
        '"findings": [{"severity": "error" | "warning", "file": "<path>", "line": <int or null>, '
        '"issue": "<what is wrong and why, one or two sentences>"}]}\n'
        'Use "error" only for defects that will break the code or clearly violate the request; '
        'everything else is "warning". An empty findings list with verdict "ok" is a valid answer.'
    )


def _parse(raw: str) -> Dict[str, Any]:
    text = _THINK_RE.sub("", raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I | re.M)
    m = _JSON_BLOCK_RE.search(text)
    data: Any = None
    if m:
        blob = m.group(0)
        try:
            data = json.loads(blob)
        except ValueError:
            # Trailing commas / single quotes: one lenient retry.
            try:
                data = json.loads(re.sub(r",\s*([}\]])", r"\1", blob))
            except ValueError:
                data = None
    if not isinstance(data, dict):
        return {"verdict": "unparsed", "summary": text[:300], "findings": []}
    findings: List[Dict[str, Any]] = []
    for f in (data.get("findings") or [])[:20]:
        if not isinstance(f, dict):
            continue
        sev = str(f.get("severity") or "warning").lower().strip()
        if sev not in _SEVERITIES:
            sev = "warning"
        line = f.get("line")
        try:
            line = int(line) if line not in (None, "", "null") else None
        except (TypeError, ValueError):
            line = None
        issue = str(f.get("issue") or f.get("message") or "").strip()
        if not issue:
            continue
        findings.append({"severity": sev, "file": str(f.get("file") or "").strip()[:300],
                         "line": line, "issue": issue[:600]})
    verdict = str(data.get("verdict") or "").lower().strip()
    if verdict not in ("ok", "issues"):
        verdict = "issues" if findings else "ok"
    if verdict == "ok" and any(f["severity"] == "error" for f in findings):
        verdict = "issues"
    return {"verdict": verdict, "summary": str(data.get("summary") or "").strip()[:400], "findings": findings}


async def review_turn(
    *,
    workspace: str,
    changed: Iterable[str],
    checkpoint_sha: Optional[str],
    user_text: str,
    endpoint_url: str,
    model: str,
    headers: Optional[Dict] = None,
    reviewer_model: Optional[str] = None,
    tests: Optional[Dict[str, Any]] = None,
    timeout_s: Optional[float] = None,
) -> Dict[str, Any]:
    """Run the review. Always returns a dict; `error` is set when it could not run."""
    t0 = time.time()
    files = [p for p in changed if p]
    reviewer = reviewer_model or model
    result: Dict[str, Any] = {
        "model": reviewer, "verdict": "skipped", "summary": "", "findings": [],
        "duration_s": 0.0, "diff_chars": 0, "truncated": False, "source": "none", "files": files[:40],
    }
    if not files or not workspace:
        result["summary"] = "nothing to review"
        return result
    try:
        d = turn_diff(workspace, files, checkpoint_sha)
    except Exception as e:
        result.update(error=f"diff failed: {e}"[:300], verdict="error")
        return result
    diff = d.get("diff") or ""
    result.update(diff_chars=len(diff), truncated=bool(d.get("truncated")), source=d.get("source"))
    if not diff.strip():
        result["summary"] = "no diff available for the changed files"
        return result
    try:
        timeout = float(timeout_s if timeout_s is not None else _setting("agent_auto_review_timeout_seconds", DEFAULT_TIMEOUT_S) or DEFAULT_TIMEOUT_S)
    except (TypeError, ValueError):
        timeout = float(DEFAULT_TIMEOUT_S)
    try:
        from src.llm_core import llm_call_async
        raw = await llm_call_async(
            url=endpoint_url, model=reviewer,
            messages=[{"role": "user", "content": _prompt(user_text, diff, files, tests)}],
            headers=headers, temperature=0.1, max_tokens=1200, timeout=int(timeout),
            max_retries=0, workload="background",
        )
    except Exception as e:
        logger.warning("[review] reviewer %s failed: %s", reviewer, e)
        result.update(error=f"{type(e).__name__}: {e}"[:300], verdict="error")
        result["duration_s"] = round(time.time() - t0, 1)
        return result
    result.update(_parse(raw if isinstance(raw, str) else str(raw)))
    result["duration_s"] = round(time.time() - t0, 1)
    logger.info("[review] %s: verdict=%s findings=%d in %ss", reviewer, result["verdict"],
                len(result["findings"]), result["duration_s"])
    return result


def fix_message(review: Dict[str, Any]) -> str:
    """Bounded fix-round instruction: only error-severity findings."""
    errs = [f for f in review.get("findings") or [] if f.get("severity") == "error"]
    lines = [
        "[Harness check — automatic message from the runtime, not from the user]",
        f"An independent review of the diff of your changes (reviewer: {review.get('model')}) flagged "
        f"{len(errs)} likely defect(s):",
    ]
    for f in errs[:6]:
        where = f.get("file") or "?"
        if f.get("line"):
            where += f":{f['line']}"
        lines.append(f"- {where}: {f.get('issue')}")
    lines.append(
        "Verify each point against the real file with read_file. Fix the ones that are real with "
        "edit_file. If a point is wrong, do not change anything for it — say why in one sentence in "
        "your final answer. Then finish."
    )
    return "\n".join(lines)


def compact(review: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not review:
        return None
    keys = ("model", "verdict", "summary", "findings", "duration_s", "diff_chars", "truncated", "source", "error")
    return {k: review.get(k) for k in keys if k in review}
