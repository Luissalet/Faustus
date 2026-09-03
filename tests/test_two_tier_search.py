"""Two-tier search: fast first, better later, never an error
(src/two_tier_search.py).

The contract these tests pin is a promise about DEGRADATION, not about
ranking: whatever is missing, the caller gets a result and is told which
lanes produced it.

  * ``refined`` — a real embedder mixed over tier 1 at α = 0.7;
  * ``hybrid``  — BM25-lite + hash vectors, fused by ``Σ 1/(60 + rank)``;
  * ``lexical`` — nothing could be vectorised at all.

and ``search`` never raises, for any corpus and any query, which is checked
against a deliberately hostile one.
"""
from __future__ import annotations

import pytest

from src import hash_embed, two_tier_search
from src.two_tier_search import ALPHA, RRF_K, rrf, search, snippet


CORPUS = [
    {"id": "m1", "text": "How do I get docker compose to use the nvidia gpu runtime?",
     "title": "Docker GPU"},
    {"id": "m2", "text": "Add a deploy.resources.reservations.devices block naming the nvidia driver.",
     "title": "Docker GPU"},
    {"id": "m3", "text": "My sourdough starter is not rising after three days of feeding.",
     "title": "Sourdough"},
    {"id": "m4", "text": "Feed it twice a day at a one to one ratio and keep it near 25 degrees.",
     "title": "Sourdough"},
    {"id": "m5", "text": "I keep getting a sqlite database is locked error under load.",
     "title": "SQLite"},
    {"id": "m6", "text": "Turn on WAL journal mode and keep the connections short lived.",
     "title": "SQLite"},
]


class FakeClock:
    """An injected clock, so elapsed_ms is a fact rather than a race."""

    def __init__(self, *ticks):
        self.ticks = list(ticks)

    def __call__(self):
        return self.ticks.pop(0) if self.ticks else 0.0


class EncoderEmbedder:
    """The ``encode(list[str]) -> list[vector]`` shape (a fastembed client)."""

    def __init__(self, table=None, explode=False):
        self.table = table or {}
        self.explode = explode
        self.calls = 0

    def encode(self, texts):
        self.calls += 1
        if self.explode:
            raise RuntimeError("the embedding endpoint is down")
        return [self.table.get(text, [0.0, 0.0, 1.0]) for text in texts]


class StoreEmbedder:
    """The ``search(query, k) -> [{"memory_id", "score"}]`` shape."""

    def __init__(self, hits=None, explode=False):
        self.hits = hits or []
        self.explode = explode

    def search(self, query, k):
        if self.explode:
            raise RuntimeError("the vector store is unreachable")
        return list(self.hits)


# ── RRF, against a hand-computed expectation ────────────────────────────────


def test_rrf_is_the_reports_formula_to_the_decimal():
    """``Σ 1/(60 + rank)`` with rank starting at 1 — computed by hand."""
    assert RRF_K == 60.0
    lexical = ["a", "b", "c"]
    semantic = ["c", "a", "d"]
    fused = rrf(lexical, semantic)
    assert fused["a"] == pytest.approx(1 / 61 + 1 / 62)     # 0.032926...
    assert fused["b"] == pytest.approx(1 / 62)              # 0.016129...
    assert fused["c"] == pytest.approx(1 / 63 + 1 / 61)     # 0.032262...
    assert fused["d"] == pytest.approx(1 / 63)              # 0.015873...
    # …and the resulting order, which is the point of the constant: "a" wins
    # on agreement even though "c" is the other lane's top hit.
    assert sorted(fused, key=lambda k: -fused[k]) == ["a", "c", "b", "d"]


