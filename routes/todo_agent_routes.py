"""routes/todo_agent_routes.py -- assign a todo to an agent.

POST /api/todos/{id}/assign-agent  {mode: "agent", project_id?, autonomy_preset?, model?}
GET  /api/todos/{id}/agent         the run record (status, session, summary)

The "todo" is a note of type todo/checklist (Notas). See src/todo_agent.py.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from core.database import Note, SessionLocal
from src import todo_agent
from src.auth_helpers import require_user


class AssignBody(BaseModel):
    mode: str = "agent"
    project_id: Optional[str] = None
    autonomy_preset: Optional[str] = None
    model: Optional[str] = None


def _load(request: Request, todo_id: str) -> Dict[str, Any]:
    user = require_user(request) or None
    db = SessionLocal()
    try:
        note = db.query(Note).filter(Note.id == todo_id).first()
        if not note or (user is not None and note.owner != user):
            raise HTTPException(404, "Todo not found")
        items = None
        if note.items:
            try:
                items = json.loads(note.items)
            except (TypeError, ValueError):
                items = None
        return {"id": note.id, "owner": user, "title": note.title, "content": note.content,
                "items": items, "due_date": note.due_date,
                "agent_run_raw": getattr(note, "agent_run", None)}
    finally:
        db.close()


def setup_todo_agent_routes() -> APIRouter:
    router = APIRouter(prefix="/api/todos", tags=["todos"])

    @router.post("/{todo_id}/assign-agent")
    async def assign_agent(request: Request, todo_id: str, body: AssignBody):
        note = _load(request, todo_id)
        try:
            run = todo_agent.assign(note, owner=note["owner"], mode=body.mode,
                                    project_id=body.project_id or "",
                                    autonomy_preset=body.autonomy_preset,
                                    model=body.model or "")
        except todo_agent.AssignError as exc:
            raise HTTPException(exc.status, exc.message)
        return {"ok": True, "todo_id": todo_id, "run": run}

    @router.get("/{todo_id}/agent")
    def get_agent_run(request: Request, todo_id: str):
        note = _load(request, todo_id)
        return {"todo_id": todo_id, "run": todo_agent.current(todo_id, note["agent_run_raw"])}

    return router
