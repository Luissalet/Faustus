"""
tests/test_media_workflows.py — the templates, and the refusals that make them
worth having.

The rule this file exists for: **the agent never supplies a graph.** So the
tests that matter are not "a template renders" but the ones where somebody
tries to put something into a render that the template never declared, and
gets a refusal that names the field.

The two shipped templates are parsed here too. A template with a placeholder
nobody declared, or a computed value keyed off an input that does not exist,
is a file that fails on the machine of whoever first tries to use it — which
is why the loader refuses it and this test reads them for real.
"""
from __future__ import annotations

import json

import pytest

from src import media_workflows as mw
from src.media_workflows import TemplateError


def template(**over):
    body = {
        "id": "image.test", "version": "1.0.0", "title": "A test recipe",
        "engine": "comfyui",
        "models": [{"name": "sd_xl_base_1.0.safetensors", "kind": "checkpoint",
                    "license": "CreativeML Open RAIL++-M"}],
        "requires_nodes": ["KSampler"],
        "outputs": {"image": "artifact:image"},
        "inputs": {
            "prompt": {"type": "text", "required": True, "max_len": 100},
            "aspect_ratio": {"type": "enum", "choices": ["1:1", "16:9"], "default": "1:1"},
            "seed": {"type": "seed"},
        },
        "computed": {
            "width": {"from": "aspect_ratio", "map": {"1:1": 1024, "16:9": 1344}},
        },
        "graph": {
            "1": {"class_type": "KSampler",
                  "inputs": {"seed": "{{seed}}", "text": "{{prompt}}",
                             "width": "{{width}}"}},
        },
    }
    body.update(over)
    return mw.parse(body)


# ── the shipped templates are real ────────────────────────────────────────

def test_the_templates_that_ship_with_faustus_all_parse():
    """They are read from disk, not from a fixture. A template whose graph
    names a placeholder nobody declared would be a file that fails in front of
    the first person to use it."""
    found = mw.catalogue()
    assert found["broken"] == [], f"a shipped template does not parse: {found['broken']}"
    ids = {w.id for w in found["workflows"]}
    assert {"image.product", "image.reference-edit"} <= ids
    for workflow in found["workflows"]:
        assert workflow.engine == "comfyui"
        assert workflow.models, f"{workflow.id} declares no model"
        assert all(m.license for m in workflow.models), (
            f"{workflow.id} has a model with no licence; the licence travels with "
            "the picture and by the time anyone asks, nobody remembers")


def test_a_shipped_template_renders_into_a_graph_with_no_placeholders_left():
    product = mw.load("image.product")
    out = mw.render(product, {"prompt": "a ceramic mug on white",
                              "aspect_ratio": "16:9", "quality": "final"})
    graph = out["graph"]

    assert graph["5"]["inputs"]["steps"] == 40                 # final, not draft
    assert graph["4"]["inputs"]["width"] == 1344               # 16:9
    assert graph["2"]["inputs"]["text"] == "a ceramic mug on white"
    assert isinstance(graph["5"]["inputs"]["seed"], int)
    assert "{{" not in json.dumps(graph), "a placeholder survived rendering"


# ── nothing undeclared gets in ────────────────────────────────────────────

def test_an_input_the_template_never_declared_is_refused_by_name():
    """The one that matters. If an unknown key were ignored, a caller could
    believe it had set something it had not — and if it were passed through,
    the template would stop being the thing that decides what a run can do."""
    with pytest.raises(TemplateError) as err:
        mw.render(template(), {"prompt": "hi", "steps": 500})
    assert err.value.path == "inputs.steps"
    assert "accepts no input called 'steps'" in err.value.message
    assert "prompt" in err.value.message, "the refusal should say what it does accept"


def test_a_value_outside_the_choices_is_refused_and_the_choices_are_named():
    with pytest.raises(TemplateError) as err:
        mw.render(template(), {"prompt": "hi", "aspect_ratio": "21:9"})
    assert err.value.path == "inputs.aspect_ratio"
    assert "['1:1', '16:9']" in err.value.message


