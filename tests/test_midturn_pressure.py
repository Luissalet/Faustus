"""Tests for mid-turn context pressure (spill + integrity + optional LLM)."""

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


@pytest.fixture()
def overflow_root(tmp_path, monkeypatch):
    root = tmp_path / "context_overflow"
    monkeypatch.setattr(co, "OVERFLOW_DIR", str(root))
    return root


def _fat_tool(call_id: str, n: int = 20000) -> dict:
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "content": ("RESULT LINE\n" * n) + f"path=src/foo/{call_id}.py",
    }


def _assistant_with_tool(call_id: str, name: str = "bash") -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": "{}"},
        }],
    }


def _settings(**overrides):
    base = {
        "agent_midturn_compact_enabled": True,
        "agent_midturn_compact_pct": 0.70,
        "agent_midturn_keep_tool_rounds": 6,
        "agent_midturn_spill_chars": 8000,
    }
    base.update(overrides)

    def _get(key, default=None):
        return base[key] if key in base else default

    return _get


def test_under_threshold_is_noop(monkeypatch, overflow_root):
    monkeypatch.setattr(cc, "get_context_length", lambda *a, **k: 200_000)
    monkeypatch.setattr(cc, "get_setting", _settings())
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        _assistant_with_tool("c1"),
        _fat_tool("c1", n=10),
    ]

    out, report = asyncio.run(cc.apply_midturn_pressure(
        messages,
        endpoint_url="http://local/v1",
        model="m",
        session_id="s",
        round_num=2,
    ))
    assert report["changed"] is False
    assert report["spilled"] == 0
    assert report.get("skipped") == "under_threshold"


def test_spills_old_fat_tool_results(monkeypatch, overflow_root):
    monkeypatch.setattr(cc, "get_context_length", lambda *a, **k: 8_000)
    monkeypatch.setattr(cc, "get_setting", _settings(
        agent_midturn_keep_tool_rounds=1,
        agent_midturn_spill_chars=500,
    ))

    async def fake_maybe_compact(session, url, model, messages, headers=None, owner=None, **kw):
        return messages, 8000, False

    monkeypatch.setattr(cc, "maybe_compact", fake_maybe_compact)
    monkeypatch.setattr(
        cc, "compact_with_integrity",
        lambda messages, **kw: (list(messages), []),
    )

    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "do the work"},
    ]
    for i in range(4):
        cid = f"call_{i}"
        messages.append(_assistant_with_tool(cid, name="write_file"))
        messages.append(_fat_tool(cid, n=800))

    before = cc.estimate_tokens(messages)
    out, report = asyncio.run(cc.apply_midturn_pressure(
        messages,
        endpoint_url="http://local/v1",
        model="m",
        session_id="sess-mid",
        round_num=5,
        owner="luis",
    ))
    assert report["changed"] is True
    assert report["spilled"] >= 1
    assert report["tokens_after"] < before
    tool_msgs = [m for m in out if m.get("role") == "tool"]
    spilled = [m for m in tool_msgs if "[overflow id=" in str(m.get("content") or "")]
    assert spilled, "expected at least one overflow stub"
    assert (overflow_root / "sess-mid").exists()
    assert any((overflow_root / "sess-mid").glob("*.json"))


def test_incognito_spills_without_disk(monkeypatch, overflow_root):
    monkeypatch.setattr(cc, "get_context_length", lambda *a, **k: 4_000)
    monkeypatch.setattr(cc, "get_setting", _settings(
        agent_midturn_compact_pct=0.50,
        agent_midturn_keep_tool_rounds=0,
        agent_midturn_spill_chars=100,
    ))

    async def fake_maybe_compact(session, url, model, messages, headers=None, owner=None, **kw):
        return messages, 4000, False

    monkeypatch.setattr(cc, "maybe_compact", fake_maybe_compact)
    monkeypatch.setattr(cc, "compact_with_integrity", lambda messages, **kw: (list(messages), []))

    messages = [
        {"role": "user", "content": "x"},
        _assistant_with_tool("c0"),
        _fat_tool("c0", n=400),
        _assistant_with_tool("c1"),
        _fat_tool("c1", n=400),
    ]

    out, report = asyncio.run(cc.apply_midturn_pressure(
        messages,
        endpoint_url="http://local/v1",
        model="m",
        session_id="incog",
        round_num=2,
        durable_overflow=False,
    ))
    assert report["spilled"] >= 1
    assert not (overflow_root / "incog").exists()
    assert any("[overflow" in str(m.get("content") or "") for m in out if m.get("role") == "tool")


def test_disabled_setting_skips(monkeypatch, overflow_root):
    monkeypatch.setattr(cc, "get_setting", _settings(agent_midturn_compact_enabled=False))
    messages = [{"role": "user", "content": "hi"}, _assistant_with_tool("c"), _fat_tool("c")]

    out, report = asyncio.run(cc.apply_midturn_pressure(
        messages, endpoint_url="u", model="m", session_id="s", round_num=1,
    ))
    assert report["changed"] is False
    assert out == messages
    assert report.get("skipped") == "disabled"
