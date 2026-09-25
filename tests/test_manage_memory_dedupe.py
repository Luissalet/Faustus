"""manage_memory add: the same fact saved twice is one memory.

Seen live: a turn resumed after an approval card replayed both of its
`add` calls, and each preference was stored twice."""
import asyncio

import src.ai_interaction as ai
from src.memory import MemoryManager


def test_adding_the_same_fact_again_returns_the_saved_one(monkeypatch, tmp_path):
    manager = MemoryManager(str(tmp_path))
    manager.save([])
    monkeypatch.setattr(ai, "_memory_manager", manager)
    monkeypatch.setattr(ai, "_memory_vector", None)

    first = asyncio.run(ai.do_manage_memory("add\nEl cumpleaños es el 14 de marzo.\nfact", owner="luis"))
    again = asyncio.run(ai.do_manage_memory("add\nel cumpleaños es el 14 de marzo\nfact", owner="luis"))
    other_owner = asyncio.run(ai.do_manage_memory("add\nEl cumpleaños es el 14 de marzo.\nfact", owner="ana"))

    assert first["results"].startswith("Memory added")
    assert again.get("duplicate") is True and again["memory_id"] == first["memory_id"]
    assert other_owner["results"].startswith("Memory added")
    texts = [(m.get("owner"), m["text"]) for m in manager.load_all()]
    assert texts.count(("luis", "El cumpleaños es el 14 de marzo.")) == 1
    assert len(texts) == 2
