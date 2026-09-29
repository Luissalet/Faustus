"""Regression test: extract_memory_from_chat must not crash on bullet lines.

The fallback only reads an explicit user-authored memory list. Its bullet
parser must still handle dashes, stars and numbered items without crashing.

There are two copies of ``MemoryManager``: ``src.memory`` and the
``services.memory`` package that ``routes/memory_routes.py`` actually imports.
The fix first landed only in ``src.memory`` while the live route path kept the
broken copy, and this test imported ``src.memory`` so it stayed green. It now
exercises both copies so the two cannot drift back apart.
"""
import pytest

from src.memory import MemoryManager as SrcMemoryManager
from services.memory.memory import MemoryManager as ServiceMemoryManager


@pytest.mark.parametrize("manager_cls", [SrcMemoryManager, ServiceMemoryManager])
def test_extract_memory_from_chat_handles_bullets(manager_cls, tmp_path):
    mgr = manager_cls(str(tmp_path))
    chat = [
        {"role": "assistant", "content": "- User likes cola"},
        {"role": "user", "content": "Remember these:\n- User likes coffee\n* Prefers tea in winter\n1. Wakes at 6am"},
    ]

    out = mgr.extract_memory_from_chat(chat)
    texts = [m["text"] for m in out]

    assert "User likes coffee" in texts       # '-' bullet (used to crash)
    assert "Prefers tea in winter" in texts   # '*' bullet (used to crash)
    assert "Wakes at 6am" in texts            # numbered list (already worked)
    assert "User likes cola" not in texts


@pytest.mark.parametrize("manager_cls", [SrcMemoryManager, ServiceMemoryManager])
def test_fallback_does_not_treat_a_user_task_list_as_memory(manager_cls, tmp_path):
    mgr = manager_cls(str(tmp_path))
    chat = [{"role": "user", "content": "Please do these tasks:\n- Buy milk\n- Send report"}]
    assert mgr.extract_memory_from_chat(chat) == []
