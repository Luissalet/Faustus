"""Reproducible generation recipes (plan §12, required tests in §22 "Multimodal").

The five the plan names: a recipe keeps model, version, seed and references; it
never proposes parameters the available model cannot run; a missing asset is
flagged; derivatives keep the relation to their parent; and a project's
preferences do not contaminate the global profile.  The rest pin the two rules
that make those five hold — hard compatibility runs before ranking, and an
explicit rating is a different signal from redoing one part of a result.
"""

from __future__ import annotations

import pytest

from src.context_engine import multimodal_memory as mm
from src.context_engine import store


@pytest.fixture(autouse=True)
def context_store(tmp_path):
    store.use_path(str(tmp_path / "ce.db"))
    try:
        yield
    finally:
        store.use_path(None)


def _recipe(**over):
    payload = {
        "owner": "alice",
        "media_type": "image",
        "request": "a product shot of the blue mug on a linen backdrop",
        "model": "flux.1-dev",
        "model_version": "2024-08",
        "provider": "comfyui",
        "prompt": "blue ceramic mug, linen backdrop, soft window light",
        "negative_prompt": "text, watermark",
        "transformations": ["upscale-2x"],
        "input_refs": ["artifact:art_1"],
        "asset_hashes": ["a" * 64],
        "params": {"sampler": "dpmpp_2m", "scheduler": "karras", "steps": 28,
                   "cfg": 4.5, "width": 1024, "height": 1024,
                   "loras": {"product-sheen": 0.4}, "clip_skip": 2},
        "seed": "9007199254740991",
        "workflow_ref": "image.product@1.0.0#abc123",
        "tools": ["comfyui"],
        "artifact_ids": ["art_9"],
        "cost": {"duration_ms": 42000, "hardware": "RTX 4090", "vram_mb": 18000},
        "license_note": "FLUX.1-dev non-commercial",
    }
    payload.update(over)
    return mm.record(**payload)


# ── what a recipe has to remember ─────────────────────────────────────────

def test_a_recipe_keeps_model_version_seed_and_references():
    recipe = _recipe()
    stored = mm.get(recipe.id)

    assert stored == recipe
    assert stored.model == "flux.1-dev"
    assert stored.model_version == "2024-08"
    assert stored.seed == "9007199254740991", "a seed is text; 2**53 does not survive a float"
    assert stored.input_refs == ("artifact:art_1",)
    assert stored.asset_hashes == ("a" * 64,)
    assert stored.artifact_ids == ("art_9",)
    assert stored.workflow_ref == "image.product@1.0.0#abc123"
    assert stored.params["loras"] == {"product-sheen": 0.4}
    assert stored.cost["vram_mb"] == 18000
    assert stored.license_note == "FLUX.1-dev non-commercial"


def test_a_recipe_round_trips_through_its_own_dict():
    recipe = _recipe()
    assert mm.GenerationRecipe.parse(recipe.to_dict()) == recipe


def test_an_unknown_field_is_refused_rather_than_ignored():
    with pytest.raises(ValueError):
        _recipe(sampler="dpmpp_2m")


def test_params_stay_open_but_typed():
    recipe = _recipe(media_type="audio", model="kokoro",
                     params={"voice": "af_heart", "language": "es", "speed": 1.1,
                             "prosody": {"pitch": -2}, "segmentation": ["sentence"]})
    assert mm.get(recipe.id).params["prosody"] == {"pitch": -2}
    with pytest.raises(ValueError):
        _recipe(params={"callback": object()})


# ── hard compatibility, before any ranking ────────────────────────────────

def test_search_drops_a_recipe_the_installed_models_cannot_run():
    recipe = _recipe(model="flux.1-dev")

    assert mm.search("mug", owner="alice", available_models=["sdxl-base"]) == []

    kept = mm.search("mug", owner="alice", available_models=["flux.1-dev"])
    assert [h["recipe"].id for h in kept] == [recipe.id]


def test_a_namespaced_model_id_still_matches():
    recipe = _recipe(model="flux.1-dev")
    hits = mm.search("mug", owner="alice", available_models=["local/flux.1-dev"])
    assert [h["recipe"].id for h in hits] == [recipe.id]


