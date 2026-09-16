"""Creator ComfyUI recipe routes (WP11), over ``src.creator.comfy_recipes``.

Same discipline as every other ``routes/creator_*`` module: gated on the
``creator_enabled`` setting (default False, checked before any store
access), owner always resolved from the authenticated session — never the
body (CONTRATO.md rule 3).

  GET  /api/creator/recipes              Every factory recipe, plus this
                                          owner's own user recipes.
  GET  /api/creator/recipes/{id}         One recipe: slots, task, a
                                          readable ``explain()``, and the
                                          underlying template's review
                                          status.
  POST /api/creator/recipes/{id}/compile Compile params (keyed by slot
                                          name) to a graph + fingerprint —
                                          NEVER submits. ``seeds: [...]``
                                          in the body instead of a single
                                          ``params`` compiles a batch of
                                          variants, one graph per seed.
  POST /api/creator/recipes              Create a user recipe from a
                                          caller-supplied template body —
                                          it is parsed up front (bad shape
                                          refused immediately) but stays
                                          UNREVIEWED: it has no path to
                                          ``media_runs.start()`` at all
                                          (``comfy_recipes`` module
                                          docstring), so it can be
                                          explained and compiled, never
                                          submitted, until a human adds it
                                          to
                                          ``config/media_workflows/reviews/
                                          approved_recipes.json``.

Submitting a recipe is WP10's job — ``POST /api/creator/adapters/comfyui/
plan`` and ``.../submit`` already accept a recipe id as ``op`` (the
additive hook in ``src/creator/adapters/comfyui.py``); this module never
calls ``media_runs`` itself.
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from src.auth_helpers import require_user
from src.creator import comfy_recipes as cr


def _owner(request: Request) -> str:
    from core import middleware as mw
    from src.owner_identity import effective_storage_owner
    user = require_user(request)
    return effective_storage_owner(user, auth_is_disabled=mw.auth_disabled()) or ""


def _require_flag() -> None:
    from src.settings import get_setting
    if not bool(get_setting("creator_enabled", False)):
        raise HTTPException(status_code=404, detail="creator is not enabled")


async def _json_object(request: Request) -> Dict[str, Any]:
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "Request body must be JSON")
    if not isinstance(payload, dict):
        raise HTTPException(400, "Request body must be a JSON object")
    return payload


def setup_creator_recipe_routes() -> APIRouter:
    router = APIRouter(prefix="/api/creator", tags=["creator-recipes"])

    @router.get("/recipes")
    async def list_recipes(request: Request) -> Dict[str, Any]:
        owner = _owner(request)
        _require_flag()
        project_id = str(request.query_params.get("project_id") or "")
        recipes = cr.catalogue(owner=owner, project_id=project_id)
        return {"recipes": [r.to_dict() for r in recipes]}

    @router.get("/recipes/{recipe_id}")
    async def get_recipe(recipe_id: str, request: Request) -> Dict[str, Any]:
        owner = _owner(request)
        _require_flag()
        recipe = cr.get_recipe(recipe_id, owner=owner)
        if recipe is None:
            raise HTTPException(404, "no such recipe")
        return cr.describe(recipe)

    @router.post("/recipes/{recipe_id}/compile")
    async def compile_recipe(recipe_id: str, request: Request) -> Dict[str, Any]:
        owner = _owner(request)
        _require_flag()
        body = await _json_object(request)
        params = body.get("params")
        if params is not None and not isinstance(params, dict):
            raise HTTPException(400, "params must be an object")
        seeds = body.get("seeds")
        if seeds is not None:
            if not isinstance(seeds, list) or not all(isinstance(s, int) and not isinstance(s, bool) for s in seeds):
                raise HTTPException(400, "seeds must be a list of integers")
            try:
                graphs = cr.compile_batch(recipe_id, params or {}, seeds=seeds, owner=owner)
            except cr.RecipeError as e:
                raise HTTPException(422, {"field": e.path, "message": e.message})
            return {"recipe_id": recipe_id, "variants": [g.to_dict() for g in graphs]}

        try:
            graph = cr.compile(recipe_id, params or {}, owner=owner)
        except cr.RecipeError as e:
            raise HTTPException(422, {"field": e.path, "message": e.message})
        return graph.to_dict()

    @router.post("/recipes")
    async def create_recipe(request: Request) -> Dict[str, Any]:
        owner = _owner(request)
        _require_flag()
        if not owner:
            raise HTTPException(401, "a user recipe needs an authenticated owner")
        body = await _json_object(request)
        project_id = str(body.get("project_id") or "")
        title = str(body.get("title") or "")
        description = str(body.get("description") or "")
        task = str(body.get("task") or "")
        slots = body.get("slots")
        template = body.get("template")
        if not isinstance(slots, list):
            raise HTTPException(400, "slots must be a list")
        if not isinstance(template, dict):
            raise HTTPException(400, "template must be an object (same shape as a config/media_workflows/*.json file)")
        try:
            recipe = cr.create_user_recipe(
                owner, project_id, title=title, description=description, task=task,
                slots=slots, template=template,
            )
        except cr.RecipeError as e:
            raise HTTPException(422, {"field": e.path, "message": e.message})
        return cr.describe(recipe)

    return router
