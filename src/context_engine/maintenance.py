"""
context_engine/maintenance.py — §13, and everything §13 refuses to do.

Background consolidation is where a memory system quietly becomes untrustworthy.
The temptation is obvious: the machine is idle, there is a model on the box,
and a nightly pass could promote the rules that "look" proven, merge the
findings that "look" duplicated and resolve the contradictions that "look"
settled.  The plan is unusually blunt about it: *"Una tarea nocturna no puede
promover por sí sola una regla a `proven`, eliminar evidencia o resolver
contradicciones materiales."*

So every task in :data:`TASK_NAMES` is deterministic and reversible-by-rebuild:

* `prune_packets`    — drops ledger rows older than a setting.  The ledger is
                       derived; the packets it describes were never stored.
* `expire_findings`  — asks the blackboard to apply its own TTL, which *marks*
                       and never deletes.
* `refresh_code_index` — re-reads files whose hash moved.  The files are the
                       truth; the index is a cache of their shape.
* `degrade_experiences` — marks experiences whose files are gone as stale.  Not
                       a delete: an experience whose file was rewritten is still
                       the record of how a problem was approached, and what it
                       has stopped being is a description of the repository.
* `audit_blocks`     — reports oversized, duplicated and contradictory blocks.
                       Reports.  Changes nothing.
* `vacuum`           — reclaims disk after the deletes above.

Nothing here calls a model, and nothing here writes a fact.  The model-assisted
half of §13.2 (proposing experiences, summarising episodes, describing recipes)
belongs to whatever service owns that judgement; its output arrives as a
*candidate*, through the same admission rules as anything else.

Two operational promises, because a maintenance pass that breaks either one
gets disabled by the first person it inconveniences and then never runs again:

**It gives the machine back.**  `budget_s` is a wall-clock ceiling checked
between tasks; when it is spent the remaining tasks come back as rows saying
so, with `ok=True`, because running out of time is a schedule problem and not a
failure.

**It gives way to the user.**  :func:`should_yield` asks `src.agent_runs`
whether any session has a turn in flight, and `run()` stops when one does.
That is the only interactive-work probe this codebase has: `bg_monitor` is a
follow-up scheduler with no notion of "busy", and `bg_jobs` tracks detached
subprocesses rather than turns.  `agent_runs.active_session_ids()` is what the
sidebar's own activity dots are drawn from, which is as close to "the user is
watching" as this process can get.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from . import store

logger = logging.getLogger(__name__)

TASK_NAMES: Tuple[str, ...] = (
    "prune_packets", "expire_findings", "refresh_code_index",
    "degrade_experiences", "audit_blocks", "vacuum",
)

# These tasks describe one project workspace. A scheduled pass fans them out
# over every project instead of recording a misleading global "no workspace"
# success and then sleeping until the next interval.
SCOPED_TASK_NAMES = frozenset({"refresh_code_index", "degrade_experiences"})

#: How often each task is worth running, in seconds.  These are floors, not
#: schedules: :func:`due` says a task *may* run, and something else decides
#: whether now is a good moment.  `vacuum` is last and rarest because it takes
#: an exclusive lock for as long as it takes.
TASK_INTERVALS_S: Dict[str, float] = {
    "prune_packets": 86_400.0,
    "expire_findings": 3_600.0,
    "refresh_code_index": 1_800.0,
    "degrade_experiences": 21_600.0,
    "audit_blocks": 86_400.0,
    "vacuum": 604_800.0,
}

#: Setting that decides how long a ledger row lives.
LEDGER_DAYS_SETTING = "agent_context_ledger_days"
DEFAULT_LEDGER_DAYS = 30

#: How many experience rows one `degrade_experiences` pass will look at.  A
#: sweep that walks a hundred thousand rows on a laptop is a sweep somebody
#: turns off.
MAX_EXPERIENCE_ROWS = 2000

SCHEMA: Tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS context_maintenance (
        name       TEXT PRIMARY KEY,
        ran_at     TEXT NOT NULL DEFAULT '',
        ok         INTEGER NOT NULL DEFAULT 1,
        changed    INTEGER NOT NULL DEFAULT 0,
        detail     TEXT NOT NULL DEFAULT '',
        elapsed_ms INTEGER NOT NULL DEFAULT 0
    )
    """,
)

store.register_schema("maintenance", SCHEMA)

_LOCK = threading.RLock()


@dataclass(frozen=True)
class TaskResult:
    """What one task did.

    `ok=False` is reserved for a task that failed; a task that had nothing to
    do, was skipped for lack of a workspace, or ran out of the time budget is
    `ok=True` with a `detail` that says which.  Conflating the three is how a
    dashboard ends up red because nobody had a project open."""

    name: str
    ok: bool = True
    changed: int = 0
    detail: str = ""
    elapsed_ms: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "changed": self.changed,
                "detail": self.detail, "elapsed_ms": self.elapsed_ms}


