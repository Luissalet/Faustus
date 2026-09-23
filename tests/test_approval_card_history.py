"""After the user approves a runtime card, the model must not read the card's
question as its own last words.

The card's "Allow this task to continue?" is streamed as assistant text so the
transcript keeps it. Seen live with a 27B on a plugin call: once approved,
the next round was that sentence again, twice, and the visible answer to
"resume my day" was the approval card. The replayed history now carries a
runtime note in its place.
"""
from src import agent_loop as al


def _msgs(last_assistant):
    return [
        {"role": "user", "content": "Resume lo que he hecho hoy"},
        {"role": "assistant", "content": last_assistant},
        {"role": "user", "content": ""},
    ]


def test_the_card_question_alone_becomes_a_runtime_note():
    msgs = _msgs("Allow this task to continue?")
    assert al._scrub_approval_card_from_history(msgs, "manage_skills")
    text = msgs[1]["content"]
    assert "allow this task to continue" not in text.lower()
    assert "manage_skills" in text and "approved" in text


def test_partial_answer_before_the_card_is_kept():
    msgs = _msgs("Voy a mirar la skill primero.\n\nAllow this task to continue?")
    assert al._scrub_approval_card_from_history(msgs, "manage_skills")
    text = msgs[1]["content"]
    assert text.startswith("Voy a mirar la skill primero.")
    assert "allow this task to continue" not in text.lower()


def test_nothing_to_scrub_leaves_history_alone():
    msgs = _msgs("Listo, aquí tienes el resumen.")
    before = [dict(m) for m in msgs]
    assert not al._scrub_approval_card_from_history(msgs, "bash")
    assert msgs == before
    assert not al._scrub_approval_card_from_history([], "bash")


def test_only_the_recent_turns_are_touched():
    old_card = {"role": "assistant", "content": "Allow this task to continue?"}
    msgs = [old_card] + [{"role": "assistant", "content": f"turno {i}"} for i in range(4)]
    assert not al._scrub_approval_card_from_history(msgs, "bash")
    assert old_card["content"] == "Allow this task to continue?"
