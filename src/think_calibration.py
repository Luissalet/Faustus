"""How well Auto's reasoning-mode decisions held up (OBJ-58, #228).

Every assistant turn records the mode it really ran with in
``metadata.think_mode`` (mode, requested, source, reasons, budget, effort; see
``routes/chat_helpers.save_assistant_response``). This module pairs each of
those turns with what the person wrote next and counts, per mode, per source
and per rule reason, how often the next message was a correction ("no, eso
esta mal", "try again"...) or the turn ended without an answer.

Turns inside a daily-battery session (``scripts/daily_eval.py`` writes the
session of every task to ``logs/daily_eval/*.json``) also get the battery's
verdict, so the same buckets show how often each mode passed its checks.

It is a read-only report to tune the thresholds by hand: nothing here changes
a decision. A turn with no next message yet is counted as a turn but not as
followed, so it never inflates or deflates a rate.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional

from src.think_mode import is_correction

DEFAULT_DAYS = 30
MAX_DAYS = 365
MAX_ROWS = 50000
# A bucket needs this many followed turns before it can be flagged.
MIN_FOLLOWED = 8
_TEXT_CHARS = 2000


def _metadata(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _record(think: Mapping[str, Any], content: str, session: Any = None) -> Dict[str, Any]:
    reasons = think.get("reasons")
    mode = str(think.get("mode") or "")
    if mode == "think" and think.get("effort") == "low":
        mode = "light"  # a low-effort think is Auto's light mode, as the next turn reads it
    return {
        "mode": mode,
        "source": str(think.get("source") or "unknown"),
        "reasons": [str(r) for r in reasons][:12] if isinstance(reasons, list) else [],
        "empty": not (content or "").strip(),
        "corrected": None,
        "session": session,
    }


def outcomes(rows: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """One record per assistant turn that carries ``think_mode``.

    ``rows`` are chat messages (``session_id``, ``role``, ``content``,
    ``metadata``) ordered by session and time. ``corrected`` is True/False
    when the person wrote again in that session, None otherwise.
    """
    out: List[Dict[str, Any]] = []
    pending: Optional[Dict[str, Any]] = None
    session = object()
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        sid = row.get("session_id")
        if sid != session:
            if pending is not None:
                out.append(pending)
            pending, session = None, sid
        role = row.get("role")
        content = row.get("content") if isinstance(row.get("content"), str) else ""
        if role == "assistant":
            think = _metadata(row.get("metadata")).get("think_mode")
            if isinstance(think, Mapping) and think.get("mode"):
                if pending is not None:
                    out.append(pending)
                pending = _record(think, content, sid)
        elif role == "user" and pending is not None:
            pending["corrected"] = is_correction(content[:_TEXT_CHARS])
            out.append(pending)
            pending = None
    if pending is not None:
        out.append(pending)
    return out


def _bucket() -> Dict[str, Any]:
    return {"turns": 0, "followed": 0, "corrected": 0, "empty": 0}


def _add(bucket: Dict[str, Any], rec: Mapping[str, Any]) -> None:
    bucket["turns"] += 1
    bucket["empty"] += 1 if rec.get("empty") else 0
    if rec.get("corrected") is not None:
        bucket["followed"] += 1
        bucket["corrected"] += 1 if rec.get("corrected") else 0


def _finish(bucket: Dict[str, Any]) -> Dict[str, Any]:
    followed = bucket["followed"]
    bucket["correction_rate"] = round(bucket["corrected"] / followed, 3) if followed else None
    bucket["empty_rate"] = round(bucket["empty"] / bucket["turns"], 3) if bucket["turns"] else None
    return bucket


def summarize(records: List[Mapping[str, Any]], *, min_followed: int = MIN_FOLLOWED) -> Dict[str, Any]:
    """Counts and rates per mode, source and reason, plus the buckets worth a look."""
    total = _bucket()
    rule = _bucket()
    by_mode: Dict[str, Dict[str, Any]] = {}
    by_source: Dict[str, Dict[str, Any]] = {}
    by_reason: Dict[str, Dict[str, Any]] = {}
    for rec in records:
        _add(total, rec)
        _add(by_mode.setdefault(rec.get("mode") or "?", _bucket()), rec)
        _add(by_source.setdefault(rec.get("source") or "unknown", _bucket()), rec)
        if rec.get("source") == "rule":
            _add(rule, rec)
            for reason in rec.get("reasons") or ["(none)"]:
                _add(by_reason.setdefault(reason, _bucket()), rec)
    for group in (by_mode, by_source, by_reason):
        for bucket in group.values():
            _finish(bucket)
    _finish(total)
    _finish(rule)

    baseline = rule["correction_rate"]
    flags: List[Dict[str, Any]] = []
    if baseline is not None:
        for kind, group in (("reason", by_reason), ("mode", by_mode)):
            for key, bucket in group.items():
                rate = bucket["correction_rate"]
                if bucket["followed"] < min_followed or rate is None:
                    continue
                if rate >= max(2 * baseline, baseline + 0.15):
                    flags.append({
                        "kind": kind, "key": key, "followed": bucket["followed"],
                        "correction_rate": rate, "baseline": baseline,
                        "hint": (f"Turns under {kind} '{key}' were corrected {rate:.0%} of the time against "
                                 f"{baseline:.0%} for all Auto turns: that {kind} may need a higher mode."),
                    })
    flags.sort(key=lambda f: (-f["correction_rate"], -f["followed"]))
    return {
        "turns": total, "auto": rule, "by_mode": by_mode, "by_source": by_source,
        "by_reason": dict(sorted(by_reason.items(), key=lambda kv: -kv[1]["turns"])),
        "flags": flags, "min_followed": min_followed,
    }


def battery_verdicts(days: int = DEFAULT_DAYS, directory: Optional[str] = None) -> Dict[str, bool]:
    """session id -> whether its daily-battery task passed, from recent reports."""
    root = directory or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", "daily_eval")
    cutoff = time.time() - days * 86400
    out: Dict[str, bool] = {}
    try:
        names = sorted(n for n in os.listdir(root) if n.endswith(".json"))
    except OSError:
        return out
    for name in names:
        path = os.path.join(root, name)
        try:
            if os.path.getmtime(path) < cutoff:
                continue
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        for task in data.get("tasks") or [] if isinstance(data, dict) else []:
            if isinstance(task, dict) and task.get("session"):
                out[str(task["session"])] = bool(task.get("ok"))
    return out


def battery(records: List[Mapping[str, Any]], verdicts: Mapping[str, bool]) -> Dict[str, Any]:
    """Pass rate of the battery turns per mode and per rule reason."""
    def bucket() -> Dict[str, Any]:
        return {"turns": 0, "passed": 0}
    total, by_mode, by_reason = bucket(), {}, {}
    for rec in records:
        verdict = verdicts.get(str(rec.get("session")))
        if verdict is None:
            continue
        targets = [total, by_mode.setdefault(rec.get("mode") or "?", bucket())]
        if rec.get("source") == "rule":
            targets += [by_reason.setdefault(r, bucket()) for r in rec.get("reasons") or ["(none)"]]
        for b in targets:
            b["turns"] += 1
            b["passed"] += 1 if verdict else 0
    for b in [total, *by_mode.values(), *by_reason.values()]:
        b["pass_rate"] = round(b["passed"] / b["turns"], 3) if b["turns"] else None
    return {"turns": total, "by_mode": by_mode, "by_reason": by_reason}


def _clamp_days(days: Any) -> int:
    try:
        value = int(days)
    except (TypeError, ValueError):
        value = DEFAULT_DAYS
    return max(1, min(MAX_DAYS, value))


def report(owner: Optional[str], days: Any = DEFAULT_DAYS, *, db=None,
           battery_dir: Optional[str] = None) -> Dict[str, Any]:
    """The calibration report for one owner's sessions over the last ``days``."""
    from sqlalchemy import func

    from core.database import ChatMessage, Session, SessionLocal

    days = _clamp_days(days)
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    own = db is None
    db = db or SessionLocal()
    try:
        query = (db.query(ChatMessage.session_id, ChatMessage.role,
                          func.substr(ChatMessage.content, 1, _TEXT_CHARS), ChatMessage.meta_data)
                 .join(Session, Session.id == ChatMessage.session_id)
                 .filter(ChatMessage.timestamp >= since)
                 .filter(ChatMessage.role.in_(("user", "assistant"))))
        if owner:
            query = query.filter(Session.owner == owner)
        rows = query.order_by(ChatMessage.session_id, ChatMessage.timestamp, ChatMessage.id).limit(MAX_ROWS).all()
    finally:
        if own:
            db.close()
    records = outcomes({"session_id": r[0], "role": r[1], "content": r[2] or "", "metadata": r[3]} for r in rows)
    out = summarize(records)
    out["battery"] = battery(records, battery_verdicts(days, battery_dir))
    out.update({"days": days, "since": since.isoformat(timespec="seconds"), "rows_read": len(rows),
                "truncated": len(rows) >= MAX_ROWS})
    return out


def session_owners(*, db=None) -> List[str]:
    """The distinct non-empty session owners (to tell a one-user install apart)."""
    from core.database import Session, SessionLocal

    own = db is None
    db = db or SessionLocal()
    try:
        rows = db.query(Session.owner).filter(Session.owner.isnot(None)).distinct().limit(3).all()
    finally:
        if own:
            db.close()
    return sorted({r[0] for r in rows if r[0]})
