"""Lot D wiring guard — xfail(strict) until the integrator applies the two
diffs in D_wiring.md. Turns green the moment both are wired.
"""
from __future__ import annotations

import inspect

import pytest


def test_chat_helpers_records_fix_memory():
    from routes import chat_helpers

    src = inspect.getsource(chat_helpers)
    assert "fix_memory.record_from_turn" in src


def test_agent_loop_injects_fix_memory_prompt_block():
    from src import agent_loop

    src = inspect.getsource(agent_loop)
    assert "_fixmem.prompt_block" in src or "fix_memory.prompt_block" in src