# ── yielding to the user ───────────────────────────────────────────────────

def should_yield() -> bool:
    """Is somebody waiting on this machine right now?

    True when any session has a turn in flight.  `run()` checks this between
    tasks and stops; it deliberately does not check it *inside* a task, because
    a half-finished index refresh is worse than one that took four more
    seconds.

    Never raises: a probe that cannot answer says no.  Answering yes on an
    import error would mean maintenance never runs on an install where
    `agent_runs` moved."""
    try:
        from src import agent_runs

        return bool(agent_runs.active_session_ids())
    except Exception:  # noqa: BLE001 - a probe that fails is not a reason to stop
        logger.debug("maintenance could not probe for interactive work")
        return False


# ── the tasks ──────────────────────────────────────────────────────────────

def _ledger_days() -> int:
    try:
        from src.settings import get_setting

        value = int(get_setting(LEDGER_DAYS_SETTING, DEFAULT_LEDGER_DAYS))
    except Exception:  # noqa: BLE001 - an unreadable setting is the default
        return DEFAULT_LEDGER_DAYS
    return value if value > 0 else DEFAULT_LEDGER_DAYS


def _prune_packets(**scope: Any) -> Tuple[int, str]:
    from . import compiler

    days = _ledger_days()
    removed = compiler.prune_packets(days=days)
    return removed, f"dropped {removed} ledger row(s) older than {days} day(s)"


def _expire_findings(**scope: Any) -> Tuple[int, str]:
    from . import shared_memory

    changed = shared_memory.expire()
    return changed, (f"the blackboard TTL touched {changed} finding(s); "
                     f"nothing was deleted")


def _refresh_code_index(*, workspace: str = "", project_id: str = "",
                        **rest: Any) -> Tuple[int, str]:
    if not workspace:
        return 0, "no workspace given; nothing to index"
    from . import code_index

    report = code_index.refresh(workspace, project_id=project_id)
    detail = (f"scanned {report.get('scanned', 0)}, reindexed "
              f"{report.get('reindexed', 0)}, removed {report.get('removed', 0)}"
              f"{'; budget ran out before the walk did' if report.get('truncated') else ''}")
    return int(report.get("reindexed", 0)) + int(report.get("removed", 0)), detail


def _missing_files(workspace: str, project_id: str) -> List[str]:
    """Paths an experience says it touched that are not on disk any more.

    This reads `experiences.touched_symbols` directly because that module
    exposes no enumeration — `search()` answers a ranked question and caps at a
    handful of rows, which is the wrong shape for a sweep.  The read is the
    only thing that happens here: the *write* goes back through
    `experiences.degrade_for_revision`, which owns the rule about what being
    stale means.  A missing table is an install without experiences, which is
    not an error."""
    root = str(workspace or "").strip()
    if not root or not os.path.isdir(root):
        return []
    try:
        with store.db() as conn:
            rows = store.rows(conn.execute(
                "SELECT touched_symbols FROM experiences WHERE project_id = ? "
                "LIMIT ?", (str(project_id or ""), MAX_EXPERIENCE_ROWS)))
    except (store.ContextStoreError, sqlite3.Error) as exc:
        logger.debug("maintenance could not read experiences: %s", exc)
        return []

    missing: List[str] = []
    for row in rows:
        for raw in store.loads_list(row.get("touched_symbols")):
            value = str(raw or "").strip().replace("\\", "/")
            if not value or "/" not in value or value in missing:
                continue
            if os.path.exists(os.path.join(root, value)):
                continue
            missing.append(value)
    return missing


def _degrade_experiences(*, workspace: str = "", project_id: str = "",
                         **rest: Any) -> Tuple[int, str]:
    if not workspace:
        return 0, "no workspace given; a file cannot be shown to be gone"
    gone = _missing_files(workspace, project_id)
    if not gone:
        return 0, "every file an experience names is still on disk"
    from . import experiences

    marked = experiences.degrade_for_revision(project_id, changed_files=gone)
    return marked, (f"{len(gone)} referenced file(s) are gone; marked {marked} "
                    f"experience(s) stale (none deleted)")


def _audit_blocks(*, owner: str = "", project_id: str = "",
                  **rest: Any) -> Tuple[int, str]:
    from . import blocks

    report = blocks.audit(owner=owner, project_id=project_id)
    problems = (len(report.get("oversized") or [])
                + len(report.get("duplicates") or [])
                + len(report.get("contradictions") or [])
                + len(report.get("over_ration") or []))
    # `changed` stays 0 on purpose: this task reports, it does not act.  A
    # number in the "changed" column for a read-only pass would make every
    # audit look like a mutation in the run history.
    return 0, (f"{report.get('blocks', 0)} block(s) audited, {problems} "
               f"problem(s) reported (nothing changed)")


