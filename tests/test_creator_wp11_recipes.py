"""WP11 — ComfyUI recipes: typed slots over `src.media_workflows` (IMG04,
IMG09).

Covers `src/creator/comfy_recipes.py`, the additive hook in
`src/creator/adapters/comfyui.py`, and `routes/creator_recipe_routes.py`
through a real FastAPI app + TestClient.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from src import media_workflows as workflows
from src.creator import comfy_recipes as cr


# ── compile(): deterministic, validated, review-aware ───────────────────

def test_compile_is_deterministic_same_input_same_graph_same_fingerprint():
    g1 = cr.compile("txt2img", {"prompt": "a red fox in snow", "seed": 42, "steps": 30})
    g2 = cr.compile("txt2img", {"prompt": "a red fox in snow", "seed": 42, "steps": 30})
    assert g1.graph == g2.graph
    assert g1.fingerprint == g2.fingerprint
    assert g1.values == g2.values


def test_compile_different_params_still_reach_the_same_recipe_fingerprint():
    # the fingerprint identifies the RECIPE/template, not one render of it —
    # WP01's MediaWorkflow.fingerprint() is graph/schema identity, not a
    # per-run digest (that is `media_runs._inputs_digest`, a different
    # concern this module does not re-implement).
    g1 = cr.compile("txt2img", {"prompt": "a fox", "seed": 1})
    g2 = cr.compile("txt2img", {"prompt": "a very different scene entirely", "seed": 2})
    assert g1.fingerprint == g2.fingerprint
    assert g1.graph != g2.graph


def test_compile_missing_required_slot_is_rejected_with_target():
    with pytest.raises(cr.RecipeError) as exc:
        cr.compile("txt2img", {"seed": 1})  # no prompt
    assert exc.value.path == "prompt"


def test_compile_out_of_range_slot_is_rejected_with_target():
    with pytest.raises(cr.RecipeError) as exc:
        cr.compile("txt2img", {"prompt": "x", "steps": 999})
    assert exc.value.path == "steps"


def test_compile_unknown_recipe_is_rejected_with_target():
    with pytest.raises(cr.RecipeError) as exc:
        cr.compile("no-such-recipe", {})
    assert exc.value.path == "recipe_id"


def test_compile_dependency_unmet_is_rejected():
    # img2img's `strength` depends on `reference_image` being present at all
    with pytest.raises(cr.RecipeError):
        cr.compile("img2img", {"prompt": "x", "strength": 0.5})  # no reference_image


def test_every_factory_recipe_compiles_and_is_reviewed():
    samples = {
        "txt2img": {"prompt": "a fox"},
        "txt2img-lora": {"prompt": "a fox"},
        "img2img": {"reference_image": "ref.png", "prompt": "a fox, but blue"},
        "inpaint": {"reference_image": "ref.png", "mask": "mask.png", "prompt": "a hat"},
        "controlnet-pose": {"controlnet": "pose.png", "prompt": "a dancer"},
        "controlnet-depth": {"controlnet": "depth.png", "prompt": "a room"},
        "upscale": {"reference_image": "ref.png"},
    }
    for recipe_id in cr._FACTORY:
        graph = cr.compile(recipe_id, samples[recipe_id])
        assert graph.reviewed is True, f"{recipe_id}: {graph.review_reason} {graph.new_nodes}"
        assert graph.fingerprint
        assert graph.graph


def test_compile_batch_produces_one_graph_per_seed_all_deterministic():
    batch1 = cr.compile_batch("txt2img", {"prompt": "a fox"}, seeds=[1, 2, 3])
    batch2 = cr.compile_batch("txt2img", {"prompt": "a fox"}, seeds=[1, 2, 3])
    assert [g.values["seed"] for g in batch1] == [1, 2, 3]
    assert [g.graph for g in batch1] == [g.graph for g in batch2]
    assert len({g.graph["5"]["inputs"]["seed"] for g in batch1}) == 3  # graphs actually differ per seed


def test_compile_batch_requires_at_least_one_seed():
    with pytest.raises(cr.RecipeError):
        cr.compile_batch("txt2img", {"prompt": "a fox"}, seeds=[])


# ── review status: an unapproved recipe never reaches submit ───────────

def test_user_recipe_with_unreviewed_graph_compiles_but_is_not_reviewed(tmp_path):
    db = str(tmp_path / "recipes.sqlite3")
    template = {
        "id": "user.mystery-node", "version": "1.0.0", "title": "Mystery",
        "engine": "comfyui",
        "inputs": {"prompt": {"type": "text", "required": True}},
        "graph": {"1": {"class_type": "SomeNodeNobodyReviewed", "inputs": {"text": "{{prompt}}"}}},
    }
    recipe = cr.create_user_recipe(
        "alice", "p1", title="Mystery", description="", task="custom",
        slots=[{"slot": "prompt", "workflow_input": "prompt", "required": True}],
        template=template, db_path=db,
    )
    graph = cr.compile(recipe.id, {"prompt": "hi"}, owner="alice", db_path=db)
    assert graph.reviewed is False
    assert graph.review_reason == "no_review_record"
    assert "SomeNodeNobodyReviewed" in graph.new_nodes

    # And it has NO path into media_runs.start() at all: the template id
    # was never written under config/media_workflows/, so the on-disk
    # catalogue media_runs.start() reads from does not know it exists —
    # "an unapproved recipe is not submitted" holds structurally, not by
    # an extra check this test could accidentally skip.
    assert workflows.load("user.mystery-node") is None


def test_user_recipe_is_owner_scoped_like_everything_else(tmp_path):
    db = str(tmp_path / "recipes.sqlite3")
    template = {
        "id": "user.thing", "version": "1.0.0", "title": "Thing", "engine": "comfyui",
        "inputs": {"prompt": {"type": "text", "required": True}},
        "graph": {"1": {"class_type": "AnyNode", "inputs": {"text": "{{prompt}}"}}},
    }
    recipe = cr.create_user_recipe(
        "alice", "p1", title="Thing", description="", task="custom",
        slots=[{"slot": "prompt", "workflow_input": "prompt", "required": True}],
        template=template, db_path=db,
    )
    assert cr.get_recipe(recipe.id, owner="alice", db_path=db) is not None
    # "not yours" reads exactly like "does not exist" (CONTRATO rule 3)
    assert cr.get_recipe(recipe.id, owner="mallory", db_path=db) is None
    assert cr.get_recipe(recipe.id, owner="", db_path=db) is None
    with pytest.raises(cr.RecipeError):
        cr.compile(recipe.id, {"prompt": "hi"}, owner="mallory", db_path=db)


def test_create_user_recipe_rejects_bad_template_shape_up_front(tmp_path):
    db = str(tmp_path / "recipes.sqlite3")
    with pytest.raises(cr.RecipeError):
        cr.create_user_recipe(
            "alice", "p1", title="Bad", description="", task="custom", slots=[],
            template={"id": "user.bad"},  # missing version/title/engine/graph
            db_path=db,
        )


def test_create_user_recipe_rejects_unknown_slot_names(tmp_path):
    db = str(tmp_path / "recipes.sqlite3")
    template = {
        "id": "user.thing2", "version": "1.0.0", "title": "Thing", "engine": "comfyui",
        "inputs": {"prompt": {"type": "text", "required": True}},
        "graph": {"1": {"class_type": "N", "inputs": {"text": "{{prompt}}"}}},
    }
    with pytest.raises(cr.RecipeError):
        cr.create_user_recipe(
            "alice", "p1", title="Thing", description="", task="custom",
            slots=[{"slot": "not_a_real_slot", "workflow_input": "prompt"}],
            template=template, db_path=db,
        )


# ── fingerprint changes if `models` changes (WP01 v2, exercised here) ──

def test_fingerprint_changes_when_models_change_same_graph_otherwise():
    base = {
        "id": "test.fp", "version": "1.0.0", "title": "t", "engine": "comfyui",
        "inputs": {"prompt": {"type": "text", "required": True}},
        "models": [{"name": "a.safetensors", "kind": "checkpoint"}],
        "graph": {"1": {"class_type": "CLIPTextEncode", "inputs": {"text": "{{prompt}}"}}},
    }
    changed = {**base, "models": [{"name": "b.safetensors", "kind": "checkpoint"}]}
    w1 = workflows.parse(base, source="t1")
    w2 = workflows.parse(changed, source="t2")
    assert w1.graph == w2.graph  # graph text itself is identical
    assert w1.fingerprint() != w2.fingerprint()  # v2 folds `models` in


def test_controlnet_pose_and_depth_recipes_differ_only_by_model_and_fingerprint():
    pose = workflows.load("image.controlnet-pose")
    depth = workflows.load("image.controlnet-depth")
    assert pose.fingerprint() != depth.fingerprint()
    assert {m.name for m in pose.models} != {m.name for m in depth.models}


# ── src/creator/adapters/comfyui.py — additive recipe_id + params hook ──

def test_adapter_plan_accepts_recipe_id_and_translates_slots_to_workflow_inputs():
    from src.creator.adapters.comfyui import ComfyUIAdapter

    adapter = ComfyUIAdapter()
    plan = adapter.plan("txt2img", {"prompt": "a fox", "seed": 7}, [])
    assert plan.engine_plan["workflow_id"] == "image.txt2img"
    assert plan.engine_plan["inputs"] == {"prompt": "a fox", "seed": 7}


def test_adapter_plan_legacy_raw_workflow_id_is_unaffected():
    from src.creator.adapters.comfyui import ComfyUIAdapter

    adapter = ComfyUIAdapter()
    plan = adapter.plan("image.product", {"prompt": "a fox"}, [])
    assert plan.engine_plan["workflow_id"] == "image.product"
    assert plan.engine_plan["inputs"] == {"prompt": "a fox"}


# ── routes/creator_recipe_routes.py ─────────────────────────────────────

@pytest.fixture()
def route_client(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    from src import settings as settings_mod
    monkeypatch.setattr(settings_mod, "get_setting",
                        lambda key, default=None: True if key == "creator_enabled" else default)
    monkeypatch.setattr(cr, "default_db_path", lambda: str(tmp_path / "recipes.sqlite3"))

    from routes.creator_recipe_routes import setup_creator_recipe_routes
    app = FastAPI()
    app.include_router(setup_creator_recipe_routes())
    return TestClient(app)


def test_route_list_recipes_includes_all_factory_recipes(route_client):
    r = route_client.get("/api/creator/recipes")
    assert r.status_code == 200, r.text
    ids = {row["id"] for row in r.json()["recipes"]}
    assert ids >= set(cr._FACTORY)


def test_route_get_recipe_detail_includes_explain_and_review_status(route_client):
    r = route_client.get("/api/creator/recipes/txt2img")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == "txt2img"
    assert "explain" in body and "prompt" in body["explain"]
    assert body["review_status"]["reviewed"] is True
    assert body["workflow_fingerprint"]


def test_route_get_recipe_404_for_unknown_id(route_client):
    assert route_client.get("/api/creator/recipes/does-not-exist").status_code == 404


def test_route_compile_returns_graph_and_fingerprint_without_submitting(route_client):
    r = route_client.post("/api/creator/recipes/txt2img/compile",
                          json={"params": {"prompt": "a fox", "seed": 5}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["fingerprint"]
    assert "graph" in body
    assert body["values"]["seed"] == 5


def test_route_compile_bad_params_is_422_with_field(route_client):
    r = route_client.post("/api/creator/recipes/txt2img/compile", json={"params": {}})
    assert r.status_code == 422
    assert r.json()["detail"]["field"] == "prompt"


def test_route_compile_batch_with_seeds(route_client):
    r = route_client.post("/api/creator/recipes/txt2img/compile",
                          json={"params": {"prompt": "a fox"}, "seeds": [1, 2, 3]})
    assert r.status_code == 200, r.text
    variants = r.json()["variants"]
    assert len(variants) == 3


def test_route_create_user_recipe_then_compile_it(route_client):
    template = {
        "id": "user.via-route", "version": "1.0.0", "title": "Via route", "engine": "comfyui",
        "inputs": {"prompt": {"type": "text", "required": True}},
        "graph": {"1": {"class_type": "UnreviewedNode", "inputs": {"text": "{{prompt}}"}}},
    }
    r = route_client.post("/api/creator/recipes", json={
        "project_id": "p1", "title": "Via route", "description": "d", "task": "custom",
        "slots": [{"slot": "prompt", "workflow_input": "prompt", "required": True}],
        "template": template,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == "user"
    assert body["review_status"]["reviewed"] is False
    recipe_id = body["id"]

    r2 = route_client.get("/api/creator/recipes")
    assert recipe_id in {row["id"] for row in r2.json()["recipes"]}

    r3 = route_client.post(f"/api/creator/recipes/{recipe_id}/compile", json={"params": {"prompt": "hi"}})
    assert r3.status_code == 200, r3.text
    assert r3.json()["reviewed"] is False


def test_route_create_user_recipe_rejects_bad_template(route_client):
    r = route_client.post("/api/creator/recipes", json={
        "title": "Bad", "description": "", "task": "x", "slots": [],
        "template": {"id": "user.bad"},
    })
    assert r.status_code == 422


def test_routes_404_when_creator_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    from src import settings as settings_mod
    monkeypatch.setattr(settings_mod, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(cr, "default_db_path", lambda: str(tmp_path / "recipes.sqlite3"))

    from routes.creator_recipe_routes import setup_creator_recipe_routes
    app = FastAPI()
    app.include_router(setup_creator_recipe_routes())
    client = TestClient(app)

    assert client.get("/api/creator/recipes").status_code == 404
    assert client.get("/api/creator/recipes/txt2img").status_code == 404
    assert client.post("/api/creator/recipes/txt2img/compile", json={"params": {}}).status_code == 404
    assert client.post("/api/creator/recipes", json={"title": "x"}).status_code == 404
