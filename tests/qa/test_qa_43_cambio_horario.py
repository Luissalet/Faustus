"""QA-43 · Cambio horario (docs/spec/v2/acceptance_scenarios.json).

Estimulo: recurrencia Europe/Madrid en transicion DST y equipo apagado.
Resultado exigido (literal): "Politica de ambiguedad/misfire visible; no
duplicacion silenciosa."

Requisitos: AUTO-01.

Estado: verde. `src/task_scheduler.py::_dst_resolve` usa `datetime.fold`
(PEP 495) para resolver el instante ambiguo (2026-10-25 02:30, ocurre dos
veces) y el inexistente (2026-03-29 02:30, salto de primavera) segun una
politica explicita y declarada por tarea (`dst_ambiguity_policy`, una de
"skip"/"run_once"/"run_all" — ver `set_task_policy`/`get_task_policy`), en
vez de dejar que `zoneinfo` elija silenciosamente. Un "misfire" (equipo
apagado durante la hora programada) tiene su propia politica declarada
(`misfire_policy`, "fire_immediately"/"skip"), aplicada por el barrido de
arranque de `TaskScheduler.start()`. Ninguna de las dos rutas puede producir
una ejecucion duplicada silenciosa: `run_all` es la UNICA forma de disparar
dos veces la misma ocurrencia ambigua, y solo porque se declaro asi
explicitamente (ver `_maybe_queue_run_all_followup`) — el resto de politicas
disparan como mucho una vez por ocurrencia.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from zoneinfo import ZoneInfo

from src.task_scheduler import (
    DEFAULT_DST_AMBIGUITY_POLICY, DEFAULT_MISFIRE_POLICY, DST_AMBIGUITY_POLICIES,
    MISFIRE_POLICIES, _dst_resolve, compute_next_run, get_task_policy, set_task_policy,
)

pytestmark = pytest.mark.qa_state("green")

MADRID = ZoneInfo("Europe/Madrid")


def test_the_policy_is_explicit_and_declared_per_task_not_implicit():
    """The literal acceptance bar: "politica ... visible". A task that never
    declares one still gets a NAMED default (not "whatever zoneinfo does"),
    and any task can declare its own."""
    assert DEFAULT_DST_AMBIGUITY_POLICY in DST_AMBIGUITY_POLICIES
    assert DEFAULT_MISFIRE_POLICY in MISFIRE_POLICIES
    undeclared = get_task_policy("qa-43-undeclared-task")
    assert undeclared["dst_ambiguity_policy"] == DEFAULT_DST_AMBIGUITY_POLICY
    assert undeclared["misfire_policy"] == DEFAULT_MISFIRE_POLICY

    set_task_policy("qa-43-declared-task", dst_ambiguity_policy="skip", misfire_policy="skip")
    declared = get_task_policy("qa-43-declared-task")
    assert declared["dst_ambiguity_policy"] == "skip"
    assert declared["misfire_policy"] == "skip"


def test_spring_forward_gap_2026_03_29_0230_never_silently_picks_a_moment():
    """2026-03-29 02:30 Europe/Madrid does not exist (clocks jump 02:00 ->
    03:00). Each policy gives a distinct, named answer — never an unexamined
    default from zoneinfo."""
    status, _ = _dst_resolve(datetime(2026, 3, 29, 2, 30), MADRID, "run_once")
    assert status == "nonexistent"

    before = datetime(2026, 3, 28, 20, 0)
    skip = compute_next_run("daily", "02:30", after=before, tz_name="Europe/Madrid",
                            dst_ambiguity_policy="skip")
    run_once = compute_next_run("daily", "02:30", after=before, tz_name="Europe/Madrid",
                                dst_ambiguity_policy="run_once")
    # skip abandons the gap day entirely and lands on the NEXT day's 02:30.
    assert skip.date() == datetime(2026, 3, 30).date()
    # run_once still gives one honest instant for the gap day.
    assert run_once.date() == datetime(2026, 3, 29).date()
    assert skip != run_once


def test_fall_back_repeat_2026_10_25_0230_occurs_twice_and_each_policy_says_which():
    """2026-10-25 02:30 Europe/Madrid happens twice (02:00 CEST repeats as
    02:00 CET an hour later). No policy is allowed to average, guess, or
    silently pick one without saying so."""
    status, _ = _dst_resolve(datetime(2026, 10, 25, 2, 30), MADRID, "run_once")
    assert status == "ambiguous"

    before = datetime(2026, 10, 24, 20, 0)
    skip = compute_next_run("daily", "02:30", after=before, tz_name="Europe/Madrid",
                            dst_ambiguity_policy="skip")
    run_once = compute_next_run("daily", "02:30", after=before, tz_name="Europe/Madrid",
                                dst_ambiguity_policy="run_once")
    # skip abandons the whole ambiguous day.
    assert skip.date() == datetime(2026, 10, 26).date()
    # run_once fires the EARLIER of the two real instants (fold=0), never both.
    assert run_once == datetime(2026, 10, 25, 0, 30)


def test_run_all_is_the_only_policy_that_fires_twice_and_only_because_it_says_so():
    """The one legitimate way to run an ambiguous occurrence twice is to
    declare it (`run_all`) — and even then, the two firings are the two
    REAL distinct UTC instants the wall clock actually named, one hour
    apart, never the same instant repeated (which would be the silent
    duplication the acceptance criterion forbids)."""
    from src.task_scheduler import TaskScheduler

    task_id = "qa-43-run-all-task"
    set_task_policy(task_id, dst_ambiguity_policy="run_all")

    class _Task:
        id = task_id
        schedule = "daily"
        scheduled_time = "02:30"
        scheduled_day = None
        scheduled_date = None
        cron_expression = None
        timezone = "Europe/Madrid"
        crew_member_id = None

    sched = TaskScheduler(session_manager=None)
    before = datetime(2026, 10, 24, 20, 0)
    first = sched._next_moment_after(None, _Task(), after=before)
    second = sched._next_moment_after(None, _Task(), after=before)

    assert first != second, "the two run_all firings are distinct UTC instants"
    assert second - first == timedelta(hours=1), "exactly the DST jump width apart"


async def test_a_three_hour_machine_outage_never_replays_the_missed_occurrences_as_a_burst():
    """The other half of the acceptance bar: a misfire (machine/scheduler was
    off across one or more due occurrences) must not silently fire once per
    missed occurrence when it comes back — `misfire_policy="skip"` abandons
    the gap outright and moves straight to the next FUTURE occurrence,
    proven here against the actual startup sweep with a real in-memory DB."""
    import os
    import tempfile

    tmp = tempfile.mkdtemp(prefix="qa43-misfire-")
    os.environ.setdefault("DATA_DIR", tmp)

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    import core.database as db_mod
    from core.database import Base, ScheduledTask

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    orig_engine, orig_session = db_mod.engine, db_mod.SessionLocal
    db_mod.engine = engine
    db_mod.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    try:
        from src.task_scheduler import TaskScheduler

        # The scheduler stores naive UTC (`_utcnow()`); a local `now` is two
        # hours ahead on Luis's machine and made the assertion lie there.
        now = datetime.utcnow().replace(microsecond=0)
        task_id = "qa-43-misfire-skip-task"
        set_task_policy(task_id, misfire_policy="skip")
        db = db_mod.SessionLocal()
        try:
            db.add(ScheduledTask(
                id=task_id, owner="u1", name="hourly", task_type="action",
                action="noop", schedule="cron", cron_expression="0 * * * *",
                timezone="Europe/Madrid", status="active",
                next_run=now - timedelta(hours=3), trigger_type="schedule",
            ))
            db.commit()
        finally:
            db.close()

        sched = TaskScheduler(session_manager=None)
        await sched.start()
        try:
            db = db_mod.SessionLocal()
            try:
                row = db.query(ScheduledTask).filter(ScheduledTask.id == task_id).first()
                # Not stuck three hours in the past, and not a burst of
                # catch-up runs either — a single, future occurrence.
                assert row.next_run > now
                assert row.next_run <= now + timedelta(hours=1, minutes=1)
            finally:
                db.close()
        finally:
            await sched.stop()
    finally:
        db_mod.engine, db_mod.SessionLocal = orig_engine, orig_session
        engine.dispose()
