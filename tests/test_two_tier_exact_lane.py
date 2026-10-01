"""Rare-term lane of src.two_tier_search: identifiers matched verbatim."""

from src import two_tier_search as tts


def _corpus():
    docs = [
        {"id": "a", "text": "The key server restarted twice and keys were rotated on the server."},
        {"id": "b", "text": "Ticket KEY-12: the login page loops after a password reset."},
        {"id": "c", "text": "Keys and servers: a general note about key management on servers."},
        {"id": "d", "text": "Call model_server_check to see whether a local model server answers."},
        {"id": "e", "text": "A model can check a server by itself if it has a tool for that."},
        {"id": "f", "text": "Notes about the garden, tomatoes and the summer."},
    ]
    return docs


def test_rare_terms_picks_identifiers_only():
    terms = tts.rare_terms("what happened with KEY-12 and `model_server_check` in src/agent_loop.py, MCP?")
    assert terms == ["model_server_check", "KEY-12", "src/agent_loop.py", "MCP"]
    assert tts.rare_terms("what happened with the server yesterday") == []
    assert tts.rare_terms("e.g. the i.e. case") == []
    assert tts.rare_terms(None) == []


def test_rare_terms_numbers_camel_and_files():
    assert tts.rare_terms("build b10456 on 20260929 via getUserName in FAUSTUS.md") == [
        "b10456", "20260929", "getUserName", "FAUSTUS.md"]
    assert tts.rare_terms("año 12 y 345") == []


def test_exact_lane_puts_ticket_first():
    found = tts.search(_corpus(), "what is KEY-12 about", k=3)
    assert "exact" in found["lanes"]
    assert found["hits"][0]["id"] == "b"


def test_exact_lane_finds_tool_name_split_by_tokenizer():
    found = tts.search(_corpus(), "model_server_check", k=2)
    assert found["hits"][0]["id"] == "d"


def test_whole_token_match_only():
    docs = [{"id": "x", "text": "KEY-123 is a different ticket"},
            {"id": "y", "text": "nothing here"}]
    scores, kept, full = tts.exact_scores("KEY-12", [(d["id"], d["text"]) for d in docs])
    assert scores == {} and kept == [] and full == set()


def test_common_term_is_not_rare():
    docs = [{"id": str(i), "text": "MCP server number %d" % i} for i in range(6)]
    docs.append({"id": "z", "text": "MCP and KEY-7 together"})
    scores, kept, full = tts.exact_scores("MCP KEY-7", [(d["id"], d["text"]) for d in docs])
    assert kept == ["KEY-7"]
    assert full == {"z"}


def test_full_match_is_rescued_into_small_head():
    docs = [{"id": "n%d" % i, "text": "deploy the release to staging, deploy notes %s" % ("x" * i)}
            for i in range(12)]
    docs.append({"id": "t", "text": "unrelated text mentioning RLS-4471 once"})
    found = tts.search(docs, "deploy release staging RLS-4471", k=3)
    assert "t" in [h["id"] for h in found["hits"]]


def test_no_rare_terms_leaves_lanes_untouched():
    found = tts.search(_corpus(), "garden tomatoes summer", k=2)
    assert "exact" not in found["lanes"]
    assert found["hits"][0]["id"] == "f"


def test_result_keys_unchanged_with_exact_lane():
    found = tts.search(_corpus(), "KEY-12", k=2)
    assert set(found) == {"hits", "tier", "degraded", "elapsed_ms", "lanes"}


def test_exact_lane_survives_refined_tier():
    class Emb:
        def encode(self, texts):
            # An embedder that prefers prose about keys and servers.
            return [[1.0 if "server" in t.lower() else 0.1, 0.5] for t in texts]

    found = tts.search(_corpus(), "KEY-12 server", k=2, embedder=Emb())
    assert "b" in [h["id"] for h in found["hits"]]
