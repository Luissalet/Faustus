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

BENCH-02 (`review_gate`, below `fix_message`) is a second, stricter question
than everything above: not "what does a model reviewing the diff think of
it", but "does this ChangeSet even have the evidence a review is allowed to
rely on at all". A `ChangeSet` (`src/contracts/changeset.py`) can be built
with `files.source="none"` (no diff was ever taken) or with verification that
never ran — the contract itself permits both, on purpose, because plenty of
turns legitimately have neither. What it must never be allowed to do is come
out the far end marked *reviewed*: an approval stamped on a ChangeSet with no
diff is a rubber stamp wearing a review's clothes, and that is a worse state
than no review at all because it looks the same in a log. `review_gate` is
the one place that stamp is allowed to be produced, and it refuses to unless
the ChangeSet already carries a real diff and a real before/after test
verdict — never a parallel store of its own, always the same `FileChanges`/
`Verification` the ChangeSet already validated at construction (hard rule 4).
"""
from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

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


def _distinct_reviewer(writer_model: str, available_models: Sequence[str]) -> Optional[str]:
    """VER-03: "not the same model that wrote it, if there is another" — a
    case-/whitespace-insensitive name comparison, the same standard a model
    identity check elsewhere in this repo uses for a bare model name (no
    endpoint, no owner) with no identity helper of its own to defer to.
    Returns the first candidate that differs from the writer, in the
    caller's own order — this function does not rank models, only excludes
    the one that would make the review not independent."""
    writer_norm = str(writer_model or "").strip().lower()
    for candidate in available_models or ():
        cand_norm = str(candidate or "").strip().lower()
        if cand_norm and cand_norm != writer_norm:
            return candidate
    return None


def available_models_for_review(owner: Optional[str]) -> List[str]:
    """VER-03 (Lote 50 wiring): a cheap, DB-only candidate list for
    `resolve_reviewer`'s `available_models` — every model id any of the
    owner's enabled endpoints has last reported (`ModelEndpoint.cached_models`,
    the same column `routes/model_routes.py` reads to avoid a live probe on
    every list). This never touches the network: a stale or empty cache just
    means "same" still falls back to `model` itself, exactly like before this
    function existed. Never raises — any failure returns ``[]``.
    """
    try:
        from core.database import ModelEndpoint, SessionLocal
        with SessionLocal() as db:
            rows = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True).all()  # noqa: E712
            ids: List[str] = []
            for ep in rows:
                if ep.owner and owner and ep.owner != owner:
                    continue
                if ep.owner and not owner:
                    continue
                try:
                    cached = json.loads(ep.cached_models or "[]")
                except (TypeError, ValueError):
                    continue
                for m in cached:
                    if isinstance(m, str) and m and m not in ids:
                        ids.append(m)
            return ids
    except Exception:
        logger.debug("[auto_review] available_models_for_review failed", exc_info=True)
        return []


def resolve_reviewer(model: str, project_override: Optional[str] = None, *,
                      available_models: Optional[Sequence[str]] = None) -> Optional[str]:
    """The model to review with, or None when auto-review is off.

    `available_models`, when a caller passes one, lets a "same model"
    setting still produce an INDEPENDENT reviewer when another model is
    actually available (VER-03) — omitted (the default), this returns
    exactly what it always has: `model` itself for "same"/truthy settings,
    so every existing caller keeps today's behaviour unchanged.
    """
    raw = (project_override or "").strip() or str(_setting("agent_auto_review", "off") or "off").strip()
    low = raw.lower()
    if low in ("", "off", "false", "0", "none", "no"):
        return None
    if low in ("same", "self", "true", "1", "on", "yes"):
        if available_models:
            distinct = _distinct_reviewer(model, available_models)
            if distinct:
                return distinct
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


def per_file_diffs(workspace: str, files: Iterable[str], checkpoint_sha: Optional[str],
                    per_file_max_chars: int = 8_000) -> Dict[str, str]:
    """Diff of each changed file, independently capped, keyed by path.

    Same two sources as `turn_diff` (the shadow checkpoint, else the user's
    own git), but never joined and never truncated as a whole — this is what
    lets a big multi-file diff be split into groups that are each reviewed
    within their own budget instead of one call losing the tail of the diff
    past MAX_DIFF_CHARS. A file this could not get a diff for (deleted repo,
    git unavailable, …) is simply absent from the result."""
    paths = [p for p in files if p]
    out: Dict[str, str] = {}
    if checkpoint_sha and workspace:
        try:
            from src import workspace_checkpoints as wc
            for p in paths:
                d = wc.diff_since(workspace, checkpoint_sha, p, max_chars=per_file_max_chars)
                if d:
                    out[p] = d
        except Exception as e:
            logger.debug("[review] per-file checkpoint diff failed: %s", e)
    missing = [p for p in paths if p not in out]
    if missing and workspace:
        for p in missing:
            try:
                d = _user_git_diff(workspace, [p], per_file_max_chars)
            except Exception:
                d = ""
            if d:
                out[p] = d
    return out


# ---------------------------------------------------------------------------
# Grouping a large, multi-file diff for review (LOTE big-diff grouping)
# ---------------------------------------------------------------------------
#
# A diff over MAX_DIFF_CHARS or touching many files used to be silently
# truncated to one call's budget: everything past the cut was never seen by
# the reviewer, with no record that it had been dropped. Past
# `auto_review_group_threshold_files` files (or when the joined diff would be
# truncated), the changed files are split into small thematic groups instead
# — same directory/module, a test file with the source it tests, config
# files together — and each group is reviewed in its own call, within its
# own MAX_DIFF_CHARS budget, so nothing is dropped without saying so.

_CONFIG_FILE_RE = re.compile(
    r"(^|/)("
    r"pyproject\.toml|setup\.(?:cfg|py)|requirements[\w.-]*\.txt|"
    r"package(?:-lock)?\.json|tsconfig[\w.-]*\.json|"
    r"Dockerfile[\w.-]*|docker-compose[\w.-]*\.ya?ml|Makefile|\.env[\w.-]*|"
    r"[\w.-]+\.(?:cfg|ini|toml|ya?ml)"
    r")$",
    re.I,
)

_TEST_AFFIX_RE = re.compile(r"^(?:test_|spec_)(?P<core1>.+)$|^(?P<core2>.+?)(?:_test|_spec)$")


def _basename_core(path: str) -> str:
    """The part of a filename that ties it to what it is about, stripped of
    a leading/trailing test/spec marker and its extension — so
    `tests/test_auto_review.py` and `src/auto_review.py` both reduce to
    `auto_review` and group together."""
    name = os.path.basename(str(path).replace("\\", "/"))
    stem = name.rsplit(".", 1)[0] if "." in name else name
    m = _TEST_AFFIX_RE.match(stem)
    if m:
        stem = m.group("core1") or m.group("core2") or stem
    return stem.lower()


def group_files_deterministic(files: Sequence[str]) -> List[List[str]]:
    """Group changed files into small thematic groups with no model call:

    1. config files (pyproject.toml, package.json, Dockerfile, *.yml, …) —
       one group, together;
    2. files that share a basename core with at least one other changed
       file — typically a test paired with the source it tests — one group
       per shared core;
    3. everything else, grouped by directory.

    Deterministic and stable: same input, same groups, same order, every
    time — the fallback `group_files_with_model` always lands on if the
    model's answer cannot be trusted."""
    ordered = [f for f in files if f]
    config = [f for f in ordered if _CONFIG_FILE_RE.search(f.replace("\\", "/"))]
    config_set = set(config)
    rest = [f for f in ordered if f not in config_set]

    core_to_files: Dict[str, List[str]] = {}
    for f in rest:
        core_to_files.setdefault(_basename_core(f), []).append(f)

    groups: List[List[str]] = []
    grouped: set = set()
    for f in rest:
        if f in grouped:
            continue
        bucket = core_to_files.get(_basename_core(f)) or [f]
        if len(bucket) > 1:
            groups.append(bucket)
            grouped.update(bucket)

    dir_to_files: Dict[str, List[str]] = {}
    dir_order: List[str] = []
    for f in rest:
        if f in grouped:
            continue
        d = os.path.dirname(f.replace("\\", "/")) or "."
        if d not in dir_to_files:
            dir_to_files[d] = []
            dir_order.append(d)
        dir_to_files[d].append(f)
        grouped.add(f)
    for d in dir_order:
        groups.append(dir_to_files[d])

    if config:
        groups.append(config)
    return groups


