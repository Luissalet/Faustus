"""routes/loop_watchdog_routes.py - what the event-loop watchdog (src/loop_watchdog) has seen.

    GET /api/diag/loop?limit=10&full=false

`full=true` adds the captured stacks and what the other threads were doing. Admin only: stacks show server paths.
The watchdog itself runs from startup; this only reads it (this process's counters, plus the log of earlier runs).
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict

from fastapi import APIRouter, Depends

from core.constants import DATA_DIR
from core.middleware import require_admin
from src import loop_watchdog


def setup_loop_watchdog_routes() -> APIRouter:
    router = APIRouter(prefix="/api/diag", tags=["diagnostics"], dependencies=[Depends(require_admin)])

    @router.get("/loop")
    async def loop_stalls(limit: int = 10, full: bool = False) -> Dict[str, Any]:
        rows = await asyncio.to_thread(loop_watchdog.read_log, DATA_DIR, limit, full=full)
        return {**loop_watchdog.status(), "log_rows": rows}

    return router
