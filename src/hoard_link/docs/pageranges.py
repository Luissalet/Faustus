"""Page ranges as people write them: ``1-3,5,8-``, ``last``, ``-1`` (the last page), ``3-last``, ``odd``, ``even``,
``all`` and the Spanish words (``última``, ``impares``, ``pares``, ``todas``, ``pág. 3``, ``1 a 3``, ``2 y 5``).

Standard library only (plus :func:`hoard_link.text.fold`). The Node twin is ``js/hoard-commons/docs.js``
(``parseRanges``, ``parseGroups``, ``describeRanges``), checked against ``tests/vectors/docs_ranges.json``.

Pages are 1-based. Every mistake raises :class:`PageRangeError` (a ``ValueError``) whose text says what was wrong
and how to write it, in Spanish by default (``lang="es"``) or English; both texts are on the exception
(``message_es``, ``message_en``) so a UI can show either.

Replaces: Kafka ``workshop/ranges.py`` (the base), Faustus ``pdf_ops.parse_page_ranges`` (which kept order and
swapped reversed ranges: ``allow_reversed=True``) and Hypatia ``pdfTools/split.ts parsePageRanges`` (which
silently dropped invalid entries; here they are errors).
"""

from __future__ import annotations

import re
from typing import Sequence

from ..text import fold

__all__ = ["PageRangeError", "parse_ranges", "parse_groups", "describe", "EXAMPLES_ES", "EXAMPLES_EN"]

EXAMPLES_ES = "Ejemplos: 3, 1-3, 2,5,8-, last, -1 (la última), 3-last, impares, pares, todas."
EXAMPLES_EN = "Examples: 3, 1-3, 2,5,8-, last, -1 (the last page), 3-last, odd, even, all."

_LAST = ("last", "ultima", "ultimo", "final", "fin", "end")
_ALL = {"all", "todas", "todo", "todos"}
_ODD = {"odd", "impar", "impares"}
_EVEN = {"even", "par", "pares"}
_RANGE = re.compile(r"^(?P<a>-?\d+|last)-(?P<b>-?\d+|last)?$")
_SINGLE = re.compile(r"^(?:-?\d+|last)$")


class PageRangeError(ValueError):
    """A page range that cannot be read. ``str(error)`` is in ``lang``; both languages are kept."""

    def __init__(self, message_es: str, message_en: str, lang: str = "es"):
        self.message_es = message_es
        self.message_en = message_en
        self.lang = lang
        super().__init__(message_en if lang == "en" else message_es)

    @property
    def es(self) -> str:
        return self.message_es

    @property
    def en(self) -> str:
        return self.message_en


def _err(lang: str, es: str, en: str) -> PageRangeError:
    return PageRangeError(f"{es} {EXAMPLES_ES}", f"{en} {EXAMPLES_EN}", lang)


def _norm(text: object) -> str:
    t = fold("" if text is None else str(text)).strip()
    t = re.sub(r"[‒-―−]", "-", t)                                   # en dash, em dash, minus sign
    t = re.sub(r"\.\.+", "-", t)
    t = re.sub(r"(?<=[\dt])\s+(?:a|hasta)\s+(?=[\d-]|last|ultima|final|fin|end)", "-", t)   # «1 a 3», «3 hasta last»
    t = re.sub(r"\s+y\s+", ",", t)
    t = re.sub(r"\s*-\s*", "-", t)
    t = re.sub(r"\b(?:pagina|paginas|pag|pags|p)\.?\s*(?=\d|-|last)", "", t)   # «pág. 3», «p. 3»
    for word in _LAST:
        t = re.sub(rf"\b{word}\b", "last", t)
    return t


def _one(token: str, total: int, original: str, lang: str) -> int:
    if token == "last":
        return total
    n = int(token)
    if n == 0:
        raise _err(lang, "La página 0 no existe: las páginas empiezan en 1.", "Page 0 does not exist: pages start at 1.")
    if n < 0:
        n = total + n + 1
        if n < 1:
            raise _err(lang, f"«{original}» queda antes de la primera página: el documento tiene {total} página(s).",
                       f"“{original}” is before the first page: the document has {total} page(s).")
        return n
    if n > total:
        raise _err(lang, f"La página {n} no existe: el documento tiene {total} página(s).",
                   f"Page {n} does not exist: the document has {total} page(s).")
    return n


def parse_groups(text: object, total: int, *, allow_reversed: bool = False, lang: str = "es") -> list[list[int]]:
    """One list of 1-based pages per comma-separated part, in the order written (``"1-3,5"`` -> ``[[1,2,3],[5]]``).
    ``allow_reversed`` reads ``5-3`` as ``3-5`` instead of raising."""
    if total < 1:
        raise _err(lang, "El documento no tiene páginas.", "The document has no pages.")
    norm = _norm(text)
    if not norm:
        raise _err(lang, "Indica las páginas.", "Say which pages.")
    groups: list[list[int]] = []
    for token in (t for t in re.split(r"[,;\s]+", norm) if t):
        if token in _ALL:
            groups.append(list(range(1, total + 1)))
        elif token in _ODD:
            groups.append(list(range(1, total + 1, 2)))
        elif token in _EVEN:
            groups.append(list(range(2, total + 1, 2)))
        elif _SINGLE.match(token):
            groups.append([_one(token, total, token, lang)])
        else:
            m = _RANGE.match(token)
            if not m:
                raise _err(lang, f"No entiendo «{token}» como página o rango.", f"“{token}” is not a page or a range.")
            first = _one(m.group("a"), total, token, lang)
            last = _one(m.group("b"), total, token, lang) if m.group("b") else total
            if first > last:
                if not allow_reversed:
                    raise _err(lang, f"El rango «{token}» está al revés: la primera página ({first}) es mayor que la última ({last}).",
                               f"The range “{token}” is backwards: the first page ({first}) is after the last ({last}).")
                first, last = last, first
            groups.append(list(range(first, last + 1)))
    if not groups:
        raise _err(lang, "Indica las páginas.", "Say which pages.")
    return groups


def parse_ranges(text: object, total: int, *, default_all: bool = False, unique: bool = True,
                 allow_reversed: bool = False, lang: str = "es") -> list[int]:
    """Flat list of 1-based pages in the order written. Empty text means every page when ``default_all``.
    ``unique`` drops repeats (``"1-3,2"`` -> ``[1,2,3]``); ``unique=False`` keeps them (to repeat a page)."""
    if not str(text or "").strip() and default_all:
        return list(range(1, total + 1))
    pages = [p for group in parse_groups(text, total, allow_reversed=allow_reversed, lang=lang) for p in group]
    if unique:
        seen: set[int] = set()
        pages = [p for p in pages if not (p in seen or seen.add(p))]
    return pages


def describe(pages: Sequence[int]) -> str:
    """``"1-3,5,8-9"`` for a list of pages (sorted, without repeats)."""
    out: list[str] = []
    items = sorted(set(pages))
    i = 0
    while i < len(items):
        j = i
        while j + 1 < len(items) and items[j + 1] == items[j] + 1:
            j += 1
        out.append(str(items[i]) if i == j else f"{items[i]}-{items[j]}")
        i = j + 1
    return ",".join(out)
