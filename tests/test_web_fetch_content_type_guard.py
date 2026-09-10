"""WEB-02 — fetch_webpage_content refuses binary content instead of silently
parsing it as HTML.

Before this change, any content type that was neither HTML, text/*, JSON,
PDF, nor a recognised text-file suffix fell straight into the HTML branch:
BeautifulSoup ran on the raw (often binary) body and the function returned
`"success": True` with empty/garbled `content` and no `error` — a fetch that
read nothing useful looked identical to a real, thin page. This module now
refuses those content types explicitly (`UnsupportedContentType`), same as
it already refuses an oversized body.
"""
import pytest

from services.search import content as content_mod


class _FakeResponse:
    def __init__(self, text, content_type, status_code=200):
        self.text = text
        self.content = text.encode("latin-1", errors="replace")
        self.headers = {"Content-Type": content_type}
        self.status_code = status_code

    def raise_for_status(self):
        return None


@pytest.fixture
def no_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(content_mod, "CONTENT_CACHE_DIR", tmp_path)
    monkeypatch.setattr(content_mod, "_cache_result", lambda *a, **k: None)


def _patch_fetch(monkeypatch, text, content_type):
    monkeypatch.setattr(
        content_mod,
        "_get_public_url",
        lambda url, headers=None, timeout=5, **kwargs: _FakeResponse(text, content_type),
    )


_BINARY_BODY = "\x89PNG\r\n\x1a\n" + ("\x00" * 40)


def test_image_content_type_is_refused_not_parsed_as_html(monkeypatch, no_cache):
    _patch_fetch(monkeypatch, _BINARY_BODY, "image/png")
    r = content_mod.fetch_webpage_content("https://example.com/photo.png")
    assert r["success"] is False
    assert "UnsupportedContentType" in r["error"]
    assert r["content"] == ""


def test_zip_content_type_is_refused(monkeypatch, no_cache):
    _patch_fetch(monkeypatch, _BINARY_BODY, "application/zip")
    r = content_mod.fetch_webpage_content("https://example.com/archive.zip")
    assert r["success"] is False
    assert "UnsupportedContentType" in r["error"]


def test_octet_stream_without_known_text_suffix_is_refused(monkeypatch, no_cache):
    # Contrast with test_web_fetch_plaintext.py's octet-stream+.txt/.json
    # cases, which must keep returning the body verbatim.
    _patch_fetch(monkeypatch, _BINARY_BODY, "application/octet-stream")
    r = content_mod.fetch_webpage_content("https://example.com/download.bin")
    assert r["success"] is False
    assert "UnsupportedContentType" in r["error"]


def test_octet_stream_with_txt_suffix_still_returns_body(monkeypatch, no_cache):
    # Regression guard: the suffix-based override for mislabelled text files
    # must still work after the binary guard is added.
    _patch_fetch(monkeypatch, "plain notes\n", "application/octet-stream")
    r = content_mod.fetch_webpage_content("https://example.com/notes.txt")
    assert r["success"] is True
    assert r["content"] == "plain notes"


def test_html_content_type_is_unaffected_by_the_guard(monkeypatch, no_cache):
    html = "<html><head><title>Hi</title></head><body><p>Hello world body text</p></body></html>"
    _patch_fetch(monkeypatch, html, "text/html; charset=utf-8")
    r = content_mod.fetch_webpage_content("https://example.com/page")
    assert r["success"] is True
    assert r["title"] == "Hi"
