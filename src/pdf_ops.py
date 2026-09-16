"""src/pdf_ops.py — PDF operations Faustus's own document layer never had:
merge, split, extract, rotate, reorder, delete pages, metadata, compress,
watermark, page count, optional to-images, optional OCR.

Reconnaissance (§1 of the Reach contract): `src/pdf_forms.py` covers AcroForm
field detection/extraction (PyMuPDF, optional, AGPL-3.0),
`src/document_actions.py` covers visual overflow detection (pdfplumber), and
`src/chat_export_pdf.py` covers *writing* a brand-new PDF from a chat
transcript (reportlab Platypus). None of the three can take an existing PDF
and merge/split/rotate/compress/watermark it — that gap is this module.

Design, matching the Stirling-PDF research note (`tool_repos.md` §2):
`pypdf` is already a HARD dependency (`requirements.txt`), MIT-licensed, and
covers every operation below except OCR and rasterisation — so this module
does not add a new hard dependency, and does not shell out to Ghostscript or
embed Stirling's Java/Spring stack, per the note's own "no copiar
Stirling-PDF entero" conclusion. `reportlab` (already required, for
`chat_export_pdf.py`) draws the watermark text. `pypdfium2`/`pdf2image` and
`ocrmypdf` are OPTIONAL: imported lazily, and every op that needs one
degrades to an explicit `"install X"` error rather than crashing the tool
call — the same shape `src/media_capabilities.py` uses for ffmpeg.

Path confinement: every path this module opens or writes goes through
`src.tool_execution._resolve_tool_path`, the SAME guard `read_file`/
`write_file` use for the active workspace / `DATA_DIR` allowlist. This module
never invents its own notion of "safe path".

Output policy: an op that produces a new document ALWAYS writes to a path
distinct from every input, unless the caller passes `overwrite: true` and
that path coincides with an input — never silently clobbered.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


class PdfOpsError(ValueError):
    """A request this module refuses to carry out, with the reason a human
    (or the model reading the tool result) needs."""


# ---------------------------------------------------------------------------
# Path confinement — reuse the SAME guard read_file/write_file use.
# ---------------------------------------------------------------------------

def resolve_path(raw_path: str) -> str:
    """Confine `raw_path` to the active workspace / DATA_DIR allowlist.

    Delegates to `src.tool_execution._resolve_tool_path`: the active-workspace
    contextvar when a workspace is bound to this turn, the default
    `DATA_DIR`/tmp allowlist (which includes `DATA_DIR/uploads`) otherwise.
    Never duplicates that policy here — a second implementation is a second
    place it can drift from the first and stop meaning the same thing.
    """
    from src.tool_execution import _resolve_tool_path
    try:
        return _resolve_tool_path(raw_path)
    except ValueError as exc:
        raise PdfOpsError(str(exc)) from exc


def _default_output_path(input_path: str, suffix: str) -> str:
    """`report.pdf` + `merged` -> `report.merged.pdf`, next to the source."""
    base, ext = os.path.splitext(input_path)
    ext = ext or ".pdf"
    return f"{base}.{suffix}{ext}"


def _resolve_output(raw_output: Optional[str], *, default_input: str, suffix: str,
                    overwrite: bool, inputs: Sequence[str]) -> str:
    """Resolve and guard the destination path.

    Never overwrites an INPUT file unless the caller explicitly set
    `overwrite: true` — that is the whole point of "a fresh file next to the
    origin, never silently clobbered" (§ Part A of the contract).
    """
    if raw_output:
        resolved = resolve_path(raw_output)
    else:
        resolved = resolve_path(_default_output_path(default_input, suffix))
    inputs_resolved = {os.path.realpath(p) for p in inputs}
    if os.path.realpath(resolved) in inputs_resolved and not overwrite:
        raise PdfOpsError(
            f"output path '{raw_output or resolved}' is the same as an input file; "
            f"pass \"overwrite\": true to replace it, or choose a different output path"
        )
    return resolved


def _require_pypdf():
    try:
        import pypdf
    except ImportError as exc:  # pragma: no cover — pypdf is a hard dependency
        raise PdfOpsError(
            "pypdf is required for PDF operations and should already be installed "
            "(see requirements.txt); reinstall with `pip install pypdf`"
        ) from exc
    return pypdf


def _open_reader(path: str):
    pypdf = _require_pypdf()
    try:
        return pypdf.PdfReader(path)
    except Exception as exc:  # noqa: BLE001 — a corrupt/encrypted PDF is data, not a crash
        raise PdfOpsError(f"could not open '{path}' as a PDF: {exc}") from exc


def _write_pdf(writer, out_path: str) -> None:
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "wb") as fh:
        writer.write(fh)


# ---------------------------------------------------------------------------
# Page range parsing — "1-3,5,8-10" (1-based, inclusive) -> 0-based indices.
# ---------------------------------------------------------------------------

def parse_page_ranges(spec: Any, page_count: int) -> List[int]:
    """Parse a human page-range spec into 0-based page indices, in the order
    given (so `"3,1"` reorders, and repeats like `"1,1"` are honoured).

    Accepts a string ("1-3,5,8-10"), or a list of ints/strings. Every index
    must land inside `[1, page_count]` (1-based) or this raises.
    """
    if page_count <= 0:
        raise PdfOpsError("document has no pages")
    if isinstance(spec, (list, tuple)):
        tokens: List[str] = [str(x) for x in spec]
    else:
        text = str(spec or "").strip()
        if not text:
            raise PdfOpsError("`pages` is required (e.g. \"1-3,5\")")
        tokens = [t.strip() for t in text.split(",") if t.strip()]
    if not tokens:
        raise PdfOpsError("`pages` is required (e.g. \"1-3,5\")")

    indices: List[int] = []
    for token in tokens:
        if "-" in token:
            parts = token.split("-", 1)
            try:
                start, end = int(parts[0]), int(parts[1])
            except ValueError as exc:
                raise PdfOpsError(f"'{token}' is not a valid page range") from exc
            if start > end:
                start, end = end, start
            span = range(start, end + 1)
        else:
            try:
                span = [int(token)]
            except ValueError as exc:
                raise PdfOpsError(f"'{token}' is not a valid page number") from exc
        for page_1based in span:
            if page_1based < 1 or page_1based > page_count:
                raise PdfOpsError(
                    f"page {page_1based} is out of range — this document has {page_count} page(s)"
                )
            indices.append(page_1based - 1)
    return indices


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

def page_count(input_path: str) -> Dict[str, Any]:
    path = resolve_path(input_path)
    reader = _open_reader(path)
    return {"input": path, "page_count": len(reader.pages)}


def merge(inputs: Sequence[str], output: Optional[str], *, overwrite: bool = False) -> Dict[str, Any]:
    """Concatenates `inputs` in order into one PDF.

    Delegates to `src.document_actions.merge_pdfs` — that function already
    does exactly this (with a page/byte budget guard, ART-07) and this
    module has no business reimplementing it; this wrapper only adds path
    confinement and the "never clobber an input" output rule the rest of
    `pdf_ops` shares.
    """
    if not inputs or len(inputs) < 2:
        raise PdfOpsError("merge: `inputs` needs at least two PDF paths")
    from src import document_actions
    resolved_inputs = [resolve_path(p) for p in inputs]
    out_path = _resolve_output(output, default_input=resolved_inputs[0], suffix="merged",
                               overwrite=overwrite, inputs=resolved_inputs)
    try:
        result = document_actions.merge_pdfs(resolved_inputs, out_path)
    except document_actions.DocumentActionError as exc:
        raise PdfOpsError(f"merge: {exc}") from exc
    return {"output": out_path, "inputs": resolved_inputs, "page_count": result["pages"]}


def split(input_path: str, ranges: Sequence[str], output_dir: Optional[str] = None,
         *, overwrite: bool = False) -> Dict[str, Any]:
    """One arbitrary page RANGE per output file — `ranges=["1-3", "4-6"]` ->
    two PDFs, possibly overlapping, possibly out of order.

    Deliberately NOT a wrapper over `src.document_actions.split_pdf`: that
    function groups pages into fixed-size chunks ("N pages per file") for a
    size/page BUDGET (ART-07) — a different job from "give me chapters 1-3
    and 4-6 as two files", which is what this op is for. Both are kept; a
    caller after uniform chunking with a budget guard still wants the
    existing function.
    """
    if not ranges:
        raise PdfOpsError("split: `ranges` (a list of page-range strings) is required")
    pypdf = _require_pypdf()
    path = resolve_path(input_path)
    reader = _open_reader(path)
    total = len(reader.pages)
    base, ext = os.path.splitext(path)
    ext = ext or ".pdf"
    out_dir = resolve_path(output_dir) if output_dir else os.path.dirname(path)
    outputs: List[str] = []
    for i, range_spec in enumerate(ranges, start=1):
        indices = parse_page_ranges(range_spec, total)
        writer = pypdf.PdfWriter()
        for idx in indices:
            writer.add_page(reader.pages[idx])
        out_name = f"{os.path.basename(base)}.part{i}{ext}"
        out_path = os.path.join(out_dir, out_name)
        if os.path.realpath(out_path) == os.path.realpath(path) and not overwrite:
            raise PdfOpsError(
                f"split: computed output '{out_path}' collides with the input; "
                f"pass \"overwrite\": true or choose `output_dir`"
            )
        _write_pdf(writer, out_path)
        outputs.append(out_path)
    return {"input": path, "outputs": outputs, "page_count": total}


def extract_pages(input_path: str, pages: Any, output: Optional[str] = None,
                  *, overwrite: bool = False) -> Dict[str, Any]:
    pypdf = _require_pypdf()
    path = resolve_path(input_path)
    reader = _open_reader(path)
    indices = parse_page_ranges(pages, len(reader.pages))
    out_path = _resolve_output(output, default_input=path, suffix="extracted",
                               overwrite=overwrite, inputs=[path])
    writer = pypdf.PdfWriter()
    for idx in indices:
        writer.add_page(reader.pages[idx])
    _write_pdf(writer, out_path)
    return {"output": out_path, "input": path, "page_count": len(indices)}


def rotate(input_path: str, pages: Any, degrees: int, output: Optional[str] = None,
          *, overwrite: bool = False) -> Dict[str, Any]:
    if degrees % 90 != 0:
        raise PdfOpsError("rotate: `degrees` must be a multiple of 90")
    pypdf = _require_pypdf()
    path = resolve_path(input_path)
    reader = _open_reader(path)
    total = len(reader.pages)
    indices = set(parse_page_ranges(pages, total)) if pages else set(range(total))
    out_path = _resolve_output(output, default_input=path, suffix="rotated",
                               overwrite=overwrite, inputs=[path])
    writer = pypdf.PdfWriter()
    for i, page in enumerate(reader.pages):
        if i in indices:
            page = page.rotate(degrees)
        writer.add_page(page)
    _write_pdf(writer, out_path)
    return {"output": out_path, "input": path, "rotated_pages": sorted(p + 1 for p in indices)}


def reorder(input_path: str, order: Sequence[Any], output: Optional[str] = None,
           *, overwrite: bool = False) -> Dict[str, Any]:
    """`order` is a full 1-based permutation of every page — every page of
    the source must appear exactly once, which is what distinguishes this
    from `extract_pages` (a subset)."""
    pypdf = _require_pypdf()
    path = resolve_path(input_path)
    reader = _open_reader(path)
    total = len(reader.pages)
    indices = parse_page_ranges(list(order), total)
    if sorted(indices) != list(range(total)):
        raise PdfOpsError(
            f"reorder: `order` must name every one of the {total} page(s) exactly once "
            f"(got {len(indices)} entries covering {len(set(indices))} distinct pages) — "
            f"use extract_pages for a subset"
        )
    out_path = _resolve_output(output, default_input=path, suffix="reordered",
                               overwrite=overwrite, inputs=[path])
    writer = pypdf.PdfWriter()
    for idx in indices:
        writer.add_page(reader.pages[idx])
    _write_pdf(writer, out_path)
    return {"output": out_path, "input": path, "page_count": total}


def delete_pages(input_path: str, pages: Any, output: Optional[str] = None,
                 *, overwrite: bool = False) -> Dict[str, Any]:
    pypdf = _require_pypdf()
    path = resolve_path(input_path)
    reader = _open_reader(path)
    total = len(reader.pages)
    to_delete = set(parse_page_ranges(pages, total))
    remaining = [i for i in range(total) if i not in to_delete]
    if not remaining:
        raise PdfOpsError("delete_pages: this would remove every page — nothing would be left")
    out_path = _resolve_output(output, default_input=path, suffix="trimmed",
                               overwrite=overwrite, inputs=[path])
    writer = pypdf.PdfWriter()
    for idx in remaining:
        writer.add_page(reader.pages[idx])
    _write_pdf(writer, out_path)
    return {"output": out_path, "input": path, "deleted_pages": sorted(p + 1 for p in to_delete),
            "page_count": len(remaining)}


_METADATA_FIELDS = {
    "title": "/Title", "author": "/Author", "subject": "/Subject", "keywords": "/Keywords",
}


def metadata(input_path: str, set_fields: Optional[Dict[str, str]] = None,
            output: Optional[str] = None, *, overwrite: bool = False) -> Dict[str, Any]:
    """Read metadata when `set_fields` is empty/None; write it (into a new
    file, same overwrite rule as every other op) when given."""
    path = resolve_path(input_path)
    reader = _open_reader(path)
    current = dict(reader.metadata or {})
    read_out = {
        "title": current.get("/Title", ""), "author": current.get("/Author", ""),
        "subject": current.get("/Subject", ""), "keywords": current.get("/Keywords", ""),
    }
    if not set_fields:
        return {"input": path, "metadata": read_out}

    unknown = [k for k in set_fields if k not in _METADATA_FIELDS]
    if unknown:
        raise PdfOpsError(
            f"metadata: unknown field(s) {unknown} — allowed: {sorted(_METADATA_FIELDS)}"
        )
    pypdf = _require_pypdf()
    out_path = _resolve_output(output, default_input=path, suffix="meta",
                               overwrite=overwrite, inputs=[path])
    writer = pypdf.PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    new_meta = {v: current.get(v, "") for v in _METADATA_FIELDS.values()}
    for key, value in set_fields.items():
        new_meta[_METADATA_FIELDS[key]] = str(value)
    writer.add_metadata(new_meta)
    _write_pdf(writer, out_path)
    return {"output": out_path, "input": path, "metadata": {
        "title": new_meta["/Title"], "author": new_meta["/Author"],
        "subject": new_meta["/Subject"], "keywords": new_meta["/Keywords"],
    }}


def compress(input_path: str, output: Optional[str] = None, *, overwrite: bool = False) -> Dict[str, Any]:
    """Re-writes the PDF with pypdf's own content-stream compression and
    duplicate-object removal. This is NOT Ghostscript-grade image
    downsampling — pypdf has no image recompression — it is stream
    deflate + object dedup, which is still a real (if modest) win on
    text-heavy documents and always reports the honest before/after size so
    a caller can see whether it helped."""
    pypdf = _require_pypdf()
    path = resolve_path(input_path)
    before_bytes = os.path.getsize(path)
    reader = _open_reader(path)
    out_path = _resolve_output(output, default_input=path, suffix="compressed",
                               overwrite=overwrite, inputs=[path])
    writer = pypdf.PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    # compress_content_streams() needs the page to already belong to a
    # PdfWriter (it replaces the content-stream object in place) — done
    # after add_page, not on the reader's own page objects.
    for page in writer.pages:
        page.compress_content_streams()
    try:
        writer.compress_identical_objects(remove_identicals=True, remove_orphans=True)
    except Exception as exc:  # noqa: BLE001 — best-effort; older pypdf lacks this
        logger.debug("pdf_ops.compress: compress_identical_objects unavailable: %s", exc)
    _write_pdf(writer, out_path)
    after_bytes = os.path.getsize(out_path)
    return {
        "output": out_path, "input": path,
        "bytes_before": before_bytes, "bytes_after": after_bytes,
        "bytes_saved": max(0, before_bytes - after_bytes),
        "percent_saved": round(100.0 * max(0, before_bytes - after_bytes) / before_bytes, 1)
                         if before_bytes else 0.0,
    }


def watermark_text(input_path: str, text: str, output: Optional[str] = None, *,
                   opacity: float = 0.3, font_size: int = 40, angle: float = 45.0,
                   overwrite: bool = False) -> Dict[str, Any]:
    """Stamps `text` diagonally across every page, using reportlab (already a
    hard dependency for `chat_export_pdf.py`) to draw one overlay page per
    input page size, then merges it under/over with pypdf."""
    text = str(text or "").strip()
    if not text:
        raise PdfOpsError("watermark_text: `text` is required")
    try:
        from reportlab.pdfgen import canvas
        from reportlab.lib.colors import Color
    except ImportError as exc:  # pragma: no cover — reportlab is a hard dependency
        raise PdfOpsError(
            "reportlab is required for watermarking and should already be installed "
            "(see requirements.txt); reinstall with `pip install reportlab`"
        ) from exc
    pypdf = _require_pypdf()
    import io

    path = resolve_path(input_path)
    reader = _open_reader(path)
    out_path = _resolve_output(output, default_input=path, suffix="watermarked",
                               overwrite=overwrite, inputs=[path])
    writer = pypdf.PdfWriter()
    for page in reader.pages:
        w = float(page.mediabox.width)
        h = float(page.mediabox.height)
        buf = io.BytesIO()
        c = canvas.Canvas(buf, pagesize=(w, h))
        c.saveState()
        c.setFillColor(Color(0.5, 0.5, 0.5, alpha=max(0.0, min(1.0, opacity))))
        c.setFont("Helvetica-Bold", font_size)
        c.translate(w / 2.0, h / 2.0)
        c.rotate(angle)
        c.drawCentredString(0, 0, text)
        c.restoreState()
        c.save()
        buf.seek(0)
        overlay_reader = pypdf.PdfReader(buf)
        page.merge_page(overlay_reader.pages[0])
        writer.add_page(page)
    _write_pdf(writer, out_path)
    return {"output": out_path, "input": path, "page_count": len(reader.pages)}


# ---------------------------------------------------------------------------
# Optional: rasterisation (pypdfium2 or pdf2image — whichever is installed)
# ---------------------------------------------------------------------------

def to_images(input_path: str, output_dir: Optional[str] = None, *, dpi: int = 150,
             pages: Any = None) -> Dict[str, Any]:
    path = resolve_path(input_path)
    out_dir = resolve_path(output_dir) if output_dir else os.path.dirname(path)
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(path))[0]

    try:
        import pypdfium2 as pdfium
    except ImportError:
        pdfium = None

    if pdfium is not None:
        pdf = pdfium.PdfDocument(path)
        total = len(pdf)
        indices = parse_page_ranges(pages, total) if pages else list(range(total))
        scale = dpi / 72.0
        outputs: List[str] = []
        for idx in indices:
            bitmap = pdf[idx].render(scale=scale)
            pil_image = bitmap.to_pil()
            out_path = os.path.join(out_dir, f"{base}.page{idx + 1}.png")
            pil_image.save(out_path)
            outputs.append(out_path)
        return {"input": path, "outputs": outputs, "engine": "pypdfium2"}

    try:
        from pdf2image import convert_from_path
    except ImportError as exc:
        raise PdfOpsError(
            "to_images requires pypdfium2 or pdf2image, neither of which is installed. "
            "Install one with `pip install -r requirements-optional.txt` "
            "(pdf2image also needs the poppler-utils binaries on PATH)."
        ) from exc
    reader = _open_reader(path)
    total = len(reader.pages)
    indices = parse_page_ranges(pages, total) if pages else list(range(total))
    first_page, last_page = min(indices) + 1, max(indices) + 1
    images = convert_from_path(path, dpi=dpi, first_page=first_page, last_page=last_page)
    wanted = set(indices)
    outputs = []
    for offset, image in enumerate(images):
        page_1based = first_page + offset
        if (page_1based - 1) not in wanted:
            continue
        out_path = os.path.join(out_dir, f"{base}.page{page_1based}.png")
        image.save(out_path)
        outputs.append(out_path)
    return {"input": path, "outputs": outputs, "engine": "pdf2image"}


# ---------------------------------------------------------------------------
# Optional: OCR via the ocrmypdf CLI (subprocess, no shell=True)
# ---------------------------------------------------------------------------

def ocr(input_path: str, output: Optional[str] = None, *, language: str = "eng",
       overwrite: bool = False, timeout_s: float = 600.0) -> Dict[str, Any]:
    exe = shutil.which("ocrmypdf")
    if not exe:
        raise PdfOpsError(
            "ocr requires the ocrmypdf CLI (MPL-2.0, needs Tesseract), which is not on PATH. "
            "Instala ocrmypdf: https://ocrmypdf.readthedocs.io/"
        )
    path = resolve_path(input_path)
    out_path = _resolve_output(output, default_input=path, suffix="ocr",
                               overwrite=overwrite, inputs=[path])
    try:
        proc = subprocess.run(
            [exe, "--language", language, "--skip-text", path, out_path],
            capture_output=True, text=True, timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        raise PdfOpsError(f"ocrmypdf did not finish within {timeout_s}s") from exc
    except OSError as exc:
        raise PdfOpsError(f"could not run ocrmypdf: {exc}") from exc
    if proc.returncode != 0:
        raise PdfOpsError(
            f"ocrmypdf exited {proc.returncode}: {(proc.stderr or proc.stdout or '').strip()[:800]}"
        )
    return {"output": out_path, "input": path, "language": language,
            "stdout": (proc.stdout or "").strip()[:2000]}


__all__ = [
    "PdfOpsError", "resolve_path", "parse_page_ranges",
    "page_count", "merge", "split", "extract_pages", "rotate", "reorder",
    "delete_pages", "metadata", "compress", "watermark_text", "to_images", "ocr",
]
