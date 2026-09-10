"""
tests/test_p1_art06_presentation.py — ART-06, lote 53.

Acceptance line: "Los graficos conservan ejes, unidades y datos; exportar
no aplana todo a imagenes salvo eleccion explicita." Pinned against
`src/presentation.py` two ways: the PPTX path (python-pptx is installed in
this environment — `pip show python-pptx` confirmed before writing this)
produces a file that, reopened, still carries the real numbers and axis
titles through a NATIVE chart object; the always-available HTML path never
touches an image at all. `flatten_chart_to_image` is exercised separately
as the opt-in the acceptance line allows.
"""
from __future__ import annotations

import os

import pytest

from src.presentation import (
    ChartSeries,
    ChartSpec,
    Presentation,
    Slide,
    flatten_chart_to_image,
    render_html_slides,
    render_pptx,
    validate_pptx,
)

pytest.importorskip("pptx", reason="python-pptx not installed in this environment")


def _deck() -> Presentation:
    chart = ChartSpec(
        kind="bar", categories=["Q1", "Q2", "Q3"],
        series=[ChartSeries(name="Revenue", values=[120.0, 150.5, 90.0])],
        title="Quarterly revenue", x_axis_title="Quarter", y_axis_title="Revenue",
        unit="k$",
    )
    return Presentation(title="Board update", slides=[
        Slide(title="Welcome", bullets=["Agenda", "Context"], notes="Keep it brief."),
        Slide(title="Revenue", chart=chart, notes="Q2 beat plan."),
    ])


def test_pptx_export_opens_and_keeps_its_chart_and_slide_count(tmp_path):
    deck = _deck()
    path = str(tmp_path / "board.pptx")
    render_pptx(deck, path)
    assert os.path.exists(path) and os.path.getsize(path) > 0

    report = validate_pptx(path, deck)
    assert report["ok"] is True, report["problems"]
    assert report["slides"] == 2
    assert report["charts_found"] == report["charts_expected"] == 1


def test_pptx_chart_keeps_real_axis_titles_and_data_not_a_picture(tmp_path):
    """Reopen the file and read the chart's OWN numbers back out — not
    trusting `render_pptx`'s return value, the same discipline
    `validate_pptx` applies."""
    from pptx import Presentation as PptxPresentation

    deck = _deck()
    path = str(tmp_path / "board.pptx")
    render_pptx(deck, path)

    reopened = PptxPresentation(path)
    revenue_slide = reopened.slides[1]
    chart_shapes = [s for s in revenue_slide.shapes if getattr(s, "has_chart", False)]
    assert len(chart_shapes) == 1
    chart = chart_shapes[0].chart
    plot = chart.plots[0]
    categories = list(plot.categories)
    assert categories == ["Q1", "Q2", "Q3"]
    series = list(chart.series)[0]
    assert list(series.values) == [120.0, 150.5, 90.0]
    assert "Revenue" in chart.value_axis.axis_title.text_frame.text
    assert "k$" in chart.value_axis.axis_title.text_frame.text
    assert chart.category_axis.axis_title.text_frame.text == "Quarter"


def test_html_fallback_never_touches_an_image_and_keeps_the_data():
    deck = _deck()
    out = render_html_slides(deck)
    assert "<img" not in out and "base64" not in out
    assert "<svg" in out                       # a real drawing, not a raster
    assert "<table" in out
    assert "120.0" in out and "150.5" in out and "90.0" in out
    assert "k$" in out
    assert "Quarter" in out and "Revenue" in out


def test_flattening_to_an_image_is_an_explicit_opt_in(tmp_path):
    """Never invoked by `render_pptx`/`render_html_slides` on their own —
    only a caller that names it gets a picture."""
    pytest.importorskip("matplotlib", reason="matplotlib not installed in this environment")
    chart = _deck().slides[1].chart
    out_path = str(tmp_path / "chart.png")
    flatten_chart_to_image(chart, out_path)
    assert os.path.exists(out_path) and os.path.getsize(out_path) > 0


def test_a_mismatched_series_length_is_rejected_up_front():
    with pytest.raises(ValueError):
        ChartSpec(kind="bar", categories=["Q1", "Q2"],
                 series=[ChartSeries(name="Revenue", values=[1.0])])


def test_an_unknown_chart_kind_is_rejected():
    with pytest.raises(ValueError):
        ChartSpec(kind="scatter3d", categories=["a"], series=[ChartSeries(name="x", values=[1.0])])
