"""Read documents into text without installing anything: docx, odt/ods/odp, pptx, xlsx, epub, rtf, html, eml and
(when ``pypdfium2`` or ``pypdf`` happens to be installed) PDF. Standard library only at import time.

Each reader returns *units* — dicts ``{"kind", "number", "title", "text"}`` (``kind`` is ``section``, ``page``,
``slide``, ``sheet`` or ``chapter``) — the shape :func:`hoard_link.docs.chunking.chunk_units` takes, so
``read_any`` -> ``chunk_units`` -> index is the whole pipeline. Headings open sections, list items become
``- item`` lines, table rows become ``a | b`` lines.

Anything heavier (scanned PDFs, OCR, legacy ``.doc``/``.xls``, layout-aware extraction) belongs to the family docs
service (``fam_docs``); here a page without a text layer only sets ``needs_ocr``.

Safety: every ZIP container goes through :func:`check_zip` (total size, per-member ratio, entry count) and
members are read with a size cap, so a zip bomb is refused with :class:`ZipBombError` instead of eating memory.

Replaces: Kafka ``readers.py`` (docx XML, html, eml, pdf), Cicero ``extract.py`` (docx tables, ``_check_zip``,
pptx), Borges ``extract/{docx,html,text}.py``, Hypatia ``_docx_blocks`` / ``_read_text_file``, Pygmalion
``_read_text_file``, Faustus ``_extract_docx_native``.
"""

from __future__ import annotations

import email
import email.policy
import importlib
import io
import posixpath
import re
import struct
import xml.etree.ElementTree as ET
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Optional, Union
from urllib.parse import unquote

from .sniff import sniff
from .textclean import clean_text, decode_text, strip_repeated_lines, useful_chars

__all__ = ["ZipBombError", "check_zip", "read_docx", "read_odt", "read_rtf", "read_html", "html_to_text",
           "read_pptx", "read_xlsx", "read_epub", "read_pdf", "read_eml", "read_any"]

MAX_UNZIPPED = 300_000_000
MAX_MEMBER = 120_000_000
MIN_USEFUL_CHARS = 40          # a PDF page with fewer letters and digits has no usable text layer


class ZipBombError(ValueError):
    """A ZIP container that would expand to an unreasonable size, or is not a ZIP at all."""


def check_zip(zf: zipfile.ZipFile, max_unzipped: int = MAX_UNZIPPED, max_ratio: int = 200, *, max_entries: int = 20_000) -> None:
    """Refuse a zip bomb before reading anything: more than ``max_entries`` members, more than ``max_unzipped``
    bytes in total, or a member of 10 MB+ that expands more than ``max_ratio`` times its compressed size.
    Raises :class:`ZipBombError`."""
    infos = zf.infolist()
    if len(infos) > max_entries:
        raise ZipBombError(f"the archive has {len(infos)} entries (limit {max_entries})")
    total = 0
    for info in infos:
        total += info.file_size
        if total > max_unzipped:
            raise ZipBombError(f"the archive expands to more than {max_unzipped // 1_000_000} MB and was refused")
        if info.file_size >= 10_000_000 and info.file_size / max(1, info.compress_size) > max_ratio:
            raise ZipBombError(f"member {info.filename!r} expands {info.file_size // max(1, info.compress_size)}x: refused")


def _read_member(z: zipfile.ZipFile, name: str, limit: int = MAX_MEMBER) -> bytes:
    with z.open(name) as fh:
        data = fh.read(limit + 1)
    if len(data) > limit:
        raise ZipBombError(f"member {name!r} is larger than {limit // 1_000_000} MB")
    return data


def _open_zip(src: Union[bytes, bytearray, memoryview, str, Path, Any], max_unzipped: int) -> zipfile.ZipFile:
    if isinstance(src, (bytes, bytearray, memoryview)):
        fh: Any = io.BytesIO(bytes(src))
    elif isinstance(src, (str, Path)):
        fh = str(src)
    else:
        fh = src
    try:
        z = zipfile.ZipFile(fh)
    except zipfile.BadZipFile as exc:
        raise ValueError("not a valid zip container (damaged or not an Office/ODF/EPUB file)") from exc
    try:
        check_zip(z, max_unzipped)
    except ZipBombError:
        z.close()
        raise
    return z


def _xml(z: zipfile.ZipFile, name: str, limit: int = MAX_MEMBER) -> ET.Element:
    try:
        return ET.fromstring(_read_member(z, name, limit))
    except KeyError as exc:
        raise ValueError(f"the file has no {name}") from exc
    except ET.ParseError as exc:
        raise ValueError(f"{name} is damaged") from exc


def _optional_xml(z: zipfile.ZipFile, name: str) -> Optional[ET.Element]:
    try:
        return _xml(z, name)
    except (ValueError, ZipBombError):
        return None


