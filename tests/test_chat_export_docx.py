"""Tests for the python-docx chat exporter.

A .docx is a zip of XML, so every case here reopens the produced bytes with
zipfile + ElementTree and asserts against ``word/document.xml`` itself rather
than trusting that ``render()`` returned without raising.

What they pin down:

  * the document is built from **real Word styles** (Heading 1-3, List Bullet /
    List Number, Quote, Table Grid) so it can be restyled in Word, instead of
    hand-applied bold and indents,
  * hyperlinks are real ``w:hyperlink`` elements with an external relationship
    - python-docx has no API for that, so it is hand-rolled and easy to get
    subtly wrong (a blue-looking run that does not click),
  * text that is not representable in XML (control characters) is stripped
    before lxml can raise on it.

Transcripts are built by hand from chat_export_model, so the renderer is
tested against the *contract* and not against the markdown pipeline.
"""

import io
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime

import pytest

from src import chat_export_docx as docx_export
from src.chat_export_model import (
    Block,
    ExportMessage,
    ExportUnavailable,
    Span,
    ToolCall,
    Transcript,
)

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def make_transcript(messages=None, name="Conversación de prueba", **kwargs):
    return Transcript(
        name=name,
        model=kwargs.pop("model", "claude-opus-5"),
        exported_at=kwargs.pop("exported_at", datetime(2026, 8, 31, 20, 15)),
        messages=list(messages or []),
        **kwargs,
    )


def para(*spans):
    return Block(kind="para", spans=[s if isinstance(s, Span) else Span(s) for s in spans])


def message(role="user", blocks=None, **kwargs):
    return ExportMessage(role=role, blocks=list(blocks or []), **kwargs)


def open_docx(data):
    assert data[:2] == b"PK", "not a zip - .docx must be an OOXML package"
    archive = zipfile.ZipFile(io.BytesIO(data))
    assert archive.testzip() is None
    assert "word/document.xml" in archive.namelist()
    return archive


def document_xml(data):
    return open_docx(data).read("word/document.xml").decode("utf-8")


def document_root(data):
    return ET.fromstring(document_xml(data))


def document_text(data):
    """Visible text, with line breaks and paragraph boundaries preserved."""
    root = document_root(data)
    paragraphs = []
    for paragraph in root.iter(W + "p"):
        parts = []
        for node in paragraph.iter():
            if node.tag == W + "t":
                parts.append(node.text or "")
            elif node.tag == W + "br":
                parts.append("\n")
            elif node.tag == W + "tab":
                parts.append("\t")
        paragraphs.append("".join(parts))
    return "\n".join(paragraphs)


def paragraph_styles(data):
    return [node.get(W + "val") for node in document_root(data).iter(W + "pStyle")]


def relationships(data):
    xml = open_docx(data).read("word/_rels/document.xml.rels").decode("utf-8")
    return ET.fromstring(xml)


def hyperlink_targets(data):
    rels = relationships(data)
    ids = {node.get(W + "id") or node.get(R + "id")
           for node in document_root(data).iter(W + "hyperlink")}
    ids.discard(None)
    return sorted(node.get("Target") for node in rels
                  if node.get("Id") in ids and node.get("Target"))


# --------------------------------------------------------------------------
# basics
# --------------------------------------------------------------------------


def test_render_returns_a_readable_docx_with_the_conversation_in_it():
    data = docx_export.render(make_transcript([
        message("user", [para("¿Cuál es la capital de España?")],
                timestamp="2026-08-31T20:10:00"),
        message("assistant", [para("Madrid.")], timestamp="2026-08-31T20:10:04",
                model="claude-opus-5"),
    ]))
    text = document_text(data)
    assert "¿Cuál es la capital de España?" in text
    assert "Madrid." in text
    assert "User" in text and "Assistant" in text
    assert "2026-08-31 20:10" in text