def test_nothing_is_converted_across_a_type():
    """`"3"` is not 3 here, the same rule as the contracts. A silent
    conversion is how a resolution reaches the engine as a string."""
    numeric = template(inputs={
        "prompt": {"type": "text", "required": True},
        "steps": {"type": "integer", "minimum": 1, "maximum": 50},
    }, computed={}, graph={"1": {"class_type": "KSampler",
                                 "inputs": {"text": "{{prompt}}",
                                            "steps": "{{steps}}"}}})
    with pytest.raises(TemplateError) as err:
        mw.render(numeric, {"prompt": "hi", "steps": "40"})
    assert err.value.path == "inputs.steps"
    assert "whole number" in err.value.message

    # And a bool is not an int, which Python would otherwise let through.
    with pytest.raises(TemplateError):
        mw.render(numeric, {"prompt": "hi", "steps": True})


def test_a_number_outside_the_range_is_refused():
    narrow = template(inputs={
        "prompt": {"type": "text", "required": True},
        "steps": {"type": "integer", "minimum": 1, "maximum": 50},
    }, computed={}, graph={"1": {"class_type": "KSampler",
                                 "inputs": {"text": "{{prompt}}", "steps": "{{steps}}"}}})
    with pytest.raises(TemplateError) as err:
        mw.render(narrow, {"prompt": "hi", "steps": 5000})
    assert "above the maximum of 50" in err.value.message


def test_a_required_input_that_is_missing_is_named():
    with pytest.raises(TemplateError) as err:
        mw.render(template(), {})
    assert err.value.path == "inputs.prompt"
    assert "required" in err.value.message


def test_a_reference_image_may_be_a_name_but_never_a_path():
    """A reference reaches the engine as a name it looks up in its own input
    folder. A path here would be someone reading a file off the machine
    through a template that looked harmless."""
    edit = mw.load("image.reference-edit")
    ok = mw.render(edit, {"reference": "mug.png", "prompt": "make it blue"})
    assert ok["graph"]["2"]["inputs"]["image"] == "mug.png"

    for attempt in ("../../etc/passwd", "/etc/passwd", "sub/dir.png",
                    "C:\\Windows\\win.ini"):
        with pytest.raises(TemplateError) as err:
            mw.render(edit, {"reference": attempt, "prompt": "x"})
        assert err.value.path == "inputs.reference"
        assert "bare filename" in err.value.message


def test_a_prompt_cannot_reach_outside_its_own_field():
    """Substitution replaces WHOLE strings only. A prompt containing what
    looks like a placeholder, or JSON, is a prompt — it never becomes part of
    the graph's structure."""
    nasty = '{{seed}} ", "steps": 999, "x": "'
    out = mw.render(template(), {"prompt": nasty})
    assert out["graph"]["1"]["inputs"]["text"] == nasty, "the prompt was mangled"
    # It stayed inside its own field: the node gained no keys, and the `{{seed}}`
    # inside the prompt text was not resolved into anything.
    assert set(out["graph"]["1"]["inputs"]) == {"seed", "text", "width"}


# ── seeds are provenance ──────────────────────────────────────────────────

def test_a_seed_nobody_chose_is_generated_here_and_recorded():
    """An engine-side random seed is a picture nobody can ever make again."""
    first = mw.render(template(), {"prompt": "hi"})
    second = mw.render(template(), {"prompt": "hi"})
    assert isinstance(first["values"]["seed"], int)
    assert 0 <= first["values"]["seed"] <= mw.MAX_SEED
    assert first["values"]["seed"] != second["values"]["seed"]

    # And a seed that IS given comes back untouched, which is what makes
    # "another one like that" work.
    again = mw.render(template(), {"prompt": "hi", "seed": first["values"]["seed"]})
    assert again["graph"] == first["graph"]


