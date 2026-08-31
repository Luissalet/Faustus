"""Tests for the reportlab PDF chat exporter.

These check the *bytes*, not just that render() returned: every case reopens
the produced PDF with pypdf, counts pages and asserts the conversation text is
really in there. The regressions they pin down are the ones a PDF exporter
actually fails at:

  * reportlab's Paragraph markup dialect eating a user's literal ``<b>``,
  * a 2 000-character URL or a minified code line running off the page,
  * Helvetica being Latin-1 only, so accents and "ñ" - which is most of what
    this user writes - come out wrong, and an emoji aborts the export.

The transcripts are built by hand from chat_export_model rather than through
build_transcript(), so the renderer is tested against the *contract*.
"""

import io
import sys
import time
from datetime import datetime

import pypdf
import pytest

from src import chat_export_pdf as pdf
from src.chat_export_model import (
    Block,
    ExportMessage,
    ExportUnavailable,
    Span,
    ToolCall,
    Transcript,
)


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


def read_pdf(data):
    assert data[:5] == b"%PDF-", "not a PDF"
    return pypdf.PdfReader(io.BytesIO(data))


def pdf_text(data):
    reader = read_pdf(data)
    return "\n".join(page.extract_text() for page in reader.pages)


def flat(text):
    """Collapse whitespace: pypdf reports a wrapped cell as "Columna\n0"."""
    return " ".join(text.split())


def worst_overflow(markup, style, width):
    """Least slack left on any wrapped line: negative means it overflows.

    Uses reportlab's own line breaker, which is the only authority on whether
    a line fits - measuring the extracted text would measure our guess instead.
    """
    from reportlab.platypus import Paragraph

    paragraph = Paragraph(markup, style)
    paragraph.wrap(width, 100000)
    slack = []
    for line in paragraph.blPara.lines:
        slack.append(line[0] if isinstance(line, tuple) else line.extraSpace)
    return min(slack) if slack else 0.0


def context():
    """A rendering context matching what render() builds internally."""
    kit = pdf._font_kit()
    page_width = pdf._rl().A4[0]
    return pdf._Ctx(kit=kit, styles=pdf._build_styles(kit),
                    width=page_width - 2 * pdf.PAGE_MARGIN_X)


# --------------------------------------------------------------------------
# basics
# --------------------------------------------------------------------------


def test_render_returns_a_readable_pdf_with_the_conversation_in_it():
    transcript = make_transcript([
        message("user", [para("¿Cuál es la capital de España?")],
                timestamp="2026-08-31T20:10:00"),
        message("assistant", [para("Madrid.")], timestamp="2026-08-31T20:10:04",
                model="claude-opus-5"),
    ])
    data = pdf.render(transcript)
    reader = read_pdf(data)
    assert len(reader.pages) == 1
    text = pdf_text(data)
    assert "¿Cuál es la capital de España?" in text
    assert "Madrid." in text
    # role and timestamp are on the page, not just in the model
    assert "User" in text and "Assistant" in text
    assert "2026-08-31 20:10" in text


def test_header_carries_name_model_and_export_date():
    transcript = make_transcript([message("user", [para("hola")])],
                                 name="Despliegue TLS", session_id="s-42",
                                 project="Faustus")
    text = pdf_text(pdf.render(transcript))
    assert "Despliegue TLS" in text
    assert "claude-opus-5" in text
    assert "2026-08-31 20:15" in text
    assert "s-42" in text and "Faustus" in text


def test_page_footer_numbers_every_page():
    long_block = [para("Párrafo número %d. " % i * 12) for i in range(120)]
    data = pdf.render(make_transcript([message("assistant", long_block)]))
    reader = read_pdf(data)
    total = len(reader.pages)
    assert total > 1
    for index, page in enumerate(reader.pages, start=1):
        assert "Page %d of %d" % (index, total) in page.extract_text()


def test_empty_transcript_still_produces_a_valid_one_page_pdf():
    data = pdf.render(make_transcript([], name="Sin mensajes"))
    reader = read_pdf(data)
    assert len(reader.pages) == 1
    text = pdf_text(data)
    assert "Sin mensajes" in text
    assert "no messages" in text


# --------------------------------------------------------------------------
# the reportlab markup trap
# --------------------------------------------------------------------------


def test_literal_markup_from_the_user_is_escaped_not_interpreted():
    """A Paragraph parses a mini-HTML: unescaped chat text would be markup."""
    hostile = "Usa <b>negrita</b> y <font color='red'>rojo</font> & <i>cursiva</i>"
    data = pdf.render(make_transcript([message("user", [para(hostile)])]))
    text = pdf_text(data)
    assert "<b>negrita</b>" in text
    assert "<font color='red'>rojo</font>" in text
    assert "&" in text and "<i>cursiva</i>" in text


