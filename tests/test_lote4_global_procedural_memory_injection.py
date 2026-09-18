"""FAUSTUS §115 item 5 — a procedural memory item with no owner (global
scope) must reach `pack_detail`/`pack` for ANY user, not just the one who
wrote it, since `scoped_items` already treats `owner == ''` as unscoped
("a rule with no project is a rule everywhere" — src/memory_engine.py).

This locks in the behavior the agent-mode "Tareas grandes" strategy
(src/agent_loop.py:_big_task_strategy_block) leans on: a standing rule like
"para trabajos largos y repetitivos usar el bucle con cursor" should not
need to be re-taught per user — one global item, written once, is enough.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import memory_engine as engine  # noqa: E402


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "DATA_DIR", str(tmp_path))
    engine.set_vector_store(None)
    engine.clear_injected()
    yield tmp_path
    engine.reset_vector_store()
    engine.clear_injected()


GLOBAL_RULE_TEXT = (
    "Para trabajos largos y repetitivos usar el bucle con cursor: una unidad "
    "completa a la vez, guardar el progreso en .faustus/ y retomar desde ahi."
)


def test_scoped_items_returns_global_procedural_rule_for_any_owner(store):
    engine.add_item(
        GLOBAL_RULE_TEXT,
        owner="",  # global — no single user owns this rule
        level="procedural",
        status="active",
        trust_class="human_explicit",
        confidence=0.9,
    )
    for owner in ("luis", "some-other-user", "anyone"):
        items = engine.scoped_items(owner=owner, project=None)
        texts = [i["text"] for i in items]
        assert GLOBAL_RULE_TEXT in texts, (
            f"global procedural rule missing from scoped_items for owner={owner!r}"
        )


def test_pack_detail_injects_global_rule_into_agent_mode_prompt_block(store):
    engine.add_item(
        GLOBAL_RULE_TEXT,
        owner="",
        level="procedural",
        status="active",
        trust_class="human_explicit",
        confidence=0.9,
    )
    detail = engine.pack_detail(owner="luis", project="", query="", char_budget=2000)
    assert "cursor" in detail["text"]
    assert GLOBAL_RULE_TEXT.split(":")[0] in detail["text"] or GLOBAL_RULE_TEXT in detail["text"]
    assert detail["ids"], "global rule should have been included and its id reported"


def test_pack_wraps_pack_detail_for_a_second_unrelated_user(store):
    engine.add_item(
        GLOBAL_RULE_TEXT,
        owner="",
        level="procedural",
        status="active",
        trust_class="human_explicit",
        confidence=0.9,
    )
    text = engine.pack(owner="brand-new-user-never-seen-before", project="", query="", char_budget=2000)
    assert "cursor" in text
