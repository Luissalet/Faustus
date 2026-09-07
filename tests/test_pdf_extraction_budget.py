"""Bound attachment preprocessing before spending local model time."""
from types import SimpleNamespace
from unittest.mock import Mock
import sys

import pytest

from src import document_processor as dp


class Page:
    def __init__(self, text='', images=()):
        self.extract_text = Mock(return_value=text)
        self._images = images
        self.images_read = 0

    @property
    def images(self):
        self.images_read += 1
        return self._images


def reader(monkeypatch, pages):
    monkeypatch.setitem(sys.modules, 'pypdf', SimpleNamespace(PdfReader=lambda _: SimpleNamespace(pages=pages)))


def test_stops_extracting_after_inline_text_budget(monkeypatch):
    pages = [Page('A' * 16000), Page('Must not extract discarded text')]
    reader(monkeypatch, pages)
    text = dp._process_pdf('input.pdf')
    pages[1].extract_text.assert_not_called()
    assert pages[0].images_read == 0
    assert 'truncated' in text
    assert len(text) < 15500


def test_text_pages_never_decode_embedded_images(monkeypatch):
    pages = [Page('Readable prose ' * 20)]
    reader(monkeypatch, pages)
    assert 'Readable prose' in dp._process_pdf('input.pdf')
    assert pages[0].images_read == 0


@pytest.mark.parametrize('answer', ['image words', '', RuntimeError('model unavailable')])
def test_vision_attempts_are_bounded_and_keep_owner(monkeypatch, answer):
    img = SimpleNamespace(image=SimpleNamespace(save=Mock()))
    pages = [Page(images=[img]) for _ in range(20)]
    reader(monkeypatch, pages)
    analyze = Mock(side_effect=answer) if isinstance(answer, Exception) else Mock(return_value=answer)
    monkeypatch.setattr(dp, 'analyze_image_with_vl', analyze)
    text = dp._process_pdf('scan.pdf', owner='alice')
    assert analyze.call_count <= 6
    assert analyze.call_count > 0
    assert all(call.kwargs == {'owner': 'alice'} for call in analyze.call_args_list)
    assert 'limit' in text.lower()
    assert 'no readable content found' not in text.lower(), 'a partial attempt is not proof the PDF is empty'


def test_image_iteration_is_lazy(monkeypatch):
    img = SimpleNamespace(image=SimpleNamespace(save=Mock()))
    def images():
        yield from [img] * 3
        raise AssertionError('decoded a fourth image outside page budget')
    reader(monkeypatch, [Page(images=images())])
    analyze = Mock(return_value='OCR text')
    monkeypatch.setattr(dp, 'analyze_image_with_vl', analyze)
    assert 'OCR text' in dp._process_pdf('scan.pdf')
    assert analyze.call_count == 3


def test_empty_pdf_page_work_is_bounded(monkeypatch):
    pages = [Page() for _ in range(150)]
    reader(monkeypatch, pages)
    text = dp._process_pdf('empty.pdf')
    assert sum(p.extract_text.call_count for p in pages) <= 100
    assert 'limit' in text.lower()


def test_small_empty_pdf_remains_honest(monkeypatch):
    reader(monkeypatch, [Page()])
    assert 'no readable content found' in dp._process_pdf('empty.pdf')
