"""Plan tracker (harness lot P1).

24 real chats on one project ("Silhouettes", see
`scratchpad/harness_wave/silhouettes_analysis.md` §2/§4) showed a plan
attachment (7 KB / 66 KB / 172 KB, inlined by `build_user_content` as
`=== File: ... ===` / `=== ZIP archive: ... ===` after the user's own text)
reinjected in full across 6+ new chats with no state carried between them.
`attachment_budgeted_text` (src/agent_harness.py) already cuts that body to a
heading table of contents once it is in the prompt, and the local model
(Qwen 27B) answered "Reference context received." with zero tool calls,
because nothing told it there WAS a plan with per-task state it could ask
about instead of the missing body.

This module is the fix's data layer: parse a plan attachment once, persist
it (and per-task status) under `DATA_DIR`, and hand back a short system
brief plus only the current task's text instead of the whole attachment.
Pure parsing + local JSON persistence only — no network, no repo writes
(never touches the user's own project tree), Windows-safe paths via
`os.path.join`/`os.path` throughout, no symlinks assumed.

Wiring this into `src/agent_loop.py` (which turn calls what, when) and
`TurnLedger.check_completion` (src/agent_harness.py) is deliberately NOT
done here — see `scratchpad/harness_wave/P1_wiring.md` for the exact diff
an integrator applies on top of this module.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

from src.constants import DATA_DIR

PLAN_TRACKER_DIR = os.path.join(DATA_DIR, "plan_tracker")
#: Bump when parse_plan changes shape: a stored tracker from an older parser
#: is re-parsed on next sight, keeping per-task state by key/title.
PARSER_VERSION = 4

# ---------------------------------------------------------------------------
# Attachment detection
# ---------------------------------------------------------------------------

# Mirrors the marker `build_user_content` writes and
# `src.agent_harness._ATTACHMENT_TITLE_RE`/`_INLINED_ATTACHMENT_RE` already
# parse for the same text — kept local (not imported) so this module has no
# import-time dependency on agent_harness; `replace_attachment` imports
# `user_authored_text` from it lazily instead, at call time only.
_MARKER_RE = re.compile(r"=== (File|ZIP archive): (.+?) ===\n?")
# Header tag `build_user_content` / `_process_zip_file` add to the inlined block
# of a plan-like text attachment: the id of the FULL text stashed on disk, so
# the tracker does not depend on the (truncated) inline copy.
_PLAN_ID_RE = re.compile(r"Plan-Id:\s*([0-9a-f]{16})")
_FULL_TEXT_MAX_BYTES = 4 * 1024 * 1024
_FULL_TEXT_KEEP = 60

_HEADING_RE = re.compile(r"^(#{1,4})\s+(.+?)\s*$", re.M)
_NUM_LIST_RE = re.compile(r"^\s{0,3}(\d{1,3})[.)]\s+(.+?)\s*$", re.M)
_CHECKBOX_RE = re.compile(r"^\s{0,3}-\s*\[( |x|X)\]\s*(.+?)\s*$", re.M)
# Fallbacks for a plan written without headings or numbers: top-level
# bullets, and lines that open with their own step word ("Paso 2:",
# "Step 3 -", "Fase 1."). Only used when nothing above matched, and only
# with at least three steps, so a letter or an essay stays at 0 tasks.
_BULLET_RE = re.compile(r"^\s{0,1}[-*\u2022]\s+(?!\[[ xX]\])(.+?)\s*$", re.M)
_STEP_LINE_RE = re.compile(
    r"^\s{0,3}((?:paso|step|fase|phase|etapa|stage)\s*\d{1,3})\s*[:.)\u2013\u2014-]\s*(.+?)\s*$",
    re.M | re.I,
)
_MIN_FALLBACK_STEPS = 3

_KEY_RE = re.compile(
    r"\b(WP\d+|Fase\s*\d+|Tarea\s*\d+(?:\.\d+)?|Task\s*\d+(?:\.\d+)?)\b", re.I
)

_ACCEPT_LABEL_LINE_RE = re.compile(
    r"^\s*#{0,4}\s*\**\s*"
    r"(Acceptance|Criterios?(?:\s+de\s+aceptaci[oó]n)?|Done when|"
    r"Definition of done|Verificaci[oó]n|Tests?|Comprobaci[oó]n)"
    r"\s*\**:?\s*$",
    re.I,
)
_ACCEPT_LABEL_INLINE_RE = re.compile(
    r"^\s*\**\s*"
    r"(Acceptance|Criterios?(?:\s+de\s+aceptaci[oó]n)?|Done when|"
    r"Definition of done|Verificaci[oó]n|Tests?|Comprobaci[oó]n)"
    r"\s*\**:\s*(.+)$",
    re.I,
)

_DEPENDS_RE = re.compile(
    r"(?:depende\s+de|after|requires)\s+"
    r"((?:WP\d+|Fase\s*\d+|Tarea\s*\d+(?:\.\d+)?|Task\s*\d+(?:\.\d+)?)"
    r"(?:\s*(?:,|y|and)\s*(?:WP\d+|Fase\s*\d+|Tarea\s*\d+(?:\.\d+)?|Task\s*\d+(?:\.\d+)?))*)",
    re.I,
)


def _iter_attachments(text: str):
    """Yield (kind, title, body) for every inlined `=== File/ZIP: ... ===`
    block in `text`, in order. Never raises."""
    marks = list(_MARKER_RE.finditer(text or ""))
    for i, m in enumerate(marks):
        start = m.end()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        yield m.group(1), m.group(2).strip(), text[start:end]


def _default_min_chars() -> int:
    try:
        from src.settings import get_setting
        return int(get_setting("agent_plan_tracker_min_chars", 3000))
    except Exception:
        return 3000


def _split_authored(text: str) -> Tuple[str, str]:
    """(what the user typed, the inlined attachment blocks that follow it)."""
    raw = text or ""
    m = re.search(r"\n=== (?:File|ZIP archive): ", raw)
    if not m:
        return raw, ""
    return raw[:m.start()], raw[m.start():]


def _resolve_full_body(body: str) -> str:
    """The inline copy of a big attachment is cut by the shared 24000-character
    budget; a `Plan-Id:` tag in its header names the full text kept on disk."""
    try:
        m = _PLAN_ID_RE.search((body or "")[:600])
        if m:
            full = load_stashed_text(m.group(1))
            if full:
                return full
    except Exception:
        pass
    return body


def find_plan_attachment(text: str, min_chars: Optional[int] = None) -> Optional[Tuple[str, str]]:
    """First inlined attachment in a user message that looks like a plan:
    >= 3 detectable tasks AND (body >= min_chars, OR a small but deliberately
    structured plan sent with an execute request such as "ejecuta el plan").
    Returns (title, FULL body): when the inline copy was cut by the attachment
    budget the full text comes from the stash named by its `Plan-Id` tag.
    None otherwise. Never raises."""
    try:
        cap = int(min_chars) if min_chars is not None else _default_min_chars()
    except Exception:
        cap = 3000
    try:
        authored, _rest = _split_authored(text or "")
        executes = strict_execute_request(authored)
        for kind, title, body in _iter_attachments(text or ""):
            if kind != "File":
                continue
            full = _resolve_full_body(body)
            big = len(_normalize(full)) >= cap
            if not big and not executes:
                continue
            spec = parse_plan(title, full)
            if len(spec.tasks) < 3:
                continue
            if not big and _split_sections_ex(_normalize(full))[0] in ("", "bullet"):
                continue
            return title, full
        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Full text of a plan-like attachment (kept off the prompt)
# ---------------------------------------------------------------------------

def _full_dir() -> str:
    return os.path.join(os.path.dirname(PLAN_TRACKER_DIR), "plan_tracker_full")


def stash_plan_text(body: str, min_tasks: int = 3) -> str:
    """Keep the FULL text of a plan-like text attachment under DATA_DIR and
    return its id (the plan hash), or "" when the text is not plan-like or too
    big. `build_user_content` tags the inlined block with it (`Plan-Id:`), so
    the tracker sees every task even when the prompt copy is truncated."""
    try:
        norm = _normalize(body)
        if not norm or len(norm.encode("utf-8", "ignore")) > _FULL_TEXT_MAX_BYTES:
            return ""
        spec = parse_plan("", norm)
        if len(spec.tasks) < int(min_tasks):
            return ""
        d = _full_dir()
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, spec.hash + ".txt")
        if not os.path.isfile(path):
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(norm)
            os.replace(tmp, path)
            _prune_full(d)
        return spec.hash
    except Exception:
        return ""


def load_stashed_text(plan_id: str) -> Optional[str]:
    safe = re.sub(r"[^0-9a-f]+", "", str(plan_id or ""))[:32]
    if not safe:
        return None
    try:
        with open(os.path.join(_full_dir(), safe + ".txt"), "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return None


def _prune_full(d: str) -> None:
    try:
        files = [os.path.join(d, n) for n in os.listdir(d) if n.endswith(".txt")]
        if len(files) <= _FULL_TEXT_KEEP:
            return
        files.sort(key=lambda p: os.path.getmtime(p))
        for p in files[:len(files) - _FULL_TEXT_KEEP]:
            try:
                os.remove(p)
            except OSError:
                pass
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

@dataclass
class PlanTask:
    id: str
    key: str
    title: str
    text: str
    acceptance: List[str] = field(default_factory=list)
    files: List[str] = field(default_factory=list)
    depends_on: List[str] = field(default_factory=list)
    #: Typed goals declared in the task's text (`test_passes: ...`,
    #: `http_ok: ...`); `plan_done` runs them (src/plan_goals.py).
    goals: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class PlanSpec:
    hash: str
    title: str
    tasks: List[PlanTask] = field(default_factory=list)


def _normalize(body: str) -> str:
    return (body or "").replace("\r\n", "\n").strip()


def _extract_key(title_line: str) -> Optional[str]:
    m = _KEY_RE.search(title_line or "")
    return m.group(1).strip() if m else None


def _extract_files(text: str) -> List[str]:
    try:
        from src.agent_harness import extract_path_tokens
        return list(extract_path_tokens(text) or [])
    except Exception:
        return []


def _extract_acceptance(text: str) -> List[str]:
    items: List[str] = []
    lines = (text or "").split("\n")
    n = len(lines)
    i = 0
    while i < n:
        line = lines[i]
        if _ACCEPT_LABEL_LINE_RE.match(line):
            j = i + 1
            while j < n and lines[j].strip() != "" and not _HEADING_RE.match(lines[j]):
                stripped = lines[j].strip()
                stripped = re.sub(r"^[-*]\s*(\[[ xX]\]\s*)?", "", stripped).strip()
                if stripped:
                    items.append(stripped)
                j += 1
            i = j
            continue
        m = _ACCEPT_LABEL_INLINE_RE.match(line)
        if m and m.group(2).strip():
            items.append(m.group(2).strip())
        cb = _CHECKBOX_RE.match(line)
        if cb:
            items.append(cb.group(2).strip())
        i += 1
    seen: set = set()
    out: List[str] = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out


def _extract_goals(text: str) -> List[Dict[str, Any]]:
    try:
        from src.plan_goals import extract_goals
        return extract_goals(text)
    except Exception:
        return []


def _extract_depends(text: str) -> List[str]:
    out: List[str] = []
    seen: set = set()
    for m in _DEPENDS_RE.finditer(text or ""):
        for part in re.split(r"\s*,\s*|\s+y\s+|\s+and\s+", m.group(1)):
            k = part.strip()
            if k and k.lower() not in seen:
                seen.add(k.lower())
                out.append(k)
    return out


def _task_heading_level(marks: List["re.Match[str]"]) -> int:
    """Which heading level holds the TASKS. Headings above it are the plan's
    own title / phase headers (the `# Plan de implementación` line that
    opens every real plan is not a task). Prefer the shallowest level with
    >= 3 headings where most carry a task key (WP03, Tarea 7, Task 2.1);
    else the shallowest level with >= 3 headings; else the deepest level."""
    by_level: Dict[int, List[str]] = {}
    for m in marks:
        by_level.setdefault(len(m.group(1)), []).append(m.group(2))
    keyed = [lvl for lvl in sorted(by_level)
             if len(by_level[lvl]) >= 3
             and sum(1 for t in by_level[lvl] if _extract_key(t)) * 2 >= len(by_level[lvl])]
    if keyed:
        return keyed[0]
    plural = [lvl for lvl in sorted(by_level) if len(by_level[lvl]) >= 3]
    if plural:
        return plural[0]
    return max(by_level) if by_level else 1


def _split_sections(text: str) -> List[Tuple[Optional[str], str, str]]:
    """(key_hint, section_title, section_body) by markdown headings first,
    then first-level numbered lists, then checkboxes. Empty when the text
    has none of those (unstructured attachment -> 0 tasks)."""
    return _split_sections_ex(text)[1]


def _split_sections_ex(text: str) -> Tuple[str, List[Tuple[Optional[str], str, str]]]:
    """`_split_sections` plus which structure produced the tasks:
    heading | numbered | checkbox | step | bullet | "" (none). Bullets are the
    weakest signal (any list is made of them), the others are deliberate."""
    marks = list(_HEADING_RE.finditer(text))
    heading_out: List[Tuple[Optional[str], str, str]] = []
    if marks:
        level = _task_heading_level(marks)
        out: List[Tuple[Optional[str], str, str]] = []
        for i, m in enumerate(marks):
            if len(m.group(1)) != level:
                continue
            start = m.end()
            end = len(text)
            for nxt in marks[i + 1:]:
                if len(nxt.group(1)) <= level:
                    end = nxt.start()
                    break
            out.append((None, m.group(2).strip(), text[start:end]))
        if len(out) >= 3:
            return "heading", out
        # One title heading over a numbered list is a list of tasks, not one task.
        heading_out = out
    for kind, pattern in (("numbered", _NUM_LIST_RE), ("checkbox", _CHECKBOX_RE)):
        marks = list(pattern.finditer(text))
        if not marks or (heading_out and len(marks) < 3):
            continue
        out = []
        for i, m in enumerate(marks):
            start = m.end()
            end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
            out.append((None, m.group(2).strip(), text[start:end]))
        return kind, out
    marks = list(_STEP_LINE_RE.finditer(text))
    if len(marks) >= _MIN_FALLBACK_STEPS:
        out = []
        for i, m in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
            out.append((None, f"{m.group(1).strip()}: {m.group(2).strip()}", text[m.end():end]))
        return "step", out
    marks = list(_BULLET_RE.finditer(text))
    if len(marks) >= _MIN_FALLBACK_STEPS:
        out = []
        for i, m in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
            out.append((None, m.group(1).strip(), text[m.end():end]))
        return "bullet", out
    if heading_out:
        return "heading", heading_out
    return "", []


def parse_plan(title: str, body: str) -> PlanSpec:
    """Never raises: unstructured text parses to a PlanSpec with 0 tasks."""
    norm = _normalize(body)
    try:
        h = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]
    except Exception:
        h = hashlib.sha256(b"").hexdigest()[:16]
    plan_title = (title or "").strip() or "untitled plan"
    try:
        _top = _HEADING_RE.search(norm)
        if _top and len(_top.group(1)) == 1 and re.search(r"\.(md|txt|markdown)$", plan_title, re.I):
            plan_title = f"{plan_title} — {_top.group(2).strip()[:120]}"
        sections = _split_sections(norm)
        tasks: List[PlanTask] = []
        for i, (_hint, sec_title, sec_body) in enumerate(sections, start=1):
            tid = f"t{i:02d}"
            key = _extract_key(sec_title) or tid
            tasks.append(PlanTask(
                id=tid,
                key=key,
                title=sec_title[:200] or tid,
                text=sec_body.strip(),
                acceptance=_extract_acceptance(sec_body),
                files=_extract_files(sec_body),
                depends_on=_extract_depends(sec_body),
                goals=_extract_goals(sec_body),
            ))
        return PlanSpec(hash=h, title=plan_title, tasks=tasks)
    except Exception:
        return PlanSpec(hash=h, title=plan_title, tasks=[])


# ---------------------------------------------------------------------------
# Persistence (DATA_DIR only, never the user's repo)
# ---------------------------------------------------------------------------

def scope_for(project_id: Optional[str] = None, workspace: Optional[str] = None) -> str:
    """`project_id` when there is one, else sha1[:12] of a normalized
    `workspace` path (Windows `\\` folded to `/`, case-folded)."""
    pid = str(project_id or "").strip()
    if pid:
        return pid
    ws = str(workspace or "").strip().replace("\\", "/").rstrip("/").lower()
    return hashlib.sha1(ws.encode("utf-8")).hexdigest()[:12]


def _safe_scope(scope: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(scope or "").strip())
    return s[:150] or "default"


def _scope_dir(scope: str) -> str:
    return os.path.join(PLAN_TRACKER_DIR, _safe_scope(scope))


def _tracker_path(scope: str, hash_: str) -> str:
    safe_hash = re.sub(r"[^A-Za-z0-9]+", "", str(hash_ or ""))[:32] or "unknown"
    return os.path.join(_scope_dir(scope), f"{safe_hash}.json")


def load(scope: str, hash_: str) -> Optional[Dict[str, Any]]:
    path = _tracker_path(scope, hash_)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save(scope: str, tracker: Dict[str, Any]) -> None:
    d = _scope_dir(scope)
    os.makedirs(d, exist_ok=True)
    path = _tracker_path(scope, tracker.get("hash", ""))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(tracker, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def upsert_from_attachment(scope: str, title: str, body: str) -> Optional[Dict[str, Any]]:
    """Parse + persist a new plan, or bump `seen_count`/`last_seen_at` on an
    already-known one (same hash) without re-parsing or touching its state.
    None when the body has no detectable task structure."""
    spec = parse_plan(title, body)
    if not spec.tasks:
        return None
    now = time.time()
    existing = load(scope, spec.hash)
    if existing and existing.get("parser_version") == PARSER_VERSION:
        existing["seen_count"] = int(existing.get("seen_count", 1)) + 1
        existing["last_seen_at"] = now
        save(scope, existing)
        return existing
    tasks = [asdict(t) for t in spec.tasks]
    state = {
        t["id"]: {"status": "pending", "evidence": "", "updated_at": now, "turn": None}
        for t in tasks
    }
    if existing:
        # A newer parser re-reads the same plan: keep every task's state,
        # matched by key first (WP03) then by title, never by position.
        old_by_key = {str(t.get("key") or ""): t["id"] for t in existing.get("tasks", []) if t.get("key")}
        old_by_title = {str(t.get("title") or "").strip().lower(): t["id"] for t in existing.get("tasks", [])}
        old_state = existing.get("state", {})
        for t in tasks:
            oid = old_by_key.get(t["key"]) or old_by_title.get(str(t["title"]).strip().lower())
            if oid and oid in old_state:
                state[t["id"]] = dict(old_state[oid])
    tracker: Dict[str, Any] = {
        "hash": spec.hash,
        "title": spec.title,
        "source_title": title,
        "parser_version": PARSER_VERSION,
        "created_at": (existing or {}).get("created_at") or now,
        "last_seen_at": now,
        "seen_count": int((existing or {}).get("seen_count", 0)) + 1,
        "tasks": tasks,
        "state": state,
    }
    save(scope, tracker)
    return tracker


def active(scope: str) -> Optional[Dict[str, Any]]:
    """Tracker with the most recent `last_seen_at` in this scope, or None."""
    d = _scope_dir(scope)
    try:
        names = [n for n in os.listdir(d) if n.endswith(".json") and not n.endswith(".tmp")]
    except FileNotFoundError:
        return None
    except Exception:
        return None
    best: Optional[Dict[str, Any]] = None
    for n in names:
        t = load(scope, n[:-5])
        if not t:
            continue
        if best is None or float(t.get("last_seen_at") or 0) > float(best.get("last_seen_at") or 0):
            best = t
    return best


def find_task(tracker: Dict[str, Any], ident: str) -> Optional[Dict[str, Any]]:
    """A task by its stable `id` (`t03`) or its plan-native `key`
    (`WP03`/`Tarea 7`), whichever is given."""
    ident = str(ident or "").strip()
    if not ident:
        return None
    for t in tracker.get("tasks", []):
        if t.get("id") == ident:
            return t
    low = ident.lower()
    for t in tracker.get("tasks", []):
        if str(t.get("key") or "").lower() == low:
            return t
    return None


# Back-compat alias for internal call sites written before the rename.
_task_by_id_or_key = find_task


def current_task(tracker: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """First `in_progress`; else first `pending` whose `depends_on` are all
    `done`; else the first `pending`; else None (nothing left)."""
    tasks = tracker.get("tasks", [])
    state = tracker.get("state", {})

    for t in tasks:
        if state.get(t["id"], {}).get("status") == "in_progress":
            return t

    def deps_done(t: Dict[str, Any]) -> bool:
        for dep in t.get("depends_on") or []:
            dep_task = _task_by_id_or_key(tracker, dep)
            if dep_task and state.get(dep_task["id"], {}).get("status") != "done":
                return False
        return True

    for t in tasks:
        if state.get(t["id"], {}).get("status") == "pending" and deps_done(t):
            return t
    for t in tasks:
        if state.get(t["id"], {}).get("status") == "pending":
            return t
    return None


def mark(
    scope: str, hash_: str, task_id: str, status: str,
    evidence: str = "", turn: Optional[int] = None,
    goals: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Set a task's status. `goals` is the report `plan_goals.evaluate_goals`
    produced for the `plan_done` that sealed it (kept with the evidence)."""
    tracker = load(scope, hash_)
    if not tracker:
        return None
    t = _task_by_id_or_key(tracker, task_id)
    if not t:
        return None
    entry: Dict[str, Any] = {
        "status": status,
        "evidence": evidence or "",
        "updated_at": time.time(),
        "turn": turn,
    }
    if goals:
        entry["goals"] = compact_goals(goals)
    tracker.setdefault("state", {})[t["id"]] = entry
    tracker["last_seen_at"] = time.time()
    save(scope, tracker)
    return tracker


