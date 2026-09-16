"""test_debt.py — persistent debt journal for tests exempted as "pre-existing"
(H5).

``src/project_tests.py::compare_with_baseline`` already tells a NEW failure
(caused by this turn) from a ``pre_existing`` one (failed at the checkpoint
too), and narrows further to ``exempt`` — a pre-existing failure in a test
file not tied by name to the files this turn changed. That machinery is
purely a per-turn comparison: nothing remembers that the SAME test has been
exempt for days. The silhouettes forensic analysis is the concrete case —
``test_real_end_to_end[star]`` (IoU 0.9755 < 0.98) went red on 14-09
(chat ``8ee5881b``), was still "pre-existing" on 15-09 (``9ca3273d``) and
again on 16-09 (``d20e933f``/``782b7d89``): three full days where the
regression was real and simply invisible, because "exempt" reads as
"forgiven" rather than "logged".

This module is the missing memory: a per-project, disk-persisted registry of
every test id seen as ``pre_existing``/``exempt``, how many turns it has been
seen for, and whether the user has explicitly dismissed it with a reason.
Once a test has been seen for ``agent_test_debt_turns`` turns (default 3) it
is :func:`overdue` and :func:`todo_items` surfaces it as a high-priority,
non-silent todo — merged into the SAME `todowrite` list the project already
persists (``src/agent_tools/coding_tools.py::save_project_todos`` /
``load_project_todos``), so it rides the existing Progress panel instead of
inventing a second one. ``H45_wiring.md`` has the exact diff to call
:func:`record` from the agent loop after ``project_tests.run_for_turn`` and
to merge :func:`todo_items` into the project's continue-turn working set; it
is not applied here (out of this lot's file ownership).

Stdlib only; never raises on a best-effort path (record/overdue/todo_items
degrade to "nothing recorded" rather than breaking a turn); the two calls
that mutate state on the caller's explicit request (`record`, `dismiss`) can
raise on a real I/O failure, same posture as
``coding_tools.save_project_todos``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from src.constants import DATA_DIR

logger = logging.getLogger(__name__)

_DEBT_DIR = os.path.join(DATA_DIR, "test_debt")

DEFAULT_TURNS = 3
_RESULT_KEYS = ("pre_existing", "exempt")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _project_hash(project_id: str) -> str:
    """A filesystem-safe, fixed-length name for `project_id` — the same
    purpose `coding_tools._safe_session_id` serves for todo files, done with
    a hash instead of a sanitized slug (CONTRATO.md's own naming:
    `DATA_DIR/test_debt/<project_hash>.json`) so an unusual project id (very
    long, or containing characters a filesystem dislikes) can never produce
    a bad path or collide after sanitization the way two different ids could
    if they were merely stripped of "unsafe" characters."""
    pid = str(project_id or "").strip() or "unknown"
    return hashlib.sha256(pid.encode("utf-8")).hexdigest()[:24]


def _registry_path(project_id: str) -> str:
    return os.path.join(_DEBT_DIR, f"{_project_hash(project_id)}.json")


def _failure_id(item: str) -> str:
    """`tests/test_a.py::test_x — AssertionError` -> `tests/test_a.py::test_x`.
    Mirrors `project_tests._failure_id` exactly (kept independent — this
    module must not import agent-loop-owned files per CONTRATO.md rule 2),
    so a nodeid recorded by one matches a nodeid looked up by the other."""
    return (item or "").split(" — ", 1)[0].strip()


def _load(project_id: str) -> Dict[str, Any]:
    path = _registry_path(project_id)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, TypeError):
        return {"project_id": str(project_id or ""), "tests": {}}
    if not isinstance(data, dict):
        return {"project_id": str(project_id or ""), "tests": {}}
    tests = data.get("tests")
    if not isinstance(tests, dict):
        tests = {}
    return {"project_id": str(data.get("project_id") or project_id or ""), "tests": tests}


def _save(project_id: str, data: Dict[str, Any]) -> None:
    os.makedirs(_DEBT_DIR, exist_ok=True)
    path = _registry_path(project_id)
    tmp = path + ".tmp"
    payload = {
        "project_id": str(project_id or ""),
        "tests": data.get("tests") or {},
        "updated_at": _now_iso(),
    }
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _turns_setting(turns: Optional[int]) -> int:
    if turns is not None:
        try:
            return max(1, int(turns))
        except (TypeError, ValueError):
            return DEFAULT_TURNS
    try:
        from src.settings import get_setting
        return max(1, int(get_setting("agent_test_debt_turns", DEFAULT_TURNS) or DEFAULT_TURNS))
    except Exception:
        return DEFAULT_TURNS


def _enabled() -> bool:
    try:
        from src.settings import get_setting
        return bool(get_setting("agent_test_debt", True))
    except Exception:
        return True


def _extract_ids(tests_result: Optional[Dict[str, Any]]) -> List[str]:
    """Every test id this turn's run reported as pre_existing/exempt, in a
    stable order (exempt first — it is the narrower, higher-signal set — then
    any pre_existing id not already included)."""
    if not isinstance(tests_result, dict):
        return []
    out: List[str] = []
    seen = set()
    for key in ("exempt", "pre_existing"):
        for item in tests_result.get(key) or []:
            fid = _failure_id(str(item))
            if fid and fid not in seen:
                seen.add(fid)
                out.append(fid)
    return out


@dataclass
class DebtEntry:
    test_id: str
    first_seen: str
    last_seen: str
    turns_seen: int
    last_turn: Optional[str]
    dismissed: bool
    dismiss_reason: Optional[str]
    dismissed_at: Optional[str]

    @classmethod
    def from_dict(cls, test_id: str, d: Dict[str, Any]) -> "DebtEntry":
        return cls(
            test_id=test_id,
            first_seen=str(d.get("first_seen") or ""),
            last_seen=str(d.get("last_seen") or ""),
            turns_seen=int(d.get("turns_seen") or 0),
            last_turn=d.get("last_turn"),
            dismissed=bool(d.get("dismissed")),
            dismiss_reason=d.get("dismiss_reason"),
            dismissed_at=d.get("dismissed_at"),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "test_id": self.test_id, "first_seen": self.first_seen, "last_seen": self.last_seen,
            "turns_seen": self.turns_seen, "last_turn": self.last_turn,
            "dismissed": self.dismissed, "dismiss_reason": self.dismiss_reason,
            "dismissed_at": self.dismissed_at,
        }


def record(project_id: str, tests_result: Optional[Dict[str, Any]], *,
           turn: Optional[Any] = None) -> List[Dict[str, Any]]:
    """Fold this turn's pre_existing/exempt test ids into the project's debt
    registry and persist it. Returns the full, current list of entries
    (dicts, see :class:`DebtEntry`).

    Idempotent per turn: passing the same `turn` value twice in a row for a
    test id that was already recorded under it does not double-count —
    `turns_seen` only advances when `turn` changes (or is omitted, in which
    case every call counts, matching a caller that calls this exactly once
    per turn as CONTRATO.md's wiring point (b) does).

    A test id NOT present in this turn's result is dropped from the registry
    — it stopped failing (fixed) or stopped being exempt (now a real new
    failure project_tests already charges a fix round for), and debt
    tracking has nothing further to say about it. A `dismissed` entry is an
    exception: it is kept (with its reason) even once the test id is no
    longer reported, so a test someone explicitly accepted does not silently
    reappear as fresh debt if it starts failing again under the exact same
    id — that re-adds a NEW entry instead (turns_seen restarts at 1), which
    is deliberate: a test that comes back after being fixed is worth
    noticing again, not auto-forgiven by an old dismissal.
    """
    if project_id is None or not str(project_id).strip():
        return []
    if not _enabled():
        return list(_load(project_id).get("tests", {}).values())
    ids = _extract_ids(tests_result)
    data = _load(project_id)
    tests: Dict[str, Any] = dict(data.get("tests") or {})
    now = _now_iso()
    turn_key = None if turn is None else str(turn)
    seen_ids = set(ids)

    for test_id in ids:
        raw = tests.get(test_id)
        if raw is None:
            entry = DebtEntry(test_id=test_id, first_seen=now, last_seen=now,
                               turns_seen=1, last_turn=turn_key, dismissed=False,
                               dismiss_reason=None, dismissed_at=None)
        else:
            entry = DebtEntry.from_dict(test_id, raw)
            entry.last_seen = now
            if turn_key is None or entry.last_turn != turn_key:
                entry.turns_seen += 1
                entry.last_turn = turn_key
        tests[test_id] = entry.to_dict()

    # Drop everything this turn's result no longer reports, except a
    # dismissed entry (kept as a quiet historical record — see docstring).
    for test_id in list(tests.keys()):
        if test_id in seen_ids:
            continue
        if not bool((tests.get(test_id) or {}).get("dismissed")):
            tests.pop(test_id, None)

    data["tests"] = tests
    _save(project_id, data)
    return list(tests.values())


def all_entries(project_id: str) -> List[Dict[str, Any]]:
    return list(_load(project_id).get("tests", {}).values())


def overdue(project_id: str, *, turns: Optional[int] = None) -> List[Dict[str, Any]]:
    """Entries seen for >= `turns` (default: `agent_test_debt_turns`, 3)
    turns and not dismissed, most-seen first."""
    threshold = _turns_setting(turns)
    entries = [
        e for e in all_entries(project_id)
        if not e.get("dismissed") and int(e.get("turns_seen") or 0) >= threshold
    ]
    entries.sort(key=lambda e: int(e.get("turns_seen") or 0), reverse=True)
    return entries


def dismiss(project_id: str, test_id: str, reason: str) -> bool:
    """Mark `test_id` dismissed with `reason`, persistently — it stops
    appearing in `overdue()`/`todo_items()` from now on (unless the same test
    id starts failing again after having been dropped from the registry and
    is recorded fresh, per `record`'s docstring). Returns False when the test
    id is not currently tracked (nothing to dismiss) or `reason` is blank —
    an undismissable debt item without a stated reason defeats the point of
    the journal — True on success."""
    test_id = str(test_id or "").strip()
    reason = str(reason or "").strip()
    if not project_id or not test_id or not reason:
        return False
    data = _load(project_id)
    tests: Dict[str, Any] = dict(data.get("tests") or {})
    raw = tests.get(test_id)
    if raw is None:
        return False
    entry = DebtEntry.from_dict(test_id, raw)
    entry.dismissed = True
    entry.dismiss_reason = reason
    entry.dismissed_at = _now_iso()
    tests[test_id] = entry.to_dict()
    data["tests"] = tests
    _save(project_id, data)
    return True


_TODO_ID_PREFIX = "test_debt:"


def _todo_text(entry: Dict[str, Any], threshold: int) -> Dict[str, str]:
    test_id = str(entry.get("test_id") or "")
    turns = int(entry.get("turns_seen") or 0)
    return {
        "es": (f"Deuda de tests: {test_id} lleva {turns} turnos exento "
               f"(umbral {threshold}) — arréglalo o descártalo con un motivo (dismiss)."),
        "en": (f"Test debt: {test_id} has been exempt for {turns} turns "
               f"(threshold {threshold}) — fix it or dismiss it with a reason."),
    }


def todo_items(project_id: str, *, turns: Optional[int] = None, language: str = "en") -> List[Dict[str, Any]]:
    """High-priority `todowrite`-shaped items for every overdue, undismissed
    test — ``[{"id", "content", "status": "pending", "priority": "high"}]``.

    Stable `id` (`test_debt:<test_id>`) and `content` (deterministic per
    test id + turns_seen) make this idempotent across calls within the same
    turn count: merging it into an existing todo list twice — or once per
    turn while `turns_seen` has not changed — produces the same item, not a
    duplicate. `language` picks which of the bilingual pair goes into
    `content`; pass "es"/"en" or leave the default and let the caller
    re-render from `content_es`/`content_en` if it wants both.
    """
    threshold = _turns_setting(turns)
    lang = "es" if str(language or "").strip().lower().startswith("es") else "en"
    out: List[Dict[str, Any]] = []
    for entry in overdue(project_id, turns=threshold):
        texts = _todo_text(entry, threshold)
        out.append({
            "id": _TODO_ID_PREFIX + str(entry.get("test_id") or ""),
            "content": texts[lang],
            "content_es": texts["es"],
            "content_en": texts["en"],
            "status": "pending",
            "priority": "high",
            "source": "test_debt",
            "test_id": entry.get("test_id"),
            "turns_seen": entry.get("turns_seen"),
        })
    return out


def merge_todo_items(existing: Iterable[Dict[str, Any]], debt_items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Merge `debt_items` (from :func:`todo_items`) into an already-loaded
    todo list without duplicating an item the previous turn already added —
    matched by the stable `id` first, falling back to exact `content` for a
    todo list that predates the `id` field (e.g. one persisted by
    `coding_tools.TodoWriteTool`, whose normalized shape has no `id`)."""
    existing_list = [dict(t) for t in existing if isinstance(t, dict)]
    have_ids = {t.get("id") for t in existing_list if t.get("id")}
    have_content = {str(t.get("content") or "").strip() for t in existing_list}
    merged = list(existing_list)
    for item in debt_items:
        if not isinstance(item, dict):
            continue
        item_id = item.get("id")
        content = str(item.get("content") or "").strip()
        if item_id and item_id in have_ids:
            continue
        if content and content in have_content:
            continue
        merged.append(dict(item))
        if item_id:
            have_ids.add(item_id)
        if content:
            have_content.add(content)
    return merged
