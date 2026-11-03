"""A memory text over MAX_TEXT_CHARS is stored cut, and the item says so.

FAUSTUS §120 Part B left open whether add_item should refuse or cut. It
keeps cutting (refusing loses the whole fact) but records the original
length in provenance instead of doing it silently.
"""
from datetime import datetime, timezone

import pytest

from src import memory_engine

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_engine, "DATA_DIR", str(tmp_path))
    memory_engine.set_vector_store(None)
    memory_engine.clear_injected()
    yield tmp_path
    memory_engine.reset_vector_store()
    memory_engine.clear_injected()


def test_long_text_is_cut_and_the_original_length_is_recorded(store):
    long_text = "word " * 700  # 3499 chars once whitespace is normalised
    item = memory_engine.add_item(long_text, owner="u", now=NOW)
    assert len(item["text"]) == memory_engine.MAX_TEXT_CHARS
    assert item["provenance"]["truncated_from_chars"] == len(" ".join(long_text.split()))
    stored = memory_engine.get_item(item["id"])
    assert stored["provenance"]["truncated_from_chars"] == len(" ".join(long_text.split()))


def test_short_text_carries_no_truncation_note(store):
    item = memory_engine.add_item("Prefers short answers.", owner="u", now=NOW)
    assert "truncated_from_chars" not in item["provenance"]
