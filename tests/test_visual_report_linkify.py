"""Bare-URL autolinking in research reports must respect bracket depth.

`_autolink_urls` used to scan a bare URL up to the first character in a
blacklist, which cut `http://[::1]:8080/x` and `.../Foo_(bar)` short and pulled
the full stop that ends a sentence into the href. It also rewrote URLs inside
code fences, inline code spans and raw `href="..."` attributes, where a URL is
content rather than a link to make.
"""
from typing import List, Tuple

import pytest
from bs4 import BeautifulSoup

from src.visual_report import _autolink_urls, _md_to_html


def links(md_text: str) -> List[Tuple[str, str]]:
    """(href, visible text) of every anchor the report renderer emits."""
    soup = BeautifulSoup(_md_to_html(md_text), "html.parser")
    return [(a.get("href", ""), a.get_text()) for a in soup.find_all("a")]


# ── bracket and paren depth ────────────────────────────────────────────────

def test_ipv6_literal_host_survives_whole():
    url = "http://[::1]:8080/path"
    assert links(f"Try {url} now") == [(url, url)]


def test_ipv6_literal_alone_on_a_line_survives_whole():
    url = "http://[::1]:8080/path"
    assert links(url) == [(url, url)]


def test_wikipedia_style_url_keeps_its_closing_paren():
    url = "https://en.wikipedia.org/wiki/Foo_(bar)"
    assert links(f"See {url} for more") == [(url, url)]


def test_balanced_bracket_in_path_is_kept():
    url = "https://example.com/a[0]"
    assert links(f"Try {url} now") == [(url, url)]


def test_nested_parens_are_balanced_not_truncated():
    url = "https://example.com/a(b(c))d"
    assert links(url) == [(url, url)]


# ── punctuation that belongs to the sentence, not the URL ──────────────────

def test_trailing_full_stop_stays_outside_the_link():
    assert links("See https://example.com/x.") == [
        ("https://example.com/x", "https://example.com/x")
    ]


@pytest.mark.parametrize("punct", [".", ",", ";", ":", "!", "?", "?!", "..."])
def test_trailing_sentence_punctuation_is_trimmed(punct):
    url = "https://example.com/x"
    assert links(f"See {url}{punct}") == [(url, url)]


def test_unopened_closing_paren_stays_outside_the_link():
    url = "https://example.com/x"
    assert links(f"(see {url})") == [(url, url)]


def test_unopened_closing_bracket_stays_outside_the_link():
    url = "https://example.com/x"
    assert links(f"[see {url}]") == [(url, url)]


def test_balanced_paren_followed_by_a_full_stop_keeps_the_paren():
    url = "https://en.wikipedia.org/wiki/Foo_(bar)"
    assert links(f"See {url}.") == [(url, url)]


# ── places a URL must be left alone ────────────────────────────────────────

def test_existing_markdown_link_is_not_double_linked():
    assert links("[text](https://example.com/y)") == [("https://example.com/y", "text")]


def test_markdown_image_source_is_not_linkified():
    out = _autolink_urls("![alt](https://example.com/i.png)")
    assert out == "![alt](https://example.com/i.png)"


def test_raw_href_attribute_is_not_linkified():
    md = '<a href="https://example.com/z">z</a>'
    assert _autolink_urls(md) == md


def test_url_inside_a_code_fence_is_not_linkified():
    md = "```\nhttps://example.com/in-fence\n```"
    assert _autolink_urls(md) == md
    html = _md_to_html(md)
    assert "https://example.com/in-fence" in html
    assert "<a " not in html


def test_url_inside_a_tilde_code_fence_is_not_linkified():
    md = "~~~\nhttps://example.com/in-fence\n~~~"
    assert _autolink_urls(md) == md


def test_url_inside_an_inline_code_span_is_not_linkified():
    md = "Inline `https://example.com/code` span"
    assert _autolink_urls(md) == md
    assert links(md) == []


def test_angle_bracket_autolink_is_not_double_linked():
    md = "<https://example.com/angle>"
    assert _autolink_urls(md) == md
    assert links(md) == [("https://example.com/angle", "https://example.com/angle")]


def test_link_reference_definition_is_not_linkified():
    md = "[ref]: https://example.com/ref\n\nSee [ref].\n"
    assert _autolink_urls(md) == md


# ── the ordinary case still works ──────────────────────────────────────────

def test_plain_bare_url_is_still_linkified():
    url = "https://example.com/plain"
    assert links(f"Read {url} today") == [(url, url)]


def test_two_bare_urls_on_one_line_are_both_linkified():
    a, b = "http://[::1]:8080/a", "http://[::1]:8080/b"
    assert links(f"Both {a} and {b} here") == [(a, a), (b, b)]


def test_fence_is_skipped_but_surrounding_prose_is_still_linkified():
    md = "Before https://example.com/a\n\n```\nhttps://example.com/fenced\n```\n\nAfter https://example.com/b"
    assert links(md) == [
        ("https://example.com/a", "https://example.com/a"),
        ("https://example.com/b", "https://example.com/b"),
    ]


def test_non_string_input_is_returned_unchanged():
    assert _autolink_urls(None) is None
