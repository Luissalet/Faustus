"""Owner-scoped coworker configuration. Runs stay in the existing chat tool path."""
import asyncio
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from core.middleware import require_admin
from src.auth_helpers import effective_user
from src import coworkers


class SaveBody(BaseModel):
    model_config=ConfigDict(extra='forbid')
    coworker: coworkers.Coworker
    id: str | None = Field(None,pattern=r'^[a-f0-9]{32}$')
    expected_revision: int | None = Field(None,ge=1)


def setup_coworker_routes():
    router=APIRouter(prefix='/api/coworkers',tags=['coworkers'],dependencies=[Depends(require_admin)])

    @router.get('')
    async def list_coworkers(request:Request):
        return {"coworkers":await asyncio.to_thread(coworkers.Store().list,effective_user(request)),"templates":coworkers.templates()}

    @router.post('')
    async def save_coworker(request:Request,body:SaveBody):
        try:
            from src import agent_defs
            if agent_defs.get(body.coworker.agent) is None:
                raise ValueError('Choose an existing agent definition')
            return await asyncio.to_thread(coworkers.Store().save,effective_user(request),body.coworker,body.id,body.expected_revision)
        except ValueError as exc:
            raise HTTPException(400,str(exc)) from exc

    @router.get('/{id}')
    async def get_coworker(request:Request,id:str):
        try:
            store=coworkers.Store();owner=effective_user(request)
            return {"coworker":store.get(owner,id),"runs":store.history(owner,id)}
        except LookupError as exc:
            raise HTTPException(404,str(exc)) from exc

    @router.post('/{id}/runs/{request_id}/close')
    async def close_mission(request:Request,id:str,request_id:str):
        try:
            return await asyncio.to_thread(coworkers.Store().resolve,effective_user(request),id,request_id)
        except (ValueError,LookupError) as exc:
            raise HTTPException(400,str(exc)) from exc
    return router
