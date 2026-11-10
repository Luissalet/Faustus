"""RAG trace routes: the retrieval the chat does, the cut it applies and the answer of the default model, in one reply."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes import rag_trace_routes as R


class FakeRag:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def search(self, query, k=5, owner=None):
        self.calls.append((query, k, owner))
        return self.rows[:k]


def row(n, similarity, text, filename="manual.md"):
    return {"id": f"doc_{n}", "document": text, "similarity": similarity, "vector_similarity": similarity, "keyword_score": 0.0,
            "metadata": {"filename": filename, "source": f"/docs/{filename}", "chunk_id": n}}


@pytest.fixture
def client(monkeypatch):
    rows = [row(1, 0.82, "The lamp was first lit in 1887."), row(2, 0.41, "The tower is 42 metres high."), row(3, 0.2, "Unrelated text."),
            row(4, 0.9, "Sixth by rank is never used."), row(5, 0.9, "x"), row(6, 0.9, "Beyond the chat depth.")]
    rag = FakeRag(rows)
    import src.rag_singleton as singleton
    monkeypatch.setattr(singleton, "get_rag_manager", lambda: rag)
    app = FastAPI()
    app.include_router(R.setup_rag_trace_routes())
    c = TestClient(app)
    c.rag = rag
    return c


def test_search_returns_the_passages_in_order_and_marks_what_the_chat_would_use(client):
    out = client.post("/api/rag/search", json={"query": "When was the lamp lit?", "k": 6}).json()
    assert [r["id"] for r in out["results"]] == [f"doc_{n}" for n in range(1, 7)]
    assert out["threshold"] == pytest.approx(0.35)
    assert [r["used"] for r in out["results"]] == [True, True, False, True, True, False]      # below the threshold, and beyond the chat's five
    assert out["results"][0]["filename"] == "manual.md" and out["results"][0]["text"].startswith("The lamp")
    assert client.rag.calls[0][1] == 6


def test_a_threshold_given_in_the_request_replaces_the_chat_one(client):
    out = client.post("/api/rag/search", json={"query": "q", "k": 3, "threshold": 0.5}).json()
    assert [r["used"] for r in out["results"]] == [True, False, False]


def test_answer_sends_the_used_passages_to_the_default_model(client, monkeypatch):
    seen = {}

    def fake_call(url, model, messages, **kwargs):
        seen.update(url=url, model=model, messages=messages, kwargs=kwargs)
        return " In 1887. "

    import src.endpoint_resolver as resolver
    import src.llm_core as llm_core
    monkeypatch.setattr(resolver, "resolve_endpoint", lambda prefix: ("http://model.invalid/v1", "test-model", {"X": "1"}))
    monkeypatch.setattr(llm_core, "llm_call", fake_call)
    out = client.post("/api/rag/answer", json={"query": "When was the lamp lit?", "k": 3}).json()
    assert out["answer"] == "In 1887." and out["model"] == "test-model" and out["context_passages"] == 2
    context = seen["messages"][1]["content"]
    assert "The lamp was first lit in 1887." in context and "The tower is 42 metres high." in context and "Unrelated text." not in context
    assert seen["messages"][-1] == {"role": "user", "content": "When was the lamp lit?"}
    assert seen["messages"][0]["role"] == "system"


def test_without_a_used_passage_the_model_gets_no_context(client, monkeypatch):
    seen = {}
    import src.endpoint_resolver as resolver
    import src.llm_core as llm_core
    monkeypatch.setattr(resolver, "resolve_endpoint", lambda prefix: ("http://model.invalid/v1", "test-model", None))
    monkeypatch.setattr(llm_core, "llm_call", lambda url, model, messages, **kw: seen.update(messages=messages) or "The documents do not contain it.")
    out = client.post("/api/rag/answer", json={"query": "q", "k": 3, "threshold": 0.99}).json()
    assert out["context_passages"] == 0 and len(seen["messages"]) == 2


def test_the_store_being_down_is_a_503_and_an_unknown_source_a_400(client, monkeypatch):
    import src.rag_singleton as singleton
    monkeypatch.setattr(singleton, "get_rag_manager", lambda: None)
    assert client.post("/api/rag/search", json={"query": "q"}).status_code == 503
    assert client.post("/api/rag/search", json={"query": "q", "source": "elsewhere"}).status_code == 400


def test_brain_source_returns_whole_notes(client, monkeypatch):
    from src.brain import notes
    monkeypatch.setattr(notes, "search", lambda owner, q, limit=20: [{"path": "Notes/a.md", "title": "A", "snippet": "…", "score": 3.2},
                                                                    {"path": "Notes/b.md", "title": "B", "snippet": "…", "score": 1.1}][:limit])
    monkeypatch.setattr(notes, "read_note", lambda owner, path: {"content": f"content of {path}"})
    out = client.post("/api/rag/search", json={"query": "q", "k": 2, "source": "brain"}).json()
    assert [r["id"] for r in out["results"]] == ["Notes/a.md", "Notes/b.md"]
    assert out["results"][0]["text"] == "content of Notes/a.md" and all(r["used"] for r in out["results"])
