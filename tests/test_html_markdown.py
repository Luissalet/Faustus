"""src.html_markdown -- HTML -> structured Markdown conversion.

Fixtures cover: headings, lists (flat + nested), a GFM table, a code block
with a language class, relative-to-absolute link resolution, javascript:
links being dropped, a layout (single-column) table flattening to text, a
nested table flattening to text, and script/style/nav removal. Also covers
fetch_webpage_content returning markdown + links with the network
monkeypatched, and a markdown_to_text roundtrip.
"""
import pytest

pytest.importorskip("bs4")

from src.html_markdown import html_to_markdown, markdown_to_text


# ---------------------------------------------------------------------------
# html_to_markdown
# ---------------------------------------------------------------------------
def test_headings_render_as_hash_markers():
    html = """
    <html><body><main>
      <h1>Title One</h1>
      <p>Intro paragraph with enough substantive filler text that it clears
      the thin-content fallback threshold comfortably on its own, repeated
      repeated repeated repeated repeated repeated repeated repeated text.</p>
      <h2>Subheading</h2>
      <p>More body text here, also long enough that this whole page is not
      classified as thin content and does not trigger the body fallback
      path, repeated repeated repeated repeated repeated repeated text.</p>
    </main></body></html>
    """
    out = html_to_markdown(html, base_url="https://example.com/")
    assert "# Title One" in out["markdown"]
    assert "## Subheading" in out["markdown"]
    assert {"level": 1, "text": "Title One"} in out["headings"]
    assert {"level": 2, "text": "Subheading"} in out["headings"]


def test_nested_lists_render_with_indentation():
    html = """
    <html><body><main>
      <p>Filler paragraph text so the page clears the thin-content
      fallback threshold and the list below is rendered from the main
      content area rather than a body-wide fallback pass, repeated text.</p>
      <ul>
        <li>Top item one
          <ul><li>Nested item A</li><li>Nested item B</li></ul>
        </li>
        <li>Top item two</li>
      </ul>
      <ol><li>First</li><li>Second</li></ol>
    </main></body></html>
    """
    out = html_to_markdown(html, base_url="https://example.com/")
    md = out["markdown"]
    assert "- Top item one" in md
    assert "  - Nested item A" in md
    assert "  - Nested item B" in md
    assert "- Top item two" in md
    assert "1. First" in md
    assert "2. Second" in md


def test_table_renders_as_gfm_pipe_table():
    html = """
    <html><body><main>
      <p>Filler paragraph text so the page clears the thin-content
      fallback threshold before the table below is checked, repeated
      repeated repeated repeated text to pad this out nicely here now.</p>
      <table>
        <tr><th>Name</th><th>Value | Weird</th></tr>
        <tr><td>Alpha</td><td>1</td></tr>
        <tr><td>Beta</td><td>2</td></tr>
      </table>
    </main></body></html>
    """
    out = html_to_markdown(html, base_url="https://example.com/")
    md = out["markdown"]
    assert "| Name | Value \\| Weird |" in md
    assert "| --- | --- |" in md
    assert "| Alpha | 1 |" in md
    assert "| Beta | 2 |" in md
    assert out["tables"] == 1


def test_layout_table_with_one_column_is_flattened_to_text():
    html = """
    <html><body><main>
      <p>Filler paragraph text so the page clears the thin-content
      fallback threshold before the layout table below is checked here,
      repeated repeated repeated repeated text to pad this out well.</p>
      <table>
        <tr><td>Just one column of text</td></tr>
        <tr><td>Another single-column row</td></tr>
      </table>
    </main></body></html>
    """
    out = html_to_markdown(html, base_url="https://example.com/")
    assert out["tables"] == 0
    assert "|" not in out["markdown"].split("Just one column")[-1][:5] if "Just one column" in out["markdown"] else True
    assert "Just one column of text" in out["markdown"]


def test_nested_table_is_flattened_to_text():
    html = """
    <html><body><main>
      <p>Filler paragraph text so the page clears the thin-content
      fallback threshold before the nested table below is checked here,
      repeated repeated repeated repeated text to pad this out well.</p>
      <table>
        <tr><td>Outer</td><td><table><tr><td>Inner</td></tr></table></td></tr>
      </table>
    </main></body></html>
    """
    out = html_to_markdown(html, base_url="https://example.com/")
    assert out["tables"] == 0
    assert "Outer" in out["markdown"]
    assert "Inner" in out["markdown"]


