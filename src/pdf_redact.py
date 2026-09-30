"""src/pdf_redact.py — real PDF redaction with verification.

Drawing a black box over text does not redact it: the text stays in the
content stream and any extractor still returns it. This module removes it.

Two engines, picked automatically:

* ``flatten`` (preferred, needs ``pypdfium2`` which is also what ``to_images``
  uses). The match boxes are located from the per-character geometry of the
  page, every affected page is rendered to an image with the boxes painted
  solid, and the page is rebuilt from that image. The text layer, the vector
  content and the annotations of those pages no longer exist, so nothing can
  be recovered. Pages without any match are copied untouched.
* ``stream`` (fallback, ``pypdf`` only). The text-showing operators of each
  page's content stream are decoded, the matched characters are deleted from
  the operands, and the page is rewritten. Works for patterns only (not for
  page rectangles) and only for single-byte fonts; anything it cannot decode
  stays in place and is caught by the verification step, in which case no
  output file is kept.

After writing, the output is re-read and every pattern is searched again
(text layer, metadata, annotations). The verification result is part of the
return value, and an output that still contains a match is deleted.

The result never echoes the matched text: only counts per page and pattern.
"""
from __future__ import annotations

import io
import logging
import os
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

MAX_PATTERNS = 50
MAX_PATTERN_LEN = 500
MAX_RECTS = 500
DEFAULT_DPI = 200
MIN_DPI, MAX_DPI = 72, 400
_PAD_PT = 1.0


class RedactError(ValueError):
    """A redaction request that cannot be carried out, with the reason."""


# ---------------------------------------------------------------------------
# Pattern handling
# ---------------------------------------------------------------------------

def compile_patterns(patterns: Optional[Sequence[str]], regex: Optional[Sequence[str]],
                     *, case_sensitive: bool = False) -> List[Tuple[str, "re.Pattern[str]"]]:
    """Literal `patterns` and regular expressions `regex` -> one compiled list.

    A literal is matched with flexible whitespace (a space in the pattern
    matches any run of whitespace), because layout engines break lines and
    add spaces freely. Returns ``[(label, compiled), ...]``; the label is the
    index-based name used in reports, never the pattern text itself.
    """
    flags = 0 if case_sensitive else re.IGNORECASE
    compiled: List[Tuple[str, "re.Pattern[str]"]] = []
    total = len(patterns or []) + len(regex or [])
    if total > MAX_PATTERNS:
        raise RedactError(f"too many patterns ({total}); the limit is {MAX_PATTERNS}")
    for i, raw in enumerate(patterns or []):
        text = str(raw or "")
        if not text.strip():
            raise RedactError(f"patterns[{i}] is empty")
        if len(text) > MAX_PATTERN_LEN:
            raise RedactError(f"patterns[{i}] is longer than {MAX_PATTERN_LEN} characters")
        body = r"\s+".join(re.escape(part) for part in text.split())
        compiled.append((f"pattern#{i + 1}", re.compile(body, flags)))
    for i, raw in enumerate(regex or []):
        text = str(raw or "")
        if not text:
            raise RedactError(f"regex[{i}] is empty")
        if len(text) > MAX_PATTERN_LEN:
            raise RedactError(f"regex[{i}] is longer than {MAX_PATTERN_LEN} characters")
        try:
            rx = re.compile(text, flags)
        except re.error as exc:
            raise RedactError(f"regex[{i}] is not a valid regular expression: {exc}") from exc
        if rx.match(""):
            raise RedactError(f"regex[{i}] matches the empty string; it would match everywhere")
        compiled.append((f"regex#{i + 1}", rx))
    return compiled


def _find_spans(text: str, compiled) -> List[Tuple[int, int, str]]:
    spans: List[Tuple[int, int, str]] = []
    for label, rx in compiled:
        for m in rx.finditer(text):
            if m.end() > m.start():
                spans.append((m.start(), m.end(), label))
    return spans


