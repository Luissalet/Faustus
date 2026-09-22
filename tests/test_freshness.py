"""Tests for src/freshness.py — the time-sensitivity heuristic.

Positives cover Spanish + English across sports, news, prices, releases,
office holders, weather, schedules, availability and explicit recent years.
Negatives cover timeless questions (math, code, definitions, "summarize this
file") that must NOT trigger an automatic web search.
"""

import pytest

from src.freshness import freshness_reasons, looks_time_sensitive

POSITIVE_CASES = [
    # Sports results (ES/EN)
    "¿Ganó el Madrid su último partido?",
    "¿Cuál fue el resultado del partido de ayer?",
    "who won the game last night",
    "what was the final score of the match",
    "¿cómo va la clasificación de la liga?",
    # News / events
    "¿qué ha pasado hoy en el mundo?",
    "dame las noticias de esta semana",
    "any breaking news today",
    "what happened yesterday",
    "latest news on AI",
    # Prices / markets
    "¿cuál es el precio del bitcoin?",
    "cuánto cuesta el euro/dólar hoy",
    "what's the current stock price of Apple",
    "cotización del dólar hoy",
    # Releases / versions
    "¿ha salido ya la nueva versión de Python?",
    "when is the latest version of Node coming out",
    "cuál es la versión actual de Ubuntu",
    "has the new release been released yet",
    # Office holders / status
    "¿quién es el entrenador actual del Real Madrid?",
    "who is the current CEO of OpenAI",
    "is he still the president",
    # Weather
    "¿qué tiempo hace hoy en Madrid?",
    "will it rain tomorrow",
    "what's the weather forecast this weekend",
    # Schedules
    "¿a qué hora juega el Barça hoy?",
    "what time does the store open",
    "cuándo es el próximo partido",
    # Availability
    "¿está abierto el museo ahora?",
    "is the new iPhone in stock",
    "hay entradas para el concierto",
    # Explicit recent years / "current"
    "what changed in the 2025 tax law",
    "cuál es la situación actual del mercado",
]

NEGATIVE_CASES = [
    "how do I sort a list in Python",
    "¿qué es una derivada?",
    "resume este archivo",
    "explain how a hash map works",
    "escribe una función que sume dos números",
    "what is the capital of France",
    "help me refactor this class",
    "¿cómo se calcula el área de un círculo?",
    "write a poem about the sea",
    "fix this bug in my code",
    # A time word on the person's own things is not a web question (seen
    # live: the first one got "search the web before answering").
    "¿Qué aplicaciones mías puedes usar ahora mismo?",
    "lee el último correo",
    "¿qué tengo hoy en el calendario?",
    "resume mis notas de esta semana",
    "what did I change in my repo yesterday",
    "¿cuál es el estado actual de la rama?",
]

# The same words on a public subject still fire, whoever asks.
OWN_BUT_PUBLIC_CASES = [
    "¿ganó mi equipo el último partido?",
    "¿qué tiempo hace hoy en mi ciudad?",
    "cuál es el precio actual de mis acciones",
    "any news about my favourite band today",
]


@pytest.mark.parametrize("text", OWN_BUT_PUBLIC_CASES)
def test_a_public_subject_fires_even_in_the_first_person(text):
    assert looks_time_sensitive(text), f"expected time-sensitive: {text!r}"


@pytest.mark.parametrize("text", POSITIVE_CASES)
def test_time_sensitive_positive(text):
    assert looks_time_sensitive(text), f"expected time-sensitive: {text!r}"
    assert freshness_reasons(text), f"expected at least one reason for: {text!r}"


@pytest.mark.parametrize("text", NEGATIVE_CASES)
def test_time_sensitive_negative(text):
    assert not looks_time_sensitive(text), f"expected timeless: {text!r}"
    assert freshness_reasons(text) == []


def test_empty_and_blank():
    assert looks_time_sensitive("") is False
    assert looks_time_sensitive("   ") is False
    assert freshness_reasons(None) == []


def test_reasons_are_stable_labels():
    reasons = freshness_reasons("¿Ganó el Madrid su último partido?")
    assert "sports_result" in reasons
    assert all(isinstance(r, str) for r in reasons)
