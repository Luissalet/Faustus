"""A turn resumed after an approval card gets back the tool work it had done.

Seen live: the paused turn's eleven tool calls lived only in the saved
message's metadata, the resumed prompt replayed just its text, and the model
redid the work."""
from src.agent_loop import _paused_turn_work_note


def _paused(events):
    return [
        {"role": "user", "content": "¿Qué partes ya existen?"},
        {"role": "assistant", "content": "Voy a verificar los candidatos.",
         "metadata": {"tool_events": events}},
    ]


def test_note_lists_done_calls_and_skips_the_pending_approved_one():
    events = [
        {"tool": "prior_art", "command": '{"action":"rubric"}', "output": "Decompose this idea…"},
        {"tool": "prior_art", "command": '{"action":"verify"}', "output": "Kozea/WeasyPrint found, BSD-3-Clause"},
        {"tool": "reach_read", "command": "https://example.org/LICENSE", "output": ""},
    ]
    note = _paused_turn_work_note(_paused(events), "reach_read")
    assert note and "Kozea/WeasyPrint found" in note and "rubric" in note
    assert "example.org/LICENSE" not in note
    assert "do not repeat them" in note


def test_no_events_no_note():
    assert _paused_turn_work_note(_paused([]), "reach_read") is None
    assert _paused_turn_work_note([{"role": "user", "content": "hola"}], "x") is None


def test_only_the_latest_turns_are_searched():
    old = {"role": "assistant", "content": "antiguo",
           "metadata": {"tool_events": [{"tool": "grep", "command": "x", "output": "y"}]}}
    msgs = [old, {"role": "assistant", "content": "a"}, {"role": "assistant", "content": "b"}]
    assert _paused_turn_work_note(msgs, "reach_read") is None


def test_the_note_is_bounded():
    events = [{"tool": "read_file", "command": f"f{i}.py", "output": "x" * 3000} for i in range(20)]
    note = _paused_turn_work_note(_paused(events), "reach_read", limit=4000)
    assert note is not None and len(note) < 4300
