"""What a file really is, from its first bytes (and, for ZIP containers, its member names) — not from its name.

Standard library only. The Node twin is ``js/hoard-commons/docs.js`` (``sniff``, ``mimeFor``), checked against
``tests/vectors/docs_sniff.json``.

:func:`sniff` returns ``Sniffed(kind, mime, ext)``:

* ``kind`` is the family an app dispatches on: ``pdf``, ``image``, ``docx``, ``xlsx``, ``pptx``, ``odt``,
  ``ods``, ``odp``, ``epub``, ``rtf``, ``ole`` (legacy .doc/.xls/.ppt), ``audio``, ``video``, ``html``,
  ``json``, ``csv``, ``eml``, ``text``, ``zip`` (a plain zip), ``archive`` (7z, rar, gzip, tar, bz2, xz),
  ``sqlite``, ``parquet`` and ``unknown``.
* ``ext`` is the canonical extension without the dot (``jpg``, ``webp``, ``heic``, ``mp3`` …); when the content
  gives no answer it is the extension of ``name``.
* ``mime`` is the media type.

Magic bytes win over the name (a ``.pdf`` that is really a PNG is ``image/png``). Office and EPUB files are all
ZIP: the member names tell them apart (``word/`` → docx, ``xl/`` → xlsx, ``ppt/`` → pptx, ``mimetype`` →
epub/odt/ods/odp). Text is recognised by being decodable (UTF-8, UTF-16 with BOM, or a known text extension
with no NUL bytes).

Replaces: Kafka ``readers.sniff``, Faustus ``creator/ingest`` / ``cli_model`` / ``upload_handler`` tables,
Galton ``services.py`` / ``api/runs.py`` inline checks, Gepetto ``card_pose_vision``, Hypatia's ``PK`` test.
"""

from __future__ import annotations

import io
import json
import posixpath
import re
import zipfile
from dataclasses import dataclass
from typing import Optional

__all__ = ["Sniffed", "sniff", "mime_for", "ext_of", "MIME", "zip_member_names"]


@dataclass(frozen=True)
class Sniffed:
    kind: str
    mime: str
    ext: str


MIME: dict[str, str] = {
    "pdf": "application/pdf", "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif",
    "webp": "image/webp", "bmp": "image/bmp", "tif": "image/tiff", "tiff": "image/tiff", "heic": "image/heic",
    "heif": "image/heif", "avif": "image/avif", "svg": "image/svg+xml", "ico": "image/x-icon",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "odt": "application/vnd.oasis.opendocument.text", "ods": "application/vnd.oasis.opendocument.spreadsheet",
    "odp": "application/vnd.oasis.opendocument.presentation", "epub": "application/epub+zip",
    "rtf": "application/rtf", "doc": "application/msword", "xls": "application/vnd.ms-excel",
    "ppt": "application/vnd.ms-powerpoint", "msg": "application/vnd.ms-outlook",
    "mp3": "audio/mpeg", "wav": "audio/wav", "flac": "audio/flac", "ogg": "audio/ogg", "opus": "audio/ogg",
    "m4a": "audio/mp4", "aac": "audio/aac", "mid": "audio/midi", "weba": "audio/webm",
    "mp4": "video/mp4", "m4v": "video/mp4", "mov": "video/quicktime", "webm": "video/webm",
    "mkv": "video/x-matroska", "avi": "video/x-msvideo", "3gp": "video/3gpp", "ogv": "video/ogg",
    "html": "text/html", "htm": "text/html", "xhtml": "application/xhtml+xml", "json": "application/json",
    "csv": "text/csv", "tsv": "text/tab-separated-values", "txt": "text/plain", "text": "text/plain",
    "md": "text/markdown", "markdown": "text/markdown", "xml": "application/xml", "yaml": "text/yaml",
    "yml": "text/yaml", "log": "text/plain", "css": "text/css", "js": "text/javascript", "py": "text/x-python",
    "eml": "message/rfc822", "ics": "text/calendar", "vcf": "text/vcard",
    "zip": "application/zip", "7z": "application/x-7z-compressed", "rar": "application/vnd.rar",
    "gz": "application/gzip", "tgz": "application/gzip", "tar": "application/x-tar", "bz2": "application/x-bzip2",
    "xz": "application/x-xz", "sqlite": "application/vnd.sqlite3", "db": "application/vnd.sqlite3",
    "parquet": "application/vnd.apache.parquet",
}
OCTET = "application/octet-stream"

