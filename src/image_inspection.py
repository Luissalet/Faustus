"""src/image_inspection.py — core logic behind the `inspect_image` tool.

The main local model is often text-only (a 27B on llama.cpp with no vision).
When the agent reads an image with `read_file`, a configured Vision model
today only ever answers a fixed "Describe this image in detail" prompt
(`src.document_processor.analyze_image_with_vl_result`) — small details (which
object a hand-drawn circle marks, a tilted symbol in a corner, faint
handwriting) get lost in a generic caption. This module gives the agent a
*targeted* look instead: crop a region, rotate, zoom, enhance, overlay a
labelled grid, ask a specific question, or detect shapes without a model at
all.

Layout, mirroring `src.pdf_ops`/`src.media_inspection`:
  * ``InspectImageError`` — the one exception this module raises; the tool
    wrapper turns it straight into a `{"error": ...}` result.
  * ``load_from_path`` / ``load_from_url`` — get a `PIL.Image` from a local
    file (image or PDF page) or a fetched URL, workspace-confined /
    SSRF-guarded exactly like `read_file`/`pdf_ops`/`web_fetch`.
  * ``process`` — crop/rotate/zoom/enhance/grid, all pure `Pillow`, returning
    the processed image plus a metadata dict a caller can hand straight back
    to the model (which region was used, whether the result was downscaled).
  * ``detect_shapes`` — circles/ellipses/rectangles/lines, OpenCV
    (`cv2.HoughCircles` + contours + `HoughLinesP`) when importable, else a
    dependency-light Pillow/`numpy` connected-components fallback. Model-free
    on purpose: "where is the circle" should not need a vision model.
  * ``image_to_b64`` / ``fraction_box_in_frame`` / grid helpers — small pure
    functions the tool wrapper and the tests both use directly.
"""

from __future__ import annotations

import base64
import io
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

logger = logging.getLogger(__name__)


class InspectImageError(Exception):
    """A bad argument, or an image/PDF this module cannot open."""


SUPPORTED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff", ".tif"}
MIME_BY_EXT = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
    ".tiff": "image/tiff", ".tif": "image/tiff",
}
FORMAT_BY_MIME = {
    "image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP",
    "image/gif": "GIF", "image/bmp": "BMP", "image/tiff": "TIFF",
}

DEFAULT_PDF_DPI = 150
DEFAULT_GRID_DIVISIONS = 10
# Never hand the model (local vision-capable, or the configured VL model)
# something bigger than this on its longest side — the same order of
# magnitude as `src.tool_images.DEFAULT_MAX_PX` for tool-result screenshots.
MAX_OUTPUT_SIDE_PX = 2048
ENHANCE_OPS = ("autocontrast", "sharpen", "grayscale", "threshold")


# ---------------------------------------------------------------------------
# Loading: local file (image or PDF page) / URL
# ---------------------------------------------------------------------------

@dataclass
class LoadedImage:
    image: Image.Image
    source: str                    # the path or url as given by the caller
    origin: str                    # "file" | "pdf" | "url"
    engine: str                    # "pillow" | "pypdfium2" | "pdf2image" | "url"
    page: Optional[int] = None
    original_size: Tuple[int, int] = field(default=(0, 0))


def resolve_path(raw_path: str) -> str:
    """Confine `raw_path` to the active workspace / DATA_DIR allowlist.

    Delegates to `src.tool_execution._resolve_tool_path` — the same helper
    `pdf_ops.resolve_path` and `read_file` use — rather than a second
    implementation that could drift from it.
    """
    from src.tool_execution import _resolve_tool_path
    try:
        return _resolve_tool_path(raw_path)
    except ValueError as exc:
        raise InspectImageError(str(exc)) from exc


def _ext_of(path: str) -> str:
    return os.path.splitext(path)[1].lower()


def _normalize_mode(img: Image.Image) -> Image.Image:
    if img.mode in ("RGB", "RGBA", "L"):
        return img
    return img.convert("RGB")


def load_from_path(raw_path: str, *, page: Optional[int] = None,
                    dpi: int = DEFAULT_PDF_DPI) -> LoadedImage:
    """A local image file, or one page of a local PDF, as a `LoadedImage`."""
    path = resolve_path(raw_path)
    if not os.path.isfile(path):
        raise InspectImageError(f"inspect_image: {raw_path}: file not found")
    ext = _ext_of(path)
    if ext == ".pdf":
        img, engine = _render_pdf_page(path, page=page, dpi=dpi)
        loaded = LoadedImage(img, raw_path, "pdf", engine, page=page or 1)
    elif ext in SUPPORTED_IMAGE_EXTS:
        try:
            img = Image.open(path)
            img.load()
        except Exception as exc:  # noqa: BLE001 - a bad image is data, not a crash
            raise InspectImageError(
                f"inspect_image: {raw_path}: could not open as an image ({exc})"
            ) from exc
        loaded = LoadedImage(_normalize_mode(img), raw_path, "file", "pillow")
    else:
        raise InspectImageError(
            f"inspect_image: {raw_path}: unsupported file type '{ext}' "
            f"(expected png/jpg/jpeg/webp/gif/bmp/tiff, or a .pdf with `page`)"
        )
    loaded.original_size = loaded.image.size
    return loaded


