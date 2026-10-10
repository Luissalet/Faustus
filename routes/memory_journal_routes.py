"""Journal of memory changes (#242).

    GET  /api/memory-journal/changes?session_id=&run_id=&store=&limit=&include_undone=
    POST /api/memory-journal/changes/{change_id}/undo

Lists what was created, edited or deleted in the facts store, the learned
memory, the instincts and the second-brain notes, newest first, with the
session and turn that did it; an undo puts one change back and refuses
(409) when the item changed again since. Only the caller's own changes.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request

from src import memory_journal
from src.auth_helpers import get_current_user


def setup_memory_journal_routes(memory_vector: Any = None) -> APIRouter:
    router = APIRouter(prefix="/api/memory-journal", tags=["memory-journal"])

    def _owner(request: Request) -> Optional[str]:
        return get_current_user(request)

    @router.get("/changes")
    async def list_changes(request: Request, session_id: str = "", run_id: str = "", turn_id: str = "",
                           store: str = "", limit: int = 100, include_undone: bool = True) -> Dict[str, Any]:
        if store and store not in memory_journal.STORES:
            raise HTTPException(400, f"store must be one of {', '.join(memory_journal.STORES)}")
        rows = await asyncio.to_thread(memory_journal.list_changes, _owner(request), session_id=session_id,
                                       run_id=run_id, turn_id=turn_id, store=store, limit=limit,
                                       include_undone=include_undone)
        return {"changes": rows, "count": len(rows), "stores": list(memory_journal.STORES)}

    @router.post("/changes/{change_id}/undo")
    async def undo_change(change_id: str, request: Request) -> Dict[str, Any]:
        try:
            return await asyncio.to_thread(memory_journal.undo, change_id, _owner(request),
                                           memory_vector=memory_vector)
        except memory_journal.UndoConflict as exc:
            raise HTTPException(409, str(exc))
        except memory_journal.UndoError as exc:
            raise HTTPException(404 if "unknown" in str(exc) else 400, str(exc))

    return router