def test_header_carries_name_model_and_export_date():
    data = docx_export.render(make_transcript(
        [message("user", [para("hola")])], name="Despliegue TLS",
        session_id="s-42", project="Faustus"))
    text = document_text(data)
    assert "Despliegue TLS" in text
    assert "claude-opus-5" in text and "2026-08-31 20:15" in text
    assert "s-42" in text and "Faustus" in text
    assert "Title" in paragraph_styles(data)


def test_empty_transcript_still_produces_a_valid_document():
    data = docx_export.render(make_transcript([], name="Sin mensajes"))
    text = document_text(data)
    assert "Sin mensajes" in text
    assert "no messages" in text


def test_footer_uses_page_number_fields_not_literal_text():
    """Word must compute the numbers, so they stay right as the doc is edited."""
    archive = open_docx(docx_export.render(make_transcript([message("user", [para("x")])])))
    footers = [name for name in archive.namelist() if name.startswith("word/footer")]
    assert footers
    xml = archive.read(footers[0]).decode("utf-8")
    assert "PAGE" in xml and "NUMPAGES" in xml
    assert "fldSimple" in xml


# --------------------------------------------------------------------------
# real Word styles
# --------------------------------------------------------------------------


def test_structure_uses_builtin_word_styles_so_it_can_be_restyled():
    blocks = [
        Block(kind="heading", level=1, spans=[Span("Título")]),
        Block(kind="heading", level=2, spans=[Span("Subtítulo")]),
        Block(kind="heading", level=3, spans=[Span("Detalle")]),
        Block(kind="list", ordered=True, items=[[para("uno")], [para("dos")]]),
        Block(kind="list", items=[
            [para("viñeta"), Block(kind="list", items=[[para("anidada")]])]]),
        Block(kind="quote", children=[para("citado")]),
    ]
    data = docx_export.render(make_transcript([message("assistant", blocks)]))
    styles = paragraph_styles(data)
    for expected in ("Heading1", "Heading2", "Heading3", "ListNumber",
                     "ListBullet", "ListBullet2", "Quote"):
        assert expected in styles, "%s missing from %s" % (expected, sorted(set(styles)))
    text = document_text(data)
    for expected in ("Título", "Subtítulo", "Detalle", "uno", "dos", "viñeta",
                     "anidada", "citado"):
        assert expected in text


def test_tables_use_the_table_grid_style_with_a_repeating_header():
    rows = [[[Span("Opción")], [Span("Valor")]],
            [[Span("--host")], [Span("0.0.0.0")]]]
    data = docx_export.render(make_transcript([
        message("assistant", [Block(kind="table", header=True, rows=rows)])]))
    root = document_root(data)
    tables = list(root.iter(W + "tbl"))
    assert len(tables) == 1
    assert [node.get(W + "val") for node in tables[0].iter(W + "tblStyle")] == ["TableGrid"]
    assert list(tables[0].iter(W + "tblHeader")), "header row does not repeat"
    text = document_text(data)
    assert "Opción" in text and "0.0.0.0" in text


def test_code_and_role_banners_use_named_styles_with_shading():
    """Shading lives in the style, not in manual per-paragraph formatting."""
    data = docx_export.render(make_transcript([
        message("user", [para("mira")]),
        message("assistant", [Block(kind="code", lang="py", text="x = 1")])]))
    styles = paragraph_styles(data)
    assert "FaustusCode" in styles
    assert "FaustusRoleUser" in styles and "FaustusRoleAssistant" in styles
    styles_xml = open_docx(data).read("word/styles.xml").decode("utf-8")
    assert 'w:styleId="FaustusCode"' in styles_xml
    assert docx_export.CODE_FILL in styles_xml       # w:shd on the style
    assert docx_export.MONO_FONT in styles_xml