def _render_pdf_page(path: str, *, page: Optional[int], dpi: int) -> Tuple[Image.Image, str]:
    """Rasterize one PDF page with whichever renderer is installed.

    Same optional-dependency shape as `src.pdf_ops.to_images`: try
    `pypdfium2` first, fall back to `pdf2image`, and degrade with a clear,
    actionable message (not a stack trace) when neither is installed.
    """
    page_num = page or 1
    if page_num < 1:
        raise InspectImageError("inspect_image: `page` is 1-based and must be >= 1")
    try:
        import pypdfium2 as pdfium
    except ImportError:
        pdfium = None
    if pdfium is not None:
        pdf = pdfium.PdfDocument(path)
        total = len(pdf)
        if page_num > total:
            raise InspectImageError(
                f"inspect_image: page {page_num} is out of range (PDF has {total} pages)"
            )
        scale = max(dpi, 1) / 72.0
        bitmap = pdf[page_num - 1].render(scale=scale)
        return bitmap.to_pil().convert("RGB"), "pypdfium2"

    try:
        from pdf2image import convert_from_path
    except ImportError as exc:
        raise InspectImageError(
            "inspect_image: rendering a PDF page needs pypdfium2 or pdf2image, neither of "
            "which is installed on this server. Install one with "
            "`pip install -r requirements-optional.txt` (pdf2image also needs the "
            "poppler-utils binaries on PATH)."
        ) from exc
    try:
        images = convert_from_path(path, dpi=dpi, first_page=page_num, last_page=page_num)
    except Exception as exc:  # noqa: BLE001
        raise InspectImageError(f"inspect_image: could not render page {page_num}: {exc}") from exc
    if not images:
        raise InspectImageError(f"inspect_image: page {page_num} could not be rendered")
    return images[0].convert("RGB"), "pdf2image"