def test_bare_angle_brackets_do_not_abort_the_export():
    """`a < b` is a parse error for reportlab's paraparser if left unescaped."""
    text = pdf_text(pdf.render(make_transcript([
        message("user", [para("si a < b && b > c, entonces a < c")]),
        message("assistant", [Block(kind="code", lang="c",
                                    text="if (a < b && b > c) { /* <ok> */ }")]),
    ])))
    assert "si a < b && b > c, entonces a < c" in text
    assert "if (a < b && b > c)" in text


def test_markup_is_escaped_everywhere_not_only_in_paragraphs():
    """Headings, tables, tool calls and the chat name go through the same gate."""
    hostile = "<b>x</b>"
    transcript = make_transcript(
        [message("assistant",
                 [Block(kind="heading", level=2, spans=[Span(hostile)]),
                  Block(kind="table", header=True,
                        rows=[[[Span(hostile)]], [[Span(hostile)]]]),
                  Block(kind="quote", children=[para(hostile)])],
                 tool_calls=[ToolCall(name=hostile, arguments=hostile, result=hostile)],
                 attachments=[hostile])],
        name=hostile)
    text = pdf_text(pdf.render(transcript))
    assert text.count("<b>x</b>") >= 6


def test_span_styles_survive_escaping():
    spans = [Span("negrita", bold=True), Span(" "), Span("cursiva", italic=True),
             Span(" "), Span("code<T>", code=True), Span(" "),
             Span("tachado", strike=True)]
    text = pdf_text(pdf.render(make_transcript([message("assistant", [para(*spans)])])))
    for word in ("negrita", "cursiva", "code<T>", "tachado"):
        assert word in text


# --------------------------------------------------------------------------
# unicode
# --------------------------------------------------------------------------


def test_spanish_accents_greek_cjk_and_emoji_never_break_the_export():
    """Accents and ñ must render; exotic scripts must degrade, not raise."""
    transcript = make_transcript(
        [message("user", [para("Configuración: ñandú, ¿qué tal? áéíóúü ÁÉÍÓÚÑ ¡vale!")]),
         message("assistant", [para("Griego αβγ, cirílico джип, CJK 日本語, emoji 😀🚀"),
                               Block(kind="code", text="# 日本語 コメント\nx = '😀'")])],
        name="Título con ñ, tildes áé y un emoji 😀")
    data = pdf.render(transcript)
    text = pdf_text(data)
    assert "Configuración: ñandú, ¿qué tal? áéíóúü ÁÉÍÓÚÑ ¡vale!" in text
    assert "Título con ñ, tildes áé" in text
    # Whatever happened to the emoji, the surrounding prose is intact and the
    # text layer is still parseable (pypdf got this far).
    assert "Griego" in text and "emoji" in text


def test_astral_characters_are_substituted_so_the_text_layer_stays_valid():
    """reportlab writes a malformed /ToUnicode entry for codepoints > U+FFFF.

    makeToUnicodeCMap() formats the destination with "%04X", which emits five
    hex digits for e.g. U+1F600 - an odd-length hex string that breaks text
    extraction for every string in that font. We substitute instead.
    """
    data = pdf.render(make_transcript([message("user", [para("hola 😀 adiós")])]))
    text = pdf_text(data)              # would raise binascii.Error if emitted
    assert "hola" in text and "adiós" in text
    assert "😀" not in text
    assert pdf.UNSUPPORTED_CHAR in text


def test_falls_back_to_helvetica_without_raising_when_no_ttf_exists(monkeypatch):
    """A stripped-down box has no font files; accents must still survive."""
    monkeypatch.setattr(pdf, "_font_index", lambda: {})
    monkeypatch.setattr(pdf, "_FONT_KIT", None)
    kit = pdf._font_kit()
    assert kit.body == pdf.BUILTIN_SANS and kit.mono == pdf.BUILTIN_MONO
    text = pdf_text(pdf.render(make_transcript([
        message("user", [para("ñandú áéíóú y 日本語 y 😀")])])))
    assert "ñandú áéíóú" in text          # Latin-1 is inside Helvetica
    assert pdf.UNSUPPORTED_CHAR in text   # everything else was substituted


def test_control_characters_are_stripped():
    data = pdf.render(make_transcript([
        message("user", [para("antes\x00\x07medio\x1fdespués")])]))
    assert "antesmediodespués" in pdf_text(data)


# --------------------------------------------------------------------------
# things that overflow
# --------------------------------------------------------------------------


