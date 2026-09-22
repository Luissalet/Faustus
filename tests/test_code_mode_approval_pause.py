"""Code Mode approval-pause (A12): a guest ``tools.call()`` that needs a
human's per-call approval PAUSES the running script and asks, instead of
just handing back a denial -- the guest subprocess is already blocked on
its own ``recv()`` for that exact call, so "pause" is simply the host
coroutine (``src.code_mode.bridge.dispatch_call``) waiting on a real
``question_store`` question before it replies.

Exercises the REAL subprocess round trip (``src.code_mode.runner.
run_code_mode`` spawns the real ``python -I src/code_mode/guest.py``
process), the same shape ``tests/acceptance/test_a10_code_mode_policy_gate.py``
and ``test_a11_code_mode_quotas.py`` use. A fake tool is registered into the
real ``TOOL_HANDLERS``/``TOOL_TAGS``/``ALWAYS_APPROVE_TOOLS`` so the test
does not depend on any real tool needing a real desktop.
"""
from __future__ import annotations

import asyncio
import time

import pytest

pytestmark = pytest.mark.asyncio

FAKE_TOOL = "fake_gated_tool"
OWNER = "luis"
SESSION = "code-mode-approval-session"


async def _fake_handler(content, ctx=None):
    return {"output": "gated tool ran", "exit_code": 0}


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    import src.constants as consts
    monkeypatch.setattr(consts, "DATA_DIR", str(tmp_path / "data"), raising=False)
    yield


@pytest.fixture(autouse=True)
def _register_fake_gated_tool(monkeypatch):
    import src.agent_tools as agent_tools_mod
    import src.tool_capabilities as tool_caps
    import src.tool_execution as tool_exec_mod

    monkeypatch.setattr(
        agent_tools_mod, "TOOL_TAGS", agent_tools_mod.TOOL_TAGS | {FAKE_TOOL}, raising=False,
    )
    monkeypatch.setitem(agent_tools_mod.TOOL_HANDLERS, FAKE_TOOL, _fake_handler)
    _patched_always_approve = frozenset(tool_caps.ALWAYS_APPROVE_TOOLS | {FAKE_TOOL})
    monkeypatch.setattr(tool_caps, "ALWAYS_APPROVE_TOOLS", _patched_always_approve, raising=False)
    # src/tool_execution.py imports ALWAYS_APPROVE_TOOLS by value at module
    # load time (`from src.tool_capabilities import ALWAYS_APPROVE_TOOLS`),
    # so its own name binding must be patched too -- the exact-approval
    # preconditions it checks (`_per_call_tool`) read THIS one, not
    # tool_capabilities's live attribute.
    monkeypatch.setattr(tool_exec_mod, "ALWAYS_APPROVE_TOOLS", _patched_always_approve, raising=False)
    yield


def _patch_settings(monkeypatch, **overrides):
    import src.settings as settings_mod
    from src.settings import DEFAULT_SETTINGS

    base = {
        # The approval gate is only consulted in "ask" mode, and this machine
        # -- or any machine running the app with auto-approve on -- has a real
        # settings row saying "auto". Pin it: this test is about what happens
        # AFTER a call is gated, not about whether the installation gates.
        "tool_approval_mode": "ask",
        "desktop_control_mode": "ask_each",
        "agent_code_mode_timeout_seconds": 20,
        "agent_code_mode_max_calls": 50,
        "agent_code_mode_max_output_bytes": 200_000,
        "agent_code_mode_pause_for_approval": True,
        "agent_code_mode_approval_wait_seconds": 20,
    }
    base.update(overrides)

    def _fast(key, default=None):
        if key in base:
            return base[key]
        return DEFAULT_SETTINGS.get(key, default)

    monkeypatch.setattr(settings_mod, "get_setting", _fast)
    # Same trap as ALWAYS_APPROVE_TOOLS above: a module that did
    # `from src.settings import get_setting` at import time holds its own
    # binding, and patching src.settings alone never reaches it. The gate
    # this test is about lives in one of those, so it read the real
    # installation's settings and the fake tool ran unblocked.
    import src.tool_capabilities as tool_caps
    monkeypatch.setattr(tool_caps, "get_setting", _fast, raising=False)


