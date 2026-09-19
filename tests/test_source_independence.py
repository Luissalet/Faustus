"""Tests for src.source_independence: clustering cited sources that are
actually the same underlying piece of reporting."""

from __future__ import annotations

from src.source_independence import (
    cluster_sources,
    independence_legend_line,
    independence_summary,
    is_near_duplicate,
    wire_markers,
)

# A long enough body (>= 80 words) to qualify for near-duplicate comparison,
# repeated verbatim by "mirrors" below.
_LONG_TEXT = (
    "The central bank raised its benchmark rate by a quarter point on "
    "Wednesday, citing persistent inflation pressure across the services "
    "sector and a labour market that has not cooled as quickly as officials "
    "had expected earlier in the year. Policymakers signalled that further "
    "increases remain possible if price growth does not slow in the coming "
    "months, though several members of the committee argued for a pause "
    "given early signs of softening demand in manufacturing and housing. "
    "The decision was not unanimous, and minutes released alongside the "
    "statement showed a split over how much further tightening is needed "
    "before the committee can be confident inflation is returning to target."
)

_UNRELATED_TEXT = (
    "The city council approved a new zoning plan for the waterfront "
    "district on Thursday, clearing the way for a mixed-use development "
    "that has been debated for nearly three years. Supporters say the "
    "project will bring badly needed housing and retail space, while "
    "opponents argue it will strain local infrastructure and change the "
    "character of a historic neighbourhood that has resisted large-scale "
    "redevelopment for decades despite repeated proposals from different "
    "developers over that period, none of which secured enough support "
    "on the council to move forward until this latest revised plan."
)


def _src(n, url, text="", title="t"):
    return {"n": n, "url": url, "title": title, "text": text}


# ---------------------------------------------------------------------------
# wire_markers
# ---------------------------------------------------------------------------


def test_wire_marker_attribution_form_detected():
    assert "Reuters" in wire_markers("LONDON (Reuters) - Markets rose on Friday.")
    assert "AP" in wire_markers("NEW YORK -- AP -- Stocks climbed.")
    assert "EFE" in wire_markers("MADRID (EFE).- El paro bajó en agosto.")


def test_wire_marker_mere_mention_not_detected():
    text = ("This roundup discusses how Reuters and other outlets covered "
            "the story earlier in the week, without quoting either directly.")
    assert wire_markers(text) == []


def test_wire_marker_only_reads_opening_window():
    filler = "x " * 400  # pushes an attribution past the 600-char window
    text = filler + "(Reuters) - late breaking marker"
    assert wire_markers(text) == []


# ---------------------------------------------------------------------------
# is_near_duplicate
# ---------------------------------------------------------------------------


def test_near_duplicate_true_for_same_text():
    assert is_near_duplicate(_LONG_TEXT, _LONG_TEXT) is True


def test_near_duplicate_false_for_unrelated_text():
    assert is_near_duplicate(_LONG_TEXT, _UNRELATED_TEXT) is False


def test_near_duplicate_false_for_short_text():
    short_a = "A short update with very little detail here today."
    short_b = "A short update with very little detail here today."
    assert is_near_duplicate(short_a, short_b) is False


# ---------------------------------------------------------------------------
# cluster_sources
# ---------------------------------------------------------------------------


def test_cluster_same_url_with_utm_differences():
    sources = [
        _src(1, "https://example.com/story?utm_source=twitter&utm_medium=social"),
        _src(2, "https://example.com/story"),
    ]
    clusters = cluster_sources(sources)
    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster.members == (1, 2)
    assert cluster.representative == 1
    assert cluster.reason == "same_url"
    assert cluster.weight == 0.5


def test_www_prefix_is_not_folded_by_canonical_url():
    # canonical_url deliberately keeps www. distinct (research_citations'
    # own docstring: some hosts serve different content there). Two pages on
    # different hosts with unrelated text stay separate sources.
    sources = [
        _src(1, "https://www.example.com/story", text=_UNRELATED_TEXT),
        _src(2, "https://example.com/story", text=_LONG_TEXT),
    ]
    clusters = cluster_sources(sources)
    assert len(clusters) == 2


def test_two_reuters_copies_on_different_domains_cluster_as_wire():
    text_a = "LONDON (Reuters) - " + _LONG_TEXT
    text_b = "LONDON (Reuters) - " + _LONG_TEXT
    sources = [
        _src(3, "https://siteA.example/markets-story", text=text_a),
        _src(7, "https://siteB.example/markets-story-mirror", text=text_b),
    ]
    clusters = cluster_sources(sources)
    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster.members == (3, 7)
    assert cluster.reason == "wire:Reuters"


def test_page_merely_mentioning_reuters_is_not_clustered_with_a_real_copy():
    wire_text = "LONDON (Reuters) - " + _LONG_TEXT
    mention_text = ("An analysis piece that references how Reuters framed the "
                     "story, but with entirely different reporting: ") + _UNRELATED_TEXT
    sources = [
        _src(1, "https://siteA.example/wire-copy", text=wire_text),
        _src(2, "https://siteB.example/analysis", text=mention_text),
    ]
    clusters = cluster_sources(sources)
    assert len(clusters) == 2
    assert {c.reason for c in clusters} == {""}


