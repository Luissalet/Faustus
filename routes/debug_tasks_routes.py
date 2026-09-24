"""Admin-only snapshot of the server's asyncio tasks.

A turn that stops making progress with the model server idle is waiting on
something inside this process: a lock, a queue, a slot. From outside, the
event loop just looks idle (a thread dump shows it in select()). This route
lists every live task with the coroutine frames it is suspended in, and the
local model slot's current holder, so the next hang says where it is.
Read-only; admin only.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List

from fastapi import APIRouter, Request

from core.middleware import require_admin


def _task_entry(task: "asyncio.Task[Any]", frames: int) -> Dict[str, Any]:
    stack = []
    try:
        for frame in task.get_stack(limit=frames):
            code = frame.f_code
            stack.append(f"{code.co_filename.rsplit('/', 1)[-1].rsplit(chr(92), 1)[-1]}:"
                         f"{frame.f_lineno} {code.co_name}")
    except Exception:  # noqa: BLE001 - a task can finish while we look
        pass
    coro = task.get_coro()
    return {
        "name": task.get_name(),
        "coro": getattr(coro, "__qualname__", repr(coro))[:160],
        "done": task.done(),
        "stack": stack,
    }


def snapshot(frames: int = 12, contains: str = "") -> Dict[str, Any]:
    tasks: List[Dict[str, Any]] = []
    for task in asyncio.all_tasks():
        entry = _task_entry(task, frames)
        if contains and not any(contains in line for line in entry["stack"] + [entry["coro"]]):
            continue
        tasks.append(entry)
    slot: Dict[str, Any] = {}
    try:
        from src import llm_core
        holder = dict(llm_core._LOCAL_MODEL_CURRENT)
        task = holder.get("task")
        slot = {
            "locked": llm_core._LOCAL_MODEL_LOCK.locked(),
            "holder_task": task.get_name() if isinstance(task, asyncio.Task) else None,
            "holder_done": task.done() if isinstance(task, asyncio.Task) else None,
            "workload": holder.get("workload"),
            "model": holder.get("model"),
            "held_seconds": round(time.time() - float(holder["started"]), 1) if holder.get("started") else None,
        }
    except Exception:  # noqa: BLE001
        pass
    return {"count": len(tasks), "local_model_slot": slot, "tasks": tasks}


def setup_debug_tasks_routes() -> APIRouter:
    router = APIRouter(prefix="/api/debug")

    @router.get("/tasks")
    async def tasks(request: Request, frames: int = 12, contains: str = ""):
        require_admin(request)
        return snapshot(frames=max(1, min(int(frames), 60)), contains=contains or "")

    return router
