"""Query-aware page pruning (src/research_prune.py): block scoring and its
threshold, BM25 with heading weights, the character cap, title retention,
Spanish accents, the Markdown fallback and the fail-safe paths."""
from pathlib import Path

import pytest

from src.research_prune import (
    BM25,
    BLOCK_THRESHOLD,
    PruneConfig,
    fold,
    prune_page,
    tokenize,
)

FIXTURES = Path(__file__).parent / "fixtures" / "research_pages"


def _page(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _by_text(result, needle):
    return next(b for b in result.blocks if needle in b.text)


# ---------------------------------------------------------------------------
# Tokenisation
# ---------------------------------------------------------------------------

def test_fold_strips_accents_and_lowercases():
    assert fold("Clasificación ÁÉÍÓÚ Ñandú") == "clasificacion aeiou nandu"


def test_tokenize_folds_accents_and_drops_stopwords_in_both_languages():
    assert tokenize("La clasificación de los WAD") == ["clasificacion", "wad"]
    assert tokenize("The grading of the disorders") == ["grading", "disorder"]
    # an all-stopword query can still be tokenised when asked to keep them
    assert tokenize("the of", drop_stopwords=False) == ["the", "of"]
    assert tokenize("the of") == []


def test_tokenize_plural_s_matches_singular():
    assert tokenize("síntomas neurológicos") == tokenize("sintoma neurologico")
    assert tokenize("disorders") == tokenize("disorder")


# ---------------------------------------------------------------------------
# BM25
# ---------------------------------------------------------------------------

def test_bm25_ranks_more_matches_higher_and_uses_idf():
    docs = [
        ["token", "bucket", "burst"],
        ["token", "window"],
        ["cache", "window"],
        ["cache", "eviction"],
    ]
    bm = BM25(docs)
    terms = ["token", "bucket"]
    scores = [bm.score(i, terms) for i in range(4)]
    assert scores[0] > scores[1] > 0
    assert scores[2] == scores[3] == 0
    # a rarer term carries more weight than a common one
    assert bm.idf("bucket") > bm.idf("window")


def test_bm25_length_normalisation_prefers_shorter_doc():
    bm = BM25([["token"] + ["filler"] * 2, ["token"] + ["filler"] * 40, ["other"]])
    assert bm.score(0, ["token"]) > bm.score(1, ["token"])


def test_bm25_empty_collection_is_safe():
    bm = BM25([])
    assert bm.score(0, ["x"]) == 0.0


def test_heading_weights_h1_over_h2_over_h3_over_none():
    body = "<p>alpha " + "filler " * 12 + "</p><p>" + "other words " * 6 + "</p><p>" + "more words " * 6 + "</p>"

    def para_score(heading_html):
        html = f"<body><main>{heading_html}{body}</main></body>"
        res = prune_page("bucket alpha", html=html, config=PruneConfig(max_chars=200, min_keep_chars=0))
        return next(b for b in res.blocks if b.text.startswith("alpha filler")).bm25

    s1 = para_score("<h1>Bucket overview</h1>")
    s2 = para_score("<h2>Bucket overview</h2>")
    s3 = para_score("<h3>Bucket overview</h3>")
    s0 = para_score("<h2>Something unrelated</h2>")
    assert s1 > s2 > s3 > s0 > 0


def test_paragraph_under_a_matching_heading_outranks_the_same_text_elsewhere():
    same = "<p>" + "Shared sentence about limits. " * 4 + "</p>"
    html = ("<body><main><h2>Token bucket</h2>" + same + "<h2>Fixed window</h2>" + same + "</main></body>")
    res = prune_page("token bucket", html=html, config=PruneConfig(max_chars=150, min_keep_chars=0))
    paras = [b for b in res.blocks if not b.is_heading]
    assert paras[0].bm25 > paras[1].bm25 >= 0
    assert paras[0].selected and not paras[1].selected


# ---------------------------------------------------------------------------
# Block scoring and threshold
# ---------------------------------------------------------------------------

def test_scoring_keeps_article_blocks_and_drops_boilerplate():
    res = prune_page("clasificación de los WAD", html=_page("whiplash_es.html"))
    assert BLOCK_THRESHOLD == 0.48
    article = _by_text(res, "La clasificación más utilizada")
    assert article.score >= BLOCK_THRESHOLD and article.passed
    for needle in ("Utilizamos cookies", "Inicio", "Artículos relacionados", "Todos los derechos reservados",
                   "Muy buen resumen", "Publicidad: curso"):
        blk = _by_text(res, needle)
        assert blk.score < BLOCK_THRESHOLD and not blk.passed, needle
    # nothing from the boilerplate reaches the text the model would read
    for needle in ("cookies", "Suscríbete", "Política de privacidad", "Muy buen resumen"):
        assert needle not in res.text


def test_class_hint_alone_does_not_hide_a_content_wrapper():
    # a wrapper called has-sidebar must not sink the article inside entry-content
    html = ("<body class='has-sidebar'><div class='page has-sidebar'><div class='entry-content'>"
            "<p>" + "Real paragraph about retries and backoff with jitter. " * 4 + "</p></div></div></body>")
    res = prune_page("retries backoff", html=html)
    assert any(b.passed and "retries" in b.text for b in res.blocks)


def test_strong_negative_ancestor_beats_positive_child_class():
    html = ("<body><div class='comments'><div class='comment-content'>"
            "<p>" + "A long reader comment that keeps going and going. " * 5 + "</p></div></div>"
            "<article><p>" + "Actual article text about pricing tiers and billing. " * 5 + "</p></article></body>")
    res = prune_page("pricing", html=html)
    comment = _by_text(res, "reader comment")
    article = _by_text(res, "Actual article")
    assert comment.negative and not comment.passed
    assert article.passed and not article.negative


def test_threshold_is_configurable():
    html = _page("rate_limit_en.html")
    strict = prune_page("token bucket", html=html, config=PruneConfig(threshold=0.99))
    loose = prune_page("token bucket", html=html, config=PruneConfig(threshold=0.1))
    assert strict.blocks_passed < loose.blocks_passed
    # with a permissive threshold boilerplate marked negative is STILL held below it
    assert _by_text(loose, "stores cookies").negative


def test_long_cookie_notice_cannot_pass_on_text_density():
    notice = "We use cookies to improve your experience and to show you personalised advertising. " * 3
    html = f"<body><div class='cookie-banner'><p>{notice}</p></div><main><p>{'Body text about topics. ' * 20}</p></main></body>"
    res = prune_page("topics", html=html)
    assert not _by_text(res, "We use cookies").passed


def test_link_heavy_block_is_penalised():
    links = "".join(f"<li><a href='/x{i}'>Related reading number {i}</a></li>" for i in range(8))
    html = f"<body><div>{'<p>' + 'Prose without any links at all. ' * 6 + '</p>'}<ul>{links}</ul></div></body>"
    res = prune_page("prose", html=html)
    li = _by_text(res, "Related reading number 3")
    p = _by_text(res, "Prose without")
    assert li.link_density == 1.0
    assert li.score < BLOCK_THRESHOLD <= p.score


def test_hidden_and_script_content_is_ignored():
    html = ("<body><main><p style='display:none'>secret hidden text that must not appear</p>"
            "<script>var x = 'script text'</script><p>" + "Visible content sentence. " * 15 + "</p></main></body>")
    res = prune_page("visible", html=html)
    assert "secret hidden" not in res.text and "script text" not in res.text
    assert "Visible content" in res.text


# ---------------------------------------------------------------------------
# BM25 selection, char cap, title
# ---------------------------------------------------------------------------

def test_ranking_keeps_best_blocks_in_document_order_under_cap():
    res = prune_page("red flags referral", html=_page("whiplash_en.html"), config=PruneConfig(max_chars=700))
    assert res.pruned_chars <= 700
    assert "Red flags that call for urgent medical referral" in res.text
    # document order: a heading chain printed before the paragraph it governs
    assert res.text.index("## Clinical assessment") < res.text.index("Red flags that call")


def test_char_cap_is_respected_and_title_always_kept():
    res = prune_page("tratamiento ejercicio", html=_page("whiplash_es.html"), config=PruneConfig(max_chars=600))
    assert res.text.startswith("# Trastornos asociados al latigazo cervical")
    assert res.pruned_chars <= 600 + 10
    assert res.blocks_kept >= 1


def test_best_match_block_is_kept_even_when_larger_than_the_cap():
    big = "Quarterly metrics explain the churn pattern. " * 40
    html = f"<body><main><p>{big}</p><p>{'Something else entirely unrelated here. ' * 10}</p></main></body>"
    res = prune_page("churn", html=html, title="Report", config=PruneConfig(max_chars=300, min_keep_chars=0))
    assert res.text.startswith("# Report")
    assert "churn" in res.text
    assert res.pruned_chars <= 300 + 12
    assert res.blocks_kept == 1


def test_everything_that_passes_is_kept_when_it_already_fits():
    res = prune_page("token", html=_page("rate_limit_en.html"), config=PruneConfig(max_chars=6000))
    assert "Retries and backoff" in res.text and "Why limits exist" in res.text
    assert "Terms Privacy" not in res.text


def test_no_query_match_falls_back_to_leading_blocks_and_says_so():
    res = prune_page("zzzqqq", html=_page("rate_limit_en.html"), config=PruneConfig(max_chars=500, min_keep_chars=0))
    assert res.fallback == "no_query_match"
    assert res.top_bm25 == 0.0
    assert "A rate limit protects a service" in res.text


def test_spanish_accents_match_unaccented_query():
    with_accent = prune_page("clasificacion wad", html=_page("whiplash_es.html"), config=PruneConfig(max_chars=500))
    assert "grado II añade signos" in with_accent.text or "La clasificación más utilizada" in with_accent.text
    assert with_accent.top_bm25 > 0
    # and the other way round: an accented query against an unaccented page
    html = "<body><main><p>" + "La clasificacion de los grados es simple y util. " * 6 + "</p><p>" + "Otro tema distinto por completo aqui. " * 10 + "</p></main></body>"
    res = prune_page("clasificación", html=html, config=PruneConfig(max_chars=300, min_keep_chars=0))
    assert "clasificacion" in res.text


def test_trace_row_reports_chars_blocks_and_score():
    res = prune_page("token bucket", html=_page("rate_limit_en.html"), original_chars=9999)
    row = res.trace()
    assert row["original_chars"] == 9999
    assert row["pruned_chars"] == len(res.text)
    assert 0 < row["blocks_kept"] <= row["blocks_passed"] <= row["blocks_total"]
    assert row["top_bm25"] > 0
    assert row["mode"] == "html"


def test_fixture_pages_are_cut_substantially_with_noise_gone():
    for name, query in [("whiplash_es.html", "signos de alarma y derivación"),
                        ("whiplash_en.html", "exercise evidence neck"),
                        ("rate_limit_en.html", "exponential backoff jitter")]:
        html = _page(name)
        res = prune_page(query, html=html, config=PruneConfig(max_chars=900))
        assert res.pruned_chars < 0.5 * res.original_chars or res.pruned_chars <= 900, name
        assert res.top_bm25 > 0


# ---------------------------------------------------------------------------
# Markdown / plain text path and fail-safes
# ---------------------------------------------------------------------------

MARKDOWN = """# Guide title

[Home](/) [About](/about) [Login](/login)

We use cookies to improve your experience. Accept all cookies to continue.

## Retry policy

Clients should retry idempotent requests with exponential backoff and jitter. Retries stop after a bounded number of attempts so the service is not flooded again.

## Other topic

Completely different material about storage engines and their compaction schedules, with enough words to matter here.

- [Terms](/terms)
- [Privacy](/privacy)

All rights reserved.
"""


def test_markdown_text_is_pruned_without_markup():
    res = prune_page("retry backoff jitter", text=MARKDOWN, title="Guide", config=PruneConfig(max_chars=260, min_keep_chars=0))
    assert res.mode == "text"
    assert "exponential backoff" in res.text
    assert "cookies" not in res.text and "All rights reserved" not in res.text and "[Terms]" not in res.text
    assert _by_text(res, "We use cookies").negative


def test_thin_page_returns_original_text_instead_of_nothing():
    res = prune_page("anything", html="<body><nav>x</nav></body>", text="Just a short page with a little text.")
    assert res.text.strip() != ""
    assert res.fallback in ("unpruned", "relaxed") or "short page" in res.text


def test_empty_input_never_raises():
    res = prune_page("q")
    assert res.text == "" and res.fallback == "empty"
    assert prune_page("q", html="<<<not html", text="").text == ""


def test_js_shell_uses_extracted_text_when_dom_is_empty():
    html = "<body><div id='root'></div><script>render()</script></body>"
    res = prune_page("retry", html=html, text=MARKDOWN, config=PruneConfig(min_keep_chars=0))
    assert res.mode == "text" and "backoff" in res.text


def test_from_settings_clamps_bad_values():
    cfg = PruneConfig.from_settings({"research_prune_threshold": "oops", "research_prune_max_chars": 10})
    assert cfg.threshold == BLOCK_THRESHOLD
    assert cfg.max_chars == 500
    cfg = PruneConfig.from_settings({"research_prune_threshold": 7, "research_prune_max_chars": 999999})
    assert cfg.threshold == 1.0 and cfg.max_chars == 60000
    assert PruneConfig.from_settings({}, max_chars=1234).max_chars == 1234