def load_from_url(url: str, *, timeout: float = 20.0) -> LoadedImage:
    """Download an http(s) image through the SSRF-guarded outbound broker.

    Same trust profile web_fetch uses for a user-named URL (`PUBLIC_UNTRUSTED`
    — public destinations only, no redirect into a private network) and the
    same ~2 MB budget that profile carries; a bigger picture should be given
    to the tool as a local `path` instead.
    """
    if not isinstance(url, str) or not url.strip():
        raise InspectImageError("inspect_image: `url` is empty")
    u = url.strip()
    low = u.lower()
    if "://" in low and not low.startswith(("http://", "https://")):
        raise InspectImageError(f"inspect_image: unsupported URL scheme (only http/https): {u[:80]}")
    if not low.startswith(("http://", "https://")):
        u = "https://" + u

    from src import outbound_fetch as of

    try:
        res = of.fetch(
            u, profile=of.PUBLIC_UNTRUSTED, timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0 (compatible; Faustus-inspect-image/1.0)",
                     "Accept": "image/*"},
            allowed_mime=("image/",),
        )
    except of.OutboundPolicyError as exc:
        raise InspectImageError(f"inspect_image: {u}: {exc}") from exc
    except of.BodyTooLargeError as exc:
        raise InspectImageError(
            f"inspect_image: {u}: {exc} — download it and pass a local `path` instead"
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise InspectImageError(f"inspect_image: {u}: fetch failed ({exc})") from exc
    if res.status_code >= 400:
        raise InspectImageError(f"inspect_image: {u}: HTTP {res.status_code}")
    raw = res.content or b""
    if not raw:
        raise InspectImageError(f"inspect_image: {u}: empty response")
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception as exc:  # noqa: BLE001
        raise InspectImageError(f"inspect_image: {u}: response was not a readable image ({exc})") from exc
    loaded = LoadedImage(_normalize_mode(img), url, "url", "url")
    loaded.original_size = img.size
    return loaded


# ---------------------------------------------------------------------------
# Region math (fractions of the FULL, pre-crop image — the coordinate system
# every result is reported in, so "12%, 55%" always means the same thing
# regardless of how many crops/zooms happened along the way)
# ---------------------------------------------------------------------------

def region_to_fraction(region: Optional[Sequence[float]], units: str,
                        size: Tuple[int, int]) -> Tuple[float, float, float, float]:
    """`region` (fractions or pixels, in either corner order) -> sorted
    fractions `(x0, y0, x1, y1)` of `size`, clamped to `[0, 1]`."""
    w, h = size
    if not region:
        return (0.0, 0.0, 1.0, 1.0)
    if len(region) != 4:
        raise InspectImageError("inspect_image: `region` must be [x0, y0, x1, y1]")
    x0, y0, x1, y1 = (float(v) for v in region)
    if units == "px":
        if w <= 0 or h <= 0:
            raise InspectImageError("inspect_image: image has zero size")
        x0, x1 = x0 / w, x1 / w
        y0, y1 = y0 / h, y1 / h
    x0, x1 = sorted((x0, x1))
    y0, y1 = sorted((y0, y1))
    x0, x1 = max(0.0, min(1.0, x0)), max(0.0, min(1.0, x1))
    y0, y1 = max(0.0, min(1.0, y0)), max(0.0, min(1.0, y1))
    if x1 - x0 < 1e-6 or y1 - y0 < 1e-6:
        raise InspectImageError("inspect_image: `region` is empty after clamping to the image")
    return (x0, y0, x1, y1)


def fraction_to_px(fraction_box: Tuple[float, float, float, float],
                    size: Tuple[int, int]) -> Tuple[int, int, int, int]:
    w, h = size
    x0, y0, x1, y1 = fraction_box
    return (
        max(0, int(round(x0 * w))), max(0, int(round(y0 * h))),
        min(w, int(round(x1 * w))), min(h, int(round(y1 * h))),
    )


def fraction_box_in_frame(
    box: Tuple[float, float, float, float],
    frame: Tuple[float, float, float, float],
) -> Optional[Tuple[float, float, float, float]]:
    """`box` (fractions of the FULL image) re-expressed as fractions of
    `frame` (also fractions of the full image) — "12% x, 55% y inside this
    frame" instead of inside the whole picture. `None` when `box` does not
    overlap `frame` at all."""
    fx0, fy0, fx1, fy1 = frame
    fw, fh = fx1 - fx0, fy1 - fy0
    if fw <= 0 or fh <= 0:
        return None
    bx0, by0, bx1, by1 = box
    ix0, iy0 = max(bx0, fx0), max(by0, fy0)
    ix1, iy1 = min(bx1, fx1), min(by1, fy1)
    if ix1 <= ix0 or iy1 <= iy0:
        return None
    return (
        (bx0 - fx0) / fw, (by0 - fy0) / fh,
        (bx1 - fx0) / fw, (by1 - fy0) / fh,
    )


def fraction_point_in_frame(point: Tuple[float, float],
                             frame: Tuple[float, float, float, float]) -> Tuple[float, float]:
    fx0, fy0, fx1, fy1 = frame
    fw, fh = (fx1 - fx0) or 1.0, (fy1 - fy0) or 1.0
    px, py = point
    return ((px - fx0) / fw, (py - fy0) / fh)


# ---------------------------------------------------------------------------
# Grid labelling (10x10 default: columns A-J, rows 1-10)
# ---------------------------------------------------------------------------

def _column_letter(index: int) -> str:
    """0 -> 'A', 25 -> 'Z', 26 -> 'AA' (never needed at the default size, but
    correct rather than raising on a caller-chosen `grid` above 26)."""
    letters = ""
    index += 1
    while index > 0:
        index, rem = divmod(index - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters


def cell_id(col: int, row: int) -> str:
    """0-based (col, row) -> \"A1\", \"C4\", ... (1-based row number)."""
    return f"{_column_letter(col)}{row + 1}"


_CELL_ID_RE = re.compile(r"\b([A-Za-z]{1,2})\s*-?\s*(\d{1,2})\b")


def parse_cell_ids(text: str, cols: int, rows: int) -> List[str]:
    """Every `\"C4\"`-shaped token in `text` that names a real cell of a
    `cols`x`rows` grid, de-duplicated, in the order first seen."""
    found: List[str] = []
    seen = set()
    for letters, digits in _CELL_ID_RE.findall(text or ""):
        col = _letters_to_index(letters)
        row = int(digits) - 1
        if col is None or not (0 <= col < cols) or not (0 <= row < rows):
            continue
        cid = cell_id(col, row)
        if cid not in seen:
            seen.add(cid)
            found.append(cid)
    return found


def _letters_to_index(letters: str) -> Optional[int]:
    letters = letters.upper()
    if not letters.isalpha():
        return None
    value = 0
    for ch in letters:
        value = value * 26 + (ord(ch) - ord("A") + 1)
    return value - 1


def cell_id_to_index(cid: str) -> Optional[Tuple[int, int]]:
    """\"C4\" -> (2, 3) (0-based column, 0-based row), or `None` if unparsable."""
    m = re.match(r"^\s*([A-Za-z]{1,2})\s*-?\s*(\d{1,2})\s*$", cid or "")
    if not m:
        return None
    col = _letters_to_index(m.group(1))
    if col is None:
        return None
    return (col, int(m.group(2)) - 1)


def grid_cell_fraction_box(col: int, row: int, cols: int, rows: int) -> Tuple[float, float, float, float]:
    return (col / cols, row / rows, (col + 1) / cols, (row + 1) / rows)


def grid_cells(cols: int, rows: int) -> List[Dict[str, Any]]:
    return [
        {"cell": cell_id(c, r), "box": list(grid_cell_fraction_box(c, r, cols, rows))}
        for r in range(rows) for c in range(cols)
    ]


# ---------------------------------------------------------------------------
# crop / rotate / zoom / enhance / grid pipeline
# ---------------------------------------------------------------------------

@dataclass
class ProcessResult:
    image: Image.Image
    region_used: Tuple[float, float, float, float]     # fractions of the ORIGINAL image
    rotate: float
    zoom: float
    enhanced: List[str]
    grid: Optional[Tuple[int, int]]                     # (cols, rows) or None
    downscaled: bool
    original_size: Tuple[int, int]
    output_size: Tuple[int, int]
    mapping_note: str


def _parse_enhance(enhance: Any) -> List[str]:
    if not enhance:
        return []
    if isinstance(enhance, str):
        enhance = [enhance]
    out = []
    for item in enhance:
        name = str(item or "").strip().lower()
        if name in ENHANCE_OPS and name not in out:
            out.append(name)
    return out


def _apply_enhance(img: Image.Image, ops: List[str]) -> Image.Image:
    for op in ops:
        if op == "autocontrast":
            base = img.convert("RGB") if img.mode not in ("RGB", "L") else img
            img = ImageOps.autocontrast(base, cutoff=1)
        elif op == "sharpen":
            base = img.convert("RGB") if img.mode not in ("RGB", "L") else img
            img = base.filter(ImageFilter.SHARPEN)
        elif op == "grayscale":
            img = img.convert("L").convert("RGB")
        elif op == "threshold":
            gray = ImageOps.autocontrast(img.convert("L"), cutoff=1)
            img = gray.point(lambda p: 255 if p > 128 else 0).convert("RGB")
    return img


def _draw_grid(img: Image.Image, cols: int, rows: int) -> Image.Image:
    img = img.convert("RGB")
    w, h = img.size
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    line_rgba = (255, 60, 60, 200)
    for c in range(1, cols):
        x = round(w * c / cols)
        draw.line([(x, 0), (x, h)], fill=line_rgba, width=1)
    for r in range(1, rows):
        y = round(h * r / rows)
        draw.line([(0, y), (w, y)], fill=line_rgba, width=1)
    try:
        font = ImageFont.load_default()
    except Exception:  # pragma: no cover - Pillow always ships a default font
        font = None
    for r in range(rows):
        for c in range(cols):
            x0, y0 = round(w * c / cols), round(h * r / rows)
            label = cell_id(c, r)
            pad = 2
            if font is not None:
                try:
                    bbox = draw.textbbox((0, 0), label, font=font)
                    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
                except Exception:  # pragma: no cover
                    tw, th = 6 * len(label), 8
            else:
                tw, th = 6 * len(label), 8
            draw.rectangle([x0, y0, x0 + tw + 2 * pad, y0 + th + 2 * pad], fill=(255, 255, 0, 170))
            draw.text((x0 + pad, y0 + pad), label, fill=(0, 0, 0, 255), font=font)
    return Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")


def draw_crosshair(img: Image.Image, point: Tuple[float, float], *, size: int = 14,
                    color: Tuple[int, int, int] = (255, 0, 0)) -> Image.Image:
    """Mark `point` (fractions of `img`'s own size) with a crosshair —
    `compare`'s "the same relative point on both images" before asking."""
    img = img.convert("RGB").copy()
    draw = ImageDraw.Draw(img)
    w, h = img.size
    px, py = point[0] * w, point[1] * h
    draw.line([(px - size, py), (px + size, py)], fill=color, width=2)
    draw.line([(px, py - size), (px, py + size)], fill=color, width=2)
    draw.ellipse([px - 4, py - 4, px + 4, py + 4], outline=color, width=2)
    return img


# ---------------------------------------------------------------------------
# Counting by tiles. A vision model miscounts many small things in one look
# and does better on a few at a time (LVLM-Count, arXiv 2412.00686, halves
# the counting error that way). Each tile is a grid cell grown by an overlap
# so an object on a border is seen whole; a red box marks the cell itself,
# and only objects whose centre lies inside the box are counted, so an object
# in the overlap of two tiles is counted by exactly one of them.
# ---------------------------------------------------------------------------

def count_tiles(n: int, overlap: float = 0.15) -> List[Dict[str, Tuple[float, float, float, float]]]:
    """The n x n tiles of a picture as fractions: each item has ``core`` (the
    grid cell, the tiles' cores partition the picture) and ``tile`` (the core
    grown by ``overlap`` of a cell on every side, clamped to the picture)."""
    n = max(1, min(int(n), 4))
    overlap = max(0.0, min(float(overlap), 0.5))
    step = 1.0 / n
    out = []
    for row in range(n):
        for col in range(n):
            core = (col * step, row * step, (col + 1) * step, (row + 1) * step)
            grow = overlap * step
            tile = (max(0.0, core[0] - grow), max(0.0, core[1] - grow),
                    min(1.0, core[2] + grow), min(1.0, core[3] + grow))
            out.append({"core": core, "tile": tile, "cell": cell_id(col, row)})
    return out


def tile_with_core_box(img: Image.Image, tile: Tuple[float, float, float, float],
                       core: Tuple[float, float, float, float]) -> Image.Image:
    """The ``tile`` crop of ``img`` with the ``core`` cell outlined in red
    (both fractions of ``img``). Only a thin line on the core's border is
    drawn, so the picture inside stays visible."""
    img = img.convert("RGB")
    w, h = img.size
    x0, y0, x1, y1 = fraction_to_px(tile, (w, h))
    crop = img.crop((x0, y0, max(x0 + 1, x1), max(y0 + 1, y1))).copy()
    cw, ch = crop.size
    tw, th = (tile[2] - tile[0]) or 1.0, (tile[3] - tile[1]) or 1.0
    bx0 = (core[0] - tile[0]) / tw * cw
    by0 = (core[1] - tile[1]) / th * ch
    bx1 = (core[2] - tile[0]) / tw * cw
    by1 = (core[3] - tile[1]) / th * ch
    width = max(2, int(round(min(cw, ch) / 160)))
    ImageDraw.Draw(crop).rectangle([bx0, by0, bx1 - 1, by1 - 1], outline=(255, 0, 0), width=width)
    return crop


_FIRST_NUMBER = re.compile(r"(?<![\w.])(\d{1,5})(?![\w.])")
_NUMBER_WORDS = {
    "zero": 0, "none": 0, "cero": 0, "ninguno": 0, "ninguna": 0, "one": 1, "uno": 1, "una": 1,
    "two": 2, "dos": 2, "three": 3, "tres": 3, "four": 4, "cuatro": 4, "five": 5, "cinco": 5,
    "six": 6, "seis": 6, "seven": 7, "siete": 7, "eight": 8, "ocho": 8, "nine": 9, "nueve": 9,
    "ten": 10, "diez": 10,
}


def parse_count(text: str) -> Optional[int]:
    """The count a model answered: the first standalone number of its first
    non-empty line, or a number word there ("none", "tres"); else the first
    number anywhere. None when there is no count at all ("unclear")."""
    text = str(text or "").strip()
    if not text:
        return None
    first = next((line for line in text.splitlines() if line.strip()), "")
    match = _FIRST_NUMBER.search(first)
    if match:
        return int(match.group(1))
    for word in re.findall(r"[a-záéíóúñ]+", first.lower()):
        if word in _NUMBER_WORDS:
            return _NUMBER_WORDS[word]
    match = _FIRST_NUMBER.search(text)
    return int(match.group(1)) if match else None


def process(
    loaded: Image.Image,
    *,
    region: Optional[Sequence[float]] = None,
    units: str = "fraction",
    rotate: float = 0.0,
    zoom: Optional[float] = None,
    max_side: Optional[int] = None,
    enhance: Any = None,
    grid: Any = None,
) -> ProcessResult:
    """Crop, rotate, zoom, enhance and grid-overlay `loaded` (a `PIL.Image`).

    Returns the processed image plus metadata: which fraction-of-the-original
    region was used, whether the output was downscaled, and — for `rotate`
    that is a multiple of 90 degrees — a `mapping_note` a caller can use to
    convert a point in the OUTPUT back to a fraction of the original image
    (arbitrary angles keep the crop but drop that reverse mapping, since a
    rotated-and-padded canvas is no longer a simple linear map).
    """
    original_size = loaded.size
    region_used = region_to_fraction(region, units, original_size)
    box_px = fraction_to_px(region_used, original_size)
    img = loaded.crop(box_px)

    rotate = float(rotate or 0.0) % 360.0
    exact_rotation = rotate in (0.0, 90.0, 180.0, 270.0)
    if rotate:
        img = img.rotate(-rotate, expand=True, resample=Image.BICUBIC, fillcolor=(255, 255, 255)
                          if img.mode == "RGB" else None)

    zoom_factor = float(zoom) if zoom is not None else 1.0
    if zoom_factor <= 0:
        raise InspectImageError("inspect_image: `zoom` must be > 0")
    if zoom_factor != 1.0:
        w, h = img.size
        img = img.resize((max(1, round(w * zoom_factor)), max(1, round(h * zoom_factor))),
                          Image.LANCZOS if zoom_factor < 1 else Image.LANCZOS)

    enhance_ops = _parse_enhance(enhance)
    if enhance_ops:
        img = _apply_enhance(img, enhance_ops)

    downscaled = False
    side_cap = min(int(max_side), MAX_OUTPUT_SIDE_PX) if max_side else MAX_OUTPUT_SIDE_PX
    w, h = img.size
    if max(w, h) > side_cap:
        scale = side_cap / float(max(w, h))
        img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
        downscaled = True

    grid_dims: Optional[Tuple[int, int]] = None
    if grid:
        if isinstance(grid, (list, tuple)) and len(grid) == 2:
            grid_dims = (max(1, int(grid[0])), max(1, int(grid[1])))
        else:
            n = max(1, int(grid)) if not isinstance(grid, bool) else DEFAULT_GRID_DIVISIONS
            grid_dims = (n, n)
        img = _draw_grid(img, grid_dims[0], grid_dims[1])

    if exact_rotation:
        note = (
            f"fx = region[0] + ((px_in_output)/(output_w))*(region[2]-region[0]) "
            f"after undoing the {int(rotate)}° rotation (region is fractions of the ORIGINAL image); "
            "see inspect_image `view`'s own worked mapping for this call."
        )
    else:
        note = (
            f"rotate={rotate:g}° is not a multiple of 90, so output pixels cannot be mapped back "
            "to a fraction of the original image; re-inspect the same region with rotate=0/90/180/270 "
            "if you need that mapping."
        )

    return ProcessResult(
        image=img, region_used=region_used, rotate=rotate, zoom=zoom_factor,
        enhanced=enhance_ops, grid=grid_dims, downscaled=downscaled,
        original_size=original_size, output_size=img.size, mapping_note=note,
    )


def output_point_to_original_fraction(
    point_px: Tuple[float, float], result: ProcessResult,
) -> Optional[Tuple[float, float]]:
    """A point in `result.image`'s pixels -> a fraction of the ORIGINAL
    image, for `rotate` a multiple of 90 degrees. `None` otherwise."""
    if result.rotate not in (0.0, 90.0, 180.0, 270.0):
        return None
    ow, oh = result.output_size
    if ow <= 0 or oh <= 0:
        return None
    u, v = point_px[0] / ow, point_px[1] / oh
    # Undo the rotation (PIL rotate(-rotate, expand=True) turned the crop
    # clockwise by `rotate` degrees): map (u, v) in the rotated frame back to
    # (u0, v0) in the pre-rotation crop.
    if result.rotate == 0.0:
        u0, v0 = u, v
    elif result.rotate == 90.0:
        u0, v0 = v, 1.0 - u
    elif result.rotate == 180.0:
        u0, v0 = 1.0 - u, 1.0 - v
    else:  # 270
        u0, v0 = 1.0 - v, u
    x0, y0, x1, y1 = result.region_used
    return (x0 + u0 * (x1 - x0), y0 + v0 * (y1 - y0))


def remap_box_to_original(
    box: Tuple[float, float, float, float], result: ProcessResult,
) -> Optional[Tuple[float, float, float, float]]:
    """A box in fractions of `result.image` (the processed/cropped output) ->
    fractions of the ORIGINAL image, for `rotate` a multiple of 90 degrees.
    `None` for any other angle (see `output_point_to_original_fraction`)."""
    if result.rotate not in (0.0, 90.0, 180.0, 270.0):
        return None
    ow, oh = result.output_size
    x0, y0, x1, y1 = box
    corners_px = [(x0 * ow, y0 * oh), (x1 * ow, y0 * oh), (x0 * ow, y1 * oh), (x1 * ow, y1 * oh)]
    mapped = [output_point_to_original_fraction(p, result) for p in corners_px]
    if any(m is None for m in mapped):
        return None
    xs = [m[0] for m in mapped]
    ys = [m[1] for m in mapped]
    return (min(xs), min(ys), max(xs), max(ys))


def image_to_b64(img: Image.Image, mime: str = "image/png") -> Tuple[str, str]:
    fmt = FORMAT_BY_MIME.get(mime, "PNG")
    buf = io.BytesIO()
    to_save = img.convert("RGB") if fmt == "JPEG" and img.mode not in ("RGB", "L") else img
    try:
        to_save.save(buf, format=fmt)
    except Exception:  # noqa: BLE001 - an odd mode/format combo: fall back to PNG
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="PNG")
        mime, fmt = "image/png", "PNG"
    return base64.b64encode(buf.getvalue()).decode("ascii"), mime


# ---------------------------------------------------------------------------
# Shapes: OpenCV when importable, else a Pillow/numpy connected-components
# fallback. Model-free — "where is the circle" should never need a VL call.
# ---------------------------------------------------------------------------

def _try_import_cv2():
    try:
        import cv2  # type: ignore
        return cv2
    except ImportError:
        return None


def _try_import_numpy():
    try:
        import numpy as np  # type: ignore
        return np
    except ImportError:
        return None


# Shapes are detected on a bounded-size working copy so the fallback's pure
# python labelling stays fast on a big photo; boxes are reported as fractions
# so this cap never shows up in the answer.
_SHAPE_WORK_MAX_SIDE = 900


def detect_shapes(img: Image.Image, *, frame: Optional[Tuple[float, float, float, float]] = None,
                   annotate: bool = True) -> Dict[str, Any]:
    """Circles/ellipses/rectangles/lines in `img`. `frame` (fractions of
    `img`) adds a `*_in_frame` box to every shape found inside it."""
    original_size = img.size
    work = img
    scale = 1.0
    if max(original_size) > _SHAPE_WORK_MAX_SIDE:
        scale = _SHAPE_WORK_MAX_SIDE / float(max(original_size))
        work = img.resize((max(1, round(original_size[0] * scale)),
                            max(1, round(original_size[1] * scale))), Image.LANCZOS)

    cv2 = _try_import_cv2()
    np = _try_import_numpy()
    if cv2 is not None and np is not None:
        shapes, engine = _detect_shapes_cv2(work, cv2, np)
    elif np is not None:
        shapes, engine = _detect_shapes_fallback(work, np)
    else:  # pragma: no cover - numpy is a hard dependency of this project
        shapes, engine = [], "unavailable"

    for shape in shapes:
        shape["box"] = list(shape["box"])
        if frame is not None:
            in_frame = fraction_box_in_frame(tuple(shape["box"]), frame)
            shape["box_in_frame"] = list(in_frame) if in_frame else None

    result: Dict[str, Any] = {"engine": engine, "shapes": shapes,
                               "circles": [s for s in shapes if s["kind"] in ("circle", "ellipse")],
                               "rectangles": [s for s in shapes if s["kind"] == "rectangle"],
                               "lines": [s for s in shapes if s["kind"] == "line"]}
    if annotate and shapes:
        result["annotated_b64"], result["annotated_mime"] = _annotate_shapes(img, shapes)
    return result


def _annotate_shapes(img: Image.Image, shapes: List[Dict[str, Any]]) -> Tuple[str, str]:
    canvas = img.convert("RGB").copy()
    draw = ImageDraw.Draw(canvas)
    w, h = canvas.size
    colors = {"circle": (255, 0, 0), "ellipse": (255, 128, 0), "rectangle": (0, 128, 255), "line": (0, 200, 0)}
    for shape in shapes:
        x0, y0, x1, y1 = fraction_to_px(tuple(shape["box"]), (w, h))
        draw.rectangle([x0, y0, x1, y1], outline=colors.get(shape["kind"], (255, 0, 255)), width=2)
    return image_to_b64(canvas, "image/png")


def _rows(np, found: Any, width: int) -> Any:
    """OpenCV's result as rows of its first `width` numbers, whatever the
    nesting (see the note in `_detect_shapes_cv2`)."""
    arr = np.asarray(found)
    if arr.ndim == 1:
        arr = arr.reshape(1, -1)
    return arr.reshape(-1, arr.shape[-1])[:, :width]


def _detect_shapes_cv2(img: Image.Image, cv2, np) -> Tuple[List[Dict[str, Any]], str]:
    arr = np.array(img.convert("RGB"))
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    h, w = gray.shape[:2]
    shapes: List[Dict[str, Any]] = []

    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    circles = cv2.HoughCircles(
        blurred, cv2.HOUGH_GRADIENT, dp=1.2, minDist=max(10, min(w, h) // 12),
        param1=100, param2=30, minRadius=max(3, min(w, h) // 60), maxRadius=max(6, min(w, h) // 2),
    )
    # Flattened by shape, not by index: OpenCV 4 returns circles as (1, N, 3)
    # and lines as (N, 1, 4); OpenCV 5 dropped a dimension, and indexing the
    # old way failed live with "cannot unpack non-iterable numpy.int32".
    if circles is not None and np.asarray(circles).size:
        for cx, cy, r in _rows(np, circles, 3):
            cx, cy, r = float(cx), float(cy), float(r)
            box = ((cx - r) / w, (cy - r) / h, (cx + r) / w, (cy + r) / h)
            shapes.append({"kind": "circle", "box": box,
                            "center": (cx / w, cy / h), "confidence": 0.7})

    edges = cv2.Canny(gray, 60, 160)
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    for cnt in contours:
        area = float(cv2.contourArea(cnt))
        if area < (w * h) * 0.001:
            continue
        x, y, cw, ch = (int(v) for v in cv2.boundingRect(cnt))
        box = (x / w, y / h, (x + cw) / w, (y + ch) / h)
        peri = cv2.arcLength(cnt, True)
        approx = cv2.approxPolyDP(cnt, 0.02 * peri, True)
        fill_ratio = area / max(1, cw * ch)
        if len(approx) == 4 and fill_ratio > 0.6:
            shapes.append({"kind": "rectangle", "box": box,
                            "center": ((x + cw / 2) / w, (y + ch / 2) / h), "confidence": 0.6})
        elif len(approx) >= 8 and fill_ratio > 0.55 and not any(
                s["kind"] == "circle" and _boxes_overlap(s["box"], box) for s in shapes):
            shapes.append({"kind": "ellipse", "box": box,
                            "center": ((x + cw / 2) / w, (y + ch / 2) / h), "confidence": 0.5})

    lines = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=40,
                             minLineLength=min(w, h) * 0.1, maxLineGap=10)
    if lines is not None and np.asarray(lines).size:
        for x1, y1, x2, y2 in _rows(np, lines, 4):
            x1, y1, x2, y2 = float(x1), float(y1), float(x2), float(y2)
            box = (min(x1, x2) / w, min(y1, y2) / h, max(x1, x2) / w, max(y1, y2) / h)
            shapes.append({"kind": "line", "box": box,
                            "endpoints": [(x1 / w, y1 / h), (x2 / w, y2 / h)], "confidence": 0.5})
    return shapes, "opencv"


def _boxes_overlap(a, b) -> bool:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    return not (ax1 < bx0 or bx1 < ax0 or ay1 < by0 or by1 < ay0)


def _detect_shapes_fallback(img: Image.Image, np) -> Tuple[List[Dict[str, Any]], str]:
    """No OpenCV: connected components of dark/saturated strokes, classified
    into circle/ellipse/rectangle/line by bounding-box fill ratio and, for a
    thin outline, how much of the component hugs its own bounding box edge
    (a hollow rectangle fills almost none of its box but almost all of its
    border)."""
    rgb = np.asarray(img.convert("RGB"), dtype=np.float32)
    gray = rgb.mean(axis=2)
    maxc = rgb.max(axis=2)
    minc = rgb.min(axis=2)
    sat = np.where(maxc > 0, (maxc - minc) / np.maximum(maxc, 1e-6), 0.0)
    mask = (gray < 200) | (sat > 0.25)

    h, w = mask.shape
    labels = _label_components(mask)
    shapes: List[Dict[str, Any]] = []
    for comp in labels:
        ys, xs = comp["ys"], comp["xs"]
        x0, x1, y0, y1 = int(xs.min()), int(xs.max()) + 1, int(ys.min()), int(ys.max()) + 1
        bw, bh = x1 - x0, y1 - y0
        area = float(len(xs))
        bbox_area = float(bw * bh)
        if area < 12 or bbox_area < 20:
            continue
        fill_ratio = area / bbox_area
        aspect = bw / bh if bh else 1.0

        margin = max(1.0, min(bw, bh) * 0.12)
        near_edge = (
            (xs - x0 < margin) | (x1 - xs <= margin) |
            (ys - y0 < margin) | (y1 - ys <= margin)
        )
        border_ratio = float(near_edge.mean())

        box = (float(x0 / w), float(y0 / h), float(x1 / w), float(y1 / h))
        center = (float((x0 + x1) / 2 / w), float((y0 + y1) / 2 / h))
        long_side, short_side = max(bw, bh), max(1, min(bw, bh))
        elongation = long_side / short_side

        if border_ratio > 0.8 and fill_ratio < 0.55 and elongation < 4:
            kind, confidence = "rectangle", min(0.85, 0.5 + border_ratio * 0.4)
        elif fill_ratio >= 0.85:
            kind, confidence = "rectangle", min(0.9, fill_ratio)
        elif 0.55 <= fill_ratio <= 0.92 and 0.55 <= aspect <= 1.8:
            kind = "circle" if 0.85 <= aspect <= 1.18 else "ellipse"
            ideal = 0.785  # pi/4, a filled circle's fraction of its bbox
            confidence = max(0.3, 1.0 - abs(fill_ratio - ideal) / ideal)
        elif elongation >= 4 and fill_ratio < 0.4:
            kind, confidence = "line", 0.5
        else:
            kind, confidence = "shape", 0.3
        shapes.append({"kind": kind, "box": box, "center": center,
                        "confidence": round(float(confidence), 2)})
    shapes.sort(key=lambda s: -s["confidence"])
    return shapes, "pillow_fallback"


def _label_components(mask) -> List[Dict[str, Any]]:
    """4-connected components of a boolean 2-D array, iterative (no
    recursion, no scipy). Returns each component's pixel coordinates."""
    np = _try_import_numpy()
    h, w = mask.shape
    visited = np.zeros_like(mask, dtype=bool)
    components: List[Dict[str, Any]] = []
    coords = np.argwhere(mask)
    coord_set_visited = visited
    for y0, x0 in coords:
        if coord_set_visited[y0, x0]:
            continue
        stack = [(int(y0), int(x0))]
        coord_set_visited[y0, x0] = True
        ys: List[int] = []
        xs: List[int] = []
        while stack:
            y, x = stack.pop()
            ys.append(y)
            xs.append(x)
            for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                ny, nx = y + dy, x + dx
                if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not coord_set_visited[ny, nx]:
                    coord_set_visited[ny, nx] = True
                    stack.append((ny, nx))
        if len(ys) >= 8:
            components.append({"ys": np.array(ys), "xs": np.array(xs)})
    return components


# ---------------------------------------------------------------------------
# High-level, model-free pipelines the tool wrapper calls directly
# ---------------------------------------------------------------------------

def shapes_pipeline(
    loaded: LoadedImage,
    *,
    region: Optional[Sequence[float]] = None,
    units: str = "fraction",
    rotate: float = 0.0,
    zoom: Optional[float] = None,
    max_side: Optional[int] = None,
    enhance: Any = None,
    frame: Optional[Sequence[float]] = None,
    frame_units: str = "fraction",
    annotate: bool = True,
) -> Dict[str, Any]:
    """`detect_shapes` over the (optionally cropped/rotated) image, with
    every box reported as a fraction of the ORIGINAL image — not the crop —
    plus `box_in_frame` when `frame` is given (fractions/px of the ORIGINAL
    image, same convention as `region`)."""
    proc = process(loaded.image, region=region, units=units, rotate=rotate,
                   zoom=zoom, max_side=max_side, enhance=enhance, grid=None)
    detection = detect_shapes(proc.image, frame=None, annotate=annotate)

    remapped_ok = proc.rotate in (0.0, 90.0, 180.0, 270.0)
    frame_box = region_to_fraction(frame, frame_units, loaded.original_size) if frame else None
    for shape in detection["shapes"]:
        original_box = remap_box_to_original(tuple(shape["box"]), proc) if remapped_ok else None
        if original_box is not None:
            shape["box"] = list(original_box)
            if "center" in shape:
                cx = (original_box[0] + original_box[2]) / 2
                cy = (original_box[1] + original_box[3]) / 2
                shape["center"] = [cx, cy]
        if frame_box is not None and original_box is not None:
            in_frame = fraction_box_in_frame(original_box, frame_box)
            shape["box_in_frame"] = list(in_frame) if in_frame else None
    # The three convenience lists in `detection` alias the same dicts already
    # updated above (`detect_shapes` builds them by filtering `shapes`), so
    # nothing further to update there.
    detection["region_used"] = list(proc.region_used)
    detection["region_mapped_to_original"] = remapped_ok
    detection["rotate"] = proc.rotate
    detection["downscaled"] = proc.downscaled
    detection["original_size"] = list(proc.original_size)
    detection["frame"] = list(frame_box) if frame_box else None
    if not remapped_ok:
        detection["warning"] = (
            f"rotate={proc.rotate:g}° is not a multiple of 90 — shape boxes are relative to the "
            "analyzed crop, not the original image."
        )
    return detection
