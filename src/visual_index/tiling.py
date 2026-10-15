"""Cutting a rendered page into strips the embedder can take, with a hash per strip.

A rendered page (a full-page capture of a web page, a PDF page at 200 dpi, a loose image) is first split into *tiles* no
taller than ``TILE_HEIGHT`` (8192 px, what a browser can capture in one piece), and each tile into *strips*: a grid of
at most ``STRIP_HEIGHT`` (1024) px high by ``STRIP_WIDTH`` (875) px wide pieces, in row-major order. A page narrower than
``STRIP_WIDTH`` gives one column of full-width strips; a wider one (a PDF page, a landscape capture) is also cut across,
so no strip is ever scaled down before it reaches the model. A tail shorter than ``MIN_SIDE`` (28 px, one patch of the
vision encoder) is dropped. Strips that are one flat colour (page margins, empty areas) are left out: nothing in them
can be retrieved, and they only dilute the results.

Every strip carries the MD5 of its pixels. Indexing the same source again re-embeds only the strips whose hash changed
(see :mod:`.store`).

The strip geometry (1024 x 875 px strips, 28 px minimum, MD5 per piece) follows the open-source PixelRAG chunker
(``embed/src/pixelrag_embed/chunk.py``), Copyright the PixelRAG authors, Apache License 2.0; this module is a
reimplementation for Faustus' own data model, not a copy. See ``THIRD_PARTY_NOTICES.md``.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Iterator, List, Optional, Tuple

from PIL import Image

Image.MAX_IMAGE_PIXELS = None          # a tall capture can pass the default decompression-bomb limit; sizes are bounded below

TILE_HEIGHT = 8192
STRIP_HEIGHT = 1024
STRIP_WIDTH = 875
INK_DELTA = 24                 # grey levels from the background that count as ink
INK_MIN_FRACTION = 0.004       # below 0.4 % ink a strip is treated as blank
MIN_SIDE = 28
#: A strip whose pixel values vary less than this (max - min over the greyscale image) is "blank".
BLANK_RANGE = 6


@dataclass
class Strip:
    """One piece of one rendered page. ``y`` is in page coordinates (not tile coordinates)."""

    page: int
    x: int
    y: int
    width: int
    height: int
    md5: str
    image: Image.Image = field(repr=False, compare=False)

    def key(self) -> Tuple[int, int, int]:
        return (self.page, self.x, self.y)


def image_md5(image: Image.Image) -> str:
    """MD5 of the pixels (mode and size included), so equal strips hash equal whatever file they came from."""
    h = hashlib.md5()
    h.update(f"{image.mode}:{image.size[0]}x{image.size[1]}".encode())
    h.update(image.tobytes())
    return h.hexdigest()


def is_blank(image: Image.Image) -> bool:
    """A strip with nothing to find: one flat colour, or so little ink (a page number, a running footer, a rule) that it
    would only match every query a little. Ink is a pixel far from the strip's commonest grey."""
    grey = image.convert("L")
    lo, hi = grey.getextrema()
    if (hi - lo) <= BLANK_RANGE:
        return True
    hist = grey.histogram()
    bg = max(range(256), key=hist.__getitem__)
    ink = sum(c for v, c in enumerate(hist) if abs(v - bg) > INK_DELTA)
    return ink < INK_MIN_FRACTION * grey.width * grey.height


def _even(total: int, cap: int) -> List[Tuple[int, int]]:
    """``(start, size)`` pieces of ``total`` px, each at most ``cap``, as even as possible (no thin tail: a 2200 px page
    with a 1024 cap gives 734 + 733 + 733, not 1024 + 1024 + 152, so a footer never becomes a strip of its own)."""
    if total <= 0:
        return []
    n = -(-total // cap)
    base, extra = divmod(total, n)
    out, pos = [], 0
    for i in range(n):
        size = base + (1 if i < extra else 0)
        out.append((pos, size))
        pos += size
    return out


def grid(width: int, height: int, *, strip_height: int = STRIP_HEIGHT, strip_width: int = STRIP_WIDTH,
         min_side: int = MIN_SIDE) -> List[Tuple[int, int, int, int]]:
    """The ``(x, y, w, h)`` boxes of the strips of a ``width x height`` image, row-major, every side at most the cap and
    the pieces of a row or a column as even as possible; pieces under ``min_side`` are dropped."""
    boxes: List[Tuple[int, int, int, int]] = []
    for y, h in _even(height, strip_height):
        if h < min_side:
            continue
        for x, w in _even(width, strip_width):
            if w < min_side:
                continue
            boxes.append((x, y, w, h))
    return boxes


def split_tiles(image: Image.Image, tile_height: int = TILE_HEIGHT) -> Iterator[Tuple[int, Image.Image]]:
    """``(y_offset, tile)`` pieces of an image taller than ``tile_height``; a shorter one comes back whole."""
    width, height = image.size
    if height <= tile_height:
        yield 0, image
        return
    y = 0
    while y < height:
        h = min(tile_height, height - y)
        yield y, image.crop((0, y, width, y + h))
        y += h


def strips_from_image(image: Image.Image, *, page: int = 1, y_offset: int = 0, skip_blank: bool = True,
                      strip_height: int = STRIP_HEIGHT, strip_width: int = STRIP_WIDTH,
                      min_side: int = MIN_SIDE) -> List[Strip]:
    """Strips of one rendered image (a page, or one tile of a tall page placed at ``y_offset``)."""
    rgb = image if image.mode == "RGB" else image.convert("RGB")
    out: List[Strip] = []
    for x, y, w, h in grid(rgb.width, rgb.height, strip_height=strip_height, strip_width=strip_width, min_side=min_side):
        piece = rgb.crop((x, y, x + w, y + h))
        if skip_blank and is_blank(piece):
            continue
        out.append(Strip(page=page, x=x, y=y + y_offset, width=w, height=h, md5=image_md5(piece), image=piece))
    return out


def strips_from_page(image: Image.Image, *, page: int = 1, tile_height: int = TILE_HEIGHT, skip_blank: bool = True,
                     strip_height: int = STRIP_HEIGHT, strip_width: int = STRIP_WIDTH,
                     min_side: int = MIN_SIDE) -> List[Strip]:
    """Strips of a whole rendered page: tiled first (bounded memory for a very tall capture), then cut."""
    out: List[Strip] = []
    for y_offset, tile in split_tiles(image, tile_height):
        out.extend(strips_from_image(tile, page=page, y_offset=y_offset, skip_blank=skip_blank,
                                     strip_height=strip_height, strip_width=strip_width, min_side=min_side))
    return out


def strip_png_bytes(strip: Strip) -> bytes:
    import io

    buf = io.BytesIO()
    strip.image.save(buf, format="PNG")
    return buf.getvalue()


def describe_geometry() -> dict:
    return {"tile_height": TILE_HEIGHT, "strip_height": STRIP_HEIGHT, "strip_width": STRIP_WIDTH, "min_side": MIN_SIDE}
