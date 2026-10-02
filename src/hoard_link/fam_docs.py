"""The family's document services, from inside an app: PDF operations, text extraction from any document, and OCR.

One owner, Kafka's Hoard (app id ``kafka``), called through the hub (``hoard_link.family.call``): the PDF workshop (merge, split, pages,
compress, protect, watermark, convert), the document readers with an OCR pass for scans, and the one OCR engine on the machine. The tool
names, arguments and result shapes are the contract in ``docs/commons/services.md``; the PDF operations use Kafka's own tool names and
argument names.

Standard library only. Nothing here raises: every function returns a dict, ``{"ok": False, "error": "...", "via": "kafka", "kind": "..."}``
when the work could not be done (``kind``: ``hub_down`` / ``app_down`` / ``app_missing`` / ``tool_missing`` / ``timeout`` / ``auth`` /
``tool_error`` / ``client_error``; a hub that does not answer says ``"hub unreachable"``, like :mod:`hoard_link.fam_notify`).

Paths are absolute paths on the shared disk, never file contents: the owner reads and writes them. A file argument may also be a Kafka
document id (``d_...``) where Kafka's tool says so. Results of the PDF operations are Kafka's own::

    from hoard_link import family, fam_docs

    family.configure("borges", DATA_DIR)
    got = fam_docs.extract("/scans/contrato.pdf", ocr="auto", lang="es")
    if got["ok"]:
        index(got["units"])                  # [{kind, number, title, text}], the chunking input; got["via"] is "kafka" or "local"
        if got["needs_ocr"]:
            ...                              # a scan and nobody could read it (local fallback, no OCR): say so
    merged = fam_docs.pdf_merge(["/a.pdf", "/b.pdf"], out_dir="/work")
    merged["output"]                         # the new file, "name (2)" if the name was taken

Long reads (``extract`` with OCR, ``ocr_pdf``) are jobs on the owner: the first call waits at most 150 s, then the status is polled until
``timeout_s`` (:mod:`hoard_link.waiting`); past it ``kind: "timeout"`` with the ``job_id`` and the job goes on (:func:`ocr_status`).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from . import _famsvc as _s

__all__ = ["pdf_info", "pdf_merge", "pdf_split", "pdf_pages", "pdf_compress", "pdf_watermark", "pdf_protect", "pdf_metadata_set",
           "pdf_to_images", "images_to_pdf", "pdf_from_office", "images_compress", "extract", "ocr_image", "ocr_pdf", "ocr_status",
           "available", "forget_availability"]

KAFKA = "kafka"
MAX_LOCAL_BYTES = 200_000_000


def available(timeout: float = 1.0) -> bool:
    """True when the hub is up and Kafka's Hoard is running. Cached for 30 seconds."""
    return _s.available("docs", timeout)


def forget_availability() -> None:
    _s.forget_availability()


def _ok(data: Any) -> dict[str, Any]:
    out = dict(data) if isinstance(data, Mapping) else {"result": data}
    out["ok"] = True
    out["via"] = KAFKA
    outputs = out.get("outputs")
    if isinstance(outputs, list) and "paths" not in out:
        out["paths"] = [str(o.get("path")) for o in outputs if isinstance(o, Mapping) and o.get("path")]
    return out


def _run(tool: str, args: Mapping[str, Any], timeout_s: float) -> dict[str, Any]:
    res = _s.call_tool(KAFKA, tool, args, timeout_s=timeout_s)
    return _ok(res["data"]) if res["ok"] else _s.public_error(res, KAFKA)


def _p(value: Any) -> str:
    """An absolute path, or a Kafka document id (``d_...``) passed through."""
    text = os.fspath(value) if not isinstance(value, str) else value
    text = text.strip()
    return text if (text.startswith("d_") and os.sep not in text and "/" not in text) else _s.as_path(text)


def _paths(values: Any) -> list[str]:
    if isinstance(values, (str, os.PathLike)):
        values = [values]
    return [_p(v) for v in (values or [])]


def _dir(value: Optional[str]) -> Optional[str]:
    return _s.as_path(value) if value else None


