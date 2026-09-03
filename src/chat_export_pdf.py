"""PDF export for chat transcripts, rendered with reportlab's Platypus.

The renderer walks the shared block model from ``src/chat_export_model.py``
(``Transcript`` -> ``ExportMessage`` -> ``Block`` -> ``Span``) and emits a
flowable story, so a transcript reads the same here as it does in Markdown,
HTML or DOCX.

Why reportlab
-------------
It is pure Python and BSD-licensed, so it installs from a wheel on every
platform we ship to.  WeasyPrint would need Pango/cairo/gdk-pixbuf as native
libraries (a non-starter for the Windows portable build) and headless Chromium
is ~150 MB plus a second process.

Why Platypus and not the canvas
-------------------------------
``SimpleDocTemplate`` + flowables gives us pagination, paragraph/table
splitting and "keep with next" for free.  Hand-driving ``canvas`` means
re-implementing all of that, badly.

The two traps this module exists to handle
------------------------------------------
1. **Markup injection.**  A Platypus ``Paragraph`` parses a small HTML dialect,
   so every piece of chat text has to be escaped before it goes in.  Otherwise
   a message that merely mentions ``<b>`` silently turns bold and one that
   contains ``a < b`` aborts the whole export with a parse error.  All text
   goes through :func:`_markup`; nothing is ever interpolated raw.
2. **Unicode.**  reportlab's default Helvetica is a Type 1 font that stops at
   Latin-1: Greek, Cyrillic, CJK and emoji come out as blanks.  We register a
   TrueType font at runtime (:func:`_font_kit`), route characters the main font
   cannot draw to a fallback font, and substitute the rest.  Accents and "ñ"
   must always render, and an emoji must never abort an export.  Note the
   webfonts in ``static/fonts`` are .woff2, which reportlab cannot read - hence
   the filesystem search plus reportlab's own bundled Vera as the floor.
"""

from __future__ import annotations

import html
import io
import logging
import os
import re
import sys
import threading
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.chat_export import is_document
from src.chat_export_model import (
    Block,
    ExportMessage,
    ExportUnavailable,
    Span,
    ToolCall,
    Transcript,
)

logger = logging.getLogger(__name__)

REPORTLAB_MISSING = (
    "PDF export requires the 'reportlab' package. Install it with "
    "`pip install reportlab` (or `pip install -r requirements.txt`)."
)

# Every user-visible string lives here so the export can be translated in one
# place. The rest of the app speaks English (see routes/session_routes.py),
# so these do too.
LABELS = {
    "user": "User",
    "assistant": "Assistant",
    "system": "System",
    "tool": "Tool",
    "model": "Model",
    "exported": "Exported",
    "messages": "messages",
    "message": "message",
    "session": "Session",
    "project": "Project",
    "workspace": "Workspace",
    "attachments": "Attachments",
    "empty": "(this conversation has no messages)",
    "page": "Page %d of %d",
    "tool_call": "tool call",
    "arguments": "arguments",
    "result": "result",
    "truncated": "... [truncated]",
    "image": "[image]",
}

# --- page geometry -----------------------------------------------------------
PAGE_MARGIN_X = 50.0          # ~17.6 mm
PAGE_MARGIN_TOP = 44.0
PAGE_MARGIN_BOTTOM = 46.0
MESSAGE_INDENT = 10.0         # body of a message sits slightly inside the role bar

# --- palette -----------------------------------------------------------------
COLOR_TEXT = "#111827"
COLOR_MUTED = "#6B7280"
COLOR_RULE = "#D1D5DB"
COLOR_LINK = "#1D4ED8"
COLOR_CODE_BG = "#F6F8FA"
COLOR_CODE_BORDER = "#E3E8EF"
COLOR_TABLE_HEAD = "#F3F4F6"
COLOR_QUOTE_BAR = "#9CA3AF"

ROLE_STYLE = {
    # role: (text colour, background tint)
    "user": ("#1D4ED8", "#EFF6FF"),
    "assistant": ("#047857", "#ECFDF5"),
    "system": ("#4B5563", "#F3F4F6"),
    "tool": ("#92400E", "#FFFBEB"),
}

# Tool arguments/results are diagnostics, not prose: keep them from turning a
# transcript into a hundred pages of JSON.
TOOL_TEXT_LIMIT = 2000

# Link schemes we will turn into real PDF link annotations. Anything else
# (javascript:, data:, or a bare relative path) is rendered as plain text:
# a bare path would be read by reportlab as an *internal* destination name and
# raise "undefined destination" when the document is saved.
SAFE_LINK_SCHEMES = ("http://", "https://", "mailto:", "ftp://", "ftps://", "tel:")

# Characters no available font can draw are replaced with this rather than
# silently dropped or, worse, raising.
UNSUPPORTED_CHAR = "?"

# Characters above the BMP (most emoji) are substituted rather than drawn, even
# when the chosen font has a glyph for them. reportlab builds the /ToUnicode
# CMap with ``"<%02X> <%04X>" % (i, v)`` (pdfbase/ttfonts.py, makeToUnicodeCMap),
# which emits five hex digits for a codepoint above U+FFFF instead of a UTF-16
# surrogate pair. That is a malformed CMap: it breaks copy/paste and search for
# *every* string in that font, and makes pypdf raise "Odd-length string" on the
# page. Substituting keeps the text layer of the PDF valid and the result the
# same on every platform, which matters more than drawing the emoji.
MAX_CODEPOINT = 0xFFFF

