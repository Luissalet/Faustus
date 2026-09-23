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
    "brain_vault_sync", "brain_extract", "brain_wiki",
)

# These tasks describe one project workspace. A scheduled pass fans them out
# over every project instead of recording a misleading global "no workspace"
# success and then sleeping until the next interval.
SCOPED_TASK_NAMES = frozenset({"refresh_code_index", "degrade_experiences"})

# These describe one brain owner (a person, not a project workspace). A
# scheduled pass fans them out over every owner with brain data instead of
# running once against whatever owner happened to be passed in, or not at
# all when no owner is given.
OWNER_TASK_NAMES = frozenset({"brain_vault_sync", "brain_extract", "brain_wiki"})

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
    # Defaults only: brain_vault_sync's real interval is read from the
    # `brain_vault_sync_seconds` setting in `_brain_interval_s`, same as the
    # ledger days above are read from a setting rather than hard-coded.
    "brain_vault_sync": 120.0,
    "brain_extract": 300.0,
    "brain_wiki": 900.0,
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


# ── the second brain: markdown vault, entity extraction, wiki pages ─────────
#
# These three mirror the deterministic-first, model-optional, fail-closed
# rules the rest of this module already lives by: `brain_vault_sync` is a
# plain file<->row sync with no model in the loop; `brain_extract` always
# does its deterministic pass and only adds an LLM batch when
# `brain_llm_extraction` is on; `brain_wiki` never runs unless
# `brain_wiki_summaries` is on, and its own fallback (`render_summary_fallback`)
# is what a disabled or failing model degrades to. All three are additionally
# gated by `brain_enabled`, on top of `should_yield`/`budget_s`, because they
# touch a per-owner vault rather than the context-engine's own tables.
#
# Unlike the tasks above, these describe one *owner*, not one project
# workspace, so they are fanned out by `OWNER_TASK_NAMES` in `run_scheduled`
# rather than `SCOPED_TASK_NAMES` — the scope dict's `owner` key is what they
# read; `workspace`/`project_id` are unused.

def _brain_enabled() -> bool:
    try:
        from src.settings import get_setting

        return bool(get_setting("brain_enabled", True))
    except Exception:  # noqa: BLE001 - unreadable setting means off
        return False


def _run_coro(coro: Any) -> Any:
    """Run a `src.brain` coroutine from this module's synchronous tasks.

    Every caller of `run()` that reaches these tasks is either already on a
    worker thread with no event loop (`scheduler_loop`'s
    `asyncio.to_thread`) or a synchronous test — never a coroutine itself —
    so a fresh `asyncio.run` is safe and avoids making the whole maintenance
    module async for the sake of two `src.brain` functions."""
    import asyncio

    return asyncio.run(coro)


def _brain_vault_sync(*, owner: str = "", **rest: Any) -> Tuple[int, str]:
    if not _brain_enabled():
        return 0, "brain is disabled; nothing synced"
    from src.brain import vault

    report = vault.sync(owner, budget_s=15.0)
    changed = int(report.get("exported", 0)) + int(report.get("imported", 0))
    errors = report.get("errors") or []
    detail = (f"exported {report.get('exported', 0)}, imported "
              f"{report.get('imported', 0)}, suppressed {report.get('suppressed', 0)}"
              f"{'; delete guard tripped' if report.get('guard_tripped') else ''}"
              f"{f'; {len(errors)} error(s)' if errors else ''}")
    return changed, detail


def _brain_extract(*, owner: str = "", **rest: Any) -> Tuple[int, str]:
    if not _brain_enabled():
        return 0, "brain is disabled; nothing extracted"
    from src.brain import extract
    from src.settings import get_setting

    use_llm = bool(get_setting("brain_llm_extraction", True))
    batch = int(get_setting("brain_llm_extraction_batch", 12) or 12)
    report = _run_coro(extract.extract_pending(
        owner, limit=batch, budget_s=15.0, use_llm=use_llm,
    ))
    changed = int(report.get("entities", 0)) + int(report.get("relations", 0))
    detail = (f"{report.get('processed', 0)} source(s) processed, "
              f"{report.get('entities', 0)} entit(y/ies), "
              f"{report.get('relations', 0)} relation(s)"
              f"{'; ' + str(report.get('errors')) + ' error(s)' if report.get('errors') else ''}")
    return changed, detail


def _brain_wiki(*, owner: str = "", **rest: Any) -> Tuple[int, str]:
    if not _brain_enabled():
        return 0, "brain is disabled; no wiki pages refreshed"
    from src.settings import get_setting

    if not bool(get_setting("brain_wiki_summaries", True)):
        return 0, "wiki summaries are off"
    from src.brain import wiki

    report = _run_coro(wiki.refresh_stale(owner, limit=5, budget_s=15.0))
    changed = int(report.get("updated", 0))
    detail = (f"{report.get('updated', 0)} page(s) refreshed, "
              f"{report.get('skipped', 0)} skipped"
              f"{'; ' + str(report.get('errors')) + ' error(s)' if report.get('errors') else ''}")
    return changed, detail


