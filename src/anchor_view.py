"""anchor_view.py -- open a file exactly at a shared anchor (Hangar E22, second part).

``view(path, anchor)`` returns what a viewer needs to show the place an anchor
(``src/anchors.py``) points at, read from the file itself:

* page   -- PDF: the page (and its text); with ``image`` a PNG of the page with the rectangle drawn on it;
* time   -- audio/video: start and end seconds, ready for a media fragment ``#t=a,b``;
* cell   -- XLSX/CSV: the cells around it with the target marked (sheet checked);
* line   -- text or code: the lines with three of context on each side;
* symbol -- code: where ``def``/``class``/``function``/``const`` (and similar) declares it, shown as lines;
* object -- 3D (OBJ, GLB/GLTF, 3MF, STL): the object names in the file with the target marked.

It never writes and never runs the file. ``found`` says whether the place exists in
that version (a page past the end, a missing cell or object is reported, not hidden).
"""
from __future__ import annotations

import base64
import csv
import io
import json
import re
import struct
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

TEXT_EXT = {".txt", ".md", ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".html", ".htm", ".css", ".xml", ".yaml", ".yml",
            ".ini", ".toml", ".csv", ".tsv", ".sql", ".sh", ".ps1", ".c", ".h", ".cpp", ".cs", ".java", ".go", ".rs", ".rb",
            ".php", ".scad", ".gcode", ".srt", ".vtt", ".svg"}
MEDIA_EXT = {".mp4", ".webm", ".mov", ".mkv", ".mp3", ".wav", ".ogg", ".m4a", ".flac"}
CONTEXT = 3
_COL = re.compile(r"^([A-Z]{1,3})(\d+)$")


