"""tests/test_inspect_image_core.py — src.image_inspection's pure logic:
region math, the crop/rotate/zoom/grid pipeline, shapes detection, grid-cell
parsing, and the file/PDF/URL loaders.
"""
from __future__ import annotations

import io

import pytest
from PIL import Image, ImageDraw

from src import image_inspection as ii


# ---------------------------------------------------------------------------
# region math
# ---------------------------------------------------------------------------

def test_region_to_fraction_default_is_whole_image():
    assert ii.region_to_fraction(None, "fraction", (400, 300)) == (0.0, 0.0, 1.0, 1.0)


def test_region_to_fraction_sorts_reversed_corners():
    # x0 > x1, y0 > y1 -- a caller that gave the corners the "wrong" way
    # round still gets a normal box back.
    box = ii.region_to_fraction([0.6, 0.6, 0.1, 0.1], "fraction", (400, 300))
    assert box == (0.1, 0.1, 0.6, 0.6)


def test_region_to_fraction_px_units_converted_and_clamped():
    box = ii.region_to_fraction([-50, -50, 500, 5000], "px", (400, 300))
    assert box == (0.0, 0.0, 1.0, 1.0)


def test_region_to_fraction_rejects_wrong_length():
    with pytest.raises(ii.InspectImageError):
        ii.region_to_fraction([0.1, 0.2, 0.3], "fraction", (400, 300))


def test_region_to_fraction_rejects_empty_after_clamp():
    with pytest.raises(ii.InspectImageError):
        ii.region_to_fraction([2.0, 2.0, 3.0, 3.0], "fraction", (400, 300))


def test_fraction_to_px_round_trip():
    box = (0.25, 0.5, 0.75, 1.0)
    px = ii.fraction_to_px(box, (400, 300))
    assert px == (100, 150, 300, 300)


def test_fraction_box_in_frame_relative_position():
    # A 40x40 box centered at (120, 150) of a 400x300 image (0.30, 0.50) sits
    # at (23.3%, 50%) inside the 50,50-350,250 frame -- the exact "12% x, 55%
    # y inside this frame" shape the brief asks for.
    box = (100 / 400, 130 / 300, 140 / 400, 170 / 300)
    frame = (50 / 400, 50 / 300, 350 / 400, 250 / 300)
    fx0, fy0, fx1, fy1 = ii.fraction_box_in_frame(box, frame)
    cx, cy = (fx0 + fx1) / 2, (fy0 + fy1) / 2
    assert cx == pytest.approx(0.2333, abs=0.01)
    assert cy == pytest.approx(0.5, abs=0.01)


def test_fraction_box_in_frame_none_when_outside():
    assert ii.fraction_box_in_frame((0.0, 0.0, 0.05, 0.05), (0.5, 0.5, 0.9, 0.9)) is None


# ---------------------------------------------------------------------------
# grid labelling
# ---------------------------------------------------------------------------

def test_cell_id_and_back():
    assert ii.cell_id(0, 0) == "A1"
    assert ii.cell_id(2, 3) == "C4"
    assert ii.cell_id_to_index("C4") == (2, 3)
    assert ii.cell_id_to_index("c4") == (2, 3)
    assert ii.cell_id_to_index("not-a-cell") is None


def test_parse_cell_ids_extracts_valid_cells_only():
    text = "I see it in C4, maybe D4 too. Z99 is out of range. Also c 4 again."
    found = ii.parse_cell_ids(text, cols=10, rows=10)
    assert "C4" in found
    assert "D4" in found
    assert "Z99" not in found  # out of a 10x10 grid
    assert found.count("C4") == 1  # de-duplicated


def test_grid_cell_fraction_box_tiles_the_image():
    box = ii.grid_cell_fraction_box(2, 3, 10, 10)
    assert box == (0.2, 0.3, 0.3, 0.4)


def test_grid_cells_lists_every_cell():
    cells = ii.grid_cells(2, 2)
    ids = {c["cell"] for c in cells}
    assert ids == {"A1", "A2", "B1", "B2"}


# ---------------------------------------------------------------------------
# crop / rotate / zoom / grid pipeline
# ---------------------------------------------------------------------------

def _blank(w=400, h=300, color="white"):
    return Image.new("RGB", (w, h), color)


def test_process_crop_region_and_output_size():
    img = _blank()
    result = ii.process(img, region=[0.1, 0.1, 0.6, 0.6], units="fraction")
    assert result.output_size == (200, 150)
    assert result.region_used == pytest.approx((0.1, 0.1, 0.6, 0.6))
    assert result.downscaled is False


def test_process_zoom_scales_output():
    img = _blank(100, 100)
    result = ii.process(img, zoom=2.0)
    assert result.output_size == (200, 200)
    assert result.zoom == 2.0


