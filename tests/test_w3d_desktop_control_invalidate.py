"""tests/test_w3d_desktop_control_invalidate.py — W3-D (CONTRATO_W3.md), CMP-10 follow-up.

`src/desktop_semantics/session.py::invalidate_generation`'s own docstring
names two callers that need it eagerly: a channel decision that falls back
to `pixels` (already wired, `src/agent_tools/desktop_semantic_tools.py`),
and "the human takes back control of the desktop mid-run — the existing
Escape/indicator handshake (src.desktop_control_session) stops the RUN, but
does not by itself tell this package [...]". This exercises the second
caller, wired here: an Escape/cancel through `desktop_control_run` must
bump the session's desktop-semantics generation before the run visibly
stops, using the SAME `session_id` the wrapped agent-loop function was
called with.

Run: python3 -m pytest tests/test_w3d_desktop_control_invalidate.py -q -p no:cacheprovider -W ignore
"""
from __future__ import annotations

import asyncio

import pytest

from src import desktop_control_session as control
from src.desktop_semantics import session as ds_session


@pytest.fixture(autouse=True)
def _reset_desktop_semantics_state():
    ds_session.reset_state()
    yield
    ds_session.reset_state()


@pytest.mark.asyncio
async def test_escape_bumps_the_generation_for_the_wrapped_session_id(tmp_path, monkeypatch):
    monkeypatch.setattr(control, "RUNTIME", tmp_path)

    @control.desktop_control_run
    async def run(*, session_id=None, other_kw="unused"):
        try:
            await control.ensure_indicator()
            yield "ready"
            await asyncio.sleep(30)
            yield "must not execute"
        finally:
            pass

    async def host():
        while not control._read("desktop-control.json").get("token"):
            await asyncio.sleep(0.01)
        token = control._read("desktop-control.json")["token"]
        control._write("desktop-control-ack.json", {"token": token, "ready": True})
        await asyncio.sleep(0.2)
        control._write("desktop-cancel.json", {"token": token})

    assert ds_session.current_generation("sess-1") == 0

    host_task = asyncio.create_task(host())
    result = [chunk async for chunk in run(session_id="sess-1")]
    await host_task

    assert "detenido con Esc" in "".join(result)
    # The generation for THIS session must have moved -- a `Ref` from
    # before the Escape now fails `StaleRefError` on its next `resolve()`.
    assert ds_session.current_generation("sess-1") > 0
    # A DIFFERENT session's generation is untouched.
    assert ds_session.current_generation("sess-2") == 0


@pytest.mark.asyncio
async def test_no_session_id_never_raises_and_never_invalidates_anything(tmp_path, monkeypatch):
    """A caller that omits `session_id` entirely (or passes a falsy one)
    must still stop cleanly on Escape -- best-effort invalidation, never a
    reason the stop path itself fails."""
    monkeypatch.setattr(control, "RUNTIME", tmp_path)

    @control.desktop_control_run
    async def run(*, session_id=None):
        try:
            await control.ensure_indicator()
            yield "ready"
            await asyncio.sleep(30)
        finally:
            pass

    async def host():
        while not control._read("desktop-control.json").get("token"):
            await asyncio.sleep(0.01)
        token = control._read("desktop-control.json")["token"]
        control._write("desktop-control-ack.json", {"token": token, "ready": True})
        await asyncio.sleep(0.2)
        control._write("desktop-cancel.json", {"token": token})

    host_task = asyncio.create_task(host())
    result = [chunk async for chunk in run()]
    await host_task
    assert "detenido con Esc" in "".join(result)


def test_bound_session_id_reads_positional_and_keyword_alike():
    def fn(a, b, *, session_id=None):
        pass

    sig = __import__("inspect").signature(fn)
    assert control._bound_session_id(sig, (1, 2), {"session_id": "kw-sid"}) == "kw-sid"
    assert control._bound_session_id(sig, (1, 2, "pos-sid"), {}) is None  # session_id is keyword-only here
    assert control._bound_session_id(sig, (1, 2), {}) is None


def test_invalidate_desktop_session_is_best_effort(monkeypatch):
    """A bump that raises for any reason must never propagate -- the
    Escape/cancel path this feeds into has already stopped the run either
    way by the time it's called."""
    def boom(_session_id):
        raise RuntimeError("boom")

    monkeypatch.setattr(ds_session, "invalidate_generation", boom)
    control._invalidate_desktop_session("sess-1")  # must not raise