def parse_rects(rects: Any, page_count: int) -> Dict[int, List[Tuple[float, float, float, float]]]:
    """`[{"page": 1, "rect": [x0, y0, x1, y1]}, ...]` (PDF points, origin at
    the bottom-left of the page, like the PDF specification) -> {page_index: rects}."""
    out: Dict[int, List[Tuple[float, float, float, float]]] = {}
    if not rects:
        return out
    if not isinstance(rects, (list, tuple)):
        raise RedactError("`rects` must be a list of {page, rect:[x0,y0,x1,y1]}")
    if len(rects) > MAX_RECTS:
        raise RedactError(f"too many rectangles ({len(rects)}); the limit is {MAX_RECTS}")
    for i, item in enumerate(rects):
        if not isinstance(item, dict):
            raise RedactError(f"rects[{i}] must be an object {{page, rect}}")
        try:
            page = int(item.get("page"))
            x0, y0, x1, y1 = (float(v) for v in item.get("rect"))
        except (TypeError, ValueError) as exc:
            raise RedactError(f"rects[{i}] needs an integer `page` and `rect` [x0,y0,x1,y1]") from exc
        if page < 1 or page > page_count:
            raise RedactError(f"rects[{i}]: page {page} is out of range (1..{page_count})")
        if x0 > x1:
            x0, x1 = x1, x0
        if y0 > y1:
            y0, y1 = y1, y0
        if x1 - x0 <= 0 or y1 - y0 <= 0:
            raise RedactError(f"rects[{i}] has zero area")
        out.setdefault(page - 1, []).append((x0, y0, x1, y1))
    return out


# ---------------------------------------------------------------------------
# Text extraction used for locating and verifying
# ---------------------------------------------------------------------------

def _pypdf_page_texts(path: str) -> List[str]:
    import pypdf
    reader = pypdf.PdfReader(path)
    texts: List[str] = []
    for page in reader.pages:
        try:
            texts.append(page.extract_text() or "")
        except Exception as exc:  # noqa: BLE001 — an unreadable page is reported, not fatal
            logger.warning("pdf_redact: could not extract a page: %s", exc)
            texts.append("")
    return texts


def _pdfium_page_texts(path: str) -> Optional[List[str]]:
    try:
        import pypdfium2 as pdfium
    except ImportError:
        return None
    try:
        pdf = pdfium.PdfDocument(path)
        out: List[str] = []
        try:
            for i in range(len(pdf)):
                page = pdf[i]
                tp = page.get_textpage()
                out.append(tp.get_text_range() or "")
                tp.close()
                page.close()
        finally:
            pdf.close()
        return out
    except Exception as exc:  # noqa: BLE001
        logger.warning("pdf_redact: pdfium text extraction failed: %s", exc)
        return None


def _variants(text: str) -> List[str]:
    """The same page text in the shapes a match may hide in: as extracted,
    whitespace-collapsed, and with every whitespace removed."""
    collapsed = re.sub(r"\s+", " ", text)
    return [text, collapsed, re.sub(r"\s+", "", text)]


def _count_in_text(text: str, compiled) -> Dict[str, int]:
    """Count, per pattern label, the most matches found in any variant of
    `text`. The whitespace-stripped variant uses patterns with their own
    whitespace removed (only for literals and regexes that do not rely on it)."""
    counts: Dict[str, int] = {}
    if not text:
        return counts
    variants = _variants(text)
    for label, rx in compiled:
        best = 0
        for idx, variant in enumerate(variants):
            if idx == 2:
                stripped = re.sub(r"\\s\+|\\ ", "", rx.pattern) if label.startswith("pattern#") else None
                if not stripped:
                    continue
                try:
                    srx = re.compile(stripped, rx.flags)
                except re.error:
                    continue
                n = len([m for m in srx.finditer(variant) if m.end() > m.start()])
            else:
                n = len([m for m in rx.finditer(variant) if m.end() > m.start()])
            best = max(best, n)
        if best:
            counts[label] = best
    return counts