def test_process_max_side_downscales_and_warns():
    img = _blank(4000, 1000)
    result = ii.process(img, max_side=500)
    assert max(result.output_size) == 500
    assert result.downscaled is True


def test_process_grid_overlay_reports_dims_and_draws_something():
    img = _blank()
    plain = ii.process(img).image
    gridded = ii.process(img, grid=4).image
    assert gridded.size == plain.size
    # The grid overlay actually changed pixels (lines/labels were drawn).
    assert list(gridded.getdata()) != list(plain.getdata())


def test_process_grid_as_explicit_cols_rows():
    img = _blank()
    result = ii.process(img, grid=[5, 2])
    assert result.grid == (5, 2)


def test_process_enhance_grayscale_removes_color():
    img = Image.new("RGB", (10, 10), (255, 0, 0))
    result = ii.process(img, enhance=["grayscale"])
    r, g, b = result.image.getpixel((5, 5))
    assert r == g == b


def test_process_rejects_zero_or_negative_zoom():
    img = _blank()
    with pytest.raises(ii.InspectImageError):
        ii.process(img, zoom=0)


def test_process_rotate_90_point_maps_back_to_original_fraction():
    img = _blank(400, 300)
    result = ii.process(img, rotate=90)
    assert result.output_size == (300, 400)  # swapped, as a 90 deg turn should
    # The new top-left corner is the ORIGINAL bottom-left corner.
    frac = ii.output_point_to_original_fraction((0, 0), result)
    assert frac == pytest.approx((0.0, 1.0), abs=0.02)


def test_process_rotate_45_drops_the_reverse_mapping():
    img = _blank(400, 300)
    result = ii.process(img, rotate=45)
    assert "not a multiple of 90" in result.mapping_note
    assert ii.output_point_to_original_fraction((10, 10), result) is None


def test_remap_box_to_original_round_trips_through_a_90_rotation():
    img = _blank(400, 300)
    proc = ii.process(img, rotate=90)
    # A box covering the whole rotated output maps back to the whole
    # original image.
    box = ii.remap_box_to_original((0.0, 0.0, 1.0, 1.0), proc)
    assert box == pytest.approx((0.0, 0.0, 1.0, 1.0), abs=1e-6)


def test_image_to_b64_round_trips():
    img = Image.new("RGB", (5, 5), (10, 20, 30))
    b64, mime = ii.image_to_b64(img, "image/png")
    assert mime == "image/png"
    raw = __import__("base64").b64decode(b64)
    reopened = Image.open(io.BytesIO(raw))
    assert reopened.size == (5, 5)


# ---------------------------------------------------------------------------
# shapes: a rectangle frame with a circle at a known relative position
# ---------------------------------------------------------------------------

def _frame_and_circle_image():
    img = Image.new("RGB", (400, 300), "white")
    d = ImageDraw.Draw(img)
    d.rectangle([50, 50, 350, 250], outline="black", width=4)
    d.ellipse([100, 130, 140, 170], fill="black")  # center (120, 150), r=20
    return img


def test_detect_shapes_finds_circle_and_rectangle_absolute_position():
    img = _frame_and_circle_image()
    result = ii.detect_shapes(img, annotate=False)
    assert result["engine"] in ("opencv", "pillow_fallback")
    assert result["circles"], "expected at least one circle/ellipse"
    circle = result["circles"][0]
    cx, cy = circle["center"]
    assert cx == pytest.approx(0.30, abs=0.03)
    assert cy == pytest.approx(0.50, abs=0.03)
    assert result["rectangles"], "expected the frame to be detected as a rectangle"


def test_detect_shapes_frame_relative_position():
    img = _frame_and_circle_image()
    frame = (50 / 400, 50 / 300, 350 / 400, 250 / 300)
    result = ii.detect_shapes(img, frame=frame, annotate=False)
    circle = result["circles"][0]
    fx0, fy0, fx1, fy1 = circle["box_in_frame"]
    cx, cy = (fx0 + fx1) / 2, (fy0 + fy1) / 2
    assert cx == pytest.approx(0.2333, abs=0.03)
    assert cy == pytest.approx(0.5, abs=0.03)


def test_detect_shapes_annotate_returns_preview_image():
    img = _frame_and_circle_image()
    result = ii.detect_shapes(img, annotate=True)
    assert "annotated_b64" in result and result["annotated_b64"]


def test_shapes_pipeline_maps_boxes_back_through_a_crop():
    img = _frame_and_circle_image()
    loaded = ii.LoadedImage(img, "test.png", "file", "pillow")
    loaded.original_size = img.size
    frame = [50 / 400, 50 / 300, 350 / 400, 250 / 300]
    result = ii.shapes_pipeline(loaded, frame=frame, annotate=False)
    assert result["region_mapped_to_original"] is True
    circle = result["circles"][0]
    cx = (circle["box"][0] + circle["box"][2]) / 2
    cy = (circle["box"][1] + circle["box"][3]) / 2
    assert cx == pytest.approx(0.30, abs=0.03)
    assert cy == pytest.approx(0.50, abs=0.03)