_GROUP_JSON_RE = re.compile(r"\[.*\]", re.S)


async def group_files_with_model(
    files: Sequence[str], *, endpoint_url: str, model: str, headers: Optional[Dict] = None,
    timeout_s: float = 60.0, workload: str = "foreground",
) -> Optional[List[List[str]]]:
    """Ask the reviewer model, once, to group changed files by INDEX — never
    by path, so the prompt (and the model's answer) stays cheap regardless
    of how long the paths are. Expects a JSON array of arrays of 0-based
    indices into `files`, e.g. `[[0, 1], [2], [3, 4, 5]]`, every index
    appearing exactly once.

    Returns `None` on anything that is not a clean, complete partition —
    a bad call, unparsable text, an out-of-range or repeated index, an index
    left out — so the caller always has a deterministic fallback to reach
    for; this never raises."""
    ordered = [f for f in files if f]
    if not ordered:
        return None
    listing = "\n".join(f"{i}: {p}" for i, p in enumerate(ordered))
    prompt = (
        "Group these changed files into small thematic review groups (same "
        "module or directory, a test file with the source file it tests, "
        "config files together). Every file must end up in exactly one "
        "group.\n\n"
        f"<files>\n{listing}\n</files>\n\n"
        "Answer with ONLY a JSON array of arrays of the file INDEX numbers "
        "above (not the paths), e.g. [[0, 1], [2], [3, 4, 5]]. No prose."
    )
    try:
        from src.llm_core import llm_call_async
        raw = await asyncio.wait_for(
            llm_call_async(
                url=endpoint_url, model=model, messages=[{"role": "user", "content": prompt}],
                headers=headers, temperature=0.0, max_tokens=500, timeout=int(timeout_s),
                max_retries=1, workload=workload or "foreground",
            ),
            timeout=timeout_s + 30,
        )
    except Exception as e:
        logger.debug("[review] model grouping call failed: %s", e)
        return None
    if isinstance(raw, tuple):
        raw = raw[0]
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = _THINK_RE.sub("", raw).strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I | re.M)
    m = _GROUP_JSON_RE.search(text)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        try:
            data = json.loads(re.sub(r",\s*([}\]])", r"\1", m.group(0)))
        except ValueError:
            return None
    if not isinstance(data, list) or not data:
        return None
    n = len(ordered)
    used: set = set()
    groups: List[List[str]] = []
    for grp in data:
        if not isinstance(grp, list):
            return None
        idxs: List[int] = []
        for raw_i in grp:
            try:
                i = int(raw_i)
            except (TypeError, ValueError):
                return None
            if i < 0 or i >= n or i in used:
                return None
            used.add(i)
            idxs.append(i)
        if idxs:
            groups.append([ordered[i] for i in idxs])
    if len(used) != n or not groups:
        return None
    return groups


