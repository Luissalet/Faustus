"""src/fix_memory.py — Fix memory: the agent grows smarter with every solved
issue.

After a turn that changed files and ran (or at least attempted) tests, one
compact record is appended to this project's own fix log: what was asked,
which files/functions were touched, which error signatures were seen, what
tests ran and how they came out, and a short solution summary. Before a new
coding task the agent can `recall` similar past fixes and get them back as a
short prompt block — the same "reference data, never a gate" posture as
`src/instincts.py` and `src/project_rules.py`.

Storage
-------
`<DATA_DIR>/fix_memory/<owner-safe>/<project-safe>.jsonl` — one JSON object
per line, appended. A sibling `<project-safe>.index.json` keeps `{id:
[keywords...]}` for a cheap keyword pre-filter (currently `recall` just scans
the (small, capped) jsonl directly; the index is kept so a future caller can
skip loading the whole file for a huge owner without changing the on-disk
shape). Both files are written with `core.atomic_io` helpers so a crash
mid-write never corrupts either.

Three things this module deliberately does NOT do
--------------------------------------------------
* No new approval gate. `record_from_turn` only ever appends a log line;
  `recall`/`prompt_block`/`stats` are pure reads. Nothing here blocks a tool
  call.
* No model call on the read path. `recall`/`prompt_block`/`stats` are
  deterministic reads of the JSONL store; only `record_from_turn` may make
  one optional background model call (`workload="background"`, utility
  endpoint) to phrase the solution summary, and it degrades to a
  deterministic summary whenever that call is unavailable or fails.
* No cross-owner or cross-project leakage. Every function takes `owner`
  explicitly; entries are scoped to one `project_key` and `recall`/
  `prompt_block` never see another project's log.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence

from src.constants import DATA_DIR

logger = logging.getLogger(__name__)

__all__ = [
    "record_from_turn", "recall", "prompt_block", "stats", "forget",
    "project_key",
]

MAX_ENTRIES_PER_PROJECT = 500
_TASK_MAX_CHARS = 400
_SOLUTION_MAX_CHARS = 600
_ERRORS_MAX = 10
_EVIDENCE_FILES_MAX = 20

_FILE_TOOLS = frozenset({"write_file", "edit_file", "apply_patch"})
_SHELL_TOOLS = frozenset({"bash", "python", "powershell"})


# ── identity helpers (mirrors src/instincts.py's owner/project scoping) ────

def project_key(workspace: Optional[str], project_id: Optional[str] = None) -> str:
    """Same `(workspace, project_id)` scoping rule as `src.instincts.
    project_key` — the workspace path wins, `project_id` is only a fallback,
    both missing means `""` (unscoped), never `None`. Re-exported here so a
    caller that only imports `fix_memory` does not also need `instincts`."""
    return str(workspace or project_id or "").strip()


def _owner_safe(owner: Optional[str]) -> str:
    from services.memory.skill_format import slugify
    return slugify(owner or "", fallback="_unowned")


def _project_safe(project: Optional[str]) -> str:
    from services.memory.skill_format import slugify
    p = (project or "").strip()
    if not p:
        return "unscoped"
    return slugify(p, fallback="project")[:60]


def _owner_dir(owner: Optional[str]) -> str:
    return os.path.join(DATA_DIR, "fix_memory", _owner_safe(owner))


def _store_path(owner: Optional[str], project: Optional[str]) -> str:
    return os.path.join(_owner_dir(owner), f"{_project_safe(project)}.jsonl")


def _index_path(owner: Optional[str], project: Optional[str]) -> str:
    return os.path.join(_owner_dir(owner), f"{_project_safe(project)}.index.json")


# ── low-level jsonl store ───────────────────────────────────────────────────

def _load_entries(owner: Optional[str], project: Optional[str]) -> List[Dict[str, Any]]:
    path = _store_path(owner, project)
    out: List[Dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    out.append(row)
    except FileNotFoundError:
        return []
    except OSError:
        return []
    return out


def _save_entries(owner: Optional[str], project: Optional[str], entries: Sequence[Dict[str, Any]]) -> None:
    path = _store_path(owner, project)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    from core.atomic_io import atomic_write_text
    text = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries)
    atomic_write_text(path, text)


def _append_entry(owner: Optional[str], project: Optional[str], entry: Dict[str, Any]) -> None:
    path = _store_path(owner, project)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _load_index(owner: Optional[str], project: Optional[str]) -> Dict[str, List[str]]:
    try:
        with open(_index_path(owner, project), encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_index(owner: Optional[str], project: Optional[str], index: Mapping[str, List[str]]) -> None:
    path = _index_path(owner, project)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    from core.atomic_io import atomic_write_json
    atomic_write_json(path, dict(index), indent=2)


# ── tokenizing / overlap (reuse skills.py's helpers when importable) ───────

def _tokenize(text: str) -> set:
    try:
        from services.memory.skills import _tokenize as _tok
        return _tok(text)
    except Exception:  # noqa: BLE001
        return {w.strip('.,!?";:()[]') for w in (text or "").lower().split() if len(w) > 1}


def _jaccard(a: set, b: set) -> float:
    try:
        from services.memory.skills import _jaccard as _jac
        return _jac(a, b)
    except Exception:  # noqa: BLE001
        if not a or not b:
            return 0.0
        return len(a & b) / len(a | b)


_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "with",
    "when", "is", "are", "this", "that", "it", "was", "be", "as", "at",
    "by", "from", "de", "la", "el", "en", "y", "o", "para", "con", "que",
    "es", "un", "una", "los", "las", "al", "del",
})


def _keywords(text: str, limit: int = 24) -> List[str]:
    toks = {t for t in _tokenize(text) if t not in _STOPWORDS and len(t) > 2}
    return sorted(toks)[:limit]


# ── error signature normalization ───────────────────────────────────────────

_TRACEBACK_TAIL_RE = re.compile(
    r"Traceback \(most recent call last\):.*?\n(\S[^\n]*?(?:Error|Exception)[^\n]*)",
    re.S,
)
_ERROR_LINE_RE = re.compile(r"^\s*(?:Error|ERROR)\s*[:\-]\s*(.+)$", re.M)
_PATH_RE = re.compile(r"(?:[A-Za-z]:)?[/\\][\w./\\-]+")
_NUM_RE = re.compile(r"\d+")


def normalize_error(text: str) -> str:
    """Exception class + message with numbers and paths masked, so the same
    underlying failure at a different line number / temp path still matches
    on recall."""
    t = _PATH_RE.sub("PATH", text or "")
    t = _NUM_RE.sub("#", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:220]


def _errors_from_text(text: str) -> List[str]:
    out: List[str] = []
    if not text:
        return out
    for m in _TRACEBACK_TAIL_RE.finditer(text):
        sig = normalize_error(m.group(1))
        if sig:
            out.append(sig)
    for m in _ERROR_LINE_RE.finditer(text):
        sig = normalize_error(m.group(1))
        if sig:
            out.append(sig)
    # de-dup, keep order
    seen = set()
    deduped = []
    for e in out:
        if e not in seen:
            seen.add(e)
            deduped.append(e)
    return deduped[:_ERRORS_MAX]


# ── diff parsing (files + touched symbols) ──────────────────────────────────

_DIFF_GIT_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)\s*$", re.M)
_DIFF_PATH_RE = re.compile(r"^(?:\+\+\+|---) [ab]/(.+)$", re.M)
_HUNK_SYMBOL_RE = re.compile(r"^@@.*@@.*?\b(?:def|class)\s+(\w+)", re.M)


def _paths_from_diff(diff: str) -> List[str]:
    paths: List[str] = []
    for m in _DIFF_GIT_RE.finditer(diff or ""):
        paths.append(m.group(2))
    for m in _DIFF_PATH_RE.finditer(diff or ""):
        p = m.group(1).strip()
        if p and p != "/dev/null":
            paths.append(p)
    seen = set()
    out = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _symbols_from_diff(diff: str) -> List[str]:
    syms = [m.group(1) for m in _HUNK_SYMBOL_RE.finditer(diff or "")]
    seen = set()
    out = []
    for s in syms:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


_PATH_TOKEN_RE = re.compile(r"[\w./-]+\.[A-Za-z0-9]{1,10}")


def _path_from_command(command: str) -> Optional[str]:
    """Best-effort file path for a write/edit/apply_patch call whose event
    carries no `diff` (a brand-new file, most often). Tries JSON args first
    (`{"path": "..."}` / `{"file": "..."}`), then the first path-looking
    token in the raw text."""
    text = (command or "").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, dict):
        for key in ("path", "file", "filename", "file_path"):
            v = parsed.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
    m = re.search(r'"?(?:path|file|filename)"?\s*[:=]\s*"?([\w./-]+\.[A-Za-z0-9]{1,10})', text)
    if m:
        return m.group(1)
    first_line = text.splitlines()[0] if text else ""
    m = _PATH_TOKEN_RE.search(first_line)
    if m and "://" not in m.group(0):
        return m.group(0)
    return None


def _extract_from_tool_events(tool_events: Sequence[Mapping[str, Any]]) -> tuple:
    files: List[str] = []
    symbols: List[str] = []
    errors: List[str] = []
    for ev in tool_events or []:
        if not isinstance(ev, Mapping):
            continue
        tool = str(ev.get("tool") or "")
        diff = ev.get("diff")
        if tool in _FILE_TOOLS:
            if isinstance(diff, str) and diff.strip():
                files.extend(_paths_from_diff(diff))
                symbols.extend(_symbols_from_diff(diff))
            else:
                guess = _path_from_command(str(ev.get("command") or ""))
                if guess:
                    files.append(guess)
        errors.extend(_errors_from_text(str(ev.get("output") or "")))
        if isinstance(diff, str):
            errors.extend(_errors_from_text(diff))
    seen_f = set()
    uniq_files = []
    for f in files:
        f = f.strip()
        if f and f not in seen_f:
            seen_f.add(f)
            uniq_files.append(f)
    seen_e = set()
    uniq_errors = []
    for e in errors:
        if e and e not in seen_e:
            seen_e.add(e)
            uniq_errors.append(e)
    seen_s = set()
    uniq_symbols = []
    for s in symbols:
        if s and s not in seen_s:
            seen_s.add(s)
            uniq_symbols.append(s)
    return (
        uniq_files[:_EVIDENCE_FILES_MAX],
        uniq_symbols[:_EVIDENCE_FILES_MAX],
        uniq_errors[:_ERRORS_MAX],
    )


# ── harness / tests interpretation ─────────────────────────────────────────

def _extract_tests(harness_meta: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    if not isinstance(harness_meta, Mapping):
        return None
    t = harness_meta.get("tests")
    if not isinstance(t, Mapping):
        return None
    out = {
        "command": str(t.get("command") or ""),
        "ok": t.get("ok"),
        "summary": str(t.get("summary") or ""),
    }
    if not out["command"] and not out["summary"] and out["ok"] is None:
        return None
    return out


def _derive_outcome(harness_meta: Optional[Mapping[str, Any]], tests: Optional[Mapping[str, Any]]) -> str:
    stop_reason = str((harness_meta or {}).get("stop_reason") or "")
    if stop_reason == "complete_unverified":
        return "unverified"
    if tests is None:
        return "unverified"
    ok = tests.get("ok")
    if ok is True:
        return "fixed"
    if ok is False:
        return "partial"
    return "unverified"


def _deterministic_summary(files: Sequence[str], tests: Optional[Mapping[str, Any]]) -> str:
    names = [os.path.basename(f) for f in files][:5]
    base = "changed " + ", ".join(names) if names else "changed files"
    if tests:
        if tests.get("summary"):
            base += f"; tests: {tests['summary']}"
        elif tests.get("ok") is True:
            base += "; tests passed"
        elif tests.get("ok") is False:
            base += "; tests failed"
    return base[:_SOLUTION_MAX_CHARS]


async def _model_summary(owner: Optional[str], task: str, files: Sequence[str],
                          errors: Sequence[str], tests: Optional[Mapping[str, Any]],
                          outcome: str) -> Optional[str]:
    if not owner:
        return None
    try:
        from src.endpoint_resolver import resolve_endpoint
        url, model, headers = resolve_endpoint("utility", owner=owner)
    except Exception:  # noqa: BLE001
        return None
    if not url or not model:
        return None
    prompt = (
        "Summarize, in one sentence (max 100 words), what was fixed and how, "
        "for future reference. Be concrete and factual, grounded only in the "
        "facts below — never invent details.\n\n"
        f"Task: {task}\n"
        f"Files changed: {', '.join(files) or '(none listed)'}\n"
        f"Errors seen: {'; '.join(errors) or '(none)'}\n"
        f"Tests: {json.dumps(dict(tests)) if tests else '(not run)'}\n"
        f"Outcome: {outcome}\n\n"
        "Respond with ONLY the one-sentence summary, no preamble."
    )
    try:
        from src.llm_core import llm_call_async
        raw = await llm_call_async(
            url=url, model=model, messages=[{"role": "user", "content": prompt}],
            headers=headers, temperature=0.2, max_tokens=200, timeout=30,
            max_retries=1, workload="background",
        )
    except Exception:  # noqa: BLE001
        logger.debug("[fix_memory] summary model call failed", exc_info=True)
        return None
    if isinstance(raw, tuple):
        raw = raw[0]
    text = (raw or "").strip() if isinstance(raw, str) else ""
    return text[:_SOLUTION_MAX_CHARS] or None


# ── rotation ─────────────────────────────────────────────────────────────

def _rotate(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Drop oldest first, but among entries beyond the cap prefer dropping
    `outcome == "unverified"` ones before anything else (oldest-first within
    each bucket)."""
    if len(entries) <= MAX_ENTRIES_PER_PROJECT:
        return entries
    ordered = sorted(entries, key=lambda e: float(e.get("ts") or 0.0))
    excess = len(ordered) - MAX_ENTRIES_PER_PROJECT
    unverified = [e for e in ordered if e.get("outcome") == "unverified"]
    to_drop_ids = {id(e) for e in unverified[:excess]}
    remaining_excess = excess - len(to_drop_ids)
    if remaining_excess > 0:
        rest = [e for e in ordered if id(e) not in to_drop_ids]
        for e in rest[:remaining_excess]:
            to_drop_ids.add(id(e))
    kept = [e for e in ordered if id(e) not in to_drop_ids]
    kept.sort(key=lambda e: float(e.get("ts") or 0.0))
    return kept


