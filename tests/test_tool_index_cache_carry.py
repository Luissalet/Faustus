"""The embedding cache keeps MCP vectors across a restart until the index is complete."""
import numpy as np

from src.tool_index_memory import EmbeddingCache, embedding_cache_key


def test_first_save_after_start_keeps_vectors_read_from_file(tmp_path):
    path = str(tmp_path / "cache.json")
    first = EmbeddingCache(path, "m")
    first.remember("builtin tool", np.ones(4))
    first.remember("mcp tool", np.full(4, 2.0))
    first.save()

    restarted = EmbeddingCache(path, "m")
    restarted.save([embedding_cache_key("m", "builtin tool")])  # built-ins indexed first
    assert EmbeddingCache(path, "m").lookup("mcp tool") is not None

    restarted.settle()
    restarted.save([embedding_cache_key("m", "builtin tool")])
    assert EmbeddingCache(path, "m").lookup("mcp tool") is None