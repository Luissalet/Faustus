"""A saved memory's use counter goes up only when the preface that carries
it is really sent: with the context engine on, a packet can replace the
preface, so the route counts the uses afterwards."""
from types import SimpleNamespace

from routes.chat_helpers import count_sent_memory_uses
from src.chat_processor import ChatProcessor


class _Memory:
    def __init__(self, rows):
        self.rows = rows
        self.incremented = []

    def load(self, owner=None):
        return list(self.rows)

    def increment_uses(self, ids):
        self.incremented.extend(ids)


class _Docs:
    rag_manager = None


ROWS = [{"id": "name", "text": "User's name is Felix.", "category": "identity", "pinned": True,
         "timestamp": 1}]


def _build(count):
    memory = _Memory(ROWS)
    proc = ChatProcessor(memory_manager=memory, personal_docs_manager=_Docs())
    proc.build_context_preface(message="What's my name?", session=SimpleNamespace(), use_rag=False,
                               use_memory=True, count_memory_uses=count)
    return proc, memory


def test_counts_at_once_by_default():
    proc, memory = _build(True)
    assert memory.incremented == ["name"]
    assert proc._last_used_memory_ids == ["name"]


def test_deferred_counting_leaves_the_counter_alone():
    proc, memory = _build(False)
    assert memory.incremented == []
    assert proc._last_used_memory_ids == ["name"]


def test_route_counts_once_and_only_when_sent():
    memory = _Memory(ROWS)
    ctx = SimpleNamespace(used_memory_ids=["name"])
    count_sent_memory_uses(ctx, memory, sent=False)
    assert memory.incremented == [] and ctx.used_memory_ids == []

    ctx = SimpleNamespace(used_memory_ids=["name"])
    count_sent_memory_uses(ctx, memory, sent=True)
    count_sent_memory_uses(ctx, memory, sent=True)
    assert memory.incremented == ["name"]
