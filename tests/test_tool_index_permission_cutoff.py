"""Narrow finished rankings without changing scores, query depth, or policy."""
import asyncio
import json
import pytest
from src import tool_index as ti, two_tier_search as search, tool_serve
from src.agent_tools import TOOL_HANDLERS
from src.tool_policy import ToolPolicy


class Lane:
    name = ti.LANE_FASTEMBED
    model = "synthetic-no-floor"
    def __init__(self, hits):
        self.hits, self.requests = hits, []
        self.collection = self
    def count(self):
        return len(self.hits)
    def encode(self, texts):
        return [[0.0]]
    def query(self, **kwargs):
        self.requests.append(kwargs["n_results"])
        hits = self.hits[:kwargs["n_results"]]
        return {"metadatas": [[{"tool_name": name} for name, score in hits]],
                "distances": [[1.0 - score for name, score in hits]]}


def index(lanes=(), backend="test"):
    result = ti.ToolIndex.__new__(ti.ToolIndex)
    result._lanes = list(lanes)
    result._backend = backend
    result._corpus = {"builtin": {
        "read_file": "synthetic permission query exact",
        "ask_user": "synthetic permission query",
        "write_file": "synthetic permission"}, "mcp": {}}
    return result


def test_vector_finished_order_filters_before_k_without_deeper_query():
    lane = Lane([("read_file", .95), ("ask_user", .8), ("write_file", .7)])
    idx = index([lane])
    assert idx.retrieve("synthetic", k=1) == ["read_file"]
    assert idx.retrieve("synthetic", k=1, candidate_filter=lambda n: n != "read_file") == ["ask_user"]
    assert lane.requests == [3, 3]


def test_vector_pool_outside_query_window_is_still_not_recovered():
    lane = Lane([(f"denied_{i}", .99-i*.001) for i in range(24)] + [("ask_user", .8)])
    assert index([lane]).retrieve("synthetic", k=1, candidate_filter=lambda n: n == "ask_user") == []
    assert lane.requests == [24]


def test_lexical_filter_keeps_scores_and_full_scoring_corpus(monkeypatch):
    corpus = index().corpus_rows()
    scored_inputs = []
    original = search.bm25_scores
    def scores(query, rows):
        scored_inputs.append(list(rows))
        return original(query, rows)
    monkeypatch.setattr(search, "bm25_scores", scores)
    baseline = search.search(corpus, "synthetic permission query exact", k=3)
    denied = baseline["hits"][0]["id"]
    narrowed = search.search(corpus, "synthetic permission query exact", k=3, candidate_filter=lambda n: n != denied)
    old_scores = {hit["id"]: hit["score"] for hit in baseline["hits"]}
    assert narrowed["hits"] and all(hit["score"] == old_scores[hit["id"]] for hit in narrowed["hits"])
    assert scored_inputs[0] == scored_inputs[1] and len(scored_inputs[1]) == 3
    assert search.search(corpus, "synthetic permission query exact", k=1,
                         candidate_filter=lambda n: n != denied)["hits"][0]["id"] != denied


def test_lexical_retrieve_filters_before_its_cut_and_denied_anchor_does_not_replace(monkeypatch):
    idx = index()
    monkeypatch.setattr(idx, "_strong_lexical_anchor", lambda q: "read_file")
    assert idx.retrieve("synthetic permission query exact", k=1, candidate_filter=lambda n: n != "read_file") == ["ask_user"]


def test_fusion_keeps_original_rrf_inputs_and_filters_final_order(monkeypatch):
    idx = index()
    lexical = ["read_file", "ask_user", "write_file"]
    vector = ["read_file", "write_file", "ask_user"]
    requests, votes = [], []
    monkeypatch.setattr(idx, "lexical_retrieve", lambda q, k: requests.append(k) or lexical)
    monkeypatch.setattr(idx, "_strong_lexical_anchor", lambda q: "read_file")
    original = search.rrf
    def rrf(*rankings, **kwargs):
        scores = original(*rankings, **kwargs)
        votes.append((rankings, scores))
        return scores
    monkeypatch.setattr(search, "rrf", rrf)
    assert idx._with_lexical_lane("synthetic", vector, 1) == ["read_file"]
    assert idx._with_lexical_lane("synthetic", vector, 1, candidate_filter=lambda n: n != "read_file") != ["read_file"]
    assert votes[0] == votes[1] and requests == [24, 24]


def test_all_denied_vector_and_lexical_remain_empty():
    assert index([Lane([("read_file", .9)])]).retrieve("synthetic", k=1, candidate_filter=lambda n: False) == []
    assert index().retrieve("synthetic", k=1, candidate_filter=lambda n: False) == []


def test_real_lookup_handler_uses_index_permission_filter(monkeypatch):
    idx = index([Lane([("read_file", .9), ("ask_user", .8)])])
    monkeypatch.setattr("src.tool_index.get_tool_index", lambda: idx)
    monkeypatch.setattr("src.tool_security.owner_is_admin_or_single_user", lambda owner: True)
    monkeypatch.setattr("src.tool_utils.get_mcp_manager", lambda: None)
    monkeypatch.setattr(tool_serve, "_keyword_hits", lambda q: [])
    monkeypatch.setattr(tool_serve, "_explicit_mcp_hits", lambda q: [])
    _, result = asyncio.run(TOOL_HANDLERS["lookup_tools"](json.dumps({"query": "synthetic", "k": 1}),
        {"tool_policy": ToolPolicy(disabled_tools=frozenset({"read_file"}))}))
    assert result["promote"] == ["ask_user"]


def test_uninspectable_legacy_adapter_still_receives_original_k(monkeypatch):
    calls = []
    class Legacy:
        def retrieve(self, query, k=8):
            calls.append(k)
            return ["ask_user"]
    monkeypatch.setattr("src.tool_index.get_tool_index", lambda: Legacy())
    monkeypatch.setattr(tool_serve, "_keyword_hits", lambda q: [])
    monkeypatch.setattr(tool_serve, "_explicit_mcp_hits", lambda q: [])
    monkeypatch.setattr("inspect.signature", lambda value: (_ for _ in ()).throw(ValueError("no signature")))
    assert tool_serve.search_catalog("synthetic", k=1, candidate_filter=lambda n: True) == ["ask_user"]
    assert calls == [1]


def test_predicate_exception_never_repopulates_vector_results():
    def failed(name):
        raise RuntimeError("synthetic predicate failure")
    with pytest.raises(RuntimeError, match="predicate failure"):
        index([Lane([("read_file", .9)])]).retrieve("synthetic", k=1, candidate_filter=failed)


def test_predicate_exception_in_lexical_search_yields_no_hits():
    def failed(name):
        raise RuntimeError("synthetic predicate failure")
    assert search.search(index().corpus_rows(), "synthetic", k=1, candidate_filter=failed)["hits"] == []
