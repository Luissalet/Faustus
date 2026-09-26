"""PENDIENTES §90 (20-09): keep_tool_rounds scales with the model's real
context window instead of a flat 6, so a large-context model does not spill
tool rounds it has plenty of room to keep (which forced the model to burn
rounds re-running tools, e.g. re-locating the workspace root, to reacquire
data that was still comfortably within its budget).
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import MagicMock

import pytest

for mod in [
    "sqlalchemy", "sqlalchemy.orm", "sqlalchemy.ext", "sqlalchemy.ext.declarative",
    "sqlalchemy.ext.hybrid", "sqlalchemy.sql", "sqlalchemy.sql.expression",
    "src.database",
    "core.models", "core.database",
]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

from src import context_compactor as cc
from src import context_overflow as co
from src.context_compactor import (
    DEFAULT_MIDTURN_KEEP_TOOL_ROUNDS,
    MIDTURN_KEEP_ROUNDS_BASE_CONTEXT,
    MIDTURN_KEEP_ROUNDS_HARD_MAX,
    scale_keep_tool_rounds,
)


# ---------------------------------------------------------------------------
# Pure function
# ---------------------------------------------------------------------------

def test_small_context_model_is_unchanged():
    # At or below the tuned baseline (32K), the flat default is kept exactly.
    assert scale_keep_tool_rounds(6, 8_000) == 6
    assert scale_keep_tool_rounds(6, 32_000) == 6


def test_large_context_model_scales_up():
    # 64K is 2x the 32K baseline -> twice the rounds; 200K reaches the cap.
    assert scale_keep_tool_rounds(6, 64_000) == int(6 * (64_000 / MIDTURN_KEEP_ROUNDS_BASE_CONTEXT))
    scaled = scale_keep_tool_rounds(6, 200_000)
    assert DEFAULT_MIDTURN_KEEP_TOOL_ROUNDS < scaled <= MIDTURN_KEEP_ROUNDS_HARD_MAX


def test_huge_context_is_capped_at_hard_max():
    assert scale_keep_tool_rounds(6, 10_000_000) == MIDTURN_KEEP_ROUNDS_HARD_MAX


def test_explicit_operator_setting_is_never_overridden():
    # A deliberately-chosen non-default value is honoured exactly, at any window.
    assert scale_keep_tool_rounds(1, 200_000) == 1
    assert scale_keep_tool_rounds(20, 32_000) == 20


def test_invalid_inputs_fall_back_cleanly():
    assert scale_keep_tool_rounds("bad", "also bad") == DEFAULT_MIDTURN_KEEP_TOOL_ROUNDS
    assert scale_keep_tool_rounds(None, 200_000) == scale_keep_tool_rounds(6, 200_000)


# ---------------------------------------------------------------------------
# Integration: apply_midturn_pressure keeps more real tool rounds verbatim
# on a large-context model than on a small one, given the same history.
# ---------------------------------------------------------------------------

@pytest.fixture()
def overflow_root(tmp_path, monkeypatch):
    root = tmp_path / "context_overflow"
    monkeypatch.setattr(co, "OVERFLOW_DIR", str(root))
    return root


def _tool_round(call_id: str, body: str) -> list:
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": call_id,
                "type": "function",
                "function": {"name": "bash", "arguments": "{}"},
            }],
        },
        {"role": "tool", "tool_call_id": call_id, "content": body},
    ]


def _fake_long_history(rounds: int = 20) -> list:
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "find the workspace root and keep building"},
    ]
    for i in range(rounds):
        # A realistic-sized tool round (a directory listing / grep output),
        # not fat enough to be spilled purely for oversize.
        body = f"round {i}: /workspace/project/src\n" + ("line of output\n" * 150)
        messages.extend(_tool_round(f"call_{i}", body))
    return messages


def _settings(**overrides):
    base = {
        "agent_midturn_compact_enabled": True,
        "agent_midturn_compact_pct": 0.40,
        "agent_midturn_keep_tool_rounds": DEFAULT_MIDTURN_KEEP_TOOL_ROUNDS,
        "agent_midturn_spill_chars": 1_000_000,  # nothing spills purely for size
    }
    base.update(overrides)

    def _get(key, default=None):
        return base[key] if key in base else default

    return _get


def _spilled_round_count(out: list) -> int:
    return sum(
        1 for m in out
        if m.get("role") == "tool" and "[overflow id=" in str(m.get("content") or "")
    )


def test_large_window_keeps_more_history_than_small_window(monkeypatch, overflow_root):
    history = _fake_long_history(rounds=20)

    async def fake_maybe_compact(session, url, model, messages, headers=None, owner=None, **kw):
        return messages, 0, False

    monkeypatch.setattr(cc, "maybe_compact", fake_maybe_compact)
    monkeypatch.setattr(cc, "compact_with_integrity", lambda messages, **kw: (list(messages), []))
    monkeypatch.setattr(cc, "get_setting", _settings())

    # Small-context model (32K, at the tuned baseline): unchanged, flat 6.
    monkeypatch.setattr(cc, "get_context_length", lambda *a, **k: 32_000)
    out_small, report_small = asyncio.run(cc.apply_midturn_pressure(
        list(history), endpoint_url="http://local/v1", model="small-ctx",
        session_id="s-small", round_num=20,
    ))
    assert report_small["keep_tool_rounds"] == DEFAULT_MIDTURN_KEEP_TOOL_ROUNDS

    # Large-context model (200K): keeps proportionally more rounds verbatim.
    monkeypatch.setattr(cc, "get_context_length", lambda *a, **k: 200_000)
    out_large, report_large = asyncio.run(cc.apply_midturn_pressure(
        list(history), endpoint_url="http://local/v1", model="large-ctx",
        session_id="s-large", round_num=20,
    ))
    assert report_large["keep_tool_rounds"] > report_small["keep_tool_rounds"]

    spilled_small = _spilled_round_count(out_small)
    spilled_large = _spilled_round_count(out_large)
    assert spilled_small > spilled_large, (
        "the 200K-context run should spill fewer rounds to overflow than the "
        "32K-context run given the identical history"
    )