def test_code_block_with_language_class_becomes_fenced_block():
    html = """
    <html><body><main>
      <p>Filler paragraph text so the page clears the thin-content
      fallback threshold before the code block below is checked here,
      repeated repeated repeated repeated text to pad this out well.</p>
      <pre><code class="language-python">def f():\n    return 1</code></pre>
    </main></body></html>
    """
    out = html_to_markdown(html, base_url="https://example.com/")
    md = out["markdown"]
    assert "```python" in md
    assert "def f():" in md
    assert "return 1" in md
    assert md.count("```") == 2


def test_inline_code_renders_with_backticks():
    html = """
    <html><body><main>
      <p>Filler paragraph text so the page clears the thin-content
      fallback threshold, repeated repeated repeated repeated repeated
      repeated repeated repeated text to pad this out nicely here.</p>
      <p>Run <code>pip install foo</code> to install it.</p>
    </main></body></html>
    """
    out = html_to_markdown(html, base_url="https://example.com/")
    assert "`pip install foo`" in out["markdown"]


def test_relative_links_resolved_to_absolute():
    html = """
    <html><body><main>
      <p>Filler paragraph text so the page clears the thin-content
      fallback threshold, repeated repeated repeated repeated repeated
      repeated repeated repeated text to pad this out nicely here.</p>
      <p>See <a href="/docs/guide">the guide</a> for more.</p>
    </main></body></html>
    """
    out = html_to_markdown(html, base_url="https://example.com/base/page")
    assert "[the guide](https://example.com/docs/guide)" in out["markdown"]
    assert any(l["url"] == "https://example.com/docs/guide" and l["text"] == "the guide"
               for l in out["links"])
    assert any(l["internal"] is True for l in out["links"])


def test_javascript_links_are_dropped_but_text_kept():
    html = """
    <html><body><main>
      <p>Filler paragraph text so the page clears the thin-content
      fallback threshold, repeated repeated repeated repeated repeated
      repeated repeated repeated text to pad this out nicely here.</p>
      <p>Click <a href="javascript:void(0)">here</a> to do nothing.</p>
    </main></body></html>
    """
    out = html_to_markdown(html, base_url="https://example.com/")
    assert "javascript:" not in out["markdown"]
    assert "here" in out["markdown"]
    assert not any(l["url"].startswith("javascript:") for l in out["links"])


def test_mailto_links_are_kept():
    html = """
    <html><body><main>
      <p>Filler paragraph text so the page clears the thin-content
      fallback threshold, repeated repeated repeated repeated repeated
      repeated repeated repeated text to pad this out nicely here.</p>
      <p>Email <a href="mailto:hi@example.com">us</a> anytime.</p>
    </main></body></html>
    """
    out = html_to_markdown(html, base_url="https://example.com/")
    assert "[us](mailto:hi@example.com)" in out["markdown"]


def test_image_with_alt_renders_markdown_image():
    html = """
    <html><body><main>
      <p>Filler paragraph text so the page clears the thin-content
      fallback threshold, repeated repeated repeated repeated repeated
      repeated repeated repeated text to pad this out nicely here.</p>
      <img src="/img/pic.png" alt="A description">
      <img src="/img/nopic.png">
    </main></body></html>
    """
    out = html_to_markdown(html, base_url="https://example.com/")
    assert "![A description](https://example.com/img/pic.png)" in out["markdown"]
    assert "nopic.png" not in out["markdown"]


def test_script_style_and_nav_are_removed():
    html = """
    <html><body>
      <nav><a href="/x">Nav link one</a><a href="/y">Nav link two</a></nav>
      <script>window.secret = "not content";</script>
      <style>.hidden { display: none; }</style>
      <main>
        <p>Filler paragraph text so the page clears the thin-content
        fallback threshold, repeated repeated repeated repeated repeated
        repeated repeated repeated text to pad this out nicely here.</p>
        <p>The real substantive article body text goes here instead.</p>
      </main>
    </body></html>
    """
    out = html_to_markdown(html, base_url="https://example.com/")
    md = out["markdown"]
    assert "window.secret" not in md
    assert "display: none" not in md
    assert "Nav link one" not in md
    assert "The real substantive article body text" in md