# Control characters have no glyph anywhere, and a lone surrogate would end up
# in reportlab's /ToUnicode CMap as an invalid UTF-16 code unit.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff]")

_SETUP_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# lazy reportlab import
# ---------------------------------------------------------------------------

_RL: Optional[SimpleNamespace] = None


def _rl() -> SimpleNamespace:
    """Import reportlab on first use.

    Imported lazily (and never at module scope) so that importing this module
    is free when reportlab is not installed, and so the caller gets an
    ``ExportUnavailable`` naming the package instead of an ImportError
    traceback from somewhere deep in the export pipeline.
    """
    global _RL
    if _RL is not None:
        return _RL
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_LEFT
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.pdfgen.canvas import Canvas
        from reportlab.platypus import (
            HRFlowable,
            Indenter,
            ListFlowable,
            ListItem,
            Paragraph,
            SimpleDocTemplate,
            Table,
            TableStyle,
        )
        from reportlab.platypus.xpreformatted import XPreformatted
    except ImportError as exc:  # pragma: no cover - exercised with a patched import
        raise ExportUnavailable(REPORTLAB_MISSING) from exc

    _RL = SimpleNamespace(
        colors=colors,
        TA_LEFT=TA_LEFT,
        A4=A4,
        ParagraphStyle=ParagraphStyle,
        pdfmetrics=pdfmetrics,
        TTFont=TTFont,
        Canvas=Canvas,
        HRFlowable=HRFlowable,
        Indenter=Indenter,
        ListFlowable=ListFlowable,
        ListItem=ListItem,
        Paragraph=Paragraph,
        SimpleDocTemplate=SimpleDocTemplate,
        Table=Table,
        TableStyle=TableStyle,
        XPreformatted=XPreformatted,
    )
    return _RL


# ---------------------------------------------------------------------------
# fonts
# ---------------------------------------------------------------------------

# (alias, (regular, bold, italic, bold-italic)) in preference order. Filenames
# are matched case-insensitively against everything found under the platform
# font directories, so the same table covers Linux, macOS and Windows.
BODY_FAMILIES: Sequence[Tuple[str, Tuple[str, ...]]] = (
    ("FaustusSans-DejaVu", ("dejavusans.ttf", "dejavusans-bold.ttf",
                            "dejavusans-oblique.ttf", "dejavusans-boldoblique.ttf")),
    ("FaustusSans-Arial", ("arial.ttf", "arialbd.ttf", "ariali.ttf", "arialbi.ttf")),
    ("FaustusSans-Segoe", ("segoeui.ttf", "segoeuib.ttf", "segoeuii.ttf", "segoeuiz.ttf")),
    ("FaustusSans-Helvetica", ("helvetica.ttf", "helvetica-bold.ttf", "")),
    ("FaustusSans-Liberation", ("liberationsans-regular.ttf", "liberationsans-bold.ttf",
                                "liberationsans-italic.ttf", "liberationsans-bolditalic.ttf")),
    ("FaustusSans-Carlito", ("carlito-regular.ttf", "carlito-bold.ttf",
                             "carlito-italic.ttf", "carlito-bolditalic.ttf")),
    # Bitstream Vera ships *inside* reportlab, so this row always resolves.
    # It covers Latin-1 - which is what Spanish actually needs - plus the
    # common punctuation and the bullet.
    ("FaustusSans-Vera", ("vera.ttf", "verabd.ttf", "verait.ttf", "verabi.ttf")),
)

MONO_FAMILIES: Sequence[Tuple[str, Tuple[str, ...]]] = (
    ("FaustusMono-DejaVu", ("dejavusansmono.ttf", "dejavusansmono-bold.ttf",
                            "dejavusansmono-oblique.ttf", "dejavusansmono-boldoblique.ttf")),
    ("FaustusMono-Consolas", ("consola.ttf", "consolab.ttf", "consolai.ttf", "consolaz.ttf")),
    ("FaustusMono-Liberation", ("liberationmono-regular.ttf", "liberationmono-bold.ttf",
                                "liberationmono-italic.ttf", "liberationmono-bolditalic.ttf")),
    ("FaustusMono-CourierNew", ("cour.ttf", "courbd.ttf", "couri.ttf", "courbi.ttf")),
    ("FaustusMono-Menlo", ("menlo-regular.ttf", "menlo-bold.ttf")),
)

# Single-face fonts registered only when a character turns up that the main
# font cannot draw (CJK, emoji, symbols). Loading these costs a few ms each, so
# it happens lazily and at most once per process.
FALLBACK_FONTS: Sequence[Tuple[str, str]] = (
    ("FaustusFB-DejaVu", "dejavusans.ttf"),
    ("FaustusFB-ArialUnicode", "arialuni.ttf"),
    ("FaustusFB-SegoeSymbol", "seguisym.ttf"),
    ("FaustusFB-SegoeEmoji", "seguiemj.ttf"),
    ("FaustusFB-Symbola", "symbola.ttf"),
    ("FaustusFB-NotoSans", "notosans-regular.ttf"),
    ("FaustusFB-Unifont", "unifont.ttf"),
    ("FaustusFB-IPAGothic", "ipag.ttf"),
    ("FaustusFB-MSGothic", "msgothic.ttc"),
    ("FaustusFB-Meiryo", "meiryo.ttc"),
    ("FaustusFB-SimSun", "simsun.ttc"),
    ("FaustusFB-Malgun", "malgun.ttf"),
    ("FaustusFB-YuGothic", "yugothm.ttc"),
    ("FaustusFB-AppleGothic", "applegothic.ttf"),
)

