"""``Transcript.extra["document"]``: a document, not a conversation.

A research report travels through the same docx/pdf renderers a chat does, so
before this flag existed it arrived wearing a chat's clothes: a "1 message"
count in the header and a grey role banner over the body. The flag turns those
two off and changes nothing else.

The risk this file exists to cover is the *other* direction. Both renderers
serve every conversation export in the product, so the guard has to be
provably inert when the flag is absent — which it is on every caller but
``src/report_export.py``. Hence the fingerprint tests: the same conversation,
rendered with no ``extra``, an empty one, an unrelated one and an explicitly
false flag, has to come out byte-for-byte the same document.

Two details make "byte-for-byte" checkable at all:

* a .docx is a zip whose entry headers carry the packing time, so the
  fingerprint is taken over the *contents* of its members;
* a PDF carries a creation date and a file ID, so reportlab's ``invariant``
  mode is switched on for the comparison (and only for it).
"""

import hashlib
import io
import re
import zipfile
from datetime import datetime

import pytest

from src.chat_export import DOCUMENT_FLAG, is_document, markdown_to_blocks
from src.chat_export_model import (
    Block,
    ExportMessage,
    Span,
    ToolCall,
    Transcript,
)

# Word style *ids*, which are the names with the spaces taken out.
ROLE_STYLES = ("FaustusRoleUser", "FaustusRoleAssistant",
               "FaustusRoleSystem", "FaustusRoleTool")
META_STYLE = "FaustusMeta"

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

BODY_MD = """## Resumen

Texto con **negrita**, `código` y un [enlace](https://example.com).

- uno
- dos

```python
def f(x):
    return x * 2
```

| a | b |
|---|---|
| 1 | 2 |

> citado
"""


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def make_transcript(**kwargs):
    """A conversation with one of everything the renderers know how to draw."""
    messages = [
        ExportMessage(role="user", blocks=markdown_to_blocks("¿Qué tal?"),
                      timestamp="2026-02-25T06:16:22Z", attachments=["nota.txt"]),
        ExportMessage(role="assistant", blocks=markdown_to_blocks(BODY_MD),
                      timestamp="2026-02-25T06:16:30Z", model="qwen3:14b",
                      tool_calls=[ToolCall(name="search", arguments="{}",
                                           result="ok", status="done",
                                           duration_s=1.25)]),
        ExportMessage(role="system", blocks=[Block(kind="para", spans=[Span("sys")])]),
    ]
    return Transcript(
        name="Una conversación",
        model=kwargs.pop("model", "qwen3:14b"),
        exported_at=kwargs.pop("exported_at", datetime(2026, 2, 25, 6, 16, 22)),
        messages=kwargs.pop("messages", messages),
        session_id=kwargs.pop("session_id", "sid-1"),
        project=kwargs.pop("project", "proyecto"),
        workspace=kwargs.pop("workspace", "ws"),
        **kwargs,
    )


def make_document(**kwargs):
    return make_transcript(extra={DOCUMENT_FLAG: True}, **kwargs)


def render_docx(transcript):
    from src import chat_export_docx
    return chat_export_docx.render(transcript)


def render_pdf(transcript):
    from src import chat_export_pdf
    return chat_export_pdf.render(transcript)


def docx_fingerprint(payload):
    """A digest of the package's contents, ignoring its zip entry timestamps."""
    archive = zipfile.ZipFile(io.BytesIO(payload))
    digest = hashlib.sha256()
    for name in sorted(archive.namelist()):
        digest.update(name.encode("utf-8"))
        digest.update(archive.read(name))
    return digest.hexdigest()


def docx_paragraphs(payload):
    """(style, text) for every paragraph, in document order."""
    import xml.etree.ElementTree as ET

    xml = zipfile.ZipFile(io.BytesIO(payload)).read("word/document.xml")
    out = []
    for paragraph in ET.fromstring(xml).iter(W + "p"):
        style = paragraph.find("./%spPr/%spStyle" % (W, W))
        text = "".join(node.text or "" for node in paragraph.iter(W + "t"))
        out.append((style.get(W + "val") if style is not None else None, text))
    return out


def pdf_text(payload):
    pypdf = pytest.importorskip("pypdf")
    reader = pypdf.PdfReader(io.BytesIO(payload))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


@pytest.fixture
def invariant_pdf(monkeypatch):
    """Fix reportlab's creation date and file ID so two renders can be equal."""
    rl_config = pytest.importorskip("reportlab.rl_config")
    monkeypatch.setattr(rl_config, "invariant", 1, raising=False)


# --------------------------------------------------------------------------
# the flag itself
# --------------------------------------------------------------------------


def test_a_transcript_without_the_flag_is_a_conversation():
    assert is_document(make_transcript()) is False


@pytest.mark.parametrize("extra", [
    {},
    {"kind": "research_report"},            # unrelated keys must not trigger it
    {DOCUMENT_FLAG: False},
    {DOCUMENT_FLAG: ""},
])
def test_only_a_truthy_flag_makes_a_document(extra):
    assert is_document(make_transcript(extra=extra)) is False


def test_the_flag_is_read_from_extra():
    assert is_document(make_document()) is True