_TEXT_EXT = {"txt", "text", "md", "markdown", "csv", "tsv", "json", "html", "htm", "xhtml", "xml", "yaml", "yml", "log",
             "css", "js", "py", "eml", "ics", "vcf", "svg", "ini", "toml", "cfg", "rst", "tex", "srt", "vtt", "sql", "sh", "ts"}
_EXT_KIND = {"pdf": "pdf", "docx": "docx", "xlsx": "xlsx", "pptx": "pptx", "odt": "odt", "ods": "ods", "odp": "odp",
             "epub": "epub", "rtf": "rtf", "doc": "ole", "xls": "ole", "ppt": "ole", "msg": "ole", "html": "html",
             "htm": "html", "xhtml": "html", "json": "json", "csv": "csv", "tsv": "csv", "eml": "eml", "zip": "zip",
             "svg": "image", "sqlite": "sqlite", "parquet": "parquet"}
for _e in ("png", "jpg", "jpeg", "gif", "webp", "bmp", "tif", "tiff", "heic", "heif", "avif", "ico"):
    _EXT_KIND[_e] = "image"
for _e in ("mp3", "wav", "flac", "ogg", "opus", "m4a", "aac", "mid", "weba"):
    _EXT_KIND[_e] = "audio"
for _e in ("mp4", "m4v", "mov", "webm", "mkv", "avi", "3gp", "ogv"):
    _EXT_KIND[_e] = "video"
for _e in ("7z", "rar", "gz", "tgz", "tar", "bz2", "xz"):
    _EXT_KIND[_e] = "archive"
for _e in _TEXT_EXT:
    _EXT_KIND.setdefault(_e, "text")

_ODF = {
    "application/vnd.oasis.opendocument.text": "odt",
    "application/vnd.oasis.opendocument.spreadsheet": "ods",
    "application/vnd.oasis.opendocument.presentation": "odp",
    "application/vnd.oasis.opendocument.graphics": "odg",
}
_HEIC_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"hevm", b"hevs"}
_MP4_AUDIO = {b"M4A ", b"M4B ", b"M4P "}


def ext_of(name: object) -> str:
    """Lowercase extension of ``name`` without the dot (``tar.gz`` counts as ``gz``); at most 8 characters."""
    base = posixpath.basename(str(name or "").replace("\\", "/"))
    if "." not in base.strip("."):
        return ""
    return base.rsplit(".", 1)[-1].lower()[:8]


def mime_for(ext: object) -> str:
    """Media type for an extension (``".PDF"``, ``"pdf"`` or a file name); ``application/octet-stream`` if unknown."""
    e = str(ext or "")
    if "." in e.strip("."):
        e = e.rsplit(".", 1)[-1]
    return MIME.get(e.strip(". ").lower(), OCTET)


def _mk(kind: str, ext: str) -> Sniffed:
    return Sniffed(kind, MIME.get(ext, OCTET), ext)


# ---- ZIP -------------------------------------------------------------------------------------------

def zip_member_names(data: bytes, *, limit: int = 100_000) -> Optional[list[str]]:
    """Member names of a ZIP held in memory (central directory; falls back to scanning local headers when the
    directory is missing or damaged). ``None`` when it is not a ZIP at all."""
    if data[:2] != b"PK":
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            return z.namelist()[:limit]
    except (zipfile.BadZipFile, ValueError, NotImplementedError, EOFError, OSError):
        pass
    names: list[str] = []
    pos = 0
    while len(names) < 64:
        pos = data.find(b"PK\x03\x04", pos, 8 * 1024 * 1024)
        if pos < 0 or pos + 30 > len(data):
            break
        n = int.from_bytes(data[pos + 26:pos + 28], "little")
        raw = data[pos + 30:pos + 30 + n]
        if len(raw) == n and n:
            names.append(raw.decode("utf-8", errors="replace"))
        pos += 4
    return names or None