def test_shapes_pipeline_warns_on_non_90_rotation():
    img = _frame_and_circle_image()
    loaded = ii.LoadedImage(img, "test.png", "file", "pillow")
    loaded.original_size = img.size
    result = ii.shapes_pipeline(loaded, rotate=30, annotate=False)
    assert result["region_mapped_to_original"] is False
    assert "warning" in result


# ---------------------------------------------------------------------------
# crosshair
# ---------------------------------------------------------------------------

def test_draw_crosshair_changes_pixels_near_the_point():
    img = Image.new("RGB", (100, 100), "white")
    marked = ii.draw_crosshair(img, (0.5, 0.5))
    assert marked.getpixel((50, 50)) != (255, 255, 255)


# ---------------------------------------------------------------------------
# loading: local file, PDF page, URL
# ---------------------------------------------------------------------------

def test_load_from_path_unsupported_extension_raises(tmp_path):
    bad = tmp_path / "notes.txt"
    bad.write_text("hello")
    with pytest.raises(ii.InspectImageError, match="unsupported file type"):
        ii.load_from_path(str(bad))


def test_load_from_path_missing_file_raises(tmp_path):
    with pytest.raises(ii.InspectImageError, match="not found"):
        ii.load_from_path(str(tmp_path / "nope.png"))


def test_load_from_path_reads_a_real_image(tmp_path):
    p = tmp_path / "pic.png"
    Image.new("RGB", (20, 10), "blue").save(p)
    loaded = ii.load_from_path(str(p))
    assert loaded.origin == "file"
    assert loaded.engine == "pillow"
    assert loaded.original_size == (20, 10)


def test_load_from_path_pdf_page_renders_or_degrades_with_reason(tmp_path):
    """No PDF rasterizer (pypdfium2/pdf2image) is installed in this
    environment (ebb7c5f3's requirements-optional.txt extras) — this asserts
    the clean degrade message rather than a stack trace. If a rasterizer IS
    present (a different environment), it asserts an actual page render
    instead, so this test stays meaningful either way."""
    reportlab = pytest.importorskip("reportlab")
    from reportlab.pdfgen import canvas

    p = tmp_path / "doc.pdf"
    c = canvas.Canvas(str(p))
    c.drawString(72, 700, "hello pdf")
    c.showPage()
    c.save()

    try:
        import pypdfium2  # noqa: F401
        has_renderer = True
    except ImportError:
        try:
            import pdf2image  # noqa: F401
            has_renderer = True
        except ImportError:
            has_renderer = False

    if not has_renderer:
        with pytest.raises(ii.InspectImageError, match="pypdfium2 or pdf2image"):
            ii.load_from_path(str(p), page=1)
    else:
        loaded = ii.load_from_path(str(p), page=1)
        assert loaded.origin == "pdf"
        assert loaded.engine in ("pypdfium2", "pdf2image")
        assert loaded.original_size[0] > 0


def test_load_from_url_uses_the_outbound_broker_no_real_network(monkeypatch):
    png_bytes = io.BytesIO()
    Image.new("RGB", (30, 20), "green").save(png_bytes, format="PNG")
    png_bytes = png_bytes.getvalue()

    class _FakeResponse:
        status_code = 200
        headers = {"content-type": "image/png"}
        content = png_bytes
        encoding = None

    calls = []

    def fake_fetch(url, *, profile, timeout=None, headers=None, allowed_mime=None, **kw):
        calls.append((url, profile, allowed_mime))
        return _FakeResponse()

    from src import outbound_fetch as of
    monkeypatch.setattr(of, "fetch", fake_fetch)

    loaded = ii.load_from_url("example.com/pic.png")
    assert loaded.origin == "url"
    assert loaded.original_size == (30, 20)
    assert calls and calls[0][0] == "https://example.com/pic.png"
    assert calls[0][1] == of.PUBLIC_UNTRUSTED


def test_load_from_url_rejects_bad_scheme():
    with pytest.raises(ii.InspectImageError, match="unsupported URL scheme"):
        ii.load_from_url("ftp://example.com/pic.png")


def test_load_from_url_reports_outbound_policy_refusal(monkeypatch):
    from src import outbound_fetch as of

    def fake_fetch(*a, **kw):
        raise of.OutboundPolicyError("blocked: private address")

    monkeypatch.setattr(of, "fetch", fake_fetch)
    with pytest.raises(ii.InspectImageError, match="blocked"):
        ii.load_from_url("http://10.0.0.5/pic.png")
