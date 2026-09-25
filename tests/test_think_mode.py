# -*- coding: utf-8 -*-
"""Per-turn reasoning mode (src/think_mode.py): the Auto rule, the mapping to
gen overrides, and the precedence the chat route applies."""

import pytest

from src import think_mode
from src.think_mode import decide, normalize, resolve_turn, to_overrides

SETTINGS = {"think_mode_budget_think": 4096, "think_mode_budget_deep": 16384}

FAST = [
    "hola",
    "Hola, ¿qué tal?",
    "buenos días",
    "gracias!",
    "vale, perfecto",
    "hi there",
    "thanks a lot",
    "how are you?",
    "¿quién eres?",
    "¿Cuál es la capital de Francia?",
    "what year did the Berlin wall fall?",
    "¿Cuántos habitantes tiene Madrid?",
    "who wrote Don Quixote?",
    "rápido: ¿cuánto es un kilo en libras?",
    "responde rápido, ¿qué día es la Hispanidad?",
    "dímelo en una palabra: ¿te gusta el café?",
    "quick question: what's the plural of cactus?",
    "yes or no: is Pluto a planet?",
    "sin pensar mucho, un nombre para un gato",
    "cuéntame un chiste",
    "tell me a fun fact about otters",
    "me voy a dormir, hasta mañana",
]

THINK = [
    "¿Por qué falla este test? AssertionError: expected 3 got 4",
    "arregla el bug en utils.py que rompe el login",
    "Traceback (most recent call last): KeyError: 'id'",
    "implement a function that merges two sorted lists",
    "refactoriza esta clase para que use inyección de dependencias",
    "```python\ndef f(x):\n    return x*2\n```\n¿qué hace esto?",
    "resuelve la ecuación 3x + 5 = 20",
    "calculate the probability of rolling two sixes",
    "compara PostgreSQL vs MySQL para una app pequeña",
    "what's the difference between TCP and UDP?",
    "pros y contras de alquilar o comprar piso",
    "haz un plan para migrar el servidor a Docker",
    "design an architecture for a chat app with offline support",
    "analiza este contrato y dime los riesgos",
    "¿cómo funciona el garbage collector de Java?",
    "why does my React component re-render twice?",
    "escribe una query SQL que devuelva los clientes sin pedidos",
    "organiza mi semana: tengo que estudiar al menos 10 horas, sin trabajar el domingo y como máximo 3 horas al día",
    "12 * 37 + 5",
    "review this pull request for security issues",
]

DEEP = [
    "explícame a fondo cómo funciona el consenso Raft",
    "analiza en profundidad este informe trimestral",
    "haz una revisión exhaustiva del módulo de pagos",
    "demuestra que la raíz de 2 es irracional",
    "prove that there are infinitely many primes",
    "think hard about this: how would you shard this database?",
    "explain step by step how TLS handshakes work",
    "resuélvelo paso a paso",
    "give me a thorough comparison of these three frameworks",
    "piénsalo bien antes de responder: ¿conviene cambiar de proveedor?",
]


@pytest.mark.parametrize("text", FAST)
def test_fast_cases(text):
    d = decide(text)
    assert d["mode"] == "fast", (text, d)
    assert d["source"] == "rule"


@pytest.mark.parametrize("text", THINK)
def test_think_cases(text):
    d = decide(text)
    assert d["mode"] == "think", (text, d)


@pytest.mark.parametrize("text", DEEP)
def test_deep_cases(text):
    d = decide(text)
    assert d["mode"] == "deep", (text, d)


def test_table_is_big_enough():
    assert len(FAST) + len(THINK) + len(DEEP) >= 40


def test_speed_word_inside_a_question_is_not_a_request_for_speed():
    d = decide("¿cuál es el algoritmo de ordenación más rápido para listas casi ordenadas?")
    assert d["mode"] == "think"


def test_long_multi_part_brief_goes_deep():
    parts = "\n".join(f"{i}. Implementa la parte {i} del módulo con tests y documentación" for i in range(1, 8))
    brief = ("Necesito que construyas el sistema de facturación completo. " * 12) + "\n" + parts
    assert decide(brief)["mode"] == "deep"


def test_many_attachments_with_analysis_go_deep():
    assert decide("analiza estos documentos", attachments=4)["mode"] == "deep"
    assert decide("analiza este documento", attachments=1)["mode"] == "think"
    assert decide("", attachments=1)["mode"] == "think"


def test_coding_turn_never_drops_below_think_unless_small_talk():
    assert decide("qué hay en la carpeta", coding=True)["mode"] == "think"
    assert decide("hola", coding=True)["mode"] == "fast"
    # A go-ahead in a running task is an approval, not small talk.
    assert decide("sí", coding=True, agent=True, history_len=4)["mode"] == "think"
    assert decide("sí")["mode"] == "fast"


def test_decide_never_raises():
    for bad in (None, 12, object(), "", "   "):
        d = decide(bad)  # type: ignore[arg-type]
        assert d["mode"] in ("fast", "think", "deep")
    d = decide("x", attachments="lots")  # type: ignore[arg-type]
    assert d["mode"] in ("fast", "think", "deep")


def test_decision_shape():
    d = decide("compara Rust vs Go")
    assert set(d) >= {"mode", "source", "reasons", "why"}
    assert "compare" in d["reasons"]


