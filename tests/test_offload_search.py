"""Unit coverage for src/offload_search.py (BM25 full-text search over
offloaded tool results) plus its hook into tool_result_offload and its
agent-tool wiring (artifact_search).
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database as db_mod
from src import artifact_store, offload_search, tool_result_offload as offload


@pytest.fixture()
def own_database(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "offload.db").as_posix()
    engine = create_engine(url, connect_args={"check_same_thread": False})
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal",
                        sessionmaker(autocommit=False, autoflush=False, bind=engine))
    db_mod.Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(artifact_store, "ARTIFACT_STORE_DIR", str(tmp_path / "store"))
    offload_search.use_path(str(tmp_path / "offload_search.db"))
    offload_search.use_fts5(None)
    yield engine
    engine.dispose()
    offload_search.use_path(None)
    offload_search.use_fts5(None)


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

def test_chunk_offsets_round_trip_exactly():
    text = "\n".join(f"line {i} some words here to pad things out" for i in range(400))
    spans = offload_search._chunk_offsets(text)
    assert spans
    for start, end in spans:
        assert 0 <= start < end <= len(text)
        # The stored chunk text must be exactly text[start:end] -- this is
        # what makes chunk_start a valid `read_artifact` start offset.
        assert text[start:end] == text[start:end]
    # Consecutive spans overlap (except possibly the very last) and cover
    # the whole text without gaps.
    assert spans[0][0] == 0
    assert spans[-1][1] == len(text)


def test_chunk_offsets_empty_text():
    assert offload_search._chunk_offsets("") == []


# ---------------------------------------------------------------------------
# index_result / search basics
# ---------------------------------------------------------------------------

def test_index_and_search_ranks_rare_term_first(own_database):
    common = "the quick brown fox jumps over the lazy dog. " * 60
    rare_chunk = common + "\nzylophantastic-marker-9284 appears exactly here.\n" + common
    full_text = common + "\n" + rare_chunk + "\n" + common

    n = offload_search.index_result("alice", "s1", "art-1", "bash", full_text)
    assert n > 0

    hits = offload_search.search("alice", "zylophantastic-marker-9284")
    assert hits
    assert "zylophantastic-marker-9284" in hits[0]["snippet"]
    assert hits[0]["artifact_id"] == "art-1"
    # Offsets must map back to the exact original text.
    start, end = hits[0]["start"], hits[0]["end"]
    assert full_text[start:end] in full_text
    assert "zylophantastic-marker-9284" in full_text[start:end]


def test_index_result_is_idempotent_per_artifact_id(own_database):
    text = "alpha beta gamma delta " * 200
    first = offload_search.index_result("alice", "s1", "art-2", "bash", text)
    second = offload_search.index_result("alice", "s1", "art-2", "bash", text)
    assert first > 0
    assert second == 0


def test_search_requires_owner_and_query(own_database):
    offload_search.index_result("alice", "s1", "art-3", "bash", "hello world " * 300)
    assert offload_search.search("", "hello") == []
    assert offload_search.search("alice", "") == []


# ---------------------------------------------------------------------------
# Owner / session / artifact scoping
# ---------------------------------------------------------------------------

def test_owner_isolation(own_database):
    offload_search.index_result("alice", "s1", "art-4", "bash", "secretwordxyz " * 300)
    offload_search.index_result("bob", "s1", "art-5", "bash", "secretwordxyz " * 300)

    alice_hits = offload_search.search("alice", "secretwordxyz")
    bob_hits = offload_search.search("bob", "secretwordxyz")

    assert alice_hits and all(h["artifact_id"] == "art-4" for h in alice_hits)
    assert bob_hits and all(h["artifact_id"] == "art-5" for h in bob_hits)


def test_session_filter(own_database):
    offload_search.index_result("alice", "s1", "art-6", "bash", "foobarterm " * 300)
    offload_search.index_result("alice", "s2", "art-7", "bash", "foobarterm " * 300)

    only_s1 = offload_search.search("alice", "foobarterm", session_id="s1")
    assert only_s1 and all(h["artifact_id"] == "art-6" for h in only_s1)


def test_artifact_filter(own_database):
    offload_search.index_result("alice", "s1", "art-8", "bash", "distinctwordhere " * 300)
    offload_search.index_result("alice", "s1", "art-9", "bash", "distinctwordhere " * 300)

    only_9 = offload_search.search("alice", "distinctwordhere", artifact_id="art-9")
    assert only_9 and all(h["artifact_id"] == "art-9" for h in only_9)


# ---------------------------------------------------------------------------
# Malicious / odd queries never raise
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("query", [
    '"unterminated quote',
    "term1 AND term2 OR term3",
    "(parens) NEAR/2 stuff",
    "",
    "   ",
    "*****",
    "col:value",
    "a\"b\"c\"d",
])
def test_odd_queries_never_raise(own_database, query):
    offload_search.index_result("alice", "s1", "art-10", "bash", "some normal text " * 300)
    # Must not raise, regardless of what it returns.
    result = offload_search.search("alice", query)
    assert isinstance(result, list)


# ---------------------------------------------------------------------------
# FTS5-unavailable fallback (LIKE scan)
# ---------------------------------------------------------------------------

def test_like_fallback_when_fts5_unavailable(own_database, monkeypatch):
    offload_search.use_fts5(False)
    n = offload_search.index_result("alice", "s1", "art-11", "bash", "findmethisway " * 300)
    assert n > 0
    hits = offload_search.search("alice", "findmethisway")
    assert hits
    assert hits[0]["artifact_id"] == "art-11"


def test_like_fallback_owner_isolation(own_database):
    offload_search.use_fts5(False)
    offload_search.index_result("alice", "s1", "art-12", "bash", "onlyaliceword " * 300)
    offload_search.index_result("bob", "s1", "art-13", "bash", "onlyaliceword " * 300)
    hits = offload_search.search("alice", "onlyaliceword")
    assert hits and all(h["artifact_id"] == "art-12" for h in hits)


# ---------------------------------------------------------------------------
# offload_if_oversized indexes automatically
# ---------------------------------------------------------------------------

def test_offload_if_oversized_indexes_the_artifact(own_database):
    big = {"output": "needlephrase-unique-9911 " + ("pad word " * 5000)}
    out = offload.offload_if_oversized(
        big, owner="alice", session_id="s1", run_id="r1", call_id="c1",
        tool="bash", threshold_chars=1000)
    assert out["artifact_id"]
    assert "artifact_search" in out["offload_note"]

    hits = offload_search.search("alice", "needlephrase-unique-9911", session_id="s1")
    assert hits
    assert hits[0]["artifact_id"] == out["artifact_id"]


def test_offload_indexing_failure_does_not_break_offload(own_database, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("indexing exploded")
    monkeypatch.setattr(offload_search, "index_result", boom)

    big = {"output": "x" * 30000}
    out = offload.offload_if_oversized(
        big, owner="alice", session_id="s1", run_id="r1", call_id="c1",
        tool="bash", threshold_chars=1000)
    assert out["artifact_id"]  # offload itself still succeeded


def test_offload_search_disabled_setting_skips_indexing(own_database, monkeypatch):
    from src import settings as settings_mod
    monkeypatch.setattr(settings_mod, "get_setting",
                        lambda key, default=None: False if key == "offload_search_enabled" else default)
    monkeypatch.setattr(offload, "get_setting", settings_mod.get_setting)

    big = {"output": "verydistinctivephrase55 " + ("pad " * 5000)}
    out = offload.offload_if_oversized(
        big, owner="alice", session_id="s1", run_id="r1", call_id="c1",
        tool="bash", threshold_chars=1000)
    assert out["artifact_id"]
    assert offload_search.search("alice", "verydistinctivephrase55", session_id="s1") == []


# ---------------------------------------------------------------------------
# Tool handler
# ---------------------------------------------------------------------------

def test_artifact_search_tool_returns_hits(own_database, monkeypatch):
    from src.agent_tools.artifact_read_tool import ArtifactSearchTool

    monkeypatch.setattr(
        "src.owner_identity.effective_storage_owner",
        lambda owner, auth_is_disabled=None: "alice",
    )
    offload_search.index_result("alice", "s1", "art-tool", "bash", "handlertestword " * 300)

    tool = ArtifactSearchTool()
    ctx = {"owner": "alice", "session_id": "s1"}
    result = asyncio.run(tool.execute(json.dumps({"query": "handlertestword"}), ctx))
    assert result["count"] >= 1
    assert result["hits"][0]["artifact_id"] == "art-tool"


def test_artifact_search_tool_requires_query(own_database, monkeypatch):
    from src.agent_tools.artifact_read_tool import ArtifactSearchTool
    monkeypatch.setattr(
        "src.owner_identity.effective_storage_owner",
        lambda owner, auth_is_disabled=None: "alice",
    )
    tool = ArtifactSearchTool()
    result = asyncio.run(tool.execute(json.dumps({}), {"owner": "alice"}))
    assert result.get("exit_code") == 1


def test_offload_persists_exact_bytes_even_where_text_mode_would_translate_newlines(monkeypatch):
    """Windows text mode writes "\\r\\n" for "\\n"; the offload must write bytes
    so the stored sha equals the digest of the original text."""
    import src.tool_result_offload as tro

    real_fdopen = os.fdopen

    def windows_like_fdopen(fd, mode="r", *args, **kwargs):
        if "b" not in mode:
            kwargs.setdefault("newline", "\r\n")
        return real_fdopen(fd, mode, *args, **kwargs)

    monkeypatch.setattr(tro.os, "fdopen", windows_like_fdopen)
    text = "\n".join(f"row {i}" for i in range(50))
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    artifact_id = tro._persist_full_result(text, digest, owner="o", session_id="s",
                                           run_id="r", call_id="c", tool="bash")
    assert artifact_id.startswith("occ_")
