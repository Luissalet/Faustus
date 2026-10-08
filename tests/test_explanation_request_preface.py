"""Preface + action negation must count as explanation-only (Agora #98).

Also preserves classic openings the pre-#98 regex already accepted
(¿Cómo funciona… / Dónde está…) without treating real implement requests
as explanation-only.
"""

from __future__ import annotations

import re

from src.explanation_intents import is_explanation_request

# Mirror of the broad action heuristic agent_loop uses (subset is enough here).
_CODING = re.compile(
    r"\b(?:fix|implement|add|pru[eé]b[ao]|probar)\b",
    re.IGNORECASE,
)


SPARKS_PREFACE = (
    "Prueba de conexion del backend Sparks. Sin ejecutar acciones ni usar "
    "herramientas: explica en castellano, en cinco pasos concretos, como "
    "comprobaria una carga de modelo distribuida en tres equipos y como "
    "distinguirias que el servidor responde de que conserva realmente un "
    "millon de tokens de contexto."
)


def test_preface_plus_action_negation_is_explanation():
    assert is_explanation_request(SPARKS_PREFACE, coding_action_re=_CODING)


def test_classic_explain_at_start_still_counts():
    assert is_explanation_request(
        "explica qué hace server.py",
        coding_action_re=_CODING,
    )


def test_inverted_question_como_funciona_still_counts():
    """Regression: old regex allowed leading ¿ before cómo funciona."""
    assert is_explanation_request("¿Cómo funciona esto?", coding_action_re=_CODING)
    assert is_explanation_request('¿Cómo funciona "el servidor"?', coding_action_re=_CODING)


def test_donde_esta_path_still_counts():
    """Regression: old regex matched Spanish dónde está (and quoted openers)."""
    assert is_explanation_request("Dónde está server.py", coding_action_re=_CODING)
    assert is_explanation_request("¿Dónde está server.py?", coding_action_re=_CODING)
    assert is_explanation_request('"Dónde está server.py"', coding_action_re=_CODING)


def test_explain_without_negation_but_coding_verb_is_not_explanation():
    # "Prueba … explica" without "sin acciones" stays a coding/action turn.
    text = "Prueba el endpoint y explica el resultado en el archivo README"
    assert not is_explanation_request(text, coding_action_re=_CODING)


def test_strong_implement_wins_over_negation():
    text = "Sin usar herramientas: implementa el endpoint de health en app.py"
    assert not is_explanation_request(text, coding_action_re=_CODING)


def test_negation_english_without_tools():
    text = (
        "Connection check. Do not use tools: explain in five steps how you "
        "would verify a distributed model load."
    )
    assert is_explanation_request(text, coding_action_re=_CODING)


def test_empty_and_greeting_are_not_explanations():
    assert not is_explanation_request("", coding_action_re=_CODING)
    assert not is_explanation_request("hola", coding_action_re=_CODING)
