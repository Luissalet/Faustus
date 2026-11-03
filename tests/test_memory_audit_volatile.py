"""The memory tidy-up drops stored workspace snapshots like extraction does.

"En la raíz del workspace hay 2 ficheros" was saved before
services/memory/volatile_facts.py existed and kept loading into prompts;
the audit removes it even when the model returns it untouched.
"""
import asyncio
import json

import services.memory.memory_extractor as mx

STORE = [
    {"id": "a", "text": "Prefiere respuestas en castellano.", "category": "preference"},
    {"id": "b", "text": "En la raíz del workspace hay 2 ficheros: `datos.json` y `NOTAS.md`.", "category": "fact"},
]


class _Manager:
    def __init__(self):
        self.saved = None

    def load(self, owner=None):
        return [dict(e) for e in STORE]

    def save(self, entries):
        self.saved = entries


def test_audit_drops_a_volatile_entry_the_model_kept(monkeypatch):
    async def echo(url, model, messages, **kw):
        return json.dumps(json.loads(messages[-1]["content"]), ensure_ascii=False)

    import src.llm_core as llm_core
    monkeypatch.setattr(llm_core, "llm_call_async", echo)
    monkeypatch.setattr(mx, "_load_tidy_state", lambda manager: {})
    monkeypatch.setattr(mx, "_save_tidy_state", lambda *a, **k: None)
    manager = _Manager()
    result = asyncio.run(mx.audit_memories(manager, None, "http://model.invalid/v1", "m"))
    assert result == {"before": 2, "after": 1}
    assert [e["id"] for e in manager.saved] == ["a"]