# ── public API ───────────────────────────────────────────────────────────

async def record_from_turn(owner: Optional[str], *, project_key: str, user_message: str,
                            tool_events: Sequence[Mapping[str, Any]],
                            harness_meta: Optional[Mapping[str, Any]] = None,
                            round_texts: Optional[Sequence[str]] = None,
                            workspace: str = "") -> Optional[Dict[str, Any]]:
    """Record one fix from a finished turn. Returns the stored entry, or
    `None` when the feature is off, there is no owner, or no file was
    actually touched this turn (nothing worth remembering)."""
    if not owner:
        return None
    try:
        from src.settings import get_setting
        if not get_setting("fix_memory_enabled", True):
            return None
    except Exception:  # noqa: BLE001
        pass

    files, symbols, errors = _extract_from_tool_events(tool_events or [])
    if not files:
        return None

    tests = _extract_tests(harness_meta)
    outcome = _derive_outcome(harness_meta, tests)
    task = (user_message or "").strip()[:_TASK_MAX_CHARS]

    summary = None
    try:
        summary = await _model_summary(owner, task, files, errors, tests, outcome)
    except Exception:  # noqa: BLE001
        summary = None
    if not summary:
        summary = _deterministic_summary(files, tests)

    now = time.time()
    import hashlib
    digest = hashlib.sha1(f"{now}|{task}|{'|'.join(files)}".encode("utf-8")).hexdigest()[:16]
    entry_id = f"fix-{digest}"

    tags = sorted({os.path.splitext(f)[1].lstrip(".") or "file" for f in files} | {outcome})

    entry = {
        "id": entry_id,
        "ts": now,
        "project": project_key or "",
        "task": task,
        "files": files,
        "symbols": symbols,
        "errors": errors,
        "tests": tests,
        "outcome": outcome,
        "solution": summary,
        "tags": tags,
    }

    try:
        _append_entry(owner, project_key, entry)
        index = _load_index(owner, project_key)
        index[entry_id] = _keywords(f"{task} {summary} {' '.join(errors)} {' '.join(files)}")
        _save_index(owner, project_key, index)

        entries = _load_entries(owner, project_key)
        if len(entries) > MAX_ENTRIES_PER_PROJECT:
            rotated = _rotate(entries)
            if len(rotated) != len(entries):
                _save_entries(owner, project_key, rotated)
                kept_ids = {e.get("id") for e in rotated}
                index = {k: v for k, v in index.items() if k in kept_ids}
                _save_index(owner, project_key, index)
    except Exception:  # noqa: BLE001
        logger.debug("[fix_memory] record_from_turn persist failed", exc_info=True)
        return entry

    return entry


