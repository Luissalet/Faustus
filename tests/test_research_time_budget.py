"""The rounds' time budget is sized for the endpoint, not fixed at 300 s.

The Research screen never sent max_time, so every run got the route's
default of 300 s. A cloud model answers in seconds and that was fine; a
local 27B needs 4-5 minutes per round, so the screen's runs got one round
and a report from nothing ("Rounds: 2 · URLs Analyzed: 0", 08-09-2026).
Unset now means 60 % of the run's wall clock; a local endpoint's wall clock
is itself research_local_time_multiplier (3) times the setting.
"""
import asyncio

import pytest

from src.research_handler import ResearchHandler

LOCAL = "http://127.0.0.1:11434/v1/chat/completions"
REMOTE = "https://api.openai.com/v1/chat/completions"


@pytest.fixture
def capture(monkeypatch):
    """Capture what start_research hands to the engine instead of running it."""
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: default)
    monkeypatch.setattr("src.research_handler._research_json_path", lambda sid: f"/tmp/{sid}.json")
    seen = {}

    async def _service(self, query, endpoint, model, **kw):
        seen.update(kw)
        seen["endpoint"] = endpoint
        return "report"
    monkeypatch.setattr(ResearchHandler, "call_research_service", _service)
    monkeypatch.setattr(ResearchHandler, "_save_result", lambda self, sid, entry: None)
    return seen


def _start(handler, endpoint, **kw):
    async def _go():
        handler.start_research("rp-budget", "q", endpoint, "m", **kw)
        await handler._active_tasks["rp-budget"]["task"]
    asyncio.run(_go())


def test_a_local_endpoint_gets_most_of_a_tripled_wall_clock(capture):
    _start(ResearchHandler(), LOCAL)
    # 1800 s setting × 3 for a local model = 5400 s wall clock; 60 % of it for the rounds
    assert capture["max_time"] == 3240


def test_a_remote_endpoint_gets_most_of_the_plain_wall_clock(capture):
    _start(ResearchHandler(), REMOTE)
    assert capture["max_time"] == 1080


def test_an_explicit_budget_is_kept(capture):
    _start(ResearchHandler(), LOCAL, max_time=300)
    assert capture["max_time"] == 300


def test_the_screen_s_minutes_reach_the_engine(capture):
    _start(ResearchHandler(), LOCAL, max_time=45 * 60)
    assert capture["max_time"] == 2700
