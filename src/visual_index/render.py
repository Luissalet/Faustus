"""Rendering a source into images: a PDF at 200 dpi, a loose image, a web page captured by the browser.

* **PDF**: ``pypdfium2`` (Apache-2.0/BSD, the renderer ``inspect_image`` already prefers), else ``pdf2image`` + poppler.
  PyMuPDF is deliberately not used (AGPL). Pages render one at a time, so a long PDF never sits in memory whole.
* **Image**: Pillow.
* **Web page**: the browser Faustus already has (``src.reach.browser``: Playwright on its own profile, the family's
  request guard, the browser lane) opens the page at 875 px wide and takes the full-page capture in tiles of at most 8192 px.
  Only ``http``/``https`` URLs that pass ``hoard_link.web.safety.check_url`` are opened.
* A URL whose path ends in ``.pdf`` or a picture extension is downloaded (SSRF-guarded) and rendered as that kind.

Every renderer can be replaced (``Renderers``) so the tests never need a browser or a PDF library.
"""
from __future__ import annotations

import hashlib
import io
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from PIL import Image

from .tiling import STRIP_WIDTH, TILE_HEIGHT

logger = logging.getLogger(__name__)

PDF_DPI = 200
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
MAX_DOWNLOAD_BYTES = 40 * 1024 * 1024


class RenderError(ValueError):
    def __init__(self, message: str, code: str = "render_failed"):
        super().__init__(message)
        self.code = code


@dataclass
class Piece:
    """One image to cut into strips. ``y_offset`` places a web tile in the page; PDF pages and pictures use 0."""

    page: int
    y_offset: int
    image: Image.Image


@dataclass
class Rendered:
    source: str                       # the key the index stores (absolute path or URL)
    kind: str                         # "pdf" | "image" | "web"
    pieces: Iterator[Piece]
    fingerprint: str = ""             # MD5 of the file's bytes; "" for web pages (their strips' hashes decide)
    title: str = ""
    pages_total: int = 0
    pages_rendered: List[int] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)


def is_url(source: str) -> bool:
    return bool(re.match(r"^https?://", str(source or "").strip(), re.I))


def file_md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def kind_of(source: str) -> str:
    low = source.lower().split("?", 1)[0].split("#", 1)[0]
    ext = os.path.splitext(low)[1]
    if ext == ".pdf":
        return "pdf"
    if ext in IMAGE_EXTS:
        return "image"
    return "web" if is_url(source) else ""


def parse_pages(spec: Any, total: int) -> List[int]:
    """1-based pages from ``[1, 3]``, ``"1-3,7"``, ``3`` or None (all), clipped to ``total``."""
    if spec in (None, "", []):
        return list(range(1, total + 1))
    items: List[Any]
    if isinstance(spec, int):
        items = [spec]
    elif isinstance(spec, str):
        items = [p for p in re.split(r"[,\s]+", spec.strip()) if p]
    else:
        items = list(spec)
    pages: List[int] = []
    for item in items:
        if isinstance(item, int):
            lo = hi = item
        else:
            m = re.fullmatch(r"(\d+)(?:\s*-\s*(\d+))?", str(item).strip())
            if not m:
                raise RenderError(f"pages: cannot read {item!r} (use 3, [1,2] or \"1-3,7\")", "invalid_pages")
            lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
        for p in range(max(1, lo), min(total, hi) + 1):
            if p not in pages:
                pages.append(p)
    return pages


# ------------------------------------------------------------------------------------------------ PDF
def pdf_page_count(path: str) -> int:
    try:
        import pypdfium2 as pdfium

        doc = pdfium.PdfDocument(path)
        try:
            return len(doc)
        finally:
            doc.close()
    except ImportError:
        pass
    try:
        from pypdf import PdfReader

        return len(PdfReader(path).pages)
    except Exception as exc:  # noqa: BLE001
        raise RenderError(f"{path}: cannot read the PDF ({exc})", "unreadable") from exc


