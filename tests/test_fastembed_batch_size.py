import sys
import types

import numpy as np


def test_fastembed_client_embeds_in_small_batches(monkeypatch, tmp_path):
    seen = {}

    class FakeTextEmbedding:
        def __init__(self, **kwargs):
            pass

        def embed(self, texts, batch_size=256, **kwargs):
            seen["batch_size"] = batch_size
            return [np.ones(4, dtype="float32") for _ in texts]

    monkeypatch.setitem(sys.modules, "fastembed", types.SimpleNamespace(TextEmbedding=FakeTextEmbedding))
    from src import embeddings
    monkeypatch.setattr(embeddings, "FASTEMBED_CACHE_DIR", str(tmp_path))
    monkeypatch.delenv("FASTEMBED_BATCH_SIZE", raising=False)
    client = embeddings.FastEmbedClient("any-model")
    assert client.encode(["a", "b"]).shape == (2, 4)
    assert seen["batch_size"] == 16

    monkeypatch.setenv("FASTEMBED_BATCH_SIZE", "4")
    embeddings.FastEmbedClient("any-model").encode(["a"])
    assert seen["batch_size"] == 4
