"""Document, file and knowledge helpers every Hoard app used to copy (HoardLink 0.8 "commons").

Standard library only at import time; Pillow, numpy, pypdfium2 and pypdf are imported inside the functions
that need them. Nothing is imported here on purpose: ``from hoard_link.docs import textsearch`` loads one
module, not the whole package.

* :mod:`~hoard_link.docs.textsearch` — safe FTS5 queries, BM25 re-scoring, highlights, stopwords.
* :mod:`~hoard_link.docs.chunking` — overlapping chunks that keep page, section, line and offsets.
* :mod:`~hoard_link.docs.sniff` — what a file really is (magic bytes, ZIP members), MIME by extension.
* :mod:`~hoard_link.docs.pageranges` — ``"1-3,5,8-"``, ``"last"``, ``"odd"`` and Spanish words.
* :mod:`~hoard_link.docs.vecmath` — float32 BLOBs, cosine, top-k, rank fusion.
* :mod:`~hoard_link.docs.citations` — one way to write «Title», p. 12.
* :mod:`~hoard_link.docs.textclean` — text cleaning, header/footer stripping, encoding fallback.
* :mod:`~hoard_link.docs.readers_lite` — docx, odt, rtf, html, pptx, xlsx, epub and PDF text without extra packages.
* :mod:`~hoard_link.docs.imaging` — thumbnails, EXIF, perceptual hashes, compress-to-limit (Pillow).
* :mod:`~hoard_link.docs.archives` — safe names, never-overwrite writes, zip-slip and zip-bomb guards.

The Node twin of the parts Node apps need is ``js/hoard-commons/docs.js``. See ``docs/commons/docs.md``.
"""

from __future__ import annotations

__all__ = [
    "textsearch", "chunking", "sniff", "pageranges", "vecmath", "citations", "textclean", "readers_lite",
    "imaging", "archives",
]