def test_the_hash_lane_is_fused_at_half_weight_and_that_is_measured_not_tuned():
    """BM25 and the hash cosine read the SAME tokens, so they are not the
    independent evidence RRF assumes. Weighted equally, the lane without IDF
    demotes the lane with it — measured, and written down in the docstring.

    This is the same rule the spec already applies at tier 2 (α = 0.7 for the
    real embedder), one level down. The constant 60 is untouched.
    """
    assert two_tier_search.HASH_WEIGHT == 0.5
    assert two_tier_search.BM25_WEIGHT == 1.0
    assert two_tier_search.RRF_K == 60.0

    # What the weight does, exactly: it halves the weak lane's leverage. It
    # does not silence it — a document only the hash lane found still scores,
    # which is the recall the lane is there for.
    lexical, hashed = ["a", "b", "c"], ["c", "d"]
    weighted = rrf(lexical, hashed,
                   weights=(two_tier_search.BM25_WEIGHT, two_tier_search.HASH_WEIGHT))
    assert weighted["a"] == pytest.approx(1 / 61)                 # lexical only
    assert weighted["c"] == pytest.approx(1 / 63 + 0.5 / 61)      # both, hash halved
    assert weighted["d"] == pytest.approx(0.5 / 62)               # hash only, still there
    equal = rrf(lexical, hashed)
    assert equal["c"] > weighted["c"] and equal["d"] > weighted["d"]
    assert equal["a"] == weighted["a"], "the strong lane's own score is untouched"


def test_the_weight_is_what_restores_recall_on_long_documents():
    """The measurement the docstring's table records, run as a test.

    At the report's plain ``Σ 1/(60+rank)`` the fusion loses documents BM25
    alone finds; at ``HASH_WEIGHT`` it does not. If a future change makes the
    hash lane a true peer, this test is where the weight stops being needed.
    """
    from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS

    docs = [(name, f"Tool: {name}\n{desc}")
            for name, desc in BUILTIN_TOOL_DESCRIPTIONS.items()]
    cases = [("run a shell command", "bash"),
             ("search the web for the latest news", "web_search")]

    for query, want in cases:
        lexical = two_tier_search._ordered(two_tier_search.bm25_scores(query, docs))
        hashed = [doc_id for doc_id, _ in hash_embed.rank(query, docs)]
        equal = two_tier_search._ordered(rrf(lexical, hashed))
        weighted = two_tier_search._ordered(
            rrf(lexical, hashed,
                weights=(two_tier_search.BM25_WEIGHT, two_tier_search.HASH_WEIGHT)))
        assert want in lexical[:8], (query, "BM25 alone finds it")
        assert want not in equal[:8], (query, "…and equal weights lose it")
        assert want in weighted[:8], (query, "…and the shipped weight keeps it")
        # the shipped path agrees with the weighted computation
        assert want in [hit["id"] for hit in
                        search([{"id": i, "text": t} for i, t in docs], query, k=8)["hits"]]


def test_rrf_weights_are_defensive_about_what_they_are_given():
    lanes = (["a"], ["b"])
    assert rrf(*lanes, weights=None) == rrf(*lanes)
    assert rrf(*lanes, weights=(1.0,))["b"] == pytest.approx(1 / 61)   # short → 1.0
    assert rrf(*lanes, weights=("junk", None))["a"] == pytest.approx(1 / 61)


def test_rrf_lets_a_ranking_of_one_lane_still_score():
    fused = rrf(["only"], [])
    assert fused == {"only": pytest.approx(1 / 61)}
    assert rrf() == {}
    assert rrf([], []) == {}


def test_rrf_k_changes_how_much_a_single_lane_can_dominate():
    small = rrf(["x"], ["y"], k=1)
    assert small["x"] == pytest.approx(0.5)


# ── the three tiers ─────────────────────────────────────────────────────────


def test_no_embedder_is_hybrid_on_hash_vectors_and_says_degraded():
    found = search(CORPUS, "nvidia gpu runtime", k=3)
    assert found["tier"] == "hybrid"
    assert found["degraded"] is True
    assert found["lanes"] == ["bm25", "hash"]
    assert found["hits"][0]["id"] == "m1"
    # the caller's own metadata rides through untouched
    assert found["hits"][0]["title"] == "Docker GPU"
    assert found["hits"][0]["rank"] == 1
    assert found["hits"][0]["tier"] == "hybrid"


def test_a_query_that_cannot_be_vectorised_at_all_is_lexical():
    """Punctuation embeds to a zero vector, so there is no vector lane to
    fuse — and the answer says ``lexical``, not ``hybrid``."""
    assert not any(hash_embed.embed("!!! ??? ..."))
    found = search(CORPUS, "!!! ??? ...", k=3)
    assert found["tier"] == "lexical"
    assert found["lanes"] == ["bm25"]
    assert found["degraded"] is True


