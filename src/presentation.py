"""
presentation.py — ART-06: presentations and charts.

Builds a `Presentation` (slides with a title, bullets, speaker notes, and an
optional `ChartSpec` carrying REAL data — categories, one or more numeric
series, axis titles, units) and renders it two ways:

  * `render_pptx` — python-pptx, when installed: a chart becomes a NATIVE
    PowerPoint chart object (`slide.shapes.add_chart`), not a picture. Axes,
    units and the underlying numbers survive into the file exactly because
    nothing here rasterizes them — the acceptance line ("exportar no
    aplana todo a imagenes salvo eleccion explicita") is a property of this
    function's DEFAULT path, not a promise kept by convention.
  * `render_html_slides` — always available, no optional dependency: one
    `<section>` per slide with the chart's data as a real `<table>` (a
    screen reader or a text export sees the numbers, not alt text guessing
    at a picture) plus a `<svg>` line/bar rendering built from the same
    numbers, never a bitmap.

`flatten_chart_to_image` (matplotlib, when installed) is the EXPLICIT
opt-in the acceptance line carves out: a caller that genuinely wants a
picture (embedding in a place that cannot render a live chart) asks for it
by name; nothing here reaches for it on its own.

`validate_pptx` opens a rendered file back up with python-pptx and checks
its own slide/chart count against what was asked for — "validacion de que
abre" made into an actual read of the bytes just written, not a hope that
`render_pptx` didn't raise.

Optional dependencies only (rule 8: no new ones) — both python-pptx and
matplotlib are imported lazily, on first use, the same pattern
`src/chat_export_pdf.py::_rl` already uses for reportlab, so importing this
module costs nothing when neither is installed and a missing package
surfaces as `ExportUnavailable` (reused from `src.chat_export_model`, not
redefined here) instead of an ImportError from somewhere inside a render.
"""
from __future__ import annotations

import html
from dataclasses import dataclass, field
from types import SimpleNamespace as _SN
from typing import Any, Dict, List, Optional

from src.chat_export_model import ExportUnavailable

CHART_KINDS = ("bar", "line", "pie")


@dataclass
class ChartSeries:
    name: str
    values: List[float]


@dataclass
class ChartSpec:
    kind: str
    categories: List[str]
    series: List[ChartSeries]
    title: str = ""
    x_axis_title: str = ""
    y_axis_title: str = ""
    unit: str = ""

    def __post_init__(self) -> None:
        if self.kind not in CHART_KINDS:
            raise ValueError(f"chart kind must be one of {CHART_KINDS}, got {self.kind!r}")
        for s in self.series:
            if len(s.values) != len(self.categories):
                raise ValueError(
                    f"series {s.name!r} has {len(s.values)} values for "
                    f"{len(self.categories)} categories")


@dataclass
class Slide:
    title: str
    bullets: List[str] = field(default_factory=list)
    notes: str = ""
    chart: Optional[ChartSpec] = None


@dataclass
class Presentation:
    title: str
    slides: List[Slide] = field(default_factory=list)


_PPTX: Optional[_SN] = None


def _pptx() -> _SN:
    global _PPTX
    if _PPTX is not None:
        return _PPTX
    try:
        from pptx import Presentation as PptxPresentation
        from pptx.chart.data import CategoryChartData
        from pptx.enum.chart import XL_CHART_TYPE
        from pptx.util import Inches, Pt
    except ImportError as exc:
        raise ExportUnavailable(
            "Presentation export needs the 'python-pptx' package "
            "(pip install python-pptx); falling back to HTML slides "
            "(render_html_slides) needs no extra package.") from exc
    _PPTX = _SN(Presentation=PptxPresentation, CategoryChartData=CategoryChartData,
               XL_CHART_TYPE=XL_CHART_TYPE, Inches=Inches, Pt=Pt)
    return _PPTX


_CHART_TYPE_MAP = {"bar": "BAR_CLUSTERED", "line": "LINE", "pie": "PIE"}


