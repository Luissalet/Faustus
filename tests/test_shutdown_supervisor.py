"""Shutdown has to stop the work it started (B-013).

Startup creates a dozen long-lived tasks — backups, the MCP connect, warmups,
keepalive, sweeps, the nightly audit, the Cookbook lifecycle, the background-job
monitor — and the old shutdown said goodbye to three of them by name. The rest
were left running on a loop that was about to close, which is not a tidy-up
problem but a correctness one: a connect task that is still awake reconnects the
MCP servers `disconnect_all()` has just closed, and a backup keeps writing under
data/ after the things it writes through have been torn down.

So these tests do not check that shutdown is fast or quiet. They check that
after it returns nothing this process started is still running, and that nothing
it started writes again — including across a second lifespan in the same
interpreter, which is what `uvicorn --reload` and this test module both do.
"""
import asyncio
import time

import pytest

from src.task_supervisor import TaskSupervisor
from src.webhook_manager import WebhookManager


async def _forever(log=None, tick=0.005):
    while True:
        if log is not None:
            log.append(time.monotonic())
        await asyncio.sleep(tick)


# ── the supervisor ──────────────────────────────────────────────────────────

async def test_a_task_spawned_after_stop_accepting_never_runs():
    """A task started during teardown is a task nobody is left to await."""
    sup = TaskSupervisor("t")
    ran = []

    async def _work():
        ran.append("ran")

    sup.stop_accepting()
    assert sup.spawn(_work(), name="late") is None
    await asyncio.sleep(0.02)

    assert ran == []
    assert sup.live() == []


async def test_close_cancels_what_outlives_the_drain_and_names_it():
    sup = TaskSupervisor("t")
    sup.spawn(_forever(), name="null-owner-sweep")

    interrupted = await sup.close(drain_timeout=0.01)

    assert interrupted == ["t:null-owner-sweep"]
    assert sup.live() == [], "a cancel that is not awaited has not stopped anything"


async def test_drain_lets_bounded_work_land_and_does_not_call_it_interrupted():
    sup = TaskSupervisor("t")
    done = []

    async def _bounded():
        await asyncio.sleep(0.02)
        done.append("committed")

    sup.spawn(_bounded(), name="delivery")

    interrupted = await sup.close(drain_timeout=2.0)

    assert done == ["committed"]
    assert interrupted == []


async def test_nothing_owned_writes_after_close_returns():
    sup = TaskSupervisor("t")
    writes = []
    sup.spawn(_forever(writes), name="auto-backups")
    await asyncio.sleep(0.03)

    await sup.close(drain_timeout=0.01)
    after_close = len(writes)
    await asyncio.sleep(0.05)

    assert after_close > 0, "the task under test never got going"
    assert len(writes) == after_close


async def test_a_cancelled_connect_cannot_reopen_what_shutdown_closed():
    """The MCP race, in miniature: teardown must cancel the connect task before
    it closes the thing the connect task would reopen."""
    sup = TaskSupervisor("t")
    events = []

    async def _connect_all():
        await asyncio.sleep(0.2)
        events.append("connect")

    sup.spawn(_connect_all(), name="mcp-connect")
    await asyncio.sleep(0.01)

    await sup.close(drain_timeout=0.01)
    events.append("disconnect")
    await asyncio.sleep(0.25)

    assert events == ["disconnect"]


async def test_start_reopens_a_closed_supervisor():
    """A second lifespan in one process must not inherit a shut supervisor."""
    sup = TaskSupervisor("t")
    await sup.close(drain_timeout=0)
    assert sup.accepting is False

    sup.start()
    ran = []

    async def _work():
        ran.append("ran")

    assert sup.spawn(_work(), name="again") is not None
    await asyncio.sleep(0.02)
    assert ran == ["ran"]


async def test_close_is_idempotent():
    sup = TaskSupervisor("t")
    sup.spawn(_forever(), name="loop")
    assert await sup.close(drain_timeout=0) == ["t:loop"]
    assert await sup.close(drain_timeout=0) == []


