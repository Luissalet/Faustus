"""CMP-09/CMP-12 — GET/PUT /api/strategy/profile, POST /api/strategy/preview,
GET /api/recipes, POST /api/recipes/from-run/{run_id}.

Thin transport over `src/strategy_policy.py` and `src/recipes.py`. Nothing
here computes a strategy or reads a run log itself — see those modules'
docstrings for the actual logic.
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from src import recipes as recipes_mod
from src import strategy_policy
from src.auth_helpers import effective_user


def _error(status: int, error_class: str, detail: str | None = None, **extra: Any) -> JSONResponse:
    """Same flat shape every route in this family uses (see
    routes/attention_routes.py::_error)."""
    body: Dict[str, Any] = {"error_class": error_class}
    if detail is not None:
        body["detail"] = detail
    body.update(extra)
    return JSONResponse(status_code=status, content=body)


def setup_strategy_routes() -> APIRouter:
    router = APIRouter(tags=["strategy"])

    @router.get("/api/strategy/profile")
    async def get_strategy_profile(request: Request, session_id: str | None = None) -> Dict[str, Any]:
        owner = effective_user(request) or ""
        active = strategy_policy.get_active(owner, session_id)
        return {
            "profile": active.get("profile", strategy_policy.DEFAULT_PROFILE),
            "recipe_id": active.get("recipe_id"),
            "profiles": list(strategy_policy.PROFILES),
        }

    @router.put("/api/strategy/profile")
    async def put_strategy_profile(request: Request, session_id: str | None = None) -> Dict[str, Any]:
        owner = effective_user(request) or ""
        try:
            body = await request.json()
        except Exception:
            body = {}
        body = body or {}
        profile = body.get("profile")
        recipe_id = body.get("recipe_id")
        if profile is not None and profile not in strategy_policy.PROFILES:
            return _error(400, "strategy.invalid_profile", f"unknown profile: {profile!r}",
                          profiles=list(strategy_policy.PROFILES))
        try:
            active = strategy_policy.set_active(
                owner, session_id=session_id, profile=profile, recipe_id=recipe_id,
            )
        except ValueError as e:
            return _error(400, "strategy.invalid_profile", str(e))
        return {"ok": True, "profile": active.get("profile"), "recipe_id": active.get("recipe_id")}

    @router.post("/api/strategy/preview")
    async def post_strategy_preview(request: Request) -> Dict[str, Any]:
        owner = effective_user(request) or ""
        try:
            body = await request.json()
        except Exception:
            body = {}
        body = body or {}
        task_text = str(body.get("task_text") or "")
        if not task_text.strip():
            return _error(400, "strategy.missing_task_text", "task_text is required")
        profile = body.get("profile") or strategy_policy.DEFAULT_PROFILE
        if profile not in strategy_policy.PROFILES:
            return _error(400, "strategy.invalid_profile", f"unknown profile: {profile!r}",
                          profiles=list(strategy_policy.PROFILES))
        context = body.get("context") if isinstance(body.get("context"), dict) else {}
        recipe_id = body.get("recipe_id")
        if recipe_id:
            context = dict(context)
            context["recipe_id"] = recipe_id
        strategy = strategy_policy.choose_strategy(task_text, profile=profile, context=context)
        result: Dict[str, Any] = {"strategy": strategy.to_dict()}
        # If the caller also names a `compare_profile`, attach the visible
        # diff for the switch — sparing the UI a second round trip when a
        # user is comparing two profiles for the same task text.
        compare_profile = body.get("compare_profile")
        if compare_profile in strategy_policy.PROFILES:
            result["diff"] = strategy_policy.profile_diff(profile, compare_profile)
        return result

    @router.get("/api/recipes")
    async def list_recipes_route(request: Request) -> Dict[str, Any]:
        owner = effective_user(request) or ""
        recipes = recipes_mod.list_recipes(owner)
        return {"recipes": [r.to_dict() for r in recipes]}

    @router.post("/api/recipes/from-run/{run_id}")
    async def recipe_from_run_route(request: Request, run_id: str) -> Dict[str, Any]:
        owner = effective_user(request) or ""
        if not owner:
            return _error(401, "recipes.no_owner", "sign in to create a recipe")
        try:
            recipe = recipes_mod.from_run(run_id, owner)
        except recipes_mod.RecipeFromRunError as e:
            status = 404 if e.error_class == "recipes.run_not_found" else 400
            return _error(status, e.error_class, e.detail)
        return {"recipe": recipe.to_dict()}

    return router
