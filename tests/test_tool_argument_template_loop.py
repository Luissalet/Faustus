"""Tool-call arguments that cycle through one sentence template (live,
exam run 15: a poem recalled from memory inside a python comment, one new
quoted line after another, 8192 tokens and 14 minutes)."""
import json

from src.llm_core import tool_argument_loop, tool_argument_template_loop

_POEMS = ["Prufrock", "Ulysses", "Break, break, break", "The Lady of Shalott"]
_LINES = ["Let us go then, you and I", "In the room the women come and go", "Talking of Michelangelo",
          "No storm shall shake thy soul", "How soon thou wast asleep", "That happy man!",
          "Like a patient etherised upon a table", "Sawdust restaurants with oyster-cracks",
          "To follow knowledge like a sinking star", "Long fields of barley and of rye",
          "On thy cold gray stones, O Sea!", "And up and down the people go"]


def _recall_loop(n_lines: int) -> str:
    out = ["# Checks for the puzzle", "# 1) which poem has the gap"]
    for i in range(n_lines):
        if i % 2:
            out.append(f'#    -> "pebbled shore" -> Tennyson, "{_POEMS[i % 4]}": "{_LINES[i % len(_LINES)]} {i}" NO.')
        else:
            out.append(f'#    -> The line is from T.S. Eliot, "Prufrock": "{_LINES[(i * 5) % len(_LINES)]} {i}" NO.')
    return json.dumps({"code": "\n".join(out)})


def test_a_recall_loop_in_a_comment_is_caught():
    args = _recall_loop(80)
    reason = tool_argument_template_loop(args)
    assert reason and "sentence template" in reason
    assert tool_argument_loop(args) is None  # the exact-repeat guard misses it


def test_short_arguments_are_left_alone():
    assert tool_argument_template_loop(_recall_loop(10)) is None


def test_a_script_with_many_similar_prints_is_not_a_loop():
    code = "\n".join(f'print("row {i}:", shift("Word{i}", [-{i % 5}]*6))' for i in range(120))
    assert tool_argument_template_loop(json.dumps({"code": code})) is None


def test_data_rows_and_a_real_document_are_not_loops():
    rows = "\n".join(f'    "key_{i}": "value number {i} with some words",' for i in range(150))
    assert tool_argument_template_loop(json.dumps({"content": "{\n" + rows + "\n}"})) is None
    places = ["Havana", "San Juan", "Port Royal", "Cartagena", "Willemstad", "Nassau", "Kingston"] * 30
    prose = "\n".join(f"Paragraph {i}: the fort at {place} guarded the bay against raids for decades."
                       for i, place in enumerate(places))
    # Same sentence with a different place each line and numbers: prose, but
    # nothing is quoted, so the place names keep the skeletons distinct.
    assert tool_argument_template_loop(json.dumps({"content": prose})) is None


def test_repeated_prose_followed_by_real_data_is_not_a_loop_in_progress():
    text = "\n".join([f'-> It is from "line {i}": guess NO.' for i in range(60)]
                     + [f'  "row{i}": [{i}, {i + 1}, {i + 2}],' for i in range(60)])
    assert tool_argument_template_loop(text) is None