def _pdf_pieces(path: str, pages: Sequence[int], dpi: int, rendered: List[int]) -> Iterator[Piece]:
    try:
        import pypdfium2 as pdfium
    except ImportError:
        pdfium = None
    if pdfium is not None:
        doc = pdfium.PdfDocument(path)
        try:
            for p in pages:
                bitmap = doc[p - 1].render(scale=dpi / 72.0)
                img = bitmap.to_pil().convert("RGB")
                rendered.append(p)
                yield Piece(p, 0, img)
        finally:
            doc.close()
        return
    try:
        from pdf2image import convert_from_path
    except ImportError as exc:
        raise RenderError("Rendering a PDF needs pypdfium2 or pdf2image (with poppler). Install one: python -m pip install pypdfium2",
                          "dependencies_missing") from exc
    for p in pages:
        imgs = convert_from_path(path, dpi=dpi, first_page=p, last_page=p)
        if imgs:
            rendered.append(p)
            yield Piece(p, 0, imgs[0].convert("RGB"))


def render_pdf(path: str, *, pages: Any = None, max_pages: int = 100, dpi: int = PDF_DPI) -> Rendered:
    total = pdf_page_count(path)
    wanted = parse_pages(pages, total)
    notes: List[str] = []
    if len(wanted) > max_pages:
        notes.append(f"{len(wanted)} pages requested, only the first {max_pages} were indexed (visual_index_max_pages); "
                     f"add the rest with pages=")
        wanted = wanted[:max_pages]
    rendered: List[int] = []
    r = Rendered(source=path, kind="pdf", pieces=iter(()), fingerprint=file_md5(path), title=os.path.basename(path),
                 pages_total=total, pages_rendered=rendered, notes=notes)
    r.pieces = _pdf_pieces(path, wanted, dpi, rendered)
    r.metrics["partial_pages"] = list(wanted) if pages not in (None, "", []) or len(wanted) < total else []
    return r


# ------------------------------------------------------------------------------------------------ image
def render_image_bytes(data: bytes, source: str, *, title: str = "") -> Rendered:
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as exc:  # noqa: BLE001
        raise RenderError(f"{source}: not a readable image ({exc})", "unreadable") from exc
    if img.mode in ("RGBA", "LA", "P"):
        base = Image.new("RGB", img.size, (255, 255, 255))
        rgba = img.convert("RGBA")
        base.paste(rgba, mask=rgba.split()[-1])
        img = base
    else:
        img = img.convert("RGB")
    return Rendered(source=source, kind="image", pieces=iter([Piece(1, 0, img)]),
                    fingerprint=hashlib.md5(data).hexdigest(), title=title or os.path.basename(source), pages_total=1,
                    pages_rendered=[1])


def render_image(path: str) -> Rendered:
    with open(path, "rb") as f:
        return render_image_bytes(f.read(), path)


# ------------------------------------------------------------------------------------------------ download
def download(url: str, *, timeout: float = 30.0, max_bytes: int = MAX_DOWNLOAD_BYTES) -> bytes:
    """A PDF or picture by URL through the SSRF-guarded outbound broker (public destinations only)."""
    from src import outbound_fetch as of

    try:
        res = of.fetch(url, profile=of.PUBLIC_UNTRUSTED, timeout=timeout, max_bytes=max_bytes,
                       headers={"User-Agent": "Mozilla/5.0 (compatible; Faustus-visual-index/1.0)"},
                       allowed_mime=("application/pdf", "image/", "application/octet-stream"))
    except of.OutboundPolicyError as exc:
        raise RenderError(f"{url}: {exc}", "policy") from exc
    except Exception as exc:  # noqa: BLE001
        raise RenderError(f"{url}: download failed ({exc})", "download_failed") from exc
    if res.status_code >= 400:
        raise RenderError(f"{url}: HTTP {res.status_code}", "http_error")
    if not res.content:
        raise RenderError(f"{url}: empty response", "empty")
    return res.content