def test_a_real_embedder_makes_it_refined_and_not_degraded():
    # A tiny embedder that puts m6 first, disagreeing with the lexical lane.
    table = {row["text"]: ([1.0, 0.0, 0.0] if row["id"] == "m6" else [0.0, 1.0, 0.0])
             for row in CORPUS}
    table["wal journal"] = [1.0, 0.0, 0.0]
    found = search(CORPUS, "wal journal", k=3, embedder=EncoderEmbedder(table))
    assert found["tier"] == "refined"
    assert found["degraded"] is False
    assert found["lanes"] == ["bm25", "hash", "embedder"]
    assert found["hits"][0]["id"] == "m6"


def test_the_store_shaped_embedder_is_accepted_too():
    store = StoreEmbedder([{"memory_id": "m3", "score": 0.9},
                           {"memory_id": "not-in-corpus", "score": 1.0},
                           {"id": "m4", "score": 0.4},
                           "junk"])
    found = search(CORPUS, "sourdough", k=3, embedder=store)
    assert found["tier"] == "refined" and found["degraded"] is False
    assert [hit["id"] for hit in found["hits"][:2]] == ["m3", "m4"]


@pytest.mark.parametrize("embedder", [
    EncoderEmbedder(explode=True),
    StoreEmbedder(explode=True),
    object(),                       # neither shape
    "not an embedder",
])
def test_a_sick_embedder_degrades_to_tier_one_instead_of_raising(embedder):
    found = search(CORPUS, "nvidia gpu runtime", k=3, embedder=embedder)
    assert found["tier"] == "hybrid"
    assert found["degraded"] is True
    assert "embedder" not in found["lanes"]
    assert found["hits"][0]["id"] == "m1"


def test_an_embedder_that_finds_nothing_leaves_the_answer_on_tier_one():
    found = search(CORPUS, "nvidia gpu", k=3, embedder=StoreEmbedder([]))
    assert found["tier"] == "hybrid" and found["degraded"] is True


def test_alpha_is_the_reports_zero_point_seven_and_tier_one_still_counts():
    """α weights the refined lane; the other 0.3 is why a lexical exact match
    is not simply thrown away."""
    assert ALPHA == 0.7
    # The embedder likes m5 a little; BM25 is certain about m6.
    store = StoreEmbedder([{"memory_id": "m5", "score": 1.0},
                           {"memory_id": "m6", "score": 0.99}])
    found = search(CORPUS, "wal journal mode connections", k=6, embedder=store)
    scores = {hit["id"]: hit["score"] for hit in found["hits"]}
    # m6's tier-1 score is the top one (1.0 normalised), m5's refined score is
    # the top one — so both survive and the mix is visible in the numbers.
    assert scores["m6"] == pytest.approx(ALPHA * (0.99 / 1.0) + (1 - ALPHA) * 1.0, abs=1e-6)
    assert 0.0 < scores["m5"] <= 1.0


# ── never an error ──────────────────────────────────────────────────────────


JUNK_CORPORA = [
    None,
    [],
    "not a corpus",
    [None, 3, "string", {"no_id": 1}, {"id": None, "text": "x"}],
    [{"id": "a"}],                                  # no text at all
    [{"id": "a", "text": None}],
    [{"id": 7, "text": 7}],                         # non-string id and text
    [{"id": "a", "text": "x"}, {"id": "a", "text": "duplicate id"}],
    [{"id": "a", "text": "x" * 200_000}],
]
JUNK_QUERIES = [None, "", "   ", "!!!", 12, {"not": "a query"}, "x" * 10_000]


@pytest.mark.parametrize("corpus", JUNK_CORPORA)
@pytest.mark.parametrize("query", JUNK_QUERIES)
def test_search_never_raises(corpus, query):
    found = search(corpus, query, k=5)
    assert set(found) == {"hits", "tier", "degraded", "elapsed_ms", "lanes"}
    assert isinstance(found["hits"], list)
    assert found["tier"] in two_tier_search.TIERS
    assert isinstance(found["degraded"], bool)


def test_an_exploding_corpus_still_answers():
    class Hostile:
        def __iter__(self):
            raise RuntimeError("this iterable is a trap")

    found = search(Hostile(), "anything", k=3)
    assert found["hits"] == [] and found["degraded"] is True