def _zip_mimetype(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            if "mimetype" in z.namelist():
                return z.open("mimetype").read(256).decode("ascii", errors="ignore").strip()
    except Exception:  # noqa: BLE001 - damaged zip: the names decide
        pass
    m = re.match(rb"PK\x03\x04.{22}(.{2})(.{2})mimetype(.{0,128})", data[:400], re.DOTALL)
    return m.group(3).decode("ascii", errors="ignore").strip() if m else ""


def _classify_zip(data: bytes, ext: str) -> Sniffed:
    names = zip_member_names(data)
    if names is None:
        return _mk("zip", "zip")
    lower = [n.lower() for n in names]
    has = lambda prefix: any(n.startswith(prefix) for n in lower)  # noqa: E731
    if "mimetype" in lower:
        mt = _zip_mimetype(data)
        if mt.startswith("application/epub+zip"):
            return _mk("epub", "epub")
        for key, e in _ODF.items():
            if mt.startswith(key):
                return _mk(e, e) if e != "odg" else Sniffed("zip", key, "odg")
    if has("word/") and ("[content_types].xml" in lower or "word/document.xml" in lower):
        return _mk("docx", "docx")
    if has("xl/"):
        return _mk("xlsx", "xlsx")
    if has("ppt/"):
        return _mk("pptx", "pptx")
    if "meta-inf/container.xml" in lower and ("mimetype" in lower or has("oebps/") or any(n.endswith(".opf") for n in lower)):
        return _mk("epub", "epub")
    if "content.xml" in lower and "meta-inf/manifest.xml" in lower:
        e = ext if ext in ("odt", "ods", "odp") else "odt"
        return _mk(e, e)
    return _mk("zip", "zip")


# ---- text ------------------------------------------------------------------------------------------

def _text_info(sample: bytes) -> Optional[str]:
    """``"utf-8"``, ``"utf-16"``, ``"latin"`` (no NUL, mostly printable) or ``None`` for binary."""
    if sample[:3] == b"\xef\xbb\xbf":
        return "utf-8"
    if sample[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return "utf-16"
    if not sample:
        return "utf-8"
    if b"\x00" in sample:
        return None
    ctrl = sum(1 for b in sample if b < 32 and b not in (9, 10, 12, 13, 27))
    if ctrl > max(2, len(sample) // 50):
        return None
    try:
        import codecs
        codecs.getincrementaldecoder("utf-8")().decode(sample, final=False)
        return "utf-8"
    except UnicodeDecodeError:
        return "latin"


def _decode_head(data: bytes, enc: str, n: int = 8192) -> str:
    head = data[:n]
    if enc == "utf-16":
        return head.decode("utf-16", errors="ignore")
    if enc == "utf-8":
        return head.decode("utf-8-sig", errors="ignore")
    return head.decode("latin-1")


def _classify_text(data: bytes, ext: str, enc: str) -> Sniffed:
    head = _decode_head(data, enc)
    low = head.lstrip().lower()
    if low.startswith("<!doctype html") or low.startswith("<html") or (low.startswith("<?xml") and "<html" in low[:400]):
        return _mk("html", "html")
    if low.startswith("<svg") or (low.startswith(("<?xml", "<!doctype svg")) and "<svg" in low[:600]):
        return _mk("image", "svg")
    if re.match(r"(?:received|return-path|message-id|mime-version|delivered-to|x-[a-z-]+):", low) or \
            (low.startswith("from ") and "\nsubject:" in low[:2000]) or \
            (ext == "eml" and re.match(r"(?:from|to|cc|bcc|subject|date|reply-to|sender):", low)):
        return _mk("eml", "eml")
    stripped = head.lstrip()
    if ext == "json" or (stripped[:1] in ("{", "[") and ext not in _TEXT_EXT - {"json"}):
        body = data if len(data) <= 2_000_000 else b""
        if body:
            try:
                json.loads(_decode_head(body, enc, len(body)))
                return _mk("json", "json")
            except ValueError:
                pass
        elif ext == "json":
            return _mk("json", "json")
    if ext in ("csv", "tsv"):
        return _mk("csv", ext)
    if ext in _TEXT_EXT or ext in ("md",):
        return _mk("html" if ext in ("html", "htm", "xhtml") else "text", ext if ext in MIME else "txt")
    lines = [ln for ln in head.splitlines() if ln.strip()][:8]
    if len(lines) >= 3:
        for sep in (",", ";", "\t"):
            counts = {ln.count(sep) for ln in lines}
            if len(counts) == 1 and next(iter(counts)) >= 2:
                return _mk("csv", "tsv" if sep == "\t" else "csv")
    return _mk("text", "txt")


# ---- entry point -----------------------------------------------------------------------------------

def sniff(name: object, data: bytes) -> Sniffed:
    """Identify a file from ``data`` (the whole file, or at least its first few KB; a ZIP needs all of it) and
    ``name`` (only a hint). Never raises."""
    ext = ext_of(name)
    d = bytes(data or b"")
    head = d[:16]
    n = len(d)

    if b"%PDF-" in d[:1024]:
        return _mk("pdf", "pdf")
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return _mk("image", "png")
    if head[:3] == b"\xff\xd8\xff":
        return _mk("image", "jpg")
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return _mk("image", "gif")
    if head[:4] == b"RIFF" and n >= 12:
        tag = d[8:12]
        if tag == b"WEBP":
            return _mk("image", "webp")
        if tag == b"WAVE":
            return _mk("audio", "wav")
        if tag == b"AVI ":
            return _mk("video", "avi")
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return _mk("image", "tif")
    if head[:2] == b"BM" and n >= 30 and int.from_bytes(d[14:18], "little") in (12, 40, 52, 56, 64, 108, 124):
        return _mk("image", "bmp")
    if head[:4] == b"\x00\x00\x01\x00" and n >= 22 and d[4] and d[5] == 0:
        return _mk("image", "ico")
    if n >= 12 and d[4:8] == b"ftyp":
        brand = d[8:12]
        if brand in _HEIC_BRANDS:
            return _mk("image", "heic")
        if brand in (b"mif1", b"msf1"):
            compat = d[16:min(n, 8 + int.from_bytes(d[:4], "big"))]
            return _mk("image", "avif" if b"avif" in compat and b"heic" not in compat else "heic")
        if brand in (b"avif", b"avis"):
            return _mk("image", "avif")
        if brand in _MP4_AUDIO:
            return _mk("audio", "m4a")
        if brand == b"qt  ":
            return _mk("video", "mov")
        if brand[:3] == b"3gp" or brand[:3] == b"3g2":
            return _mk("video", "3gp")
        return _mk("video", "mp4")
    if head[:4] == b"\x1aE\xdf\xa3":
        return _mk("video", "webm" if b"webm" in d[:64] else "mkv")
    if head[:4] == b"fLaC":
        return _mk("audio", "flac")
    if head[:4] == b"OggS":
        if b"OpusHead" in d[:64]:
            return _mk("audio", "opus")
        if b"theora" in d[:96]:
            return _mk("video", "ogv")
        return _mk("audio", "ogg")
    if head[:3] == b"ID3":
        return _mk("audio", "mp3")
    if head[:4] == b"MThd":
        return _mk("audio", "mid")
    if n >= 2 and d[0] == 0xFF and d[1] & 0xE0 == 0xE0 and ext in ("mp3", "mp2", "aac") and (d[1] >> 3) & 3 != 1 and (d[1] >> 1) & 3 != 0:
        return _mk("audio", "aac" if ext == "aac" else "mp3")
    if head[:6] == b"7z\xbc\xaf\x27\x1c":
        return _mk("archive", "7z")
    if head[:6] == b"Rar!\x1a\x07":
        return _mk("archive", "rar")
    if head[:2] == b"\x1f\x8b":
        return _mk("archive", "gz")
    if head[:3] == b"BZh":
        return _mk("archive", "bz2")
    if head[:6] == b"\xfd7zXZ\x00":
        return _mk("archive", "xz")
    if n >= 262 and d[257:262] == b"ustar":
        return _mk("archive", "tar")
    if head[:16] == b"SQLite format 3\x00":
        return _mk("sqlite", "sqlite")
    if head[:4] == b"PAR1" and n >= 8 and d[-4:] == b"PAR1":
        return _mk("parquet", "parquet")
    if head[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        e = ext if ext in ("doc", "xls", "ppt", "msg") else "doc"
        return Sniffed("ole", MIME.get(e, OCTET), e)
    if head[:5] == b"{\\rtf":
        return _mk("rtf", "rtf")
    if head[:2] == b"PK" and head[2:4] in (b"\x03\x04", b"\x05\x06", b"\x07\x08"):
        return _classify_zip(d, ext)

    if n == 0:
        kind = _EXT_KIND.get(ext, "unknown")
        return Sniffed(kind, MIME.get(ext, OCTET), ext or "bin")
    enc = _text_info(d[:8192])
    if enc is not None and (enc != "latin" or ext in _TEXT_EXT):
        return _classify_text(d, ext, enc)
    return Sniffed("unknown", OCTET, ext or "bin")