BUILTIN_SANS = "Helvetica"
BUILTIN_MONO = "Courier"


@dataclass
class _FontKit:
    """Which fonts we ended up with, and what each of them can draw."""

    body: str
    mono: str
    coverage: Dict[str, Optional[Dict[int, int]]] = field(default_factory=dict)
    fallbacks: List[str] = field(default_factory=list)
    bullet: str = "\u2022"
    _fallbacks_loaded: bool = False

    def covers(self, font: str, ch: str) -> bool:
        cov = self.coverage.get(font, None)
        if cov is None:
            # A built-in Type 1 face: WinAnsi, i.e. Latin-1 and no further.
            try:
                ch.encode("latin-1")
                return True
            except UnicodeEncodeError:
                return False
        return ord(ch) in cov


_FONT_KIT: Optional[_FontKit] = None
_FONT_INDEX: Optional[Dict[str, str]] = None


def _font_dirs() -> List[str]:
    """Directories to search for TrueType faces, most specific first."""
    dirs: List[str] = []
    if os.name == "nt":
        windir = os.environ.get("WINDIR") or r"C:\Windows"
        dirs.append(os.path.join(windir, "Fonts"))
        local = os.environ.get("LOCALAPPDATA")
        if local:
            dirs.append(os.path.join(local, "Microsoft", "Windows", "Fonts"))
    elif sys.platform == "darwin":
        dirs += [
            "/System/Library/Fonts",
            "/System/Library/Fonts/Supplemental",
            "/Library/Fonts",
            os.path.expanduser("~/Library/Fonts"),
        ]
    else:
        dirs += [
            "/usr/share/fonts",
            "/usr/local/share/fonts",
            os.path.expanduser("~/.fonts"),
            os.path.expanduser("~/.local/share/fonts"),
        ]
    try:
        import reportlab

        dirs.append(os.path.join(os.path.dirname(reportlab.__file__), "fonts"))
    except Exception:  # pragma: no cover - reportlab is present by the time we get here
        pass
    return dirs


def _font_index() -> Dict[str, str]:
    """Map lowercase font filename -> full path, built once per process."""
    global _FONT_INDEX
    if _FONT_INDEX is not None:
        return _FONT_INDEX
    index: Dict[str, str] = {}
    seen = 0
    for directory in _font_dirs():
        if not directory or not os.path.isdir(directory):
            continue
        try:
            for root, _dirs, files in os.walk(directory):
                for name in files:
                    low = name.lower()
                    if low.endswith((".ttf", ".ttc")):
                        index.setdefault(low, os.path.join(root, name))
                seen += len(files)
                if seen > 40000:  # pathological font tree; we have enough
                    break
        except OSError:
            continue
    _FONT_INDEX = index
    return index


def _register_ttf(alias: str, path: str) -> bool:
    """Register one TTF/TTC face. Returns False on any reportlab complaint."""
    rl = _rl()
    try:
        if path.lower().endswith(".ttc"):
            rl.pdfmetrics.registerFont(rl.TTFont(alias, path, subfontIndex=0))
        else:
            rl.pdfmetrics.registerFont(rl.TTFont(alias, path))
        return True
    except Exception as exc:  # broken file, CFF outlines, unsupported table...
        logger.debug("chat_export_pdf: cannot use font %s (%s)", path, exc)
        return False


def _register_family(alias: str, filenames: Sequence[str]) -> Optional[str]:
    """Register a 1-4 face family; return the family name, or None."""
    rl = _rl()
    index = _font_index()
    files = list(filenames) + [""] * (4 - len(filenames))
    regular = index.get((files[0] or "").lower())
    if not regular or not _register_ttf(alias, regular):
        return None
    faces = {"normal": alias}
    for suffix, key, filename in (
        ("-Bold", "bold", files[1]),
        ("-Italic", "italic", files[2]),
        ("-BoldItalic", "boldItalic", files[3]),
    ):
        path = index.get((filename or "").lower())
        if path and _register_ttf(alias + suffix, path):
            faces[key] = alias + suffix
        else:
            # Missing face: point at the regular so <b>/<i> still resolve to a
            # real font rather than silently reverting to Helvetica.
            faces[key] = faces.get("bold" if key == "boldItalic" else "normal", alias)
    rl.pdfmetrics.registerFontFamily(
        alias,
        normal=faces["normal"],
        bold=faces["bold"],
        italic=faces["italic"],
        boldItalic=faces["boldItalic"],
    )
    return alias


def _coverage_of(font: str) -> Optional[Dict[int, int]]:
    """Codepoint -> glyph map for a TTF, or None for a built-in Type 1 font."""
    rl = _rl()
    try:
        face = rl.pdfmetrics.getFont(font).face
        return getattr(face, "charToGlyph", None)
    except Exception:
        return None