def test_nothing_to_rank_is_not_a_degradation():
    """An empty corpus or a blank query has nothing to degrade, and does not
    wake an embedder to report it."""
    embedder = EncoderEmbedder()
    for found in (search([], "query", embedder=embedder),
                  search(CORPUS, "", embedder=embedder)):
        assert found["hits"] == []
        assert found["tier"] == "lexical"
        assert found["degraded"] is False
        assert found["lanes"] == []
    assert embedder.calls == 0


@pytest.mark.parametrize("k,expected", [(1, 1), (3, 3), (0, 6), (None, 6), ("junk", 6),
                                        (10_000, 6)])
def test_k_is_clamped_rather_than_trusted(k, expected):
    assert len(search(CORPUS, "the", k=k)["hits"]) <= expected


# ── the clock is injectable, and the results are deterministic ──────────────


def test_elapsed_ms_comes_from_the_injected_clock():
    found = search(CORPUS, "sourdough", clock=FakeClock(10.0, 10.25))
    assert found["elapsed_ms"] == pytest.approx(250.0)


def test_a_broken_clock_does_not_break_the_search():
    def explode():
        raise RuntimeError("no clock")

    found = search(CORPUS, "sourdough", clock=explode)
    assert found["hits"] and found["elapsed_ms"] == 0.0


def test_the_same_call_twice_gives_the_same_order():
    first = [hit["id"] for hit in search(CORPUS, "keep it near the connections", k=6)["hits"]]
    second = [hit["id"] for hit in search(CORPUS, "keep it near the connections", k=6)["hits"]]
    assert first == second


def test_bm25_normalises_to_one_and_ignores_a_query_with_no_terms():
    scores = two_tier_search.bm25_scores(
        "sourdough", [(row["id"], row["text"]) for row in CORPUS])
    assert max(scores.values()) == pytest.approx(1.0)
    assert two_tier_search.bm25_scores("", [("a", "x")]) == {}
    assert two_tier_search.bm25_scores("x", []) == {}
    assert two_tier_search.bm25_scores("x", [("a", "")]) == {}


# ── snippets: the span that matched, and its real offsets ───────────────────


def test_snippet_returns_the_offsets_into_the_original_text():
    text = "The quick brown fox jumps over the lazy dog near the river bank."
    out = snippet(text, "jumps", width=40)
    assert text[out["match_start"]:out["match_end"]] == "jumps"
    assert out["text"] in text
    assert text[out["start"]:out["end"]] == out["text"]


def test_snippet_never_invents_a_highlight():
    out = snippet("nothing here matches", "absent")
    assert out["match_start"] is None and out["match_end"] is None
    assert out["start"] == 0 and out["text"].startswith("nothing")


def test_snippet_prefers_the_longest_matching_term():
    out = snippet("the reciprocal rank fusion constant", "the fusion")
    assert out["match_start"] == "the reciprocal rank fusion constant".index("fusion")


@pytest.mark.parametrize("text,query,width", [
    (None, None, None), ("", "", 0), ("x", "x", -3), ("x" * 5000, "x", "junk"),
])
def test_snippet_never_raises(text, query, width):
    out = snippet(text, query, width) if width is not None else snippet(text, query)
    assert set(out) == {"text", "start", "end", "match_start", "match_end"}
    assert isinstance(out["text"], str)


def test_snippet_window_is_bounded_and_does_not_start_mid_word():
    body = "alpha " * 200 + "needle " + "omega " * 200
    out = snippet(body, "needle", width=120)
    assert len(out["text"]) <= 120
    assert "needle" in out["text"]
    assert not out["text"].startswith("lpha")


# ── the measured claim in the module docstring ──────────────────────────────


