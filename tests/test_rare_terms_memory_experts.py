"""OBJ-51: the rare-term lane reaches the memory engine and the expert corpora.

Identifiers (error codes, hashes, file names, API names) are what BM25
tokenisation breaks: the memory tokenizer keeps ``(foo_bar.py:42)`` or
``error=0x80070005`` glued to its punctuation, and the expert tokenizer splits
``app.log`` and ``v0.35.1`` into common words. The lane that
``src.two_tier_search`` already had reads the raw query and the raw text
instead; these tests pin that both searches use it, that an exact identifier
hit outranks what the fuzzy lanes preferred, and that a prose query is scored
exactly as before.
"""

import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import experts  # noqa: E402
from src import hash_embed, memory_engine as engine, two_tier_search as tts  # noqa: E402

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------- helpers ----


def test_exact_lane_package_matches_exact_scores():
    docs = [("a", "see (foo_bar.py:42) for the crash"), ("b", "bar and foo are placeholder names"),
            ("c", "nothing here"), ("d", "also nothing"), ("e", "still nothing")]
    lane = tts.exact_lane("where is foo_bar.py", docs)
    scores, terms, full = tts.exact_scores("where is foo_bar.py", docs)
    assert lane["scores"] == scores and lane["terms"] == terms and lane["full"] == full
    assert lane["ranked"] == ["a"] and lane["normalised"] == {"a": 1.0}
    empty = tts.exact_lane("plain prose query", docs)
    assert empty == {"scores": {}, "ranked": [], "normalised": {}, "terms": [], "full": set()}


def test_bare_acronyms_and_numbers_do_not_open_the_lane_for_memory_and_experts():
    docs = [("a", "Always run QA tests before the 2026 release"), ("b", "QA is a team"),
            ("c", "nothing"), ("d", "nothing either"), ("e", "again nothing")]
    # The hybrid search keeps treating them as rare (unchanged contract)...
    assert tts.exact_scores("Always run QA tests in 2026", docs)[1] == ["QA", "2026"]
    # ...the callers that already rank them through BM25 do not.
    assert tts.rare_terms("Always run QA tests in 2026", plain=False) == []
    assert tts.exact_lane("Always run QA tests in 2026", docs)["terms"] == []
    # What a word tokenizer does mangle still opens it, acronym-looking or not.
    assert tts.rare_terms("GPU2 A1B2C3D ERR_CONN_RESET OBJ-51", plain=False) == [
        "GPU2", "A1B2C3D", "ERR_CONN_RESET", "OBJ-51"]


def test_promote_exact_full_is_the_rule_search_already_applied():
    ordered = ["n%d" % i for i in range(10)] + ["hit"]
    promoted, rescued = tts.promote_exact_full(ordered, ["hit"], {"hit"}, 4)
    assert rescued == ["hit"] and "hit" in promoted[:4] and sorted(promoted) == sorted(ordered)
    # Nothing to rescue when it is already in the head, or when nothing is full.
    assert tts.promote_exact_full(["hit", "x"], ["hit"], {"hit"}, 2) == (["hit", "x"], [])
    assert tts.promote_exact_full(["x", "y"], [], set(), 2) == (["x", "y"], [])


def test_front_load_full_is_a_stable_partition():
    assert tts.front_load_full(["a", "b", "c", "d"], {"c", "d"}) == ["c", "d", "a", "b"]
    assert tts.front_load_full(["a", "b"], set()) == ["a", "b"]


# ---------------------------------------------------------------- memory ----


class FuzzyVectors:
    """A healthy 'semantic' lane that ranks by hash-vector cosine and puts the
    fuzzy decoys first, the way an embedder does with an identifier it cannot
    read."""

    healthy = True

    def __init__(self, texts):
        self.texts = texts

    def search(self, query, k=8, **kwargs):
        qv = hash_embed.embed(query)
        scored = sorted(((min(1.0, hash_embed.similarity(qv, hash_embed.embed(t)) * 1.6), mid)
                         for mid, t in self.texts.items()), key=lambda p: (-p[0], p[1]))
        return [{"memory_id": mid, "score": s} for s, mid in scored if s > 0][:k]


