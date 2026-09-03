"""A finished research report, rendered as a document.

What these pin down is the difference between "the export ran" and "the export
is a document someone can hand to a colleague":

  * a report whose ``stats`` are missing, empty or half-filled must never
    print the literal "None" in its metadata line — the research JSON's every
    field is optional and several are absent on a run that ended early;
  * the md export has to survive being read back (``markdown_to_blocks`` of it
    returns the blocks it was rendered from), because md is the format people
    paste into another tool;
  * the sources appendix is written by two different places now — the
    researcher appends its own "## Fuentes", and this module appends one when
    it doesn't — and the report must never carry both;
  * a Spanish question with accents and a "?" has to become a filename a
    Windows machine will accept;
  * the docx and pdf a colleague actually receives open as a *document* —
    the question, the export date, the report's own metadata line — and not
    as a chat transcript of one message spoken by "Report".

The binary formats are exercised through ``pytest.importorskip`` so the suite
stays green without python-docx / reportlab, and the *unavailable* path is
asserted without them by patching the import.
"""

import io
import json
import re
import zipfile
from datetime import datetime

import pytest

from src.chat_export import DOCUMENT_FLAG, is_document, markdown_to_blocks
from src.chat_export_model import ExportUnavailable
from src.report_export import (
    REPORT_FORMATS,
    available_formats,
    build_report_blocks,
    build_report_transcript,
    ends_with_sources_section,
    render_report,
    report_filename,
)

# The question this feature exists for.
SPANISH_QUERY = "¿Es eficaz la fisioterapia para el dolor lumbar crónico?"

REPORT_BODY = """## Resumen

El **ejercicio terapéutico** es la intervención con mejor evidencia.

- Ejercicio supervisado: eficaz
- Masaje: evidencia débil

### Detalle

| Terapia | Evidencia |
|---|---|
| Ejercicio | Alta |
| Masaje | Baja |

> La guía NICE lo recomienda como primera línea.

Ver la [revisión Cochrane](https://cochrane.org/lbp) para el detalle.
"""


def research_json(**overrides):
    """A research JSON in the shape ``_save_result`` writes to disk."""
    data = {
        "query": SPANISH_QUERY,
        "status": "done",
        "result": "Resultado formateado para el chat",
        "raw_report": REPORT_BODY,
        "sources": [
            {"title": "Cochrane review", "url": "https://cochrane.org/lbp"},
            {"title": "NICE guideline NG59", "url": "https://nice.org.uk/ng59"},
        ],
        "raw_findings": [],
        "stats": {
            "Duration": "182.4s",
            "Rounds": 3,
            "Queries": 9,
            "URLs": 14,
            "Model": "qwen3:14b",
            "Search": "searxng",
            "Category": "Health",
        },
        "category": "health",
        "started_at": 1772000000.0,
        "completed_at": 1772000182.4,
        "owner": "luis",
    }
    data.update(overrides)
    return data


def text_of(data, fmt="md"):
    return render_report(data, fmt).content.decode("utf-8")


def docx_texts(payload):
    """Every <w:t> run in a .docx, in document order."""
    document = zipfile.ZipFile(io.BytesIO(payload)).read("word/document.xml")
    return re.findall(r"<w:t[^>]*>(.*?)</w:t>", document.decode("utf-8"), re.S)


# ---------------------------------------------------------------------------
# the document
# ---------------------------------------------------------------------------

def test_every_format_renders_non_empty_bytes():
    data = research_json()
    for fmt in REPORT_FORMATS:
        if fmt == "docx":
            pytest.importorskip("docx")
        if fmt == "pdf":
            pytest.importorskip("reportlab")
        result = render_report(data, fmt)
        assert result.content, fmt
        assert result.media_type
        assert result.filename.endswith("." + fmt)


def test_document_opens_with_the_question_then_its_metadata():
    md = text_of(research_json())
    lines = [line for line in md.splitlines() if line.strip()]
    assert lines[0] == "# " + SPANISH_QUERY
    assert lines[1].startswith("*Completed ")
    for expected in ("Model: qwen3:14b", "Rounds: 3", "Sources: 2",
                     "Duration: 182.4s", "Category: Health"):
        assert expected in lines[1]


def test_footer_names_faustus_and_the_export_time():
    md = text_of(research_json())
    footer = [line for line in md.splitlines() if line.strip()][-1]
    assert "Faustus" in footer
    assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", footer)