async def _wait_for_open_question(owner: str, *, timeout_s: float = 10.0) -> dict:
    from src import question_store

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        rows = [
            q for q in question_store.list_open(owner=owner)
            if q["session_id"].startswith("code_mode:")
        ]
        if rows:
            return rows[0]
        await asyncio.sleep(0.05)
    raise AssertionError("code_mode approval question never opened")


_SCRIPT = (
    "print('before')\n"
    "r = tools.call('fake_gated_tool', {'x': 1})\n"
    "print('after', r)\n"
)


@pytest.mark.asyncio
async def test_approve_runs_the_call_once_and_the_script_continues(monkeypatch):
    _patch_settings(monkeypatch)
    from src.code_mode.runner import run_code_mode
    from src import question_store

    task = asyncio.ensure_future(
        run_code_mode(_SCRIPT, session_id=SESSION, owner=OWNER)
    )
    try:
        opened = await _wait_for_open_question(OWNER)
        assert FAKE_TOOL in opened["question"]
        resolution = question_store.resolve_question(
            opened["question_id"], {"option_ids": ["approve"]}, owner=OWNER,
        )
        assert resolution["ok"] is True
        result = await asyncio.wait_for(task, timeout=15)
    finally:
        if not task.done():
            task.cancel()

    assert result.get("exit_code") == 0, result
    output = result.get("output") or ""
    assert output.count("before") == 1, output
    assert "gated tool ran" in output
    assert result.get("result_chars") == len(output)
    approvals = result.get("approvals") or []
    assert len(approvals) == 1
    assert approvals[0]["tool"] == FAKE_TOOL
    assert approvals[0]["decision"] == "approve"
    assert approvals[0]["waited_ms"] >= 0


@pytest.mark.asyncio
async def test_deny_returns_a_clean_denial_and_the_script_continues(monkeypatch):
    _patch_settings(monkeypatch)
    from src.code_mode.runner import run_code_mode
    from src import question_store

    task = asyncio.ensure_future(
        run_code_mode(_SCRIPT, session_id=SESSION, owner=OWNER)
    )
    try:
        opened = await _wait_for_open_question(OWNER)
        resolution = question_store.resolve_question(
            opened["question_id"], {"option_ids": ["deny"]}, owner=OWNER,
        )
        assert resolution["ok"] is True
        result = await asyncio.wait_for(task, timeout=15)
    finally:
        if not task.done():
            task.cancel()

    assert result.get("exit_code") == 0, result
    output = result.get("output") or ""
    assert output.count("before") == 1
    assert "declined by user" in output
    approvals = result.get("approvals") or []
    assert len(approvals) == 1 and approvals[0]["decision"] == "deny"


@pytest.mark.asyncio
async def test_timeout_denies_and_cancels_the_question(monkeypatch):
    _patch_settings(
        monkeypatch,
        agent_code_mode_timeout_seconds=10,
        agent_code_mode_approval_wait_seconds=1,
    )
    from src.code_mode.runner import run_code_mode
    from src import question_store

    result = await asyncio.wait_for(
        run_code_mode(_SCRIPT, session_id=SESSION, owner=OWNER), timeout=15,
    )

    assert result.get("exit_code") == 0, result
    output = result.get("output") or ""
    assert "timed out" in output
    approvals = result.get("approvals") or []
    assert len(approvals) == 1 and approvals[0]["decision"] == "timeout"

    rows = question_store.list_open(owner=OWNER)
    assert rows == [], "a timed-out approval question must not stay open"


