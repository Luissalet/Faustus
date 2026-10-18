"""The intent-without-action supervisor's detector: an action announced in the
final words of a round, mid-sentence too, but not an offer."""
from src.agent_loop import announced_action
import pytest


def test_a_promise_after_a_sentence_counts():
    # live 17-10-2026, a Hoard copilot on a small model: the change was never made
    text = ("The right tool exists: `team_popular_sets` builds the team with each Pokémon's most popular set and "
            "saves it to the trainer. I'll replace Pablo's team with these six, using the names as written.")
    match = announced_action(text)
    assert match and "replace Pablo's team" in match.group(0)


def test_the_old_phrasings_still_count():
    assert announced_action("Let me tail the output to see the error")
    assert announced_action("Voy a pulsar el botón de guardar")
    assert announced_action("Listo. Ahora compruebo el estado del servidor")
    assert announced_action("I'll update the trainer now")


def test_offers_and_small_talk_do_not():
    assert announced_action("Let me know what you think.") is None
    assert announced_action("Done. I'll add more sets if you want.") is None
    assert announced_action("¿Quieres que lo cambie? Si quieres, actualizo el equipo.") is None
    assert announced_action("Creo que ya está todo.") is None
    assert announced_action("") is None


def test_one_real_promise_among_offers_still_counts():
    text = "I'll add the rest if you want. I'll save the team first."
    match = announced_action(text)
    assert match and "save the team" in match.group(0)


@pytest.mark.parametrize('text', [
    'The id I passed was missing a few letters — retrying with the exact id from the context.',
    'Retrying with the complete identifier now.',
    "I'll retry the failed read with the saved identifier.",
    "I'm retrying with the complete identifier.",
])
def test_a_retry_announced_after_a_failed_tool_is_still_an_unfinished_action(text):
    assert announced_action(text)


@pytest.mark.parametrize('text', [
    'The server is retrying the request in the background.',
    'Retrying cannot recover a deleted object.',
    'Retrying with the correct identifier if you want.',
    'The read failed; no retry was performed.',
])
def test_reports_and_offers_about_retries_are_not_unfinished_actions(text):
    assert announced_action(text) is None


@pytest.mark.parametrize('text', [
    'Sigo pensando que src/calc.py está bien así.',
    'Sigo creyendo que el resultado es correcto.',
    'Sigo opinando que la solución es suficiente.',
    'Sigo convencido de que la función está bien.',
    'Sigo sin poder confirmar el resultado.',
    'Sigo aquí, pero ya he terminado la revisión.',
    'He leído el archivo y he corregido la función.',
    'Ya he ejecutado los tests; todavía fallan.',
])
def test_opinions_and_completed_work_are_not_tool_promises(text):
    assert announced_action(text) is None


@pytest.mark.parametrize('text', [
    'Sigo leyendo src/calc.py para encontrar el fallo.',
    'Ahora sigo buscando la causa en los logs.',
    'Sigo comprobando el estado del servidor.',
    'Sigo ejecutando los tests del proyecto.',
    'Sigo editando el archivo para corregir la función.',
    'Sigo reintentando la consulta con el identificador completo.',
])
def test_continued_concrete_tool_actions_still_count(text):
    assert announced_action(text)
