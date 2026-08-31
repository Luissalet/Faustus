"""Quoting a selected passage into the composer.

The quoting rules are the whole feature: a quote that renders as loose lines,
or that trims itself to nothing, is worse than retyping the sentence.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_MODULE = (_REPO / "static" / "js" / "quoteSelection.js").as_uri()


def _node(script):
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    res = subprocess.run(["node", "--input-type=module"], input=script, capture_output=True,
                         text=True, encoding="utf-8", cwd=_REPO, timeout=30)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout.strip().splitlines()[-1])


def test_a_multi_paragraph_selection_stays_one_blockquote():
    out = _node(f"""
      import {{ blockquote }} from {json.dumps(_MODULE)};
      console.log(JSON.stringify({{
        para: blockquote('  first line\\n\\nsecond line  '),
        crlf: blockquote('a\\r\\nb'),
        empty: blockquote('   '),
        nullish: blockquote(null),
      }}));
    """)
    # The blank line keeps its ">" or Markdown renders two separate quotes.
    assert out["para"] == "> first line\n>\n> second line"
    assert out["crlf"] == "> a\n> b"
    assert out["empty"] == "" and out["nullish"] == ""


def test_a_long_passage_is_capped_on_a_word_boundary():
    out = _node(f"""
      import {{ blockquote }} from {json.dumps(_MODULE)};
      const words = ('word ').repeat(400);
      const oneToken = 'x'.repeat(50) + ' ' + 'y'.repeat(900);
      const q = blockquote(words);
      console.log(JSON.stringify({{
        len: q.length,
        endsEllipsis: q.endsWith('…'),
        noDanglingSpace: !q.endsWith(' …'),
        // One unbroken run must not trim the quote away to almost nothing.
        unbrokenLen: blockquote(oneToken).length,
      }}));
    """)
    assert out["endsEllipsis"] and out["noDanglingSpace"]
    assert 600 < out["len"] < 760
    assert out["unbrokenLen"] > 600


def test_an_existing_draft_survives_and_sits_below_the_quote():
    out = _node(f"""
      import {{ withQuote }} from {json.dumps(_MODULE)};
      console.log(JSON.stringify({{
        withDraft: withQuote('why does this happen?', 'the cache is warm'),
        empty: withQuote('', 'the cache is warm'),
        nothingToQuote: withQuote('keep me', '   '),
      }}));
    """)
    assert out["withDraft"] == "> the cache is warm\n\nwhy does this happen?"
    assert out["empty"] == "> the cache is warm\n\n"
    assert out["nothingToQuote"] == "keep me"


def test_the_composer_wires_the_quote_button():
    src = (_REPO / "static" / "js" / "chat.js").read_text(encoding="utf-8", errors="replace")
    assert "./quoteSelection.js" in src and "initQuoteSelection()" in src
    css = (_REPO / "static" / "style.css").read_text(encoding="utf-8", errors="replace")
    assert ".quote-selection-btn" in css
