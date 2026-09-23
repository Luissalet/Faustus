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


# ── wiring: brain entity typing ─────────────────────────────────────────────

@pytest.fixture()
def brain(tmp_path, monkeypatch, endpoint):
    from src.brain import db as brain_db
    from src import memory_engine as engine
    brain_db.use_dir(str(tmp_path / "brain"))
    monkeypatch.setattr(engine, "DATA_DIR", str(tmp_path / "engine"))
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path / "engine"))
    engine.set_vector_store(None)
    monkeypatch.setattr("src.context_engine.maintenance.should_yield", lambda: False)

    async def _no_llm(*a, **k):
        return json.dumps({"entities": [], "relations": []})

    monkeypatch.setattr("src.llm_core.llm_call_async", _no_llm)
    yield engine
    brain_db.use_dir(None)
    engine.reset_vector_store()


def _type_answer(letter: str, p: float = 0.92):
    others = [chr(ord("A") + i) for i in range(8) if chr(ord("A") + i) != letter]
    rest = (1 - p - 0.02) / len(others)
    return openai_body([{"token": letter, "logprob": _lp(p)}]
                       + [{"token": o, "logprob": _lp(rest)} for o in others], content=letter)


def test_extract_types_an_other_entity_in_the_background(monkeypatch, brain):
    from src.brain import entities, extract
    # organization is the third type (C)
    server = serve(monkeypatch, lambda p: (200, _type_answer("C")))
    brain.add_item("Ada works at Cordera Labs", owner="ada", trust_class="human_explicit")
    report = run(extract.extract_pending("ada", use_llm=True))
    labs = [e for e in entities.list_entities("ada") if e["name"] == "Cordera Labs"]
    assert labs and labs[0]["type"] == "organization"
    assert report["typed"] >= 1
    asked = [r for r in server.requests if "Cordera Labs" in r["payload"]["messages"][1]["content"]]
    assert asked and "Ada works at Cordera Labs" in asked[0]["payload"]["messages"][1]["content"]
    # asked once: a second sweep over the same sentence asks nothing new
    n = len(server.requests)
    run(extract.extract_pending("ada", use_llm=True))
    assert len(server.requests) == n


def test_extract_typing_respects_the_gate_and_low_confidence(monkeypatch, brain):
    from src.brain import entities, extract
    brain.add_item("Ada works at Cordera Labs", owner="ada", trust_class="human_explicit")
    # not resident: nothing asked, type unchanged
    brain_state = {"resident": ["another-model:70b"]}
    monkeypatch.setattr("src.background_job_guard._resident_model_names",
                        lambda url: brain_state["resident"])
    server = serve(monkeypatch, lambda p: (200, _type_answer("C")))
    run(extract.extract_pending("ada", use_llm=True))
    assert server.requests == []
    assert all(e["type"] == "other" for e in entities.list_entities("ada")
               if e["name"] == "Cordera Labs")
    # resident but unsure: asked, nothing changed
    brain_state["resident"] = [MODEL]
    server = serve(monkeypatch, lambda p: (200, openai_body(
        [{"token": "C", "logprob": _lp(0.4)}, {"token": "A", "logprob": _lp(0.35)}], content="C")))
    run(extract.extract_pending("ada", use_llm=True))
    assert server.requests
    assert all(e["type"] == "other" for e in entities.list_entities("ada")
               if e["name"] == "Cordera Labs")


def test_extract_typing_only_in_background_and_setting_off(monkeypatch, brain, settings):
    from src.brain import extract
    brain.add_item("Ada works at Cordera Labs", owner="ada", trust_class="human_explicit")
    server = serve(monkeypatch, lambda p: (200, _type_answer("C")))
    run(extract.extract_pending("ada", use_llm=True, background=False))
    assert server.requests == []
    settings["typed_decision_entity_types"] = False
    run(extract.extract_pending("ada", use_llm=True))
    assert server.requests == []


def test_typing_never_overrides_a_type_set_meanwhile(monkeypatch, brain):
    from src.brain import entities, extract
    brain.add_item("Ada works at Cordera Labs", owner="ada", trust_class="human_explicit")
    run(extract.extract_pending("ada", use_llm=False))
    labs = [e for e in entities.list_entities("ada") if e["name"] == "Cordera Labs"][0]

    def responder(p):
        entities.update_entity(labs["id"], type="project")  # a person typed it by hand
        return 200, _type_answer("C")

    serve(monkeypatch, responder)
    sources = [{"source_ref": r, "text": "Ada works at Cordera Labs"} for r in entities.sources_for(labs["id"])]
    report = {}
    run(extract.type_untyped_entities("ada", sources, report))
    assert entities.get_entity(labs["id"])["type"] == "project"