def recall(owner: Optional[str], project_key: str, query: str, *,
           files: Optional[Sequence[str]] = None,
           errors: Optional[Sequence[str]] = None,
           k: int = 5) -> List[Dict[str, Any]]:
    """Most relevant past fixes for `query` in this project. Score = lexical
    (Jaccard) overlap of `query` against each entry's task+solution+errors,
    boosted when `files`/`errors` are shared, tied-broken by recency."""
    if not owner:
        return []
    entries = _load_entries(owner, project_key)
    if not entries:
        return []

    q_tokens = _tokenize(query or "")
    want_files = {f.strip() for f in (files or []) if f.strip()}
    want_errors = {normalize_error(e) for e in (errors or []) if e}

    scored = []
    for e in entries:
        text = f"{e.get('task', '')} {e.get('solution', '')} {' '.join(e.get('errors') or [])}"
        base = _jaccard(q_tokens, _tokenize(text))
        boost = 0.0
        if want_files:
            shared = want_files & set(e.get("files") or [])
            boost += min(0.4, 0.15 * len(shared))
        if want_errors:
            e_errs = set(e.get("errors") or [])
            if want_errors & e_errs:
                boost += 0.25
        score = round(base + boost, 4)
        if score <= 0.0 and not (want_files or want_errors) and not q_tokens:
            score = 0.0001  # empty query: still allow "most recent" to surface
        scored.append((score, float(e.get("ts") or 0.0), e))

    scored.sort(key=lambda t: (-t[0], -t[1]))
    out = []
    for score, _ts, e in scored[:max(0, int(k or 0))]:
        item = dict(e)
        item["score"] = score
        out.append(item)
    return out


