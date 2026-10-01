"""src/chat_bridges/text_format.py: Markdown to the HTML subset Telegram accepts,
and cutting an answer into messages of at most 4096 characters."""
import re

import pytest

from src.chat_bridges import text_format as tf


def test_supported_markup_becomes_telegram_html():
    out = tf.markdown_to_html("# Title\nSome **bold**, *italic*, __also bold__, ~~gone~~ and `code`.\n- one\n* two")
    assert out.splitlines()[0] == "<b>Title</b>"
    assert "<b>bold</b>" in out and "<i>italic</i>" in out and "<b>also bold</b>" in out
    assert "<s>gone</s>" in out and "<code>code</code>" in out
    assert "• one" in out and "• two" in out


def test_everything_is_escaped_before_any_tag_is_made():
    out = tf.markdown_to_html("<script>alert('x')</script> & <img src=x onerror=1> 1 < 2 > 0")
    assert "<script" not in out and "<img" not in out
    assert "&lt;script&gt;" in out and "&amp;" in out and "1 &lt; 2 &gt; 0" in out
    # a link keeps only an http(s) address and escapes the rest
    link = tf.markdown_to_html("[a](https://e.com/?x=1&y=2) [b](javascript:alert(1)) [c](https://e.com/\"onclick=1)")
    assert '<a href="https://e.com/?x=1&amp;y=2">a</a>' in link
    assert "javascript:" in link and "<a href=\"javascript" not in link
    assert 'href="https://e.com/"onclick' not in link


def test_code_is_kept_verbatim_and_not_formatted():
    out = tf.markdown_to_html("```python\nif a < b and **c**:\n    print('&')\n```\nafter `**not bold**` end")
    assert '<pre><code class="language-python">if a &lt; b and **c**:\n    print(\'&amp;\')</code></pre>' in out
    assert "<code>**not bold**</code>" in out
    # an odd language word never lands in an attribute
    odd = tf.markdown_to_html("```py\"><script>\nx\n```")
    assert "<script>" not in odd and "class=" not in odd


def test_an_unclosed_fence_still_makes_valid_html():
    out = tf.markdown_to_html("```\nopen block\nsecond line")
    assert out == "<pre>open block\nsecond line</pre>"


def test_quotes_and_rules():
    out = tf.markdown_to_html("> quoted & more\n> second\n\n---\ntext")
    assert "<blockquote>quoted &amp; more\nsecond</blockquote>" in out and "──" in out


def test_snake_case_and_stars_in_words_are_left_alone():
    assert tf.markdown_to_html("use snake_case_names and 2*3*4") == "use snake_case_names and 2*3*4"


def test_every_message_is_within_the_limit_and_nothing_is_lost():
    text = "\n\n".join(f"Item {i}: " + "lorem ipsum " * 40 + "<&>" for i in range(200))
    pieces = tf.render_messages(text)
    assert len(pieces) > 5 and all(0 < len(p) <= tf.MAX_MESSAGE for p in pieces)
    assert [int(n) for n in re.findall(r"Item (\d+):", "".join(pieces))] == list(range(200))


@pytest.mark.parametrize("text", ["a" * 20000, "&" * 6000, "<" * 6000, "word " * 5000, "`x` " * 4000, "**b** " * 3000])
def test_awkward_inputs_still_fit(text):
    pieces = tf.render_messages(text)
    assert pieces and all(0 < len(p) <= tf.MAX_MESSAGE for p in pieces)


def test_a_long_code_block_is_closed_and_reopened_per_message():
    code = "```js\n" + "\n".join(f"let v{i} = {i} < 3;" for i in range(1200)) + "\n```"
    pieces = tf.render_messages(code)
    assert len(pieces) >= 3
    for piece in pieces:
        assert len(piece) <= tf.MAX_MESSAGE
        assert piece.startswith('<pre><code class="language-js">') and piece.endswith("</code></pre>")
    joined = "\n".join(re.sub(r"<[^>]+>", "", p) for p in pieces).replace("&lt;", "<")
    assert [int(n) for n in re.findall(r"let v(\d+) =", joined)] == list(range(1200))


def test_pairs_carry_the_plain_text_for_the_fallback():
    pairs = tf.render_pairs("**bold** and <tag>")
    assert pairs == [("<b>bold</b> and &lt;tag&gt;", "**bold** and <tag>")]


def test_empty_answers_make_no_messages():
    assert tf.render_messages("") == [] and tf.render_messages(" \n\n ") == []
    assert tf.plain_chunks("") == []


def test_plain_chunks_cut_at_line_breaks():
    text = "\n".join(["line " + str(i) * 30 for i in range(400)])
    chunks = tf.plain_chunks(text, 1000)
    assert all(len(c) <= 1000 for c in chunks) and len(chunks) > 5
    assert "\n".join(chunks).count("line ") == 400
    assert tf.plain_chunks("x" * 2500, 1000) == ["x" * 1000, "x" * 1000, "x" * 500]
