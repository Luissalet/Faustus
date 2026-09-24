"""The resume after an approval keeps the latest results, not the first ones.

Live, 24-09-2026: a turn that had asked the vision model some twenty
questions paused on a python approval; the carried note kept only the first
6000 characters (its initial file reads) and the model started over.
"""
from src.agent_loop import _build_actions_snapshot, _paused_turn_work_note


def _events(n):
    evs = [{"tool": "read_file", "command": "ENUNCIADO.md", "output": "statement " * 50}]
    for i in range(n):
        evs.append({"tool": "inspect_image", "command": f"region {i}",
                    "output": f"ANSWER-{i}: " + "x" * 1800})
    evs.append({"tool": "python", "command": "print(1)", "output": "Waiting for an exact user approval"})
    return evs


def test_the_newest_answers_survive_and_the_omission_is_said():
    msgs = [{"role": "user", "content": "solve"},
            {"role": "assistant", "content": "", "metadata": {"tool_events": _events(20)}}]
    note = _paused_turn_work_note(msgs, "python")
    assert "ANSWER-19" in note and "ANSWER-12" in note
    assert "earlier call(s) omitted" in note
    assert len(note) < 26000


def test_the_verifier_view_is_unchanged():
    snap = _build_actions_snapshot(_events(20), limit=8000)
    assert snap.startswith("[read_file]") and len(snap) == 8000
