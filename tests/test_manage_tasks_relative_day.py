"""PENDIENTES §99 (17-09): a task created for "mañana a las 8:00" landed with
``next_run`` on TODAY. ``do_manage_tasks`` now resolves relative day words
(hoy/mañana/pasado mañana, today/tomorrow) from the request text against the
user's local date, so a request naming "tomorrow" can never land on today —
without touching a built-in watcher's own `when` content parameter (e.g.
weather_report's "which day's forecast") or an explicit cron expression.
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from tests.helpers.import_state import clear_fake_database_modules

clear_fake_database_modules()

import core.database as cdb
from core.database import ScheduledTask
import src.task_scheduler as task_scheduler
from src.tools import system as tools_system
from src.tools.system import (
    _relative_day_offset_from_text,
    _resolve_relative_day_next_run,
    do_manage_tasks,
)

_TMPDB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_ENGINE = create_engine(
    f"sqlite:///{_TMPDB.name}",
    connect_args={"check_same_thread": False},
    poolclass=NullPool,
)
cdb.Base.metadata.create_all(_ENGINE)
_TS = sessionmaker(bind=_ENGINE, autoflush=False, autocommit=False)


@pytest.fixture(autouse=True)
def _use_this_module_db(monkeypatch):
    # ``do_manage_tasks`` does ``from core.database import SessionLocal`` at
    # call time, and ``core.database.SessionLocal`` is a single process-wide
    # attribute — a bare module-level assignment here would leak into (or be
    # clobbered by) any other test module doing the same thing in the same
    # pytest session, purely by import order. Scope it to each test instead.
    monkeypatch.setattr(cdb, "SessionLocal", _TS)


def _get(task_id):
    db = _TS()
    try:
        return db.query(ScheduledTask).filter(ScheduledTask.id == task_id).first()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Pure text detection
# ---------------------------------------------------------------------------

def test_detects_spanish_and_english_relative_days():
    assert _relative_day_offset_from_text("Recuérdame mañana a las 8:00") == 1
    assert _relative_day_offset_from_text("remind me tomorrow at 8am") == 1
    assert _relative_day_offset_from_text("hazlo pasado mañana") == 2
    assert _relative_day_offset_from_text("do it the day after tomorrow") == 2
    assert _relative_day_offset_from_text("revisar hoy mismo") == 0
    assert _relative_day_offset_from_text("do this today") == 0


def test_no_match_returns_none():
    assert _relative_day_offset_from_text("resumen semanal de correo") is None
    assert _relative_day_offset_from_text(None, None) is None


def test_json_watcher_params_are_not_treated_as_prose():
    # weather_report's own `when` PARAMETER, serialized into `prompt` as JSON —
    # never a scheduling instruction, even though it contains the word.
    assert _relative_day_offset_from_text('{"place": "Madrid", "when": "tomorrow"}') is None


# ---------------------------------------------------------------------------
# Pure next_run resolution
# ---------------------------------------------------------------------------

def test_pushes_next_run_forward_when_text_says_tomorrow(monkeypatch):
    fixed_today = datetime(2026, 9, 17, 6, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(tools_system, "_local_today", lambda tz_name: fixed_today)
    today_8am = datetime(2026, 9, 17, 8, 0)  # naive UTC, as compute_next_run returns
    result = _resolve_relative_day_next_run(
        today_8am, task_type="llm", schedule="daily", tz_name="UTC",
        texts=("Recuérdame mañana a las 8:00 revisar el informe", None),
    )
    assert result.date() == (fixed_today.date() + timedelta(days=1))


def test_leaves_next_run_alone_without_relative_wording(monkeypatch):
    fixed_today = datetime(2026, 9, 17, 6, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(tools_system, "_local_today", lambda tz_name: fixed_today)
    today_8am = datetime(2026, 9, 17, 8, 0)
    result = _resolve_relative_day_next_run(
        today_8am, task_type="llm", schedule="daily", tz_name="UTC",
        texts=("revisar el informe todos los días", None),
    )
    assert result == today_8am


def test_never_moves_an_already_correct_date_earlier(monkeypatch):
    fixed_today = datetime(2026, 9, 17, 6, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(tools_system, "_local_today", lambda tz_name: fixed_today)
    already_tomorrow = datetime(2026, 9, 19, 8, 0)  # further out than "mañana" needs
    result = _resolve_relative_day_next_run(
        already_tomorrow, task_type="llm", schedule="daily", tz_name="UTC",
        texts=("hazlo mañana",), )
    assert result == already_tomorrow


def test_action_tasks_and_cron_are_excluded(monkeypatch):
    fixed_today = datetime(2026, 9, 17, 6, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(tools_system, "_local_today", lambda tz_name: fixed_today)
    today_8am = datetime(2026, 9, 17, 8, 0)
    # A built-in watcher action: never reinterpreted.
    assert _resolve_relative_day_next_run(
        today_8am, task_type="action", schedule="daily", tz_name="UTC",
        texts=('{"when": "tomorrow"}',),
    ) == today_8am
    # An explicit cron expression: exact, never reinterpreted.
    assert _resolve_relative_day_next_run(
        today_8am, task_type="llm", schedule="cron", tz_name="UTC",
        texts=("mañana",),
    ) == today_8am


# ---------------------------------------------------------------------------
# End-to-end through do_manage_tasks
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_daily_task_for_tomorrow_never_lands_on_today(monkeypatch):
    fixed_today = datetime(2026, 9, 17, 6, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(tools_system, "_local_today", lambda tz_name: fixed_today)
    # Simulate the naive scheduler answer a weak local model plus the
    # existing "next occurrence of 08:00" logic can produce: TODAY at 08:00,
    # since that instant has not yet passed for the (mocked) current time.
    monkeypatch.setattr(
        task_scheduler, "compute_next_run",
        lambda *a, **k: datetime(2026, 9, 17, 8, 0),
    )
    out = await do_manage_tasks(
        json.dumps({
            "action": "create",
            "task_type": "llm",
            "prompt": "Recuérdame mañana a las 8:00 revisar el informe",
            "schedule": "daily",
            "scheduled_time": "08:00",
            "timezone": "UTC",
        }),
        owner="luis",
    )
    assert out["exit_code"] == 0
    stored = _get(out["task_id"])
    assert stored.next_run.date() == datetime(2026, 9, 18).date()


@pytest.mark.asyncio
async def test_create_daily_task_without_relative_wording_is_unaffected(monkeypatch):
    fixed_today = datetime(2026, 9, 17, 6, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(tools_system, "_local_today", lambda tz_name: fixed_today)
    monkeypatch.setattr(
        task_scheduler, "compute_next_run",
        lambda *a, **k: datetime(2026, 9, 17, 8, 0),
    )
    out = await do_manage_tasks(
        json.dumps({
            "action": "create",
            "task_type": "llm",
            "prompt": "Revisar el informe cada día",
            "schedule": "daily",
            "scheduled_time": "08:00",
            "timezone": "UTC",
        }),
        owner="luis",
    )
    assert out["exit_code"] == 0
    stored = _get(out["task_id"])
    assert stored.next_run == datetime(2026, 9, 17, 8, 0)


@pytest.mark.asyncio
async def test_weather_watcher_when_param_is_not_reinterpreted(monkeypatch):
    fixed_today = datetime(2026, 9, 17, 6, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(tools_system, "_local_today", lambda tz_name: fixed_today)
    monkeypatch.setattr(
        task_scheduler, "compute_next_run",
        lambda *a, **k: datetime(2026, 9, 17, 8, 0),
    )
    out = await do_manage_tasks(
        json.dumps({
            "action": "create",
            "task_type": "action",
            "action_name": "weather_report",
            "params": {"place": "Móstoles", "when": "tomorrow"},
            "schedule": "daily",
            "scheduled_time": "08:00",
            "timezone": "UTC",
        }),
        owner="luis",
    )
    assert out["exit_code"] == 0
    stored = _get(out["task_id"])
    # The watcher's "which day to forecast" param never shifts the task's own
    # run day — it stays exactly what compute_next_run said (today).
    assert stored.next_run == datetime(2026, 9, 17, 8, 0)
