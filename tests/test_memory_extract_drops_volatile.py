# -*- coding: utf-8 -*-
"""The guard has to be WIRED, not just correct.

`tests/test_volatile_facts.py` proves the rule. This one runs the real
`extract_and_store` against a stubbed model that returns one snapshot of the
workspace and one durable fact, and asserts the store ends up with exactly
the durable one.
"""

import json

import pytest

from src.memory import MemoryManager
import services.memory.memory_extractor as extractor


class _Session:
    session_id = "s-1"

    def get_context_messages(self):
        return [
            {"role": "user", "content": "cuantos ficheros hay en la raiz del workspace?"},
            {"role": "assistant", "content": "Hay 2: datos.json y NOTAS.md."},
            {"role": "user", "content": "vale. por cierto, trabajo siempre en milimetros"},
            {"role": "assistant", "content": "Anotado."},
        ]


MODEL_ANSWER = json.dumps([
    {"text": "Hay 2 ficheros en la raiz del workspace.", "category": "fact"},
    {"text": "User prefers working in millimeters.", "category": "preference"},
])


@pytest.mark.asyncio
async def test_the_snapshot_never_reaches_the_store(tmp_path, monkeypatch):
    async def _fake_llm_call_async(*args, **kwargs):
        return MODEL_ANSWER

    monkeypatch.setattr("src.llm_core.llm_call_async", _fake_llm_call_async)

    manager = MemoryManager(str(tmp_path))
    await extractor.extract_and_store(
        _Session(), manager, None,
        endpoint_url="http://127.0.0.1:1/v1", model="stub",
    )

    stored = [e.get("text", "") for e in manager.load_all()]
    assert any("millimeters" in t for t in stored), stored
    assert not any("ficheros" in t for t in stored), stored


@pytest.mark.asyncio
async def test_control_without_the_guard_the_snapshot_is_stored(tmp_path, monkeypatch):
    """The control for the test above.

    A test that passes whether or not the guard exists proves nothing. With
    `volatile_reason` neutered, the same stubbed answer must reach the store
    -- so the assertion above is really about the guard and not about some
    other filter further down.
    """
    async def _fake_llm_call_async(*args, **kwargs):
        return MODEL_ANSWER

    monkeypatch.setattr("src.llm_core.llm_call_async", _fake_llm_call_async)
    monkeypatch.setattr(extractor, "volatile_reason", lambda _text: None)

    manager = MemoryManager(str(tmp_path))
    await extractor.extract_and_store(
        _Session(), manager, None,
        endpoint_url="http://127.0.0.1:1/v1", model="stub",
    )

    stored = [e.get("text", "") for e in manager.load_all()]
    assert any("ficheros" in t for t in stored), stored