MEMORIES = {
    "errreset": "The sync worker fails with ERR_CONN_RESET when the proxy drops idle sockets.",
    "connfuzzy": "Connection resets happen when a network proxy closes idle sockets; retry with backoff.",
    "hresult": "Windows installer aborted: error=0x80070005 (access denied) on the temp folder.",
    "accessfuzzy": "Access denied errors during installation come from missing write permission.",
    "foobar": "The nightly job crashed at (foo_bar.py:42), it reads its settings from the env file.",
    "scripts": "Python scripts for the nightly job read settings from environment variables.",
    "useeffect": "In the dashboard `useEffect()` must return a cleanup function or the websocket leaks.",
    "hooks": "React hooks that subscribe to sockets need cleanup functions to avoid leaks.",
    "hash": "The regression was introduced in commit a1b2c3d, which changed the retry policy.",
    "commits": "Retry policy changes were reviewed last week and merged after the regression.",
    "version": "Release v0.35.1-rc2 fixed the sync worker and went to staging on Friday.",
    "versionfuzzy": "Release notes for the sync worker say the staging release fixed several bugs.",
    "garden": "Luis plants tomatoes in the garden every spring and waters them at dawn.",
    "coffee": "Luis prefers dark roast coffee in the morning and tea in the evening.",
    "deploy": "Deployments go through staging first and production only after the smoke tests pass.",
}


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(engine, "memory_conflict_detection_enabled", lambda: False)
    engine.set_vector_store(None)
    label_of, id_of = {}, {}
    for label, text in MEMORIES.items():
        row = engine.add_item(text, owner="o", project="p", level="semantic",
                              trust_class="human_explicit", now=NOW)
        label_of[row["id"]], id_of[label] = label, row["id"]
    yield SimpleNamespace(label_of=label_of, id_of=id_of)
    engine.reset_vector_store()


def _ranked(store, query, **kwargs):
    hits = engine.search(query, "o", "p", k=5, now=NOW, touch_hits=False, **kwargs)
    return [store.label_of[h["id"]] for h in hits], hits


@pytest.mark.parametrize("query, expected", [
    ("ERR_CONN_RESET", "errreset"),
    ("what is behind 0x80070005", "hresult"),
    ("foo_bar.py", "foobar"),
    ("where is foo_bar.py configured", "foobar"),
    ("useEffect", "useeffect"),
    ("a1b2c3d", "hash"),
    ("v0.35.1", "version"),
])
def test_memory_finds_identifiers_that_tokenisation_breaks(store, query, expected):
    labels, hits = _ranked(store, query)
    assert labels[0] == expected
    assert hits[0]["exact"] == 1.0 and hits[0]["relevance"] >= 1.0


def test_memory_baseline_really_misses_the_glued_identifiers(store):
    # The reason the lane exists: BM25 alone has nothing for these.
    assert engine.bm25_scores("foo_bar.py", [(i, t) for i, t in
                                             ((store.id_of[l], MEMORIES[l]) for l in MEMORIES)]) == {}
    assert engine.bm25_scores("0x80070005", [(store.id_of["hresult"], MEMORIES["hresult"])]) == {}


def test_memory_exact_hit_outranks_fuzzy_semantic_matches(store):
    engine.set_vector_store(FuzzyVectors({store.id_of[l]: t for l, t in MEMORIES.items()}))
    for query, expected in (("ERR_CONN_RESET", "errreset"), ("0x80070005", "hresult"),
                            ("useEffect cleanup", "useeffect"), ("v0.35.1", "version")):
        labels, hits = _ranked(store, query)
        assert labels[0] == expected, (query, labels)
        assert hits[0]["semantic"] >= 0.0 and hits[0]["degraded"] is False
        if len(hits) > 1:
            assert hits[0]["score"] > hits[1]["score"]


def test_memory_prose_queries_are_scored_exactly_as_before(store):
    for query in ("how does the team handle deployments", "what does Luis drink in the morning",
                  "plants and garden routine", "retry policy and regression discussion"):
        labels, hits = _ranked(store, query)
        assert hits, query
        for hit in hits:
            assert "exact" not in hit
            assert hit["relevance"] == round(0.9 * hit["lexical"] + 0.1 * hit["graph"], 6)


def test_memory_acronym_in_a_prose_query_is_scored_as_before(store):
    engine.add_item("Always run QA tests before every release in 2026", owner="o", project="p",
                    level="semantic", trust_class="human_explicit", now=NOW)
    hits = engine.search("Always run QA tests in 2026", "o", "p", k=3, now=NOW, touch_hits=False)
    assert hits and "exact" not in hits[0]
    assert hits[0]["relevance"] == round(0.9 * hits[0]["lexical"] + 0.1 * hits[0]["graph"], 6)