def verify_no_match(path: str, compiled, *, check_metadata: bool = True,
                    check_annotations: bool = True) -> Dict[str, Any]:
    """Re-open `path` from disk and search it again. Returns
    ``{"verified": bool, "remaining": [{"where", "pattern", "count"}], "checked_with": [...]}``."""
    import pypdf
    remaining: List[Dict[str, Any]] = []
    checked = ["pypdf-text"]
    for page_no, text in enumerate(_pypdf_page_texts(path), start=1):
        for label, n in _count_in_text(text, compiled).items():
            remaining.append({"where": f"page {page_no} text", "pattern": label, "count": n})
    pdfium_texts = _pdfium_page_texts(path)
    if pdfium_texts is not None:
        checked.append("pdfium-text")
        for page_no, text in enumerate(pdfium_texts, start=1):
            for label, n in _count_in_text(text, compiled).items():
                remaining.append({"where": f"page {page_no} text (pdfium)", "pattern": label, "count": n})
    reader = pypdf.PdfReader(path)
    if check_metadata:
        checked.append("metadata")
        meta = reader.metadata or {}
        for key, value in dict(meta).items():
            for label, n in _count_in_text(str(value), compiled).items():
                remaining.append({"where": f"metadata {key}", "pattern": label, "count": n})
    if check_annotations:
        checked.append("annotations")
        for page_no, page in enumerate(reader.pages, start=1):
            for annot in page.get("/Annots") or []:
                try:
                    obj = annot.get_object()
                except Exception:  # noqa: BLE001
                    continue
                for key in ("/Contents", "/V", "/TU", "/T"):
                    val = obj.get(key)
                    if val is None:
                        continue
                    for label, n in _count_in_text(str(val), compiled).items():
                        remaining.append({"where": f"page {page_no} annotation {key}",
                                          "pattern": label, "count": n})
    return {"verified": not remaining, "remaining": remaining, "checked_with": checked}


# ---------------------------------------------------------------------------
# Engine A: locate with pdfium, flatten the affected pages
# ---------------------------------------------------------------------------

def _have_pdfium() -> bool:
    try:
        import pypdfium2  # noqa: F401
        return True
    except ImportError:
        return False


def _merge_line_boxes(boxes: List[Tuple[float, float, float, float]]):
    """Merge per-character boxes on the same line into one box per line."""
    merged: List[List[float]] = []
    for x0, y0, x1, y1 in boxes:
        placed = False
        for m in merged:
            overlap = min(m[3], y1) - max(m[1], y0)
            if overlap > 0.5 * min(m[3] - m[1], y1 - y0):
                m[0], m[1], m[2], m[3] = min(m[0], x0), min(m[1], y0), max(m[2], x1), max(m[3], y1)
                placed = True
                break
        if not placed:
            merged.append([x0, y0, x1, y1])
    return [tuple(m) for m in merged]


def _locate_with_pdfium(page, compiled) -> Tuple[List[Tuple[float, float, float, float]], Dict[str, int]]:
    tp = page.get_textpage()
    try:
        text = tp.get_text_range() or ""
        counts: Dict[str, int] = {}
        boxes: List[Tuple[float, float, float, float]] = []
        for start, end, label in _find_spans(text, compiled):
            counts[label] = counts.get(label, 0) + 1
            span_boxes = []
            for ci in range(start, end):
                if text[ci].isspace():
                    continue
                x0, y0, x1, y1 = tp.get_charbox(ci)
                if x1 > x0 and y1 > y0:
                    span_boxes.append((x0, y0, x1, y1))
            boxes.extend(_merge_line_boxes(span_boxes))
        return boxes, counts
    finally:
        tp.close()


def _to_pixels(rect, *, rotation: int, origin: Tuple[float, float], width: float,
               height: float, scale: float, pad: float):
    """User-space rectangle -> pixel rectangle on the rendered (rotated) page.
    `width`/`height` are the UNROTATED crop-box size."""
    x0, y0, x1, y1 = rect
    x0, x1 = x0 - pad, x1 + pad
    y0, y1 = y0 - pad, y1 + pad
    pts = []
    for x, y in ((x0, y0), (x1, y0), (x0, y1), (x1, y1)):
        u, v = x - origin[0], y - origin[1]
        if rotation == 0:
            px, py = u, height - v
        elif rotation == 90:
            px, py = v, u
        elif rotation == 180:
            px, py = width - u, v
        else:  # 270
            px, py = height - v, width - u
        pts.append((px * scale, py * scale))
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def _flatten_page(page, rects, dpi: int):
    """Render `page` with `rects` painted black -> (PIL image, size in points)."""
    from PIL import ImageDraw
    scale = dpi / 72.0
    rotation = int(page.get_rotation() or 0) % 360
    cx0, cy0, cx1, cy1 = page.get_cropbox()
    width, height = cx1 - cx0, cy1 - cy0
    bitmap = page.render(scale=scale)
    image = bitmap.to_pil().convert("RGB")
    draw = ImageDraw.Draw(image)
    for rect in rects:
        box = _to_pixels(rect, rotation=rotation, origin=(cx0, cy0), width=width,
                         height=height, scale=scale, pad=_PAD_PT)
        draw.rectangle(box, fill=(0, 0, 0))
    w_pt, h_pt = (height, width) if rotation in (90, 270) else (width, height)
    return image, (w_pt, h_pt)


