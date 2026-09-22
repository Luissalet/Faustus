"""The seven dimensions, and the two things that make them more than a form.

The methodology this comes from ships a markdown template and trusts everyone
to fill it in. These tests pin the two places where that trust is replaced by
something that actually holds:

  * the schema, so a backend that decodes under it cannot leave a section out,
  * the validator, so a section filled with "TBD" is not accepted either.

The second matters more than it looks. A grammar guarantees a string is
present; it cannot guarantee the string says anything, and a canvas of seven
polite nothings stored as a design is worse than no design, because the next
session reads it as settled.
"""
import json

import pytest

from src import design_canvas as dc
from src.design_canvas import DesignCanvasError


def _full(**overrides):
    canvas = {
        "requirements": [
            "A stopped engine is reported on the page before a chat fails",
            "The report names the model and the engine that serves it",
        ],
        "entities": [
            "EngineStatus: engine id, running flag, served model alias",
            "DefaultModel: the alias a chat uses when none is chosen",
        ],
        "approach": (
            "Read the engine list already on the page and compare each one's "
            "served alias against the default model, rendering a notice when "
            "the match is not running. Rejected: polling a new endpoint for "
            "the same fact, which would add a request per refresh for data "
            "the page already holds."
        ),
        "structure": [
            "studio/src/screens/settings/LocalModels.tsx -- the notice",
            "tests/test_engine_notice.py -- new, covers the match",
        ],
        "operations": [
            "engineServesDefault(engine, status, alias) -> bool, false when unknown",
            "startEngine(id) -> promise, rejects with the server message",
        ],
        "norms": [
            "Strings go in the translation table, never inline in the component",
            "A new component ships with its contract check under studio/checks",
        ],
        "safeguards": [
            "A missing alias could match every engine: unknown compares false",
            "A double click could start twice: the button disables while working",
        ],
    }
    canvas.update(overrides)
    return canvas


# ---------------------------------------------------------------------------
# The schema
# ---------------------------------------------------------------------------

def test_schema_requires_all_seven_dimensions():
    schema = dc.canvas_schema()
    assert set(schema["required"]) == {key for key, _h, _a in dc.DIMENSIONS}
    assert len(schema["required"]) == 7


def test_schema_forbids_an_eighth_field():
    """No room to answer something easier than what was asked."""
    assert dc.canvas_schema()["additionalProperties"] is False


def test_the_wire_schema_carries_shape_and_nothing_else():
    """The floors belong to the validator, not to the grammar.

    They were in the schema first. llama-server compiles it to a grammar, and
    with the floors in it the decoder produced an empty string under HTTP 200
    -- so the floors now live where they can be checked and reported properly.
    """
    schema = dc.canvas_schema()
    for prop in schema["properties"].values():
        assert "minLength" not in prop and "minItems" not in prop
        assert "minLength" not in prop.get("items", {})
    assert schema["properties"]["approach"]["type"] == "string"
    assert schema["properties"]["requirements"]["type"] == "array"


def test_prompt_names_every_dimension():
    text = json.dumps(dc.build_prompt("add a thing"))
    for key, _heading, _ask in dc.DIMENSIONS:
        assert key in text


def test_prompt_needs_a_goal():
    with pytest.raises(DesignCanvasError):
        dc.build_prompt("   ")


# ---------------------------------------------------------------------------
# The validator: the half a grammar cannot do
# ---------------------------------------------------------------------------

def test_a_real_canvas_passes():
    out = dc.validate(_full())
    assert len(out["requirements"]) == 2
    assert out["approach"].startswith("Read the engine list")


@pytest.mark.parametrize("filler", ["TBD", "N/A", "None", "todo", "see above",
                                    "to be determined", "..."])
def test_filler_entries_are_not_answers(filler):
    with pytest.raises(DesignCanvasError) as exc:
        dc.validate(_full(safeguards=[filler, filler]))
    assert "Safeguards" in str(exc.value)


def test_a_single_real_entity_is_allowed():
    """A small change can honestly introduce one entity. The first live run
    was rejected for exactly this, on a canvas whose single entity was the
    right answer -- a rule strict enough to reject good work teaches people to
    pad it."""
    out = dc.validate(_full(entities=[
        "nightly_pass_enabled: a boolean setting read before the pass runs"]))
    assert len(out["entities"]) == 1


def test_a_single_requirement_is_not_a_design():
    """Two stand where one is the tell: one requirement is a task."""
    with pytest.raises(DesignCanvasError) as exc:
        dc.validate(_full(requirements=["The nightly pass can be turned off"]))
    assert "Requirements" in str(exc.value)


