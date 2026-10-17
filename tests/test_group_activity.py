"""A silent/replayed stream must not look freshly active after polling/reload."""
from src.group_activity import activity_detail
import asyncio

from src.group_orchestrator import GroupCoordinator, GroupStore


def test_progress_survives_reload_without_polling_refreshing_it():
    started = activity_detail({}, now=100)
    event = {"run_id": "r", "last_event": {"sequence": 1, "thinking": True, "delta": "x"}}
    live = activity_detail(event, started, event=True, now=110)
    reloaded = activity_detail(live, event=True, now=500)
    assert reloaded["activity"]["started_at"] == 100
    assert reloaded["activity"]["last_event_at"] == 110
    assert reloaded["activity"]["event_count"] == 1
    assert reloaded["activity"]["phase"] == "thinking"
    event["last_event"] = {"sequence": 2, "thinking": True, "delta": "x"}
    advanced = activity_detail(event, reloaded, event=True, now=510)
    assert advanced["activity"]["last_event_at"] == 510
    assert advanced["activity"]["event_count"] == 2


def test_tool_completion_is_waiting_not_still_executing_and_terminal_clock_stops():
    using = activity_detail({"last_event": {"type": "tool_start", "tool": "read_file"}}, event=True, now=10)
    assert using["activity"]["tool"] == "read_file"
    waiting = activity_detail({"last_event": {"type": "tool_output", "exit_code": 1}}, using, event=True, now=20)
    assert waiting["activity"]["phase"] == "waiting"
    assert "tool" not in waiting["activity"]
    done = activity_detail(waiting, status="done", now=30)
    assert activity_detail(done, status="done", now=60)["activity"]["finished_at"] == 30
    assert done["activity"]["last_event_at"] == 20


def test_heartbeat_does_not_fake_model_progress_and_run_identity_is_respected():
    event = {"run_id": "a", "last_event": {"sequence": 1, "delta": "x"}}
    live = activity_detail(event, event=True, now=10)
    heartbeat = activity_detail({"run_id": "a", "last_event": {"type": "heartbeat"}}, live, event=True, now=80)
    assert heartbeat["activity"]["last_event_at"] == 10
    second = activity_detail({**event, "run_id": "b"}, live, event=True, now=90)
    assert second["activity"]["last_event_at"] == 90
    assert second["activity"]["event_count"] == 2


def test_no_invented_signal_before_first_event_and_inputs_not_mutated():
    raw = {"run_id": "r"}
    started = activity_detail(raw, now=10)
    assert raw == {"run_id": "r"}
    assert "last_event_at" not in started["activity"]
    paused = activity_detail(started, status="waiting_user", now=20)
    assert started["activity"]["phase"] == "waiting"
    resumed = activity_detail(paused, now=30)
    assert "finished_at" not in resumed["activity"]


class Transcript:
    def validate(self, *args): pass
    def user(self, *args): pass
    def reply(self, *args): pass


class Invoker:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def invoke(self, *, progress, **kwargs):
        detail = {"run_id": "real-fixture-run", "last_event": {"sequence": 3, "thinking": True, "delta": "x"}}
        await progress("", detail)
        self.started.set()
        await self.release.wait()
        return {"text": "Finished", "detail": detail}

    async def stop(self, *args): pass


async def test_native_coordinator_persists_signal_and_freezes_terminal_state(tmp_path):
    invoker = Invoker()
    path = tmp_path / "groups.db"
    driver = GroupCoordinator(invoker, Transcript(), store=GroupStore(path))
    driver.adopt({"parentId": "g", "mode": "parallel", "name": "Room", "participants": [
        {"sessionId": sid, "route": {"model": sid}} for sid in ("a", "b")
    ]}, None)
    await driver.submit("g", None, "work", "idempotent")
    await invoker.started.wait()
    active = driver.snapshot("g", None)
    activity = active["steps"][0]["detail"]["activity"]
    assert activity["phase"] == "thinking" and activity["last_event_at"] >= activity["started_at"]
    assert driver.snapshot("g", None)["steps"][0]["detail"]["activity"] == activity
    invoker.release.set()
    await asyncio.gather(*list(driver.tasks.values()))
    restored = GroupCoordinator(invoker, Transcript(), store=GroupStore(path)).snapshot("g", None)
    assert not restored["busy"]
    for step in restored["steps"]:
        assert step["detail"]["activity"]["phase"] == "done"
        assert step["detail"]["activity"]["finished_at"] >= step["detail"]["activity"]["last_event_at"]


async def test_stop_finishes_activity_instead_of_leaving_running_clock(tmp_path):
    invoker = Invoker()
    driver = GroupCoordinator(invoker, Transcript(), store=GroupStore(tmp_path / "groups.db"))
    driver.adopt({"parentId": "g", "mode": "round-robin", "name": "Room", "participants": [
        {"sessionId": sid, "route": {"model": sid}} for sid in ("a", "b")
    ]}, None)
    await driver.submit("g", None, "work", "id")
    await invoker.started.wait()
    await driver.stop("g", None)
    snapshot = driver.snapshot("g", None)
    assert not snapshot["busy"]
    for step in snapshot["steps"]:
        assert step["status"] == "stopped"
        assert step["detail"]["activity"]["phase"] == "stopped"
        assert step["detail"]["activity"]["finished_at"] > 0