def _image_to_pdf_page(image, size_pt: Tuple[float, float], dpi: int):
    import pypdf
    buf = io.BytesIO()
    image.save(buf, format="PDF", resolution=float(dpi))
    buf.seek(0)
    page = pypdf.PdfReader(buf).pages[0]
    # Keep the original page size exactly (rounding of pixels aside).
    page.scale_to(size_pt[0], size_pt[1])
    return page


def _redact_flatten(path: str, out_path: str, compiled, rect_map, dpi: int) -> Dict[str, Any]:
    import pypdf
    import pypdfium2 as pdfium
    reader = pypdf.PdfReader(path)
    writer = pypdf.PdfWriter()
    pdf = pdfium.PdfDocument(path)
    per_page: List[Dict[str, Any]] = []
    flattened: List[int] = []
    try:
        for i, src_page in enumerate(reader.pages):
            page = pdf[i]
            try:
                boxes, counts = _locate_with_pdfium(page, compiled) if compiled else ([], {})
                extra = rect_map.get(i, [])
                if not boxes and not extra:
                    writer.add_page(src_page)
                    continue
                image, size_pt = _flatten_page(page, list(boxes) + list(extra), dpi)
            finally:
                page.close()
            writer.add_page(_image_to_pdf_page(image, size_pt, dpi))
            flattened.append(i + 1)
            per_page.append({"page": i + 1, "pattern_matches": counts, "rectangles": len(extra),
                             "boxes": len(boxes) + len(extra)})
    finally:
        pdf.close()
    return {"writer": writer, "reader": reader, "flattened_pages": flattened, "per_page": per_page}


# ---------------------------------------------------------------------------
# Engine B: rewrite the content stream with pypdf only
# ---------------------------------------------------------------------------

_TEXT_OPS = {b"Tj", b"TJ", b"'", b'"'}


def _font_decoders(page) -> Dict[str, Any]:
    """font resource name -> dict(code-byte -> str) for single-byte fonts.
    Fonts with multi-byte codes are left out (they are not rewritten)."""
    decoders: Dict[str, Any] = {}
    try:
        fonts = page["/Resources"]["/Font"]
    except Exception:  # noqa: BLE001
        return decoders
    for name, ref in dict(fonts).items():
        try:
            font = ref.get_object()
        except Exception:  # noqa: BLE001
            continue
        if str(font.get("/Subtype")) == "/Type0":
            continue
        table: Dict[int, str] = {}
        try:
            from pypdf._cmap import build_char_map
            _subtype, _space, _enc, cmap = build_char_map(str(name), 200.0, page)[:4]
            for k, v in (cmap or {}).items():
                if isinstance(k, str) and len(k) == 1 and isinstance(v, str):
                    table[ord(k)] = v
        except Exception:  # noqa: BLE001 — fall back to cp1252 below
            table = {}
        decoders[str(name)] = table
    return decoders


def _decode_bytes(raw: bytes, table: Dict[int, str]) -> List[str]:
    out: List[str] = []
    for b in raw:
        if b in table:
            out.append(table[b])
        else:
            out.append(bytes([b]).decode("cp1252", errors="replace"))
    return out