# ── wiring: memory-conflict suggestions ─────────────────────────────────────

@pytest.fixture()
def memstore(tmp_path, monkeypatch, endpoint):
    from src import memory_engine as engine
    monkeypatch.setattr(engine, "DATA_DIR", str(tmp_path))
    engine.set_vector_store(None)
    engine.clear_injected()
    monkeypatch.setattr("src.context_engine.maintenance.should_yield", lambda: False)
    yield engine
    engine.reset_vector_store()
    engine.clear_injected()


def _two_unruled_items(engine):
    from datetime import datetime, timedelta, timezone
    t0 = datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)
    a = engine.add_item("Bruno's team meets every Monday morning", owner="ada",
                        trust_class="human_explicit", now=t0)
    b = engine.add_item("Bruno's team meets every Thursday afternoon now", owner="ada",
                        trust_class="human_explicit", now=t0 + timedelta(days=30))
    return a, b


def test_conflict_advice_files_a_suggestion_never_an_open_conflict(monkeypatch, memstore):
    from src import memory_conflicts as mc
    a, b = _two_unruled_items(memstore)
    assert mc.classify(a["text"], b["text"]) is None
    assert mc.list_conflicts(owner="ada") == []
    server = serve(monkeypatch, lambda p: (200, openai_body(
        [{"token": "B", "logprob": _lp(0.9)}, {"token": "A", "logprob": _lp(0.05)}], content="B")))
    report = run(mc.advise("ada"))
    assert report["asked"] == 1 and report["suggested"] == 1
    assert mc.list_conflicts(owner="ada") == []  # nothing open
    suggested = mc.list_conflicts(owner="ada", status="suggested")
    assert len(suggested) == 1 and suggested[0]["reason"] == "model_suggested"
    assert "update" in suggested[0]["detail"]
    assert mc.open_conflict_for(a["id"], "ada") is None  # no ranking penalty
    ctx = server.requests[0]["payload"]["messages"][1]["content"]
    assert ctx.index("Monday") < ctx.index("Thursday")  # older first
    # asked once
    run(mc.advise("ada"))
    assert len(server.requests) == 1
    # the owner may still resolve it by hand
    assert mc.resolve(suggested[0]["id"], "both", owner="ada")["status"] == "kept_both"


def test_conflict_advice_compatible_or_unavailable_files_nothing(monkeypatch, memstore):
    from src import memory_conflicts as mc
    _two_unruled_items(memstore)
    endpoint_state = {"r": []}
    monkeypatch.setattr("src.background_job_guard._resident_model_names", lambda url: endpoint_state["r"])
    server = serve(monkeypatch, lambda p: (200, openai_body([{"token": "C", "logprob": -0.01}], content="C")))
    report = run(mc.advise("ada"))
    assert report["skipped"] == "model_not_resident" and server.requests == []
    endpoint_state["r"] = [MODEL]
    report = run(mc.advise("ada"))
    assert report["asked"] == 1 and report["suggested"] == 0
    assert mc.list_conflicts(owner="ada", status="suggested") == []


def test_conflict_advice_skips_pairs_the_rules_already_judge(memstore):
    from src import memory_conflicts as mc
    items = [
        {"id": "1", "text": "Ada lives in Bluehaven", "created_at": "2026-01-01"},
        {"id": "2", "text": "Ada lives in Villanueva", "created_at": "2026-02-01"},
        {"id": "3", "text": "Cordera Labs sells garden tools", "created_at": "2026-03-01"},
    ]
    assert mc.advice_candidates(items) == []


def test_maintenance_registers_the_advice_task():
    from src.context_engine import maintenance
    assert "memory_conflict_advice" in maintenance.TASK_NAMES
    assert "memory_conflict_advice" in maintenance.TASKS
    assert "memory_conflict_advice" in maintenance.OWNER_TASK_NAMES
    assert maintenance.TASK_INTERVALS_S["memory_conflict_advice"] >= 600


def test_chat_route_uses_the_freshness_decision_on_the_hot_path():
    """The chat route asks `decide_freshness` (rule first, typed decision only
    when the rule is unsure) where it used to ask the bare keyword rule, and
    keeps the rule as its fallback if that call itself fails."""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "routes" / "chat_routes.py").read_text(encoding="utf-8")
    block = src[src.index("_auto_web = False"):src.index('logger.info("[freshness] time-sensitive question')]
    assert "await decide_freshness(message" in block
    assert "looks_time_sensitive(message)" in block  # the fallback