# ── the webhook manager ─────────────────────────────────────────────────────

async def test_webhook_close_waits_for_a_delivery_already_in_flight():
    """A delivery ends in a row UPDATE recording its outcome; cancelled blind,
    the row keeps claiming the previous attempt's result forever."""
    wm = WebhookManager()
    recorded = []

    async def _deliver():
        await asyncio.sleep(0.02)
        recorded.append("status written")

    wm._spawn_tracked(_deliver())

    left = await wm.close(drain_timeout=2.0)

    assert recorded == ["status written"]
    assert left == 0


async def test_webhook_close_cancels_a_delivery_that_outlives_the_deadline():
    wm = WebhookManager()
    wm._spawn_tracked(_forever())

    left = await wm.close(drain_timeout=0.01)

    assert left == 1
    assert [t for t in wm._bg_tasks if not t.done()] == []


async def test_no_webhook_is_started_after_stop_accepting():
    wm = WebhookManager()
    fired = []

    async def _deliver():
        fired.append("sent")

    wm.stop_accepting()
    assert wm._spawn_tracked(_deliver()) is None
    wm.fire_and_forget("chat.completed", {})
    await asyncio.sleep(0.02)

    assert fired == []
    assert wm._bg_tasks == set()


async def test_start_reopens_the_webhook_manager():
    """It is a module-level singleton: a closed gate would silently swallow
    every webhook of the next lifespan."""
    wm = WebhookManager()
    await wm.close(drain_timeout=0)
    wm.start()
    sent = []

    async def _deliver():
        sent.append("sent")

    assert wm._spawn_tracked(_deliver()) is not None
    await asyncio.sleep(0.02)
    assert sent == ["sent"]


# ── the background-job monitor ──────────────────────────────────────────────

