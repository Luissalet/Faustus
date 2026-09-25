"""The context packet keeps its query and its slot for a whole turn.

A runtime note the loop appends with the user role mid-turn is neither the
question nor the place the packet goes: either one moving loses the prompt
cache for everything after the packet (seen live: 15-19 s of prompt
reprocessing per round on a 27B)."""

from src.agent_loop import _insert_before_latest_user
from src.context_engine.manifest import _last_user_index
from src.context_engine.wiring import last_user_text


def _turn():
    return [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "¿Qué tengo mañana?"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "tool_call_id": "c1", "content": "nada"},
        {"role": "user", "_harness_note": True, "content": "[Use the calendar result above.]"},
    ]


def test_a_harness_note_is_not_the_question():
    rows = _turn()
    assert _last_user_index(rows) == 1
    assert last_user_text(rows) == "¿Qué tengo mañana?"


def test_notes_alone_still_give_a_question():
    rows = [{"role": "user", "_harness_note": True, "content": "note"}]
    assert _last_user_index(rows) == 0


def test_the_packet_stays_before_the_persons_message():
    packet = {"role": "user", "_agent_injected": "context_engine", "content": "ctx"}
    out = _insert_before_latest_user(_turn(), packet)
    assert out[1] is packet
    assert out[2]["content"] == "¿Qué tengo mañana?"
    # Same slot as on the first round, before any tool call or note.
    first = _insert_before_latest_user(_turn()[:2], packet)
    assert first[1] is packet


def test_a_tool_image_does_not_pull_the_packet_behind_it():
    """`inspect_image` hands its picture over in a user-role message marked
    untrusted; live, the packet moved behind it and 14,000 tokens were
    re-read."""
    packet = {"role": "user", "_agent_injected": "context_engine", "content": "ctx"}
    rows = _turn() + [{"role": "user", "content": [{"type": "text", "text": "[image from inspect_image]"}],
                       "metadata": {"trusted": False, "source": "tool result: inspect_image"}}]
    out = _insert_before_latest_user(rows, packet)
    assert out[1] is packet
    assert _last_user_index(rows) == 1