def test_a_single_safeguard_means_the_second_one_was_not_looked_for():
    with pytest.raises(DesignCanvasError) as exc:
        dc.validate(_full(safeguards=[
            "A missing alias could match every engine: unknown compares false"]))
    assert "Safeguards" in str(exc.value)


def test_a_missing_dimension_is_named_in_the_error():
    broken = _full()
    del broken["entities"]
    with pytest.raises(DesignCanvasError) as exc:
        dc.validate(broken)
    assert "Entities" in str(exc.value)


def test_a_one_line_approach_is_rejected():
    with pytest.raises(DesignCanvasError):
        dc.validate(_full(approach="We add the notice."))


def test_every_problem_is_reported_at_once():
    """One round trip tells the model everything wrong, not the first thing."""
    with pytest.raises(DesignCanvasError) as exc:
        dc.validate(_full(norms=["TBD"], safeguards=["N/A", "N/A"]))
    message = str(exc.value)
    assert "Norms" in message and "Safeguards" in message


def test_a_prose_list_is_taken_line_by_line():
    """A model that answered one field as a bulleted string is not a failure."""
    out = dc.validate(_full(norms=(
        "- Strings go in the translation table, never inline\n"
        "- A new component ships with its contract check"
    )))
    assert len(out["norms"]) == 2
    assert out["norms"][0].startswith("Strings go")


def test_a_canvas_must_be_an_object():
    with pytest.raises(DesignCanvasError):
        dc.validate(["requirements", "entities"])


# ---------------------------------------------------------------------------
# What the graph gets
# ---------------------------------------------------------------------------

def test_paths_come_out_of_structure():
    paths = dc.referenced_paths(_full())
    assert "studio/src/screens/settings/LocalModels.tsx" in paths
    assert "tests/test_engine_notice.py" in paths


def test_prose_in_structure_contributes_no_ref():
    """A bogus ref would be reported as a broken file forever after."""
    assert dc.referenced_paths(
        {"structure": ["Somewhere in the settings screen", "the usual place"]}
    ) == []


def test_a_slash_alone_is_not_a_path():
    """The first live run turned "measure the token/latency cost" into a ref.
    An extension is required, so prose with a slash in it contributes none."""
    assert dc.referenced_paths({"structure": [
        "Measure the token/latency cost of the new path and record it",
        "Touch src/llm_core.py where the payload is built",
    ]}) == ["src/llm_core.py"]


def test_paths_are_not_duplicated():
    assert dc.referenced_paths({"structure": [
        "src/thing.py -- the reader", "src/thing.py -- and the writer",
    ]}) == ["src/thing.py"]


def test_markdown_carries_every_heading_in_order():
    rendered = dc.render(_full())
    positions = [rendered.index(f"## {h}") for _k, h, _a in dc.DIMENSIONS]
    assert positions == sorted(positions)


def test_summary_is_one_line_and_bounded():
    line = dc.summarise(_full(), "x" * 400)
    assert "\n" not in line
    assert len(line) < 400


# ---------------------------------------------------------------------------
# Reading the answer back
# ---------------------------------------------------------------------------

def test_a_bare_object_parses():
    assert dc.parse_response(json.dumps(_full()))["approach"]


def test_an_object_wrapped_in_prose_still_parses():
    """A backend with no constrained decoding chats around its answer."""
    raw = f"Here is the design:\n\n```json\n{json.dumps(_full())}\n```\nHope that helps."
    assert len(dc.parse_response(raw)["requirements"]) == 2


def test_braces_inside_strings_do_not_end_the_object():
    canvas = _full(approach=(
        "The payload gains a response_format of {\"type\": \"json_schema\"} so "
        "the decoder enforces the shape. Rejected: asking for JSON in the "
        "prompt, which returns a value outside the allowed set."
    ))
    assert "json_schema" in dc.parse_response(json.dumps(canvas))["approach"]


def test_an_answer_with_no_object_is_an_error():
    with pytest.raises(DesignCanvasError):
        dc.parse_response("I would start by looking at the settings screen.")


def test_an_empty_answer_is_an_error():
    with pytest.raises(DesignCanvasError):
        dc.parse_response("")


def test_a_shaped_but_empty_canvas_does_not_survive_parsing():
    """The two halves meet: the shape is right, the content is not."""
    hollow = {key: ("TBD" if key in dc._PROSE else ["TBD", "TBD"])
              for key, _h, _a in dc.DIMENSIONS}
    with pytest.raises(DesignCanvasError):
        dc.parse_response(json.dumps(hollow))
