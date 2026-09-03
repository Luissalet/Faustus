"""The cross-encoder stage, and the promise that it is never silently absent.

Faustus could already recognise a reranker (``FAMILY_RERANK``) and already
refused to route chat to one, but nothing ever called one. These tests pin the
two halves of wiring that up:

* **the call itself** — head bounding, truncation, and one named reason for
  each way a reranker can let you down; and
* **the honesty of the stamp** — a result that says ``reranked`` was reranked,
  a result that was not says why, and a caller that never asked for reranking
  gets back byte-for-byte what it got before this module existed.

The last one is the compatibility guarantee, and it has its own tests at the
bottom because it is the property that makes the stage safe to switch on.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from src import rerank as rr
from src import two_tier_search
from src.two_tier_search import search


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class Endpoint:
    """Enough of a ``ModelEndpoint`` row for the real resolver to chew on."""

    def __init__(self, models, base_url="http://localhost:8080", ep_id="ep1",
                 api_key=None, hidden=()):
        self.id = ep_id
        self.name = "local"
        self.base_url = base_url
        self.api_key = api_key
        self.is_enabled = True
        self.provider_auth_id = None
        self.cached_models = json.dumps(list(models))
        self.pinned_models = None
        self.hidden_models = json.dumps(list(hidden))


class Response:
    def __init__(self, payload, status=200, raw=None):
        self._payload = payload
        self.status_code = status
        self._raw = raw

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError("boom", request=None, response=self)

    def json(self):
        if self._raw is not None:
            raise ValueError(self._raw)
        return self._payload


def install_endpoint(monkeypatch, models=("bge-reranker-v2-m3",), **kw):
    """Point discovery at one fake endpoint, leaving the resolver itself real."""
    monkeypatch.setattr(rr, "_settings_choice", lambda owner: ("", ""))
    monkeypatch.setattr(rr, "_endpoint_rows",
                        lambda owner, db: [Endpoint(models, **kw)])
    monkeypatch.setattr("core.database.SessionLocal", lambda: SimpleNamespace(
        query=lambda *a, **k: None, close=lambda: None))


def install_transport(monkeypatch, handler):
    """Replace httpx.Client so a test can answer (or refuse) the POST itself."""
    import httpx

    calls = []

    class Client:
        def __init__(self, *a, **kw):
            self.timeout = kw.get("timeout")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None, headers=None):
            calls.append({"url": url, "json": json, "headers": headers or {}})
            return handler(url, json, headers)

    monkeypatch.setattr(httpx, "Client", Client)
    return calls


def forbid_transport(monkeypatch):
    """Any HTTP client construction at all is a test failure."""
    import httpx

    def explode(*a, **kw):
        raise AssertionError("the reranker stage touched the network")

    monkeypatch.setattr(httpx, "Client", explode)


def scores_payload(pairs, key="relevance_score"):
    return {"model": "bge-reranker-v2-m3",
            "results": [{"index": i, key: s} for i, s in pairs]}


PASSAGES = [
    {"id": "a", "text": "how to run a shell command"},
    {"id": "b", "text": "the weather in Madrid"},
    {"id": "c", "text": "opening a terminal and running commands"},
]


# ---------------------------------------------------------------------------
# Nothing configured — the case every machine starts in
# ---------------------------------------------------------------------------


def test_with_no_reranker_it_says_so_and_never_opens_a_socket(monkeypatch):
    """The named-reason contract, and the reason it is safe to call always.

    A machine that has never pulled a cross-encoder must not pay a connection
    attempt per search to find that out again.
    """
    monkeypatch.setattr(rr, "_settings_choice", lambda owner: ("", ""))
    monkeypatch.setattr(rr, "_endpoint_rows", lambda owner, db: [])
    monkeypatch.setattr("core.database.SessionLocal", lambda: SimpleNamespace(
        query=lambda *a, **k: None, close=lambda: None))
    forbid_transport(monkeypatch)

    ok, why = rr.available()
    assert ok is False and why == rr.REASON_NO_RERANKER

    result = rr.rerank("shell command", PASSAGES)
    assert result.reranked is False
    assert result.reason == rr.REASON_NO_RERANKER
    assert result.degraded is True
    assert result.passages == PASSAGES, "the input order survives untouched"
    assert result.order == [0, 1, 2]
    assert result.scores == [None, None, None]


def test_an_endpoint_with_only_chat_models_is_not_a_reranker(monkeypatch):
    install_endpoint(monkeypatch, models=("qwen3-8b-instruct", "llama-3.1-8b"))
    forbid_transport(monkeypatch)
    assert rr.available() == (False, rr.REASON_NO_RERANKER)


def test_discovery_reuses_the_list_endpoint_resolver_already_maintains():
    """The markers are derived, not retyped, so the two cannot drift.

    A model this app refuses to route chat to *because it is a reranker* is
    exactly the model this module wants; if those two lists were separate
    copies, one of them would eventually be wrong.
    """
    from src.endpoint_resolver import _NON_CHAT_MODEL

    assert set(rr._rerank_markers()) == {m for m in _NON_CHAT_MODEL if "rerank" in m}
    assert rr._looks_like_reranker("BAAI/bge-reranker-v2-m3")
    assert rr._looks_like_reranker("jina-rerank-v2")
    assert not rr._looks_like_reranker("qwen3-8b-instruct")
    assert not rr._looks_like_reranker("text-embedding-3-small")


# ---------------------------------------------------------------------------
# A reranker that is there and works
# ---------------------------------------------------------------------------


def test_a_working_reranker_reorders_and_reports_its_scores(monkeypatch):
    install_endpoint(monkeypatch)
    install_transport(monkeypatch, lambda u, j, h: Response(
        scores_payload([(0, 0.4), (1, -2.0), (2, 0.9)])))

    result = rr.rerank("shell command", PASSAGES)
    assert result.reranked is True and result.reason is None
    assert [p["id"] for p in result.passages] == ["c", "a", "b"]
    assert result.order == [2, 0, 1]
    assert result.scores == [0.9, 0.4, -2.0]
    assert result.model == "bge-reranker-v2-m3"


def test_the_other_spelling_of_the_same_response_is_accepted(monkeypatch):
    """llama.cpp says ``results``/``relevance_score``; others say ``data``/``score``."""
    install_endpoint(monkeypatch)
    install_transport(monkeypatch, lambda u, j, h: Response(
        {"data": [{"index": 0, "score": 0.1}, {"index": 1, "score": 0.8}]}))
    assert [p["id"] for p in rr.rerank("q", PASSAGES[:2]).passages] == ["b", "a"]


def test_it_posts_to_v1_rerank_with_the_query_and_the_documents(monkeypatch):
    install_endpoint(monkeypatch)
    calls = install_transport(monkeypatch, lambda u, j, h: Response(
        scores_payload([(0, 1.0), (1, 0.5), (2, 0.2)])))

    rr.rerank("shell command", PASSAGES)
    assert len(calls) == 1
    assert calls[0]["url"] == "http://localhost:8080/v1/rerank"
    assert calls[0]["json"]["query"] == "shell command"
    assert calls[0]["json"]["model"] == "bge-reranker-v2-m3"
    assert calls[0]["json"]["documents"] == [p["text"] for p in PASSAGES]


def test_a_base_url_that_already_carries_v1_is_not_given_a_second_one(monkeypatch):
    install_endpoint(monkeypatch, base_url="http://localhost:8080/v1")
    calls = install_transport(monkeypatch, lambda u, j, h: Response(
        scores_payload([(0, 1.0)])))
    rr.rerank("q", PASSAGES[:1])
    assert calls[0]["url"] == "http://localhost:8080/v1/rerank"


def test_an_api_key_travels_on_the_usual_authorization_header(monkeypatch):
    install_endpoint(monkeypatch, api_key="sk-test")
    calls = install_transport(monkeypatch, lambda u, j, h: Response(
        scores_payload([(0, 1.0)])))
    rr.rerank("q", PASSAGES[:1])
    assert calls[0]["headers"]["Authorization"] == "Bearer sk-test"


def test_bare_strings_are_a_valid_passage_list(monkeypatch):
    install_endpoint(monkeypatch)
    calls = install_transport(monkeypatch, lambda u, j, h: Response(
        scores_payload([(0, 0.1), (1, 0.9)])))
    result = rr.rerank("q", ["first", "second"])
    assert calls[0]["json"]["documents"] == ["first", "second"]
    assert result.passages == ["second", "first"]


# ---------------------------------------------------------------------------
# Bounding the work
# ---------------------------------------------------------------------------


def test_only_the_head_is_sent_and_the_tail_keeps_its_place(monkeypatch):
    """A cross-encoder costs a forward pass per passage, so it sees the head.

    The tail is not dropped, though: reranking is a reordering, and a stage
    that silently truncated the result would be a filter wearing a sort's
    clothes.
    """
    install_endpoint(monkeypatch)
    corpus = [{"id": f"p{i}", "text": f"passage {i}"} for i in range(50)]
    # score the head in reverse so the reordering is unmistakable
    calls = install_transport(monkeypatch, lambda u, j, h: Response(
        scores_payload([(i, float(i)) for i in range(len(j["documents"]))])))

    result = rr.rerank("q", corpus, head=10)
    assert len(calls[0]["json"]["documents"]) == 10, "only the head crosses the wire"
    assert [p["id"] for p in result.passages[:10]] == [f"p{i}" for i in range(9, -1, -1)]
    assert [p["id"] for p in result.passages[10:]] == [f"p{i}" for i in range(10, 50)]
    assert len(result.passages) == 50, "nothing was dropped"


def test_the_default_head_is_the_module_constant(monkeypatch):
    install_endpoint(monkeypatch)
    corpus = [{"id": f"p{i}", "text": "x"} for i in range(200)]
    calls = install_transport(monkeypatch, lambda u, j, h: Response(
        scores_payload([(0, 1.0)])))
    rr.rerank("q", corpus)
    assert len(calls[0]["json"]["documents"]) == rr.RERANK_HEAD == 30


def test_a_huge_passage_is_truncated_before_it_is_uploaded(monkeypatch):
    install_endpoint(monkeypatch)
    calls = install_transport(monkeypatch, lambda u, j, h: Response(
        scores_payload([(0, 1.0)])))
    rr.rerank("q", [{"id": "big", "text": "x" * 500_000}])
    sent = calls[0]["json"]["documents"][0]
    assert len(sent) == rr.MAX_PASSAGE_CHARS < 500_000


def test_top_k_trims_the_answer_without_changing_the_order(monkeypatch):
    install_endpoint(monkeypatch)
    install_transport(monkeypatch, lambda u, j, h: Response(
        scores_payload([(0, 0.4), (1, -2.0), (2, 0.9)])))
    result = rr.rerank("q", PASSAGES, top_k=2)
    assert [p["id"] for p in result.passages] == ["c", "a"]
    assert result.order == [2, 0] and result.scores == [0.9, 0.4]


# ---------------------------------------------------------------------------
# Every way it can fail, and the name each one carries
# ---------------------------------------------------------------------------


def _refuses(*_a, **_k):
    import httpx

    raise httpx.ConnectError("connection refused")


def _times_out(*_a, **_k):
    import httpx

    raise httpx.ReadTimeout("too slow")


@pytest.mark.parametrize("handler,reason", [
    (_refuses, rr.REASON_UNREACHABLE),
    (_times_out, rr.REASON_TIMEOUT),
    (lambda u, j, h: Response(None, status=500), rr.REASON_UNREACHABLE),
    (lambda u, j, h: Response(None, status=404), rr.REASON_UNREACHABLE),
    (lambda u, j, h: Response(None, raw="not json"), rr.REASON_UNREACHABLE),
    (lambda u, j, h: Response({"nonsense": True}), rr.REASON_BAD_RESPONSE),
    (lambda u, j, h: Response({"results": "not a list"}), rr.REASON_BAD_RESPONSE),
    (lambda u, j, h: Response({"results": []}), rr.REASON_BAD_RESPONSE),
    (lambda u, j, h: Response(scores_payload([(99, 1.0)])), rr.REASON_BAD_RESPONSE),
])
def test_every_failure_is_a_named_reason_and_the_input_order(monkeypatch, handler, reason):
    """Never an exception, never a lie, never a reshuffle you cannot explain.

    The last case is a server answering about an index that was not in the
    batch: trusting it would reorder by a score that belongs to some other
    document, which is worse than not reranking.
    """
    install_endpoint(monkeypatch)
    install_transport(monkeypatch, handler)

    result = rr.rerank("shell command", PASSAGES)
    assert result.reranked is False
    assert result.reason == reason
    assert result.reason in rr.REASONS
    assert result.passages == PASSAGES
    assert result.order == [0, 1, 2]


def test_an_empty_or_blank_request_is_not_a_degradation(monkeypatch):
    """Nothing was withheld, so nothing is reported as missing."""
    forbid_transport(monkeypatch)
    for result in (rr.rerank("q", []), rr.rerank("   ", PASSAGES)):
        assert result.reranked is True and result.reason is None


def test_a_reranker_that_scores_only_some_passages_keeps_the_rest_in_place(monkeypatch):
    install_endpoint(monkeypatch)
    install_transport(monkeypatch, lambda u, j, h: Response(
        scores_payload([(2, 5.0)])))
    result = rr.rerank("q", PASSAGES)
    assert [p["id"] for p in result.passages] == ["c", "a", "b"]
    assert result.scores == [5.0, None, None]


# ---------------------------------------------------------------------------
# An admin's explicit choice
# ---------------------------------------------------------------------------


def test_a_pinned_endpoint_and_model_win_over_a_name_match(monkeypatch):
    monkeypatch.setattr(rr, "_settings_choice", lambda owner: ("ep2", "my-cross-encoder"))
    monkeypatch.setattr(rr, "_endpoint_rows", lambda owner, db: [
        Endpoint(("bge-reranker-v2-m3",), ep_id="ep1"),
        Endpoint(("my-cross-encoder",), base_url="http://box:9090", ep_id="ep2"),
    ])
    monkeypatch.setattr("core.database.SessionLocal", lambda: SimpleNamespace(
        query=lambda *a, **k: None, close=lambda: None))
    calls = install_transport(monkeypatch, lambda u, j, h: Response(
        scores_payload([(0, 1.0)])))

    rr.rerank("q", PASSAGES[:1])
    assert calls[0]["url"] == "http://box:9090/v1/rerank"
    assert calls[0]["json"]["model"] == "my-cross-encoder"


def test_a_model_the_admin_disabled_is_not_used(monkeypatch):
    install_endpoint(monkeypatch, models=("bge-reranker-v2-m3",),
                     hidden=("bge-reranker-v2-m3",))
    forbid_transport(monkeypatch)
    assert rr.available() == (False, rr.REASON_NO_RERANKER)


# ---------------------------------------------------------------------------
# Tier 3 in two_tier_search
# ---------------------------------------------------------------------------


TOOLS = [
    {"id": "bash", "text": "Tool: bash. Run a shell command on this machine."},
    {"id": "web_search", "text": "Tool: web_search. Search the web for news."},
    {"id": "glob", "text": "Tool: glob. Find files by pattern on disk."},
    {"id": "read_file", "text": "Tool: read_file. Read a file from disk."},
]


def by_key(key):
    """A stub cross-encoder that scores by a key the test controls.

    Deliberately not a scorer with an opinion about text: the point is that
    whatever it decides is what comes out, so the test proves the wiring
    rather than the model.
    """

    def call(query, passages):
        order = sorted(range(len(passages)),
                       key=lambda i: (-key(passages[i]), i))
        return rr.RerankResult(
            passages=[passages[i] for i in order], order=order,
            scores=[float(key(passages[i])) for i in order], reranked=True)

    return call


# Four documents every lane scores, so the reranker is handed all of them and
# the reordering under test is its own rather than the fusion's shortlist.
SHELL_DOCS = [
    {"id": "d1", "text": "shell command reference guide"},
    {"id": "d2", "text": "shell command examples for beginners"},
    {"id": "d3", "text": "shell command troubleshooting"},
    {"id": "d4", "text": "shell command history and aliases"},
]


def test_a_reranker_reorders_the_fused_result_and_stamps_the_tier():
    """The stamp must describe what happened, on the result and on every hit."""
    wanted = {"d3": 10.0, "d1": 5.0, "d4": 1.0, "d2": 0.5}
    fused = search(SHELL_DOCS, "shell command", k=4)
    found = search(SHELL_DOCS, "shell command", k=4,
                   reranker=by_key(lambda row: wanted[row["id"]]))

    assert found["tier"] == "reranked"
    assert found["rerank_reason"] is None
    assert "rerank" in found["lanes"]
    assert [h["id"] for h in found["hits"]] == ["d3", "d1", "d4", "d2"]
    assert all(h["tier"] == "reranked" for h in found["hits"])
    # the score reported is the one that produced the order, not the fused one
    assert [h["score"] for h in found["hits"]] == [10.0, 5.0, 1.0, 0.5]
    # …and it really is a different order from the one fusion produced
    assert [h["id"] for h in fused["hits"]] != [h["id"] for h in found["hits"]]


def test_without_the_reranker_the_same_query_ranks_differently():
    """Proof the reordering above was the reranker's doing and not the fusion's."""
    fused = search(TOOLS, "run a shell command", k=4)
    assert fused["hits"][0]["id"] == "bash"
    assert fused["tier"] == "hybrid"


@pytest.mark.parametrize("reason", [
    rr.REASON_NO_RERANKER, rr.REASON_UNREACHABLE, rr.REASON_TIMEOUT,
    rr.REASON_BAD_RESPONSE,
])
def test_an_unavailable_reranker_keeps_the_tier_and_carries_the_reason(reason):
    def unavailable(query, passages):
        return rr.RerankResult(passages=list(passages),
                               order=list(range(len(passages))),
                               scores=[None] * len(passages),
                               reranked=False, reason=reason)

    plain = search(TOOLS, "run a shell command", k=4)
    found = search(TOOLS, "run a shell command", k=4, reranker=unavailable)

    assert found["tier"] == plain["tier"] == "hybrid"
    assert found["rerank_reason"] == reason
    assert "rerank" not in found["lanes"]
    assert [h["id"] for h in found["hits"]] == [h["id"] for h in plain["hits"]]
    assert [h["score"] for h in found["hits"]] == [h["score"] for h in plain["hits"]]


def test_a_reranker_that_raises_is_a_degradation_not_a_500():
    def boom(query, passages):
        raise RuntimeError("the cross-encoder fell over")

    found = search(TOOLS, "run a shell command", k=4, reranker=boom)
    assert found["tier"] == "hybrid"
    assert found["rerank_reason"] == rr.REASON_UNREACHABLE
    assert found["hits"], "search still answered"


def test_the_reranker_sees_more_candidates_than_k():
    """The whole value of the stage is promoting something k would have cut."""
    seen = {}

    def watcher(query, passages):
        seen["n"] = len(passages)
        return rr.RerankResult(passages=list(passages),
                               order=list(range(len(passages))),
                               scores=[1.0] * len(passages), reranked=True)

    corpus = [{"id": f"d{i}", "text": f"shell command number {i}"} for i in range(40)]
    search(corpus, "shell command", k=3, reranker=watcher)
    assert seen["n"] == two_tier_search.RERANK_HEAD == 30


def test_the_rerank_head_is_configurable_per_call():
    seen = {}

    def watcher(query, passages):
        seen["n"] = len(passages)
        return rr.RerankResult(passages=list(passages),
                               order=list(range(len(passages))),
                               scores=[1.0] * len(passages), reranked=True)

    corpus = [{"id": f"d{i}", "text": f"shell command number {i}"} for i in range(40)]
    search(corpus, "shell command", k=3, reranker=watcher, rerank_head=5)
    assert seen["n"] == 5


def test_reranking_reorders_but_never_loses_a_document():
    """Tier 3 is a sort, not a filter."""
    corpus = [{"id": f"d{i}", "text": f"shell command number {i}"} for i in range(40)]
    plain = search(corpus, "shell command", k=40)
    found = search(corpus, "shell command", k=40,
                   reranker=by_key(lambda row: -int(row["id"][1:])))
    assert {h["id"] for h in found["hits"]} == {h["id"] for h in plain["hits"]}


def test_an_empty_corpus_asked_to_rerank_reports_nothing_withheld():
    found = search([], "anything", reranker=by_key(lambda row: 0.0))
    assert found["hits"] == [] and found["rerank_reason"] is None
    assert found["tier"] == "lexical"


def test_true_means_the_configured_cross_encoder(monkeypatch):
    """``reranker=True`` is the shipped path — resolver, HTTP and all."""
    install_endpoint(monkeypatch)
    install_transport(monkeypatch, lambda u, j, h: Response(
        scores_payload([(i, float(i)) for i in range(len(j["documents"]))])))

    found = search(TOOLS, "run a shell command", k=4, reranker=True)
    assert found["tier"] == "reranked" and found["rerank_reason"] is None
    assert found["hits"][0]["id"] != "bash", "the stub inverted the fused order"


def test_true_with_nothing_configured_degrades_with_the_named_reason(monkeypatch):
    monkeypatch.setattr(rr, "_settings_choice", lambda owner: ("", ""))
    monkeypatch.setattr(rr, "_endpoint_rows", lambda owner, db: [])
    monkeypatch.setattr("core.database.SessionLocal", lambda: SimpleNamespace(
        query=lambda *a, **k: None, close=lambda: None))
    forbid_transport(monkeypatch)

    found = search(TOOLS, "run a shell command", k=4, reranker=True)
    assert found["tier"] == "hybrid"
    assert found["rerank_reason"] == rr.REASON_NO_RERANKER


# ---------------------------------------------------------------------------
# The compatibility guarantee
# ---------------------------------------------------------------------------


def test_a_caller_that_does_not_ask_for_reranking_gets_the_old_answer_exactly():
    """The dict existing callers already destructure must not gain a key.

    ``src/history_import.py`` and ``src/tool_index.py`` both call ``search()``
    without a reranker; if switching this feature on changed their answer,
    tier 3 would not be an addition, it would be a migration.
    """
    found = search(TOOLS, "run a shell command", k=4)
    assert set(found) == {"hits", "tier", "degraded", "elapsed_ms", "lanes"}
    assert "rerank_reason" not in found
    assert "rerank" not in found["lanes"]
    assert set(found["hits"][0]) == {"id", "text", "score", "rank", "tier"}


def test_the_existing_tool_search_path_is_untouched_by_this_module():
    """``ToolIndex.retrieve`` with no embedder — the 0-to-16 lexical floor."""
    from src.tool_index import ToolIndex

    index = ToolIndex.__new__(ToolIndex)
    index._lanes = []
    assert index.retrieve("take a screenshot of my screen", k=5)[0] == "desktop_screenshot"
    assert "bash" in index.retrieve("run a shell command", k=8)


def test_the_hash_lane_weight_is_left_exactly_where_the_measurement_put_it():
    """Whether a reranker makes the 0.5 unnecessary is a later measurement.

    Changing it here would silently re-open the failure the docstring's table
    records, and would do it under cover of an unrelated feature.
    """
    assert two_tier_search.HASH_WEIGHT == 0.5
    assert two_tier_search.BM25_WEIGHT == 1.0
    assert two_tier_search.RRF_K == 60.0
    assert two_tier_search.ALPHA == 0.7


# ---------------------------------------------------------------------------
# Tier 3 in the expert corpus search
# ---------------------------------------------------------------------------


@pytest.fixture()
def expert(tmp_path, monkeypatch):
    from services import experts
    from src import memory_engine

    monkeypatch.setattr(experts, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(memory_engine, "DATA_DIR", str(tmp_path))
    memory_engine.set_vector_store(None)
    experts.reset_vector_stores()
    monkeypatch.setattr(experts, "vector_store", lambda slug: None)

    made = experts.create_expert("Corrector", description="ritmo y diálogo",
                                 instructions="No toques la voz del autor.")
    slug = made["slug"]
    with open(os.path.join(experts.corpus_dir(slug), "notes.md"), "w",
              encoding="utf-8") as fh:
        for paragraph in ("Rhythm is the first thing a reader feels.",
                          "Dialogue must carry the weight of the scene.",
                          "Pacing tightens when the verbs shorten.",
                          "Point of view must not drift midscene."):
            # padded so the splitter yields several chunks — a one-chunk
            # corpus cannot demonstrate a reordering
            fh.write(paragraph + " " + ("filler prose about craft. " * 120) + "\n\n")
    experts.reindex(slug)
    yield SimpleNamespace(slug=slug, module=experts)
    experts.reset_vector_stores()
    memory_engine.reset_vector_store()


def test_the_expert_search_stamps_reranked_only_when_it_was(expert):
    experts = expert.module
    # The last chunk the *fusion* ranked, not the last in the index: a chunk
    # BM25 never scored is not a candidate, so promoting it would prove
    # nothing about the wiring.
    plain = experts.search(expert.slug, "rhythm dialogue", k=50, reranker=None)
    assert len(plain["hits"]) > 1, "the fixture needs something to reorder"

    last = plain["hits"][-1]["chunk_id"]

    def to_the_top(query, passages):
        order = sorted(range(len(passages)),
                       key=lambda i: (passages[i]["id"] != last, i))
        return rr.RerankResult(passages=[passages[i] for i in order], order=order,
                               scores=[9.0 if passages[i]["id"] == last else 0.1
                                       for i in order], reranked=True)

    found = experts.search(expert.slug, "rhythm dialogue", k=5, reranker=to_the_top)
    assert found["tier"] == "reranked" and found["rerank_reason"] is None
    assert found["hits"][0]["chunk_id"] == last
    assert all(h["tier"] == "reranked" for h in found["hits"])


def test_the_expert_search_with_no_reranker_answers_exactly_as_before(expert):
    """The compatibility guarantee on the second search path."""
    experts = expert.module
    plain = experts.search(expert.slug, "rhythm dialogue", k=5, reranker=None)

    assert plain["tier"] == "lexical" and plain["degraded"] is True
    assert plain["rerank_reason"] == rr.REASON_NO_RERANKER
    assert set(plain["hits"][0]) == {"chunk_id", "source", "page", "start_line",
                                     "end_line", "text", "score", "tier"}
    assert all(h["tier"] == "lexical" for h in plain["hits"])


def test_the_expert_search_default_degrades_when_nothing_is_configured(expert, monkeypatch):
    """The shipped default is ``reranker=True``; with no endpoint it is honest."""
    experts = expert.module
    monkeypatch.setattr(rr, "_settings_choice", lambda owner: ("", ""))
    monkeypatch.setattr(rr, "_endpoint_rows", lambda owner, db: [])
    monkeypatch.setattr("core.database.SessionLocal", lambda: SimpleNamespace(
        query=lambda *a, **k: None, close=lambda: None))
    forbid_transport(monkeypatch)

    found = experts.search(expert.slug, "rhythm dialogue", k=5)
    assert found["tier"] == "lexical"
    assert found["rerank_reason"] == rr.REASON_NO_RERANKER
    assert found["hits"], "the search still answered"


def test_an_expert_reranker_that_raises_does_not_cost_the_turn(expert):
    experts = expert.module

    def boom(query, passages):
        raise RuntimeError("cross-encoder down")

    found = experts.search(expert.slug, "rhythm dialogue", k=5, reranker=boom)
    assert found["tier"] == "lexical"
    assert found["rerank_reason"] == rr.REASON_UNREACHABLE
    assert found["hits"]


def test_the_empty_expert_answer_is_unchanged_shape(expert):
    """The no-such-expert reply is a non-answer; it has nothing to say about
    reranking and must keep the exact shape its caller already asserts."""
    experts = expert.module
    assert experts.search("no-such-expert", "anything") == {
        "hits": [], "tier": "lexical", "degraded": False}
