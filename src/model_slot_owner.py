"""Who owns the local model slot: the turn, not the asyncio task.

`llm_core._local_model_slot` serialises local model traffic and lets the
holder call the model again from inside its own stream (a recovery step
after an aborted stream, whose generator still sits inside the slot). It
used to recognise the holder by `asyncio.current_task()` — but the agent
loop body runs under `desktop_control_session.desktop_control_run`, which
advances the generator one step per new task, so the task that took the
slot was always finished a moment later and a nested call from the same
turn waited on itself forever (live, 24-09-2026, exam run 18; the admin
task snapshot showed the slot "held by" a finished task while the turn
streamed on).

A wrapper that spreads one logical run across tasks sets `SLOT_OWNER` to
an object that identifies the run, in the context it creates those tasks
from, so every step sees the same owner. Without it, the task is the owner,
as before.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from typing import Any, Optional

SLOT_OWNER: ContextVar[Optional[Any]] = ContextVar("faustus_local_model_slot_owner", default=None)


def current_owner() -> Any:
    owner = SLOT_OWNER.get()
    if owner is not None:
        return owner
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None