def _vacuum(**scope: Any) -> Tuple[int, str]:
    store.vacuum()
    return 0, "reclaimed free pages"


TASKS: Dict[str, Callable[..., Tuple[int, str]]] = {
    "prune_packets": _prune_packets,
    "expire_findings": _expire_findings,
    "refresh_code_index": _refresh_code_index,
    "degrade_experiences": _degrade_experiences,
    "audit_blocks": _audit_blocks,
    "vacuum": _vacuum,
}


# ── the run record ─────────────────────────────────────────────────────────

def _remember(result: TaskResult, *, now: Optional[datetime] = None) -> None:
    moment = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    stamp = moment.isoformat().replace("+00:00", "Z")
    try:
        with store.db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO context_maintenance "
                "(name, ran_at, ok, changed, detail, elapsed_ms) "
                "VALUES (?,?,?,?,?,?)",
                (result.name, stamp, 1 if result.ok else 0, result.changed,
                 result.detail[:512], result.elapsed_ms))
    except (store.ContextStoreError, sqlite3.Error) as exc:
        logger.debug("maintenance could not record %s: %s", result.name, exc)


def last_run() -> Dict[str, Any]:
    """When each task last ran and what it did.

    A task that has never run is simply absent, which is the honest answer and
    the one :func:`due` needs — a zero timestamp would be indistinguishable
    from a clock that was wrong once."""
    out: Dict[str, Any] = {}
    try:
        with store.db() as conn:
            for row in store.rows(conn.execute("SELECT * FROM context_maintenance")):
                out[str(row.get("name"))] = {
                    "ran_at": str(row.get("ran_at") or ""),
                    "ok": bool(row.get("ok")),
                    "changed": int(row.get("changed") or 0),
                    "detail": str(row.get("detail") or ""),
                    "elapsed_ms": int(row.get("elapsed_ms") or 0),
                }
    except (store.ContextStoreError, sqlite3.Error) as exc:
        logger.warning("maintenance history unreadable: %s", exc)
    return out


def due(*, now: Optional[datetime] = None) -> List[str]:
    """Tasks whose interval has elapsed, in `TASK_NAMES` order.

    Order matters and is not alphabetical: pruning before vacuuming means the
    vacuum reclaims what the prune freed, and refreshing the index before
    degrading experiences means the staleness check sees the current tree."""
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    history = last_run()
    ready: List[str] = []
    for name in TASK_NAMES:
        entry = history.get(name) or {}
        ran_at = store.parse_iso(entry.get("ran_at")) if entry.get("ran_at") else None
        if ran_at is None:
            ready.append(name)
            continue
        interval = TASK_INTERVALS_S.get(name, 3_600.0)
        if (moment - ran_at).total_seconds() >= interval:
            ready.append(name)
    return ready


# ── the pass ───────────────────────────────────────────────────────────────

def run(names: Sequence[str] = (), *, owner: str = "", project_id: str = "",
        workspace: str = "", budget_s: float = 30.0) -> List[TaskResult]:
    """Run the named tasks (or all of them), inside a time budget.

    Returns one row per requested task, in the order `TASK_NAMES` gives, so a
    caller can index the result against what it asked for.  A task that did not
    run because the budget was spent, or because a user started a turn, comes
    back `ok=True` and says which — those are schedule outcomes, not failures.

    Never raises.  A task that throws costs itself and nothing else."""
    wanted = [name for name in TASK_NAMES
              if not names or name in set(str(n) for n in names)]
    unknown = sorted({str(n) for n in (names or ())} - set(TASK_NAMES))
    for name in unknown:
        logger.debug("maintenance ignoring unknown task %r", name)

    try:
        ceiling = float(budget_s)
    except (TypeError, ValueError):
        ceiling = 30.0
    if ceiling <= 0:
        ceiling = 30.0

    scope = {"owner": str(owner or ""), "project_id": str(project_id or ""),
             "workspace": str(workspace or "")}
    started = time.monotonic()
    results: List[TaskResult] = []
    stopped = ""

    with _LOCK:
        for name in wanted:
            if stopped:
                results.append(TaskResult(name=name, ok=True, changed=0,
                                          detail=stopped))
                continue
            spent = time.monotonic() - started
            if spent >= ceiling:
                stopped = (f"skipped: the {ceiling:.0f}s time budget was spent "
                           f"after {len(results)} task(s)")
                results.append(TaskResult(name=name, ok=True, changed=0,
                                          detail=stopped))
                continue
            if should_yield():
                stopped = "skipped: a turn is in flight and maintenance yields"
                results.append(TaskResult(name=name, ok=True, changed=0,
                                          detail=stopped))
                continue

            task = TASKS.get(name)
            if task is None:  # pragma: no cover - TASK_NAMES and TASKS agree
                results.append(TaskResult(name=name, ok=False, changed=0,
                                          detail="no such task"))
                continue
            began = time.monotonic()
            try:
                changed, detail = task(**scope)
                result = TaskResult(name=name, ok=True, changed=int(changed or 0),
                                    detail=str(detail or ""),
                                    elapsed_ms=int((time.monotonic() - began) * 1000))
            except Exception as exc:  # noqa: BLE001 - one task, one failure
                logger.warning("maintenance task %s failed: %s", name, exc,
                               exc_info=True)
                result = TaskResult(name=name, ok=False, changed=0,
                                    detail=f"{type(exc).__name__}: {exc}"[:512],
                                    elapsed_ms=int((time.monotonic() - began) * 1000))
            results.append(result)
            _remember(result)

    logger.info("context maintenance ran %d/%d task(s) in %.1fs",
                sum(1 for r in results if r.elapsed_ms or r.changed),
                len(results), time.monotonic() - started)
    return results