TASKS: Dict[str, Callable[..., Tuple[int, str]]] = {
    "prune_packets": _prune_packets,
    "expire_findings": _expire_findings,
    "refresh_code_index": _refresh_code_index,
    "degrade_experiences": _degrade_experiences,
    "audit_blocks": _audit_blocks,
    "vacuum": _vacuum,
    "brain_vault_sync": _brain_vault_sync,
    "brain_extract": _brain_extract,
    "brain_wiki": _brain_wiki,
}


def _brain_owners() -> List[str]:
    """Every owner with brain data, from the brain store and memory items.

    There is no owner registry to enumerate against, so this unions the
    three places an owner can show up: the vault's own tables (a note or
    entity written straight to the store), and `memory_engine` (an owner who
    has memories but has not synced the vault yet). A vault folder on disk
    with no matching DB row is not possible — `vault.sync` always writes a
    `notes` row for anything it exports — so the filesystem is not a fourth
    source here."""
    owners: set = set()
    try:
        from src.brain import db as brain_db

        with brain_db.db() as conn:
            for table in ("notes", "entities"):
                try:
                    rows = conn.execute(
                        f"SELECT DISTINCT owner FROM {table}").fetchall()
                except sqlite3.Error:
                    continue
                for row in rows:
                    value = str(row[0] or "").strip()
                    if value:
                        owners.add(value)
    except Exception as exc:  # noqa: BLE001 - an unreadable store yields no owners
        logger.debug("maintenance could not enumerate brain owners: %s", exc)

    try:
        from src import memory_engine

        for item in memory_engine.list_items(owner=None, limit=2000):
            value = str((item or {}).get("owner") or "").strip()
            if value:
                owners.add(value)
    except Exception as exc:  # noqa: BLE001
        logger.debug("maintenance could not enumerate memory owners: %s", exc)

    return sorted(owners)


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


def _brain_interval_s(name: str) -> float:
    """`brain_vault_sync`'s interval is a setting; the other two brain tasks
    keep their static defaults (there was no contract for making them
    separately configurable, and two more settings for two more intervals
    is not worth it while nobody has asked to tune them)."""
    if name != "brain_vault_sync":
        return TASK_INTERVALS_S.get(name, 3_600.0)
    try:
        from src.settings import get_setting

        value = float(get_setting("brain_vault_sync_seconds", 120) or 120)
    except Exception:  # noqa: BLE001 - an unreadable setting is the default
        return TASK_INTERVALS_S["brain_vault_sync"]
    return value if value > 0 else TASK_INTERVALS_S["brain_vault_sync"]


def due(*, now: Optional[datetime] = None) -> List[str]:
    """Tasks whose interval has elapsed, in `TASK_NAMES` order.

    Order matters and is not alphabetical: pruning before vacuuming means the
    vacuum reclaims what the prune freed, and refreshing the index before
    degrading experiences means the staleness check sees the current tree.

    The brain tasks are additionally gated on `brain_enabled` here, not just
    inside each task, so a disabled brain does not even show up as "due" —
    `run_scheduled` then skips straight past them instead of recording a
    once-a-cycle no-op row for a feature the owner turned off."""
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    history = last_run()
    ready: List[str] = []
    for name in TASK_NAMES:
        if name in OWNER_TASK_NAMES and not _brain_enabled():
            continue
        entry = history.get(name) or {}
        ran_at = store.parse_iso(entry.get("ran_at")) if entry.get("ran_at") else None
        if ran_at is None:
            ready.append(name)
            continue
        interval = _brain_interval_s(name)
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
        if name in OWNER_TASK_NAMES:
            owners = _brain_owners()
            if not owners:
                one = run((name,), budget_s=remaining)
                if one:
                    results.append(one[0])
                continue
            aggregate = _run_fanned_out(
                name, [{"owner": o} for o in owners], "owner(s)",
                ceiling=ceiling, started=started,
            )
            _remember(aggregate)
            results.append(aggregate)
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

        aggregate = _run_fanned_out(
            name, scopes, "project workspace(s)", ceiling=ceiling, started=started,
        )
        _remember(aggregate)
        results.append(aggregate)
    return results


def _run_fanned_out(name: str, scopes: Sequence[Dict[str, str]], unit: str, *,
                    ceiling: float, started: float) -> TaskResult:
    """Run one task once per scope (a project workspace or a brain owner),
    folding the rows into a single aggregate result — the same shape
    `run_scheduled` has always recorded for `SCOPED_TASK_NAMES`, now shared
    with the owner fan-out so both read the same way in `last_run()`."""
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
        f"{completed}/{len(scopes)} {unit}; "
        f"changed {changed}; failures {failures}; yielded {yielded}"
        + (f"; budget skipped {omitted}" if omitted else "")
    )
    return TaskResult(name=name, ok=failures == 0, changed=changed,
                      detail=detail, elapsed_ms=elapsed_ms)


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
    "SCOPED_TASK_NAMES", "OWNER_TASK_NAMES", "TaskResult", "should_yield",
    "run", "run_scheduled", "scheduler_loop", "due", "last_run",
]
