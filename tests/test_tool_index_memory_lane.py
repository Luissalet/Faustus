"""Tool index without ChromaDB: the in-memory fastembed lane.

When the ChromaDB container is down (Docker Desktop closed is a normal state
on a desktop PC) the tool index used to raise "ChromaDB is not reachable",
retry every 30 s and leave every agent turn on keyword-only tool selection.
These tests pin the replacement: an in-process cosine lane that needs only
the embedder, a persisted embedding cache so a restart re-embeds nothing that
did not change, and a singleton getter that never blocks a request.

No network: the embedder is a deterministic bag-of-words fake.
"""

import json
import logging
import os
import re
import threading
import time
import zlib

import numpy as np
import pytest

from tests.helpers.embedding_lanes import FakeChroma, FakeEmbedder, patch_chroma


# ── deterministic embedder ──────────────────────────────────────────────────

class HashingEmbedder:
    """Bag-of-words hashed into a fixed number of buckets, L2-normalised.

    Deterministic and dependency-free, and lexical overlap is enough to make
    tool retrieval meaningful for smoke tests.
    """

    def __init__(self, dim=512, model="hash-bow"):
        self.dim = dim
        self.model = model
        self.url = "local://test"
        self.calls = 0
        self.texts_seen = []

    def get_sentence_embedding_dimension(self):
        return self.dim

    def encode(self, texts, normalize_embeddings=True):
        self.calls += 1
        self.texts_seen.extend(texts)
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for row, text in enumerate(texts):
            for tok in re.findall(r"[a-z0-9_]+", text.lower()):
                out[row, zlib.crc32(tok.encode("utf-8")) % self.dim] += 1.0
        norms = np.linalg.norm(out, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return out / norms


def _unit(*values):
    vec = np.array(values, dtype=np.float32)
    return (vec / np.linalg.norm(vec)).tolist()


# ── MemoryCollection contract ──────────────────────────────────────────────

def _seeded_collection():
    from src.tool_index_memory import MemoryCollection

    col = MemoryCollection("odysseus_tool_index_fastembed")
    col.upsert(
        ids=["builtin_bash", "builtin_python", "mcp_fetch"],
        documents=["Tool: bash\nRun shell commands", "Tool: python\nRun Python", "Tool: fetch\nFetch a URL"],
        embeddings=[_unit(1, 0, 0), _unit(0.9, 0.1, 0), _unit(0, 1, 0)],
        metadatas=[
            {"tool_name": "bash", "tool_type": "builtin"},
            {"tool_name": "python", "tool_type": "builtin"},
            {"tool_name": "fetch", "tool_type": "mcp"},
        ],
    )
    return col


def test_memory_collection_upsert_get_delete_shapes():
    col = _seeded_collection()
    assert col.count() == 3

    got = col.get(where={"tool_type": "builtin"})
    assert got["ids"] == ["builtin_bash", "builtin_python"]
    assert got["documents"] == ["Tool: bash\nRun shell commands", "Tool: python\nRun Python"]
    assert [m["tool_name"] for m in got["metadatas"]] == ["bash", "python"]

    by_id = col.get(ids=["mcp_fetch", "missing"])
    assert by_id["ids"] == ["mcp_fetch"]

    # upsert replaces an existing row in place
    col.upsert(
        ids=["builtin_bash"],
        documents=["Tool: bash\nchanged"],
        embeddings=[_unit(0, 0, 1)],
        metadatas=[{"tool_name": "bash", "tool_type": "builtin"}],
    )
    assert col.count() == 3
    assert col.get(ids=["builtin_bash"])["documents"] == ["Tool: bash\nchanged"]

    col.delete(ids=["builtin_python", "missing"])
    assert col.count() == 2
    assert col.get()["ids"] == ["builtin_bash", "mcp_fetch"]

    col.delete(where={"tool_type": "mcp"})
    assert col.get()["ids"] == ["builtin_bash"]


def test_memory_collection_query_is_chroma_shaped_and_ranked_by_cosine():
    col = _seeded_collection()
    res = col.query(query_embeddings=[_unit(1, 0, 0)], n_results=3, include=["metadatas", "distances"])

    # one inner list per query embedding, like chromadb
    assert res["ids"] == [["builtin_bash", "builtin_python", "mcp_fetch"]]
    assert [m["tool_name"] for m in res["metadatas"][0]] == ["bash", "python", "fetch"]
    distances = res["distances"][0]
    assert distances == sorted(distances)
    assert distances[0] == pytest.approx(0.0, abs=1e-6)          # identical direction
    assert distances[-1] == pytest.approx(1.0, abs=1e-6)         # orthogonal
    assert 0.0 < distances[1] < 0.2

    # n_results is honoured and clamped to the collection size
    assert res_ids(col.query(query_embeddings=[_unit(1, 0, 0)], n_results=2)) == ["builtin_bash", "builtin_python"]
    assert len(res_ids(col.query(query_embeddings=[_unit(1, 0, 0)], n_results=50))) == 3

    # un-normalised query vectors are normalised before scoring
    scaled = col.query(query_embeddings=[[5.0, 0.0, 0.0]], n_results=1)
    assert scaled["distances"][0][0] == pytest.approx(0.0, abs=1e-6)

    # several query embeddings → several result lists
    multi = col.query(query_embeddings=[_unit(1, 0, 0), _unit(0, 1, 0)], n_results=1)
    assert multi["ids"] == [["builtin_bash"], ["mcp_fetch"]]


def res_ids(result):
    return result["ids"][0]


def test_memory_collection_query_where_filters_by_metadata():
    col = _seeded_collection()
    only_mcp = col.query(query_embeddings=[_unit(1, 0, 0)], n_results=3, where={"tool_type": "mcp"})
    assert only_mcp["ids"] == [["mcp_fetch"]]
    assert only_mcp["metadatas"][0][0]["tool_type"] == "mcp"

    nothing = col.query(query_embeddings=[_unit(1, 0, 0)], n_results=3, where={"tool_type": "nope"})
    assert nothing["ids"] == [[]]
    assert nothing["metadatas"] == [[]]
    assert nothing["distances"] == [[]]

    operators = col.query(
        query_embeddings=[_unit(1, 0, 0)],
        n_results=3,
        where={"$and": [{"tool_type": {"$eq": "builtin"}}, {"tool_name": {"$in": ["python"]}}]},
    )
    assert operators["ids"] == [["builtin_python"]]


def test_memory_collection_rejects_dimension_mismatch():
    col = _seeded_collection()
    with pytest.raises(ValueError, match="dimension"):
        col.upsert(ids=["x"], documents=["x"], embeddings=[[1.0, 0.0]], metadatas=[{}])
    with pytest.raises(ValueError, match="dimension"):
        col.query(query_embeddings=[[1.0, 0.0]], n_results=1)


# ── persisted embedding cache ─────────────────────────────────────────────

def test_embedding_cache_miss_then_hit_across_restart(tmp_path):
    from src.tool_index_memory import CachingEmbedder, EmbeddingCache, MemoryCollection, embedding_cache_key

    path = os.path.join(str(tmp_path), "tool_index_cache.json")
    docs = ["Tool: bash\nRun shell commands", "Tool: python\nRun Python"]

    # first process: everything is a miss and the collection persists it
    first = HashingEmbedder()
    cache = EmbeddingCache(path, model=first.model)
    embedder = CachingEmbedder(first, cache)
    vectors = embedder.encode(docs)
    assert first.calls == 1 and first.texts_seen == docs
    MemoryCollection("t", cache=cache).upsert(
        ids=["a", "b"], documents=docs, embeddings=vectors.tolist(), metadatas=[{}, {}]
    )
    assert os.path.exists(path)
    with open(path, encoding="utf-8") as fh:
        payload = json.load(fh)
    assert payload["model"] == first.model
    assert payload["dimension"] == first.dim
    assert set(payload["entries"]) == {embedding_cache_key(first.model, d) for d in docs}

    # second process: same texts → no model call; a changed text → one call for that text only
    second = HashingEmbedder()
    warm = CachingEmbedder(second, EmbeddingCache(path, model=second.model))
    again = warm.encode(docs)
    assert second.calls == 0
    np.testing.assert_allclose(again, vectors, atol=1e-6)

    warm.encode([docs[0], "Tool: python\nRun Python (changed)"])
    assert second.calls == 1
    assert second.texts_seen == ["Tool: python\nRun Python (changed)"]

    # a different model invalidates the whole file
    other = HashingEmbedder(model="other-model")
    CachingEmbedder(other, EmbeddingCache(path, model=other.model)).encode(docs)
    assert other.calls == 1 and other.texts_seen == docs


def test_embedding_cache_tolerates_corrupt_file(tmp_path):
    from src.tool_index_memory import CachingEmbedder, EmbeddingCache

    path = os.path.join(str(tmp_path), "tool_index_cache.json")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{not json")
    client = HashingEmbedder()
    embedder = CachingEmbedder(client, EmbeddingCache(path, model=client.model))
    assert embedder.encode(["x"]).shape == (1, client.dim)
    assert client.calls == 1


def test_persisted_cache_mirrors_collection_contents(tmp_path):
    """Deleted rows leave the file; queries never enter it."""
    from src.tool_index_memory import CachingEmbedder, EmbeddingCache, MemoryCollection, embedding_cache_key

    path = os.path.join(str(tmp_path), "cache.json")
    client = HashingEmbedder()
    cache = EmbeddingCache(path, model=client.model)
    embedder = CachingEmbedder(client, cache)
    col = MemoryCollection("t", cache=cache)
    docs = ["Tool: one", "Tool: two"]
    col.upsert(ids=["1", "2"], documents=docs, embeddings=embedder.encode(docs).tolist(), metadatas=[{}, {}])
    embedder.encode(["what is the weather"])  # a query
    col.delete(ids=["2"])
    col.upsert(ids=["3"], documents=["Tool: three"], embeddings=embedder.encode(["Tool: three"]).tolist(), metadatas=[{}])

    with open(path, encoding="utf-8") as fh:
        entries = json.load(fh)["entries"]
    assert set(entries) == {embedding_cache_key(client.model, d) for d in ("Tool: one", "Tool: three")}


# ── ToolIndex fallback ─────────────────────────────────────────────────────

def _chroma_down(monkeypatch):
    import src.chroma_client as chroma_client

    def _raise():
        raise RuntimeError("ChromaDB is not reachable at localhost:8100. Start the ChromaDB service")

    monkeypatch.setattr(chroma_client, "get_chroma_client", _raise)


def _use_embedder(monkeypatch, tmp_path, embedder=None):
    import src.embedding_lanes as lanes
    import src.tool_index_memory as tim

    embedder = embedder or HashingEmbedder()
    monkeypatch.setattr(lanes, "_build_fastembed_client", lambda: embedder)
    monkeypatch.setattr(tim, "DEFAULT_CACHE_PATH", os.path.join(str(tmp_path), "tool_index_cache.json"))
    return embedder


def test_tool_index_uses_memory_lane_when_chroma_is_unreachable(monkeypatch, tmp_path, caplog):
    _chroma_down(monkeypatch)
    _use_embedder(monkeypatch, tmp_path)
    import src.tool_index as ti

    monkeypatch.setattr(ti, "_memory_lane_announced", False)
    with caplog.at_level(logging.DEBUG, logger="src.tool_index"):
        index = ti.ToolIndex()
        index.index_builtin_tools()
        again = ti.ToolIndex()

    assert index.healthy and again.healthy
    assert index.backend == "memory"
    assert [lane.name for lane in index._lanes] == ["fastembed"]
    assert "bash" in index.retrieve("run a shell command", k=8)

    announced = [r for r in caplog.records if "tool index: in-memory lane (ChromaDB not reachable)" in r.getMessage()]
    assert [r.levelno for r in announced] == [logging.INFO, logging.DEBUG], "announced once at INFO"
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING and "Chroma" in r.getMessage()]


def test_tool_index_keeps_chroma_lanes_when_available(monkeypatch, tmp_path):
    fake = FakeChroma()
    patch_chroma(monkeypatch, fake)
    import src.embedding_lanes as lanes

    def fail_custom():
        raise RuntimeError("no custom endpoint")

    monkeypatch.setattr(lanes, "_build_custom_client", fail_custom)
    _use_embedder(monkeypatch, tmp_path, FakeEmbedder(384, "mini", "local://fastembed"))
    from src.tool_index import ToolIndex

    index = ToolIndex()
    index.index_builtin_tools()
    assert index.backend == "chroma"
    assert fake.collections["odysseus_tool_index_fastembed"].count() > 0
    assert not os.path.exists(os.path.join(str(tmp_path), "tool_index_cache.json"))


def test_memory_lane_retrieval_quality_smoke(monkeypatch, tmp_path):
    _chroma_down(monkeypatch)
    _use_embedder(monkeypatch, tmp_path)
    from src.tool_index import ToolIndex

    index = ToolIndex()
    index.index_builtin_tools()

    assert "bash" in index.retrieve("run a shell command on the server", k=5)
    assert "desktop_screenshot" in index.retrieve("take a screenshot of my screen", k=5)
    assert "manage_calendar" in index.retrieve("calendar event management: list, create, update", k=5)
    email = index.retrieve("list emails for a folder, newest first", k=5)
    assert "list_emails" in email

    tools = index.get_tools_for_query("send an email to bob", k=8)
    assert "send_email" in tools and "manage_memory" in tools


def test_memory_lane_restart_reembeds_only_changed_descriptions(monkeypatch, tmp_path):
    _chroma_down(monkeypatch)
    first = _use_embedder(monkeypatch, tmp_path)
    from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS, ToolIndex

    ToolIndex().index_builtin_tools()
    assert first.calls >= 1
    assert len(first.texts_seen) == len(BUILTIN_TOOL_DESCRIPTIONS)

    second = _use_embedder(monkeypatch, tmp_path)
    ToolIndex().index_builtin_tools()
    assert second.calls == 0, "unchanged descriptions must come from the persisted cache"

    monkeypatch.setitem(BUILTIN_TOOL_DESCRIPTIONS, "bash", "Run shell commands (edited)")
    third = _use_embedder(monkeypatch, tmp_path)
    ToolIndex().index_builtin_tools()
    assert third.calls == 1
    assert third.texts_seen == ["Tool: bash\nRun shell commands (edited)"]


def test_memory_lane_indexes_and_replaces_mcp_tools(monkeypatch, tmp_path):
    _chroma_down(monkeypatch)
    _use_embedder(monkeypatch, tmp_path)
    from src.tool_index import ToolIndex

    class Mgr:
        def __init__(self, text, generation):
            self._generation = generation
            self._text = text

        def get_tool_descriptions_for_prompt(self, disabled):
            return self._text

    index = ToolIndex()
    index.index_builtin_tools()
    index.index_mcp_tools(Mgr("**home:**\n- turn_on_lights: Turn on the smart lights in a room\n", 1))
    assert index._lanes[0].collection.get(where={"tool_type": "mcp"})["ids"] == ["mcp_turn_on_lights"]
    assert "turn_on_lights" in index.retrieve("turn on the smart lights", k=5)

    index.index_mcp_tools(Mgr("**home:**\n- open_garage: Open the garage door\n", 2))
    assert index._lanes[0].collection.get(where={"tool_type": "mcp"})["ids"] == ["mcp_open_garage"]
    assert "turn_on_lights" not in index.retrieve("turn on the smart lights", k=5)


# ── singleton getter ───────────────────────────────────────────────────────

@pytest.fixture
def clean_singleton():
    import src.tool_index as ti

    ti.reset_tool_index()
    yield ti
    ti.reset_tool_index()


def test_get_tool_index_caches_unavailable_for_the_retry_interval(monkeypatch, tmp_path, clean_singleton, caplog):
    ti = clean_singleton
    _chroma_down(monkeypatch)
    import src.embedding_lanes as lanes

    attempts = []

    def no_fastembed():
        attempts.append(1)
        raise RuntimeError("Local fastembed is not installed")

    monkeypatch.setattr(lanes, "_build_fastembed_client", no_fastembed)

    with caplog.at_level(logging.DEBUG, logger="src.tool_index"):
        assert ti.get_tool_index() is None
        assert ti.get_tool_index() is None
        assert ti.get_tool_index() is None
    assert len(attempts) == 1, "the unavailable answer is cached for the retry interval"

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1

    # after the interval elapses it probes exactly once more; the repeat
    # failure is not another WARNING and the next wait is longer
    assert ti._retry_interval == ti._RETRY_INTERVAL
    monkeypatch.setattr(ti, "_last_attempt", 0.0)
    with caplog.at_level(logging.DEBUG, logger="src.tool_index"):
        assert ti.get_tool_index() is None
    assert len(attempts) == 2
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1
    assert ti._retry_interval == 2 * ti._RETRY_INTERVAL


def test_get_tool_index_does_not_block_while_a_build_is_in_flight(monkeypatch, tmp_path, clean_singleton):
    ti = clean_singleton
    _chroma_down(monkeypatch)
    import src.embedding_lanes as lanes
    import src.tool_index_memory as tim

    monkeypatch.setattr(tim, "DEFAULT_CACHE_PATH", os.path.join(str(tmp_path), "tool_index_cache.json"))
    release = threading.Event()
    started = threading.Event()

    def slow_fastembed():
        started.set()
        release.wait(5)
        return HashingEmbedder()

    monkeypatch.setattr(lanes, "_build_fastembed_client", slow_fastembed)

    results = {}
    worker = threading.Thread(target=lambda: results.setdefault("built", ti.get_tool_index()))
    worker.start()
    assert started.wait(5)

    t0 = time.monotonic()
    assert ti.get_tool_index() is None
    assert time.monotonic() - t0 < 0.5, "a concurrent caller must not wait for the build"

    release.set()
    worker.join(10)
    assert results["built"] is not None
    assert ti.get_tool_index() is results["built"]


def test_get_tool_index_falls_back_to_memory_when_chroma_indexing_fails(monkeypatch, tmp_path, clean_singleton):
    ti = clean_singleton
    fake = FakeChroma()
    patch_chroma(monkeypatch, fake)
    import src.embedding_lanes as lanes

    def fail_custom():
        raise RuntimeError("no custom endpoint")

    monkeypatch.setattr(lanes, "_build_custom_client", fail_custom)
    _use_embedder(monkeypatch, tmp_path)

    def broken_upsert(**_kwargs):
        raise RuntimeError("chroma write failed")

    col = fake.get_or_create_collection("odysseus_tool_index_fastembed")
    col.upsert = broken_upsert

    index = ti.get_tool_index()
    assert index is not None and index.healthy
    assert index.backend == "memory"
    assert "bash" in index.retrieve("run a shell command", k=8)