def compact_goals(report: Dict[str, Any]) -> Dict[str, Any]:
    """The part of a goals report worth persisting: counts plus one short row
    per goal (no unbounded output)."""
    rows = []
    for r in (report or {}).get("results") or []:
        rows.append({
            "id": r.get("id"), "kind": r.get("kind"), "target": str(r.get("target") or "")[:160],
            "status": r.get("status"), "evidence": str(r.get("evidence") or "")[:400],
        })
    return {
        "ok": bool((report or {}).get("ok")),
        "passed": int((report or {}).get("passed") or 0),
        "failed": int((report or {}).get("failed") or 0),
        "skipped": int((report or {}).get("skipped") or 0),
        "checked_at": (report or {}).get("checked_at"),
        "results": rows,
    }


def record_goals(scope: str, hash_: str, task_id: str, report: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Persist the latest goals report on a task WITHOUT changing its status
    (a failed run leaves the task open but remembers why)."""
    tracker = load(scope, hash_)
    if not tracker:
        return None
    t = _task_by_id_or_key(tracker, task_id)
    if not t:
        return None
    state = tracker.setdefault("state", {})
    entry = dict(state.get(t["id"]) or {"status": "pending", "evidence": "", "turn": None})
    entry["goals"] = compact_goals(report)
    entry["updated_at"] = time.time()
    state[t["id"]] = entry
    save(scope, tracker)
    return tracker


def _norm_path(path: str) -> str:
    return str(path or "").replace("\\", "/").strip().strip("/").lower()


def _task_matches_todo(task: Dict[str, Any], todo: Dict[str, Any]) -> bool:
    content = str(todo.get("content") or "").strip().lower()
    if not content:
        return False
    key = str(task.get("key") or "").strip().lower()
    title = str(task.get("title") or "").strip().lower()
    if key and re.search(r"\b" + re.escape(key) + r"\b", content):
        return True
    return bool(title) and (title in content or content in title)


def reconcile(
    scope: str, tracker: Dict[str, Any], *,
    todos: Optional[List[Dict[str, Any]]] = None,
    mutated_paths: Optional[List[str]] = None,
    workspace: Optional[str] = None,
    turn: Optional[Any] = None,
) -> List[str]:
    """Close plan tasks the turn evidently finished even though the model
    never called plan_done (live 17-09: the 27B did all four WPs through
    todowrite/update_plan and the tracker still said 0/4).

    A pending task becomes ``done`` when EITHER a completed todo names its
    key/title, OR every file the task lists exists in the workspace and at
    least one of them was mutated this turn. Evidence is recorded as such;
    returns the ids marked. Never raises."""
    marked: List[str] = []
    try:
        state = tracker.setdefault("state", {})
        done_todos = [t for t in (todos or []) if isinstance(t, dict)
                      and str(t.get("status") or "").lower() in ("completed", "done")]
        muts = {_norm_path(p) for p in (mutated_paths or []) if p}
        for task in tracker.get("tasks", []):
            st = state.get(task["id"], {}).get("status")
            if st in ("done", "skipped"):
                continue
            # A task with typed goals is sealed by plan_done (which runs them),
            # never by a todo or a written file.
            try:
                from src.plan_goals import holds_auto_close
                if holds_auto_close(task, state.get(task["id"])):
                    continue
            except Exception:
                pass
            evidence = ""
            if any(_task_matches_todo(task, td) for td in done_todos):
                evidence = "todo completed: " + next(
                    str(td.get("content") or "")[:120] for td in done_todos if _task_matches_todo(task, td))
            elif task.get("files") and workspace and muts:
                files = [str(f) for f in task.get("files") or []]
                exist = all(os.path.isfile(os.path.join(workspace, f.replace("/", os.sep))) for f in files)
                touched = [f for f in files if _norm_path(f) in muts
                           or any(m.endswith("/" + _norm_path(f)) for m in muts)]
                if exist and touched:
                    evidence = "files present and written this turn: " + ", ".join(touched[:6])
            if evidence:
                state[task["id"]] = {"status": "done", "evidence": evidence,
                                     "updated_at": time.time(), "turn": turn, "auto": True}
                marked.append(task["id"])
        if marked:
            tracker["last_seen_at"] = time.time()
            save(scope, tracker)
    except Exception:
        return marked
    return marked


def progress(tracker: Dict[str, Any]) -> Dict[str, int]:
    tasks = tracker.get("tasks", [])
    state = tracker.get("state", {})
    total = len(tasks)
    done = sum(1 for t in tasks if state.get(t["id"], {}).get("status") == "done")
    skipped = sum(1 for t in tasks if state.get(t["id"], {}).get("status") == "skipped")
    pending = total - done - skipped
    return {"done": done, "total": total, "skipped": skipped, "pending": pending}


# ---------------------------------------------------------------------------
# Prompt surfaces
# ---------------------------------------------------------------------------

def brief(tracker: Dict[str, Any], language: str = "en", max_chars: int = 2500) -> str:
    """A deterministic system-block summary: progress, the current task, and
    the next 3 pending — no adjectives, no attempt to read like prose."""
    lang = "es" if str(language or "en").lower().startswith("es") else "en"
    prog = progress(tracker)
    cur = current_task(tracker)
    state = tracker.get("state", {})
    lines: List[str] = []

    if lang == "es":
        lines.append(
            f'Plan activo "{tracker.get("title", "")}" ({tracker.get("hash", "")}): '
            f'{prog["done"]}/{prog["total"]} hechas.'
        )
    else:
        lines.append(
            f'Active plan "{tracker.get("title", "")}" ({tracker.get("hash", "")}): '
            f'{prog["done"]}/{prog["total"]} done.'
        )

    if cur:
        key = cur.get("key") or cur.get("id")
        acc = "; ".join(cur.get("acceptance") or [])
        files = ", ".join(cur.get("files") or [])
        if lang == "es":
            lines.append(
                f'Tarea actual {key} — {cur.get("title", "")}. '
                f'Criterios: {acc}. Ficheros: {files}.'
            )
        else:
            lines.append(
                f'Current task {key} — {cur.get("title", "")}. '
                f'Acceptance: {acc}. Files: {files}.'
            )

    if lang == "es":
        lines.append(
            "Usa plan_status/plan_task/plan_done; el plan completo NO esta en "
            "este mensaje y no debe volver a pedirse."
        )
    else:
        lines.append(
            "Use plan_status/plan_task/plan_done; the full plan is NOT in "
            "this message and must not be requested again."
        )

    cur_id = cur.get("id") if cur else None
    pending = [
        t for t in tracker.get("tasks", [])
        if state.get(t["id"], {}).get("status") == "pending" and t.get("id") != cur_id
    ]
    for t in pending[:3]:
        key = t.get("key") or t.get("id")
        lines.append(f'- {key}: {t.get("title", "")}')

    out = "\n".join(lines)
    cap = max(0, int(max_chars or 0))
    if cap and len(out) > cap:
        out = out[:cap].rstrip()
    return out


def replace_attachment(text: str, tracker: Dict[str, Any], max_task_chars: int = 6000,
                       authored: Optional[str] = None) -> str:
    """`user_authored_text(text)` + brief + only the current task's text —
    the inlined attachment body (which may be 172 KB) never reaches this
    output. `authored` replaces the typed part (a pasted plan keeps only its
    intro there)."""
    if authored is None:
        try:
            from src.agent_harness import user_authored_text
            authored = user_authored_text(text)
        except Exception:
            authored = text or ""

    parts = [authored, "", brief(tracker)]
    cur = current_task(tracker)
    if cur:
        key = cur.get("key") or cur.get("id")
        body = cur.get("text") or ""
        cap = max(0, int(max_task_chars or 0))
        if cap and len(body) > cap:
            body = body[:cap].rstrip() + "\n...[truncated]"
        parts.append("")
        parts.append(f'=== Plan task {key}: {cur.get("title", "")} ===')
        parts.append(body)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Measure 4: distinguish "here's a plan for context" from "execute it"
# ---------------------------------------------------------------------------

_EXEC_VERB_RE = re.compile(
    r"\b("
    r"implementa(?:r|do)?|implement|sigue|contin[uú]a|continue|finish|termina|"
    r"completa|haz(?:lo)?|build|crea|start|empieza|do it|go|adelante|"
    r"keep\s+(?:going|implementing)|next task|siguiente|"
    r"ejecuta(?:r|lo)?|execute|run|proceed|procede|aplica(?:r|lo)?|realiza(?:r|lo)?"
    r")\b",
    re.I,
)


def looks_like_execute_request(user_text: str) -> bool:
    """True for a short imperative-execution message ("Sigue implementando
    el plan", "Implementa todo lo posible", "Finish"); False for a question
    or a longer, non-imperative message ("Aquí tienes el plan para que lo
    tengas de contexto")."""
    text = (user_text or "").strip()
    if not text or "?" in text or len(text) > 400:
        return False
    return bool(_EXEC_VERB_RE.search(text))


# ---------------------------------------------------------------------------
# A plan typed or pasted in the message body (no attachment)
# ---------------------------------------------------------------------------

_PLAN_WORDS_RE = re.compile(
    r"\b(plan|plano|tareas?|tasks?|pasos?|steps?|fases?|phases?|etapas?|checklist|todo|"
    r"roadmap|hoja de ruta|implementa\w*|implement\w*)\b", re.I)


def _default_paste_min_chars() -> int:
    try:
        from src.settings import get_setting
        return int(get_setting("agent_plan_tracker_paste_min_chars", 600))
    except Exception:
        return 600


def _pasted_intro(norm: str) -> str:
    """What the user wrote before the first task of a pasted plan."""
    kind, sections = _split_sections_ex(norm)
    if not sections:
        return ""
    idx = norm.find(sections[0][1])
    if idx <= 0:
        return ""
    return norm[:norm.rfind("\n", 0, idx) + 1].strip()


def find_plan_in_text(text: str, min_chars: Optional[int] = None) -> Optional[Tuple[str, str]]:
    """A plan typed or pasted in the message itself (no `=== File:` block).

    Needs >= 3 tasks from a real list (numbered, checkboxes, "Paso N", task
    headings; plain bullets only when the message is an execute request) and
    EITHER the message is long enough (`agent_plan_tracker_paste_min_chars`,
    with plan vocabulary or a deliberate list) OR it reads as an execute
    request ("ejecuta esto", "implementa el plan"). A question, or a list of
    questions, is never a plan. Returns (title, body from the first task on —
    the intro is kept in the prompt, the tasks go to the tracker) or None.
    Never raises."""
    try:
        floor = int(min_chars) if min_chars is not None else _default_paste_min_chars()
        if floor <= 0:
            return None
        authored, _rest = _split_authored(text or "")
        norm = _normalize(authored)
        if len(norm) < 30:
            return None
        kind, sections = _split_sections_ex(norm)
        if len(sections) < 3:
            return None
        titles = [str(s[1]) for s in sections]
        if sum(1 for t in titles if t.rstrip().endswith("?")) * 2 >= len(titles):
            return None
        intro = _pasted_intro(norm)
        tail = intro[-300:]
        asks = "?" in tail
        executes = strict_execute_request(tail)
        strong = kind in ("heading", "numbered", "checkbox", "step")
        deliberate = kind in ("checkbox", "step") or bool(_PLAN_WORDS_RE.search(norm[:600]))
        if executes and not asks:
            ok = strong or bool(_PLAN_WORDS_RE.search(norm[:600]))
        elif asks:
            ok = False
        else:
            ok = strong and deliberate and len(norm) >= floor
        if not ok:
            return None
        idx = norm.find(titles[0])
        start = norm.rfind("\n", 0, idx) + 1 if idx > 0 else 0
        body = norm[start:].strip()
        first = (intro.splitlines() or [titles[0]])[0].strip() or titles[0]
        return "pasted plan: " + first[:100], body
    except Exception:
        return None


def replace_pasted(text: str, tracker: Dict[str, Any], max_task_chars: int = 6000) -> str:
    """The prompt copy of a message that carried a pasted plan: the user's
    intro, the brief and only the current task (like `replace_attachment`);
    any real attachment blocks that followed are kept."""
    authored, rest = _split_authored(text or "")
    intro = _pasted_intro(_normalize(authored))[:6000]
    if not intro:
        intro = "(plan pegado en el mensaje, ya registrado en el seguimiento)"
    return replace_attachment(text, tracker, max_task_chars, authored=intro) + rest


# ---------------------------------------------------------------------------
# Keep working inside the turn while tasks remain
# ---------------------------------------------------------------------------

#: Consecutive continuations that close no task before the turn asks.
AUTOCONTINUE_FRUITLESS_MAX = 3


def closed_count(tracker: Dict[str, Any]) -> int:
    """Tasks that are finished one way or the other (done or skipped)."""
    p = progress(tracker or {})
    return int(p.get("done", 0)) + int(p.get("skipped", 0))


def autocontinue_decision(
    tracker: Optional[Dict[str, Any]], *, enabled: bool = True, armed: bool = True,
    used: int = 0, cap: int = 40, fruitless: int = 0,
    awaiting_user: bool = False, recovery_active: bool = False,
) -> Dict[str, Any]:
    """Whether a turn that just ended without a tool call must go on with the
    plan. Pure: `used` continuations granted this turn, `fruitless` of them
    since the last task closed. Actions: ``continue``; ``stop`` (end the turn
    asking a concrete question); ``none`` (let the turn end as usual)."""
    if not enabled:
        return {"action": "none", "reason": "disabled"}
    if not tracker:
        return {"action": "none", "reason": "no_plan"}
    cur = current_task(tracker)
    if not cur:
        return {"action": "none", "reason": "done"}
    if not armed:
        return {"action": "none", "reason": "not_armed"}
    if awaiting_user:
        return {"action": "none", "reason": "awaiting_user", "task": cur}
    if recovery_active:
        return {"action": "stop", "reason": "loop_recovery", "task": cur}
    if int(used) >= int(cap):
        return {"action": "stop", "reason": "cap", "task": cur}
    if int(fruitless) >= AUTOCONTINUE_FRUITLESS_MAX:
        return {"action": "stop", "reason": "no_progress", "task": cur}
    return {"action": "continue", "reason": "pending", "task": cur}


def continuation_note(tracker: Dict[str, Any]) -> str:
    """The harness message that sends the model on to the next task."""
    prog = progress(tracker)
    cur = current_task(tracker) or {}
    key = cur.get("key") or cur.get("id") or "?"
    acc = "; ".join((cur.get("acceptance") or [])[:4])
    files = ", ".join((cur.get("files") or [])[:6])
    return (
        "[Harness check — automatic runtime message, not a new user request] "
        f'The plan "{tracker.get("title", "")}" is not finished: {prog["done"]}/{prog["total"]} '
        f"tasks done, {prog['pending']} still open. Next task: {key} — {cur.get('title', '')}. "
        + (f"Acceptance: {acc}. " if acc else "")
        + (f"Files: {files}. " if files else "")
        + "The plan itself is the go-ahead: do NOT ask the user whether to continue and do not "
        "restate what you already did. Call plan_task for this task, do the work with tools, "
        "verify it the way the task says (run it, read the file back), then call plan_done with "
        "concrete evidence (what you ran or created and what it showed). If it is truly impossible "
        "or already covered, call plan_skip with the reason and go to the next one. Stop only when "
        "every task is done or skipped, or when a decision only the user can make blocks you "
        "(then call ask_user once, naming the task)."
    )


# A stricter reading for importing a plan WITHOUT the size evidence: the broad
# list above also fires on "here you go", which is not a go-ahead.
_STRICT_EXEC_RE = re.compile(
    r"\b(implementa(?:r|do|lo)?|implement|sigue|contin[u\u00fa]a|continue|finish|termina|"
    r"completa|haz(?:lo)?|build|empieza|start|ejecuta(?:r|lo)?|execute|run|proceed|procede|"
    r"aplica(?:r|lo)?|realiza(?:r|lo)?|adelante|keep\s+(?:going|implementing)|lleva\s+a\s+cabo)\b",
    re.I,
)


def strict_execute_request(user_text: str) -> bool:
    text = (user_text or "").strip()
    if not text or "?" in text or len(text) > 400:
        return False
    return bool(_STRICT_EXEC_RE.search(text))