# ---------------------------------------------------------------------------------------------------------------------
# PDF operations (Kafka's workshop tools, same names and arguments)
# ---------------------------------------------------------------------------------------------------------------------

@_s.never_raises(KAFKA)
def pdf_info(file: str, *, password: str = "", timeout_s: float = 60.0) -> dict[str, Any]:
    """``pdf_info``: pages, sizes, encryption, metadata and whether the PDF has a text layer."""
    return _run("pdf_info", _s.clean_args(file=_p(file), password=password), timeout_s)


@_s.never_raises(KAFKA)
def pdf_merge(files: Sequence[str], *, ranges: Optional[Sequence[str]] = None, password: str = "", output: str = "", out_dir: str = "",
              file_result: bool = False, timeout_s: float = 300.0) -> dict[str, Any]:
    """``pdf_merge``: join PDFs in order, optionally only some pages of each (``ranges``, same length as ``files``: ``["", "1-3"]``).
    ``output`` is the result file, ``out_dir`` a folder instead; nothing is overwritten (a taken name becomes ``name (2)``).
    ``file_result`` also files the result in Kafka (``doc_ids``). Returns Kafka's ``{ok, outputs: [{path...}], output, paths, ...}``."""
    return _run("pdf_merge", _s.clean_args(files=_paths(files), ranges=list(ranges) if ranges else None, password=password, output=_dir(output),
                                           out_dir=_dir(out_dir), file_result=bool(file_result) or None), timeout_s)


@_s.never_raises(KAFKA)
def pdf_split(file: str, *, mode: str = "ranges", ranges: str = "", every: int = 1, password: str = "", out_dir: str = "",
              file_result: bool = False, timeout_s: float = 300.0) -> dict[str, Any]:
    """``pdf_split``: ``mode`` ``pages`` (one file per page), ``ranges`` (one per comma-separated range, ``"1-3,4-6,7-"``) or ``every``
    (one per ``every`` pages). Default folder: ``<name>_dividido`` next to the source."""
    return _run("pdf_split", _s.clean_args(file=_p(file), mode=mode, ranges=ranges, every=int(every) if mode == "every" else None,
                                           password=password, out_dir=_dir(out_dir), file_result=bool(file_result) or None), timeout_s)


@_s.never_raises(KAFKA)
def pdf_pages(file: str, action: str, *, pages: str = "", degrees: int = 90, order: str = "", password: str = "", output: str = "",
              out_dir: str = "", timeout_s: float = 300.0) -> dict[str, Any]:
    """``pdf_pages``: ``action`` ``extract`` / ``delete`` (need ``pages``: ``"1-3,5,8-"``, ``last``, ``odd``), ``rotate`` (``pages``, default
    all; ``degrees`` 90/180/270) or ``reorder`` (``order``: ``"3,1,2"`` or ``reverse``)."""
    return _run("pdf_pages", _s.clean_args(action=action, file=_p(file), pages=pages, degrees=int(degrees) if action == "rotate" else None,
                                           order=order, password=password, output=_dir(output), out_dir=_dir(out_dir)), timeout_s)


@_s.never_raises(KAFKA)
def pdf_compress(file: str, *, preset: str = "ebook", target_mb: Optional[float] = None, engine: str = "auto", password: str = "",
                 output: str = "", out_dir: str = "", timeout_s: float = 600.0) -> dict[str, Any]:
    """``pdf_compress``: shrink a PDF. ``preset`` ``screen`` (smallest) < ``ebook`` < ``printer`` < ``prepress``; ``target_mb`` keeps trying
    stronger settings until it fits (and says when it cannot); ``engine`` ``auto`` / ``ghostscript`` / ``pypdf``."""
    return _run("pdf_compress", _s.clean_args(file=_p(file), preset=preset, target_mb=target_mb, engine=engine, password=password,
                                              output=_dir(output), out_dir=_dir(out_dir)), timeout_s)