def test_thin_main_falls_back_to_full_body():
    html = """
    <html><body>
      <div class="app-shell">x</div>
      <div>
        <p>This app-landing page has no obviously "content"-classed wrapper
        around its real text, so the class-regex heuristic only finds the
        tiny app-shell div and this whole function must fall back to the
        noise-stripped body instead of returning a near-empty result, which
        is exactly the behavior this test exists to pin down here today.</p>
      </div>
    </body></html>
    """
    out = html_to_markdown(html, base_url="https://example.com/")
    assert "app-landing page has no obviously" in out["markdown"]


# ---------------------------------------------------------------------------
# markdown_to_text
# ---------------------------------------------------------------------------
def test_markdown_to_text_strips_syntax_but_keeps_words():
    md = (
        "# Title\n\n"
        "This is **bold** and *italic* and `code`.\n\n"
        "- item one\n"
        "- item two\n\n"
        "> a quote\n\n"
        "[a link](https://example.com/x)\n\n"
        "```python\n"
        "print('hi')\n"
        "```\n"
    )
    text = markdown_to_text(md)
    assert "#" not in text.split("\n")[0] or "Title" in text
    assert "**" not in text
    assert "*italic*" not in text
    assert "`code`" not in text
    assert "item one" in text
    assert "item two" in text
    assert "a quote" in text
    assert "a link" in text
    assert "https://example.com/x" not in text
    assert "print('hi')" in text
    assert "```" not in text


def test_markdown_to_text_table_becomes_spaced_words():
    md = "| Name | Value |\n| --- | --- |\n| Alpha | 1 |\n"
    text = markdown_to_text(md)
    assert "|" not in text
    assert "Name" in text and "Value" in text
    assert "Alpha" in text and "1" in text


def test_markdown_to_text_roundtrip_is_idempotent_on_plain_prose():
    prose = "Plain prose with no markdown syntax at all in it whatsoever."
    assert markdown_to_text(prose) == prose


# ---------------------------------------------------------------------------
# fetch_webpage_content integration (network monkeypatched)
# ---------------------------------------------------------------------------
class _FakeResponse:
    status_code = 200
    headers = {"Content-Type": "text/html; charset=utf-8"}
    content = b""

    def __init__(self, text: str):
        self.text = text

    def raise_for_status(self):
        return None


@pytest.fixture
def no_cache(monkeypatch, tmp_path):
    from services.search import content as content_mod
    monkeypatch.setattr(content_mod, "CONTENT_CACHE_DIR", tmp_path)
    content_mod.content_cache_index.clear()
    monkeypatch.setattr(content_mod, "_cache_result", lambda *a, **k: None)
    return content_mod


_HTML_PAGE = """
<html><head><title>Doc Page</title></head><body><main>
  <h1>Doc Page</h1>
  <p>This is a substantive paragraph with enough words to clear the thin
  content threshold comfortably, repeated repeated repeated repeated
  repeated repeated repeated repeated repeated repeated text here now.</p>
  <ul><li>First point</li><li>Second point</li></ul>
  <p>See <a href="/more">more info</a> on this topic for further detail.</p>
</main></body></html>
"""


def test_fetch_webpage_content_returns_markdown_by_default(no_cache, monkeypatch):
    monkeypatch.setattr(
        no_cache, "_get_public_url",
        lambda url, headers, timeout, **kwargs: _FakeResponse(_HTML_PAGE),
    )
    result = no_cache.fetch_webpage_content("https://example.com/doc")
    assert result["success"] is True
    assert result["format"] == "markdown"
    assert "# Doc Page" in result["content"]
    assert "- First point" in result["content"]
    assert any(l["url"] == "https://example.com/more" for l in result["links"])
    assert result["headings"] and result["headings"][0]["text"] == "Doc Page"


def test_fetch_webpage_content_format_text_keeps_flat_text(no_cache, monkeypatch):
    monkeypatch.setattr(
        no_cache, "_get_public_url",
        lambda url, headers, timeout, **kwargs: _FakeResponse(_HTML_PAGE),
    )
    result = no_cache.fetch_webpage_content("https://example.com/doc", format="text")
    assert result["success"] is True
    assert result["format"] == "text"
    assert "# Doc Page" not in result["content"]
    assert "Doc Page" in result["content"]
    assert result["links"] == []