def _font_kit() -> _FontKit:
    """Pick and register the body + mono fonts once per process."""
    global _FONT_KIT
    if _FONT_KIT is not None:
        return _FONT_KIT
    with _SETUP_LOCK:
        if _FONT_KIT is not None:  # pragma: no cover - lost the race
            return _FONT_KIT
        body = None
        for alias, files in BODY_FAMILIES:
            body = _register_family(alias, files)
            if body:
                break
        mono = None
        for alias, files in MONO_FAMILIES:
            mono = _register_family(alias, files)
            if mono:
                break
        if not body:
            logger.warning(
                "chat_export_pdf: no TrueType face found, falling back to "
                "Helvetica - characters outside Latin-1 will be substituted"
            )
            body = BUILTIN_SANS
        if not mono:
            mono = BUILTIN_MONO
        kit = _FontKit(body=body, mono=mono)
        kit.coverage[body] = _coverage_of(body) if body != BUILTIN_SANS else None
        kit.coverage[mono] = _coverage_of(mono) if mono != BUILTIN_MONO else None
        kit.bullet = "\u2022" if kit.covers(body, "\u2022") else "-"
        _FONT_KIT = kit
    return _FONT_KIT


def _load_fallbacks(kit: _FontKit) -> None:
    """Register the extra-coverage fonts. Called at most once, on first miss."""
    if kit._fallbacks_loaded:
        return
    with _SETUP_LOCK:
        if kit._fallbacks_loaded:  # pragma: no cover - lost the race
            return
        index = _font_index()
        for alias, filename in FALLBACK_FONTS:
            path = index.get(filename)
            if not path or alias in kit.coverage:
                continue
            if _register_ttf(alias, path):
                cov = _coverage_of(alias)
                if cov:
                    kit.coverage[alias] = cov
                    kit.fallbacks.append(alias)
        kit._fallbacks_loaded = True
        logger.debug("chat_export_pdf: unicode fallback fonts: %s", kit.fallbacks)


def _font_for(kit: _FontKit, primary: str, ch: str) -> Optional[str]:
    """Font that can draw ``ch``: the primary, a fallback, or None."""
    if ord(ch) > MAX_CODEPOINT:
        return None
    if kit.covers(primary, ch):
        return primary
    if ch in "\n\r\t ":
        return primary
    _load_fallbacks(kit)
    for alias in kit.fallbacks:
        if kit.covers(alias, ch):
            return alias
    return None


# ---------------------------------------------------------------------------
# text -> Platypus markup
# ---------------------------------------------------------------------------


def _clean(text: Any) -> str:
    """Coerce to str and drop what no font can draw and no CMap can encode."""
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    return _CONTROL_CHARS.sub("", text)


def _font_runs(kit: _FontKit, text: Any,
               font: Optional[str] = None) -> List[Tuple[str, str]]:
    """Split text into (font, chunk) runs, one per font that can draw it.

    Characters no available font covers are replaced with
    ``UNSUPPORTED_CHAR`` and stay in the primary run - an export must degrade,
    never raise, when someone types an emoji.
    """
    font = font or kit.body
    cleaned = _clean(text)
    if not cleaned:
        return []
    runs: List[Tuple[str, List[str]]] = []
    for ch in cleaned:
        target = _font_for(kit, font, ch)
        if target is None:
            target, ch = font, UNSUPPORTED_CHAR
        if runs and runs[-1][0] == target:
            runs[-1][1].append(ch)
        else:
            runs.append((target, [ch]))
    return [(name, "".join(chars)) for name, chars in runs]


def _markup(kit: _FontKit, text: Any, font: Optional[str] = None) -> str:
    """Escape chat text for a Paragraph and route it to fonts that can draw it.

    This is the single choke point for the reportlab markup trap: every piece
    of transcript text reaches a Paragraph through here, XML-escaped, so a
    literal ``<b>`` in a message stays a literal ``<b>``.
    """
    font = font or kit.body
    out: List[str] = []
    for run_font, chunk in _font_runs(kit, text, font):
        chunk = html.escape(chunk, quote=False)
        if run_font != font:
            chunk = '<font face="%s">%s</font>' % (run_font, chunk)
        out.append(chunk)
    return "".join(out)


def _safe_href(href: Any) -> str:
    """Return an external URL we are willing to link, else ""."""
    url = _clean(href).strip()
    if not url:
        return ""
    return url if url.lower().startswith(SAFE_LINK_SCHEMES) else ""


def _spans_markup(kit: _FontKit, spans: Sequence[Span]) -> str:
    """Render inline spans as Platypus markup."""
    parts: List[str] = []
    for span in spans or ():
        if not isinstance(span, Span):
            parts.append(_markup(kit, span))
            continue
        is_code = bool(span.code)
        text = _markup(kit, span.text, kit.mono if is_code else kit.body)
        if not text:
            continue
        if is_code:
            text = '<font face="%s" backcolor="%s">%s</font>' % (
                kit.mono, COLOR_CODE_BG, text)
        if span.bold:
            text = "<b>%s</b>" % text
        if span.italic:
            text = "<i>%s</i>" % text
        if span.strike:
            text = "<strike>%s</strike>" % text
        href = _safe_href(span.href)
        if href:
            text = '<link href="%s" color="%s">%s</link>' % (
                html.escape(href, quote=True), COLOR_LINK, text)
        parts.append(text)
    return "".join(parts)