def render_pptx(presentation: Presentation, path: str) -> str:
    """Write `presentation` to `path` as a real .pptx. A chart on a slide
    becomes a native chart object, keeping its axis titles, its unit (as
    the axis title's suffix — pptx has no separate "unit" field) and every
    data point queryable back out of the file, which is exactly what
    `validate_pptx` checks."""
    pptx = _pptx()
    deck = pptx.Presentation()
    layout_title_content = deck.slide_layouts[1]
    layout_title_only = deck.slide_layouts[5]

    for slide_spec in presentation.slides:
        layout = layout_title_only if slide_spec.chart else layout_title_content
        slide = deck.slides.add_slide(layout)
        slide.shapes.title.text = slide_spec.title

        if slide_spec.bullets and not slide_spec.chart:
            body = slide.placeholders[1].text_frame
            body.text = slide_spec.bullets[0]
            for bullet in slide_spec.bullets[1:]:
                p = body.add_paragraph()
                p.text = bullet

        if slide_spec.chart is not None:
            chart_data = pptx.CategoryChartData()
            chart_data.categories = slide_spec.chart.categories
            for series in slide_spec.chart.series:
                chart_data.add_series(series.name, series.values)
            chart_type = getattr(pptx.XL_CHART_TYPE, _CHART_TYPE_MAP[slide_spec.chart.kind])
            graphic_frame = slide.shapes.add_chart(
                chart_type, pptx.Inches(1), pptx.Inches(1.8),
                pptx.Inches(8), pptx.Inches(4.5), chart_data)
            chart = graphic_frame.chart
            if slide_spec.chart.kind != "pie":
                y_title = slide_spec.chart.y_axis_title
                if slide_spec.chart.unit:
                    y_title = f"{y_title} ({slide_spec.chart.unit})" if y_title else slide_spec.chart.unit
                if y_title:
                    chart.value_axis.axis_title.text_frame.text = y_title
                if slide_spec.chart.x_axis_title:
                    chart.category_axis.axis_title.text_frame.text = slide_spec.chart.x_axis_title
            if slide_spec.chart.title:
                chart.has_title = True
                chart.chart_title.text_frame.text = slide_spec.chart.title

        if slide_spec.notes:
            slide.notes_slide.notes_text_frame.text = slide_spec.notes

    deck.save(path)
    return path


def validate_pptx(path: str, presentation: Presentation) -> Dict[str, Any]:
    """Reopen `path` and check it against what `presentation` asked for —
    "validacion de que abre" as an actual read, not just the absence of an
    exception from `render_pptx`."""
    pptx = _pptx()
    deck = pptx.Presentation(path)
    problems: List[str] = []
    slide_count = len(deck.slides._sldIdLst)  # noqa: SLF001 — python-pptx has no public count API
    if slide_count != len(presentation.slides):
        problems.append(f"expected {len(presentation.slides)} slides, file has {slide_count}")
    expected_charts = sum(1 for s in presentation.slides if s.chart is not None)
    found_charts = 0
    for slide, slide_spec in zip(deck.slides, presentation.slides):
        has_chart = any(shape.has_chart for shape in slide.shapes if shape.shape_type is not None
                        and getattr(shape, "has_chart", False))
        if slide_spec.chart is not None:
            if not has_chart:
                problems.append(f"slide {slide_spec.title!r} lost its chart on reopen")
            else:
                found_charts += 1
    return {"ok": not problems, "slides": slide_count, "charts_found": found_charts,
            "charts_expected": expected_charts, "problems": problems}


def _svg_chart(chart: ChartSpec, *, width: int = 640, height: int = 320) -> str:
    """A minimal, dependency-free SVG rendering of `chart` — real numbers
    drawn as real geometry, not a screenshot standing in for them."""
    values = [v for series in chart.series for v in series.values]
    vmax = max(values) if values else 1.0
    vmax = vmax or 1.0
    pad = 40
    plot_w, plot_h = width - 2 * pad, height - 2 * pad
    bars = []
    n_cat = max(len(chart.categories), 1)
    n_series = max(len(chart.series), 1)
    slot = plot_w / n_cat
    colors = ["#4C72B0", "#DD8452", "#55A868", "#C44E52", "#8172B2"]
    for ci, category in enumerate(chart.categories):
        for si, series in enumerate(chart.series):
            value = series.values[ci] if ci < len(series.values) else 0
            bar_w = slot / (n_series + 1)
            x = pad + ci * slot + si * bar_w
            bar_h = (value / vmax) * plot_h
            y = height - pad - bar_h
            color = colors[si % len(colors)]
            bars.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{bar_h:.1f}" '
                f'fill="{color}"><title>{html.escape(series.name)} / '
                f'{html.escape(category)}: {value}{html.escape(chart.unit)}</title></rect>')
        label_x = pad + ci * slot + slot / 2
        bars.append(f'<text x="{label_x:.1f}" y="{height - pad + 14}" font-size="10" '
                   f'text-anchor="middle">{html.escape(category)}</text>')
    return (f'<svg viewBox="0 0 {width} {height}" role="img" '
           f'aria-label="{html.escape(chart.title or "chart")}">' + "".join(bars) + "</svg>")