def _col_index(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n


def _col_letters(n: int) -> str:
    out = ""
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def _lines(path: Path) -> List[str]:
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def _line_window(lines: List[str], start: int, end: Optional[int]) -> Dict[str, Any]:
    end = end or start
    if end < start:                 # «L9-3» written backwards: show the same lines, marked
        start, end = end, start
    found = 1 <= start <= len(lines)
    lo, hi = max(1, start - CONTEXT), min(len(lines), end + CONTEXT)
    return {"found": found, "total_lines": len(lines), "start": start, "end": end,
            "lines": [{"n": i, "text": lines[i - 1][:400], "hit": start <= i <= end} for i in range(lo, hi + 1)] if found else []}


_DECL = r"(?:def|class|function|const|let|var|async\s+def|async\s+function|interface|type|struct|enum|fn|func|module|sub)"


def _symbol(lines: List[str], name: str) -> Dict[str, Any]:
    short = name.split(".")[-1]
    rx = re.compile(rf"^\s*(?:export\s+)?(?:default\s+)?(?:public\s+|private\s+|static\s+)*{_DECL}\s+{re.escape(short)}\b")
    for i, ln in enumerate(lines, 1):
        if rx.search(ln):
            indent = len(ln) - len(ln.lstrip())
            end = i
            for j in range(i + 1, min(len(lines), i + 200) + 1):
                s = lines[j - 1]
                if s.strip() and len(s) - len(s.lstrip()) <= indent and not s.lstrip().startswith((")", "}", "]")):
                    break
                end = j
            return {**_line_window(lines, i, min(end, i + 40)), "declared_at": i}
    return {"found": False, "total_lines": len(lines), "lines": []}


def _cell_grid(rows: List[List[Any]], sheet: str, cell: str, sheets: List[str]) -> Dict[str, Any]:
    m = _COL.match(cell.upper())
    col, row = _col_index(m.group(1)), int(m.group(2))
    lo_r, hi_r = max(1, row - CONTEXT), row + CONTEXT
    lo_c, hi_c = max(1, col - CONTEXT), col + CONTEXT
    grid = []
    for r in range(lo_r, hi_r + 1):
        vals = rows[r - 1] if r - 1 < len(rows) else []
        grid.append({"row": r, "cells": [{"ref": f"{_col_letters(c)}{r}", "value": None if c - 1 >= len(vals) or vals[c - 1] is None else str(vals[c - 1])[:120],
                                          "hit": (r, c) == (row, col)} for c in range(lo_c, hi_c + 1)]})
    value = rows[row - 1][col - 1] if row - 1 < len(rows) and col - 1 < len(rows[row - 1]) else None
    return {"found": value is not None, "sheet": sheet, "sheets": sheets, "cell": cell.upper(), "value": None if value is None else str(value)[:500],
            "columns": [_col_letters(c) for c in range(lo_c, hi_c + 1)], "grid": grid}


def _cells(path: Path, sheet: str, cell: str) -> Dict[str, Any]:
    ext = path.suffix.lower()
    if ext in (".csv", ".tsv"):
        with path.open(encoding="utf-8", errors="replace", newline="") as fh:
            rows = [r for _, r in zip(range(100000), csv.reader(fh, delimiter="\t" if ext == ".tsv" else ","))]
        return _cell_grid(rows, sheet or path.stem, cell, [path.stem])
    from openpyxl import load_workbook
    wb = load_workbook(str(path), read_only=True, data_only=True)
    try:
        names = list(wb.sheetnames)
        pick = next((n for n in names if n.lower() == (sheet or "").lower()), None) if sheet else names[0]
        if pick is None:
            return {"found": False, "sheet": sheet, "sheets": names, "cell": cell.upper(), "reason": f"no sheet {sheet!r}"}
        m = _COL.match(cell.upper())
        need_r = int(m.group(2)) + CONTEXT
        rows = [list(r) for _, r in zip(range(need_r), wb[pick].iter_rows(values_only=True))]
    finally:
        wb.close()                  # a read-only workbook keeps the file locked on Windows until it is closed
    return _cell_grid(rows, pick, cell, names)


def object_names(path: Path) -> List[str]:
    """Names of the objects/meshes/nodes a 3D file declares (OBJ o/g, GLB/GLTF nodes and meshes, 3MF objects, STL solid)."""
    ext = path.suffix.lower()
    names: List[str] = []
    if ext == ".obj":
        for ln in _lines(path):
            if ln.startswith(("o ", "g ")) and ln[2:].strip():
                names.append(ln[2:].strip())
    elif ext in (".glb", ".gltf"):
        if ext == ".glb":
            data = path.read_bytes()
            length = struct.unpack_from("<I", data, 12)[0]
            doc = json.loads(data[20:20 + length].decode("utf-8"))
        else:
            doc = json.loads(path.read_text(encoding="utf-8"))
        for key in ("nodes", "meshes"):
            names += [str(n["name"]) for n in doc.get(key) or [] if isinstance(n, dict) and n.get("name")]
    elif ext == ".3mf":
        with zipfile.ZipFile(path) as z:
            for info in z.infolist():
                if info.filename.lower().endswith(".model"):
                    names += re.findall(r"<object\b[^>]*\bname=\"([^\"]+)\"", z.read(info).decode("utf-8", "replace"))
    elif ext == ".stl":
        head = path.read_bytes()[:256]
        if head[:5].lower() == b"solid":
            first = head.split(b"\n", 1)[0][5:].strip().decode("utf-8", "replace")
            if first:
                names.append(first)
    return list(dict.fromkeys(names))


def _page(path: Path, page: int, rect: Optional[List[float]], image: bool) -> Dict[str, Any]:
    import fitz  # PyMuPDF
    with fitz.open(str(path)) as doc:
        total = doc.page_count
        if not 1 <= page <= total:
            return {"found": False, "page": page, "pages": total}
        pg = doc[page - 1]
        out: Dict[str, Any] = {"found": True, "page": page, "pages": total, "size": [round(pg.rect.width, 1), round(pg.rect.height, 1)],
                               "rect": rect, "text": [ln for ln in pg.get_text("text").splitlines() if ln.strip()][:80]}
        if rect:
            x, y, w, h = rect
            out["text_in_rect"] = pg.get_text("text", clip=fitz.Rect(x, y, x + w, y + h)).strip()[:1000]
        if image:
            pix = pg.get_pixmap(dpi=96)
            from PIL import Image, ImageDraw
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            if rect:
                k = pix.width / pg.rect.width
                x, y, w, h = rect
                ImageDraw.Draw(img).rectangle([x * k, y * k, (x + w) * k, (y + h) * k], outline=(220, 38, 38), width=4)
            buf = io.BytesIO()
            img.save(buf, "PNG", optimize=True)
            out["image"] = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
        return out


def view(path: Path, anchor: Optional[Dict[str, Any]], *, image: bool = False) -> Dict[str, Any]:
    """What to show for ``anchor`` inside ``path`` (see the module docstring)."""
    path = Path(path)
    ext = path.suffix.lower()
    base: Dict[str, Any] = {"file": path.name, "ext": ext, "anchor": anchor}
    if not anchor:
        return {**base, "kind": None, "found": True, "reason": "no anchor: the whole file"}
    kind = anchor.get("kind")
    try:
        if kind == "page":
            if ext != ".pdf":
                return {**base, "kind": kind, "found": False, "reason": f"a page anchor needs a PDF, this is {ext or 'a file without extension'}"}
            return {**base, "kind": kind, **_page(path, int(anchor["page"]), anchor.get("rect"), image)}
        if kind == "time":
            return {**base, "kind": kind, "found": ext in MEDIA_EXT, "start": anchor["start"], "end": anchor.get("end"),
                    "media": "video" if ext in {".mp4", ".webm", ".mov", ".mkv"} else "audio" if ext in MEDIA_EXT else None,
                    "fragment": f"#t={anchor['start']}" + (f",{anchor['end']}" if anchor.get("end") is not None else ""),
                    **({} if ext in MEDIA_EXT else {"reason": f"a time anchor needs audio or video, this is {ext}"})}
        if kind == "cell":
            if ext not in (".xlsx", ".xlsm", ".csv", ".tsv"):
                return {**base, "kind": kind, "found": False, "reason": f"a cell anchor needs a spreadsheet, this is {ext}"}
            return {**base, "kind": kind, **_cells(path, anchor.get("sheet") or "", anchor["cell"])}
        if kind in ("line", "symbol"):
            if ext not in TEXT_EXT:
                return {**base, "kind": kind, "found": False, "reason": f"a {kind} anchor needs a text or code file, this is {ext}"}
            lines = _lines(path)
            got = _line_window(lines, int(anchor["start"]), anchor.get("end")) if kind == "line" else _symbol(lines, anchor["name"])
            return {**base, "kind": kind, **got}
        if kind == "object":
            names = object_names(path)
            hit = next((n for n in names if n.lower() == str(anchor["name"]).lower()), None)
            return {**base, "kind": kind, "found": hit is not None, "object": hit or anchor["name"], "objects": names,
                    **({} if names else {"reason": "no named objects in this file"})}
    except Exception as exc:  # noqa: BLE001 - a broken file is a fact to show, not a crash
        return {**base, "kind": kind, "found": False, "reason": f"{type(exc).__name__}: {str(exc)[:200]}"}
    return {**base, "kind": kind, "found": False, "reason": f"unknown anchor kind {kind!r}"}
