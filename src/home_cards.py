"""src/home_cards.py — which automations a user pinned to Home, and what
each card shows: the task, its last successful result, when it ran and
when it runs next.

The pin list is a DATA_DIR sidecar (`home_cards.json`, owner → ordered
task ids, the house pattern of `connector_sidecar.py`); the content is
read live from `ScheduledTask` + `TaskRun`, so nothing is duplicated.
A card is just a pinned task: "Run now" is the task's run, removing the
card does not delete the task.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
MAX_CARDS = 24


def _file() -> str:
    from src.constants import DATA_DIR
    return os.path.join(DATA_DIR, "home_cards.json")


def _load() -> Dict[str, Any]:
    try:
        with open(_file(), encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data: Dict[str, Any]) -> None:
    try:
        from core.atomic_io import atomic_write_json
        atomic_write_json(_file(), data)
    except Exception:  # noqa: BLE001
        os.makedirs(os.path.dirname(_file()), exist_ok=True)
        with open(_file(), "w", encoding="utf-8") as fh:
            json.dump(data, fh)


def _key(owner: Optional[str]) -> str:
    return owner or "_"


def pinned(owner: Optional[str]) -> List[str]:
    return list((_load().get(_key(owner)) or {}).get("cards") or [])


def pin(owner: Optional[str], task_id: str, *, position: Optional[int] = None) -> List[str]:
    with _LOCK:
        data = _load()
        entry = data.setdefault(_key(owner), {"cards": []})
        cards = [c for c in (entry.get("cards") or []) if c != task_id]
        if position is None or position >= len(cards):
            cards.append(task_id)
        else:
            cards.insert(max(0, position), task_id)
        entry["cards"] = cards[:MAX_CARDS]
        _save(data)
        return list(entry["cards"])


def unpin(owner: Optional[str], task_id: str) -> List[str]:
    with _LOCK:
        data = _load()
        entry = data.setdefault(_key(owner), {"cards": []})
        entry["cards"] = [c for c in (entry.get("cards") or []) if c != task_id]
        _save(data)
        return list(entry["cards"])


def reorder(owner: Optional[str], order: List[str]) -> List[str]:
    with _LOCK:
        data = _load()
        entry = data.setdefault(_key(owner), {"cards": []})
        current = list(entry.get("cards") or [])
        kept = [c for c in order if c in current] + [c for c in current if c not in order]
        entry["cards"] = kept[:MAX_CARDS]
        _save(data)
        return list(entry["cards"])


def is_pinned(owner: Optional[str], task_id: str) -> bool:
    return task_id in pinned(owner)


# ---------------------------------------------------------------------------
# card content
# ---------------------------------------------------------------------------

def _iso(dt) -> Optional[str]:
    if dt is None:
        return None
    try:
        s = dt.isoformat()
        return s if s.endswith("Z") or "+" in s else s + "Z"
    except Exception:  # noqa: BLE001
        return str(dt)


def cards(owner: Optional[str]) -> List[Dict[str, Any]]:
    """The pinned tasks with their latest result, in the user's order.
    A pinned id whose task no longer exists is dropped silently."""
    ids = pinned(owner)
    if not ids:
        return []
    from core.database import ScheduledTask, SessionLocal, TaskRun
    out: List[Dict[str, Any]] = []
    db = SessionLocal()
    try:
        tasks = {t.id: t for t in db.query(ScheduledTask).filter(ScheduledTask.id.in_(ids)).all()}
        for tid in ids:
            t = tasks.get(tid)
            if t is None or (owner and t.owner and t.owner != owner):
                continue
            runs = (db.query(TaskRun).filter(TaskRun.task_id == tid)
                    .order_by(TaskRun.started_at.desc()).limit(8).all())
            last_ok = next((r for r in runs if r.status == "success" and r.result), None)
            last = runs[0] if runs else None
            out.append({
                "task_id": tid, "name": t.name, "task_type": t.task_type, "action": t.action,
                "schedule": t.schedule, "scheduled_time": t.scheduled_time, "timezone": t.timezone,
                "cron_expression": t.cron_expression, "status": t.status,
                "next_run": _iso(t.next_run), "last_run": _iso(t.last_run),
                "result": (last_ok.result if last_ok else None),
                "result_at": _iso(last_ok.finished_at or last_ok.started_at) if last_ok else None,
                "result_model": getattr(last_ok, "model", None) if last_ok else None,
                "last_status": last.status if last else None,
                "last_error": (last.error if last and last.status == "error" else None),
                "running": bool(last and last.status == "running"),
            })
    finally:
        db.close()
    return out