def test_body_comes_from_raw_report():
    md = text_of(research_json())
    assert "## Resumen" in md
    assert "El **ejercicio terapéutico** es la intervención con mejor evidencia." in md
    assert "Resultado formateado para el chat" not in md


def test_empty_raw_report_falls_back_to_result():
    md = text_of(research_json(raw_report="", stats={}))
    assert "Resultado formateado para el chat" in md


# ---------------------------------------------------------------------------
# missing metadata must not print "None"
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("overrides", [
    {"stats": None},
    {"stats": {}},
    {"stats": {"Rounds": 2}},
    {"stats": {"Model": "qwen3:14b"}, "category": None, "completed_at": None},
    {"sources": None, "stats": None, "category": None},
    {"completed_at": None, "started_at": None},
])
def test_absent_metadata_never_renders_as_none(overrides):
    md = text_of(research_json(**overrides))
    assert "None" not in md
    assert ": *" not in md          # a label left dangling with no value
    assert "· ·" not in md


def test_a_report_with_no_completion_time_claims_none():
    """The start time must not be printed as the completion time."""
    data = research_json(completed_at=None)
    md = text_of(data)
    assert "Completed" not in md
    # The filename still gets a stamp, from the start time.
    assert report_filename(data, "md") == "research_Es_eficaz_la_fisioterapia_" \
                                          "para_el_dolor_lumbar_cronico_20260225_061320.md"


def test_a_report_with_nothing_but_a_question_still_renders():
    md = text_of({"query": "Solo la pregunta"})
    assert md.startswith("# Solo la pregunta")
    assert "None" not in md
    assert "Faustus" in md


def test_missing_sources_produce_no_appendix():
    md = text_of(research_json(sources=None))
    assert "## Sources" not in md
    assert "Sources:" not in md      # the metadata count goes too


def test_sources_appendix_is_a_numbered_list_of_links():
    md = text_of(research_json())
    assert "## Sources" in md
    assert "1. [Cochrane review](https://cochrane.org/lbp)" in md
    assert "2. [NICE guideline NG59](https://nice.org.uk/ng59)" in md


def test_a_source_without_a_url_still_lists_its_title():
    md = text_of(research_json(sources=[{"title": "Informe interno"}]))
    assert "1. Informe interno" in md


# ---------------------------------------------------------------------------
# never two sources sections
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("heading", [
    "## Fuentes", "## Sources", "# Referencias", "### references",
    "##  FUENTES", "## Fuentes consultadas",
])
def test_body_that_already_ends_in_sources_gets_no_second_appendix(heading):
    body = "Cuerpo del informe.\n\n%s\n\n- [A](https://a.test)\n" % heading
    md = text_of(research_json(raw_report=body))
    headings = re.findall(
        r"^#{1,3}\s*(?:fuentes|sources|referencias|references)\b",
        md, re.IGNORECASE | re.MULTILINE,
    )
    assert len(headings) == 1
    assert "Cochrane review" not in md      # our appendix stayed out entirely


def test_a_sources_heading_that_is_not_last_does_not_suppress_the_appendix():
    body = "## Sources of error\n\nTexto.\n\n## Conclusión\n\nFin.\n"
    md = text_of(research_json(raw_report=body))
    assert "## Sources\n" in md
    assert "1. [Cochrane review](https://cochrane.org/lbp)" in md


def test_a_sources_heading_inside_a_code_fence_does_not_suppress_the_appendix():
    body = "Cuerpo.\n\n```markdown\n## Fuentes\n```\n"
    md = text_of(research_json(raw_report=body))
    assert "## Sources" in md


def test_ends_with_sources_section_ignores_a_body_with_no_headings():
    assert ends_with_sources_section(markdown_to_blocks("Solo un párrafo.")) is False


# ---------------------------------------------------------------------------
# md round-trip
# ---------------------------------------------------------------------------

def test_markdown_round_trips_to_the_same_blocks():
    """The md export re-parses to the document it was rendered from.

    Rendered and re-parsed at a pinned ``exported_at`` because the footer
    carries the export time, which would otherwise differ by a second between
    the two calls.
    """
    data = research_json()
    exported_at = datetime(2026, 9, 3, 12, 0, 0)
    blocks = build_report_blocks(data, exported_at=exported_at)

    import src.report_export as report_export
    md = report_export._render_markdown(blocks) + "\n"

    assert markdown_to_blocks(md) == blocks


