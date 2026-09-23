"""Wiring of typed decisions into their three advisory callers: the
freshness check on a chat turn (src/freshness.py), the brain's entity typing
pass (src/brain/extract.py) and the memory-conflict suggestions
(src/memory_conflicts.py, run by the `memory_conflict_advice` maintenance
task). Same fakes as tests/test_typed_decision.py: no network.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import typed_decision as td  # noqa: E402
from tests.test_typed_decision import (  # noqa: E402,F401
    MODEL, _lp, endpoint, ollama_body, openai_body, run, serve, settings,
)


# ── wiring: freshness ───────────────────────────────────────────────────────

def test_freshness_rule_confident_makes_no_model_call(monkeypatch, endpoint):
    from src import freshness
    server = serve(monkeypatch, lambda p: (200, openai_body([{"token": "B", "logprob": -0.01}])))
    for text in ["¿Quién ganó el partido de ayer?", "What is the price of bitcoin?",
                 "Explain recursion", "2+2?", "hola", "What's on my calendar today?"]:
        before = freshness.looks_time_sensitive(text)
        out = run(freshness.decide_freshness(text))
        assert out["source"] == "rule" and out["decision"] is None
        assert out["time_sensitive"] == before
    assert server.requests == [] and endpoint["residency_calls"] == 0


def test_freshness_uncertain_uses_the_decision(monkeypatch, endpoint):
    from src import freshness
    server = serve(monkeypatch, lambda p: (200, openai_body(
        [{"token": "A", "logprob": _lp(0.93)}, {"token": "B", "logprob": _lp(0.05)}])))
    text = "¿Quién dirige ahora el club?"
    assert freshness.looks_time_sensitive(text) is False
    out = run(freshness.decide_freshness(text))
    assert out["source"] == "typed_decision" and out["time_sensitive"] is True
    assert out["decision"]["value"] == "yes" and out["decision"]["confidence"] > 0.9
    assert len(server.requests) == 1
    # a weak time word the rule would have searched for: a confident "no" wins
    serve(monkeypatch, lambda p: (200, openai_body(
        [{"token": "B", "logprob": _lp(0.95)}, {"token": "A", "logprob": _lp(0.02)}], content="B")))
    text = "¿Qué significa el último párrafo del poema?"
    assert freshness.looks_time_sensitive(text) is True
    out = run(freshness.decide_freshness(text))
    assert out["time_sensitive"] is False and out["source"] == "typed_decision"


def test_freshness_unavailable_or_unsure_keeps_the_rule(monkeypatch, endpoint, settings):
    from src import freshness
    endpoint["resident"] = []
    server = serve(monkeypatch, lambda p: (200, openai_body([{"token": "A", "logprob": -0.01}])))
    text = "¿Cuál es la última novedad?"
    out = run(freshness.decide_freshness(text))
    assert out["source"] == "rule" and out["time_sensitive"] is True
    assert out["decision"]["method"] == "unavailable" and server.requests == []
    endpoint["resident"] = [MODEL]
    serve(monkeypatch, lambda p: (200, openai_body(
        [{"token": "A", "logprob": _lp(0.5)}, {"token": "B", "logprob": _lp(0.45)}])))
    out = run(freshness.decide_freshness(text))
    assert out["source"] == "rule" and out["decision"]["reason"] == "low_confidence"
    serve(monkeypatch, lambda p: (200, {"choices": [{"message": {"content": "B"}}]}))
    out = run(freshness.decide_freshness(text))
    assert out["source"] == "rule" and out["decision"]["method"] == "letter"
    settings["typed_decision_freshness"] = False
    server = serve(monkeypatch, lambda p: (200, openai_body([{"token": "B", "logprob": -0.01}])))
    out = run(freshness.decide_freshness(text))
    assert out["decision"] is None and server.requests == []


def test_freshness_assessment_never_changes_the_rule_verdict():
    from src import freshness
    samples = ["", "   ", "¿Ganó el Madrid?", "lee el último correo", "Is Pluto a planet?",
               "```python\nprint(1)\n```", "resume el artículo de hoy", "tell me a joke"]
    for text in samples:
        a = freshness.freshness_assessment(text)
        assert a["time_sensitive"] == freshness.looks_time_sensitive(text)


def test_chat_route_uses_the_freshness_decision_on_the_hot_path():
    """The chat route asks `decide_freshness` (rule first, typed decision only
    when the rule is unsure) where it used to ask the bare keyword rule, and
    keeps the rule as its fallback if that call itself fails."""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "routes" / "chat_routes.py").read_text(encoding="utf-8")
    block = src[src.index("_auto_web = False"):src.index('logger.info("[freshness] time-sensitive question')]
    assert "await decide_freshness(message" in block
    assert "looks_time_sensitive(message)" in block  # the fallback