@_s.never_raises(KAFKA)
def pdf_watermark(file: str, text: str, *, opacity: float = 0.3, angle: float = 45, font_size: float = 60, color: str = "gris", pages: str = "",
                  password: str = "", output: str = "", out_dir: str = "", timeout_s: float = 300.0) -> dict[str, Any]:
    """``pdf_watermark``: stamp ``text`` diagonally on the pages (default all). ``color``: ``gris``, ``rojo``, ``azul``, ``negro``, ``verde``,
    ``naranja`` or ``#RRGGBB``."""
    return _run("pdf_watermark", _s.clean_args(file=_p(file), text=text, opacity=opacity, angle=angle, font_size=font_size, color=color, pages=pages,
                                               password=password, output=_dir(output), out_dir=_dir(out_dir)), timeout_s)


@_s.never_raises(KAFKA)
def pdf_protect(file: str, action: str, password: str, *, owner_password: str = "", current_password: str = "", allow_print: bool = True,
                allow_copy: bool = True, allow_modify: bool = True, output: str = "", out_dir: str = "", timeout_s: float = 300.0) -> dict[str, Any]:
    """``pdf_protect``: ``action`` ``protect`` (AES-256 with ``password``) or ``unprotect`` (``password`` is the current one). The passwords
    travel to Kafka through the hub and are never echoed back."""
    return _run("pdf_protect", _s.clean_args(action=action, file=_p(file), password=password, owner_password=owner_password,
                                             current_password=current_password, allow_print=bool(allow_print), allow_copy=bool(allow_copy),
                                             allow_modify=bool(allow_modify), output=_dir(output), out_dir=_dir(out_dir)), timeout_s)


@_s.never_raises(KAFKA)
def pdf_metadata_set(file: str, *, title: Optional[str] = None, author: Optional[str] = None, subject: Optional[str] = None,
                     keywords: Optional[str] = None, password: str = "", output: str = "", out_dir: str = "", timeout_s: float = 120.0) -> dict[str, Any]:
    """``pdf_metadata_set``: set title, author, subject or keywords (an empty string clears one; ``None`` keeps it)."""
    args = {"file": _p(file), **{k: v for k, v in (("title", title), ("author", author), ("subject", subject), ("keywords", keywords)) if v is not None},
            **_s.clean_args(password=password, output=_dir(output), out_dir=_dir(out_dir))}
    return _run("pdf_metadata_set", args, timeout_s)


@_s.never_raises(KAFKA)
def pdf_to_images(file: str, *, pages: str = "", format: str = "png", dpi: int = 150, quality: int = 90, password: str = "", out_dir: str = "",
                  timeout_s: float = 600.0) -> dict[str, Any]:
    """``pdf_to_images``: render pages (default all) as ``png`` or ``jpg``. Default folder: ``<name>_imagenes`` next to the source."""
    return _run("pdf_to_images", _s.clean_args(file=_p(file), pages=pages, format=format, dpi=int(dpi), quality=int(quality), password=password,
                                               out_dir=_dir(out_dir)), timeout_s)


@_s.never_raises(KAFKA)
def images_to_pdf(images: Sequence[str], *, page_size: str = "A4", margin_mm: float = 10, orientation: str = "auto", output: str = "",
                  out_dir: str = "", file_result: bool = False, timeout_s: float = 300.0) -> dict[str, Any]:
    """``pdf_from_images``: one image per page, in order (paths, document ids, or a folder). ``page_size`` ``A4`` / ``Letter`` / ``fit``."""
    return _run("pdf_from_images", _s.clean_args(images=_paths(images), page_size=page_size, margin_mm=margin_mm, orientation=orientation,
                                                 output=_dir(output), out_dir=_dir(out_dir), file_result=bool(file_result) or None), timeout_s)


@_s.never_raises(KAFKA)
def pdf_from_office(file: str, *, engine: str = "auto", output: str = "", out_dir: str = "", timeout_s: float = 300.0) -> dict[str, Any]:
    """``pdf_from_office``: a .docx / .doc / .odt / .rtf to PDF (Word on Windows, else LibreOffice)."""
    return _run("pdf_from_office", _s.clean_args(file=_p(file), engine=engine, output=_dir(output), out_dir=_dir(out_dir)), timeout_s)