def _spans_text(spans: Sequence[Span]) -> str:
    """Plain text of a run of spans (used to size table columns)."""
    return "".join(_clean(getattr(s, "text", s)) for s in spans or ())


def _display_width(text: str) -> int:
    """Column count, counting CJK as double width."""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text)


def _wrap_code(text: str, max_cols: int) -> str:
    """Hard-wrap code so long lines break instead of running off the page.

    Platypus will not break a run of non-space characters inside preformatted
    text, and a minified line or a long URL in a fenced block is exactly the
    case every exporter gets wrong: it either overflows the margin or is
    clipped. We break by *display* column so CJK stays aligned too.
    """
    max_cols = max(20, int(max_cols))
    out: List[str] = []
    for line in _clean(text).replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = line.replace("\t", "    ")
        if _display_width(line) <= max_cols:
            out.append(line)
            continue
        current: List[str] = []
        width = 0
        for ch in line:
            char_width = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
            if width + char_width > max_cols and current:
                out.append("".join(current))
                current, width = [], 0
            current.append(ch)
            width += char_width
        out.append("".join(current))
    return "\n".join(out)


def _truncate(text: str, limit: int) -> str:
    text = _clean(text)
    if len(text) <= limit:
        return text
    return text[:limit] + "\n" + LABELS["truncated"]


def _format_timestamp(raw: Any) -> str:
    """ISO 8601 -> "2026-08-31 20:15"; anything unparseable is passed through."""
    text = _clean(raw).strip()
    if not text:
        return ""
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return text[:40]


# ---------------------------------------------------------------------------
# styles
# ---------------------------------------------------------------------------


def _build_styles(kit: _FontKit) -> Dict[str, Any]:
    rl = _rl()
    hex_color = rl.colors.HexColor
    style = rl.ParagraphStyle

    base = dict(fontName=kit.body, textColor=hex_color(COLOR_TEXT), alignment=rl.TA_LEFT)
    styles: Dict[str, Any] = {}

    styles["title"] = style("fx-title", fontName=kit.body, fontSize=19, leading=23,
                            textColor=hex_color(COLOR_TEXT), spaceAfter=3)
    styles["meta"] = style("fx-meta", fontName=kit.body, fontSize=8.5, leading=12,
                           textColor=hex_color(COLOR_MUTED), spaceAfter=2)
    styles["body"] = style("fx-body", fontSize=9.8, leading=14, spaceAfter=6, **base)
    styles["note"] = style("fx-note", fontName=kit.body, fontSize=8.5, leading=12,
                           textColor=hex_color(COLOR_MUTED), spaceBefore=2, spaceAfter=6)

    for level, (size, space_before) in enumerate(
            ((15, 12), (13, 11), (11.6, 10), (10.6, 9), (10, 8), (9.6, 8)), start=1):
        styles["h%d" % level] = style(
            "fx-h%d" % level, fontSize=size, leading=size * 1.32,
            spaceBefore=space_before, spaceAfter=4, keepWithNext=1, **base)

    for role, (color, tint) in ROLE_STYLE.items():
        styles["role_" + role] = style(
            "fx-role-" + role, fontName=kit.body, fontSize=9.6, leading=13,
            textColor=hex_color(color), backColor=hex_color(tint),
            borderPadding=(4, 6, 4, 6), spaceBefore=13, spaceAfter=7, keepWithNext=1)

    styles["code"] = style("fx-code", fontName=kit.mono, fontSize=7.8, leading=10.2,
                           textColor=hex_color(COLOR_TEXT),
                           backColor=hex_color(COLOR_CODE_BG),
                           borderColor=hex_color(COLOR_CODE_BORDER), borderWidth=0.5,
                           borderPadding=(6, 7, 6, 7),
                           # must exceed the 6pt top padding, or the grey box is
                           # painted over the language label sitting above it
                           spaceBefore=10, spaceAfter=8)
    styles["code_lang"] = style("fx-code-lang", fontName=kit.mono, fontSize=6.8, leading=9,
                                textColor=hex_color(COLOR_MUTED), spaceBefore=6, spaceAfter=0)
    styles["quote"] = style("fx-quote", fontSize=9.6, leading=13.5, spaceAfter=5,
                            fontName=kit.body, textColor=hex_color("#374151"))
    styles["cell"] = style("fx-cell", fontSize=7.8, leading=10.4, fontName=kit.body,
                           textColor=hex_color(COLOR_TEXT))
    styles["tool"] = style("fx-tool", fontName=kit.mono, fontSize=7.2, leading=9.6,
                           textColor=hex_color("#5B3A0B"),
                           backColor=hex_color(ROLE_STYLE["tool"][1]),
                           borderColor=hex_color("#FDE68A"), borderWidth=0.5,
                           borderPadding=(5, 6, 5, 6), spaceBefore=4, spaceAfter=6)
    return styles


@dataclass
class _Ctx:
    """Everything the block renderers need, resolved once per export."""

    kit: _FontKit
    styles: Dict[str, Any]
    width: float
    # A document has no speakers and no message count to report; see
    # ``DOCUMENT_FLAG`` in src/chat_export.py.
    document_mode: bool = False


# ---------------------------------------------------------------------------
# blocks -> flowables
# ---------------------------------------------------------------------------


