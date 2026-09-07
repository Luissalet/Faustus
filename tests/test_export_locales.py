import io
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from src.export_locale import Labels, localized_render
from src.report_export import render_report


def data(lang):
    return {"query": "¿Qué sabemos de las fuentes para este informe?", "report_language": lang,
            "raw_report": "## Resultado\n\nTexto de prueba.", "completed_at": 1772000182,
            "stats": {"Model": "local", "Rounds": 2},
            "sources": [{"url": "https://example.test", "title": "Source A"}]}


@pytest.mark.parametrize("fmt", ["md", "txt", "html"])
@pytest.mark.parametrize("lang,present,absent", [("es", "Exportado desde Faustus", "Exported from Faustus"),
                                              ("en", "Exported from Faustus", "Exportado desde Faustus")])
def test_report_text_formats_follow_explicit_language(fmt, lang, present, absent):
    text = render_report(data(lang), fmt).content.decode("utf-8")
    assert present in text and absent not in text
    assert ("Modelo: local" if lang == "es" else "Model: local") in text
    if fmt == "html":
        assert f'<html lang="{lang}">' in text


@pytest.mark.parametrize("lang,subject,page", [("es", "Documento", "Página "), ("en", "Document", "Page ")])
def test_docx_subject_and_footer_share_report_language(lang, subject, page):
    docx = pytest.importorskip("docx")
    document = docx.Document(io.BytesIO(render_report(data(lang), "docx").content))
    assert document.core_properties.subject == subject
    assert page in " ".join(p.text for p in document.sections[0].footer.paragraphs)


@pytest.mark.parametrize("lang,subject,page", [("es", "Documento", "Página 1 de"), ("en", "Document", "Page 1 of")])
def test_pdf_subject_and_footer_share_report_language(lang, subject, page):
    pytest.importorskip("reportlab")
    pypdf = pytest.importorskip("pypdf")
    pdf = pypdf.PdfReader(io.BytesIO(render_report(data(lang), "pdf").content))
    assert pdf.metadata.subject == subject
    assert page in pdf.pages[0].extract_text()


def test_export_locales_are_isolated_across_concurrent_jobs_and_failures():
    import threading
    labels = Labels({"model": "Model"})
    barrier = threading.Barrier(2)
    @localized_render
    def run(transcript):
        barrier.wait(timeout=5)
        return labels["model"]
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(run, SimpleNamespace(extra={"language": lang})) for lang in ("es", "en")]
        assert [f.result(timeout=6) for f in futures] == ["Modelo", "Model"]
    @localized_render
    def fail(transcript):
        assert labels["model"] == "Modelo"
        raise RuntimeError("render failed")
    with pytest.raises(RuntimeError):
        fail(SimpleNamespace(extra={"language": "es"}))
    assert labels["model"] == "Model"


def test_txt_preserves_deep_list_and_code_indentation():
    from src.chat_export import blocks_to_txt
    from src.chat_export_model import Block, Span
    leaf = Block(kind="list", items=[[Block(kind="para", spans=[Span(text="third")])]])
    child = Block(kind="list", items=[[Block(kind="para", spans=[Span(text="second")]), leaf]])
    root = Block(kind="list", items=[[Block(kind="para", spans=[Span(text="first")]), child,
                                      Block(kind="code", text="if ready:\n    run()")]])
    text = blocks_to_txt([root])
    assert "- first\n    - second\n        - third" in text
    assert "        if ready:\n            run()" in text