def test_memory_unknown_or_common_identifier_changes_nothing(store):
    labels, hits = _ranked(store, "ERR_NOT_THERE_99")
    assert labels == [] and hits == []
    # Present in more than half of a corpus of four or more: not rare, dropped.
    for i in range(20):
        engine.add_item("Build step BLD-%d logged the shared marker COMMON_FLAG today" % i,
                        owner="o", project="p", level="semantic", trust_class="human_explicit", now=NOW)
    hits = engine.search("COMMON_FLAG", "o", "p", k=5, now=NOW, touch_hits=False)
    assert hits and all("exact" not in h for h in hits)


def test_memory_trust_still_scales_an_exact_hit(store):
    other = engine.add_item("A second note about ERR_CONN_RESET, but flagged harmful by users.",
                            owner="o", project="p", level="semantic",
                            trust_class="human_explicit", now=NOW)
    for n in range(4):
        engine.add_feedback(other["id"], "harmful", ref="r%d" % n, event_id="e%d" % n, now=NOW)
    hits = engine.search("ERR_CONN_RESET", "o", "p", k=5, now=NOW, touch_hits=False)
    ids = [h["id"] for h in hits]
    assert ids[0] == store.id_of["errreset"] and other["id"] in ids
    by_id = {h["id"]: h for h in hits}
    # Both hold the identifier verbatim; the memory's own track record decides.
    assert by_id[other["id"]]["exact"] == by_id[store.id_of["errreset"]]["exact"] == 1.0
    assert by_id[other["id"]]["score"] < by_id[store.id_of["errreset"]]["score"]


