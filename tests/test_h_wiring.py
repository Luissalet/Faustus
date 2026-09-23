"""tests/test_h_wiring.py — lot H's integrator-file cabling.

`src/lifecycle_hooks.py` and `routes/lifecycle_hooks_routes.py` are this
lot's own files and are fully tested in `tests/test_lifecycle_hooks.py`.
Wiring them into `app.py`, `src/agent_loop.py` and `routes/chat_helpers.py`
is the integrator's job (COMMON.md — those files are off limits to this
lot); the exact diff is in `H_wiring.md`. These assertions describe the
wired behaviour and are expected to fail until the integrator applies it —
`strict=True` means this file will start failing (loudly) the moment the
wiring lands, as the signal to delete the `xfail` mark.
"""
from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _read(relpath: str) -> str:
    return (_REPO_ROOT / relpath).read_text(encoding="utf-8")


def test_app_registers_the_lifecycle_hooks_router():
    src = _read("app.py")
    assert "from routes.lifecycle_hooks_routes import setup_lifecycle_hooks_routes" in src
    assert "app.include_router(setup_lifecycle_hooks_routes())" in src


def test_agent_loop_runs_session_start_and_turn_start_hooks():
    src = _read("src/agent_loop.py")
    assert "lifecycle_hooks" in src
    assert 'run_async("turn_start"' in src or "_hook_events" in src


def test_agent_loop_runs_pre_tool_and_post_tool_hooks():
    src = _read("src/agent_loop.py")
    assert 'run_async("pre_tool"' in src
    assert 'run_async("post_tool"' in src
    assert "attach_to_result" in src


def test_agent_loop_runs_pre_compact_hooks():
    src = _read("src/agent_loop.py")
    assert 'run_async("pre_compact"' in src


def test_chat_helpers_runs_turn_end_hooks():
    src = _read("routes/chat_helpers.py")
    assert "lifecycle_hooks" in src
    assert 'run_async("turn_end"' in src