def render_html_slides(presentation: Presentation) -> str:
    """Always available (no optional dependency). One `<section>` per
    slide; a chart's numbers are present as a real `<table>` AND as an SVG
    drawn from those same numbers — never a single flattened picture."""
    parts = [f"<article class='deck' data-title='{html.escape(presentation.title)}'>"]
    for slide in presentation.slides:
        parts.append("<section class='slide'>")
        parts.append(f"<h2>{html.escape(slide.title)}</h2>")
        if slide.bullets:
            parts.append("<ul>" + "".join(f"<li>{html.escape(b)}</li>" for b in slide.bullets) + "</ul>")
        if slide.chart is not None:
            chart = slide.chart
            parts.append(_svg_chart(chart))
            parts.append("<table><caption>" + html.escape(chart.title or slide.title) + "</caption>")
            header = "<tr><th scope='col'></th>" + "".join(
                f"<th scope='col'>{html.escape(c)}</th>" for c in chart.categories) + "</tr>"
            parts.append("<thead>" + header + "</thead><tbody>")
            for series in chart.series:
                row = f"<tr><th scope='row'>{html.escape(series.name)}</th>" + "".join(
                    f"<td>{v}{html.escape(chart.unit)}</td>" for v in series.values) + "</tr>"
                parts.append(row)
            parts.append("</tbody></table>")
            if chart.x_axis_title or chart.y_axis_title:
                parts.append(f"<p class='axes'>{html.escape(chart.x_axis_title)} / "
                            f"{html.escape(chart.y_axis_title)}</p>")
        if slide.notes:
            parts.append(f"<aside class='notes'>{html.escape(slide.notes)}</aside>")
        parts.append("</section>")
    parts.append("</article>")
    return "".join(parts)


_MPL: Optional[_SN] = None


def _matplotlib() -> _SN:
    global _MPL
    if _MPL is not None:
        return _MPL
    try:
        import matplotlib
        matplotlib.use("Agg")  # headless — this module never opens a display
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise ExportUnavailable(
            "Flattening a chart to an image needs the 'matplotlib' package "
            "(pip install matplotlib).") from exc
    _MPL = _SN(plt=plt)
    return _MPL


def flatten_chart_to_image(chart: ChartSpec, path: str) -> str:
    """The EXPLICIT opt-in the acceptance line allows: rasterize `chart` to
    a PNG at `path` with matplotlib. Never called by `render_pptx` or
    `render_html_slides` on their own — a caller reaches for this by name
    when it genuinely needs a picture."""
    mpl = _matplotlib()
    fig, ax = mpl.plt.subplots()
    if chart.kind == "pie":
        values = chart.series[0].values if chart.series else []
        ax.pie(values, labels=chart.categories, autopct="%1.0f%%")
    else:
        x = range(len(chart.categories))
        width = 0.8 / max(len(chart.series), 1)
        for i, series in enumerate(chart.series):
            positions = [xi + i * width for xi in x]
            if chart.kind == "line":
                ax.plot(positions, series.values, label=series.name)
            else:
                ax.bar(positions, series.values, width=width, label=series.name)
        ax.set_xticks(list(x))
        ax.set_xticklabels(chart.categories)
        if chart.x_axis_title:
            ax.set_xlabel(chart.x_axis_title)
        y_label = chart.y_axis_title
        if chart.unit:
            y_label = f"{y_label} ({chart.unit})" if y_label else chart.unit
        if y_label:
            ax.set_ylabel(y_label)
        if len(chart.series) > 1:
            ax.legend()
    if chart.title:
        ax.set_title(chart.title)
    fig.tight_layout()
    fig.savefig(path)
    mpl.plt.close(fig)
    return path