def test_near_duplicate_mirror_clustered_without_wire_marker():
    sources = [
        _src(4, "https://mirror-one.example/piece", text=_LONG_TEXT),
        _src(9, "https://mirror-two.example/piece-copy", text=_LONG_TEXT),
    ]
    clusters = cluster_sources(sources)
    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster.members == (4, 9)
    assert cluster.reason == "near_duplicate"


def test_short_texts_not_clustered_by_near_duplicate_rule():
    short = "Breaking: markets moved today on light volume across sectors."
    sources = [
        _src(1, "https://siteA.example/a", text=short),
        _src(2, "https://siteB.example/b", text=short),
    ]
    clusters = cluster_sources(sources)
    assert len(clusters) == 2
    assert all(c.reason == "" for c in clusters)


def test_stable_deterministic_ordering():
    sources = [
        _src(5, "https://example.com/x?utm_source=a"),
        _src(1, "https://other.example/y", text=_UNRELATED_TEXT),
        _src(2, "https://example.com/x"),
    ]
    clusters_first = cluster_sources(sources)
    clusters_second = cluster_sources(list(reversed(sources)))
    as_tuples = lambda clusters: [(c.representative, c.members, c.reason) for c in clusters]
    assert as_tuples(clusters_first) == as_tuples(clusters_second)
    # Sorted by representative (lowest member number).
    reps = [c.representative for c in clusters_first]
    assert reps == sorted(reps)


def test_non_dict_and_zero_number_sources_are_skipped():
    sources = [None, {"n": 0, "url": "https://x.example/"}, _src(1, "https://y.example/")]
    clusters = cluster_sources(sources)
    assert [c.members for c in clusters] == [(1,)]


# ---------------------------------------------------------------------------
# independence_summary
# ---------------------------------------------------------------------------


def test_independence_summary_counts():
    sources = [
        _src(1, "https://example.com/x?utm_source=a"),
        _src(2, "https://example.com/x"),
        _src(3, "https://other.example/y", text=_UNRELATED_TEXT),
    ]
    summary = independence_summary(sources, cited_numbers=[1, 2, 3])
    assert summary["total_cited"] == 3
    assert summary["independent_cited"] == 2  # {1,2} collapse, 3 stands alone
    assert len(summary["clusters"]) == 1
    assert summary["clusters"][0]["members"] == [1, 2]
    assert summary["clusters"][0]["reason"] == "same_url"


def test_independence_summary_all_independent_has_no_named_clusters():
    sources = [
        _src(1, "https://a.example/", text=_LONG_TEXT),
        _src(2, "https://b.example/", text=_UNRELATED_TEXT),
    ]
    summary = independence_summary(sources, cited_numbers=[1, 2])
    assert summary["total_cited"] == 2
    assert summary["independent_cited"] == 2
    assert summary["clusters"] == []


def test_independence_summary_ignores_uncited_duplicate_members():
    sources = [
        _src(1, "https://example.com/x?utm_source=a"),
        _src(2, "https://example.com/x"),
        _src(3, "https://other.example/y", text=_UNRELATED_TEXT),
    ]
    # Only source 1 of the duplicate pair is actually cited; the cluster
    # still exists, but it has only one CITED member, so it is not "named".
    summary = independence_summary(sources, cited_numbers=[1, 3])
    assert summary["total_cited"] == 2
    assert summary["independent_cited"] == 2
    assert summary["clusters"] == []


def test_independence_summary_ignores_duplicate_and_invalid_cited_numbers():
    sources = [_src(1, "https://a.example/", text=_LONG_TEXT)]
    summary = independence_summary(sources, cited_numbers=[1, 1, "not-a-number", 0, None])
    assert summary["total_cited"] == 1
    assert summary["independent_cited"] == 1


# ---------------------------------------------------------------------------
# independence_legend_line
# ---------------------------------------------------------------------------


def test_legend_line_empty_when_no_clusters():
    assert independence_legend_line({"clusters": []}, "en") == ""
    assert independence_legend_line({}, "en") == ""


def test_legend_line_wire_english():
    summary = {
        "total_cited": 9,
        "independent_cited": 7,
        "clusters": [{"representative": 2, "members": [2, 5, 7], "reason": "wire:Reuters"}],
    }
    line = independence_legend_line(summary, "en")
    assert "[2]" in line and "[5]" in line and "[7]" in line
    assert "Reuters" in line
    assert "9 cited, 7 independent." in line
    assert line.startswith("Sources")


def test_legend_line_same_url_spanish():
    summary = {
        "total_cited": 4,
        "independent_cited": 3,
        "clusters": [{"representative": 1, "members": [1, 3], "reason": "same_url"}],
    }
    line = independence_legend_line(summary, "es")
    assert "[1]" in line and "[3]" in line
    assert "4 citadas, 3 independientes." in line
    assert line.startswith("Las fuentes")


def test_legend_line_multiple_clusters_joined():
    summary = {
        "total_cited": 6,
        "independent_cited": 3,
        "clusters": [
            {"representative": 1, "members": [1, 2], "reason": "same_url"},
            {"representative": 4, "members": [4, 5, 6], "reason": "near_duplicate"},
        ],
    }
    line = independence_legend_line(summary, "en")
    assert line.count(";") == 1
    assert "6 cited, 3 independent." in line
