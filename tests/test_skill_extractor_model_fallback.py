"""The background skill extractor asked Ollama for a configured model that
was not actually installed there (404) and simply gave up. It must instead
try the same fallback chain other background helpers use (configured task
model, then utility/default, then the model already carrying the
conversation) -- and skip quietly, with a log line, when nothing in that
chain is usable at all."""

import logging

import pytest

from services.memory import skill_extractor
from src import task_endpoint


class _FakeSession:
    session_id = "s1"

    def get_context_messages(self):
        return [
            {"role": "user", "content": "Walk me through deploying the service"},
            {"role": "assistant", "content": "Sure, here's the runbook..."},
        ]


class _FakeSkillsManager:
    def __init__(self):
        self.added = []

    def load(self, owner=None):
        return []

    def add_skill(self, **kwargs):
        self.added.append(kwargs)
        return {"id": "skill-1", **kwargs}


_GOOD_RESPONSE = (
    '{"title": "Deploy runbook", "problem": "manual deploys are error-prone", '
    '"solution": "use the deploy script", "steps": ["build", "push", "restart"], '
    '"tags": ["deploy"], "confidence": 0.9}'
)


async def test_falls_back_past_a_model_that_is_not_installed(monkeypatch):
    """The configured task model 404s (not installed on Ollama); the
    extractor must still succeed by trying the next candidate, instead of
    dropping the whole extraction."""
    monkeypatch.setattr(
        task_endpoint, "resolve_task_candidates",
        lambda **kw: [
            ("http://ollama/v1", "qwen3.5:9b", {}),          # not installed -> 404
            ("http://ollama/v1", "qwen3-coder:30b", {}),      # actually installed
        ],
    )

    calls = []

    async def fake_llm_call_async(url, model, messages, **kwargs):
        calls.append(model)
        if model == "qwen3.5:9b":
            raise RuntimeError("404: model 'qwen3.5:9b' not found")
        return _GOOD_RESPONSE

    monkeypatch.setattr("src.llm_core.llm_call_async", fake_llm_call_async)

    skills_manager = _FakeSkillsManager()
    entry = await skill_extractor.maybe_extract_skill(
        _FakeSession(),
        skills_manager,
        endpoint_url="http://ollama/v1",
        model="qwen3.5:9b",
        headers={},
        round_count=3,
        tool_count=3,
        owner="alice",
    )

    assert calls == ["qwen3.5:9b", "qwen3-coder:30b"]
    assert entry is not None and entry["title"] == "Deploy runbook"
    assert skills_manager.added


async def test_skips_quietly_when_nothing_is_available(monkeypatch, caplog):
    """No candidate at all (nothing configured, nothing installed): the
    extractor must return None without raising and without a scary
    traceback-level warning."""
    monkeypatch.setattr(task_endpoint, "resolve_task_candidates", lambda **kw: [])

    async def fake_llm_call_async(*args, **kwargs):
        raise AssertionError("must not be called when there is no candidate")

    monkeypatch.setattr("src.llm_core.llm_call_async", fake_llm_call_async)

    skills_manager = _FakeSkillsManager()
    with caplog.at_level(logging.INFO, logger="services.memory.skill_extractor"):
        entry = await skill_extractor.maybe_extract_skill(
            _FakeSession(),
            skills_manager,
            endpoint_url="http://ollama/v1",
            model="qwen3.5:9b",
            headers={},
            round_count=3,
            tool_count=3,
            owner="alice",
        )

    assert entry is None
    assert not skills_manager.added
    assert any(r.levelno == logging.INFO and "skipping" in r.message for r in caplog.records)
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)
