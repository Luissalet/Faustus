"""Tool selection when the embedder does not speak the user's language.

Faustus falls back to a small local embedding model when ChromaDB is not
running, which on a personal machine is most of the time. That model is
English. It does not fail loudly on a Spanish request — it ranks plausibly
and wrongly, which is worse, because nothing downstream can tell.

Measured on the real catalogue before this was fixed, with the embedding
lane deciding alone:

    "lee el fichero server.py"            -> download_model, list_served_models…
                                             and no read_file at all
    "qué aplicaciones mías puedes usar"   -> WhatsApp tools, while BM25 scored
                                             the right answer 1.0 at rank 1

Seventeen requests in both languages: the lane alone got 11, the lexical
path alone 15, the two fused 16. So both lanes vote. The point is not that
BM25 is better — it is that neither of them should be deciding alone.
"""
from __future__ import annotations

import pytest

from tests.test_tool_index_memory_lane import _chroma_down, _use_embedder


class EnglishOnlyEmbedder:
    """An embedder that understands English and is noise otherwise.

    Not a caricature: a bag-of-words model trained on English text does
    exactly this with Spanish — it returns a confident vector built from
    nothing the query actually said.
    """

    def __init__(self, dim=256):
        self.dim = dim
        self.model = "english-only"
        self.url = "local://test"
        self.calls = 0

    def get_sentence_embedding_dimension(self):
        return self.dim

    def encode(self, texts, normalize_embeddings=True):
        self.calls += 1
        out = []
        for text in texts:
            vec = [0.0] * self.dim
            for word in str(text).lower().split():
                if word.isascii():
                    vec[hash(word) % self.dim] += 1.0
                else:
                    # Non-ASCII: a stable but meaningless bucket, which is
                    # what "the model has never seen this token" looks like
                    # from the outside.
                    vec[len(word) % self.dim] += 1.0
            norm = sum(v * v for v in vec) ** 0.5 or 1.0
            out.append([v / norm for v in vec])
        return out


@pytest.fixture
def index(monkeypatch, tmp_path):
    _chroma_down(monkeypatch)
    _use_embedder(monkeypatch, tmp_path, embedder=EnglishOnlyEmbedder())
    from src.tool_index import ToolIndex

    idx = ToolIndex()
    idx.index_builtin_tools()
    assert idx.backend == "memory"
    return idx


def test_a_request_in_another_language_still_finds_its_tool(index):
    """The regression that started this: every one of these has an exact
    phrase in the tool's own examples, and the embedding lane could not
    see any of them."""
    assert "read_file" in index.retrieve("lee el fichero server.py", k=8)
    assert "plugins_list" in index.retrieve("qué aplicaciones mías puedes usar", k=8)
    assert "plugin_app" in index.retrieve("abre mi app de escribir y enséñamela", k=8)
    assert "desktop_screenshot" in index.retrieve("hazme una captura de pantalla", k=8)


def test_english_requests_did_not_pay_for_it(index):
    """Fusing must not trade one blind spot for another."""
    assert "read_file" in index.retrieve("read the file server.py", k=8)
    assert "plugins_list" in index.retrieve("what plugins do you have connected", k=8)
    assert "bash" in index.retrieve("run a shell command", k=8)


def test_the_lexical_lane_votes_even_when_the_embedder_is_confident(index, monkeypatch):
    """A lane that answers every question with the same wrong list must not
    be able to decide the turn on its own."""
    fixed = ["generate_image", "edit_image", "inspect_media",
             "download_model", "list_downloads", "manage_tokens",
             "whatsapp_send", "board_link"]
    # What the embedding lane returns, whatever it was asked.
    names = index._with_lexical_lane("lee el fichero server.py", list(fixed), 8)
    assert "read_file" in names, names
    assert names[:2] != fixed[:2], "the lane cannot keep the whole head either"


def test_a_broken_lexical_lane_leaves_the_embedder_in_charge(index, monkeypatch):
    """Ranking is not worth failing a turn over: if the lexical side raises,
    the answer is the one the lane gave, not an exception."""
    def boom(self, query, k=8):
        raise RuntimeError("corpus unavailable")

    monkeypatch.setattr(type(index), "lexical_retrieve", boom)
    names = index.retrieve("read the file server.py", k=8)
    assert isinstance(names, list) and names


def test_retrieval_does_not_get_slower_than_the_turn_it_serves(index):
    """Both lanes on every turn is only defensible because it is cheap:
    about 30ms against a turn measured in seconds."""
    import time

    index.retrieve("warm the caches", k=8)
    started = time.perf_counter()
    for _ in range(5):
        index.retrieve("lee el fichero server.py", k=8)
    per_query = (time.perf_counter() - started) / 5
    assert per_query < 0.5, f"{per_query * 1000:.0f}ms per query"
