"""Tests for `src/brain/wikilinks.py`: link/embed parsing, tag extraction and
target rewriting (the mechanism `notes.rename_note` uses to keep every other
note's links pointing at a renamed file)."""

from __future__ import annotations

from src.brain import wikilinks as wl


def test_parse_plain_link():
    links = wl.parse("See [[Ada]] for details.")
    assert links == [{"target": "Ada", "label": "", "heading": "", "is_embed": False}]


def test_parse_piped_label():
    links = wl.parse("Talk to [[Bruno|the boss]].")
    assert links == [{"target": "Bruno", "label": "the boss", "heading": "", "is_embed": False}]


def test_parse_heading_link():
    links = wl.parse("See [[Cordera Labs#history]].")
    assert links[0]["target"] == "Cordera Labs"
    assert links[0]["heading"] == "history"
    assert links[0]["label"] == ""


def test_parse_heading_and_label_together():
    links = wl.parse("See [[Cordera Labs#history|their story]].")
    assert links[0] == {
        "target": "Cordera Labs", "label": "their story", "heading": "history", "is_embed": False,
    }


def test_parse_embed():
    links = wl.parse("![[diagram.md]]")
    assert links[0]["is_embed"] is True
    assert links[0]["target"] == "diagram.md"


def test_parse_multiple_links_in_order():
    links = wl.parse("[[A]] then [[B|b]] then [[C#h]]")
    assert [l["target"] for l in links] == ["A", "B", "C"]


def test_parse_ignores_plain_brackets():
    assert wl.parse("This [is] not a link, [also not].") == []


def test_tags_inline_hash_words():
    assert wl.tags("Работа on #proyecto-x and #urgente today") == ["proyecto-x", "urgente"]


def test_tags_ignores_atx_headings():
    body = "# Title\n\n## Subtitle\n\nBody with a real #tag here."
    assert wl.tags(body) == ["tag"]


def test_tags_merges_frontmatter_tags_deduped():
    body = "Body with #inline tag."
    fm = {"tags": ["Inline", "extra"]}
    # frontmatter tags are taken as-is (case preserved) and inline tags too;
    # dedup only collapses exact duplicates, so "Inline" and "inline" both survive.
    assert set(wl.tags(body, fm)) == {"inline", "Inline", "extra"}


def test_tags_empty_body_and_no_frontmatter():
    assert wl.tags("") == []
    assert wl.tags("no tags here") == []


def test_rewrite_target_plain_link():
    assert wl.rewrite_target("See [[Ada]] now.", "Ada", "Ada Lovelace") == "See [[Ada Lovelace]] now."


def test_rewrite_target_preserves_label():
    body = "Talk to [[Bruno|the boss]]."
    assert wl.rewrite_target(body, "Bruno", "Bruno Diaz") == "Talk to [[Bruno Diaz|the boss]]."


def test_rewrite_target_preserves_heading_and_label():
    body = "[[Cordera Labs#history|their story]]"
    out = wl.rewrite_target(body, "Cordera Labs", "Cordera Labs Inc")
    assert out == "[[Cordera Labs Inc#history|their story]]"


def test_rewrite_target_preserves_embed_flag():
    assert wl.rewrite_target("![[old]]", "old", "new") == "![[new]]"


def test_rewrite_target_only_touches_exact_matches():
    body = "[[Ada]] and [[Adam]] are different people."
    out = wl.rewrite_target(body, "Ada", "Ada Lovelace")
    assert out == "[[Ada Lovelace]] and [[Adam]] are different people."


def test_rewrite_target_noop_when_old_equals_new_or_missing():
    body = "[[Ada]] text"
    assert wl.rewrite_target(body, "Ada", "Ada") == body
    assert wl.rewrite_target(body, "", "New") == body


def test_rewrite_target_multiple_occurrences():
    body = "[[Ada]] met [[Ada]] again."
    assert wl.rewrite_target(body, "Ada", "Ada L.") == "[[Ada L.]] met [[Ada L.]] again."
