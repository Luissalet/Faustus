"""Home cards (src/home_cards.py, routes/home_cards_routes.py): pin an
automation, see its latest successful result, unpin, reorder."""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta

import pytest

from src import home_cards as hc


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    import src.constants as consts
    monkeypatch.setattr(consts, "DATA_DIR", str(tmp_path), raising=False)


def _task(owner="admin", name="Tiempo"):
    from core.database import ScheduledTask, SessionLocal, TaskRun
    db = SessionLocal()
    tid = str(uuid.uuid4())
    try:
        db.add(ScheduledTask(id=tid, owner=owner, name=name, prompt='{"place": "X"}', task_type="action",
                             action="weather_report", schedule="daily", scheduled_time="08:00",
                             trigger_type="schedule", status="active", next_run=datetime.utcnow() + timedelta(hours=1)))
        db.add(TaskRun(id=str(uuid.uuid4()), task_id=tid, started_at=datetime.utcnow() - timedelta(hours=2),
                       finished_at=datetime.utcnow() - timedelta(hours=2), status="success", result="**Tiempo** soleado"))
        db.add(TaskRun(id=str(uuid.uuid4()), task_id=tid, started_at=datetime.utcnow() - timedelta(minutes=5),
                       finished_at=datetime.utcnow() - timedelta(minutes=4), status="error", error="boom"))
        db.commit()
    finally:
        db.close()
    return tid


def test_pin_unpin_reorder_are_per_owner():
    a, b = _task(), _task(name="Noticias")
    assert hc.pin("admin", a) == [a]
    assert hc.pin("admin", b) == [a, b]
    assert hc.pin("admin", a) == [b, a], "re-pinning moves to the end"
    assert hc.reorder("admin", [a, b, "ghost"]) == [a, b]
    assert hc.pinned("other") == []
    assert hc.unpin("admin", a) == [b]
    assert hc.is_pinned("admin", b) and not hc.is_pinned("admin", a)


def test_cards_show_the_latest_successful_result_and_the_last_status():
    tid = _task()
    hc.pin("admin", tid)
    cards = hc.cards("admin")
    assert len(cards) == 1
    c = cards[0]
    assert c["name"] == "Tiempo" and c["action"] == "weather_report"
    assert c["result"] == "**Tiempo** soleado" and c["result_at"]
    assert c["last_status"] == "error" and c["last_error"] == "boom" and c["running"] is False
    assert c["next_run"] and c["schedule"] == "daily"
    # another owner cannot see it through the pin list
    hc.pin("other", tid)
    assert hc.cards("other") == []


def test_routes_pin_requires_an_existing_task():
    from routes.home_cards_routes import setup_home_cards_routes, PinBody
    from fastapi import HTTPException
    router = setup_home_cards_routes()
    routes = {(next(iter(r.methods)), r.path): r.endpoint for r in router.routes}

    class Req:
        headers = {}
        state = type("S", (), {"current_user": "admin"})()
        client = type("C", (), {"host": "127.0.0.1"})()
        cookies = {}

    with pytest.raises(HTTPException) as exc:
        asyncio.run(routes[("POST", "/api/home/cards")](PinBody(task_id="nope"), Req()))
    assert exc.value.status_code == 404
    tid = _task()
    out = asyncio.run(routes[("POST", "/api/home/cards")](PinBody(task_id=tid), Req()))
    assert tid in out["cards"]
    listed = asyncio.run(routes[("GET", "/api/home/cards")](Req()))
    assert listed["cards"][0]["task_id"] == tid
