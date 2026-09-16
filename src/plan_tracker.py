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

# ---------------------------------------------------------------------------
# Attachment detection
# ---------------------------------------------------------------------------

# Mirrors the marker `build_user_content` writes and
# `src.agent_harness._ATTACHMENT_TITLE_RE`/`_INLINED_ATTACHMENT_RE` already
# parse for the same text — kept local (not imported) so this module has no
# import-time dependency on agent_harness; `replace_attachment` imports
# `user_authored_text` from it lazily instead, at call time only.
_MARKER_RE = re.compile(r"=== (File|ZIP archive): (.+?) ===\n?")

_HEADING_RE = re.compile(r"^(#{1,4})\s+(.+?)\s*$", re.M)
_NUM_LIST_RE = re.compile(r"^\s{0,3}(\d{1,3})[.)]\s+(.+?)\s*$", re.M)
_CHECKBOX_RE = re.compile(r"^\s{0,3}-\s*\[( |x|X)\]\s*(.+?)\s*$", re.M)

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


def find_plan_attachment(text: str, min_chars: Optional[int] = None) -> Optional[Tuple[str, str]]:
    """First inlined attachment in a user message that looks like a plan:
    body >= min_chars AND >= 3 detectable tasks. None otherwise. Never raises."""
    try:
        cap = int(min_chars) if min_chars is not None else _default_min_chars()
    except Exception:
        cap = 3000
    try:
        for _kind, title, body in _iter_attachments(text or ""):
            if len(_normalize(body)) < cap:
                continue
            spec = parse_plan(title, body)
            if len(spec.tasks) >= 3:
                return title, body
        return None
    except Exception:
        return None


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


def _split_sections(text: str) -> List[Tuple[Optional[str], str, str]]:
    """(key_hint, section_title, section_body) by markdown headings first,
    then first-level numbered lists, then checkboxes. Empty when the text
    has none of those (unstructured attachment -> 0 tasks)."""
    for pattern in (_HEADING_RE, _NUM_LIST_RE, _CHECKBOX_RE):
        marks = list(pattern.finditer(text))
        if not marks:
            continue
        out: List[Tuple[Optional[str], str, str]] = []
        for i, m in enumerate(marks):
            start = m.end()
            end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
            title = m.group(2).strip()
            out.append((None, title, text[start:end]))
        return out
    return []


def parse_plan(title: str, body: str) -> PlanSpec:
    """Never raises: unstructured text parses to a PlanSpec with 0 tasks."""
    norm = _normalize(body)
    try:
        h = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]
    except Exception:
        h = hashlib.sha256(b"").hexdigest()[:16]
    plan_title = (title or "").strip() or "untitled plan"
    try:
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
    if existing:
        existing["seen_count"] = int(existing.get("seen_count", 1)) + 1
        existing["last_seen_at"] = now
        save(scope, existing)
        return existing
    tasks = [asdict(t) for t in spec.tasks]
    state = {
        t["id"]: {"status": "pending", "evidence": "", "updated_at": now, "turn": None}
        for t in tasks
    }
    tracker: Dict[str, Any] = {
        "hash": spec.hash,
        "title": spec.title,
        "source_title": title,
        "created_at": now,
        "last_seen_at": now,
        "seen_count": 1,
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
) -> Optional[Dict[str, Any]]:
    tracker = load(scope, hash_)
    if not tracker:
        return None
    t = _task_by_id_or_key(tracker, task_id)
    if not t:
        return None
    tracker.setdefault("state", {})[t["id"]] = {
        "status": status,
        "evidence": evidence or "",
        "updated_at": time.time(),
        "turn": turn,
    }
    tracker["last_seen_at"] = time.time()
    save(scope, tracker)
    return tracker


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


def replace_attachment(text: str, tracker: Dict[str, Any], max_task_chars: int = 6000) -> str:
    """`user_authored_text(text)` + brief + only the current task's text —
    the inlined attachment body (which may be 172 KB) never reaches this
    output."""
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
    r"keep\s+(?:going|implementing)|next task|siguiente"
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
