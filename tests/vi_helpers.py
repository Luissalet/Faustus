"""Shared builders for the visual index tests: synthetic pages, a service on a fake embedder, a fake browser."""
from __future__ import annotations

from typing import Callable, List, Optional, Tuple

from PIL import Image

from src.visual_index import service as svc
from src.visual_index.embedder import EmbedderPool, FakeEmbedder
from src.visual_index.render import Piece, Rendered, Renderers, WebCapture, render_image_bytes
from src.visual_index.store import VisualStore

RED, BLUE, GREEN = (220, 40, 40), (40, 70, 220), (40, 190, 60)


def page(bands: List[Tuple[int, Tuple[int, int, int]]], width: int = 875) -> Image.Image:
    """A picture made of horizontal colour bands ``(height, rgb)``; white bands count as blank."""
    img = Image.new("RGB", (width, sum(h for h, _ in bands)), (255, 255, 255))
    y = 0
    for h, rgb in bands:
        band = Image.new("RGB", (width, h), rgb)
        if rgb != (255, 255, 255):                      # a uniform strip counts as blank; draw "text" lines so it is content
            ink = tuple(c // 2 for c in rgb)
            for line in range(5, h, 40):
                band.paste(Image.new("RGB", (width, 2), ink), (0, line))
        img.paste(band, (0, y))
        y += h
    return img


def make_service(tmp_path, *, embedder: Optional[FakeEmbedder] = None, renderers: Optional[Renderers] = None):
    emb = embedder or FakeEmbedder()
    pool = EmbedderPool(lambda: emb, idle_s=0)
    service = svc.VisualIndexService(store=VisualStore(str(tmp_path / "vi")), pool=pool, renderers=renderers,
                                     resolve=lambda p: p)
    return service, emb


def png_file(tmp_path, name: str, image: Image.Image) -> str:
    path = tmp_path / name
    image.save(path)
    return str(path)


def fake_web(pages: dict) -> Renderers:
    """``pages``: url -> list of (y_offset, image). The browser is never opened."""
    calls: List[str] = []

    def web(url, owner=None, max_tiles=8, **kw):
        calls.append(url)
        tiles = pages[url]
        return Rendered(source=url, kind="web", pieces=iter([Piece(1, y, im.copy()) for y, im in tiles]), title=f"title of {url}",
                        pages_total=1, pages_rendered=[1])

    r = Renderers(web=web)
    r.calls = calls  # type: ignore[attr-defined]
    return r


def fake_capture(pages: dict) -> Callable[..., WebCapture]:
    def capture(url, owner=None, max_tiles=8, **kw):
        tiles = pages[url]
        return WebCapture(tiles=list(tiles), metrics={}, final_url=url, title="t", total_height=sum(i.height for _, i in tiles))

    return capture