@pytest.mark.parametrize("broken", [None, "document", 7, object()])
def test_a_malformed_extra_reads_as_a_conversation(broken):
    """``extra`` is free-form, so a bad one must not abort an export."""
    transcript = make_transcript()
    transcript.extra = broken
    assert is_document(transcript) is False
    assert is_document(None) is False


# --------------------------------------------------------------------------
# a conversation renders exactly as it did before the flag existed
# --------------------------------------------------------------------------


@pytest.mark.parametrize("extra", [
    None,
    {},
    {"kind": "chat", "owner": "luis"},
    {DOCUMENT_FLAG: False},
])
def test_docx_conversation_is_byte_identical_whatever_extra_holds(extra):
    pytest.importorskip("docx")
    baseline = docx_fingerprint(render_docx(make_transcript()))
    transcript = make_transcript()
    if extra is not None:
        transcript.extra = extra
    assert docx_fingerprint(render_docx(transcript)) == baseline


@pytest.mark.parametrize("extra", [
    None,
    {},
    {"kind": "chat", "owner": "luis"},
    {DOCUMENT_FLAG: False},
])
def test_pdf_conversation_is_byte_identical_whatever_extra_holds(extra, invariant_pdf):
    pytest.importorskip("reportlab")
    baseline = render_pdf(make_transcript())
    transcript = make_transcript()
    if extra is not None:
        transcript.extra = extra
    assert render_pdf(transcript) == baseline


def test_a_conversation_still_gets_its_banners_and_its_count():
    pytest.importorskip("docx")
    paragraphs = docx_paragraphs(render_docx(make_transcript()))
    styles = [style for style, _text in paragraphs]
    assert "FaustusRoleUser" in styles and "FaustusRoleAssistant" in styles
    assert any("3 messages" in text for _style, text in paragraphs)


# --------------------------------------------------------------------------
# a document loses the chat furniture, and only that
# --------------------------------------------------------------------------


def test_docx_document_drops_the_role_banners_and_the_message_count():
    pytest.importorskip("docx")
    payload = render_docx(make_document())
    assert payload[:2] == b"PK"
    paragraphs = docx_paragraphs(payload)
    for style, _text in paragraphs:
        assert style not in ROLE_STYLES
    joined = "\n".join(text for _style, text in paragraphs)
    assert "1 message" not in joined and "3 messages" not in joined
    assert "User" not in joined and "Assistant" not in joined


def test_docx_document_keeps_the_export_date_under_the_title():
    pytest.importorskip("docx")
    texts = [text for _style, text in docx_paragraphs(render_docx(make_document()))]
    assert texts[0] == "Una conversación"
    assert texts[1].startswith("Model: qwen3:14b")
    assert "Exported 2026-02-25 06:16" in texts[1]


def test_docx_document_differs_from_the_conversation_only_by_that_furniture():
    """Everything else — styles, headings, code, tables, links — is untouched."""
    pytest.importorskip("docx")
    conversation = docx_paragraphs(render_docx(make_transcript()))
    document = docx_paragraphs(render_docx(make_document()))

    survivors = [(style, text) for style, text in conversation
                 if style not in ROLE_STYLES]

    # The header's metadata line is the only paragraph whose text changes.
    assert survivors[1][0] == document[1][0] == META_STYLE
    assert survivors[1][1] == document[1][1].replace(
        "  \u00b7  Exported", "  \u00b7  3 messages  \u00b7  Exported")
    # Everything after it — headings, list, code, table, quote, tool call,
    # attachments — is the same paragraph with the same style.
    assert survivors[2:] == document[2:]


def test_a_document_with_nothing_to_say_gets_no_empty_metadata_line():
    pytest.importorskip("docx")
    bare = Transcript(name="Informe", model="", exported_at=None,
                      messages=[ExportMessage(role="report",
                                              blocks=[Block(kind="para",
                                                            spans=[Span("cuerpo")])])],
                      extra={DOCUMENT_FLAG: True})
    paragraphs = docx_paragraphs(render_docx(bare))
    assert paragraphs[0][1] == "Informe"
    assert META_STYLE not in [style for style, _text in paragraphs]
    assert "cuerpo" in "\n".join(text for _style, text in paragraphs)


def test_pdf_document_drops_the_role_banners_and_the_message_count():
    pytest.importorskip("reportlab")
    text = pdf_text(render_pdf(make_document()))
    assert "message" not in text.lower()
    assert not re.search(r"^\s*(User|Assistant|System)\s*$", text, re.M)
    assert "Exported 2026-02-25 06:16" in text


def test_pdf_document_keeps_the_body_the_conversation_had():
    pytest.importorskip("reportlab")
    text = pdf_text(render_pdf(make_document()))
    for expected in ("Resumen", "negrita", "def f(x):", "citado", "uno"):
        assert expected in text


def test_a_document_with_no_messages_still_renders():
    pytest.importorskip("docx")
    pytest.importorskip("reportlab")
    empty = Transcript(name="Informe vacío", model="",
                       exported_at=datetime(2026, 2, 25, 6, 16, 22),
                       extra={DOCUMENT_FLAG: True})
    assert render_docx(empty)[:2] == b"PK"
    assert render_pdf(empty)[:5] == b"%PDF-"