@_s.never_raises(KAFKA)
def images_compress(sources: Sequence[str], *, limit_mb: Optional[float] = None, limit_kb: Optional[float] = None, recursive: bool = True,
                    lossless_only: bool = False, skip_small: bool = False, out_dir: str = "", time_limit_s: float = 70.0,
                    timeout_s: float = 300.0) -> dict[str, Any]:
    """``images_compress``: PNG / JPEG / WEBP under a size limit (``limit_mb`` or ``limit_kb``), files or folders. Kafka stops after
    ``time_limit_s`` and reports ``pending``: call again with the same ``out_dir`` to continue."""
    return _run("images_compress", _s.clean_args(sources=_paths(sources), limit_mb=limit_mb, limit_kb=limit_kb, recursive=bool(recursive),
                                                 lossless_only=bool(lossless_only), skip_small=bool(skip_small), out_dir=_dir(out_dir),
                                                 time_limit_s=float(time_limit_s)), max(float(timeout_s), float(time_limit_s) + 30.0))


# ---------------------------------------------------------------------------------------------------------------------
# extraction and OCR
# ---------------------------------------------------------------------------------------------------------------------

def _readers_result(path: str, ocr: str) -> dict[str, Any]:
    """The same extraction in this process: :func:`hoard_link.docs.readers_lite.read_any` on the file's bytes (no OCR)."""
    from .docs import readers_lite
    size = os.path.getsize(path)
    if size > MAX_LOCAL_BYTES:
        raise ValueError(f"the file is {size // 1_000_000} MB; the local reader takes at most {MAX_LOCAL_BYTES // 1_000_000} MB")
    data = Path(path).read_bytes()
    out = readers_lite.read_any(os.path.basename(path), data)
    out["notes"] = list(out.get("notes") or [])
    if out.get("needs_ocr") and ocr != "off":
        out["notes"].append("OCR was not run: Kafka's Hoard is not available" + (" (ocr=force asked for it)" if ocr == "force" else ""))
    return out


def _extract_result(data: Any, via: str) -> dict[str, Any]:
    """An extraction in the ``read_any`` shape plus ``ok`` / ``via`` / ``pages_ocr``; a document the reader could not read is
    ``{"ok": False, "error", "via", "kind": "tool_error", "result": <what it did return>}``."""
    out = dict(data) if isinstance(data, Mapping) else {}
    units = [dict(u) for u in (out.get("units") or []) if isinstance(u, Mapping)]
    text = out.get("text")
    if not isinstance(text, str):
        text = "\n\n".join(str(u.get("text") or "") for u in units if u.get("text"))
    result = {**out, "kind": str(out.get("kind") or ""), "title": str(out.get("title") or ""), "text": text, "units": units,
              "needs_ocr": bool(out.get("needs_ocr")), "notes": [str(n) for n in (out.get("notes") or [])],
              "pages_ocr": int(out.get("pages_ocr") or 0), "via": via, "ok": True}
    if out.get("error"):
        return {"ok": False, "error": str(out["error"]), "via": via, "kind": "tool_error", "result": {**result, "ok": False}}
    return result


def _is_extract_done(data: Any) -> bool:
    return _s.default_done(data)


