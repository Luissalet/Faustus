"""routes/loop_card_routes.py -- HTTP surface for Loop Cards (src/loop_card.py).

GET    /api/loop-cards[?project=]           the owner's cards, newest first
POST   /api/loop-cards                      {goal, command, extract, direction?, repeats?, cwd?, project?, timeout_s?}  measure and store
GET    /api/loop-cards/{id}                 the whole card (baseline runs, check history)
POST   /api/loop-cards/{id}/check           {command?, repeats?, rebase?, change?}  measure again: win / loss / noise
POST   /api/loop-cards/{id}/plan            {keep_bar_pct?, keep_bar_abs?, min_pairs?, guard_command?, stop_rule?, timebox_min?, reason?}
POST   /api/loop-cards/{id}/arms            {arms: [{name, command?, cwd?, label?}], rounds?, min_pairs?, aa?}  paired arms vs the control
DELETE /api/loop-cards/{id}

GET    /api/loop-improve[?card=]            improvement runs (src/loop_arms.py), newest first
POST   /api/loop-improve                    {id, files, proposals?, iterations?, rounds?, guard_command?, apply?, model?, hints?}
GET    /api/loop-improve/{job}              one run with its hypotheses, arms, verdicts and log
POST   /api/loop-improve/{job}/cancel
DELETE /api/loop-improve/{job}

GET    /api/prompt-mirror                   earlier prompt-mirror runs
POST   /api/prompt-mirror                   {days?, redact_terms?}  scorecard of how the owner directs Faustus (src/prompt_mirror.py)
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from core.middleware import require_admin
from src import loop_arms, loop_card, prompt_mirror
from src.auth_helpers import effective_user


def _reply(result: Dict[str, Any]):
    if result.get("error_class"):
        status = 404 if str(result["error_class"]).endswith("not_found") else 400
        return JSONResponse(status_code=status, content=result)
    return result


async def _json(request: Request) -> Dict[str, Any]:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    return body if isinstance(body, dict) else {}


def setup_loop_card_routes() -> APIRouter:
    router = APIRouter(tags=["loop-cards"])

    def owner_of(request: Request) -> str:
        require_admin(request)
        return effective_user(request) or ""

    @router.get("/api/loop-cards")
    async def list_cards(request: Request, project: str = ""):
        return _reply(loop_card.run(owner_of(request), {"action": "list", "project": project or None}))

    @router.post("/api/loop-cards")
    async def create_card(request: Request):
        owner = owner_of(request)
        return _reply(await asyncio.to_thread(loop_card.run, owner, {**(await _json(request)), "action": "create"}))

    @router.get("/api/loop-cards/{card_id}")
    async def get_card(card_id: str, request: Request):
        return _reply(loop_card.run(owner_of(request), {"action": "get", "id": card_id}))

    @router.post("/api/loop-cards/{card_id}/check")
    async def check_card(card_id: str, request: Request):
        owner = owner_of(request)
        return _reply(await asyncio.to_thread(loop_card.run, owner, {**(await _json(request)), "action": "check", "id": card_id}))

    @router.post("/api/loop-cards/{card_id}/plan")
    async def plan_card(card_id: str, request: Request):
        owner = owner_of(request)
        return _reply(loop_card.run(owner, {**(await _json(request)), "action": "plan", "id": card_id}))

    @router.post("/api/loop-cards/{card_id}/arms")
    async def arms_card(card_id: str, request: Request):
        owner = owner_of(request)
        return _reply(await asyncio.to_thread(loop_arms.run_arms_action, owner, {**(await _json(request)), "id": card_id}))

    @router.get("/api/loop-improve")
    async def list_improve(request: Request, card: str = ""):
        return _reply(await loop_arms.run_improve_action(owner_of(request), {"action": "list", "id": card or None}))

    @router.post("/api/loop-improve")
    async def start_improve(request: Request):
        owner = owner_of(request)
        body = await _json(request)
        body.pop("wait_s", None)
        return _reply(await loop_arms.run_improve_action(owner, {**body, "action": "start"}))

    @router.get("/api/loop-improve/{job_id}")
    async def get_improve(job_id: str, request: Request):
        return _reply(await loop_arms.run_improve_action(owner_of(request), {"action": "status", "job": job_id}))

    @router.post("/api/loop-improve/{job_id}/cancel")
    async def cancel_improve(job_id: str, request: Request):
        return _reply(await loop_arms.run_improve_action(owner_of(request), {"action": "cancel", "job": job_id}))

    @router.delete("/api/loop-improve/{job_id}")
    async def delete_improve(job_id: str, request: Request):
        return _reply(await loop_arms.run_improve_action(owner_of(request), {"action": "delete", "job": job_id}))

    @router.get("/api/prompt-mirror")
    async def mirror_history(request: Request):
        return _reply(prompt_mirror.history(owner_of(request)))

    @router.post("/api/prompt-mirror")
    async def mirror_run(request: Request):
        owner = owner_of(request)
        data = await asyncio.to_thread(prompt_mirror.run, owner, await _json(request))
        if not data.get("error"):
            data["report"] = prompt_mirror.render(data)
        return _reply(data)

    @router.delete("/api/loop-cards/{card_id}")
    async def delete_card(card_id: str, request: Request):
        return _reply(loop_card.run(owner_of(request), {"action": "delete", "id": card_id}))

    return router