async def group_files(
    files: Sequence[str], *, endpoint_url: str, model: str, headers: Optional[Dict] = None,
    timeout_s: float = 60.0, workload: str = "foreground", with_model: bool = False,
) -> List[List[str]]:
    """The groups to review a large diff in: the model's grouping when
    `with_model` is on and it returns a clean partition, else the
    deterministic grouping — which is also what a disabled or failed model
    grouping always falls back to."""
    if with_model:
        try:
            grouped = await group_files_with_model(
                files, endpoint_url=endpoint_url, model=model, headers=headers,
                timeout_s=timeout_s, workload=workload,
            )
        except Exception as e:
            logger.debug("[review] group_files_with_model raised: %s", e)
            grouped = None
        if grouped:
            return grouped
    return group_files_deterministic(files)


def _dedup_findings(findings: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Findings from several group reviews, deduped by (file, line, message)
    — the same finding flagged by two overlapping groups (a test grouped
    with its source, both mentioning the same broken call) is kept once."""
    seen: set = set()
    out: List[Dict[str, Any]] = []
    for f in findings:
        key = (f.get("file") or "", f.get("line"), _norm_line(f.get("issue") or ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out


# ---------------------------------------------------------------------------
# The shape of the answer
# ---------------------------------------------------------------------------
#
# The prompt below has always described this object, and `_parse` has always
# had to go looking for it inside whatever prose came back. On a 9B model that
# search fails often enough to matter: a measured 44 % of review passes landed
# as "unparsed", each one 15 s of GPU spent on nothing and a turn with no
# review. This is the same object as a JSON Schema, so an endpoint that can
# decode under a grammar (native Ollama's `format`) simply cannot produce
# anything else — the parser stays, but as a net rather than as the mechanism.
#
# `evidence` is REQUIRED on every finding. That is the load-bearing part.
# Until now a finding with no evidence was a thing the model could say and
# `ground_findings` had to catch afterwards; with the schema the reviewer
# cannot even open a finding it has nothing to point at. `ground_findings`
# stays exactly as it is — it still has to check that the evidence is real,
# which no schema can do, and it is still the only defence on every endpoint
# that does not support constrained decoding.
REVIEW_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["ok", "issues"]},
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": ["error", "warning"]},
                    "file": {"type": "string"},
                    # The reviewer often genuinely cannot tell; null is an
                    # honest answer and `_parse` already normalises it.
                    "line": {"type": ["integer", "null"]},
                    "evidence": {"type": "string"},
                    "issue": {"type": "string"},
                },
                "required": ["severity", "file", "evidence", "issue"],
            },
        },
    },
    "required": ["verdict", "summary", "findings"],
}


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
        "correct, say so. Every finding must point at concrete lines of the diff. Parts of the request "
        "that describe the agent's own workflow (keep a todo list, check the syntax, run the tests, "
        "report back, ask before doing X) are NOT code requirements: never report their absence from "
        "the diff. Line numbers refer to the NEW file when you can tell.\n\n"
        f"<user_request>\n{(user_text or '')[:3000]}\n</user_request>\n\n"
        f"<changed_files>\n{', '.join(files[:40])}\n</changed_files>{test_line}\n\n"
        f"<diff>\n{diff}\n</diff>\n\n"
        "Answer with ONLY a JSON object, no prose before or after:\n"
        '{"verdict": "ok" | "issues", "summary": "<one sentence>", '
        '"findings": [{"severity": "error" | "warning", "file": "<path>", "line": <int or null>, '
        '"evidence": "<one line copied VERBATIM from the diff (without the leading + or -), or, for something the request asks for and the diff does not do, the exact words of the request>", '
        '"issue": "<what is wrong and why, one or two sentences>"}]}\n'
        'Use "error" only for defects that will break the code or clearly violate the request; '
        'everything else is "warning". A finding whose evidence is neither in the diff nor in the '
        'request is discarded. An empty findings list with verdict "ok" is a valid answer.'
    )


_WS_RE = re.compile(r"\s+")


def _norm_line(text: str) -> str:
    return _WS_RE.sub(" ", (text or "").strip()).strip("+- ").lower()


# Findings about the agent's *process* rather than the code: small reviewers
# keep reporting "the request asked to use todowrite / check the syntax / run
# the tests and the diff does not show it". Those are never code defects.
_WORKFLOW_RE = re.compile(
    r"\b(?:todo\s*write|todowrite|todo list|lista de (?:tareas|objetivos)|"
    r"syntax check|check(?:ing|ed)? (?:the )?syntax|comprob\w+ la sintaxis|sintaxis|"
    r"run(?:ning)? (?:the )?tests?|ejecut\w+ (?:los )?tests?|"
    r"report(?:ing)? back|inform\w+ al usuario|ask(?:ing)? (?:the user|before)|preguntar)\b",
    re.I,
)


def _looks_like_workflow_finding(f: Dict[str, Any]) -> bool:
    text = f"{f.get('issue') or ''}"
    ev = f"{f.get('evidence') or ''}"
    return bool(_WORKFLOW_RE.search(text)) and not _WORKFLOW_RE.search(ev)


def ground_findings(findings: List[Dict[str, Any]], diff: str, user_text: str = "") -> Dict[str, Any]:
    """Keep only what the reviewer can point at. A finding whose `evidence`
    is neither a line of the diff nor a phrase of the request (whitespace- and
    case-insensitive) is *ungrounded*: it is kept for the user but demoted to a
    warning, so it never costs a fix round. Small local reviewers invent defects
    about code they did not see (qwen3.5:9b flagged a button "placed after"
    the other one, then argued with itself in the finding text)."""
    lines = {_norm_line(l) for l in (diff or "").splitlines() if l[:1] in "+-" and not l.startswith(("+++", "---"))}
    lines.discard("")
    normalized_diff = _norm_line(diff)
    normalized_request = _norm_line(user_text)
    out: List[Dict[str, Any]] = []
    ungrounded = 0
    for f in findings:
        ev = _norm_line(f.get("evidence") or "")
        grounded = bool(ev) and (
            ev in lines
            or (len(ev) >= 12 and ev in normalized_diff)
            or (len(ev) >= 8 and normalized_request and ev in normalized_request)
        )
        g = dict(f)
        g["grounded"] = grounded
        if grounded and _looks_like_workflow_finding(g):
            # A real diff line attached to a complaint about the agent's
            # workflow ("no todowrite", "did not check syntax"): not a defect.
            g["workflow"] = True
            grounded = False
            g["grounded"] = False
        if not grounded:
            ungrounded += 1
            if g.get("severity") == "error":
                g["severity"] = "warning"
                g["demoted"] = True
        out.append(g)
    return {"findings": out, "ungrounded": ungrounded}


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
        evidence = f.get("evidence")
        findings.append({"severity": sev, "file": str(f.get("file") or "").strip()[:300],
                         "line": line, "issue": issue[:600],
                         "evidence": str(evidence).strip()[:300] if isinstance(evidence, (str, int, float)) and str(evidence).strip() else ""})
    verdict = str(data.get("verdict") or "").lower().strip()
    if verdict not in ("ok", "issues"):
        verdict = "issues" if findings else "ok"
    if verdict == "ok" and any(f["severity"] == "error" for f in findings):
        verdict = "issues"
    return {"verdict": verdict, "summary": str(data.get("summary") or "").strip()[:400], "findings": findings}


async def _call_reviewer(
    *, diff: str, files: List[str], user_text: str, endpoint_url: str, reviewer: str,
    headers: Optional[Dict], tests: Optional[Dict[str, Any]], timeout: float, workload: str,
) -> Tuple[Dict[str, Any], Optional[str]]:
    """One reviewer completion over `diff`/`files`, parsed and grounded.

    Returns `(parsed, error)`: `error` is `None` on success, and `parsed` is
    `{"verdict": "ok"|"issues", "summary", "findings", "ungrounded"}`; on
    failure `parsed` is an empty `{"verdict": "error", ...}` shell so a
    caller merging several of these (one per review group) can still fold it
    in without a type check. This is the single call `review_turn` always
    made — factored out so the grouped path (below) can make it more than
    once, byte-identical each time, instead of duplicating it."""
    empty: Dict[str, Any] = {"verdict": "error", "summary": "", "findings": []}
    try:
        from src.llm_core import llm_call_async
        # max_retries is the number of *attempts* (0 would never call the
        # model and return None — seen live: verdict "unparsed", summary "None").
        # The review is part of the user's turn: "foreground" — a "background"
        # call waits behind the local-model gate while the chat is active,
        # i.e. forever, since this very turn is what keeps the chat active.
        # Hard bound on the whole call (HTTP timeout + gate wait): a review must
        # never hang the turn it belongs to.
        # response_schema: on a native Ollama endpoint the answer is decoded
        # under REVIEW_SCHEMA and cannot come back as prose with a half-closed
        # object inside. Everywhere else it is dropped before it reaches the
        # wire and `_parse` below does the work it has always done.
        raw = await asyncio.wait_for(
            llm_call_async(
                url=endpoint_url, model=reviewer,
                messages=[{"role": "user", "content": _prompt(user_text, diff, files, tests)}],
                headers=headers, temperature=0.1, max_tokens=1200, timeout=int(timeout),
                max_retries=1, workload=workload or "foreground",
                response_schema=REVIEW_SCHEMA,
            ),
            timeout=timeout + 30,
        )
    except asyncio.TimeoutError:
        logger.warning("[review] reviewer %s did not answer within %ss", reviewer, int(timeout) + 30)
        return empty, f"review timed out after {int(timeout) + 30} s"
    except Exception as e:
        logger.warning("[review] reviewer %s failed: %s", reviewer, e)
        return empty, f"{type(e).__name__}: {e}"[:300]
    if isinstance(raw, tuple):
        raw = raw[0]
    if not isinstance(raw, str) or not raw.strip():
        logger.warning("[review] reviewer %s returned an empty answer", reviewer)
        return empty, "the reviewer returned an empty answer"
    parsed = _parse(raw)
    if parsed["verdict"] == "unparsed":
        logger.warning("[review] %s: answer was not a JSON object: %r", reviewer, raw[:300])
    else:
        grounded = ground_findings(parsed["findings"], diff, user_text)
        parsed["findings"] = grounded["findings"]
        parsed["ungrounded"] = grounded["ungrounded"]
        if parsed["verdict"] == "issues" and not any(f["severity"] == "error" for f in parsed["findings"]) \
                and parsed["findings"] and all(not f["grounded"] for f in parsed["findings"]):
            # Nothing the reviewer said can be located in the diff.
            parsed["verdict"] = "ok"
            parsed["summary"] = (parsed.get("summary") or "").strip()
            parsed["summary"] = ("no finding could be located in the diff" + (f" ({parsed['summary']})" if parsed["summary"] else ""))[:400]
    return parsed, None


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
    workload: str = "foreground",
) -> Dict[str, Any]:
    """Run the review. Always returns a dict; `error` is set when it could not run.

    A diff that stays under `MAX_DIFF_CHARS` and touches at most
    `auto_review_group_threshold_files` files is reviewed exactly as before:
    one call, one prompt. Past either limit, the changed files are split
    into small thematic groups (`group_files`) and reviewed group by group,
    each within its own budget, so a large turn's diff is never silently
    truncated out of review; findings from every group are merged and
    deduped, and files beyond `auto_review_max_groups` groups are reported
    as `not_reviewed` rather than dropped without a trace.
    """
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
        threshold_files = int(_setting("auto_review_group_threshold_files", 6) or 6)
    except (TypeError, ValueError):
        threshold_files = 6
    needs_grouping = bool(d.get("truncated")) or len(files) > threshold_files

    if not needs_grouping:
        parsed, err = await _call_reviewer(
            diff=diff, files=files, user_text=user_text, endpoint_url=endpoint_url,
            reviewer=reviewer, headers=headers, tests=tests, timeout=timeout, workload=workload,
        )
        if err:
            result.update(error=err, verdict="error")
            result["duration_s"] = round(time.time() - t0, 1)
            return result
        result.update(parsed)
        result["duration_s"] = round(time.time() - t0, 1)
        logger.info("[review] %s: verdict=%s findings=%d in %ss", reviewer, result["verdict"],
                    len(result["findings"]), result["duration_s"])
        return result

    # ── large / many-file diff: review in groups ──────────────────────────
    try:
        max_groups = int(_setting("auto_review_max_groups", 4) or 4)
    except (TypeError, ValueError):
        max_groups = 4
    max_groups = max(1, max_groups)
    group_with_model_on = bool(_setting("auto_review_group_with_model", False))

    try:
        groups = await group_files(
            files, endpoint_url=endpoint_url, model=reviewer, headers=headers,
            timeout_s=min(timeout, 60.0), workload=workload, with_model=group_with_model_on,
        )
    except Exception as e:
        logger.debug("[review] group_files raised, falling back to deterministic: %s", e)
        groups = None
    if not groups:
        groups = group_files_deterministic(files) or [files]

    reviewed_groups = groups[:max_groups]
    not_reviewed = [f for g in groups[max_groups:] for f in g]

    try:
        pf = per_file_diffs(workspace, files, checkpoint_sha, per_file_max_chars=MAX_DIFF_CHARS)
    except Exception as e:
        logger.debug("[review] per_file_diffs failed: %s", e)
        pf = {}

    all_findings: List[Dict[str, Any]] = []
    group_file_lists: List[List[str]] = []
    group_chars: List[int] = []
    truncated_files: List[str] = []
    total_chars = 0
    total_ungrounded = 0
    any_error: Optional[str] = None
    any_ok_call = False
    any_issues = False

    for group in reviewed_groups:
        group_present = [f for f in group if pf.get(f)]
        if not group_present:
            continue
        group_diff = "\n".join(pf[f] for f in group_present)
        if len(group_diff) > MAX_DIFF_CHARS:
            group_diff = group_diff[:MAX_DIFF_CHARS] + "\n… diff truncated for review"
            truncated_files.extend(group_present)
        total_chars += len(group_diff)
        group_file_lists.append(group_present)
        group_chars.append(len(group_diff))
        parsed, err = await _call_reviewer(
            diff=group_diff, files=group_present, user_text=user_text, endpoint_url=endpoint_url,
            reviewer=reviewer, headers=headers, tests=tests, timeout=timeout, workload=workload,
        )
        if err:
            any_error = any_error or err
            continue
        any_ok_call = True
        if parsed.get("verdict") == "issues":
            any_issues = True
        total_ungrounded += int(parsed.get("ungrounded") or 0)
        all_findings.extend(parsed.get("findings") or [])

    result["findings"] = _dedup_findings(all_findings)
    result["ungrounded"] = total_ungrounded
    result["groups"] = len(group_file_lists)
    result["group_files"] = group_file_lists
    result["not_reviewed"] = not_reviewed
    result["truncated_files"] = truncated_files
    result["group_chars"] = group_chars
    result["diff_chars"] = total_chars
    result["truncated"] = bool(truncated_files) or bool(not_reviewed)
    if not any_ok_call:
        result.update(error=any_error or "all review groups failed", verdict="error")
    else:
        has_error_finding = any(f.get("severity") == "error" for f in result["findings"])
        result["verdict"] = "issues" if (has_error_finding or any_issues or result["findings"]) else "ok"
        summary = f"reviewed in {result['groups']} group(s)"
        if not_reviewed:
            summary += f", {len(not_reviewed)} file(s) not reviewed (group cap)"
        if any_error:
            summary += f" ({any_error})"
        result["summary"] = summary[:400]
    result["duration_s"] = round(time.time() - t0, 1)
    logger.info("[review] %s: grouped verdict=%s findings=%d groups=%d not_reviewed=%d in %ss",
                reviewer, result["verdict"], len(result["findings"]), result["groups"],
                len(not_reviewed), result["duration_s"])
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
    keys = ("model", "verdict", "summary", "findings", "duration_s", "diff_chars", "truncated", "source", "error",
            "ungrounded", "disputed", "groups", "group_files", "not_reviewed", "truncated_files", "group_chars")
    return {k: review.get(k) for k in keys if k in review}


# ---------------------------------------------------------------------------
# BENCH-02 — a real review gate for a ChangeSet
# ---------------------------------------------------------------------------

def verification_from_run_verifier(result: Optional[Mapping[str, Any]]) -> Any:
    """Turn `src.verification.run_verifier`'s dict (lot 23's VER-02: a
    before/after test comparison against a checkpoint baseline) into the
    `Verification` `src.contracts.changeset.ChangeSet` already validates and
    carries — so a caller that just ran the real verifier does not have to
    hand-translate field names to satisfy `review_gate` below, and there is
    never a second "did the tests pass" object competing with the
    ChangeSet's own.

    `run_verifier`'s `new_failures` (failures the checkpoint baseline did not
    have) becomes `Verification.failures`; a run whose only failures were
    already failing before this turn (`preexisting` non-empty, no
    `new_failures`) sets `pre_existing_only`, matching what `evidence_gaps`
    already does with that flag.
    """
    from src.contracts.changeset import Verification

    data = dict(result or {})
    ran = bool(data.get("ran"))
    ok = data.get("ok")
    if ok is not None:
        ok = bool(ok) if ran else None  # the contract refuses ok set without ran
    new_failures = [str(f) for f in (data.get("new_failures") or [])]
    preexisting = [str(f) for f in (data.get("preexisting") or [])]
    pre_existing_only = bool(ran and ok is False and preexisting and not new_failures)
    return Verification(
        mode=str(data.get("kind") or "tests")[:32] or "tests",
        ran=ran, ok=ok,
        inconclusive=bool(data.get("inconclusive")),
        pre_existing_only=pre_existing_only,
        command=str(data.get("command") or "")[:500],
        summary=str(data.get("summary") or "")[:1000],
        failures=tuple(new_failures[:200]),
    )


def _in_declared_scope(path: str, scope: Tuple[str, ...]) -> bool:
    """Is `path` covered by one entry of a declared scope?

    An entry matches as a glob (`src/state_mirror/*`), as an exact path, or
    as a directory prefix (`tests/eval` covers `tests/eval/harness.py`) — the
    same three spellings a lot's own PROPIOS list is written in, so a scope
    can be copied out of a lot file verbatim rather than re-encoded as regex."""
    norm = str(path).replace("\\", "/").lstrip("/")
    for raw in scope:
        pat = str(raw).replace("\\", "/").strip().rstrip("/")
        if not pat:
            continue
        if norm == pat or fnmatch.fnmatch(norm, pat):
            return True
        if norm.startswith(pat + "/"):
            return True
    return False


def scope_violations(changeset: Any, declared_scope: Iterable[str] = ()) -> Tuple[str, ...]:
    """Changed files `declared_scope` does not cover, worst case first.

    Empty whenever there is nothing to accuse a turn WITH: no scope was
    declared, or (same reasoning as `ChangeSet.unsupported_claims`) the
    change list itself is not exact — a truncated or mtime-derived list
    cannot be used to prove a file was touched, only to suggest it."""
    scope = tuple(s for s in (declared_scope or ()) if s)
    if not scope or not changeset.files.exact:
        return ()
    return tuple(p for p in changeset.files.paths if not _in_declared_scope(p, scope))


@dataclass(frozen=True)
class ReviewGateResult:
    """Whether this ChangeSet may be marked reviewed, and — when not — every
    reason it cannot be, not just the first one found."""

    approved: bool
    blocking: Tuple[str, ...] = ()
    out_of_scope: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {"approved": self.approved, "blocking": list(self.blocking),
                "out_of_scope": list(self.out_of_scope)}


def review_gate(changeset: Any, *, declared_scope: Iterable[str] = ()) -> ReviewGateResult:
    """BENCH-02: may `changeset` be approved as reviewed?

    Three things are required, all read off the ChangeSet's own validated
    fields, never recomputed or stored again here:

    1. A real diff. `changeset.files.exact` (true only when the change list
       came from a checkpoint and was not truncated — `FileChanges.exact`)
       must hold. A ChangeSet built with no diff at all (`files.source ==
       "none"`, the default) fails this outright — the lot's own requirement
       ("un ChangeSet sin diff no se puede aprobar como revisado").
    2. A real before/after test verdict. `changeset.verification.ran` must be
       true and `.ok` must not be `None` — a verifier that never ran, or ran
       and reached no verdict (`run_verifier`'s own `inconclusive`), blocks
       approval exactly like a missing diff does. Whether the tests PASSED is
       not asked here: a reviewed ChangeSet may still say "tests fail, and
       here is why" — `.ok is False` alone is not blocking, only `ran=False`
       or `ok=None` is. That distinction is `Verification`'s own (`ok` is
       three-valued so "not verified" is never confused with "passed").
    3. Nothing touched outside `declared_scope`, when one was given
       (`scope_violations`, above). A ChangeSet that changed a file nobody
       declared in scope is not approved silently; it is refused and named.

    Never raises, and never mutates `changeset` — a caller that wants the
    refusal recorded writes `.blocking`/`.to_dict()` into wherever it persists
    review metadata itself (e.g. `ChangeSet.review`), the same "hold the
    result, do not become a second store of it" rule `detect_divergence` and
    `repair` (BASE-02, `src/state_mirror/divergence.py`) already follow.
    """
    blocking: List[str] = []
    if not changeset.files.exact:
        blocking.append(
            f"no exact diff: files.source={changeset.files.source!r}"
            + (" (truncated)" if changeset.files.truncated else "")
            + " — only an untruncated checkpoint diff counts as real evidence"
        )
    v = changeset.verification
    if not v.ran:
        blocking.append("no before/after test run was recorded (verification.ran is False)")
    elif v.ok is None:
        blocking.append(
            "the test run did not reach a verdict (verification.ok is None): "
            + (v.summary or "no summary given")
        )
    out_of_scope = scope_violations(changeset, declared_scope)
    if out_of_scope:
        blocking.append(
            f"{len(out_of_scope)} file(s) changed outside the declared scope: "
            + ", ".join(out_of_scope[:8])
        )
    return ReviewGateResult(approved=not blocking, blocking=tuple(blocking),
                            out_of_scope=out_of_scope)