def test_round_trip_survives_a_list_that_follows_a_paragraph():
    """The case the shared markdown serializer gets wrong.

    ``chat_export._blocks_to_md`` keeps a list tight against its introducing
    paragraph; ``sane_lists`` then reads the bullets as part of the paragraph,
    and the list is gone. Reports are mostly prose-then-bullets, so this is the
    common shape, not an edge case.
    """
    body = "Los hallazgos principales:\n\n- Uno\n- Dos\n"
    data = research_json(raw_report=body, sources=None)
    blocks = build_report_blocks(data, exported_at=datetime(2026, 9, 3, 12, 0, 0))

    import src.report_export as report_export
    reparsed = markdown_to_blocks(report_export._render_markdown(blocks))

    assert [b.kind for b in reparsed] == [b.kind for b in blocks]
    assert any(b.kind == "list" and len(b.items) == 2 for b in reparsed)


# ---------------------------------------------------------------------------
# filenames
# ---------------------------------------------------------------------------

def test_filename_is_ascii_safe_for_the_spanish_question():
    name = report_filename(research_json(), "docx")
    assert name == ("research_Es_eficaz_la_fisioterapia_para_el_dolor_lumbar_cronico"
                    "_20260225_061622.docx")
    assert name.isascii()
    assert re.fullmatch(r"[A-Za-z0-9._-]+", name)


def test_filename_slug_is_capped_and_stamped():
    data = research_json(query="palabra " * 40)
    name = report_filename(data, "pdf")
    stem = name[len("research_"):-len("_20260225_061622.pdf")]
    assert len(stem) <= 60
    assert name.endswith("_20260225_061622.pdf")


def test_filename_survives_a_query_that_is_only_punctuation():
    name = report_filename({"query": "¿¿¿???", "completed_at": 1772000182.4}, "md")
    assert name == "research_20260225_061622.md"


def test_two_exports_of_one_report_get_the_same_name():
    data = research_json()
    assert report_filename(data, "md") == report_filename(data, "md")


def test_caller_supplied_filename_wins_and_gets_an_extension():
    result = render_report(research_json(), "md", filename="informe lumbar")
    assert result.filename == "informe_lumbar.md"


# ---------------------------------------------------------------------------
# formats
# ---------------------------------------------------------------------------

def test_unknown_format_raises_value_error_naming_the_supported_ones():
    with pytest.raises(ValueError) as excinfo:
        render_report(research_json(), "rtf")
    for fmt in REPORT_FORMATS:
        assert fmt in str(excinfo.value)


def test_format_aliases_resolve():
    assert render_report(research_json(), "markdown").filename.endswith(".md")
    assert render_report(research_json(), "TXT").filename.endswith(".txt")


def test_html_is_a_standalone_document_without_the_chat_frame():
    html = text_of(research_json(), "html")
    assert html.startswith("<!DOCTYPE html>")
    assert "<style>" in html                      # stylesheet embedded, nothing fetched
    assert "Conversation" not in html
    assert 'class="role"' not in html
    assert "<h1>" in html and "Resumen" in html


def test_html_escapes_a_report_that_contains_markup():
    data = research_json(raw_report="<script>alert(1)</script> y **texto**")
    html = text_of(data, "html")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_txt_reads_without_a_renderer():
    txt = text_of(research_json(), "txt")
    assert SPANISH_QUERY in txt
    assert "Ejercicio supervisado: eficaz" in txt
    assert "Faustus" in txt


def test_json_carries_the_research_metadata_and_the_block_model():
    payload = json.loads(text_of(research_json(), "json"))
    assert payload["name"] == SPANISH_QUERY
    assert payload["extra"]["kind"] == "research_report"
    assert payload["extra"]["model"] == "qwen3:14b"
    assert len(payload["extra"]["sources"]) == 2
    assert payload["messages"][0]["blocks"][0]["kind"] == "heading"


# ---------------------------------------------------------------------------
# the transcript handed to the binary renderers
# ---------------------------------------------------------------------------

def test_the_transcript_is_flagged_as_a_document():
    """The whole fix: the renderers are told this is not a conversation."""
    transcript = build_report_transcript(research_json())
    assert transcript.extra[DOCUMENT_FLAG] is True
    assert is_document(transcript) is True


def test_transcript_holds_one_message_with_a_non_chat_role():
    transcript = build_report_transcript(research_json())
    assert transcript.name == SPANISH_QUERY
    assert len(transcript.messages) == 1
    assert transcript.messages[0].role not in ("user", "assistant", "system", "tool")
    # Left empty on purpose: docx/pdf print these in a header of their own,
    # directly above the report's own metadata line.
    assert transcript.model == ""
    assert transcript.session_id == ""