def test_a_2000_character_url_wraps_instead_of_running_off_the_page():
    url = "https://ejemplo.es/" + "a" * 2000
    ctx = context()
    width = ctx.width - pdf.MESSAGE_INDENT
    markup = pdf._markup(ctx.kit, url)
    assert worst_overflow(markup, ctx.styles["body"], width) >= -0.5

    started = time.time()
    data = pdf.render(make_transcript([message("user", [para(url)])]))
    assert time.time() - started < 10       # no pathological line-breaking
    reader = read_pdf(data)
    assert 1 <= len(reader.pages) <= 3
    assert "aaaaaaaaaa" in pdf_text(data)


def test_a_long_unbroken_code_line_is_wrapped_by_character():
    ctx = context()
    columns = pdf._mono_cols(ctx, "code", ctx.width - pdf.MESSAGE_INDENT)
    line = "x" * 5000
    wrapped = pdf._wrap_code(line, columns)
    assert len(wrapped.splitlines()) >= 5000 // columns
    assert max(len(part) for part in wrapped.splitlines()) <= columns
    # and the wrapped result really fits the code frame
    assert worst_overflow(pdf._markup(ctx.kit, wrapped, ctx.kit.mono),
                          ctx.styles["code"], ctx.width - pdf.MESSAGE_INDENT) >= -0.5


def test_code_wrapping_keeps_indentation_and_blank_lines():
    source = "def f():\n    if x:\n        return 1\n\n    return 0"
    assert pdf._wrap_code(source, 120) == source
    text = pdf_text(pdf.render(make_transcript([
        message("assistant", [Block(kind="code", lang="python", text=source)])])))
    assert "        return 1" in text


def test_a_500_line_code_block_paginates():
    source = "\n".join("linea_%03d = compute(%d)  # comentario" % (i, i) for i in range(500))
    data = pdf.render(make_transcript([
        message("assistant", [Block(kind="code", lang="python", text=source)])]))
    reader = read_pdf(data)
    assert len(reader.pages) >= 5           # it split across pages by itself
    text = pdf_text(data)
    assert "linea_000 = compute(0)" in text
    assert "linea_499 = compute(499)" in text


def test_a_ten_column_table_fits_the_page():
    header = [[Span("Columna %d" % i)] for i in range(10)]
    rows = [header] + [[[Span("v%d-%d" % (r, c))] for c in range(10)] for r in range(4)]
    block = Block(kind="table", header=True, rows=rows)
    ctx = context()
    width = ctx.width - pdf.MESSAGE_INDENT
    table = pdf._table_flowable(ctx, block, width)
    assert sum(table._colWidths) <= width + 0.5
    text = flat(pdf_text(pdf.render(make_transcript([message("assistant", [block])]))))
    assert "Columna 0" in text and "Columna 9" in text
    assert "v3-7" in text


def test_ragged_table_rows_do_not_crash():
    rows = [[[Span("a")], [Span("b")], [Span("c")]], [[Span("solo")]], []]
    text = flat(pdf_text(pdf.render(make_transcript([
        message("assistant", [Block(kind="table", header=True, rows=rows)])]))))
    assert "solo" in text


# --------------------------------------------------------------------------
# structure
# --------------------------------------------------------------------------


def test_lists_headings_quotes_and_rules_all_render():
    blocks = [
        Block(kind="heading", level=1, spans=[Span("Título")]),
        Block(kind="heading", level=3, spans=[Span("Subtítulo")]),
        Block(kind="list", ordered=True, items=[
            [para("primero")],
            [para("segundo"), Block(kind="list", items=[[para("anidado")]])],
        ]),
        Block(kind="list", items=[[para("viñeta")]]),
        Block(kind="quote", children=[para("citado")]),
        Block(kind="hr"),
        Block(kind="image", href="https://example.com/i.png", spans=[Span("un diagrama")]),
    ]
    text = pdf_text(pdf.render(make_transcript([message("assistant", blocks)])))
    for expected in ("Título", "Subtítulo", "primero", "segundo", "anidado",
                     "viñeta", "citado", "un diagrama"):
        assert expected in text


def test_tool_calls_are_rendered_with_name_arguments_and_result():
    call = ToolCall(name="run_shell", arguments='{"cmd": "ls -la"}',
                    result="total 8\ndrwxr-xr-x", status="ok", duration_s=0.42)
    text = pdf_text(pdf.render(make_transcript([
        message("assistant", [para("Ejecuto esto:")], tool_calls=[call])])))
    assert "run_shell" in text
    assert '"cmd": "ls -la"' in text
    assert "drwxr-xr-x" in text
    assert "0.42s" in text


