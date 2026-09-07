"""Helpers for the optional markitdown document-extraction dependency.

markitdown (MIT, Microsoft) converts Office/EPUB documents to Markdown, which is
more token-efficient and model-legible than a raw text dump. It is **optional**:
install with `pip install -r requirements-optional.txt`. When absent, callers
degrade gracefully (chat shows a hint; the RAG indexer skips the file) — the MIT
core never hard-depends on it. Mirrors the optional-dependency pattern in
`src/pdf_runtime.py`.
"""

import logging
import os

logger = logging.getLogger(__name__)
MAX_NATIVE_DOCX_XML_BYTES = 16 * 1024 * 1024

MARKITDOWN_MISSING = (
    "Office/EPUB document extraction requires markitdown. Install optional "
    "dependencies with `pip install -r requirements-optional.txt`."
)

# Formats routed through markitdown. PDFs stay on pypdf (src/document_processor
# and src/personal_docs); plain text/code/csv/json/markdown/html stay on the
# cheaper built-in text path. These are the formats currently dropped entirely.
MARKITDOWN_EXTS = frozenset({".docx", ".pptx", ".xlsx", ".xls", ".epub"})


def is_markitdown_format(path: str) -> bool:
    """True if the file extension is one we route through markitdown."""
    if not isinstance(path, str):
        return False
    return os.path.splitext(path)[1].lower() in MARKITDOWN_EXTS


def load_markitdown():
    """Return the MarkItDown class, or raise a user-facing setup hint."""
    try:
        from markitdown import MarkItDown  # optional dependency
    except ImportError as exc:
        raise RuntimeError(MARKITDOWN_MISSING) from exc
    return MarkItDown


def _extract_docx_native(path: str) -> str | None:
    """Pure-Python .docx text extractor — no external deps.

    A .docx file is just a zip of XML. The body prose lives in <w:t> runs
    inside <w:p> paragraphs. Iterating with ElementTree (rather than
    re.findall) keeps paragraph breaks intact and lets the XML parser handle
    namespaces + entity unescaping. Loses tables, footnotes, images and
    list bullets. Standard heading styles are retained as Markdown headings.
    Bound the decompressed XML before parsing so an attachment cannot expand
    an arbitrarily large ZIP member into the application's memory.
    """
    import zipfile
    import xml.etree.ElementTree as ET

    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    try:
        with zipfile.ZipFile(path) as z:
            member = z.getinfo("word/document.xml")
            if member.file_size > MAX_NATIVE_DOCX_XML_BYTES:
                logger.warning("DOCX document XML exceeds the native extraction budget")
                return None
            with z.open(member) as stream:
                xml_bytes = stream.read(MAX_NATIVE_DOCX_XML_BYTES + 1)
            if len(xml_bytes) > MAX_NATIVE_DOCX_XML_BYTES:
                return None
    except (zipfile.BadZipFile, KeyError, OSError):
        return None
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return None
    paragraphs: list[str] = []
    for para in root.iter(f"{ns}p"):
        runs = [t.text or "" for t in para.iter(f"{ns}t")]
        line = "".join(runs).strip()
        if line:
            style = para.find(f"{ns}pPr/{ns}pStyle")
            style_id = (style.get(f"{ns}val", "") if style is not None else "").lower()
            if style_id.startswith("heading") and style_id[7:] in {str(n) for n in range(1, 10)}:
                line = "#" * min(int(style_id[7:]), 6) + " " + line
            paragraphs.append(line)
    return "\n\n".join(paragraphs) if paragraphs else None


def convert_to_markdown(path: str) -> str | None:
    """Convert a document to Markdown text via markitdown.

    Returns the extracted Markdown, or ``None`` if markitdown is unavailable or
    the conversion fails — callers degrade gracefully rather than erroring.

    Fallback: when markitdown isn't installed and the file is a .docx, run
    the bundled pure-Python extractor so the most common case (Word docs)
    works out of the box. Other Office/EPUB formats still need markitdown.
    """
    try:
        markitdown_cls = load_markitdown()
    except RuntimeError:
        if isinstance(path, str) and path.lower().endswith(".docx"):
            text = _extract_docx_native(path)
            if text:
                logger.info(
                    "markitdown not installed — used native .docx extractor for %s",
                    path,
                )
                return text
        logger.warning("markitdown not installed; cannot extract %s", path)
        return None
    try:
        result = markitdown_cls().convert(path)
        text = getattr(result, "text_content", None)
        if text is None:
            text = getattr(result, "markdown", None)
        return text
    except Exception as e:
        logger.warning("markitdown failed to convert %s: %s", path, e)
        return None
