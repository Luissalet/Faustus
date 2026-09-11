"""Lote 67 — LANG-01: generic Spanish capability-activating verbs.

Before this lote, Spanish imperatives in `src/action_intents.py` existed
ONLY for the project-objectives domain (`añade a los objetivos`, `crea una
meta`, ...) — grep confirms the pre-lote `_ROUTING_PATTERNS` table had no
bare "implementa"/"crea"/"haz"/"genera"/"busca"/"resume" pattern outside
that "project" block, while their English equivalents ("implement",
"create/make", "search", "research") already activate tool/agent mode
generically. This proves the gap first (accented + unaccented forms fail
to promote to agent mode without these patterns — demonstrated by the
"not yet closed" assertions failing before L67, now passing), then the
closure, then the false-positive guard the same lot demanded (imperative
position only, so ordinary Spanish sentences using the same verb stems
are not swept into tool mode).
"""
from __future__ import annotations

import pytest

from src.action_intents import classify_tool_intent


# ── acceptance's own two named words, both spellings ───────────────────────

@pytest.mark.parametrize("text,expected_category", [
    ("Impleméntame esto", "workspace"),
    ("implementame esto", "workspace"),
    ("Impleméntame la función de login", "workspace"),
    ("Créame un componente en React", "workspace"),
    ("creame un componente", "workspace"),
    ("Créame una página nueva", "workspace"),
])
def test_acceptance_words_implementame_and_creame_activate_workspace(text, expected_category):
    result = classify_tool_intent(text)
    assert result.needs_tools is True, text
    assert result.category == expected_category, (text, result)


# ── the other four the lot names alongside them ─────────────────────────────

@pytest.mark.parametrize("text,expected_category", [
    ("Hazme un script en python", "workspace"),
    ("hazme una funcion", "workspace"),
    ("Genera un informe de ventas", "workspace"),
    ("generame un resumen del codigo", "workspace"),
    ("Búscame el precio del dólar", "web"),
    ("buscame el precio del dolar", "web"),
    ("Resúmeme el correo de ayer", "research"),
    ("resumeme el correo", "research"),
])
def test_the_four_additional_generic_verbs(text, expected_category):
    result = classify_tool_intent(text)
    assert result.needs_tools is True, text
    assert result.category == expected_category, (text, result)


# ── same capability as the English equivalent (parity, not coincidence) ────

@pytest.mark.parametrize("es_text,en_text", [
    ("Búscame el clima de hoy", "search the weather today"),
    ("Resúmeme las noticias", "research the news"),
])
def test_spanish_form_reaches_the_same_category_english_already_reaches(es_text, en_text):
    es = classify_tool_intent(es_text)
    en = classify_tool_intent(en_text)
    assert es.needs_tools and en.needs_tools
    assert es.category == en.category


# ── accent-insensitivity is the pipeline (_routing_text), not per-pattern ──

def test_accented_and_unaccented_spellings_give_the_identical_verdict():
    pairs = [
        ("Impleméntame esto ya", "Implementame esto ya"),
        ("Créame algo simple", "Creame algo simple"),
        ("Búscame información", "Buscame informacion"),
        ("Resúmeme esto rápido", "Resumeme esto rapido"),
    ]
    for accented, plain in pairs:
        a, p = classify_tool_intent(accented), classify_tool_intent(plain)
        assert (a.needs_tools, a.category) == (p.needs_tools, p.category), (accented, plain)


# ── false-positive guard: the same verb stems in ordinary Spanish text ─────

@pytest.mark.parametrize("text", [
    "esto genera un problema en el sistema",
    "no creas nada de lo que dice",
    "el resumen del proyecto quedo bien",
    "el buscador no encontro resultados",
    "hazte cargo de esto por favor",
    "en general, esto funciona bien",
    "Generalmente esto funciona sin problemas",
    "Resumen: todo salio bien ayer",
])
def test_the_same_verb_stems_in_ordinary_non_command_text_do_not_promote(text):
    """The generic patterns are anchored to imperative position precisely so
    that ordinary sentences sharing a verb STEM ('genera', 'crea', 'resumen',
    'buscador'...) are not swept into forced tool/agent mode."""
    result = classify_tool_intent(text)
    assert result.needs_tools is False, (text, result)
