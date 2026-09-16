"""Tests for disk-backed context overflow (mid-turn spill)."""

from __future__ import annotations

import json
import os
import time

import pytest

from src import context_overflow as co


@pytest.fixture()
def overflow_root(tmp_path, monkeypatch):
    root = tmp_path / "context_overflow"
    monkeypatch.setattr(co, "OVERFLOW_DIR", str(root))
    return root


def test_persist_writes_content_addressed_file(overflow_root):
    record = co.persist(
        session_id="sess-1",
        content="hello overflow " * 100,
        tool="bash",
        call_id="call_abc",
        role="tool",
        run_id="run-1",
        round_num=3,
    )
    assert record["content_sha256"]
    path = overflow_root / "sess-1" / f"{record['content_sha256']}.json"
    assert path.is_file()
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["tool"] == "bash"
    assert loaded["call_id"] == "call_abc"
    assert "hello overflow" in loaded["content"]


def test_identical_content_shares_one_blob(overflow_root):
    body = "same bytes " * 200
    a = co.persist(session_id="s", content=body, tool="write_file", call_id="1")
    b = co.persist(session_id="s", content=body, tool="write_file", call_id="2")
    assert a["content_sha256"] == b["content_sha256"]
    assert len(list((overflow_root / "s").glob("*.json"))) == 1


def test_load_restores_content(overflow_root):
    body = "recover me " * 50
    rec = co.persist(session_id="s", content=body, tool="bash", call_id="c1")
    assert co.load(session_id="s", content_sha256=rec["content_sha256"]) == body


def test_persist_refused_when_incognito(overflow_root):
    with pytest.raises(co.OverflowDisabled):
        co.persist(
            session_id="s",
            content="x" * 100,
            tool="bash",
            call_id="c",
            durable=False,
        )
    assert not (overflow_root / "s").exists()


def test_prune_removes_old_unreferenced(overflow_root, monkeypatch):
    monkeypatch.setattr(co, "keep_hours", lambda: 1)
    rec = co.persist(session_id="s", content="old " * 40, tool="bash", call_id="c")
    path = overflow_root / "s" / f"{rec['content_sha256']}.json"
    old = time.time() - 7200
    os.utime(path, (old, old))
    removed = co.prune(session_id="s", referenced_ids=set())
    assert removed == 1
    assert not path.exists()


def test_prune_keeps_referenced(overflow_root, monkeypatch):
    monkeypatch.setattr(co, "keep_hours", lambda: 1)
    rec = co.persist(session_id="s", content="keep " * 40, tool="bash", call_id="c")
    path = overflow_root / "s" / f"{rec['content_sha256']}.json"
    old = time.time() - 7200
    os.utime(path, (old, old))
    removed = co.prune(session_id="s", referenced_ids={rec["content_sha256"]})
    assert removed == 0
    assert path.exists()
