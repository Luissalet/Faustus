"""src/answer_checks.py: weekdays the calendar contradicts, visible working."""
import pytest

from src import answer_checks as ac


@pytest.mark.parametrize("text,real", [
    ("el 25 de diciembre de 2026 cae en domingo", "viernes"),
    ("Domingo, 25 de diciembre de 2026", "viernes"),
    ("2. Domingo (25 de diciembre de 2026).", "viernes"),  # seen live
    ("**Domingo** (25 de diciembre de 2026)", "viernes"),
    ("Sunday (December 25, 2026)", "friday"),
    ("El 1 de enero de 2027 será jueves.", "viernes"),
    ("December 25, 2026 is a Sunday.", "friday"),
    ("Sunday, December 25, 2026", "friday"),
    ("Sunday 25 December 2026", "friday"),
])
def test_a_wrong_weekday_for_a_full_date_is_found(text, real):
    found = ac.weekday_mismatches(text)
    assert found and found[0]["real"] == real


@pytest.mark.parametrize("text", [
    "el 25 de diciembre de 2026 cae en viernes",
    "Friday, December 25, 2026",
    "hoy es viernes 25 de septiembre",           # no year: not checked
    "el 25 de diciembre de 2026 hay reunión; el domingo descanso",  # not paired
    "31 de febrero de 2026 es lunes",            # not a date
])
def test_right_or_unpaired_or_invalid_dates_are_left_alone(text):
    assert ac.weekday_mismatches(text) == []


@pytest.mark.parametrize("text", [
    "suman 3... espera, recalculo: 3",
    "Déjame ser riguroso: 3 manzanas",
    "4, wait: no, 3",
    "Let me recount the days: 91.",
])
def test_visible_working_is_found(text):
    assert ac.thinking_aloud(text)


@pytest.mark.parametrize("text", [
    "Espera un momento en la cola del servidor.",
    "Wait times are long on Mondays.",
    "```\nwait: 5\n```",
    "Tienes 3 manzanas.",
    # correcting the user's premise is wanted, not working
    "Corrección: hoy es viernes 25 de septiembre, no jueves.",
])
def test_ordinary_text_is_not_working(text):
    assert ac.thinking_aloud(text) == []


def test_the_rewrite_note_names_the_calendar_weekday_and_the_phrases():
    note = ac.rewrite_note(ac.weekday_mismatches("el 25 de diciembre de 2026 cae en domingo"),
                           ["espera,"])
    assert "2026-12-25 is a viernes" in note and '"espera,"' in note