def prompt_block(entries: Sequence[Mapping[str, Any]], budget_tokens: int = 600) -> str:
    """`"Past fixes in this project:\\n- task -> solution (files: a, b)"`, cut
    to `budget_tokens` (roughly 4 chars/token, or `src.model_context.
    estimate_tokens` when importable), most relevant entry first."""
    if not entries:
        return ""

    def _estimate(text: str) -> int:
        # Same chars*0.3 ratio `src.model_context.estimate_tokens` uses for
        # message content, applied directly to a plain string (that helper
        # takes a list of chat messages, not raw text).
        return max(1, int(len(text or "") * 0.3))

    header = "Past fixes in this project:"
    lines = [header]
    used = _estimate(header)
    for e in entries:
        task = str(e.get("task") or "").strip()
        solution = str(e.get("solution") or "").strip()
        fs = e.get("files") or []
        files_bit = f" (files: {', '.join(os.path.basename(f) for f in fs[:3])})" if fs else ""
        line = f"- {task[:140]} -> {solution[:200]}{files_bit}"
        cost = _estimate(line)
        if used + cost > budget_tokens and len(lines) > 1:
            break
        lines.append(line)
        used += cost
    if len(lines) <= 1:
        return ""
    return "\n".join(lines)


def stats(owner: Optional[str], project_key: str) -> Dict[str, Any]:
    entries = _load_entries(owner, project_key)
    by_outcome: Dict[str, int] = {}
    tag_counts: Dict[str, int] = {}
    for e in entries:
        by_outcome[e.get("outcome", "unverified")] = by_outcome.get(e.get("outcome", "unverified"), 0) + 1
        for tag in e.get("tags") or []:
            tag_counts[tag] = tag_counts.get(tag, 0) + 1
    top_tags = sorted(tag_counts.items(), key=lambda kv: -kv[1])[:10]
    return {
        "total": len(entries),
        "by_outcome": by_outcome,
        "top_tags": [t for t, _c in top_tags],
        "project": project_key or "",
    }


def forget(owner: Optional[str], id: str) -> bool:
    """Delete one entry by id, scanning every project log this owner has
    (an id is a content hash, unique on its own; the caller need not know
    which project it belongs to)."""
    if not owner or not id:
        return False
    owner_dir = _owner_dir(owner)
    try:
        names = os.listdir(owner_dir)
    except OSError:
        return False
    for name in names:
        if not name.endswith(".jsonl"):
            continue
        project = name[: -len(".jsonl")]
        entries = _load_entries(owner, project)
        if not any(e.get("id") == id for e in entries):
            continue
        remaining = [e for e in entries if e.get("id") != id]
        _save_entries(owner, project, remaining)
        index = _load_index(owner, project)
        if id in index:
            del index[id]
            _save_index(owner, project, index)
        return True
    return False