def test_transcript_rejects_a_non_dict():
    with pytest.raises(TypeError):
        build_report_transcript("not a dict")


def test_title_stays_in_the_body_for_the_text_formats():
    blocks = build_report_transcript(research_json()).messages[0].blocks
    assert blocks[0].kind == "heading" and blocks[0].level == 1


def test_title_leaves_the_body_when_the_renderer_prints_it_itself():
    transcript = build_report_transcript(research_json(), title_in_body=False)
    blocks = transcript.messages[0].blocks
    assert transcript.name == SPANISH_QUERY
    assert not (blocks[0].kind == "heading" and blocks[0].level == 1)
    assert blocks[0].spans[0].italic                # straight to the metadata line


def test_docx_contains_the_report_text():
    pytest.importorskip("docx")
    texts = docx_texts(render_report(research_json(), "docx").content)
    joined = "\n".join(texts)
    assert SPANISH_QUERY in joined
    assert "Resumen" in joined
    assert "Cochrane review" in joined
    # The renderer prints the transcript name as the document title, so the
    # body must not repeat the question straight underneath it.
    assert joined.count(SPANISH_QUERY) == 1
    # No speaker banner over a document that has no speakers.
    assert "Assistant" not in joined
    assert "User" not in joined
    assert "Report" not in joined


def test_docx_opens_with_the_question_the_export_date_and_the_metadata_line():
    """The first three lines of the file someone is handed, in order."""
    pytest.importorskip("docx")
    texts = docx_texts(render_report(research_json(), "docx").content)
    assert texts[0] == SPANISH_QUERY
    assert re.fullmatch(r"Exported \d{4}-\d{2}-\d{2} \d{2}:\d{2}", texts[1])
    assert texts[2].startswith("Completed ")
    # The count of a chat with one turn in it, on a document with no turns.
    assert not any("message" in text for text in texts[:3])


def test_pdf_contains_the_report_text():
    pytest.importorskip("reportlab")
    pypdf = pytest.importorskip("pypdf")
    payload = render_report(research_json(), "pdf").content
    assert payload[:5] == b"%PDF-"
    reader = pypdf.PdfReader(io.BytesIO(payload))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    assert "fisioterapia" in text
    assert "Resumen" in text
    assert "Assistant" not in text
    assert "1 message" not in text
    assert not re.search(r"^\s*Report\s*$", text, re.M)
    assert re.search(r"^Exported \d{4}-\d{2}-\d{2} \d{2}:\d{2}$", text, re.M)


def test_missing_binary_dependency_surfaces_as_export_unavailable(monkeypatch):
    """No python-docx installed: a message naming the package, not a traceback."""
    import builtins

    import src.chat_export_docx as docx_renderer
    monkeypatch.setattr(docx_renderer, "_DX", None)
    real_import = builtins.__import__

    def refuse_docx(name, *args, **kwargs):
        if name == "docx" or name.startswith("docx."):
            raise ImportError("No module named 'docx'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse_docx)

    with pytest.raises(ExportUnavailable) as excinfo:
        render_report(research_json(), "docx")
    assert "python-docx" in str(excinfo.value)


# ---------------------------------------------------------------------------
# availability probe
# ---------------------------------------------------------------------------

def test_available_formats_covers_every_format_and_text_ones_are_always_true():
    formats = available_formats(refresh=True)
    assert set(formats) == set(REPORT_FORMATS)
    for fmt in ("md", "txt", "html", "json"):
        assert formats[fmt] is True
    assert isinstance(formats["docx"], bool)
    assert isinstance(formats["pdf"], bool)


def test_available_formats_is_cached_per_process(monkeypatch):
    import src.report_export as report_export

    first = report_export.available_formats(refresh=True)
    calls = []

    def counted(name):
        calls.append(name)
        raise ImportError(name)

    monkeypatch.setattr(report_export.importlib, "import_module", counted)
    assert report_export.available_formats() == first
    assert calls == []


def test_available_formats_reports_a_missing_package_as_false(monkeypatch):
    import src.report_export as report_export

    monkeypatch.setattr(report_export.importlib, "import_module",
                        lambda name: (_ for _ in ()).throw(ImportError(name)))
    formats = report_export.available_formats(refresh=True)
    assert formats["docx"] is False and formats["pdf"] is False
    assert formats["md"] is True
    report_export.available_formats(refresh=True)   # leave the cache honest