# ------------------------------------------------------------------------------------------------ web capture
#: Counted in the live page after it settled; the same numbers feed the "is this page mostly a picture of data" test.
PAGE_METRICS_JS = """() => {
  const doc = document.documentElement, body = document.body || doc;
  const text = (body.innerText || '').trim();
  const big = (el) => { const r = el.getBoundingClientRect(); return r.width >= 200 && r.height >= 120; };
  const imgs = Array.from(document.images || []);
  return {
    height: Math.max(doc.scrollHeight, body.scrollHeight, doc.offsetHeight),
    text_chars: text.length,
    tables: document.querySelectorAll('table').length,
    canvases: document.querySelectorAll('canvas').length,
    svgs: document.querySelectorAll('svg').length,
    big_images: imgs.filter(big).length,
    images: imgs.length,
    iframes: document.querySelectorAll('iframe').length,
    title: document.title || ''
  };
}"""


@dataclass
class WebCapture:
    tiles: List[Tuple[int, Image.Image]]      # (y_offset, tile)
    metrics: Dict[str, Any]
    final_url: str = ""
    title: str = ""
    total_height: int = 0
    truncated: bool = False


def capture_page(url: str, *, owner: Optional[str] = None, width: int = STRIP_WIDTH, tile_height: int = TILE_HEIGHT,
                 max_tiles: int = 8, timeout_s: float = 30.0, settle_s: float = 6.0, cancel: Any = None,
                 profile: str = "research") -> WebCapture:
    """Open ``url`` in the browser at ``width`` px and take the full page in tiles of at most ``tile_height`` px.

    Raises :class:`RenderError`. Runs on the calling thread (call it with ``asyncio.to_thread`` from async code)."""
    from src import browser_lanes
    from src.hoard_link.web import safety
    from src.reach import browser as reach_browser

    problem = safety.check_url(url, safety.PUBLIC)
    if problem:
        raise RenderError(f"{url}: {problem}", "policy")
    rung = reach_browser.browser_for(owner, profile)
    if not rung.available():
        raise RenderError(rung.unavailable_reason() or "no browser available", "browser_unavailable")
    started = time.monotonic()
    try:
        with reach_browser._profile_use(owner, profile, timeout_s, cancel) as remaining:
            with browser_lanes.read_lane(url, purpose="visual_index capture"):
                return _capture_in_session(rung, url, width, tile_height, max_tiles, min(remaining, timeout_s + settle_s + 20),
                                           timeout_s, settle_s)
    except RenderError:
        raise
    except Exception as exc:  # noqa: BLE001 - lane busy, browser crash, navigation error
        raise RenderError(f"{url}: {type(exc).__name__}: {str(exc)[:200]}", "capture_failed") from exc
    finally:
        logger.debug("captured %s in %.1fs", url, time.monotonic() - started)


def _capture_in_session(rung: Any, url: str, width: int, tile_height: int, max_tiles: int, budget_s: float,
                        timeout_s: float, settle_s: float) -> WebCapture:
    with rung.browser_session(headless=True) as ctx:
        page = ctx.new_page()
        try:
            page.set_viewport_size({"width": int(width), "height": 900})
            page.goto(url, wait_until="domcontentloaded", timeout=int(timeout_s * 1000))
            try:
                page.wait_for_load_state("networkidle", timeout=int(settle_s * 1000))
            except Exception:  # noqa: BLE001 - chatty pages never go idle
                pass
            metrics = page.evaluate(PAGE_METRICS_JS) or {}
            height = int(metrics.get("height") or 0)
            if height <= 0:
                raise RenderError(f"{url}: the page has no height to capture", "empty")
            # Lazy images load when scrolled to: walk down once, bounded, then come back to the top.
            limit = min(height, tile_height * max_tiles)
            y = 0
            while y < limit and y < 40000:
                page.evaluate("(y) => window.scrollTo(0, y)", y)
                page.wait_for_timeout(120)
                y += 900
            page.evaluate("() => window.scrollTo(0, 0)")
            page.wait_for_timeout(200)
            tiles: List[Tuple[int, Image.Image]] = []
            y = 0
            while y < limit:
                h = min(tile_height, limit - y)
                png = page.screenshot(full_page=True, type="png", clip={"x": 0, "y": y, "width": int(width), "height": int(h)})
                tiles.append((y, Image.open(io.BytesIO(png)).convert("RGB")))
                y += h
            return WebCapture(tiles=tiles, metrics=metrics, final_url=page.url, title=str(metrics.get("title") or ""),
                              total_height=height, truncated=height > limit)
        finally:
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass


