"""The turn's start notes (contract criteria, active plan) survive context
trimming (src/context_compactor.py `is_pinned_note`, `trim_for_context`).

They travel as user-role harness notes in front of the person's request. The
trimmer used to drop every non-first system message and, under pressure, the
oldest conversation messages: the note was one of both."""
import pytest

from src.context_compactor import is_pinned_note, trim_for_context
from src.model_context import estimate_tokens

CONTRACT = {"role": "user", "_harness_note": True, "metadata": {"contract_criteria": True},
            "content": "[Contract criteria - automatic runtime message] 133 requirements. C001 ..."}
PLAN = {"role": "user", "_harness_note": True, "metadata": {"plan_note": True},
        "content": "Active plan \"X\" (abc): 0/11 done. Call plan_status."}
TASK = {"role": "user", "content": "the task: build the meter"}


def _history(turns=60, size=400):
    out = []
    for i in range(turns):
        out.append({"role": "user", "content": f"old question {i} " + "w " * size})
        out.append({"role": "assistant", "content": f"old answer {i} " + "w " * size})
    return out


def _turn(rounds=10, size=150):
    out = []
    for i in range(rounds):
        out.append({"role": "assistant", "content": f"round {i} " + "a " * size})
        out.append({"role": "user", "content": f"tool result {i} " + "r " * size, "_harness_note": True})
    return out


def _messages(*notes, rounds=10):
    """Preamble, older turns, the start notes, the task, then the work of the turn."""
    return ([{"role": "system", "content": "system preamble " + "s " * 300},
             {"role": "system", "content": "memory blob " + "m " * 300}]
            + _history() + list(notes) + [TASK] + _turn(rounds))


@pytest.mark.parametrize("preserve", [False, True])
def test_pinned_notes_survive_a_trim_that_drops_the_old_history(preserve):
    msgs = _messages(CONTRACT, PLAN)
    assert estimate_tokens(msgs) > 6000
    out = trim_for_context(msgs, 6000, 512, preserve_current_request=preserve)
    assert estimate_tokens(out) <= 6000 - 512
    contents = [m["content"] for m in out]
    assert CONTRACT["content"] in contents and PLAN["content"] in contents
    kept = [m for m in out if is_pinned_note(m)]
    assert [m["role"] for m in kept] == ["user", "user"]
    # in their original order, and in front of what followed them
    assert contents.index(CONTRACT["content"]) < contents.index(PLAN["content"])
    if preserve:
        assert contents.index(PLAN["content"]) + 1 == contents.index(TASK["content"])
    # the old history did go
    assert not any("old question 0 " in c for c in contents)


def test_without_the_flag_the_same_note_is_trimmed_away():
    plain = {k: v for k, v in CONTRACT.items() if k != "metadata"}
    out = trim_for_context(_messages(plain, rounds=14), 3000, 512)
    assert plain["content"] not in [m["content"] for m in out]
    # ...and the flagged one is kept under the same pressure
    flagged = trim_for_context(_messages(CONTRACT, rounds=14), 3000, 512)
    assert CONTRACT["content"] in [m["content"] for m in flagged]
    assert estimate_tokens(flagged) <= 3000 - 512


def test_a_system_role_pinned_note_is_essential():
    note = {"role": "system", "metadata": {"contract_criteria": True}, "content": "pinned system note"}
    msgs = _messages()
    msgs.insert(3, note)
    out = trim_for_context(msgs, 6000, 512)
    assert "pinned system note" in [m["content"] for m in out]
    assert "memory blob" not in " ".join(m["content"] for m in out if m["role"] == "system")


def test_nothing_changes_when_the_request_fits():
    msgs = [{"role": "system", "content": "sys"}, CONTRACT, {"role": "user", "content": "task"}]
    assert trim_for_context(msgs, 8000, 512) == msgs


def test_the_route_budget_counts_the_pinned_notes_as_required():
    # src/agent_loop.py _trim_route_request_messages adds the pinned notes that
    # sit before the current request to what must fit; guard the wiring.
    from pathlib import Path
    import src.agent_loop as al
    source = Path(al.__file__).read_text(encoding="utf-8")
    assert "[m for m in convo[:anchor] if is_pinned_note(m)]" in source
