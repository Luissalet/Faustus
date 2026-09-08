"""Deep Research waits for the model to load; it does not race it.

08-09-2026: a research on a local 29 GB model died before round one with
"Probe failed … timed out after 1 attempts". Nothing was broken — the weights
were still being read off disk when the fixed 15s pre-flight budget expired.
The chat never had that problem because it waits (LLMConfig.STREAM_TIMEOUT).
And when it did fail, the reason never reached the screen: it was parked in
the entry's `result`, so /api/research/result served the failure text as if it
were the report, cleared it, and the next call 404'd.
"""
import asyncio

import pytest

from src.llm_core import LLMConfig
from src.research_handler import ResearchHandler

LOCAL = "http://127.0.0.1:11434/v1/chat/completions"
REMOTE = "https://api.openai.com/v1/chat/completions"


@pytest.fixture
def budget_default(monkeypatch):
    """No saved setting: the default for the endpoint kind is what applies."""
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: default)


@pytest.fixture
def probe_call(monkeypatch):
    """Capture the single pre-flight call instead of making it."""
    seen = {}

    async def _call(**kwargs):
        seen.update(kwargs)
        return "hi"

    monkeypatch.setattr("src.llm_core.llm_call_async", _call)
    return seen


def test_local_model_gets_the_chat_s_patience(budget_default, probe_call):
    events = []
    asyncio.run(ResearchHandler._probe_endpoint(LOCAL, "qwen3.8:27b-q8_0", None, events.append))

    assert probe_call["timeout"] == LLMConfig.STREAM_TIMEOUT
    assert events == [{"phase": "loading_model", "model": "qwen3.8:27b-q8_0"}]


def test_remote_endpoint_keeps_a_short_budget(budget_default, probe_call):
    events = []
    asyncio.run(ResearchHandler._probe_endpoint(REMOTE, "gpt-4o", {"Authorization": "x"}, events.append))

    assert probe_call["timeout"] == 60
    assert events == [{"phase": "probing", "model": "gpt-4o"}]


def test_the_budget_is_a_setting(monkeypatch, probe_call):
    monkeypatch.setattr(
        "src.settings.get_setting",
        lambda key, default=None: 900 if key == "research_model_load_timeout_seconds" else default,
    )

    asyncio.run(ResearchHandler._probe_endpoint(LOCAL, "qwen3.8:27b-q8_0"))

    assert probe_call["timeout"] == 900


def test_an_absurd_budget_is_bounded(monkeypatch, probe_call):
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: 999999)

    asyncio.run(ResearchHandler._probe_endpoint(LOCAL, "m"))

    assert probe_call["timeout"] == 7200


@pytest.mark.asyncio
async def test_a_failure_reaches_the_screen_and_is_never_served_as_a_report(monkeypatch):
    handler = ResearchHandler()

    async def _boom(*args, **kwargs):
        raise RuntimeError("Model 'qwen3.8:27b-q8_0' probe failed: timed out")

    monkeypatch.setattr(handler, "call_research_service", _boom)
    handler.start_research("rp-load-test", "why did it fail", LOCAL, "qwen3.8:27b-q8_0")
    entry = handler._active_tasks["rp-load-test"]
    await entry["task"]

    assert entry["status"] == "error"
    # The reason travels in `error`; `result` stays empty so /api/research/
    # result answers 404 instead of handing the failure text out as a report
    # (and clearing it, which turned every later call into a 404).
    assert entry["result"] is None
    assert handler.get_result("rp-load-test") is None
    assert "probe failed" in entry["error"]
    # …and it is announced, which is what puts it on the screen.
    assert entry["progress"]["phase"] == "error"
    assert "probe failed" in entry["progress"]["message"]