@_s.never_raises(KAFKA)
def extract(path: str, *, ocr: str = "auto", max_pages: int = 400, lang: str = "es", local_fallback: bool = True,
            timeout_s: float = 900.0) -> dict[str, Any]:
    """The text of any document, scans included. Kafka's ``doc_extract`` through the hub: the readers for docx, odt, pptx, xlsx, epub, rtf,
    html, eml, text and PDF, and an OCR pass over the pages without a text layer (``ocr``: ``auto`` = only those pages, ``off``, ``force`` =
    every page). When Kafka cannot be reached and ``local_fallback`` is set, :func:`hoard_link.docs.readers_lite.read_any` reads the file here:
    everything but the OCR (a scan then comes back with ``needs_ocr: True`` and a note).

    Returns the shape of ``readers_lite.read_any`` plus ``ok``, ``via`` (``"kafka"`` or ``"local"``) and ``pages_ocr``: ``{ok, kind, title,
    text, units: [{kind, number, title, text}], needs_ocr, notes, pages_ocr, via, mime?}``. ``needs_ocr`` stays True only when a page is still
    unreadable. A damaged or unsupported file gives ``ok: False`` and ``error``."""
    src = _s.as_path(path)
    if not src:
        return {"ok": False, "error": "path is required", "via": KAFKA, "kind": "client_error"}
    mode = str(ocr or "auto").strip().lower()
    if mode not in ("auto", "off", "force"):
        return {"ok": False, "error": f"ocr must be auto, off or force, not {ocr!r}", "via": KAFKA, "kind": "client_error"}
    args = {"path": src, "ocr": mode, "max_pages": int(max_pages), "lang": str(lang or "es")}
    res = _s.run_job(KAFKA, "doc_extract", args, "ocr_status", timeout_s=float(timeout_s), is_done=_is_extract_done)
    if res["ok"]:
        return _extract_result(res["data"], KAFKA)
    if local_fallback and _s.is_unavailable(res):
        try:
            return _extract_result(_readers_result(src, mode), "local")
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{res['error']}; local fallback failed: {type(exc).__name__}: {exc}"[:400], "via": "local",
                    "kind": res["kind"], "hub_error": res["error"]}
    out = _s.public_error(res, KAFKA)
    if res.get("kind") == "timeout":
        out.update({"job_id": res.get("job_id"), "still_running": True})
    return out


@_s.never_raises(KAFKA)
def ocr_image(path: str, *, lang: str = "es", blocks: bool = False, timeout_s: float = 120.0) -> dict[str, Any]:
    """Text from an image (``ocr_image``): ``{ok, text, blocks, backend, via}``; ``blocks=True`` also returns the recognised lines with their
    boxes (``[{text, box: [[x, y] x4], score}]``). No local fallback: the OCR engine is Kafka's."""
    return _run("ocr_image", _s.clean_args(path=_s.as_path(path), lang=lang, blocks=bool(blocks) or None), timeout_s)


@_s.never_raises(KAFKA)
def ocr_pdf(path: str, *, pages: Optional[str] = None, dpi: int = 200, lang: str = "es", max_pages: int = 40, timeout_s: float = 900.0) -> dict[str, Any]:
    """OCR of a PDF's pages (``ocr_pdf``), without extracting the other text: ``{ok, text, pages: [{page, text}], pages_done, pages_total,
    backend, via}``. ``pages``: ``"1-5,9"`` (default: the first ``max_pages``). Runs as a job: still running at ``timeout_s`` gives
    ``kind: "timeout"`` with ``job_id`` (:func:`ocr_status`)."""
    args = _s.clean_args(path=_s.as_path(path), pages=pages, dpi=int(dpi), lang=lang, max_pages=int(max_pages))
    res = _s.run_job(KAFKA, "ocr_pdf", args, "ocr_status", timeout_s=float(timeout_s))
    if res["ok"]:
        return _ok(res["data"])
    out = _s.public_error(res, KAFKA)
    if res.get("kind") == "timeout":
        out.update({"job_id": res.get("job_id"), "still_running": True})
    return out


@_s.never_raises(KAFKA)
def ocr_status(job_id: str = "", wait_s: float = 0, timeout_s: float = 30.0) -> dict[str, Any]:
    """Without ``job_id``: the OCR engine of the machine, ``{ok, available, backend, languages, ...}``. With one: that job
    (:func:`extract` / :func:`ocr_pdf` left it running), its final result when done; ``wait_s`` (0 to 150) blocks until it finishes."""
    wait = _s.clamp_wait(wait_s)
    args = _s.clean_args(job_id=job_id, wait_s=wait if job_id else None)
    res = _s.call_tool(KAFKA, "ocr_status", args, timeout_s=max(float(timeout_s), wait + _s.HUB_MARGIN_S))
    return _ok(res["data"]) if res["ok"] else _s.public_error(res, KAFKA)