def test_compatible_explains_the_refusal():
    recipe = _recipe()
    verdict = mm.compatible(recipe, available_models=["sdxl-base"],
                            available_workflows=["image.portrait@2.0.0"])

    assert verdict["ok"] is False
    reasons = {m["what"] for m in verdict["missing"]}
    assert {"model", "workflow"} <= reasons
    assert all(m["detail"] for m in verdict["missing"]), "a refusal without a detail"


def test_compatible_refuses_nothing_it_cannot_check():
    verdict = mm.compatible(_recipe())
    assert verdict == {"ok": True, "missing": [], "degraded": False}


def test_a_missing_asset_is_flagged_not_ignored():
    verdict = mm.compatible(_recipe(), available_models=["flux.1-dev"],
                            asset_exists=lambda ref: False)

    assert verdict["ok"] is True, "a lost reference does not stop a new render"
    assert verdict["degraded"] is True
    assert {"what": "asset", "detail": "artifact:art_1"} in verdict["missing"]


def test_a_recipe_with_no_seed_or_version_runs_but_is_degraded():
    verdict = mm.compatible(_recipe(seed="", model_version=""),
                            available_models=["flux.1-dev"])
    assert verdict["ok"] is True
    assert verdict["degraded"] is True
    assert {"seed", "model_version"} <= {m["what"] for m in verdict["missing"]}


def test_an_unreproducible_recipe_is_never_offered():
    recipe = _recipe(status="unreproducible")
    assert mm.search("mug", owner="alice") == []
    assert mm.compatible(recipe)["ok"] is False


def test_search_filters_by_media_type():
    picture = _recipe(media_type="image")
    _recipe(media_type="video", model="wan-2.1", request="the mug, rotating")

    hits = mm.search("mug", owner="alice", media_type="image", k=10)
    assert [h["recipe"].id for h in hits] == [picture.id]


# ── derivatives ───────────────────────────────────────────────────────────

def test_derive_keeps_the_parent_and_never_its_artifacts():
    parent = _recipe()
    mm.rate(parent.id, rating=5)
    mm.note_rejection(parent.id, part="hands")

    child = mm.derive(parent.id, prompt="green ceramic mug, linen backdrop")

    assert child.id != parent.id
    assert child.parent_id == parent.id
    assert mm.get(child.id).parent_id == parent.id
    assert child.artifact_ids == (), "a derivative claiming its parent's outputs"
    assert (child.rating, child.rejections, child.issues) == (0, 0, ())
    # what is reproducible does travel
    assert child.model == parent.model
    assert child.model_version == parent.model_version
    assert child.seed == parent.seed
    assert child.workflow_ref == parent.workflow_ref
    assert child.params == parent.params
    assert child.license_note == parent.license_note
    assert child.prompt == "green ceramic mug, linen backdrop"


def test_replacing_the_inputs_drops_the_parents_asset_hashes():
    parent = _recipe()
    child = mm.derive(parent.id, input_refs=["artifact:art_2"])

    assert child.input_refs == ("artifact:art_2",)
    assert child.asset_hashes == (), "hashes of references it no longer uses"


def test_derive_refuses_to_inherit_a_reception():
    parent = _recipe()
    for field, value in (("rating", 5), ("rejections", 2),
                         ("issues", ["hands"]), ("artifact_ids", ["art_1"])):
        with pytest.raises(ValueError):
            mm.derive(parent.id, **{field: value})


def test_derive_needs_a_parent_that_exists():
    with pytest.raises(ValueError):
        mm.derive("recipe_nope", prompt="anything")


# ── strong and weak signals ───────────────────────────────────────────────

def test_rate_is_strong_and_note_rejection_is_local():
    recipe = _recipe()
    mm.rate(recipe.id, rating=5)
    before = mm.search("mug", owner="alice")[0]
    assert before["signals"]["rating"] == 1.0

    after = mm.note_rejection(recipe.id, part="hands", note="six fingers")

    assert after.rating == 5, "redoing one part is not a bad rating"
    assert after.rejections == 1
    assert after.issues == ("hands: six fingers",)

    hit = mm.search("mug", owner="alice")[0]
    assert hit["signals"]["rating"] == 1.0
    assert hit["signals"]["reproducibility"] < before["signals"]["reproducibility"]


def test_a_rejection_has_to_name_the_part():
    recipe = _recipe()
    with pytest.raises(ValueError):
        mm.note_rejection(recipe.id, part="  ")


