"""One way to write a citation, so every app that answers from documents points at its sources the same way.

Standard library only. The Node twin is ``cite`` in ``js/hoard-commons/docs.js``; both are checked against
``tests/vectors/docs_cite.json``.

Spanish (default, the family's language)::

    doc/page   «Contrato», p. 12
    doc        informe.md § Resultados          informe.md, l. 40          informe.md
    book       «El Aleph», cap. 3
    chat       [chat «Viaje a Lisboa» · 2026-09-30 · turno 4]
    link       [enlace «Receta de pan» · example.org]
    mail       [correo «Factura de octubre» · banco@example.org · 2026-10-01]
    code       src/app.py § Install:42

English (``lang="en"``): curly quotes, ``ch.``, ``line``, ``turn``, ``[link …]``, ``[mail …]``.

Replaces: Borges ``queries.citation`` (the base), Vitruvius ``_cite``, Faustus ``pdf_page_evidence_ref`` text.
"""

from __future__ import annotations

from typing import Any, Optional

__all__ = ["cite", "KINDS"]

KINDS = ("doc", "page", "book", "chat", "link", "mail", "code")

_WORDS = {
    "es": {"q": ("«", "»"), "page": "p.", "chapter": "cap.", "line": "l.", "turn": "turno", "chat": "chat",
           "link": "enlace", "mail": "correo"},
    "en": {"q": ("“", "”"), "page": "p.", "chapter": "ch.", "line": "line", "turn": "turn", "chat": "chat",
           "link": "link", "mail": "mail"},
}


def _s(v: Any) -> str:
    if v is None:
        return ""
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v).strip()


def cite(kind: str, title: Any, *, page: Any = None, section: Any = None, line: Any = None, date: Any = None,
         turn: Any = None, site: Any = None, lang: str = "es") -> str:
    """The citation text for a source. ``kind`` is ``doc`` (a file: page, section or line), ``page`` (a document
    page, always ``«Title», p. N``), ``book`` (chapter in ``section``), ``chat`` (date, turn), ``link`` (site),
    ``mail`` (``site`` is the sender, plus date) or ``code`` (path, ``section`` heading, ``line``). Missing parts
    are left out. ``ValueError`` for an unknown ``kind``."""
    if kind not in KINDS:
        raise ValueError(f"unknown citation kind {kind!r}; use one of {', '.join(KINDS)}")
    w = _WORDS["en" if str(lang).lower().startswith("en") else "es"]
    lq, rq = w["q"]
    t, sec, ln, pg = _s(title), _s(section), _s(line), _s(page)
    quoted = f"{lq}{t}{rq}"

    if kind in ("doc", "page"):
        if pg:
            return f"{quoted}, {w['page']} {pg}" + (f" · {sec}" if sec and kind == "doc" else "")
        if kind == "page":
            return quoted
        if sec:
            return f"{t} § {sec}"
        if ln:
            return f"{t}, {w['line']} {ln}"
        return t
    if kind == "book":
        return f"{quoted}, {w['chapter']} {sec}" if sec else quoted
    if kind == "chat":
        parts = [p for p in (_s(date), sec, f"{w['turn']} {_s(turn)}" if _s(turn) else "") if p]
        return f"[{w['chat']} {quoted}" + (f" · {' · '.join(parts)}]" if parts else "]")
    if kind == "link":
        s = _s(site)
        return f"[{w['link']} {quoted}" + (f" · {s}]" if s else "]")
    if kind == "mail":
        parts = [p for p in (_s(site), _s(date)) if p]
        return f"[{w['mail']} {quoted}" + (f" · {' · '.join(parts)}]" if parts else "]")
    # code
    out = t
    if sec:
        out += f" § {sec}"
    if ln:
        out += f":{ln}"
    return out
