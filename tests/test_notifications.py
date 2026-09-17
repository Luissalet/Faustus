"""src/notifications.py — the mobile app's server-side event bus (lot M-A).

Small and deterministic on purpose: this module has exactly one job (never
lose the last ~200 events, never raise into whoever is emitting), so the
tests exercise that job directly rather than through any route.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from src import notifications as N


@pytest.fixture(autouse=True)
def _isolated_bus(tmp_path, monkeypatch):
    """Every test gets a fresh ring, a fresh subscriber table, and its own
    on-disk file — the module's module-level state would otherwise leak
    between tests (and between this file's tests and anything else that
    imports `src.notifications` in the same process)."""
    monkeypatch.setattr(N, "NOTIFICATIONS_FILE", str(tmp_path / "notifications.jsonl"))
    N._ring.clear()
    N._subscribers.clear()
    N._next_id = 1
    yield
    N._ring.clear()
    N._subscribers.clear()
    N._next_id = 1


def test_emit_returns_the_stored_event_with_an_incrementing_id():
    first = N.emit("turn_finished", owner="alice", title="Chat", body="hi")
    second = N.emit("turn_finished", owner="alice", title="Chat", body="again")
    assert first["id"] == 1
    assert second["id"] == 2
    assert first["kind"] == "turn_finished"
    assert first["owner"] == "alice"


def test_body_and_title_are_truncated_to_200_chars():
    long_body = "x" * 500
    event = N.emit("reminder", owner="alice", title="t" * 300, body=long_body)
    assert len(event["body"]) == 200
    assert len(event["title"]) == 200


def test_emit_never_raises_even_with_a_bad_data_payload(monkeypatch):
    """Fire-and-forget by contract: a broken persist path must not surface
    to the caller (a chat turn / approval decision / scheduled task)."""
    def _boom(*a, **kw):
        raise OSError("disk is gone")
    monkeypatch.setattr(N, "_persist_ring", _boom)
    # Should not raise, and should still be visible via list() since the
    # ring update happens before the (failing) persist call.
    event = N.emit("turn_finished", owner="bob", title="t", body="b")
    assert event is not None
    rows, _ = N.list_events(owner="bob")
    assert len(rows) == 1


def test_list_events_filters_by_owner_and_respects_since_id():
    N.emit("turn_finished", owner="alice", title="a1", body="")
    N.emit("turn_finished", owner="bob", title="b1", body="")
    N.emit("turn_finished", owner="alice", title="a2", body="")

    rows, last_id = N.list_events(owner="alice", since_id=0)
    assert [r["title"] for r in rows] == ["a1", "a2"]
    assert last_id == rows[-1]["id"]

    rows2, last_id2 = N.list_events(owner="alice", since_id=last_id)
    assert rows2 == []
    assert last_id2 == last_id  # caught-up client doesn't regress its cursor


def test_list_events_includes_ownerless_legacy_rows():
    N.emit("reminder", owner=None, title="legacy", body="")
    N.emit("reminder", owner="alice", title="mine", body="")
    rows, _ = N.list_events(owner="alice", since_id=0)
    assert {r["title"] for r in rows} == {"legacy", "mine"}


def test_list_events_respects_limit():
    for i in range(10):
        N.emit("task_finished", owner="alice", title=f"t{i}", body="")
    rows, last_id = N.list_events(owner="alice", since_id=0, limit=3)
    assert len(rows) == 3
    assert [r["title"] for r in rows] == ["t7", "t8", "t9"]
    assert last_id == rows[-1]["id"]


def test_ring_is_bounded_to_200():
    for i in range(N.RING_SIZE + 50):
        N.emit("task_finished", owner="alice", title=f"t{i}", body="")
    rows, _ = N.list_events(owner="alice", since_id=0, limit=1000)
    assert len(rows) == N.RING_SIZE
    assert rows[0]["title"] == "t50"       # the oldest 50 were evicted
    assert rows[-1]["title"] == f"t{N.RING_SIZE + 49}"


def test_events_persist_to_disk_and_survive_a_reload():
    N.emit("turn_finished", owner="alice", title="persisted", body="yes")
    on_disk = [json.loads(line) for line in open(N.NOTIFICATIONS_FILE, encoding="utf-8") if line.strip()]
    assert len(on_disk) == 1
    assert on_disk[0]["title"] == "persisted"

    # Simulate a process restart: clear in-memory state, reload from disk.
    N._ring.clear()
    N._next_id = 1
    N._load_from_disk()
    rows, _ = N.list_events(owner="alice", since_id=0)
    assert len(rows) == 1
    assert rows[0]["title"] == "persisted"
    # The id counter resumed past what was on disk, so the next emit does
    # not collide with a replayed id.
    second = N.emit("turn_finished", owner="alice", title="new", body="")
    assert second["id"] == on_disk[0]["id"] + 1


def test_persisted_file_never_exceeds_the_ring_size():
    for i in range(N.RING_SIZE + 20):
        N.emit("task_finished", owner="alice", title=f"t{i}", body="")
    lines = [ln for ln in open(N.NOTIFICATIONS_FILE, encoding="utf-8") if ln.strip()]
    assert len(lines) == N.RING_SIZE


@pytest.mark.asyncio
async def test_subscribe_receives_events_emitted_after_it_subscribed():
    queue = N.subscribe("alice")
    N.emit("turn_finished", owner="alice", title="live", body="")
    event = await asyncio.wait_for(queue.get(), timeout=1.0)
    assert event["title"] == "live"
    N.unsubscribe("alice", queue)


@pytest.mark.asyncio
async def test_unsubscribe_stops_delivery():
    queue = N.subscribe("alice")
    N.unsubscribe("alice", queue)
    N.emit("turn_finished", owner="alice", title="missed", body="")
    assert queue.empty()


def test_subscriber_scoped_to_owner_does_not_see_another_owners_event():
    q_alice = N.subscribe("alice")
    N.emit("turn_finished", owner="bob", title="not for alice", body="")
    assert q_alice.empty()
    N.unsubscribe("alice", q_alice)


def test_latest_id_reflects_the_ring_tail():
    assert N.latest_id() == 0
    e1 = N.emit("turn_finished", owner="alice", title="t", body="")
    assert N.latest_id() == e1["id"]


def test_subscriber_count_tracks_subscribe_and_unsubscribe():
    assert N.subscriber_count("alice") == 0
    q = N.subscribe("alice")
    assert N.subscriber_count("alice") == 1
    assert N.subscriber_count() == 1
    N.unsubscribe("alice", q)
    assert N.subscriber_count("alice") == 0


# ── hook wiring: the four emission sites, tested directly against the bus ──
#
# Each of these drives the actual production function (not a re-
# implementation of its logic) with the minimal fake inputs it needs, so a
# change to the hook's shape breaks here rather than only being noticed by a
# phone that stops getting notifications.

def test_task_scheduler_hook_uses_result_first_line_as_body():
    from src.task_scheduler import notify_task_finished

    notify_task_finished(
        task_name="Weather report", owner="luis", task_id="t1",
        status="success", result="18°C, light rain\n(Open-Meteo)",
    )
    rows, _ = N.list_events(owner="luis", since_id=0)
    assert len(rows) == 1
    assert rows[0]["kind"] == "task_finished"
    assert rows[0]["title"] == "Weather report"
    assert rows[0]["body"] == "18°C, light rain"
    assert rows[0]["data"] == {"task_id": "t1", "status": "success"}


def test_task_scheduler_hook_falls_back_to_error_then_status():
    from src.task_scheduler import notify_task_finished

    notify_task_finished(task_name="Mail digest", owner="luis", task_id="t2",
                          status="error", result="", error="SMTP timeout")
    rows, _ = N.list_events(owner="luis", since_id=0)
    assert rows[0]["body"] == "SMTP timeout"

    notify_task_finished(task_name="No-op task", owner="luis", task_id="t3",
                          status="success", result="", error="")
    rows, _ = N.list_events(owner="luis", since_id=0)
    assert rows[-1]["body"] == "success"


def test_task_scheduler_hook_never_raises_on_a_broken_bus(monkeypatch):
    from src import task_scheduler

    def _boom(*a, **kw):
        raise RuntimeError("bus is down")
    monkeypatch.setattr(N, "emit", _boom)
    # Must not raise — the scheduler's own finalization must never fail
    # because a notification could not be sent.
    task_scheduler.notify_task_finished(
        task_name="x", owner="luis", task_id="t1", status="success",
    )


def test_approvals_hook_notifies_pending_with_plan_action_as_title():
    from routes.approvals_routes import _notify_approval
    from src.contracts import ApprovalPlan
    from src.contracts.approval import Approval

    plan = ApprovalPlan.parse({
        "action": "publish", "detail": "Publish the September clip.",
        "recipients": ["youtube:channel-1"],
    })
    card = Approval.parse({
        "id": "apr_1", "plan": plan.to_dict(), "status": "pending", "owner": "luis",
    })
    _notify_approval("approval_pending", card, session_id="s1")

    rows, _ = N.list_events(owner="luis", since_id=0)
    assert rows[0]["kind"] == "approval_pending"
    assert rows[0]["title"] == "publish"
    assert rows[0]["body"] == "Publish the September clip."
    assert rows[0]["approval_id"] == "apr_1"
    assert rows[0]["session_id"] == "s1"


def test_approvals_hook_notifies_a_real_decision_only():
    from routes.approvals_routes import _notify_decision

    granted_result = {
        "ok": True, "reason": "granted",
        "approval": {
            "id": "apr_1", "owner": "luis",
            "plan": {"action": "publish", "detail": "Publish the clip."},
        },
    }
    _notify_decision("apr_1", "granted", granted_result)
    rows, _ = N.list_events(owner="luis", since_id=0)
    assert len(rows) == 1
    assert rows[0]["kind"] == "approval_resolved"
    assert rows[0]["body"] == "granted: Publish the clip."

    # A lost race (already decided) must NOT produce a second notification.
    lost_race = {"ok": False, "reason": "already_granted", "approval": {"owner": "luis"}}
    _notify_decision("apr_1", "granted", lost_race)
    rows2, _ = N.list_events(owner="luis", since_id=0)
    assert len(rows2) == 1  # unchanged


def test_dispatch_reminder_emits_reminder_kind(monkeypatch):
    """`dispatch_reminder` fires its own `reminder` notification independent
    of the in-app `add_notification` call (which would otherwise mislabel
    this as `task_finished` — see the hook's comment in note_routes.py)."""
    import asyncio as _asyncio
    import src.settings as settings_mod
    from routes.note import note_routes

    monkeypatch.setattr(settings_mod, "load_settings", lambda: {"reminder_channel": "browser"})
    monkeypatch.setattr(note_routes, "_scheduler_ref", None)

    _asyncio.run(note_routes.dispatch_reminder(
        title="Buy milk", note_body="from the corner shop", note_id="note-1",
        owner="luis", queue_browser=True,
    ))

    rows, _ = N.list_events(owner="luis", since_id=0)
    reminders = [r for r in rows if r["kind"] == "reminder"]
    assert len(reminders) == 1
    assert reminders[0]["title"] == "Buy milk"
    assert reminders[0]["data"] == {"note_id": "note-1"}
