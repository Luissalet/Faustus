"""reach_read / reach_search / reach_doctor tool wiring + execution (R1)."""
from __future__ import annotations

import asyncio
import json

import pytest

from src.agent_tools import TOOL_HANDLERS, TOOL_TAGS
from src.agent_tools.reach_tools import ReachReadTool, ReachSearchTool, ReachDoctorTool
from src.reach.base import ReachResult


def test_reach_tools_are_registered_everywhere_expected():
    for name in ("reach_read", "reach_search", "reach_doctor"):
        assert name in TOOL_HANDLERS
        assert name in TOOL_TAGS


def test_reach_read_tool_returns_output_and_attempts(monkeypatch):
    async def fake_read(url, channel=None, ctx=None, **kw):
        return ReachResult(
            channel="github", backend="github_api", url=url, title="A Repo", text="repo body",
            source_trust="public_api", attempts=[{"backend": "github_api", "ok": True, "reason": ""}],
        )

    import src.agent_tools.reach_tools as rt
    monkeypatch.setattr("src.reach.router.read", fake_read)

    result = asyncio.run(ReachReadTool().execute(json.dumps({"url": "github.com/a/b"}), {}))
    assert result["exit_code"] == 0
    assert "repo body" in result["output"]
    assert result["backend"] == "github_api"
    assert result["untrusted_content"] is True
    assert result["attempts"][0]["ok"] is True


def test_reach_read_tool_reports_error_when_no_backend_worked(monkeypatch):
    async def fake_read(url, channel=None, ctx=None, **kw):
        return ReachResult(channel="web", backend="", url=url, error="every backend failed",
                            attempts=[{"backend": "faustus_web_fetch", "ok": False, "reason": "boom"}])

    monkeypatch.setattr("src.reach.router.read", fake_read)
    result = asyncio.run(ReachReadTool().execute("example.com", {}))
    assert result["exit_code"] == 1
    assert "every backend failed" in result["error"]


def test_reach_read_tool_requires_url():
    result = asyncio.run(ReachReadTool().execute("{}", {}))
    assert result["exit_code"] == 1


def test_reach_search_tool_aggregates_by_channel(monkeypatch):
    async def fake_search(query, channels=None, limit=10, ctx=None, **kw):
        return {
            "github": [ReachResult(channel="github", backend="github_api", url="u1", title="hit1", text="snippet")],
        }

    monkeypatch.setattr("src.reach.router.search", fake_search)
    result = asyncio.run(ReachSearchTool().execute(json.dumps({"query": "faustus", "channels": ["github"]}), {}))
    assert result["exit_code"] == 0
    assert "hit1" in result["output"]
    assert result["channels"] == ["github"]


def test_reach_doctor_tool_reports_summary(monkeypatch):
    async def fake_doctor(live=False):
        return {
            "channels": {"web": {"status": "ready", "active_backend": "faustus_web_fetch",
                                  "backends": [{"name": "faustus_web_fetch", "status": "ready", "reason": ""}]}},
            "ready_count": 1, "total_count": 1, "summary": "1/1 channels ready", "live": live,
        }

    monkeypatch.setattr("src.reach.doctor.doctor", fake_doctor)
    result = asyncio.run(ReachDoctorTool().execute("{}", {}))
    assert result["exit_code"] == 0
    assert "1/1 channels ready" in result["output"]
    assert result["ready_count"] == 1


def test_reach_doctor_tool_parses_live_flag(monkeypatch):
    captured = {}

    async def fake_doctor(live=False):
        captured["live"] = live
        return {"channels": {}, "ready_count": 0, "total_count": 0, "summary": "0/0", "live": live}

    monkeypatch.setattr("src.reach.doctor.doctor", fake_doctor)
    asyncio.run(ReachDoctorTool().execute(json.dumps({"live": True}), {}))
    assert captured["live"] is True