def _redact_stream_page(page, compiled) -> Dict[str, int]:
    """Delete matched characters from the page's text-showing operands.
    Returns counts per pattern label; 0 matches -> page untouched."""
    from pypdf.generic import ContentStream, ByteStringObject, TextStringObject, ArrayObject, NameObject
    content = ContentStream(page.get_contents(), page.pdf)
    ops = content.operations
    decoders = _font_decoders(page)
    # Flat list of (op_index, sub_index(None for Tj), byte_index, char) in stream order.
    cells: List[Tuple[int, Optional[int], int, str]] = []
    text_parts: List[str] = []
    current_font: Optional[str] = None
    skip_ops: set = set()
    for oi, (operands, operator) in enumerate(ops):
        if operator == b"Tf" and operands:
            current_font = str(operands[0])
            continue
        if operator not in _TEXT_OPS:
            continue
        table = decoders.get(current_font or "")
        if table is None:
            skip_ops.add(oi)   # multi-byte or unknown font: cannot rewrite safely
            text_parts.append(" ")
            cells.append((oi, None, -1, " "))
            continue
        strings = []
        if operator == b"TJ":
            for si, item in enumerate(operands[0]):
                if isinstance(item, (ByteStringObject, TextStringObject, bytes, str)):
                    strings.append((si, item))
        else:
            strings.append((None, operands[-1]))
        for si, item in strings:
            raw = item if isinstance(item, (bytes, bytearray)) else (
                bytes(item.original_bytes) if hasattr(item, "original_bytes") else str(item).encode("latin-1", "replace"))
            for bi, ch in enumerate(_decode_bytes(bytes(raw), table)):
                cells.append((oi, si, bi, ch))
                text_parts.append(ch)
        cells.append((oi, None, -1, " "))   # virtual separator between show operations
        text_parts.append(" ")
    text = "".join(text_parts)
    counts: Dict[str, int] = {}
    delete: Dict[Tuple[int, Optional[int]], set] = {}
    for start, end, label in _find_spans(text, compiled):
        counts[label] = counts.get(label, 0) + 1
        for ci in range(start, end):
            oi, si, bi, _ch = cells[ci]
            if bi < 0 or oi in skip_ops:
                continue
            delete.setdefault((oi, si), set()).add(bi)
    if not delete:
        return counts
    for (oi, si), drop in delete.items():
        operands, operator = ops[oi]

        def cut(item):
            raw = item if isinstance(item, (bytes, bytearray)) else (
                bytes(item.original_bytes) if hasattr(item, "original_bytes") else str(item).encode("latin-1", "replace"))
            kept = bytes(b for idx, b in enumerate(bytes(raw)) if idx not in drop)
            return ByteStringObject(kept)

        if operator == b"TJ":
            arr = ArrayObject(list(operands[0]))
            arr[si] = cut(arr[si])
            ops[oi] = ([arr], operator)
        else:
            new_operands = list(operands)
            new_operands[-1] = cut(new_operands[-1])
            ops[oi] = (new_operands, operator)
    content.operations = ops
    page[NameObject("/Contents")] = content
    return counts


def _redact_stream(path: str, compiled) -> Dict[str, Any]:
    import pypdf
    reader = pypdf.PdfReader(path)
    writer = pypdf.PdfWriter()
    per_page: List[Dict[str, Any]] = []
    changed: List[int] = []
    for i, page in enumerate(reader.pages):
        out_page = writer.add_page(page)
        counts = _redact_stream_page(out_page, compiled)
        if counts:
            changed.append(i + 1)
            per_page.append({"page": i + 1, "pattern_matches": counts, "rectangles": 0, "boxes": 0})
    return {"writer": writer, "reader": reader, "flattened_pages": [], "rewritten_pages": changed,
            "per_page": per_page}


# ---------------------------------------------------------------------------
# Shared finishing: metadata / annotations scrub, write, verify
# ---------------------------------------------------------------------------

def _scrub_metadata_and_annots(writer, reader, compiled) -> Dict[str, int]:
    from pypdf.generic import NameObject
    stats = {"metadata_fields_cleared": 0, "annotations_removed": 0}
    meta = dict(reader.metadata or {})
    clean: Dict[str, str] = {}
    for key, value in meta.items():
        if _count_in_text(str(value), compiled):
            stats["metadata_fields_cleared"] += 1
        else:
            clean[str(key)] = str(value)
    try:
        writer.add_metadata(clean)
        for key in meta:
            if str(key) not in clean:
                writer._info.get_object().pop(NameObject(str(key)), None)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001 — verification will report leftovers
        logger.warning("pdf_redact: metadata scrub failed: %s", exc)
    for page in writer.pages:
        annots = page.get("/Annots")
        if not annots:
            continue
        kept = []
        for annot in annots:
            try:
                obj = annot.get_object()
            except Exception:  # noqa: BLE001
                continue
            hit = any(_count_in_text(str(obj.get(k)), compiled) for k in ("/Contents", "/V", "/TU", "/T")
                      if obj.get(k) is not None)
            if hit:
                stats["annotations_removed"] += 1
            else:
                kept.append(annot)
        if len(kept) != len(annots):
            page[NameObject("/Annots")] = type(annots)(kept)
    return stats