def test_the_tool_index_lexical_floor_ranks_a_known_tool_first_with_no_embedder():
    """The wiring in ``src.tool_index.retrieve``: NO embedder, no lanes, no
    ChromaDB, nothing indexed — and the right tool still comes back.

    Before this, that path returned an empty list and every agent turn fell
    back to keyword-only tool selection. The bar is the one the vector-lane
    suite already sets — membership in the k that ``retrieve`` returns, since
    that is the set the agent loop unions into the prompt — plus rank 1 where
    the lexical floor genuinely earns it.
    """
    from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS, ToolIndex

    index = ToolIndex.__new__(ToolIndex)     # no constructor, so no embedder
    index._lanes = []

    # rank 1, on queries where the floor really does put it there
    assert index.retrieve("take a screenshot of my screen", k=5)[0] == "desktop_screenshot"
    assert index.retrieve("find files by glob pattern", k=5)[0] == "glob"
    assert index.lexical_retrieve("read a file from disk and return its contents",
                                  k=5)[0] == "read_file"
    # membership, the contract the existing vector-lane tests assert
    assert "bash" in index.retrieve("run a shell command", k=8)
    assert "web_search" in index.retrieve("search the web for the latest news", k=8)
    assert "manage_calendar" in index.retrieve("calendar event management", k=8)
    # …and it is the same corpus the vector lanes index, not a separate list.
    assert {row["id"] for row in index.corpus_rows()} == set(BUILTIN_TOOL_DESCRIPTIONS)


def test_the_lexical_floor_only_fires_when_no_lane_answered():
    """A working lane must never be second-guessed by the floor."""
    from src.tool_index import ToolIndex

    class Lane:
        name = "fastembed"

        class collection:
            @staticmethod
            def query(**_kwargs):
                return {"ids": [["builtin_glob"]],
                        "metadatas": [[{"tool_name": "glob", "tool_type": "builtin"}]],
                        "distances": [[0.1]]}

        @staticmethod
        def count():
            return 1

        @staticmethod
        def encode(texts):
            return [[0.0] * 8 for _ in texts]

    index = ToolIndex.__new__(ToolIndex)
    index._lanes = [Lane()]
    # the lane answers "glob" for a shell query, and the floor does not argue
    assert index.retrieve("run a shell command", k=3) == ["glob"]


def test_the_lexical_floor_tracks_what_was_indexed(monkeypatch, tmp_path):
    """An MCP server that disconnects must not leave its tools in the floor."""
    import src.embedding_lanes as lanes
    import src.tool_index as ti
    import src.tool_index_memory as tim
    from tests.test_tool_index_memory_lane import HashingEmbedder, _chroma_down

    _chroma_down(monkeypatch)
    monkeypatch.setattr(lanes, "_build_fastembed_client", lambda: HashingEmbedder())
    monkeypatch.setattr(tim, "DEFAULT_CACHE_PATH", str(tmp_path / "cache.json"))

    class Manager:
        _generation = 1

        @staticmethod
        def get_tool_descriptions_for_prompt(_disabled):
            return "**home:**\n- turn_on_lights: switch the smart lights on\n"

    index = ti.ToolIndex()
    index.index_builtin_tools()
    index.index_mcp_tools(Manager())
    assert "turn_on_lights" in {row["id"] for row in index.corpus_rows()}

    Manager._generation = 2
    Manager.get_tool_descriptions_for_prompt = staticmethod(lambda _d: "")
    index.index_mcp_tools(Manager())
    assert "turn_on_lights" not in {row["id"] for row in index.corpus_rows()}
    assert "bash" in {row["id"] for row in index.corpus_rows()}


def test_tier_one_beats_nothing_on_the_tool_corpus():
    """The docstring says the lexical floor turns 0/20 into 10/20 top-1 on
    ``BUILTIN_TOOL_DESCRIPTIONS``. Pin the shape of that claim so the number
    in the docstring cannot rot into fiction unnoticed."""
    from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS

    corpus = [{"id": name, "text": f"Tool: {name}\n{desc}"}
              for name, desc in BUILTIN_TOOL_DESCRIPTIONS.items()]
    cases = [
        ("take a screenshot of my screen", "desktop_screenshot"),
        ("read a file from disk and return its contents", "read_file"),
        ("generate an image from a text prompt", "generate_image"),
        ("create a scheduled task every morning", "manage_tasks"),
        ("find files by glob pattern", "glob"),
    ]
    top1 = 0
    for query, want in cases:
        found = search(corpus, query, k=5)
        assert found["tier"] == "hybrid" and found["degraded"] is True
        names = [hit["id"] for hit in found["hits"]]
        assert want in names, (query, names)
        if names[:1] == [want]:
            top1 += 1
    assert top1 >= 4, "the lexical floor is meant to be usable, not perfect"
