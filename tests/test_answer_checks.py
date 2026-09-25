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
    "Martes 29 de octubre… o sea, 29 de septiembre",  # seen live
    "the 29th of October... I mean, September",
])
def test_visible_working_is_found(text):
    assert ac.thinking_aloud(text)


@pytest.mark.parametrize("text", [
    "Espera un momento en la cola del servidor.",
    "Wait times are long on Mondays.",
    "```\nwait: 5\n```",
    "Tienes 3 manzanas.",
    "Trabajas en remoto, o sea, desde casa.",
    # correcting the user's premise is wanted, not working
    "Corrección: hoy es viernes 25 de septiembre, no jueves.",
])
def test_ordinary_text_is_not_working(text):
    assert ac.thinking_aloud(text) == []


def test_the_rewrite_note_names_the_calendar_weekday_and_the_phrases():
    note = ac.rewrite_note(ac.weekday_mismatches("el 25 de diciembre de 2026 cae en domingo"),
                           ["espera,"])
    assert "2026-12-25 is a viernes" in note and '"espera,"' in note


def test_a_weekday_asked_about_a_date_in_the_question_is_checked():
    q = ("Si hoy es jueves 25 de septiembre de 2026, ¿qué día de la semana será el "
         "25 de diciembre de 2026?")
    # two dates: the one after the question is the one asked about (seen live)
    assert ac.asked_weekday_mismatch(q, "2. Domingo.")[0]["real"] == "viernes"
    assert ac.asked_weekday_mismatch("¿Qué día es mejor, el 1 de mayo de 2026 o el 2 de mayo de 2026?", "El sábado.") == []
    q1 = "¿Qué día de la semana será el 25 de diciembre de 2026?"
    found = ac.asked_weekday_mismatch(q1, "2. Domingo.")
    assert found and found[0]["real"] == "viernes"
    assert ac.asked_weekday_mismatch(q1, "Cae en viernes.") == []
    assert ac.asked_weekday_mismatch(q1, "No lo sé.") == []
    assert ac.asked_weekday_mismatch("What day of the week is December 25, 2026?", "Sunday.")[0]["real"] == "friday"


_CAL = ["AI: Found 2 event(s) between 2026-09-27 and 2026-10-04: - 2026-09-29T17:00:00 -> "
        "2026-09-29T18:00:00: [Cita con el dentista](#event-64) #health (Personal) - "
        "2026-10-01T10:00:00 -> 2026-10-01T11:00:00: [Reunión con el fontanero](#event-7) #admin"]


def test_a_suggested_slot_the_listed_calendar_has_busy_is_found():
    # seen live: the event was listed, then its own slot suggested as free
    answer = ("- Martes 29, 17:00–18:00: Cita con el dentista\n\n"
              "**Mi sugerencia:** el **martes 29 de septiembre a las 17:00**. Son huecos libres.")
    found = ac.slot_conflicts(answer, _CAL)
    assert found and found[0]["event"] == "Cita con el dentista"
    assert "Cita con el dentista" in ac.rewrite_note([], [], found)


@pytest.mark.parametrize("answer", [
    "Te sugiero el jueves 1 a las 16:00.",                       # free
    "El martes 29 a las 17:00 tienes la Cita con el dentista.",  # names the event, not a suggestion
    "Tienes algo el martes 29 a las 17:00.",                    # no suggestion
])
def test_free_or_non_suggested_slots_are_left_alone(answer):
    assert ac.slot_conflicts(answer, _CAL) == []


def test_no_calendar_listed_means_nothing_to_check():
    assert ac.slot_conflicts("Te sugiero el martes 29 a las 17:00.", ["no events"]) == []