def redact(path: str, out_path: str, *, patterns: Optional[Sequence[str]] = None,
           regex: Optional[Sequence[str]] = None, rects: Any = None,
           case_sensitive: bool = False, dpi: int = DEFAULT_DPI,
           engine: Optional[str] = None) -> Dict[str, Any]:
    """Redact `path` into `out_path` (both already confined by the caller).

    Returns a report without any matched text. ``verified`` is True only when
    the re-read output holds no match; otherwise the output is deleted and
    ``output`` is None.
    """
    import pypdf
    if not patterns and not regex and not rects:
        raise RedactError("give `patterns`, `regex` or `rects` — there is nothing to redact")
    dpi = max(MIN_DPI, min(MAX_DPI, int(dpi)))
    compiled = compile_patterns(patterns, regex, case_sensitive=case_sensitive)
    try:
        reader = pypdf.PdfReader(path)
    except Exception as exc:  # noqa: BLE001
        raise RedactError(f"could not open '{path}' as a PDF: {exc}") from exc
    if reader.is_encrypted:
        raise RedactError("the PDF is encrypted; decrypt it before redacting")
    page_count = len(reader.pages)
    rect_map = parse_rects(rects, page_count)

    chosen = (engine or "").strip().lower() or ("flatten" if _have_pdfium() else "stream")
    if chosen not in ("flatten", "stream"):
        raise RedactError("`engine` must be 'flatten' or 'stream'")
    if chosen == "flatten" and not _have_pdfium():
        raise RedactError("the flatten engine needs pypdfium2 (pip install pypdfium2); "
                          "without it only text patterns can be redacted, with engine 'stream'")
    if chosen == "stream" and rect_map:
        raise RedactError("page rectangles need the flatten engine (pypdfium2 is required)")

    if chosen == "flatten":
        work = _redact_flatten(path, out_path, compiled, rect_map, dpi)
    else:
        work = _redact_stream(path, compiled)
    per_page = work["per_page"]
    total_matches = sum(sum(p["pattern_matches"].values()) for p in per_page)
    total_rects = sum(p["rectangles"] for p in per_page)
    base = {"engine": chosen, "page_count": page_count,
            "flattened_pages": work["flattened_pages"],
            "rewritten_pages": work.get("rewritten_pages", []),
            "matches_removed": total_matches, "rectangles_applied": total_rects,
            "per_page": per_page}

    # Metadata / annotations can carry a match even when no page text did.
    scrub_needed = bool(compiled)
    if not per_page and not scrub_needed:
        return {**base, "output": None, "verified": True, "remaining": [],
                "checked_with": [], "note": "nothing matched; no file was written"}
    stats = {"metadata_fields_cleared": 0, "annotations_removed": 0}
    if compiled:
        stats = _scrub_metadata_and_annots(work["writer"], work["reader"], compiled)
    if not per_page and not any(stats.values()):
        return {**base, "output": None, "verified": True, "remaining": [],
                "checked_with": [], "note": "nothing matched; no file was written", **stats}

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "wb") as fh:
        work["writer"].write(fh)

    if compiled:
        verification = verify_no_match(out_path, compiled)
    else:
        verification = {"verified": True, "remaining": [], "checked_with": ["rectangles-only"]}
    result = {**base, **stats, **verification, "output": out_path}
    if not verification["verified"]:
        try:
            os.remove(out_path)
        except OSError:
            pass
        result["output"] = None
        result["note"] = ("verification failed: the output still contained matching text and "
                          "was deleted. " + ("Retry with the flatten engine (install pypdfium2)."
                                             if chosen == "stream" else ""))
    return result


__all__ = ["RedactError", "compile_patterns", "parse_rects", "verify_no_match", "redact"]
