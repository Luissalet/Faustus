"""anchors.py -- shared anchors into a page, a time range, a cell, a 3D object, a line or a symbol (Hangar E22, first part).

An anchor says *where* inside a file a remark points:

==========  ==============================  ======================
kind        written as                      example
==========  ==============================  ======================
page        ``p3`` or ``p3@x,y,w,h``         ``p3@120,80,200,40``
time        ``t=mm:ss`` or ``t=a-b``         ``t=00:01:10-00:01:25``
cell        ``Sheet!A1`` or ``cell:B4``      ``Presupuesto!C12``
object      ``obj:name``                     ``obj:Tapa``
line        ``L120`` or ``L120-140``         ``L42``
symbol      ``sym:name``                     ``sym:compute_total``
==========  ==============================  ======================

``parse`` reads the canonical form and the everyday Spanish/English wording
people use in feedback ("página 3", "min 1:10", "celda B4", "pieza Tapa",
"línea 42"). ``ref`` joins an anchor to a versioned file so the same
identifier keeps pointing at that revision after another one exists, e.g.
``review:caja-final@v2#p3`` -- the review desk (Hangar E32) uses it for
comments; any tool can resolve it with ``parse_ref``.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional

KINDS = ("page", "time", "cell", "object", "line", "symbol")

_TIME = r"\d{1,2}:\d{2}(?::\d{2})?"
_RECT = r"(?:@|\s*\(\s*)(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\)?"
_PAGE = re.compile(r"(?:^|(?<=[\s(,;]))(?:p\.?\s*|p[áa]g(?:ina)?\.?\s*|page\s+|hoja\s+(?=\d))(\d{1,4})\b(?:" + _RECT + r")?", re.I)
_TIME_RE = re.compile(r"(?:t\s*=\s*|\bmin(?:uto)?\.?\s+|\bat\s+|\ben el\s+|\bsegundo\s+)(" + _TIME + r")(?:\s*(?:-|–|a|to|al)\s*(" + _TIME + r"))?"
                      r"|(?<![\d:])(" + _TIME + r")\s*(?:-|–)\s*(" + _TIME + r")(?![\d:])", re.I)
_CELL = re.compile(r"(?:\b(?:celda|cell)\s*:?\s*)(?:([\w ]{1,40})!)?([A-Z]{1,3}[1-9]\d{0,6})\b|\b([A-Za-z_][\w]{0,40})!([A-Z]{1,3}[1-9]\d{0,6})\b")
_OBJECT = re.compile(r"\b(obj|objeto|object|pieza|part|mesh|malla)(\s*:\s*|\s+)([«\"']?)([\w.\-]{1,80})", re.I)
_LINE = re.compile(r"(?:\bL|\bl[íi]neas?\s+|\blines?\s+)(\d{1,6})(?:\s*(?:-|–|a|to)\s*(\d{1,6}))?\b")
_SYMBOL = re.compile(r"\b(sym|s[íi]mbolo|symbol|funci[óo]n|function|clase|class)(\s*:\s*|\s+)(`?)([A-Za-z_][\w.]{0,120})`?", re.I)
_REF = re.compile(r"^review:([a-z0-9-]{3,80})@v(\d{1,4})(?:#(.+))?$")


def _secs(t: str) -> int:
    parts = [int(p) for p in t.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def _hms(s: int) -> str:
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def label(a: Dict[str, Any]) -> str:
    k = a.get("kind")
    if k == "page":
        r = a.get("rect")
        return f"p{a['page']}" + (("@" + ",".join(f"{v:g}" for v in r)) if r else "")
    if k == "time":
        return f"t={_hms(a['start'])}" + (f"-{_hms(a['end'])}" if a.get("end") is not None else "")
    if k == "cell":
        return f"{a['sheet']}!{a['cell']}" if a.get("sheet") else f"cell:{a['cell']}"
    if k == "object":
        return f"obj:{a['name']}"
    if k == "line":
        return f"L{a['start']}" + (f"-{a['end']}" if a.get("end") else "")
    if k == "symbol":
        return f"sym:{a['name']}"
    return ""


def _named(m: "re.Match[str]") -> bool:
    """Words such as "part", "class" or "pieza" are ordinary prose ("the first part
    needs work"): in everyday wording they only anchor when the name is marked
    (colon, quotes, backticks), looks like an identifier or starts with a capital."""
    kw, sep, quote, name = m.group(1), m.group(2), m.group(3), m.group(4)
    if kw.lower() in ("obj", "sym") or ":" in sep or quote:
        return True
    return name[:1].isupper() or any(ch in name for ch in "_.0123456789")


def _search(kind: str, rx: "re.Pattern[str]", text: str):
    if kind not in ("object", "symbol"):
        return rx.search(text)
    return next((m for m in rx.finditer(text) if _named(m)), None)


def parse(text: str) -> Optional[Dict[str, Any]]:
    """The first anchor written in ``text`` (canonical or everyday wording), or None."""
    text = str(text or "")
    found = []
    for kind, rx in (("page", _PAGE), ("time", _TIME_RE), ("cell", _CELL), ("object", _OBJECT), ("line", _LINE), ("symbol", _SYMBOL)):
        m = _search(kind, rx, text)
        if m:
            found.append((m.start(), kind, m))
    if not found:
        return None
    _, kind, m = min(found, key=lambda x: x[0])
    if kind == "page":
        a: Dict[str, Any] = {"kind": "page", "page": int(m.group(1))}
        if m.group(2):
            a["rect"] = [float(m.group(i)) for i in range(2, 6)]
    elif kind == "time":
        start, end = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        a = {"kind": "time", "start": _secs(start), "end": _secs(end) if end else None}
        if a["end"] is not None and a["end"] < a["start"]:
            a["start"], a["end"] = a["end"], a["start"]
    elif kind == "cell":
        sheet, cell = (m.group(1), m.group(2)) if m.group(2) else (m.group(3), m.group(4))
        a = {"kind": "cell", "sheet": (sheet or "").strip(), "cell": cell}
    elif kind == "object":
        a = {"kind": "object", "name": m.group(4).rstrip(".,;")}
    elif kind == "line":
        a = {"kind": "line", "start": int(m.group(1)), "end": int(m.group(2)) if m.group(2) else None}
    else:
        a = {"kind": "symbol", "name": m.group(4).rstrip(".")}
    a["label"] = label(a)
    return a


def ref(desk_id: str, version: int, anchor: Optional[Dict[str, Any]] = None) -> str:
    return f"review:{desk_id}@v{int(version)}" + (f"#{anchor['label']}" if anchor else "")


def parse_ref(text: str) -> Optional[Dict[str, Any]]:
    m = _REF.match(str(text or "").strip())
    if not m:
        return None
    return {"desk": m.group(1), "version": int(m.group(2)), "anchor": parse(m.group(3)) if m.group(3) else None}
