"""Channel detection (R1) — pure function, no network."""
from __future__ import annotations

import pytest

from src.reach.router import CHANNELS, detect_channel


@pytest.mark.parametrize("url,expected", [
    ("https://www.youtube.com/watch?v=abc123", "youtube"),
    ("https://youtu.be/abc123", "youtube"),
    ("https://music.youtube.com/watch?v=abc", "youtube"),
    ("https://x.com/foo/status/123", "x"),
    ("https://twitter.com/foo/status/123", "x"),
    ("https://www.reddit.com/r/python/comments/abc/title/", "reddit"),
    ("reddit.com/r/python", "reddit"),
    ("https://github.com/anomalyco/opencode", "github"),
    ("https://news.ycombinator.com/item?id=1", "hackernews"),
    ("https://arxiv.org/abs/2301.00001", "arxiv"),
    ("https://en.wikipedia.org/wiki/Transformer", "wikipedia"),
    ("https://example.com/blog/feed", "rss"),
    ("https://example.com/feed.xml", "rss"),
    ("https://example.com/some/page", "web"),
    ("what is the capital of France", "web"),
    ("", "web"),
])
def test_detect_channel(url, expected):
    assert detect_channel(url) == expected


def test_all_declared_channels_are_registered():
    for name in ("web", "youtube", "github", "reddit", "x", "hackernews", "rss", "arxiv", "wikipedia"):
        assert name in CHANNELS
        assert CHANNELS[name].backends, f"{name} channel has no backends"