def test_inline_code_uses_a_character_style():
    data = docx_export.render(make_transcript([
        message("user", [para(Span("corre "), Span("uvicorn", code=True))])]))
    root = document_root(data)
    run_styles = [node.get(W + "val") for node in root.iter(W + "rStyle")]
    assert "FaustusInlineCode" in run_styles
    assert "uvicorn" in document_text(data)


# --------------------------------------------------------------------------
# hyperlinks
# --------------------------------------------------------------------------


def test_links_are_real_hyperlinks_with_an_external_relationship():
    url = "https://example.com/a?b=1&c=2"
    data = docx_export.render(make_transcript([
        message("assistant", [para(Span("la doc", href=url), Span(" y más"))])]))
    root = document_root(data)
    links = list(root.iter(W + "hyperlink"))
    assert len(links) == 1
    rel_id = links[0].get(R + "id")
    assert rel_id, "w:hyperlink has no r:id"
    # the link text is inside the hyperlink element, not merely next to it
    assert "la doc" in "".join(node.text or "" for node in links[0].iter(W + "t"))
    entry = [node for node in relationships(data) if node.get("Id") == rel_id]
    assert len(entry) == 1
    assert entry[0].get("Target") == url
    assert entry[0].get("TargetMode") == "External"
    assert entry[0].get("Type").endswith("/hyperlink")
    assert "Hyperlink" in [node.get(W + "val") for node in root.iter(W + "rStyle")]


def test_unsafe_or_relative_hrefs_are_not_linked():
    spans = [Span("bueno", href="https://example.com/ok"), Span(" "),
             Span("malo", href="javascript:alert(1)"), Span(" "),
             Span("relativo", href="../otra.html")]
    data = docx_export.render(make_transcript([message("assistant", [para(*spans)])]))
    assert hyperlink_targets(data) == ["https://example.com/ok"]
    text = document_text(data)
    assert "malo" in text and "relativo" in text     # text kept, link dropped


# --------------------------------------------------------------------------
# text that breaks XML writers
# --------------------------------------------------------------------------


def test_literal_markup_from_the_user_stays_text():
    hostile = "Usa <b>negrita</b> & <w:p>etiquetas</w:p>"
    data = docx_export.render(make_transcript([message("user", [para(hostile)])]))
    assert hostile in document_text(data)
    raw = document_xml(data)
    assert "&lt;b&gt;negrita&lt;/b&gt;" in raw
    # the injected <w:p> did not become a paragraph element
    assert len(list(document_root(data).iter(W + "p"))) < 20


def test_control_characters_are_stripped_instead_of_raising():
    """lxml refuses \\x00-\\x08 outright: they must never reach the writer."""
    data = docx_export.render(make_transcript([
        message("user", [para("antes\x00\x07medio\x1fdespués")]),
        message("assistant", [Block(kind="code", text="a\x00b")])]))
    text = document_text(data)
    assert "antesmediodespués" in text
    assert "ab" in text


def test_unicode_survives_intact_including_emoji_and_cjk():
    """A .docx stores UTF-8 and Word does its own font fallback, so unlike the
    PDF nothing has to be substituted here."""
    data = docx_export.render(make_transcript(
        [message("user", [para("Configuración: ñandú, ¿qué tal? áéíóúü ÁÉÍÓÚÑ")]),
         message("assistant", [para("Griego αβγ, cirílico джип, CJK 日本語, emoji 😀🚀")])],
        name="Título con ñ y 😀"))
    text = document_text(data)
    assert "Configuración: ñandú, ¿qué tal? áéíóúü ÁÉÍÓÚÑ" in text
    assert "αβγ" in text and "джип" in text and "日本語" in text and "😀🚀" in text
    assert "Título con ñ y 😀" in text


# --------------------------------------------------------------------------
# things that overflow
# --------------------------------------------------------------------------


def test_a_2000_character_url_is_kept_whole():
    url = "https://ejemplo.es/" + "a" * 2000
    started = time.time()
    data = docx_export.render(make_transcript([message("user", [para(url)])]))
    assert time.time() - started < 10
    assert url in document_text(data)