def test_rate_refuses_a_score_outside_the_scale():
    recipe = _recipe()
    for bad in (-1, 6, True):
        with pytest.raises(ValueError):
            mm.rate(recipe.id, rating=bad)
    assert mm.get(recipe.id).rating == 0


def test_rate_and_note_rejection_on_a_missing_recipe_answer_none():
    assert mm.rate("recipe_nope", rating=3) is None
    assert mm.note_rejection("recipe_nope", part="hands") is None


def test_using_a_file_again_is_not_modelled_as_a_preference():
    # §12.4: downloading or reusing is a weak signal at best. This module would
    # rather hold no opinion than a wrong one, so there is no verb for it.
    for verb in ("reuse", "download", "view", "open", "like"):
        assert not hasattr(mm, verb)


def test_the_rated_recipe_wins_a_tie():
    plain = _recipe(request="a product shot of the blue mug on a linen backdrop")
    loved = _recipe(request="a product shot of the blue mug on a linen backdrop")
    mm.rate(loved.id, rating=5)

    hits = mm.search("blue mug linen", owner="alice", k=10)
    assert [h["recipe"].id for h in hits][0] == loved.id
    assert plain.id in [h["recipe"].id for h in hits]


# ── a project's taste stays in the project ────────────────────────────────

def test_project_preferences_do_not_contaminate_the_global_profile():
    scoped = _recipe(project_id="campaign-a")
    mm.rate(scoped.id, rating=5)
    everywhere = _recipe(project_id="")

    in_project = {h["recipe"].id: h for h in
                  mm.search("mug", owner="alice", project_id="campaign-a", k=10)}
    assert in_project[scoped.id]["signals"]["rating"] == 1.0

    globally = {h["recipe"].id: h for h in mm.search("mug", owner="alice", k=10)}
    assert scoped.id in globally, "it is still the same person's work"
    assert globally[scoped.id]["signals"]["rating"] == 0.0
    assert globally[everywhere.id]["signals"]["rating"] == 0.0


def test_a_recipe_from_another_project_is_not_offered():
    scoped = _recipe(project_id="campaign-a")
    hits = mm.search("mug", owner="alice", project_id="campaign-b", k=10)
    assert scoped.id not in [h["recipe"].id for h in hits]


def test_search_does_not_mix_owners():
    mine = _recipe(owner="alice")
    _recipe(owner="bob")
    assert [h["recipe"].id for h in mm.search("mug", owner="alice", k=10)] == [mine.id]


# ── what the compiler is handed ───────────────────────────────────────────

def test_candidates_carry_the_compact_recipe_and_not_the_log():
    recipe = _recipe()
    hits = mm.search("mug", owner="alice", available_models=["flux.1-dev"])
    candidate = mm.as_candidates(hits)[0]

    assert candidate.source_type == "recipe"
    assert candidate.section == "multimodal_recipes"
    assert candidate.source_ref == f"recipe:{recipe.id}"
    assert candidate.degraded is False
    assert candidate.meta["seed"] == recipe.seed
    assert candidate.meta["model_version"] == "2024-08"
    assert "image.product@1.0.0#abc123" in candidate.body
    assert "dpmpp_2m" in candidate.body


def test_a_degraded_hit_says_so_in_the_candidate():
    _recipe(seed="")
    candidate = mm.as_candidates(mm.search("mug", owner="alice"))[0]

    assert candidate.degraded is True
    assert any(m["what"] == "seed" for m in candidate.meta["missing"])


def test_stats_counts_the_recipe_book():
    first = _recipe()
    mm.rate(first.id, rating=4)
    mm.derive(first.id, prompt="a green one")
    _recipe(media_type="video", model="wan-2.1", status="unreproducible")

    summary = mm.stats(owner="alice")
    assert summary["total"] == 3
    assert summary["by_media_type"] == {"image": 2, "video": 1}
    assert summary["by_status"] == {"active": 2, "unreproducible": 1}
    assert summary["rated"] == 1
    assert summary["derived"] == 1
    assert summary["with_seed"] == 3


def test_a_bare_string_of_names_is_refused_rather_than_read_by_character():
    recipe = _recipe()
    with pytest.raises(ValueError):
        mm.search("mug", owner="alice", available_models="flux.1-dev")
    with pytest.raises(ValueError):
        mm.compatible(recipe, available_workflows="image.product@1.0.0")
