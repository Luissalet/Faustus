"""The intent-without-action supervisor's detector: an action announced in the
final words of a round, mid-sentence too, but not an offer."""
from src.agent_loop import announced_action


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