def test_a_long_unbroken_code_line_is_hard_wrapped():
    data = docx_export.render(make_transcript([
        message("assistant", [Block(kind="code", text="x" * 5000)])]))
    code_lines = [line for line in document_text(data).splitlines() if line.startswith("x")]
    assert len(code_lines) > 10
    assert max(len(line) for line in code_lines) <= 200
    assert sum(len(line) for line in code_lines) == 5000    # nothing was dropped


def test_a_500_line_code_block_keeps_every_line_in_one_shaded_paragraph():
    source = "\n".join("linea_%03d = compute(%d)" % (i, i) for i in range(500))
    data = docx_export.render(make_transcript([
        message("assistant", [Block(kind="code", lang="python", text=source)])]))
    root = document_root(data)
    code_paragraphs = [p for p in root.iter(W + "p")
                       if any(node.get(W + "val") == "FaustusCode"
                              for node in p.iter(W + "pStyle"))]
    assert len(code_paragraphs) == 1
    assert len(list(code_paragraphs[0].iter(W + "br"))) == 499
    text = document_text(data)
    assert "linea_000 = compute(0)" in text
    assert "linea_499 = compute(499)" in text


def test_code_indentation_and_blank_lines_survive():
    source = "def f():\n    if x:\n        return 1\n\n    return 0"
    data = docx_export.render(make_transcript([
        message("assistant", [Block(kind="code", text=source)])]))
    assert "        return 1" in document_text(data)
    assert 'xml:space="preserve"' in document_xml(data)


def test_a_ten_column_table_keeps_all_its_columns():
    header = [[Span("Columna %d" % i)] for i in range(10)]
    rows = [header] + [[[Span("v%d-%d" % (r, c))] for c in range(10)] for r in range(4)]
    data = docx_export.render(make_transcript([
        message("assistant", [Block(kind="table", header=True, rows=rows)])]))
    table = list(document_root(data).iter(W + "tbl"))[0]
    assert len(list(table.iter(W + "gridCol"))) == 10
    assert len(list(table.iter(W + "tr"))) == 5
    text = document_text(data)
    assert "Columna 0" in text and "Columna 9" in text and "v3-7" in text


def test_ragged_table_rows_do_not_crash():
    rows = [[[Span("a")], [Span("b")], [Span("c")]], [[Span("solo")]], []]
    data = docx_export.render(make_transcript([
        message("assistant", [Block(kind="table", header=True, rows=rows)])]))
    assert "solo" in document_text(data)


# --------------------------------------------------------------------------
# messages
# --------------------------------------------------------------------------


def test_tool_calls_are_rendered_in_their_own_style():
    call = ToolCall(name="run_shell", arguments='{"cmd": "ls -la"}',
                    result="total 8\ndrwxr-xr-x", status="ok", duration_s=0.42)
    data = docx_export.render(make_transcript([
        message("assistant", [para("Ejecuto esto:")], tool_calls=[call])]))
    assert "FaustusToolCall" in paragraph_styles(data)
    text = document_text(data)
    assert "run_shell" in text and '"cmd": "ls -la"' in text
    assert "drwxr-xr-x" in text and "0.42s" in text


def test_huge_tool_output_is_truncated():
    data = docx_export.render(make_transcript([
        message("assistant", [], tool_calls=[ToolCall(name="dump", result="x" * 50000)])]))
    text = document_text(data)
    assert "truncated" in text
    assert len(text) < 20000


def test_attachments_are_listed():
    data = docx_export.render(make_transcript([
        message("user", [para("mira")], attachments=["captura.png", "informe.pdf"])]))
    text = document_text(data)
    assert "captura.png" in text and "informe.pdf" in text