def render_web(url: str, *, owner: Optional[str] = None, max_tiles: int = 8, capture: Optional[Callable[..., WebCapture]] = None,
               **kwargs: Any) -> Rendered:
    cap = (capture or capture_page)(url, owner=owner, max_tiles=max_tiles, **kwargs)
    notes = []
    if cap.truncated:
        notes.append(f"the page is {cap.total_height}px tall; only the first {max_tiles * TILE_HEIGHT}px were captured")
    return Rendered(source=url, kind="web", pieces=iter([Piece(1, y, im) for y, im in cap.tiles]), title=cap.title or url,
                    pages_total=1, pages_rendered=[1], notes=notes, metrics=dict(cap.metrics))


# ------------------------------------------------------------------------------------------------ dispatcher
@dataclass
class Renderers:
    """The four ways of turning a source into images; tests replace any of them."""

    pdf: Callable[..., Rendered] = render_pdf
    image: Callable[[str], Rendered] = render_image
    web: Callable[..., Rendered] = render_web
    download: Callable[..., bytes] = download


def resolve_local_path(raw: str) -> str:
    """A local source path, confined to the workspace/DATA_DIR allowlist (same helper ``read_file`` uses)."""
    from src.tool_execution import _resolve_tool_path

    try:
        return _resolve_tool_path(raw)
    except ValueError as exc:
        raise RenderError(str(exc), "path_not_allowed") from exc


def render_source(source: str, *, owner: Optional[str] = None, pages: Any = None, max_pages: int = 100, max_tiles: int = 8,
                  renderers: Optional[Renderers] = None, resolve: Optional[Callable[[str], str]] = None) -> Rendered:
    r = renderers or Renderers()
    source = str(source or "").strip()
    if not source:
        raise RenderError("empty source", "invalid_arguments")
    if is_url(source):
        kind = kind_of(source)
        if kind in ("pdf", "image"):
            data = r.download(source)
            if kind == "image":
                return render_image_bytes(data, source)
            import tempfile

            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp.write(data)
            rendered = r.pdf(tmp.name, pages=pages, max_pages=max_pages)
            rendered.source = source
            rendered.title = os.path.basename(source.split("?", 1)[0]) or source
            _cleanup_when_done(rendered, tmp.name)
            return rendered
        return r.web(source, owner=owner, max_tiles=max_tiles)
    path = (resolve or resolve_local_path)(source)
    if not os.path.isfile(path):
        raise RenderError(f"{source}: file not found", "not_found")
    kind = kind_of(path)
    if kind == "pdf":
        rendered = r.pdf(path, pages=pages, max_pages=max_pages)
    elif kind == "image":
        rendered = r.image(path)
    else:
        raise RenderError(f"{source}: unsupported type (a PDF, a picture, or an http/https URL)", "unsupported")
    rendered.source = path
    return rendered


def _cleanup_when_done(rendered: Rendered, tmp_path: str) -> None:
    inner = rendered.pieces

    def wrapper() -> Iterator[Piece]:
        try:
            yield from inner
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    rendered.pieces = wrapper()