def _mono_cols(ctx: _Ctx, style_name: str, width: float) -> int:
    """How many monospaced columns fit in ``width`` at that style's size."""
    rl = _rl()
    style = ctx.styles[style_name]
    pad = style.borderPadding
    pad_x = (pad[1] + pad[3]) if isinstance(pad, (tuple, list)) and len(pad) == 4 else (
        (pad * 2) if isinstance(pad, (int, float)) else 12)
    try:
        char = rl.pdfmetrics.stringWidth("0", style.fontName, style.fontSize)
    except Exception:  # pragma: no cover - defensive
        char = style.fontSize * 0.6
    return max(20, int((width - pad_x - 2) / max(char, 0.1)))


def _code_flowables(ctx: _Ctx, text: str, lang: str, width: float) -> List[Any]:
    rl = _rl()
    out: List[Any] = []
    lang = _clean(lang).strip()
    if lang:
        out.append(rl.Paragraph(_markup(ctx.kit, lang, ctx.kit.mono), ctx.styles["code_lang"]))
    wrapped = _wrap_code(text, _mono_cols(ctx, "code", width))
    # XPreformatted keeps the newlines and the indentation, splits across
    # pages, and paints the grey box - but it parses markup, so escape first.
    out.append(rl.XPreformatted(_markup(ctx.kit, wrapped, ctx.kit.mono) or " ",
                                ctx.styles["code"]))
    return out