def test_unknown_roles_and_missing_metadata_are_tolerated():
    data = docx_export.render(Transcript(
        name="", model="", exported_at=datetime(2026, 1, 1),
        messages=[ExportMessage(role="developer", blocks=[para("sin rol conocido")]),
                  ExportMessage(role="", blocks=[para("sin rol")], timestamp="mañana")]))
    text = document_text(data)
    assert "Developer" in text
    assert "sin rol conocido" in text and "sin rol" in text


# --------------------------------------------------------------------------
# scale and dependency handling
# --------------------------------------------------------------------------


def test_two_hundred_messages_render_in_a_few_seconds():
    messages = []
    for index in range(200):
        role = "user" if index % 2 == 0 else "assistant"
        messages.append(message(
            role,
            [para("Mensaje %d con acentos: configuración, ñ." % index),
             Block(kind="code", lang="python", text="print(%d)\nvalor = %d" % (index, index))],
            timestamp="2026-08-31T20:%02d:00" % (index % 60)))
    started = time.time()
    data = docx_export.render(make_transcript(messages))
    elapsed = time.time() - started
    assert elapsed < 20, "200 messages took %.1fs" % elapsed
    text = document_text(data)
    assert "Mensaje 0 con" in text and "Mensaje 199 con" in text


def test_missing_python_docx_raises_export_unavailable_naming_the_package(monkeypatch):
    monkeypatch.setattr(docx_export, "_DX", None)
    monkeypatch.setitem(sys.modules, "docx.shared", None)
    with pytest.raises(ExportUnavailable) as excinfo:
        docx_export.render(make_transcript([message("user", [para("hola")])]))
    assert "python-docx" in str(excinfo.value)
    assert "pip install" in str(excinfo.value)


def test_missing_python_docx_is_not_a_bare_import_error(monkeypatch):
    monkeypatch.setattr(docx_export, "_DX", None)
    monkeypatch.setitem(sys.modules, "docx.enum.style", None)
    with pytest.raises(ExportUnavailable):
        docx_export.render(make_transcript())
    assert issubclass(ExportUnavailable, RuntimeError)


# --------------------------------------------------------------------------
# awkward shapes
# --------------------------------------------------------------------------


def test_empty_and_malformed_blocks_are_skipped_without_raising():
    blocks = [
        Block(kind="para"),
        Block(kind="list", items=[]),
        Block(kind="table", rows=[]),
        Block(kind="quote", children=[]),
        Block(kind="code", text=""),
        Block(kind="heading", level=99, spans=[Span("nivel absurdo")]),
        Block(kind="marciano", text="tipo desconocido"),
        para("y el resto sigue"),
    ]
    data = docx_export.render(make_transcript([message("assistant", blocks)]))
    text = document_text(data)
    assert "nivel absurdo" in text
    assert "tipo desconocido" in text
    assert "y el resto sigue" in text
    # Word only ships Heading 1-9 as outline styles; we clamp to 3
    assert "Heading3" in paragraph_styles(data)


def test_deeply_nested_lists_clamp_to_the_styles_word_provides():
    innermost = Block(kind="list", items=[[para("n4")]])
    third = Block(kind="list", items=[[para("n3"), innermost]])
    second = Block(kind="list", items=[[para("n2"), third]])
    first = Block(kind="list", ordered=True, items=[[para("n1"), second]])
    data = docx_export.render(make_transcript([message("assistant", [first])]))
    styles = paragraph_styles(data)
    assert "ListNumber" in styles
    assert "ListBullet2" in styles and "ListBullet3" in styles
    assert not [name for name in styles if name.startswith("ListBullet4")]
    text = document_text(data)
    for level in ("n1", "n2", "n3", "n4"):
        assert level in text


def test_a_quote_wrapping_a_code_block_keeps_both():
    code = Block(kind="code", text="\n".join("linea %d" % i for i in range(500)))
    data = docx_export.render(make_transcript([
        message("assistant", [Block(kind="quote", children=[code])])]))
    text = document_text(data)
    assert "linea 0" in text and "linea 499" in text