def test_memory_a_failing_rare_lane_costs_nothing(store, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("lane exploded")
    monkeypatch.setattr(tts, "exact_lane", boom)
    labels, hits = _ranked(store, "ERR_CONN_RESET and the sync worker")
    assert labels and "errreset" in labels
    assert all("exact" not in h for h in hits)


def test_memory_strict_context_search_has_the_lane_too(store):
    hits = engine.context_search("foo_bar.py", "o", "p", 3, now=NOW, semantic_enabled=False)
    assert [store.label_of[h["id"]] for h in hits][0] == "foobar"
    assert hits[0]["exact"] == 1.0


# --------------------------------------------------------------- experts ----


class ExpertVectors:
    """Fuzzy semantic lane over the expert chunks (hash-vector cosine)."""

    healthy = True

    def __init__(self, texts, preferred=()):
        self.texts = texts
        self.preferred = set(preferred)   # chunk ids the "embedder" likes best

    def add(self, *a, **k):
        pass

    def remove(self, *a, **k):
        pass

    def count(self):
        return len(self.texts)

    def search(self, query, k=8):
        qv = hash_embed.embed(query)
        scored = sorted(((1.0 if cid in self.preferred else
                          min(0.9, hash_embed.similarity(qv, hash_embed.embed(t)) * 1.6), cid)
                         for cid, t in self.texts.items()), key=lambda p: (-p[0], p[1]))
        return [{"memory_id": cid, "score": s} for s, cid in scored][:k]


DOCS = {
    "errors.md": "# Network\n\nThe worker reports ERR_CONN_RESET when the proxy closes the socket.\n\n"
                 "Connection resets are usually a proxy closing idle sockets; retry with backoff.\n",
    "files.md": "# Crash dumps\n\nCrash dumps are written to C:\\Users\\luis\\app.log by the installer.\n",
    "decoy.md": "# Logging\n\nThe app log rotates daily. The application log viewer parses every log line.\n\n"
                "Log level, log format, log rotation and app log retention are all configurable.\n",
    "release_a.md": "# Release\n\nRelease v0.35.1-rc2 fixed the sync worker and shipped to staging.\n",
    "release_b.md": "# Release\n\nRelease v0.36.0 fixed 35 bugs; the v0 branch was retired and the notes list 35 fixes.\n",
    "prose.md": "# Pacing\n\nPacing tightens when the verbs shorten.\n\nPoint of view must not drift in a scene.\n",
}


@pytest.fixture
def expert(tmp_path, monkeypatch):
    monkeypatch.setattr(experts, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(engine, "DATA_DIR", str(tmp_path))
    engine.set_vector_store(None)
    experts.reset_vector_stores()
    lanes = {}
    monkeypatch.setattr(experts, "vector_store", lambda slug: lanes.get(experts._clean_slug(slug)))
    made = experts.create_expert("Rare bench", description="bench", instructions="bench", rubric=["x"])
    slug = made["slug"]
    for name, body in DOCS.items():
        with open(os.path.join(experts.corpus_dir(slug), name), "w", encoding="utf-8") as fh:
            fh.write(body)
    experts.reindex(slug)
    chunks = experts.load_index(slug)
    yield SimpleNamespace(slug=slug, lanes=lanes, chunks=chunks,
                          texts={str(c["id"]): c.get("text") or "" for c in chunks})
    experts.reset_vector_stores()
    engine.reset_vector_store()


def _sources(result):
    return [h["source"] for h in result["hits"]]


def test_expert_baseline_tokenisation_really_misreads_these(expert):
    # `app.log` -> app, log ; `v0.35.1` -> v0, 35: the words of the decoys.
    assert experts._terms("app.log") == ["app", "log"]
    assert experts._terms("v0.35.1") == ["v0", "35"]


@pytest.mark.parametrize("query, first", [
    ("app.log", "files.md"),
    ("v0.35.1", "release_a.md"),
    ("what does ERR_CONN_RESET mean", "errors.md"),
])
def test_expert_exact_identifier_ranks_first_lexical_only(expert, query, first):
    result = experts.search(expert.slug, query, k=4, reranker=None)
    assert _sources(result)[0] == first
    assert result["tier"] == "lexical" and result["degraded"] is True
    assert result["exact_terms"]
    assert result["hits"][0]["score"] > experts.EXACT_FULL_BONUS - 0.01


def test_expert_exact_identifier_outranks_fuzzy_semantic_hits(expert):
    decoys = {str(c["id"]) for c in expert.chunks if c["source"] in ("decoy.md", "release_b.md")}
    expert.lanes[expert.slug] = ExpertVectors(expert.texts, preferred=decoys)
    for query, first in (("app.log", "files.md"), ("v0.35.1", "release_a.md")):
        result = experts.search(expert.slug, query, k=4, reranker=None)
        assert result["tier"] == "hybrid" and _sources(result)[0] == first, (query, _sources(result))
        scores = [h["score"] for h in result["hits"]]
        assert scores == sorted(scores, reverse=True)


def test_expert_exact_hit_survives_a_reranker_that_prefers_the_decoy(expert):
    def prefers_decoy(query, passages):
        order = sorted(range(len(passages)), key=lambda i: 0 if passages[i]["source"] == "decoy.md" else 1)
        return SimpleNamespace(reranked=True, order=order, scores=[1.0 - 0.1 * n for n in range(len(order))],
                               passages=[], reason=None)
    result = experts.search(expert.slug, "app.log", k=4, reranker=prefers_decoy)
    assert result["tier"] == "reranked" and result["rerank_reason"] is None
    assert _sources(result)[0] == "files.md"
    # ...and for a query with no identifier the reranker keeps the last word.
    prose = experts.search(expert.slug, "application log viewer", k=4, reranker=prefers_decoy)
    assert _sources(prose)[0] == "decoy.md" and "exact_terms" not in prose


def test_expert_prose_queries_are_unchanged(expert):
    for query in ("pacing and short verbs", "application log viewer rotation", "connection reset proxy"):
        result = experts.search(expert.slug, query, k=4, reranker=None)
        assert result["hits"] and "exact_terms" not in result
        lexical = experts.bm25_scores(query, [(c, t) for c, t in expert.texts.items()])
        scores = {h["chunk_id"]: h["score"] for h in result["hits"]}
        assert all(round(lexical[cid], 6) == value for cid, value in scores.items())


def test_expert_unknown_identifier_changes_nothing(expert):
    result = experts.search(expert.slug, "ERR_NEVER_SEEN_7", k=4, reranker=None)
    assert result["hits"] == [] and "exact_terms" not in result


def test_expert_a_failing_rare_lane_costs_nothing(expert, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("lane exploded")
    monkeypatch.setattr(tts, "exact_lane", boom)
    result = experts.search(expert.slug, "application log viewer app.log", k=3, reranker=None)
    assert result["hits"] and "exact_terms" not in result