@pytest.mark.asyncio
async def test_wall_timeout_is_not_consumed_by_the_approval_wait(monkeypatch):
    # The script's own wall-time budget is smaller than how long the human
    # takes to answer -- without pausing the clock this would be killed as
    # terminated_by="timeout" before the approval could ever be granted.
    _patch_settings(
        monkeypatch,
        agent_code_mode_timeout_seconds=1,
        agent_code_mode_approval_wait_seconds=20,
    )
    from src.code_mode.runner import run_code_mode
    from src import question_store

    task = asyncio.ensure_future(
        run_code_mode(_SCRIPT, session_id=SESSION, owner=OWNER)
    )
    try:
        opened = await _wait_for_open_question(OWNER)
        await asyncio.sleep(2.0)  # already past the 1s script wall-time budget
        resolution = question_store.resolve_question(
            opened["question_id"], {"option_ids": ["approve"]}, owner=OWNER,
        )
        assert resolution["ok"] is True
        result = await asyncio.wait_for(task, timeout=15)
    finally:
        if not task.done():
            task.cancel()

    assert result.get("terminated") is not True, result
    assert result.get("exit_code") == 0, result
    assert "gated tool ran" in (result.get("output") or "")


@pytest.mark.asyncio
async def test_destructive_guard_denial_is_never_turned_into_a_question(monkeypatch):
    # A hard policy denial (the destructive-command guard) must stay a plain
    # rejection in Code Mode -- never a question a script can sit and wait
    # to have blessed. Same script/settings shape as the approvable case,
    # just a real bash tool instead of the fake gated one.
    _patch_settings(monkeypatch)
    from src.code_mode.runner import run_code_mode
    from src import question_store

    code = "r = tools.call('bash', {'command': 'rm -rf /tmp/a12_target'})\nprint(r)\n"
    result = await asyncio.wait_for(
        run_code_mode(code, session_id=SESSION, owner=OWNER), timeout=15,
    )
    assert result.get("exit_code") == 0, result
    assert "Destructive command" in (result.get("output") or "")
    assert not (result.get("approvals") or [])
    assert question_store.list_open(owner=OWNER) == []


@pytest.mark.asyncio
async def test_decision_from_answer_accepts_option_ids_and_text_case_insensitively():
    from src.code_mode.bridge import _decision_from_answer

    # option_ids: the shape both POST /api/chat (question_id) and the new
    # POST /api/questions/{id}/answer route store when the tray's option
    # buttons are clicked.
    assert _decision_from_answer({"option_ids": ["approve"]}) == "approve"
    assert _decision_from_answer({"option_ids": ["Approve"]}) == "approve"
    assert _decision_from_answer({"option_ids": ["DENY"]}) == "deny"
    assert _decision_from_answer({"option_ids": []}) == "deny"
    # Free-typed text, case-insensitive.
    assert _decision_from_answer({"text": "approve"}) == "approve"
    assert _decision_from_answer({"text": "APPROVE"}) == "approve"
    assert _decision_from_answer({"text": "deny"}) == "deny"
    assert _decision_from_answer({"text": "sure, go ahead"}) == "deny"
    # option_ids wins over a stray text value.
    assert _decision_from_answer({"text": "deny", "option_ids": ["approve"]}) == "approve"
    # Anything unparseable is a deny, never a silent approve.
    assert _decision_from_answer({}) == "deny"
    assert _decision_from_answer(None) == "deny"


@pytest.mark.asyncio
async def test_tools_list_reports_requires_approval(monkeypatch):
    _patch_settings(monkeypatch)
    from src.code_mode import bridge

    rows = {row["name"]: row for row in bridge.list_tools()}
    assert rows[FAKE_TOOL]["requires_approval"] is True
    assert rows["read_file"]["requires_approval"] is False


@pytest.mark.asyncio
async def test_setting_off_keeps_old_behaviour(monkeypatch):
    _patch_settings(monkeypatch, agent_code_mode_pause_for_approval=False)
    from src.code_mode.runner import run_code_mode
    from src import question_store

    result = await asyncio.wait_for(
        run_code_mode(_SCRIPT, session_id=SESSION, owner=OWNER), timeout=15,
    )
    assert result.get("exit_code") == 0, result
    output = result.get("output") or ""
    assert "'blocked': True" in output
    assert not (result.get("approvals") or [])
    assert question_store.list_open(owner=OWNER) == []
