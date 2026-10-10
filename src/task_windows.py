"""Run windows, period keys and closed run statuses for scheduled tasks (#263).

Pure logic, no database and no clock of its own: every function takes the
instants it needs, so the scheduler (and the tests, with a fake clock) decide
what "now" is.

Three optional guards sit on top of the existing scheduler. A task that
declares none of them behaves exactly as it always did.

* **window** (`window_minutes`): how many minutes after its scheduled moment a
  run is still welcome. A catch-up that reaches the task later than that —
  the PC was off over the slot, the foreground kept postponing it — is not run
  late; it is recorded as `skipped-window` and the schedule moves on.
* **period key** (`period_key` in `day` | `iso_week` | `month`): the task runs
  at most once per calendar period of its own time zone. A second or late
  firing inside a period that already ran is recorded as `skipped-period`.
* **pause**: a paused task never runs; when something tries (a manual run, a
  firing the scheduler records for a task that opted into the guards) the
  attempt is recorded as `skipped-paused`.

Every run row carries one of the *closed* statuses below, with a short reason.
Rows written before this module keep their historical `status` word
(`success`, `error`, ...); `closed_status_for` maps those on the way out, so
nothing stored has to be rewritten.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional, Tuple

PERIOD_KEYS = ("day", "iso_week", "month")

#: Longest window accepted: thirty days, in minutes.
MAX_WINDOW_MINUTES = 30 * 24 * 60

OK = "ok"
PARTIAL = "partial"
FAILED = "failed"
SKIPPED_WINDOW = "skipped-window"
SKIPPED_PERIOD = "skipped-period"
SKIPPED_PAUSED = "skipped-paused"
BLOCKED_LOGIN = "blocked-login"

#: The closed vocabulary a run can end in. Nothing else is a final answer.
CLOSED_STATUSES = (OK, PARTIAL, FAILED, SKIPPED_WINDOW, SKIPPED_PERIOD,
                   SKIPPED_PAUSED, BLOCKED_LOGIN)

#: Statuses that use up the period: the occurrence ran (well or badly) and a
#: second firing in the same period must do nothing. A run blocked at the
#: login did not really run, so it leaves the period open for a retry.
PERIOD_CONSUMING = (OK, PARTIAL, FAILED)

_LOGIN_RE = re.compile(
    r"(authentication (failed|error|required)|auth(entication)? failure|login (failed|required|error)|"
    r"log ?in (failed|required)|sign[- ]?in (failed|required)|invalid (credentials|password|token)|"
    r"bad credentials|wrong password|unauthori[sz]ed|\b401\b|app[- ]specific password|"
    r"session (expired|has expired)|not (logged|signed) in|please (log|sign) ?in|"
    r"credentials? (rejected|expired|missing)|AUTHENTICATIONFAILED)",
    re.IGNORECASE,
)


def normalize_window_minutes(value) -> Optional[int]:
    """`None`/`""`/`0` mean "no window". Anything else must be an integer in
    1..MAX_WINDOW_MINUTES, or this raises ValueError."""
    if value is None or value == "" or value is False:
        return None
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        raise ValueError("window_minutes must be a whole number of minutes")
    if minutes == 0:
        return None
    if minutes < 0 or minutes > MAX_WINDOW_MINUTES:
        raise ValueError(f"window_minutes must be between 1 and {MAX_WINDOW_MINUTES}")
    return minutes


def normalize_period_key(value) -> Optional[str]:
    """`None`/`""` mean "no period limit"; otherwise one of PERIOD_KEYS."""
    if value is None or value == "":
        return None
    key = str(value).strip().lower()
    if key not in PERIOD_KEYS:
        raise ValueError(f"period_key must be one of {PERIOD_KEYS}")
    return key


def _to_local(anchor_utc_naive: datetime, tz_name: Optional[str]) -> datetime:
    aware = anchor_utc_naive.replace(tzinfo=timezone.utc)
    if tz_name:
        try:
            from zoneinfo import ZoneInfo
            return aware.astimezone(ZoneInfo(tz_name))
        except Exception:
            pass
    return aware


def period_value(anchor_utc_naive: datetime, tz_name: Optional[str], key: str) -> str:
    """The calendar period an occurrence belongs to, in the task's own time
    zone (UTC for a legacy task that has none): `2026-10-10`, `2026-W41` or
    `2026-10`."""
    local = _to_local(anchor_utc_naive, tz_name)
    if key == "day":
        return local.strftime("%Y-%m-%d")
    if key == "iso_week":
        year, week, _ = local.isocalendar()
        return f"{year}-W{week:02d}"
    if key == "month":
        return local.strftime("%Y-%m")
    raise ValueError(f"period_key must be one of {PERIOD_KEYS}")


def minutes_late(anchor_utc_naive: datetime, now_utc_naive: datetime) -> float:
    return (now_utc_naive - anchor_utc_naive).total_seconds() / 60.0


def window_violation(anchor_utc_naive: datetime, now_utc_naive: datetime,
                     window_minutes: Optional[int]) -> Optional[str]:
    """`None` when a run at `now` is inside the window (or there is none);
    otherwise the short reason the run is skipped."""
    if not window_minutes:
        return None
    late = minutes_late(anchor_utc_naive, now_utc_naive)
    if late <= window_minutes:
        return None
    stamp = anchor_utc_naive.replace(microsecond=0).isoformat(sep=" ")
    return f"Due {stamp} UTC; reached {int(late)} min late, the window is {window_minutes} min"


def guard_anchor(origin: Optional[datetime], due_at: datetime) -> datetime:
    """The instant an occurrence was FIRST due. A postponement (the foreground
    was busy, a quiet window, the startup sweep) moves `next_run` but must not
    move this, or a task could be pushed past its window forever."""
    if origin is not None and origin <= due_at:
        return origin
    return due_at


def closed_status_for(status: Optional[str], result: Optional[str] = None,
                      error: Optional[str] = None) -> Tuple[Optional[str], str]:
    """`(closed_status, reason)` for a stored run row.

    `closed_status` is `None` while the run is still open (`queued`,
    `running`) or for a word this module does not know; the reason is always a
    short single line.
    """
    s = str(status or "").strip().lower()
    text = (error or result or "").strip()
    line = _first_line(text)
    if s in CLOSED_STATUSES and s.startswith("skipped-"):
        return s, line
    if s in (PARTIAL, BLOCKED_LOGIN, FAILED, OK):
        return s, line
    if s == "success":
        if error and str(error).strip():
            return PARTIAL, _first_line(error)
        return OK, _first_line(result or "") or "completed"
    if s in ("error", "failed", "failure"):
        if _LOGIN_RE.search(text):
            return BLOCKED_LOGIN, line
        return FAILED, line or "failed"
    if s == "aborted":
        return FAILED, line or "aborted"
    if s == "fenced":
        return SKIPPED_PERIOD, line or "duplicate occurrence"
    if s == "skipped":
        if "paused" in text.lower():
            return SKIPPED_PAUSED, line
        return OK, line or "nothing to do"
    return None, line


def _first_line(text: str, limit: int = 160) -> str:
    for raw in str(text or "").splitlines():
        raw = raw.strip()
        if raw:
            return raw if len(raw) <= limit else raw[: limit - 1].rstrip() + "…"
    return ""