def run_scheduled(*, projects: Sequence[Mapping[str, Any]] = (),
                  budget_s: float = 30.0) -> List[TaskResult]:
    """Run every due task, fanning workspace tasks across all projects."""
    ready = due()
    if not ready:
        return []
    try:
        ceiling = max(1.0, float(budget_s))
    except (TypeError, ValueError):
        ceiling = 30.0
    started = time.monotonic()
    results: List[TaskResult] = []

    scopes: List[Dict[str, str]] = []
    seen = set()
    for raw in projects or ():
        if not isinstance(raw, Mapping):
            continue
        workspace = str(raw.get("workspace") or "").strip()
        project_id = str(raw.get("id") or raw.get("project_id") or "").strip()
        owner = str(raw.get("owner") or "").strip()
        if not workspace or not project_id:
            continue
        key = (owner, project_id, os.path.realpath(workspace))
        if key in seen:
            continue
        seen.add(key)
        scopes.append({"owner": owner, "project_id": project_id,
                       "workspace": workspace})

    for name in ready:
        remaining = ceiling - (time.monotonic() - started)
        if remaining <= 0:
            result = TaskResult(
                name=name,
                detail=f"skipped: the {ceiling:.0f}s scheduled budget was spent",
            )
            results.append(result)
            _remember(result)
            continue
        if name not in SCOPED_TASK_NAMES:
            one = run((name,), budget_s=remaining)
            if one:
                results.append(one[0])
            continue
        if not scopes:
            one = run((name,), budget_s=remaining)
            if one:
                results.append(one[0])
            continue

        changed = elapsed_ms = failures = completed = yielded = 0
        for scope in scopes:
            remaining = ceiling - (time.monotonic() - started)
            if remaining <= 0:
                break
            one = run((name,), budget_s=remaining, **scope)
            if not one:
                continue
            row = one[0]
            changed += row.changed
            elapsed_ms += row.elapsed_ms
            failures += 0 if row.ok else 1
            yielded += 1 if row.detail.startswith("skipped:") else 0
            completed += 1
        omitted = max(0, len(scopes) - completed)
        detail = (
            f"{completed}/{len(scopes)} project workspace(s); "
            f"changed {changed}; failures {failures}; yielded {yielded}"
            + (f"; budget skipped {omitted}" if omitted else "")
        )
        aggregate = TaskResult(
            name=name, ok=failures == 0, changed=changed,
            detail=detail, elapsed_ms=elapsed_ms,
        )
        _remember(aggregate)
        results.append(aggregate)
    return results


async def scheduler_loop(*, interval_s: Optional[float] = None,
                         budget_s: float = 30.0) -> None:
    """Run deterministic maintenance periodically until app shutdown."""
    import asyncio

    if interval_s is None:
        try:
            from src.settings import get_setting
            interval_s = float(get_setting(
                "agent_context_maintenance_seconds", 300
            ) or 300)
        except Exception:  # noqa: BLE001
            interval_s = 300.0
    interval = max(60.0, min(float(interval_s), 86_400.0))
    # Do not make first paint compete with index/database maintenance.
    await asyncio.sleep(min(60.0, interval))
    while True:
        try:
            from services.projects import get_store
            projects = await asyncio.to_thread(get_store().list, None)
            await asyncio.to_thread(
                run_scheduled, projects=projects, budget_s=budget_s
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("scheduled context maintenance failed: %s", exc)
        await asyncio.sleep(interval)


__all__ = [
    "TASK_NAMES", "TASK_INTERVALS_S", "LEDGER_DAYS_SETTING",
    "DEFAULT_LEDGER_DAYS", "MAX_EXPERIENCE_ROWS", "SCHEMA", "TASKS",
    "SCOPED_TASK_NAMES", "TaskResult", "should_yield", "run",
    "run_scheduled", "scheduler_loop", "due", "last_run",
]