@pytest.mark.parametrize("raw,want", [
    ("auto", "auto"), ("Rápido", "fast"), ("rapido", "fast"), ("fast", "fast"), ("off", "fast"),
    ("pensar", "think"), ("on", "think"), ("THINK", "think"), ("a fondo", "deep"), ("deep", "deep"),
    ("", ""), (None, ""), ("banana", ""),
])
def test_normalize(raw, want):
    assert normalize(raw) == want


def test_to_overrides_mapping():
    assert to_overrides("fast", "qwen3", SETTINGS) == {"think": False}
    assert to_overrides("think", "qwen3", SETTINGS) == {"think": True, "reasoning_budget": 4096}
    assert to_overrides("deep", "qwen3", SETTINGS) == {
        "think": True, "reasoning_budget": 16384, "reasoning_effort": "high"}
    assert to_overrides("deep", "qwen3", SETTINGS, effort=False) == {"think": True, "reasoning_budget": 16384}
    assert to_overrides("auto", "qwen3", SETTINGS) == {}
    assert to_overrides("nonsense", "qwen3", SETTINGS) == {}


def test_to_overrides_reads_budget_settings():
    assert to_overrides("think", "m", {"think_mode_budget_think": 2048})["reasoning_budget"] == 2048
    assert to_overrides("deep", "m", {"think_mode_budget_deep": "bad"})["reasoning_budget"] == 16384


def test_budget_settings_are_registered():
    from src.settings import DEFAULT_SETTINGS
    assert DEFAULT_SETTINGS["think_mode_default"] == "auto"
    assert DEFAULT_SETTINGS["think_mode_budget_think"] == think_mode.DEFAULT_BUDGET_THINK
    assert DEFAULT_SETTINGS["think_mode_budget_deep"] == think_mode.DEFAULT_BUDGET_DEEP
    assert DEFAULT_SETTINGS["think_mode_deep_watchdog_factor"] == 2.0


# ── precedence (what the chat route applies) ────────────────────────────────

def test_explicit_mode_beats_gen_think():
    r = resolve_turn("deep", {"think": False, "top_p": 0.9}, "hola", settings=SETTINGS)
    assert r["overrides"] == {"top_p": 0.9, "think": True, "reasoning_budget": 16384, "reasoning_effort": "high"}
    assert r["event"]["source"] == "explicit" and r["event"]["mode"] == "deep"
    assert r["event"]["budget"] == 16384


def test_explicit_fast_beats_gen_think_on():
    r = resolve_turn("fast", {"think": True, "reasoning_budget": 999}, "demuestra el teorema", settings=SETTINGS)
    assert r["overrides"] == {"think": False}
    assert r["event"]["mode"] == "fast"


def test_gen_think_beats_auto():
    r = resolve_turn("auto", {"think": True}, "hola", settings=SETTINGS)
    assert r["overrides"] == {"think": True}
    assert r["event"]["source"] == "override" and r["event"]["mode"] == "think"
    r = resolve_turn("", {"think": False}, "demuestra que 1+1=2", settings=SETTINGS)
    assert r["overrides"] == {"think": False}
    assert r["event"]["mode"] == "fast"


def test_auto_applies_the_rule():
    r = resolve_turn("auto", {}, "hola", settings=SETTINGS)
    assert r["overrides"] == {"think": False}
    assert r["event"] == {**r["event"], "mode": "fast", "requested": "auto", "source": "rule", "budget": None}
    r = resolve_turn("auto", {}, "arregla el bug en app.py", settings=SETTINGS)
    assert r["overrides"] == {"think": True, "reasoning_budget": 4096}
    assert r["event"]["budget"] == 4096


def test_auto_keeps_a_client_pinned_budget():
    r = resolve_turn("auto", {"reasoning_budget": 1000}, "arregla el bug en app.py", settings=SETTINGS)
    assert r["overrides"] == {"think": True, "reasoning_budget": 1000}


def test_default_mode_applies_when_nothing_requested():
    r = resolve_turn(None, {}, "hola", settings=SETTINGS, default_mode="think")
    assert r["overrides"] == {"think": True, "reasoning_budget": 4096}
    assert r["event"]["source"] == "explicit"


def test_non_thinking_model_is_left_alone():
    r = resolve_turn("deep", {"top_p": 0.5}, "demuestra algo", supports_thinking=False, settings=SETTINGS)
    assert r == {"overrides": {"top_p": 0.5}, "event": None}


def test_resolve_turn_does_not_mutate_input():
    base = {"think": True}
    resolve_turn("fast", base, "x", settings=SETTINGS)
    assert base == {"think": True}


@pytest.mark.parametrize("text", [
    "Tengo 3 cajas: la A pesa el doble que la B y la C pesa 4 kg más que la A. "
    "Juntas pesan 44 kg. ¿Cuánto pesa cada una?",
    "A train leaves at 3 pm going 80 km/h and another at 4 pm going 100 km/h. How long until the second catches up?",
    "Si 5 obreros tardan 12 días, ¿cuántos días tardan 3 obreros?",
    "What is 15% of 240 plus half of 60?",
])
def test_word_problems_get_reasoning(text):
    out = decide(text)
    assert out["mode"] == "think", out
    assert "math" in out["reasons"]


@pytest.mark.parametrize("text", [
    "tengo 2 perros y 1 gato",
    "quedamos a las 5 el día 3",
])
def test_numbers_alone_are_not_a_word_problem(text):
    assert decide(text)["mode"] == "fast"
