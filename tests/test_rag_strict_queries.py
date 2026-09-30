"""Strict queries preserve failures; legacy retrieval keeps its fallback."""
import pytest

from src.embedding_lanes import EmbeddingLane, query_lanes, query_lanes_strict
from src.rag_vector import VectorRAG
from src.rag_manager import RAGManager


class Encoder:
    def encode(self, texts, normalize_embeddings=True):
        return [[1., 0., 0.] for _ in texts]


class Collection:
    def __init__(self, count=1, failure=None):
        self.value, self.failure = count, failure
        self.count_calls = 0
        self.query_calls = []

    def count(self):
        self.count_calls += 1
        if self.failure == "count":
            raise OSError("QA count failure")
        return self.value

    def query(self, **kwargs):
        self.query_calls.append(kwargs)
        if self.failure == "query":
            raise OSError("QA query failure")
        result = {"ids": [["qa-id"]], "documents": [["hat"]],
                  "metadatas": [[{"owner": "qa", "source": "qa.txt"}]], "distances": [[0.]]}
        if self.failure == "shape":
            result["documents"] = [[]]
        return result


def lane(collection):
    return EmbeddingLane("qa", Encoder(), collection, "qa", "qa", "", 3, "qa-fingerprint")


def rag(*collections):
    obj = VectorRAG.__new__(VectorRAG)
    obj._lanes = [lane(c) for c in collections]
    obj._healthy = True
    obj._collection = collections[0] if collections else None
    return obj


def test_existing_lenient_query_hides_count_failure():
    broken = lane(Collection(failure="count"))
    assert broken.healthy
    assert query_lanes([broken], "hat", lambda _: 1, ["documents"],
                       raise_if_all_failed=True) == []


@pytest.mark.parametrize("failure", ["count", "query", "shape"])
@pytest.mark.parametrize("broken_first", [False, True])
def test_any_lane_failure_raises_without_fallback(failure, broken_first, monkeypatch):
    collections = [Collection(), Collection(failure=failure)]
    if broken_first:
        collections.reverse()
    obj = rag(*collections)
    monkeypatch.setattr(obj, "_keyword_search_fallback", lambda *a, **k: pytest.fail("strict fallback"))
    with pytest.raises((OSError, ValueError)):
        obj.search("hat", owner="qa", strict=True)


@pytest.mark.parametrize("invalid", [True, -1, 1.0, "1", None])
def test_invalid_count_is_not_empty(invalid):
    with pytest.raises(ValueError):
        rag(Collection(count=invalid)).search("hat", strict=True)


def test_known_empty_counts_once_and_never_queries():
    empty = Collection(count=0)
    assert rag(empty).search("hat", owner="qa", strict=True) == []
    assert empty.count_calls == 1
    assert empty.query_calls == []


def test_counts_once_preserves_owner_limit_and_identity():
    collection = Collection(count=30)
    obj = rag(collection)
    assert obj.search("hat", k=2, owner="qa", strict=True)[0]["id"] == "qa-id"
    assert collection.count_calls == 1
    assert collection.query_calls[0]["n_results"] == 12
    assert collection.query_calls[0]["where"] == {"owner": "qa"}


def test_capture_does_not_switch_collection_after_count():
    old, replacement = Collection(), Collection()
    captured = lane(old)
    original_count = old.count
    def mutate():
        captured.collection = replacement
        return original_count()
    old.count = mutate
    result = query_lanes_strict([captured], "hat", lambda _, count: count,
                               ["documents", "metadatas", "distances"])
    assert result[0][0].collection is old
    assert len(old.query_calls) == 1 and replacement.count_calls == 0


def test_no_lanes_cannot_certify_empty():
    with pytest.raises(RuntimeError):
        rag().search("hat", strict=True)


def test_legacy_default_still_uses_keyword_fallback(monkeypatch):
    obj = rag(Collection(failure="query"))
    monkeypatch.setattr(obj, "_keyword_search_fallback", lambda *a, **k: ["legacy-fallback"])
    assert obj.search("hat") == ["legacy-fallback"]


def test_manager_legacy_call_does_not_pass_new_keyword():
    calls = []
    class Legacy:
        def search(self, query, k, owner=None):
            calls.append((query, k, owner))
            return []
    manager = RAGManager.__new__(RAGManager)
    manager.vector_rag = Legacy()
    assert manager.search("hat", 2, "qa") == []
    assert calls == [("hat", 2, "qa")]


def test_real_chroma_strict_query_and_absence(tmp_path):
    import chromadb
    from chromadb.config import Settings
    try:
        from chromadb.is_thin_client import is_thin_client
    except ImportError:
        is_thin_client = False
    if is_thin_client:
        pytest.skip("full Chroma runtime required; use isolated QA environment")
    client = chromadb.PersistentClient(path=str(tmp_path / "chroma"),
                                     settings=Settings(anonymized_telemetry=False))
    collection = client.create_collection("strict-qa", embedding_function=None,
                                          metadata={"hnsw:space": "cosine"})
    collection.add(ids=["owned", "other"], embeddings=[[1., 0., 0.], [0., 1., 0.]],
                   documents=["hat", "other owner"],
                   metadatas=[{"owner": "qa", "source": "qa.txt"},
                              {"owner": "other", "source": "other.txt"}])
    manager = RAGManager.__new__(RAGManager)
    manager.vector_rag = rag(collection)
    rows = manager.search("hat", k=2, owner="qa", strict=True)
    assert [row["id"] for row in rows] == ["owned"]
    assert rows == manager.search("hat", k=2, owner="qa")
    assert manager.search("hat", owner="absent-owner", strict=True) == []
    partial = rag(collection, Collection(failure="count"))
    with pytest.raises(OSError):
        partial.search("hat", owner="qa", strict=True)
    partial = rag(collection, Collection(failure="query"))
    with pytest.raises(OSError):
        partial.search("hat", owner="qa", strict=True)
    collection.update(ids=["owned"], documents=["hat changed"], embeddings=[[1., 0., 0.]])
    assert manager.search("hat", owner="qa", strict=True)[0]["document"] == "hat changed"
    collection.delete(ids=["owned", "other"])
    assert manager.search("hat", owner="qa", strict=True) == []
    client.delete_collection("strict-qa")
    with pytest.raises(Exception):
        manager.search("hat", owner="qa", strict=True)