def test_huge_tool_output_is_truncated_rather_than_flooding_the_document():
    call = ToolCall(name="dump", result="x" * 50000)
    data = pdf.render(make_transcript([message("assistant", [], tool_calls=[call])]))
    assert len(read_pdf(data).pages) <= 3
    assert "truncated" in pdf_text(data)


def test_attachments_are_listed():
    text = pdf_text(pdf.render(make_transcript([
        message("user", [para("mira esto")], attachments=["captura.png", "informe.pdf"])])))
    assert "captura.png" in text and "informe.pdf" in text


def test_only_external_urls_become_link_annotations():
    """A bare or javascript: href must not become a PDF action.

    Besides the obvious, reportlab reads a scheme-less href as an *internal*
    destination and raises "undefined destination" when saving.
    """
    spans = [Span("bueno", href="https://example.com/ok"), Span(" "),
             Span("malo", href="javascript:alert(1)"), Span(" "),
             Span("relativo", href="../otra/pagina.html")]
    data = pdf.render(make_transcript([message("assistant", [para(*spans)])]))
    page = read_pdf(data).pages[0]
    uris = [a.get_object()["/A"]["/URI"] for a in (page.get("/Annots") or [])]
    assert uris == ["https://example.com/ok"]
    text = pdf_text(data)
    assert "malo" in text and "relativo" in text      # text kept, link dropped


def test_unknown_roles_and_missing_metadata_are_tolerated():
    text = pdf_text(pdf.render(Transcript(
        name="", model="", exported_at=datetime(2026, 1, 1),
        messages=[ExportMessage(role="developer", blocks=[para("sin rol conocido")]),
                  ExportMessage(role="", blocks=[para("sin rol")], timestamp="mañana")])))
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
    data = pdf.render(make_transcript(messages))
    elapsed = time.time() - started
    assert elapsed < 20, "200 messages took %.1fs" % elapsed
    reader = read_pdf(data)
    assert len(reader.pages) > 10
    assert "Mensaje 199" in pdf_text(data)


def test_missing_reportlab_raises_export_unavailable_naming_the_package(monkeypatch):
    monkeypatch.setattr(pdf, "_RL", None)
    monkeypatch.setitem(sys.modules, "reportlab.platypus", None)
    with pytest.raises(ExportUnavailable) as excinfo:
        pdf.render(make_transcript([message("user", [para("hola")])]))
    assert "reportlab" in str(excinfo.value)
    assert "pip install" in str(excinfo.value)


def test_missing_reportlab_is_not_a_bare_import_error(monkeypatch):
    monkeypatch.setattr(pdf, "_RL", None)
    monkeypatch.setitem(sys.modules, "reportlab.platypus", None)
    with pytest.raises(ExportUnavailable):
        pdf.render(make_transcript())
    assert issubclass(ExportUnavailable, RuntimeError)


# --------------------------------------------------------------------------
# awkward shapes
# --------------------------------------------------------------------------


def test_a_quote_wrapping_a_huge_code_block_still_paginates():
    """A quote is a one-row Table; without splitInRow the row would be taller
    than a page and reportlab would raise a LayoutError."""
    code = Block(kind="code", text="\n".join("linea %d" % i for i in range(500)))
    data = pdf.render(make_transcript([
        message("assistant", [Block(kind="quote", children=[code])])]))
    reader = read_pdf(data)
    assert len(reader.pages) >= 5
    assert "linea 499" in pdf_text(data)


def test_a_table_taller_than_a_page_repeats_its_header():
    rows = [[[Span("col %d" % c)] for c in range(4)]]
    rows += [[[Span("fila %d columna %d con texto de relleno" % (r, c))]
              for c in range(4)] for r in range(200)]
    data = pdf.render(make_transcript([
        message("assistant", [Block(kind="table", header=True, rows=rows)])]))
    reader = read_pdf(data)
    assert len(reader.pages) > 3
    for page in reader.pages[1:]:
        assert "col 0" in flat(page.extract_text())


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
    text = pdf_text(pdf.render(make_transcript([message("assistant", blocks)])))
    assert "nivel absurdo" in text
    assert "tipo desconocido" in text
    assert "y el resto sigue" in text


def test_deeply_nested_lists_render():
    innermost = Block(kind="list", items=[[para("n4")]])
    third = Block(kind="list", items=[[para("n3"), innermost]])
    second = Block(kind="list", items=[[para("n2"), third]])
    first = Block(kind="list", ordered=True, items=[[para("n1"), second]])
    text = pdf_text(pdf.render(make_transcript([message("assistant", [first])])))
    for level in ("n1", "n2", "n3", "n4"):
        assert level in text