def _ln(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _unit(kind: str, title: str, text: str) -> dict[str, Any]:
    return {"kind": kind, "number": 0, "title": title, "text": text}


def _finish(units: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop empty units and number sections/slides/sheets/chapters 1..n (pages keep their number)."""
    out = []
    for u in units:
        u["text"] = clean_text(u["text"], dehyphenate=False, unstack=False)
        u["title"] = re.sub(r"\s+", " ", u["title"] or "").strip()
        if u["text"]:
            out.append(u)
    n = 0
    for u in out:
        if u["kind"] != "page":
            n += 1
            u["number"] = n
    return out


# ======================================================================================== DOCX
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
_DC = "{http://purl.org/dc/elements/1.1/}"
_HEADING_NAME = re.compile(r"^(?:heading|título|titulo|überschrift|titre)\s*(\d)$", re.I)


def _style_levels(root: Optional[ET.Element]) -> dict[str, tuple[Optional[int], bool]]:
    """styleId -> (heading level or None, is a list style); level 0 is the Title style."""
    info: dict[str, dict[str, Any]] = {}
    if root is not None:
        for st in root.iter(f"{_W}style"):
            sid = st.get(f"{_W}styleId") or ""
            name_el = st.find(f"{_W}name")
            based = st.find(f"{_W}basedOn")
            info[sid] = {"name": (name_el.get(f"{_W}val") if name_el is not None else "") or "",
                         "based": based.get(f"{_W}val") if based is not None else None}
    out: dict[str, tuple[Optional[int], bool]] = {}
    for sid in info:
        level: Optional[int] = None
        is_list = False
        cur: Optional[str] = sid
        for _ in range(6):
            if not cur or cur not in info:
                break
            name = info[cur]["name"]
            m = _HEADING_NAME.match(name.strip())
            if m:
                level = int(m.group(1))
                break
            if name.strip().lower() in ("title", "título", "titulo"):
                level = 0
                break
            if name.lower().startswith("list"):
                is_list = True
            cur = info[cur]["based"]
        out[sid] = (level, is_list)
    return out


def _wp_text(el: ET.Element, out: list[str]) -> None:
    for child in el:
        tag = child.tag
        if tag == f"{_W}t":
            out.append(child.text or "")
        elif tag == f"{_W}tab":
            out.append(" ")
        elif tag in (f"{_W}br", f"{_W}cr"):
            out.append("\n")
        elif tag == f"{_W}noBreakHyphen":
            out.append("-")
        elif tag in (f"{_W}pPr", f"{_W}rPr", f"{_W}del", f"{_W}instrText", f"{_MC}Fallback", f"{_W}delText",
                     f"{_W}footnoteReference", f"{_W}endnoteReference", f"{_W}commentReference"):
            continue
        elif tag == f"{_W}p":                  # a paragraph inside a text box inside a run
            out.append("\n")
            _wp_text(child, out)
        else:
            _wp_text(child, out)


def _paragraph_text(p: ET.Element) -> str:
    out: list[str] = []
    _wp_text(p, out)
    return "".join(out)


def _cell_text(tc: ET.Element) -> str:
    parts: list[str] = []
    for child in tc:
        if child.tag == f"{_W}p":
            t = _paragraph_text(child).strip()
            if t:
                parts.append(t)
        elif child.tag == f"{_W}tbl":
            parts.append("; ".join(c for c in (_cell_text(x) for x in child.iter(f"{_W}tc")) if c))
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def _table_rows(tbl: ET.Element) -> list[str]:
    rows = []
    for tr in tbl.findall(f"{_W}tr"):
        cells = [_cell_text(tc) for tc in tr.findall(f"{_W}tc")]
        if any(cells):
            rows.append(" | ".join(cells))
    return rows


def _docx_parse(src: Any, max_unzipped: int) -> tuple[str, list[dict[str, Any]]]:
    z = _open_zip(src, max_unzipped)
    with z:
        names = set(z.namelist())
        if "word/document.xml" not in names:
            raise ValueError("not a Word document (no word/document.xml)")
        doc = _xml(z, "word/document.xml")
        styles = _style_levels(_optional_xml(z, "word/styles.xml") if "word/styles.xml" in names else None)
        core = _optional_xml(z, "docProps/core.xml") if "docProps/core.xml" in names else None
    title = ""
    if core is not None:
        t = core.find(f"{_DC}title")
        title = (t.text or "").strip() if t is not None else ""
    body = doc.find(f"{_W}body")
    units: list[dict[str, Any]] = [_unit("section", "", "")]
    blocks: list[list[str]] = [[]]          # blocks of the current unit; list items and rows share a block
    last_kind = ""

    def add(kind: str, text: str) -> None:
        nonlocal last_kind
        if kind in ("list", "table") and last_kind == kind and blocks[-1]:
            blocks[-1][-1] += "\n" + text
        else:
            blocks[-1].append(text)
        last_kind = kind

    def walk(container: ET.Element) -> None:
        nonlocal last_kind, title
        for child in container:
            tag = child.tag
            if tag == f"{_W}p":
                ppr = child.find(f"{_W}pPr")
                sid = ""
                outline: Optional[int] = None
                has_num = False
                ilvl = 0
                if ppr is not None:
                    ps = ppr.find(f"{_W}pStyle")
                    sid = ps.get(f"{_W}val") if ps is not None else ""
                    ol = ppr.find(f"{_W}outlineLvl")
                    if ol is not None and (ol.get(f"{_W}val") or "").isdigit() and int(ol.get(f"{_W}val")) < 9:
                        outline = int(ol.get(f"{_W}val")) + 1
                    num = ppr.find(f"{_W}numPr")
                    if num is not None:
                        nid = num.find(f"{_W}numId")
                        has_num = nid is not None and nid.get(f"{_W}val") not in (None, "0")
                        il = num.find(f"{_W}ilvl")
                        if il is not None and (il.get(f"{_W}val") or "").isdigit():
                            ilvl = int(il.get(f"{_W}val"))
                level, style_list = styles.get(sid or "", (None, False))
                text = _paragraph_text(child).strip()
                if not text:
                    continue
                if level is None and outline is not None:
                    level = outline
                if level is not None:
                    flat = re.sub(r"\s+", " ", text)
                    if level == 0 and not title:
                        title = flat
                    units.append(_unit("section", flat, ""))
                    blocks.append([])
                    last_kind = ""
                elif has_num or style_list:
                    add("list", "  " * min(ilvl, 6) + "- " + re.sub(r"\s*\n\s*", " ", text))
                else:
                    add("para", text)
            elif tag == f"{_W}tbl":
                rows = _table_rows(child)
                if rows:
                    add("table", "\n".join(rows))
            elif tag in (f"{_W}sdt", f"{_W}sdtContent", f"{_W}customXml", f"{_W}ins", f"{_W}smartTag"):
                walk(child)

    if body is not None:
        walk(body)
    for u, b in zip(units, blocks):
        u["text"] = "\n\n".join(b)
    return title, _finish(units)


def read_docx(src: Union[bytes, str, Path, Any], *, max_unzipped: int = MAX_UNZIPPED) -> list[dict[str, Any]]:
    """Units of a ``.docx`` (bytes, a path or a file object): one ``section`` per heading (text before the first
    heading is a section with an empty title). Headings come from the style (``Heading N``, ``Title``, any
    language) or the paragraph outline level. ``ValueError`` for a damaged file, :class:`ZipBombError` for a bomb."""
    return _docx_parse(src, max_unzipped)[1]


# ======================================================================================== ODF
_OFFICE = "{urn:oasis:names:tc:opendocument:xmlns:office:1.0}"
_TEXT = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
_TABLE = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"
_DRAW = "{urn:oasis:names:tc:opendocument:xmlns:drawing:1.0}"
_PRES = "{urn:oasis:names:tc:opendocument:xmlns:presentation:1.0}"


def _odf_inline(el: ET.Element, out: list[str]) -> None:
    if el.text:
        out.append(el.text)
    for child in el:
        tag = child.tag
        if tag == f"{_TEXT}s":
            out.append(" " * int(child.get(f"{_TEXT}c") or 1))
        elif tag == f"{_TEXT}tab":
            out.append(" ")
        elif tag == f"{_TEXT}line-break":
            out.append("\n")
        elif tag in (f"{_TEXT}note", f"{_OFFICE}annotation", f"{_TEXT}bookmark", f"{_TEXT}tracked-changes"):
            pass
        elif tag in (f"{_TEXT}p", f"{_TEXT}h"):
            out.append("\n")
            _odf_inline(child, out)
        else:
            _odf_inline(child, out)
        if child.tail:
            out.append(child.tail)


def _odf_par(el: ET.Element) -> str:
    out: list[str] = []
    _odf_inline(el, out)
    return "".join(out).strip()


def _odf_cell(cell: ET.Element) -> str:
    parts = [_odf_par(p) for p in cell.iter() if p.tag in (f"{_TEXT}p", f"{_TEXT}h")]
    return re.sub(r"\s+", " ", " ".join(x for x in parts if x)).strip()


def _odf_rows(table: ET.Element, max_rows: int = 20000) -> list[str]:
    rows: list[str] = []
    for tr in table.iter(f"{_TABLE}table-row"):
        cells: list[str] = []
        for c in tr:
            if c.tag not in (f"{_TABLE}table-cell", f"{_TABLE}covered-table-cell"):
                continue
            text = _odf_cell(c) if c.tag == f"{_TABLE}table-cell" else ""
            rep = int(c.get(f"{_TABLE}number-columns-repeated") or 1)
            cells.extend([text] * (min(rep, 50) if text else 1))
        while cells and not cells[-1]:
            cells.pop()
        if cells:
            rows.append(" | ".join(cells))
        if len(rows) >= max_rows:
            break
    return rows


def _odf_parse(src: Any, max_unzipped: int) -> tuple[str, list[dict[str, Any]]]:
    z = _open_zip(src, max_unzipped)
    with z:
        content = _xml(z, "content.xml")
        meta = _optional_xml(z, "meta.xml") if "meta.xml" in z.namelist() else None
    title = ""
    if meta is not None:
        for t in meta.iter(f"{_DC}title"):
            title = (t.text or "").strip()
    body = content.find(f"{_OFFICE}body")
    units: list[dict[str, Any]] = []
    if body is None:
        return title, units
    for part in body:
        if part.tag == f"{_OFFICE}text":
            units.extend(_odf_text_units(part))
        elif part.tag == f"{_OFFICE}spreadsheet":
            for table in part.iter(f"{_TABLE}table"):
                units.append(_unit("sheet", table.get(f"{_TABLE}name") or "", "\n".join(_odf_rows(table))))
        elif part.tag == f"{_OFFICE}presentation":
            for page in part.iter(f"{_DRAW}page"):
                texts = []
                for el in page.iter():
                    if el.tag in (f"{_TEXT}p", f"{_TEXT}h"):
                        t = _odf_par(el)
                        if t:
                            texts.append(t)
                units.append(_unit("slide", page.get(f"{_DRAW}name") or "", "\n".join(dict.fromkeys(texts))))
    return title, _finish(units)


def _odf_text_units(root: ET.Element) -> list[dict[str, Any]]:
    units = [_unit("section", "", "")]
    blocks: list[list[str]] = [[]]
    last = [""]

    def add(kind: str, text: str) -> None:
        if kind in ("list", "table") and last[0] == kind and blocks[-1]:
            blocks[-1][-1] += "\n" + text
        else:
            blocks[-1].append(text)
        last[0] = kind

    def lst(el: ET.Element, depth: int) -> None:
        for item in el:
            if item.tag not in (f"{_TEXT}list-item", f"{_TEXT}list-header"):
                continue
            for sub in item:
                if sub.tag in (f"{_TEXT}p", f"{_TEXT}h"):
                    t = re.sub(r"\s*\n\s*", " ", _odf_par(sub))
                    if t:
                        add("list", "  " * min(depth, 6) + "- " + t)
                elif sub.tag == f"{_TEXT}list":
                    lst(sub, depth + 1)

    def walk(container: ET.Element) -> None:
        for el in container:
            tag = el.tag
            if tag == f"{_TEXT}h":
                t = re.sub(r"\s+", " ", _odf_par(el))
                if t:
                    units.append(_unit("section", t, ""))
                    blocks.append([])
                    last[0] = ""
            elif tag == f"{_TEXT}p":
                t = _odf_par(el)
                if t:
                    add("para", t)
            elif tag == f"{_TEXT}list":
                lst(el, 0)
            elif tag == f"{_TABLE}table":
                rows = _odf_rows(el)
                if rows:
                    add("table", "\n".join(rows))
            elif tag in (f"{_TEXT}section", f"{_TEXT}table-of-content", f"{_TEXT}index-body", f"{_TEXT}alphabetical-index",
                         f"{_TEXT}illustration-index", f"{_TEXT}user-index", f"{_TEXT}bibliography"):
                walk(el)

    walk(root)
    for u, b in zip(units, blocks):
        u["text"] = "\n\n".join(b)
    return units


def read_odt(src: Union[bytes, str, Path, Any], *, max_unzipped: int = MAX_UNZIPPED) -> list[dict[str, Any]]:
    """Units of an OpenDocument file: ``.odt`` (a ``section`` per heading), ``.ods`` (a ``sheet`` per table, rows as
    ``a | b``) or ``.odp`` (a ``slide`` per page)."""
    return _odf_parse(src, max_unzipped)[1]


# ======================================================================================== RTF
_RTF_SKIP = {"fonttbl", "colortbl", "stylesheet", "info", "pict", "object", "objdata", "header", "headerl", "headerr",
             "headerf", "footer", "footerl", "footerr", "footerf", "filetbl", "listtable", "listoverridetable",
             "revtbl", "rsidtbl", "themedata", "datastore", "latentstyles", "xmlnstbl", "mmathpr", "generator",
             "private", "bkmkstart", "bkmkend", "fldinst", "colorschememapping", "wgrffmtfilter", "pgptbl",
             "defchp", "defpap", "listtext", "pntext", "pntxta", "pntxtb", "nonshppict", "falt", "panose"}
_RTF_CHARS = {"emdash": "—", "endash": "–", "bullet": "•", "lquote": "‘", "rquote": "’", "ldblquote": "“",
              "rdblquote": "”", "emspace": " ", "enspace": " ", "qmspace": " ", "tab": " ", "lbrace": "{", "rbrace": "}",
              "backslash": "\\", "zwj": "", "zwnj": "", "ltrmark": "", "rtlmark": ""}
_CELL = "\x1f"
_RTF_PLAIN = re.compile(r"[^{}\\\r\n]+")
_RTF_CODEPAGES = {"mac": "mac_roman", "pc": "cp437", "pca": "cp850"}


def read_rtf(data: Union[bytes, str]) -> str:
    """Plain text of an RTF document: control words and groups like the font/colour tables, pictures, headers and
    footers skipped; ``\\'hh`` bytes decoded with the document code page; ``\\uN`` as Unicode; paragraphs become
    line breaks and table cells ``a | b``. Minimal on purpose (no styles, no lists numbering)."""
    s = data if isinstance(data, str) else bytes(data).decode("latin-1")
    out: list[str] = []
    buf = bytearray()
    codepage = "cp1252"
    stack: list[tuple[bool, int]] = []
    skip = False
    uc = 1
    skip_chars = 0
    i, n = 0, len(s)

    def flush() -> None:
        if buf:
            try:
                out.append(bytes(buf).decode(codepage))
            except (UnicodeDecodeError, LookupError):
                out.append(bytes(buf).decode("cp1252", errors="replace"))
            buf.clear()

    def emit(text: str) -> None:
        nonlocal skip_chars
        if skip or not text:
            return
        flush()
        out.append(text)

    while i < n:
        c = s[i]
        if c == "{":
            stack.append((skip, uc))
            i += 1
            j = i
            if s.startswith("\\*", j):
                skip = True
        elif c == "}":
            flush()
            if stack:
                skip, uc = stack.pop()
            i += 1
        elif c == "\\":
            i += 1
            if i >= n:
                break
            d = s[i]
            if d == "'":
                hx = s[i + 1:i + 3]
                i += 3
                if skip_chars:
                    skip_chars -= 1
                elif not skip:
                    try:
                        buf.append(int(hx, 16))
                    except ValueError:
                        pass
            elif d in "\\{}":
                i += 1
                if skip_chars:
                    skip_chars -= 1
                else:
                    emit(d)
            elif d == "~":
                i += 1
                emit(" ")
            elif d == "_":
                i += 1
                emit("-")
            elif d == "-" or d == "|" or d == ":":
                i += 1
            elif d == "*":
                i += 1
                skip = True
            elif d in "\r\n":
                i += 1
                emit("\n")
            elif d.isalpha():
                m = re.match(r"([a-zA-Z]+)(-?\d+)?[ ]?", s[i:i + 40])
                word, param = m.group(1), m.group(2)
                i += m.end()
                lw = word.lower()
                if lw in _RTF_SKIP:
                    skip = True
                elif lw == "uc":
                    uc = int(param or 1)
                elif lw == "u" and param is not None:
                    if not skip:
                        v = int(param)
                        emit(chr(v + 65536 if v < 0 else v))
                    skip_chars = uc
                elif lw == "bin" and param:
                    i += int(param)
                elif lw == "ansicpg" and param:
                    codepage = f"cp{param}"
                elif lw in _RTF_CODEPAGES:
                    codepage = _RTF_CODEPAGES[lw]
                elif lw in ("par", "line", "sect", "page", "row", "pagebb"):
                    emit("\n")
                elif lw == "cell":
                    emit(_CELL)
                elif lw in _RTF_CHARS:
                    emit(_RTF_CHARS[lw])
            else:
                i += 1
        elif c in "\r\n":
            i += 1
        else:
            run = _RTF_PLAIN.match(s, i)
            chunk = run.group(0)
            i = run.end()
            if skip_chars:
                drop = min(skip_chars, len(chunk))
                chunk = chunk[drop:]
                skip_chars -= drop
            emit(chunk)
    flush()
    text = "".join(out)
    text = text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")      # joins \u surrogate pairs
    text = re.sub(_CELL + r"[ ]*(?=\n|$)", "", text).replace(_CELL, " | ")
    return clean_text(text, dehyphenate=False, unstack=False)


# ======================================================================================== HTML
class _HtmlText(HTMLParser):
    BLOCK = {"br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "section", "article", "ul", "ol",
             "hr", "blockquote", "pre", "header", "footer", "nav", "aside", "figure", "figcaption", "dd", "dt", "dl", "main"}
    SKIP = {"script", "style", "head", "noscript", "template", "svg", "iframe", "object", "canvas"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False
        self._cells = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag == "title":
            self._in_title = True
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")
            if tag == "tr":
                self._cells = 0
        elif tag in ("td", "th"):
            if self._cells:
                self.parts.append(" | ")
            self._cells += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag in self.SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    """Visible text of an HTML string with the standard library: scripts, styles and ``<head>`` dropped, block
    elements on their own lines, table cells joined with `` | ``. Broken markup falls back to tag stripping."""
    parser = _HtmlText()
    try:
        parser.feed(html[:5_000_000])
        parser.close()
    except Exception:  # noqa: BLE001 - html.parser can choke on very broken markup
        import html as _html
        return clean_text(_html.unescape(re.sub(r"<[^>]+>", " ", html)), dehyphenate=False, unstack=False)
    return re.sub(r"\n{2,}", "\n", clean_text("".join(parser.parts), dehyphenate=False, unstack=False))


def _html_title(html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html[:200_000], re.I | re.S)
    if not m:
        return ""
    import html as _html
    return re.sub(r"\s+", " ", _html.unescape(m.group(1))).strip()


def _decode_html(data: Union[bytes, str]) -> str:
    if isinstance(data, str):
        return data
    b = bytes(data)
    m = re.search(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_\-]+)""", b[:4096], re.I)
    if m and not b[:3] == b"\xef\xbb\xbf":
        try:
            return b.decode(m.group(1).decode("ascii"))
        except (LookupError, UnicodeDecodeError):
            pass
    return decode_text(b)


def _readable(html: str) -> Optional[str]:
    """The family's article extractor when ``hoard_link.web.htmltext`` is installed (looked up at call time)."""
    try:
        mod = importlib.import_module(__name__.split(".")[0] + ".web.htmltext")
        fn = getattr(mod, "readable", None)
        if fn is None:
            return None
        res = fn(html)
        if isinstance(res, str):
            return res
        if isinstance(res, (tuple, list)) and len(res) >= 2 and isinstance(res[1], str):       # (title, text)
            return res[1]
        if isinstance(res, dict):
            return res.get("text") or res.get("content") or None
        text = getattr(res, "text", None)
        return text if isinstance(text, str) else None
    except Exception:  # noqa: BLE001 - optional helper; the built-in parser is the fallback
        return None


def read_html(data: Union[bytes, str]) -> str:
    """Readable text of an HTML page or fragment (bytes are decoded using ``<meta charset>``, a BOM or the
    Windows-1252/Latin-1 fallback). Uses :func:`hoard_link.web.htmltext.readable` when that module exists, else the
    built-in :func:`html_to_text`."""
    html = _decode_html(data)
    text = _readable(html)
    if text and text.strip():
        # the article extractor separates blocks with blank lines; keep one line per block like html_to_text
        return re.sub(r"\n{2,}", "\n", clean_text(text, dehyphenate=False, unstack=False))
    return html_to_text(html)


# ======================================================================================== PPTX
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"


def _a_paragraph(p: ET.Element) -> str:
    out = []
    for el in p.iter():
        if el.tag == f"{_A}t":
            out.append(el.text or "")
        elif el.tag == f"{_A}br":
            out.append("\n")
    return "".join(out).strip()


def _slide_text(root: ET.Element) -> tuple[str, list[str]]:
    title = ""
    lines: list[str] = []

    def shapes(parent: ET.Element) -> None:
        nonlocal title
        for sh in parent:
            if sh.tag == f"{_P}sp":
                ph = sh.find(f"{_P}nvSpPr/{_P}nvPr/{_P}ph")
                is_title = ph is not None and ph.get("type") in ("title", "ctrTitle")
                paras = [_a_paragraph(p) for p in sh.findall(f"{_P}txBody/{_A}p")]
                paras = [t for t in paras if t]
                if is_title and paras and not title:
                    title = " ".join(paras)
                else:
                    lines.extend(paras)
            elif sh.tag == f"{_P}graphicFrame":
                for tr in sh.iter(f"{_A}tr"):
                    cells = [re.sub(r"\s+", " ", " ".join(x for x in (_a_paragraph(p) for p in tc.findall(f"{_A}txBody/{_A}p")) if x)).strip()
                             for tc in tr.findall(f"{_A}tc")]
                    if any(cells):
                        lines.append(" | ".join(cells))
            elif sh.tag == f"{_P}grpSp":
                shapes(sh)

    tree = root.find(f"{_P}cSld/{_P}spTree")
    if tree is not None:
        shapes(tree)
    return title, lines


def read_pptx(src: Union[bytes, str, Path, Any], *, max_unzipped: int = MAX_UNZIPPED) -> list[dict[str, Any]]:
    """One ``slide`` unit per slide: title, body text, table rows and the speaker notes (as ``(notes) …``)."""
    z = _open_zip(src, max_unzipped)
    with z:
        names = z.namelist()
        slides = sorted((n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                        key=lambda n: int(re.search(r"(\d+)\.xml$", n).group(1)))
        if not slides:
            raise ValueError("not a PowerPoint file (no slides)")
        units = []
        for n in slides[:2000]:
            title, lines = _slide_text(_xml(z, n))
            notes = ""
            rels = f"ppt/slides/_rels/{posixpath.basename(n)}.rels"
            if rels in names:
                for rel in _xml(z, rels):
                    if (rel.get("Type") or "").endswith("/notesSlide"):
                        target = posixpath.normpath(posixpath.join("ppt/slides", rel.get("Target") or ""))
                        if target in names:
                            _, nl = _slide_text(_xml(z, target))
                            notes = " ".join(t for t in nl if not t.isdigit())
            body = "\n".join(lines)
            if notes:
                body += ("\n\n" if body else "") + f"(notes) {notes}"
            units.append(_unit("slide", title, body))
    return _finish(units)


# ======================================================================================== XLSX
_S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_BUILTIN_DATE_FMT = set(range(14, 23)) | {45, 46, 47}


def _col_index(ref: str) -> int:
    n = 0
    for ch in ref:
        if not ch.isalpha():
            break
        n = n * 26 + (ord(ch.upper()) - 64)
    return max(0, n - 1)


def _is_date_format(code: str) -> bool:
    c = re.sub(r'"[^"]*"|\[[^\]]*\]|\\.', "", code or "")
    return bool(re.search(r"[ymdhs]", c, re.I)) and not re.search(r"0\.0|#", c)


def read_xlsx(src: Union[bytes, str, Path, Any], *, max_unzipped: int = MAX_UNZIPPED, max_rows: int = 5000) -> list[dict[str, Any]]:
    """One ``sheet`` unit per worksheet (visible or hidden), rows as ``a | b | c`` (columns kept in place, trailing
    empty cells trimmed), dates shown as ISO dates, at most ``max_rows`` rows per sheet. Formulas show their cached
    value."""
    z = _open_zip(src, max_unzipped)
    with z:
        names = set(z.namelist())
        if "xl/workbook.xml" not in names:
            raise ValueError("not an Excel file (no xl/workbook.xml)")
        wb = _xml(z, "xl/workbook.xml")
        rels_root = _optional_xml(z, "xl/_rels/workbook.xml.rels")
        rels = {r.get("Id"): r.get("Target") for r in (rels_root if rels_root is not None else [])}
        shared: list[str] = []
        sst = _optional_xml(z, "xl/sharedStrings.xml") if "xl/sharedStrings.xml" in names else None
        if sst is not None:
            for si in sst.findall(f"{_S}si"):
                shared.append("".join(t.text or "" for t in si.iter(f"{_S}t") if not _is_phonetic(si, t)))
        date_xf: set[int] = set()
        styles = _optional_xml(z, "xl/styles.xml") if "xl/styles.xml" in names else None
        if styles is not None:
            custom = {int(nf.get("numFmtId")): nf.get("formatCode") or "" for nf in styles.iter(f"{_S}numFmt") if (nf.get("numFmtId") or "").isdigit()}
            xfs = styles.find(f"{_S}cellXfs")
            for idx, xf in enumerate(list(xfs) if xfs is not None else []):
                fid = int(xf.get("numFmtId") or 0)
                if fid in _BUILTIN_DATE_FMT or (fid in custom and _is_date_format(custom[fid])):
                    date_xf.add(idx)
        units = []
        for sheet in wb.iter(f"{_S}sheet"):
            target = rels.get(sheet.get(f"{_R}id"))
            if not target:
                continue
            path = posixpath.normpath(target.lstrip("/") if target.startswith("/") else posixpath.join("xl", target))
            if path not in names:
                continue
            rows: list[str] = []
            info = z.getinfo(path)
            if info.file_size > MAX_MEMBER:
                raise ZipBombError(f"sheet {path!r} is larger than {MAX_MEMBER // 1_000_000} MB")
            with z.open(path) as fh:
                for _, row in ET.iterparse(fh, events=("end",)):
                    if row.tag != f"{_S}row":
                        continue
                    cells: dict[int, str] = {}
                    for c in row.findall(f"{_S}c"):
                        t = c.get("t")
                        v = c.find(f"{_S}v")
                        val = ""
                        if t == "s" and v is not None and (v.text or "").isdigit() and int(v.text) < len(shared):
                            val = shared[int(v.text)]
                        elif t == "inlineStr":
                            val = "".join(x.text or "" for x in c.iter(f"{_S}t"))
                        elif v is not None and v.text is not None:
                            val = v.text
                            if t == "b":
                                val = "TRUE" if val == "1" else "FALSE"
                            elif t in (None, "n") and int(c.get("s") or 0) in date_xf:
                                val = _xl_date(val)
                        val = re.sub(r"\s+", " ", val).strip()
                        if val:
                            cells[_col_index(c.get("r") or "")] = val
                    if cells:
                        width = max(cells) + 1
                        rows.append(" | ".join(cells.get(i, "") for i in range(width)))
                    row.clear()
                    if len(rows) >= max_rows:
                        break
            units.append(_unit("sheet", sheet.get("name") or "", "\n".join(rows)))
    return _finish(units)


def _xl_date(serial: str) -> str:
    import datetime as dt

    try:
        days = float(serial)
        when = dt.datetime(1899, 12, 30) + dt.timedelta(days=days)
    except (ValueError, OverflowError):
        return serial
    return when.date().isoformat() if days == int(days) else when.isoformat(timespec="minutes")


def _is_phonetic(si: ET.Element, t: ET.Element) -> bool:
    for ph in si.iter(f"{_S}rPh"):
        if t in list(ph.iter(f"{_S}t")):
            return True
    return False


# ======================================================================================== EPUB
def read_epub(src: Union[bytes, str, Path, Any], *, max_unzipped: int = MAX_UNZIPPED) -> list[dict[str, Any]]:
    """One ``chapter`` unit per spine item of an EPUB, in reading order, text via :func:`html_to_text`."""
    return _epub_parse(src, max_unzipped)[1]


def _epub_parse(src: Any, max_unzipped: int) -> tuple[str, list[dict[str, Any]]]:
    z = _open_zip(src, max_unzipped)
    with z:
        container = _xml(z, "META-INF/container.xml")
        rootfile = next((r.get("full-path") for r in container.iter() if _ln(r.tag) == "rootfile"), None)
        if not rootfile:
            raise ValueError("EPUB without a rootfile")
        opf = _xml(z, rootfile)
        base = posixpath.dirname(rootfile)
        manifest = {}
        title = ""
        for el in opf.iter():
            ln = _ln(el.tag)
            if ln == "item":
                manifest[el.get("id")] = (el.get("href") or "", el.get("media-type") or "")
            elif ln == "title" and not title:
                title = (el.text or "").strip()
        names = set(z.namelist())
        units = []
        for ref in (el for el in opf.iter() if _ln(el.tag) == "itemref"):
            href, mt = manifest.get(ref.get("idref"), ("", ""))
            if not href or "html" not in mt and "xml" not in mt:
                continue
            path = posixpath.normpath(posixpath.join(base, unquote(href.split("#")[0])))
            if path not in names:
                continue
            html = _decode_html(_read_member(z, path, 20_000_000))
            heading = re.search(r"<h[1-3][^>]*>(.*?)</h[1-3]>", html, re.I | re.S)
            ctitle = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", heading.group(1))).strip() if heading else _html_title(html)
            units.append(_unit("chapter", ctitle, html_to_text(html)))
            if len(units) >= 2000:
                break
    return title, _finish(units)


# ======================================================================================== PDF
def read_pdf(data: bytes, *, max_pages: int = 400, strip_headers: bool = True) -> list[dict[str, Any]]:
    """One ``page`` unit per page of a PDF, using ``pypdfium2`` or else ``pypdf`` (imported here, never at module
    import). ``unit["needs_ocr"]`` is ``True`` on pages with fewer than 40 letters and digits (no text layer).
    Repeated headers and footers are removed (``strip_headers``). Raises :class:`hoard_link.errors.Unavailable`
    when neither library is installed and ``ValueError`` for an unreadable or password-protected file."""
    return _pdf_parse(data, max_pages, strip_headers)[1]


def _pdf_parse(data: bytes, max_pages: int = 400, strip_headers: bool = True) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """``(title, page units, info)``; ``info`` has ``pages`` (total), ``thin`` and ``engine``."""
    raw: list[str] = []
    title = ""
    total = 0
    engine = ""
    try:
        import pypdfium2 as pdfium  # type: ignore
    except ImportError:
        pdfium = None
    if pdfium is not None:
        engine = "pypdfium2"
        try:
            pdf = pdfium.PdfDocument(data)
        except Exception as exc:  # noqa: BLE001 - PdfiumError: encrypted or corrupt
            raise ValueError(f"the PDF cannot be opened ({type(exc).__name__}); it may be encrypted or damaged") from exc
        try:
            total = len(pdf)
            try:
                title = str((pdf.get_metadata_dict() or {}).get("Title") or "").strip()
            except Exception:  # noqa: BLE001
                title = ""
            for i in range(min(total, max_pages)):
                page = pdf[i]
                try:
                    tp = page.get_textpage()
                    try:
                        raw.append(tp.get_text_bounded() or "")
                    finally:
                        tp.close()
                finally:
                    page.close()
        finally:
            pdf.close()
    else:
        try:
            from pypdf import PdfReader  # type: ignore
        except ImportError:
            from ..errors import missing_dependency
            raise missing_dependency("pypdfium2", "reading PDF text", pip_name="pypdfium2 (or pypdf)") from None
        engine = "pypdf"
        try:
            reader = PdfReader(io.BytesIO(data))
            if reader.is_encrypted and not reader.decrypt(""):
                raise ValueError("the PDF is password protected")
            total = len(reader.pages)
            meta = reader.metadata
            title = str(getattr(meta, "title", "") or "").strip() if meta else ""
            for page in reader.pages[:max_pages]:
                raw.append(page.extract_text() or "")
        except ValueError:
            raise
        except Exception as exc:  # noqa: BLE001 - pypdf raises many types on damaged files
            raise ValueError(f"the PDF cannot be read ({type(exc).__name__}: {exc})") from exc
    pages = [clean_text(t) for t in raw]
    if strip_headers:
        pages = strip_repeated_lines(pages)
    units = []
    thin = 0
    for i, t in enumerate(pages, start=1):
        unit = {"kind": "page", "number": i, "title": "", "text": t}
        if useful_chars(t) < MIN_USEFUL_CHARS:
            unit["needs_ocr"] = True
            thin += 1
        units.append(unit)
    return title, units, {"pages": total, "thin": thin, "engine": engine}


# ======================================================================================== EML
def read_eml(data: bytes) -> str:
    """Text of an RFC 822 message: headers (From, To, Cc, Date, Subject) and the plain-text body (the HTML body
    converted when there is no plain one). Attachments are not read."""
    return _eml_parse(data)[1]


def _eml_parse(data: bytes) -> tuple[str, str, list[str]]:
    """``(subject, text, attachment names)``."""
    msg = email.message_from_bytes(bytes(data), policy=email.policy.default)
    subject = str(msg.get("Subject") or "").strip()
    head = [f"{h}: {msg.get(h)}" for h in ("From", "To", "Cc", "Date", "Subject") if msg.get(h)]
    body = ""
    try:
        part = msg.get_body(preferencelist=("plain", "html"))
        if part is not None:
            raw = part.get_content()
            body = html_to_text(raw) if part.get_content_type() == "text/html" else clean_text(raw, dehyphenate=False, unstack=False)
    except Exception:  # noqa: BLE001 - undecodable charset
        body = ""
    attachments = [a.get_filename() or "(unnamed)" for a in msg.iter_attachments()]
    return subject, "\n".join(head) + ("\n\n" + body if body else ""), attachments


# ======================================================================================== dispatch
def _result(kind: str, title: str, units: list[dict[str, Any]], needs_ocr: bool, notes: list[str], **extra: Any) -> dict[str, Any]:
    text = "\n\n".join(u["text"] for u in units if u["text"])
    return {"kind": kind, "title": title, "text": text, "units": units, "needs_ocr": needs_ocr, "notes": notes, **extra}


def read_any(name: str, data: bytes, *, max_pages: int = 400, max_chars: int = 5_000_000) -> dict[str, Any]:
    """Read any supported file into ``{"kind", "title", "text", "units", "needs_ocr", "notes"}`` (plus ``mime`` and
    ``error``). The type comes from :func:`~hoard_link.docs.sniff.sniff`, not the name. It never raises for a bad
    file: a damaged or unsupported one gives empty text and the reason in ``notes`` / ``error``. Images and
    PDFs without a text layer set ``needs_ocr``; PDFs need ``pypdfium2`` or ``pypdf`` installed (without either:
    ``needs_ocr`` and a note)."""
    from ..errors import Unavailable

    raw = bytes(data or b"")
    sn = sniff(name, raw)
    notes: list[str] = []
    stem = Path(str(name or "").replace("\\", "/")).stem
    base = {"mime": sn.mime, "error": None}

    def fail(msg: str, kind: Optional[str] = None, ocr: bool = False) -> dict[str, Any]:
        r = _result(kind or sn.kind, stem, [], ocr, [*notes, msg], **base)
        r["error"] = msg
        return r

    if not raw:
        return fail("the file is empty")
    try:
        k = sn.kind
        if k == "pdf":
            try:
                title, units, info = _pdf_parse(raw, max_pages)
            except Unavailable as exc:
                notes.append("no PDF library is installed (pypdfium2 or pypdf): use the docs service")
                r = _result("pdf", stem, [], True, notes, **base)
                r["error"] = str(exc)
                return r
            if info["pages"] > max_pages:
                notes.append(f"only the first {max_pages} of {info['pages']} pages were read")
            majority = info["thin"] > 0 and info["thin"] >= max(1, len(units) / 2)
            if info["thin"]:
                notes.append(f"{info['thin']} page(s) have no text layer" + (": it needs OCR" if majority else ""))
            return _result("pdf", title or stem, units, majority, notes, **base)
        if k == "image":
            if sn.ext == "svg":
                notes.append("vector image: no text extracted")
                return _result("image", stem, [], False, notes, **base)
            notes.append("image: text needs OCR")
            return _result("image", stem, [], True, notes, **base)
        if k == "docx":
            title, units = _docx_parse(raw, MAX_UNZIPPED)
            return _result("docx", title or stem, units, False, notes, **base)
        if k in ("odt", "ods", "odp"):
            title, units = _odf_parse(raw, MAX_UNZIPPED)
            return _result(k, title or stem, units, False, notes, **base)
        if k == "pptx":
            return _result("pptx", stem, read_pptx(raw), False, notes, **base)
        if k == "xlsx":
            return _result("xlsx", stem, read_xlsx(raw), False, notes, **base)
        if k == "epub":
            title, units = _epub_parse(raw, MAX_UNZIPPED)
            return _result("epub", title or stem, units, False, notes, **base)
        if k == "rtf":
            text = read_rtf(raw)
            return _result("rtf", stem, [_unit_one(text)] if text else [], False, notes, **base)
        if k == "html":
            html = _decode_html(raw)
            text = read_html(html)
            return _result("html", _html_title(html) or stem, [_unit_one(text)] if text else [], False, notes, **base)
        if k == "eml":
            subject, text, atts = _eml_parse(raw)
            if atts:
                notes.append("attachments not read: " + ", ".join(atts[:10]))
            return _result("eml", subject or stem, [_unit_one(text, subject)] if text else [], False, notes, **base)
        if k in ("text", "csv", "json"):
            text = clean_text(decode_text(raw), dehyphenate=False, unstack=False)
            if len(text) > max_chars:
                text = text[:max_chars]
                notes.append(f"text cut at {max_chars} characters")
            return _result(k, stem, [_unit_one(text)] if text else [], False, notes, **base)
        if k == "ole":
            return fail("legacy Office format (.doc/.xls/.ppt): convert it to docx, xlsx or pptx")
        return fail(f"unsupported file type ({sn.mime})")
    except ZipBombError as exc:
        return fail(f"refused: {exc}")
    except (ValueError, KeyError, zipfile.BadZipFile, ET.ParseError, struct.error, OSError) as exc:
        return fail(f"could not be read: {exc}")


def _unit_one(text: str, title: str = "") -> dict[str, Any]:
    return {"kind": "section", "number": 1, "title": title, "text": text}