def test_the_render_reports_everything_needed_to_do_it_again():
    out = mw.render(mw.load("image.product"), {"prompt": "a mug"})
    assert out["workflow"] == "image.product" and out["version"] == "1.0.0"
    assert len(out["fingerprint"]) == 64
    assert out["models"][0]["license"]
    # defaults included, not just what the caller typed — a default nobody
    # wrote down is a picture that cannot be reproduced either
    assert out["values"]["negative_prompt"]
    assert out["values"]["quality"] == "draft"


def test_the_fingerprint_changes_when_the_recipe_changes_not_when_inputs_do():
    base = template()
    other_steps = template(computed={
        "width": {"from": "aspect_ratio", "map": {"1:1": 512, "16:9": 1344}}})
    assert base.fingerprint() != other_steps.fingerprint()
    assert mw.render(base, {"prompt": "a"})["fingerprint"] == \
           mw.render(base, {"prompt": "b"})["fingerprint"]


# ── a broken template is caught when it is read ───────────────────────────

def test_a_graph_placeholder_nobody_declared_is_refused_at_load():
    with pytest.raises(TemplateError) as err:
        template(graph={"1": {"class_type": "KSampler",
                              "inputs": {"cfg": "{{mystery}}"}}})
    assert "{{mystery}}" in err.value.message
    assert "declared" in err.value.message


def test_a_computed_value_keyed_off_a_missing_input_is_refused_at_load():
    with pytest.raises(TemplateError) as err:
        template(computed={"width": {"from": "nope", "map": {"a": 1}}})
    assert err.value.path == "workflow.computed.width.from"


def test_a_computed_table_with_a_hole_says_which_value_it_does_not_cover():
    thin = template(computed={"width": {"from": "aspect_ratio", "map": {"1:1": 1024}}})
    with pytest.raises(TemplateError) as err:
        mw.render(thin, {"prompt": "hi", "aspect_ratio": "16:9"})
    assert err.value.path == "computed.width"
    assert "16:9" in err.value.message


def test_an_expression_is_not_an_option():
    """`computed` is a lookup table, deliberately. A template file is data
    somebody pastes; the moment it can express a computation it is code."""
    with pytest.raises(TemplateError) as err:
        template(computed={"width": "aspect_ratio == '1:1' and 1024 or 1344"})
    assert "lookup table" in err.value.message


def test_an_enum_with_no_choices_is_refused():
    with pytest.raises(TemplateError) as err:
        template(inputs={"mode": {"type": "enum"}})
    assert err.value.path == "workflow.inputs.mode.choices"


def test_a_broken_file_in_the_folder_is_reported_not_skipped(tmp_path):
    """A recipe that silently vanished from the list is how somebody spends
    an afternoon wondering why the model refuses to use it."""
    good = tmp_path / "good.json"
    good.write_text(json.dumps(template().to_dict(with_graph=True) | {
        "inputs": {"prompt": {"type": "text", "required": True}},
        "computed": {}, "graph": {"1": {"class_type": "K",
                                        "inputs": {"t": "{{prompt}}"}}},
    }), encoding="utf-8")
    (tmp_path / "bad.json").write_text("{ not json", encoding="utf-8")
    (tmp_path / "wrong.json").write_text(
        json.dumps({"id": "x", "version": "1.0.0", "title": "t",
                    "engine": "comfyui",
                    "graph": {"1": {"inputs": {"a": "{{ghost}}"}}}}),
        encoding="utf-8")

    found = mw.catalogue(directory=str(tmp_path))
    assert [w.id for w in found["workflows"]] == ["image.test"]
    reasons = {b["file"]: b["reason"] for b in found["broken"]}
    assert "bad.json" in reasons and "wrong.json" in reasons
    assert "ghost" in reasons["wrong.json"]


def test_no_template_directory_at_all_is_a_fact_not_a_crash(tmp_path):
    found = mw.catalogue(directory=str(tmp_path / "nowhere"))
    assert found["workflows"] == [] and found["broken"] == []
    assert "no template directory" in found["note"]
