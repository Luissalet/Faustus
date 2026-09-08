"""Desktop host indicator handshake and Escape cancellation for agent runs."""
from __future__ import annotations

import asyncio
import contextvars
import functools
import json
from pathlib import Path
import time
import uuid

RUNTIME = Path(__file__).resolve().parents[1] / "data" / "runtime"
_run = contextvars.ContextVar("desktop_control_run", default=None)


def _read(name):
    try:
        return json.loads((RUNTIME / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write(name, value):
    RUNTIME.mkdir(parents=True, exist_ok=True)
    path = RUNTIME / name
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value), encoding="utf-8")
    tmp.replace(path)


def cancelled(state):
    return _read("desktop-cancel.json").get("token") == state["token"]


def _heartbeat(state):
    if state["active"]:
        _write("desktop-control.json", {"token": state["token"], "updated": time.time()})


async def ensure_indicator():
    state = _run.get()
    if state is None:
        raise RuntimeError("Desktop control requires an active agent run in the Faustus desktop app.")
    if cancelled(state):
        raise asyncio.CancelledError("Desktop control stopped with Escape")
    other = _read("desktop-control.json")
    if other.get("token") not in (None, state["token"]) and time.time() - other.get("updated", 0) < 5:
        raise RuntimeError("Another task is controlling the desktop. Stop it first.")
    state["active"] = True
    _heartbeat(state)
    for _ in range(40):
        if cancelled(state):
            raise asyncio.CancelledError("Desktop control stopped with Escape")
        ack = _read("desktop-control-ack.json")
        if ack.get("token") == state["token"]:
            if ack.get("ready"):
                return
            raise RuntimeError(ack.get("error") or "Desktop indicator is unavailable")
        await asyncio.sleep(0.1)
    raise RuntimeError("Open the Faustus desktop app: the screen indicator and Escape stop must be ready before desktop control.")


def desktop_control_run(function):
    @functools.wraps(function)
    async def wrapped(*args, **kwargs):
        state = {"token": uuid.uuid4().hex, "active": False}
        context = _run.set(state)
        iterator = function(*args, **kwargs)
        pending = None
        beat = 0.0
        try:
            while True:
                pending = asyncio.create_task(anext(iterator))
                while not pending.done():
                    await asyncio.wait({pending}, timeout=0.1)
                    if state["active"] and cancelled(state):
                        pending.cancel()
                        await asyncio.gather(pending, return_exceptions=True)
                        yield 'data: {"delta":"Control de pantalla detenido con Esc."}\n\n'
                        yield "data: [DONE]\n\n"
                        return
                    if time.monotonic() - beat > 1:
                        _heartbeat(state)
                        beat = time.monotonic()
                try:
                    chunk = pending.result()
                except StopAsyncIteration:
                    return
                yield chunk
        finally:
            if pending and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            await iterator.aclose()
            if _read("desktop-control.json").get("token") == state["token"]:
                _write("desktop-control.json", {})
            _run.reset(context)
    return wrapped