def _table_flowable(ctx: _Ctx, block: Block, width: float) -> Optional[Any]:
    rl = _rl()
    rows = [row for row in (block.rows or []) if row is not None]
    if not rows:
        return None
    ncols = max((len(row) for row in rows), default=0)
    if ncols <= 0:
        return None

    # Column widths proportional to content, with a floor so a 10-column table
    # still has room to wrap in every column.
    weights = []
    for col in range(ncols):
        longest = 1
        for row in rows:
            if col < len(row):
                longest = max(longest, _display_width(_spans_text(row[col])))
        weights.append(min(longest, 40) + 4)
    total = float(sum(weights)) or 1.0
    floor = min(30.0, width / ncols)
    widths = [max(floor, width * w / total) for w in weights]
    scale = width / sum(widths)
    widths = [w * scale for w in widths]

    data = []
    for index, row in enumerate(rows):
        cells = []
        for col in range(ncols):
            spans = row[col] if col < len(row) else []
            head = block.header and index == 0
            markup = _spans_markup(ctx.kit, spans)
            if head:
                markup = "<b>%s</b>" % markup if markup else markup
            cells.append(rl.Paragraph(markup or " ", ctx.styles["cell"]))
        data.append(cells)

    commands = [
        ("GRID", (0, 0), (-1, -1), 0.4, rl.colors.HexColor(COLOR_RULE)),
        ("FONTNAME", (0, 0), (-1, -1), ctx.kit.body),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    if block.header:
        commands.append(("BACKGROUND", (0, 0), (-1, 0), rl.colors.HexColor(COLOR_TABLE_HEAD)))
    table = rl.Table(data, colWidths=widths, repeatRows=1 if block.header else 0,
                     splitByRow=1, splitInRow=1, hAlign="LEFT",
                     spaceBefore=4, spaceAfter=8)
    table.setStyle(rl.TableStyle(commands))
    return table


def _quote_flowable(ctx: _Ctx, block: Block, width: float) -> Optional[Any]:
    rl = _rl()
    inner = _blocks_flowables(ctx, block.children, width - 14, quote=True)
    if not inner:
        return None
    table = rl.Table([[inner]], colWidths=[width], splitByRow=1, splitInRow=1,
                     hAlign="LEFT", spaceBefore=2, spaceAfter=7)
    table.setStyle(rl.TableStyle([
        ("LINEBEFORE", (0, 0), (0, -1), 2, rl.colors.HexColor(COLOR_QUOTE_BAR)),
        ("FONTNAME", (0, 0), (-1, -1), ctx.kit.body),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    return table


def _list_flowable(ctx: _Ctx, block: Block, width: float) -> Optional[Any]:
    rl = _rl()
    indent = 16.0
    items = []
    for item_blocks in block.items or []:
        flows = _blocks_flowables(ctx, item_blocks, width - indent)
        if not flows:
            flows = [rl.Paragraph(" ", ctx.styles["body"])]
        items.append(rl.ListItem(flows, leftIndent=indent, spaceBefore=0, spaceAfter=0))
    if not items:
        return None
    common = dict(leftIndent=indent, bulletFontName=ctx.kit.body, bulletFontSize=9,
                  spaceBefore=2, spaceAfter=6, start=1)
    if block.ordered:
        return rl.ListFlowable(items, bulletType="1", bulletFormat="%s.", **common)
    common["start"] = ctx.kit.bullet
    return rl.ListFlowable(items, bulletType="bullet", **common)


def _block_flowables(ctx: _Ctx, block: Block, width: float,
                     quote: bool = False) -> List[Any]:
    rl = _rl()
    kind = getattr(block, "kind", "") or "para"
    body_style = ctx.styles["quote" if quote else "body"]

    if kind == "heading":
        level = block.level if isinstance(block.level, int) else 1
        level = min(6, max(1, level))
        markup = _spans_markup(ctx.kit, block.spans)
        if not markup:
            return []
        return [rl.Paragraph("<b>%s</b>" % markup, ctx.styles["h%d" % level])]

    if kind == "code":
        return _code_flowables(ctx, block.text, block.lang, width)

    if kind == "list":
        flow = _list_flowable(ctx, block, width)
        return [flow] if flow else []

    if kind == "quote":
        flow = _quote_flowable(ctx, block, width)
        return [flow] if flow else []

    if kind == "table":
        flow = _table_flowable(ctx, block, width)
        return [flow] if flow else []

    if kind == "hr":
        return [rl.HRFlowable(width="100%", thickness=0.6, spaceBefore=7, spaceAfter=9,
                              color=rl.colors.HexColor(COLOR_RULE))]

    if kind == "image":
        # Images are not fetched: an export must not make network calls, and a
        # local path may not exist by the time the PDF is opened.
        alt = _spans_text(block.spans) or block.text or ""
        label = "%s %s" % (LABELS["image"], alt.strip()) if alt.strip() else LABELS["image"]
        markup = _markup(ctx.kit, label)
        href = _safe_href(block.href)
        if href:
            markup = '<link href="%s" color="%s">%s</link>' % (
                html.escape(href, quote=True), COLOR_LINK, markup)
        return [rl.Paragraph("<i>%s</i>" % markup, ctx.styles["note"])]

    # "para" and anything unknown: render whatever text we can find.
    markup = _spans_markup(ctx.kit, block.spans)
    if not markup and block.text:
        markup = _markup(ctx.kit, block.text)
    if not markup:
        return []
    return [rl.Paragraph(markup, body_style)]


def _blocks_flowables(ctx: _Ctx, blocks: Sequence[Block], width: float,
                      quote: bool = False) -> List[Any]:
    out: List[Any] = []
    for block in blocks or ():
        try:
            out.extend(_block_flowables(ctx, block, width, quote=quote))
        except Exception:
            # One malformed block must not cost the user the whole export.
            logger.exception("chat_export_pdf: skipping unrenderable block %r",
                             getattr(block, "kind", "?"))
    return out


# ---------------------------------------------------------------------------
# messages
# ---------------------------------------------------------------------------


def _role_key(role: Any) -> str:
    role = _clean(role).strip().lower()
    return role if role in ROLE_STYLE else ("system" if role else "system")


def _role_label(role: Any) -> str:
    key = _clean(role).strip().lower()
    if key in LABELS:
        return LABELS[key]
    return key.replace("_", " ").title() if key else LABELS["system"]


def _tool_flowables(ctx: _Ctx, call: ToolCall, width: float) -> List[Any]:
    rl = _rl()
    name = _clean(getattr(call, "name", "")) or "?"
    head = "%s %s" % (LABELS["tool_call"], name)
    bits = []
    status = _clean(getattr(call, "status", ""))
    if status:
        bits.append(status)
    duration = getattr(call, "duration_s", None)
    if isinstance(duration, (int, float)):
        bits.append("%.2fs" % duration)
    if bits:
        head += "  (%s)" % ", ".join(bits)

    lines = [head]
    arguments = _truncate(getattr(call, "arguments", "") or "", TOOL_TEXT_LIMIT).strip()
    if arguments:
        lines.append("%s: %s" % (LABELS["arguments"], arguments))
    result = _truncate(getattr(call, "result", "") or "", TOOL_TEXT_LIMIT).strip()
    if result:
        lines.append("%s: %s" % (LABELS["result"], result))
    text = _wrap_code("\n".join(lines), _mono_cols(ctx, "tool", width))
    return [rl.XPreformatted(_markup(ctx.kit, text, ctx.kit.mono) or " ", ctx.styles["tool"])]


def _message_flowables(ctx: _Ctx, message: ExportMessage) -> List[Any]:
    rl = _rl()
    width = ctx.width - MESSAGE_INDENT
    out: List[Any] = []

    if not ctx.document_mode:
        role = _role_key(getattr(message, "role", ""))
        label = _role_label(getattr(message, "role", ""))

        banner_bits = ["<b>%s</b>" % _markup(ctx.kit, label)]
        trailing = []
        timestamp = _format_timestamp(getattr(message, "timestamp", ""))
        if timestamp:
            trailing.append(timestamp)
        model = _clean(getattr(message, "model", "")).strip()
        if model:
            trailing.append(model)
        if trailing:
            banner_bits.append('<font color="%s" size="8">%s</font>'
                               % (COLOR_MUTED, _markup(ctx.kit, "  \u00b7  ".join(trailing))))
        out.append(rl.Paragraph("  \u00b7  ".join(banner_bits),
                                ctx.styles["role_" + role]))

    out.append(rl.Indenter(left=MESSAGE_INDENT))
    out.extend(_blocks_flowables(ctx, getattr(message, "blocks", None) or [], width))

    for call in getattr(message, "tool_calls", None) or []:
        try:
            out.extend(_tool_flowables(ctx, call, width))
        except Exception:
            logger.exception("chat_export_pdf: skipping unrenderable tool call")

    attachments = [_clean(a) for a in (getattr(message, "attachments", None) or []) if _clean(a)]
    if attachments:
        out.append(rl.Paragraph(
            "<i>%s: %s</i>" % (LABELS["attachments"], _markup(ctx.kit, ", ".join(attachments))),
            ctx.styles["note"]))

    out.append(rl.Indenter(left=-MESSAGE_INDENT))
    return out


def _header_flowables(ctx: _Ctx, transcript: Transcript) -> List[Any]:
    rl = _rl()
    name = _clean(getattr(transcript, "name", "")).strip() or "Conversation"
    out = [rl.Paragraph("<b>%s</b>" % _markup(ctx.kit, name), ctx.styles["title"])]

    messages = list(getattr(transcript, "messages", None) or [])
    meta: List[str] = []
    model = _clean(getattr(transcript, "model", "")).strip()
    if model:
        meta.append("%s: %s" % (LABELS["model"], model))
    # "1 message" is true of a document only as an accident of how it travels.
    if not ctx.document_mode:
        meta.append("%d %s" % (len(messages),
                               LABELS["messages"] if len(messages) != 1 else LABELS["message"]))
    exported_at = getattr(transcript, "exported_at", None)
    if isinstance(exported_at, datetime):
        meta.append("%s %s" % (LABELS["exported"], exported_at.strftime("%Y-%m-%d %H:%M")))
    elif exported_at:
        meta.append("%s %s" % (LABELS["exported"], _clean(exported_at)))

    for key in ("project", "workspace", "session_id"):
        value = _clean(getattr(transcript, key, "")).strip()
        if value:
            meta.append("%s: %s" % (LABELS["session" if key == "session_id" else key], value))

    # Only reachable for a document: a conversation always has its count.
    if meta:
        out.append(rl.Paragraph(_markup(ctx.kit, "  \u00b7  ".join(meta)), ctx.styles["meta"]))
    out.append(rl.HRFlowable(width="100%", thickness=0.8, spaceBefore=6, spaceAfter=2,
                             color=rl.colors.HexColor(COLOR_RULE)))
    return out


# ---------------------------------------------------------------------------
# page furniture
# ---------------------------------------------------------------------------


def _numbered_canvas(kit: _FontKit, footer_left: str):
    """Canvas subclass that stamps "Page N of M" once the total is known.

    The page count is only known after the whole story is laid out, so pages
    are buffered and the footer is drawn on the second pass.
    """
    rl = _rl()
    label_font = kit.body if kit.body != BUILTIN_SANS else BUILTIN_SANS

    class _NumberedCanvas(rl.Canvas):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._pages: List[dict] = []

        def showPage(self):
            self._pages.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._pages)
            for state in self._pages:
                self.__dict__.update(state)
                self._draw_footer(total)
                super().showPage()
            super().save()

        def _draw_footer(self, total: int) -> None:
            self.saveState()
            self.setFont(label_font, 7.5)
            self.setFillColor(rl.colors.HexColor(COLOR_MUTED))
            width, _height = self._pagesize
            baseline = PAGE_MARGIN_BOTTOM - 26
            self.setStrokeColor(rl.colors.HexColor(COLOR_RULE))
            self.setLineWidth(0.4)
            self.line(PAGE_MARGIN_X, baseline + 12, width - PAGE_MARGIN_X, baseline + 12)
            if footer_left:
                self._draw_runs(PAGE_MARGIN_X, baseline, footer_left, 7.5)
            self.setFont(label_font, 7.5)
            self.drawRightString(width - PAGE_MARGIN_X, baseline,
                                 LABELS["page"] % (self._pageNumber, total))
            self.restoreState()

        def _draw_runs(self, x: float, y: float, text: str, size: float) -> None:
            """drawString can only use one font, so draw one run per font."""
            for font, chunk in _font_runs(kit, text):
                self.setFont(font, size)
                self.drawString(x, y, chunk)
                x += rl.pdfmetrics.stringWidth(chunk, font, size)

    return _NumberedCanvas


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


def render(transcript: Transcript) -> bytes:
    """Render a transcript as a PDF document.

    Raises :class:`ExportUnavailable` (never a bare ImportError) when reportlab
    is not installed.
    """
    rl = _rl()
    kit = _font_kit()
    page_width, page_height = rl.A4
    ctx = _Ctx(kit=kit, styles=_build_styles(kit), width=page_width - 2 * PAGE_MARGIN_X,
               document_mode=is_document(transcript))

    story: List[Any] = _header_flowables(ctx, transcript)
    messages = list(getattr(transcript, "messages", None) or [])
    if not messages:
        story.append(rl.Paragraph("<i>%s</i>" % _markup(kit, LABELS["empty"]),
                                  ctx.styles["note"]))
    for message in messages:
        story.extend(_message_flowables(ctx, message))

    name = _clean(getattr(transcript, "name", "")).strip() or "Conversation"
    footer_left = _clean(name)
    if len(footer_left) > 70:
        footer_left = footer_left[:69] + "\u2026"

    buffer = io.BytesIO()
    doc = rl.SimpleDocTemplate(
        buffer,
        pagesize=(page_width, page_height),
        leftMargin=PAGE_MARGIN_X, rightMargin=PAGE_MARGIN_X,
        topMargin=PAGE_MARGIN_TOP, bottomMargin=PAGE_MARGIN_BOTTOM,
        title=name, author="Faustus", subject="Chat transcript",
        creator="Faustus chat export",
    )
    doc.build(story, canvasmaker=_numbered_canvas(kit, footer_left))
    return buffer.getvalue()
