"""QA-40 · PDF escaneado (docs/spec/v2/acceptance_scenarios.json).

Estimulo: PDF sin texto y con tabla en imagen.
Resultado exigido (literal): "Lectura visual/OCR opcional con paginas y
limitaciones, no documento vacio inventado."

Requisitos: ART-07.

Estado: verde. `src/document_processor.py::_process_pdf` ya detecta paginas
sin texto extraible (`page_text` corto) y cae a lectura visual via
`analyze_image_with_vl` sobre las imagenes incrustadas, anotando de que
pagina viene cada lectura. Este test construye un PDF real de una sola
pagina, SIN texto, con una imagen incrustada (como una pagina escaneada), y
comprueba que `_process_pdf` usa la ruta visual en vez de devolver un
documento vacio - `analyze_image_with_vl` se sustituye porque requiere un
modelo de vision real/red, igual que el resto del repo sustituye el LLM en
sus tests (p.ej. tests/e2e/fake_llm.py), no el mecanismo de deteccion bajo
prueba.
"""
import io

import pytest

from src import document_processor as dp

pytestmark = pytest.mark.qa_state("green")


def _make_scanned_pdf(path):
    from PIL import Image
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    img = Image.new("RGB", (300, 300), color=(240, 240, 240))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)

    c = canvas.Canvas(str(path), pagesize=(300, 300))
    c.drawImage(ImageReader(buf), 0, 0, width=300, height=300)
    c.showPage()
    c.save()


def test_a_textless_scanned_pdf_reads_via_vision_instead_of_coming_back_empty(tmp_path, monkeypatch):
    pdf_path = tmp_path / "scanned.pdf"
    _make_scanned_pdf(pdf_path)

    calls = []

    def fake_vl(image_path, owner=None):
        calls.append(image_path)
        return "Table: Item | Qty\nWidget | 12"

    monkeypatch.setattr(dp, "analyze_image_with_vl", fake_vl)

    result = dp._process_pdf(str(pdf_path), owner="luis")

    assert calls, "the scanned page never reached vision OCR"
    assert "Page 1" in result  # the page it came from is named, not silent
    assert "Widget" in result  # the visual reading made it into the output
    assert result.strip() != "", "a scanned PDF must never come back as an invented empty document"
