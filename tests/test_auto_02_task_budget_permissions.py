"""AUTO-02 — every scheduled task carries a budget and a permission set
declared at creation (`set_task_policy`/`get_task_policy`,
`src/autonomy_budget.py`); one execution can neither widen the permission
set nor exceed the budget, and exhaustion checkpoints + notifies instead of
silently stopping or silently running past it.

Two behaviors, both driven through the real scheduler code path:
  * permission clamp — `_execute_llm_task` folds `all_tools - permissions`
    into `disabled_tools` before the agent loop ever sees the tool list.
  * budget exhaustion — `_run_agent_loop` stops mid-turn, closes the event
    stream it was consuming, records a notification, and returns a result
    string carrying a checkpoint.
"""
from __future__ import annotations

import json
import os
import tempfile
import uuid
from types import SimpleNamespace

_tmp_data = tempfile.mkdtemp(prefix="odysseus-auto02-budget-test-")
os.environ.setdefault("DATA_DIR", _tmp_data)
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_tmp_data}/app.db")

from src.task_scheduler import TaskScheduler, set_task_policy
from src.autonomy_budget import Budget


def _make_task(task_id, prompt="do the thing"):
    return SimpleNamespace(
        id=task_id, crew_member_id=None, endpoint_url="http://ep/v1", model="m",
        session_id="s", owner="u1", prompt=prompt, name="job", max_steps=5,
        character_id=None,
    )


def _patch_scheduler_deps(monkeypatch):
    monkeypatch.setattr(
        "src.settings.get_setting",
        lambda key, default=None: [] if key == "disabled_tools" else default,
    )
    monkeypatch.setattr("src.tool_index.get_tool_index", lambda: None)


# ---------------------------------------------------------------------------
# Permission clamp — declared permissions are a ceiling, never widened.
# ---------------------------------------------------------------------------

async def test_declared_permissions_clamp_disabled_tools_before_the_agent_loop(monkeypatch):
    _patch_scheduler_deps(monkeypatch)
    tid = f"auto02-perm-{uuid.uuid4()}"
    set_task_policy(tid, permissions=["bash"])

    captured = {}

    async def _capture(*args, **kwargs):
        captured.update(kwargs)
        return "ok"

    sched = TaskScheduler(session_manager=None)
    sched._run_agent_loop = _capture
    await sched._execute_llm_task(_make_task(tid), db=None)

    disabled = captured.get("disabled_tools")
    assert disabled is not None
    assert "bash" not in disabled, "the one declared-permitted tool must stay offered"
    # Some other builtin tool the task did NOT declare must be clamped off —
    # this is the actual "ceiling, never widened" behavior under test.
    from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
    other_tools = set(BUILTIN_TOOL_DESCRIPTIONS.keys()) - {"bash"}
    assert other_tools <= disabled, "every non-declared tool must be disabled"


async def test_undeclared_task_keeps_the_historical_unrestricted_offer(monkeypatch):
    """Backward compat: a task that never called set_task_policy sees exactly
    the same disabled_tools as before AUTO-02 existed (empty, absent the
    crew/global settings this test already neutralizes)."""
    _patch_scheduler_deps(monkeypatch)
    tid = f"auto02-perm-undeclared-{uuid.uuid4()}"

    captured = {}

    async def _capture(*args, **kwargs):
        captured.update(kwargs)
        return "ok"

    sched = TaskScheduler(session_manager=None)
    sched._run_agent_loop = _capture
    await sched._execute_llm_task(_make_task(tid), db=None)

    assert captured.get("disabled_tools") in (None, set())


# ---------------------------------------------------------------------------
# Budget exhaustion — the agent loop stops mid-turn, closes the stream,
# notifies, and returns a checkpointed result.
# ---------------------------------------------------------------------------

class _TrackingAgen:
    """A stand-in for the async generator `stream_agent_loop` returns —
    tracks whether `aclose()` was actually called (not just "loop broke")
    and how much of the stream was left unconsumed at that point."""

    def __init__(self, events):
        self._events = list(events)
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)

    async def aclose(self):
        self.closed = True


def _tool_output_event(i):
    return "data: " + json.dumps({
        "type": "tool_output", "tool": "bash", "output": f"step {i} done",
    }) + "\n\n"


async def test_budget_exhaustion_stops_mid_turn_closes_the_stream_and_notifies(monkeypatch):
    tid = f"auto02-budget-{uuid.uuid4()}"
    set_task_policy(tid, budget_preset="supervised")

    tracking = _TrackingAgen([_tool_output_event(i) for i in range(5)])

    def _fake_stream_agent_loop(**kwargs):
        return tracking

    monkeypatch.setattr("src.agent_loop.stream_agent_loop", _fake_stream_agent_loop)
    monkeypatch.setattr("src.task_endpoint.resolve_task_candidates", lambda **kw: [])
    # A one-tool-call budget: the FIRST tool_output event already exhausts it.
    monkeypatch.setattr(
        "src.autonomy_budget.resolve_budget",
        lambda preset, get_setting=None, overrides=None: Budget(max_tool_calls=1),
    )

    sched = TaskScheduler(session_manager=None)
    result = await sched._run_agent_loop(
        "http://ep/v1", "model", _make_task(tid), "s",
    )

    assert result.startswith("[budget_exhausted:tool_calls]")
    assert '"note":' in result, "checkpoint JSON is embedded in the returned text"
    assert tracking.closed is True, "agen.aclose() must actually be called, not just break"
    assert len(tracking._events) > 0, (
        "the loop must stop BEFORE draining the stream naturally — otherwise "
        "this proves nothing about stopping early"
    )

    notifications = [n for n in sched._pending_notifications if n["status"] == "budget_exhausted"]
    assert len(notifications) == 1
    assert notifications[0]["task_id"] == tid
    assert notifications[0]["owner"] == "u1"


async def test_budget_not_exhausted_runs_the_stream_to_completion_normally(monkeypatch):
    """Control case: with a generous budget, nothing about the loop's normal
    completion path changes — no notification, no checkpoint prefix, the
    stream is exhausted naturally (aclose() is not needed and not called)."""
    tid = f"auto02-budget-ok-{uuid.uuid4()}"
    set_task_policy(tid, budget_preset="bounded_autonomous")

    tracking = _TrackingAgen([_tool_output_event(i) for i in range(2)])

    def _fake_stream_agent_loop(**kwargs):
        return tracking

    monkeypatch.setattr("src.agent_loop.stream_agent_loop", _fake_stream_agent_loop)
    monkeypatch.setattr("src.task_endpoint.resolve_task_candidates", lambda **kw: [])
    monkeypatch.setattr(
        "src.autonomy_budget.resolve_budget",
        lambda preset, get_setting=None, overrides=None: Budget(max_tool_calls=1000),
    )
    # Grace summarization would otherwise make a real LLM call once the
    # stream ends with no `full_text` — stub it out.
    async def _fake_task_llm_call_async(**kwargs):
        return "summary"
    monkeypatch.setattr("src.task_endpoint.task_llm_call_async", _fake_task_llm_call_async)

    sched = TaskScheduler(session_manager=None)
    result = await sched._run_agent_loop(
        "http://ep/v1", "model", _make_task(tid), "s",
    )

    assert not result.startswith("[budget_exhausted:")
    assert tracking.closed is False, "a naturally-finished stream is not explicitly closed again"
    assert len(tracking._events) == 0
    assert not [n for n in sched._pending_notifications if n["status"] == "budget_exhausted"]
