"""Vector hits below the tool embedder's measured floor are not candidates
(src/tool_index.py TOOL_SCORE_FLOOR)."""
import src.tool_index as ti


class Lane:
    name = ti.LANE_FASTEMBED

    def __init__(self, model, hits):
        self.model = model
        self.hits = hits  # [(tool, score)]
        self.collection = self

    def count(self):
        return len(self.hits)

    def encode(self, texts):
        return [[0.0]]

    def query(self, **kwargs):
        return {"metadatas": [[{"tool_name": n} for n, _ in self.hits]],
                "distances": [[1.0 - s for _, s in self.hits]]}


def _index(lane):
    index = ti.ToolIndex.__new__(ti.ToolIndex)
    index._lanes = [lane]
    index._backend = "test"
    index._corpus = {"builtin": {}, "mcp": {}}
    return index


def test_noise_below_the_floor_gives_no_candidates():
    lane = Lane(ti.DEFAULT_TOOL_EMBED_MODEL, [("upcoming", 0.12), ("world_search", 0.11)])
    assert _index(lane).retrieve("Tengo 3 huevos, ¿qué ceno?", k=8) == []


def test_hits_above_the_floor_stay_in_order():
    lane = Lane(ti.DEFAULT_TOOL_EMBED_MODEL, [("manage_calendar", 0.41), ("manage_notes", 0.38), ("git_log", 0.29)])
    assert _index(lane).retrieve("Mira mi calendario", k=8) == ["manage_calendar", "manage_notes"]


def test_a_model_without_a_measured_floor_keeps_every_hit():
    lane = Lane("sentence-transformers/all-MiniLM-L6-v2", [("upcoming", 0.12), ("world_search", 0.11)])
    assert _index(lane).retrieve("Tengo 3 huevos", k=8) == ["upcoming", "world_search"]