@pytest.fixture
def monitor(monkeypatch):
    from src import bg_jobs, bg_monitor

    monkeypatch.setattr(bg_monitor, "POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(bg_jobs, "mark_followed_up", lambda job_id: None)
    yield bg_monitor
    monitor_task = bg_monitor._monitor_task
    if monitor_task is not None and not monitor_task.done():
        monitor_task.cancel()
    bg_monitor._monitor_task = None
    bg_monitor._accepting = True
    bg_monitor._stop = None


async def test_the_monitor_starts_no_follow_up_once_it_stops_accepting(monitor, monkeypatch):
    """A follow-up appends to a session and calls save_sessions(); one that
    starts during teardown writes through machinery already being closed."""
    from src import bg_jobs

    started = []

    async def _run_followup(rec):
        started.append(rec["id"])
        return True

    monkeypatch.setattr(bg_jobs, "pending_followups", lambda: [{"id": "j1"}])
    monkeypatch.setattr(monitor, "_run_followup", _run_followup)

    monitor.start_bg_monitor()
    await asyncio.sleep(0.05)
    assert started, "the monitor never ran a follow-up to begin with"

    monitor.stop_accepting()
    await monitor.drain(time.monotonic() + 2.0)
    settled = len(started)
    await asyncio.sleep(0.05)

    assert len(started) == settled
    assert monitor._monitor_task.done(), "stop_accepting must also end the loop"


async def test_the_monitor_loop_is_gone_after_close(monitor, monkeypatch):
    from src import bg_jobs

    monkeypatch.setattr(bg_jobs, "pending_followups", lambda: [])
    task = monitor.start_bg_monitor()

    await monitor.close()

    assert task.done()
    assert monitor._monitor_task is None
    await monitor.close()  # idempotent, and safe with nothing started


async def test_restarting_the_monitor_after_a_shutdown_works(monitor, monkeypatch):
    from src import bg_jobs

    monkeypatch.setattr(bg_jobs, "pending_followups", lambda: [])
    monitor.start_bg_monitor()
    await monitor.close()

    task = monitor.start_bg_monitor()
    await asyncio.sleep(0.03)

    assert not task.done(), "a monitor that starts already-stopped is a silent no-op"


# ── the real lifespan, twice ────────────────────────────────────────────────

@pytest.fixture
def quiet_app(monkeypatch):
    """The real startup/shutdown sequence with its slow leaves replaced.

    The substitutes stand in for exactly the behaviours under test: a backup
    that keeps writing, and an MCP connect that is still in flight when the
    disconnect happens. Everything about the ordering and the ownership is the
    application's own code.
    """
    import app as app_module
    from src import agent_runs, backup_service, bg_jobs, builtin_mcp
    from src import cookbook_serve_lifecycle, crash_recovery, secret_files, tool_index

    events: list = []
    writes: list = []

    async def _connect_all_enabled():
        await asyncio.sleep(0.3)
        events.append("connect")

    async def _disconnect_all():
        events.append("disconnect")

    async def _run_auto_backups():
        while True:
            writes.append(time.monotonic())
            await asyncio.sleep(0.005)

    async def _cookbook_loop():
        while True:
            await asyncio.sleep(0.01)

    async def _anoop(*a, **kw):
        return None

    monkeypatch.setattr(app_module.mcp_manager, "connect_all_enabled", _connect_all_enabled)
    monkeypatch.setattr(app_module.mcp_manager, "disconnect_all", _disconnect_all)
    monkeypatch.setattr(builtin_mcp, "register_builtin_servers", _anoop)
    monkeypatch.setattr(backup_service, "run_auto_backups", _run_auto_backups)
    monkeypatch.setattr(cookbook_serve_lifecycle, "cookbook_serve_lifecycle_loop", _cookbook_loop)
    monkeypatch.setattr(tool_index, "tool_index_warmup_enabled", lambda: False)
    monkeypatch.setattr(secret_files, "harden_secret_files", lambda: None)
    monkeypatch.setattr(agent_runs, "recover_interrupted_runs", lambda sm: [])
    monkeypatch.setattr(crash_recovery, "boot_scan", lambda: {})
    monkeypatch.setattr(app_module.skills_manager, "backfill_owner", lambda *a, **kw: 0)
    monkeypatch.setattr(app_module.task_scheduler, "start", _anoop)
    monkeypatch.setattr(app_module.task_scheduler, "stop", _anoop)
    monkeypatch.setattr(app_module.task_scheduler, "ensure_defaults", _anoop)
    monkeypatch.setattr(app_module, "upload_cleanup_func", None)
    # Shorter than the connect stand-in below, so the drain expires and the
    # cancellation path — the one the MCP race needs — is what runs here.
    monkeypatch.setattr(app_module, "SHUTDOWN_DRAIN_TIMEOUT_S", 0.05)
    # The monitor is real; its job store is not this test's business.
    monkeypatch.setattr(bg_jobs, "pending_followups", lambda: [])
    return app_module, events, writes


async def test_repeated_lifespan_leaves_nothing_of_its_own_running(quiet_app):
    app_module, events, writes = quiet_app

    for run in (1, 2):
        async with app_module.app.router.lifespan_context(app_module.app):
            await asyncio.sleep(0.05)
            started = list(app_module.app.state._startup_tasks)
            assert started, f"run {run}: startup created no tracked tasks"
            assert writes, f"run {run}: the backup stand-in never ran"

        alive = [t for t in started if not t.done()]
        assert alive == [], f"run {run}: still running after shutdown: {alive}"
        assert app_module.app.state.task_supervisor.live() == []

        settled = len(writes)
        await asyncio.sleep(0.05)
        assert len(writes) == settled, f"run {run}: a background task wrote after shutdown"


async def test_the_mcp_connect_cannot_reconnect_after_the_disconnect(quiet_app):
    """The failure the old sequence produced live: disconnect_all() closed the
    servers, then a connect task that was still awake reopened all of them."""
    app_module, events, _writes = quiet_app

    async with app_module.app.router.lifespan_context(app_module.app):
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.4)

    assert events == ["disconnect"], f"connect landed after shutdown: {events}"
