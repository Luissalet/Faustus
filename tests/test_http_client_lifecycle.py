"""#1093 HTTP clients must close on their owner loop.

GREEN contract from research #1054 / tcp-native21. No model, DB, or product
network: only local loops and (where needed) real httpx clients.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import threading
from pathlib import Path

import pytest

from src import llm_core


ROOT = Path(__file__).resolve().parents[1]


def _reset_pool(monkeypatch):
    monkeypatch.setattr(llm_core, "_http_client", None)
    monkeypatch.setattr(llm_core, "_http_client_owner", None)
    # Fresh WeakKeyDictionary so prior loops do not leak into this test.
    import weakref

    monkeypatch.setattr(llm_core, "_loop_http_clients", weakref.WeakKeyDictionary())


async def _get():
    return llm_core._get_http_client()


def test_close_owned_http_clients_api_exists():
    """GREEN: llm_core exposes an awaited close for factory-owned clients."""
    assert hasattr(llm_core, "close_owned_http_clients")
    assert asyncio.iscoroutinefunction(llm_core.close_owned_http_clients)


def test_owned_loop_run_is_implemented():
    """GREEN: owned_loop.run must actually run a coroutine (stub is RED)."""
    from src import owned_loop

    async def _one():
        return 7

    coro = _one()
    try:
        assert owned_loop.run(coro) == 7
    except NotImplementedError:
        coro.close()
        raise


def test_replaced_client_is_closed_when_owner_loop_dies(monkeypatch):
    """GREEN: getter replacement must not leave the prior factory client open.

    Research tcp-native21 case 'replacement': after owner loop closes, a new
    getter returns a distinct client while the old one stays is_closed=False
    while still referenced. Fix must aclose (or equivalent) on the owner path
    before discard/replace.
    """
    _reset_pool(monkeypatch)
    box = {}

    def _owner():
        box["first"] = asyncio.run(_get())

    t = threading.Thread(target=_owner)
    t.start()
    t.join(5)
    first = box["first"]
    second = asyncio.run(_get())
    assert second is not first
    assert first.is_closed, (
        "prior factory client must be closed when its owner loop is gone; "
        "current getter replaces without aclose"
    )


def test_shutdown_event_closes_llm_http_pool():
    """GREEN: ASGI shutdown must aclose the llm_core pool after consumers."""
    src = (ROOT / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(
        n
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "_shutdown_event"
    )
    text = ast.get_source_segment(src, fn) or ""
    closes_pool = (
        "close_owned_http_clients" in text
        or ("llm_core" in text and "aclose" in text)
    )
    assert closes_pool, (
        "_shutdown_event must close the llm_core HTTP pool after accredited consumers"
    )


def test_model_calls_run_coroutine_uses_owned_loop():
    """GREEN: short-lived sync wrappers must not call bare asyncio.run."""
    src = (ROOT / "src" / "workflows" / "model_calls.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(
        n
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "run_coroutine"
    )
    text = ast.get_source_segment(src, fn) or ""
    assert "owned_loop" in text, "run_coroutine must route through owned_loop"
    assert "asyncio.run(" not in text, "bare asyncio.run leaves HTTP clients unclosed"


def test_post_turn_create_task_is_adopted():
    """GREEN: post-turn work must be adopted so shutdown can drain it before pool close."""
    src = (ROOT / "routes" / "chat_helpers.py").read_text(encoding="utf-8")
    # Spawn site: create_task then adopt on the supervisor (name pin).
    create_idx = src.index("_post_turn_task = asyncio.get_running_loop().create_task(")
    adopt_idx = src.index('name="post-turn-automation"')
    assert adopt_idx > create_idx
    assert adopt_idx - create_idx < 500
    assert ".adopt(" in src[create_idx:adopt_idx + 40]


def test_owned_loop_close_helper_callable_from_running_loop(monkeypatch):
    """GREEN: close_loop_http_clients awaited on the owner loop succeeds."""
    _reset_pool(monkeypatch)
    from src import owned_loop

    async def _body():
        client = llm_core._get_http_client()
        await owned_loop.close_loop_http_clients()
        assert client.is_closed
        return True

    assert asyncio.run(_body()) is True
