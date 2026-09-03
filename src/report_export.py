"""Research report export — one finished report, out of the app as a file.

A deep-research report could only ever be *read*, as the visual HTML page the
browser renders. This module makes it a document: markdown, Word or PDF (plus
html/txt/json), assembled once as blocks and handed to the export pipeline that
already exists for conversations.

Nothing here parses markdown or lays out a page. ``src/chat_export.py`` does
both, from the block model in ``src/chat_export_model.py``:

    research JSON -> build_report_transcript() -> Transcript
    Transcript    -> render_report()           -> ExportResult

Document shape (see :func:`build_report_blocks`): the question as an h1, one
italic metadata line, the report body, a sources appendix when the body does
not already carry one, and a footer.

Where this bends around the conversation exporter
-------------------------------------------------
The pipeline was built for chats, and two of its renderers say so out loud:

* ``render_md`` / ``render_txt`` / ``render_html`` open with
  "Conversation: <name>", a message count, and a per-message ``### ASSISTANT``
  banner. On a report that is wrong twice over — a duplicated title and a
  speaker label on a document with no speakers — so those three formats are
  composed here from the *same* block serializers those renderers use
  (``_block_to_md``, ``_blocks_to_txt``, ``_blocks_to_html``), skipping only
  the chat furniture around them. They are private names in that module; the
  seam this module would rather import is a public ``blocks_to_md`` /
  ``blocks_to_txt`` / ``blocks_to_html``. Markdown also needs its own join —
  see :func:`_render_markdown`.
* ``src/chat_export_docx.py`` and ``src/chat_export_pdf.py`` take a Transcript
  and *always* emit a header ("N messages · Exported …") and a role banner per
  message. Neither is suppressible from the outside, and editing those files is
  not this change's business. The report therefore travels as a single message
  whose role is ``"report"``: unknown roles fall back to the neutral grey
  "system" styling in both renderers, so the banner reads "Report" instead of
  "Assistant". The minimal fix, when someone owns those files: honour a
  ``transcript.extra["document"]`` flag by skipping the role banner in
  ``_add_message`` / ``_message_flowables`` and the message count in
  ``_add_header`` / ``_header_flowables``. ``extra`` already exists on
  Transcript and is ignored by both.
"""

from __future__ import annotations

import importlib
import re
import unicodedata
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from src.chat_export import (
    _HTML_CSS,
    _block_to_md,
    _blocks_to_html,
    _blocks_to_txt,
    markdown_to_blocks,
    normalize_format,
    render,
    render_json,
    sanitize_export_filename,
)
from src.chat_export_model import (
    MEDIA_TYPES,
    Block,
    ExportMessage,
    ExportResult,
    ExportUnavailable,
    Span,
    Transcript,
)

__all__ = [
    "REPORT_FORMATS",
    "available_formats",
    "build_report_blocks",
    "build_report_transcript",
    "render_report",
    "report_filename",
]

#: Formats a report can be asked for. ``md`` first: it is the lossless one.
REPORT_FORMATS = ("md", "docx", "pdf", "html", "txt", "json")

#: The role the single report message carries. Not a chat role, so the docx and
#: pdf renderers style its banner neutrally instead of colouring it as a
#: speaker — see the module docstring.
REPORT_ROLE = "report"

#: A body that already ends in its own sources section must not get a second
#: one. Agent-written reports append "## Fuentes" or "## Sources" themselves.
_SOURCES_HEADING_RE = re.compile(
    r"^#{1,3}\s*(fuentes|sources|referencias|references)\b", re.IGNORECASE
)

_SOURCES_TITLE = "Sources"

_FOOTER_PREFIX = "Exported from Faustus"


# ---------------------------------------------------------------------------
# reading the research JSON
# ---------------------------------------------------------------------------

def _text(value: Any) -> str:
    """A trimmed string for any value, and "" for None.

    Every field of the research JSON is optional and some are written by the
    researcher rather than by us, so a metadata line must never be able to
    print the literal "None".
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _stats(data: Dict[str, Any]) -> Dict[str, Any]:
    """The stats dict keyed lowercase.

    ``DeepResearcher.get_stats`` capitalises its keys ("Duration", "Rounds"),
    but the value is also ``None`` on a report that failed early, and older
    files on disk are not guaranteed to use that spelling.
    """
    raw = data.get("stats")
    if not isinstance(raw, dict):
        return {}
    return {str(key).strip().lower(): value for key, value in raw.items()}


def _sources(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = data.get("sources")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _report_markdown(data: Dict[str, Any]) -> str:
    """The report body: ``raw_report``, or ``result`` when it is empty.

    ``raw_report`` is the model's own markdown; ``result`` is the formatted
    variant handed to the chat. Older files have only ``result``.
    """
    return _text(data.get("raw_report")) or _text(data.get("result"))


def _time_of(data: Dict[str, Any], *keys: str) -> Optional[datetime]:
    """The first of *keys* that holds a usable time, as a datetime.

    ``_save_result`` writes unix timestamps; be tolerant of an ISO string in
    case an older file or another writer used one.
    """
    for key in keys:
        raw = data.get(key)
        if isinstance(raw, (int, float)) and raw > 0:
            try:
                return datetime.fromtimestamp(float(raw))
            except (OverflowError, OSError, ValueError):
                continue
        text = _text(raw)
        if text:
            try:
                return datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                continue
    return None


def _metadata_bits(data: Dict[str, Any]) -> List[str]:
    """The metadata line, as label/value pairs, skipping everything absent."""
    stats = _stats(data)
    bits: List[str] = []

    # Only completed_at earns the word "Completed": falling back to the start
    # time here would label the moment the research began as the moment it
    # finished.
    completed = _time_of(data, "completed_at")
    if completed is not None:
        bits.append("Completed %s" % completed.strftime("%Y-%m-%d %H:%M"))

    model = _text(stats.get("model"))
    if model:
        bits.append("Model: %s" % model)

    rounds = _text(stats.get("rounds"))
    if rounds:
        bits.append("Rounds: %s" % rounds)

    sources = _sources(data)
    if sources:
        bits.append("Sources: %d" % len(sources))
    # Deliberately no fallback to stats["URLs"]: that counts every page fetched,
    # including the ones the researcher discarded, so printing it under
    # "Sources" would overstate what the report actually rests on.

    duration = _text(stats.get("duration"))
    if duration:
        bits.append("Duration: %s" % duration)

    category = _text(data.get("category")) or _text(stats.get("category"))
    if category:
        bits.append("Category: %s" % category.capitalize())

    return bits


# ---------------------------------------------------------------------------
# the document
# ---------------------------------------------------------------------------

def _para(text: str, *, italic: bool = False) -> Block:
    return Block(kind="para", spans=[Span(text=text, italic=italic)])


def _heading(text: str, level: int) -> Block:
    return Block(kind="heading", level=level, spans=[Span(text=text)])


def _heading_line(block: Block) -> str:
    """A heading block back as its markdown line, for the sources probe."""
    level = min(max(block.level or 1, 1), 6)
    return "#" * level + " " + "".join(span.text for span in block.spans).strip()


def ends_with_sources_section(blocks: Sequence[Block]) -> bool:
    """True when the report body already closes with its own sources section.

    Matched on the parsed blocks rather than the raw markdown so a "## Sources"
    line *inside a fenced code block* cannot suppress the real appendix.
    """
    for block in reversed(blocks):
        if block.kind != "heading":
            continue
        return bool(_SOURCES_HEADING_RE.match(_heading_line(block)))
    return False


def _sources_blocks(data: Dict[str, Any]) -> List[Block]:
    sources = _sources(data)
    if not sources:
        return []
    items: List[List[Block]] = []
    for source in sources:
        url = _text(source.get("url"))
        title = _text(source.get("title")) or url
        if not title:
            continue
        span = Span(text=title, href=url) if url else Span(text=title)
        items.append([Block(kind="para", spans=[span])])
    if not items:
        return []
    return [_heading(_SOURCES_TITLE, 2),
            Block(kind="list", ordered=True, items=items)]


def build_report_blocks(data: Dict[str, Any], *,
                        exported_at: Optional[datetime] = None,
                        title_in_body: bool = True) -> List[Block]:
    """The whole report as blocks: title, metadata, body, sources, footer.

    ``title_in_body=False`` leaves the h1 out, for a renderer that prints
    ``transcript.name`` as the document title itself — see
    :func:`build_report_transcript`.
    """
    exported_at = exported_at or datetime.now()
    blocks: List[Block] = []

    query = _text(data.get("query"))
    if query and title_in_body:
        blocks.append(_heading(query, 1))

    bits = _metadata_bits(data)
    if bits:
        blocks.append(_para(" · ".join(bits), italic=True))

    body = markdown_to_blocks(_report_markdown(data))
    blocks.extend(body)

    if not ends_with_sources_section(body):
        blocks.extend(_sources_blocks(data))

    blocks.append(Block(kind="hr"))
    blocks.append(_para("%s · %s"
                        % (_FOOTER_PREFIX, exported_at.strftime("%Y-%m-%d %H:%M:%S")),
                        italic=True))
    return blocks


def build_report_transcript(data: Dict[str, Any], *,
                            title_in_body: bool = True) -> Transcript:
    """Wrap the report blocks in the Transcript the renderers consume.

    ``model`` and ``session_id`` are deliberately left empty: the docx and pdf
    renderers print them in a header of their own, directly above the report's
    metadata line, which would say the same thing twice. Both travel in
    ``extra`` instead, where the json export picks them up.

    Those two renderers also print ``name`` as the document's title, so they
    ask for ``title_in_body=False`` — otherwise page one opens with the
    question, and then the question again as the body's first heading.
    """
    if not isinstance(data, dict):
        raise TypeError("research data must be a dict")

    exported_at = datetime.now()
    stats = _stats(data)
    query = _text(data.get("query")) or "Research report"
    message = ExportMessage(
        role=REPORT_ROLE,
        blocks=build_report_blocks(data, exported_at=exported_at,
                                   title_in_body=title_in_body),
        raw_text=_report_markdown(data),
    )
    return Transcript(
        name=query,
        model="",
        exported_at=exported_at,
        messages=[message],
        extra={
            "kind": "research_report",
            "query": query,
            "status": _text(data.get("status")),
            "category": _text(data.get("category")),
            "model": _text(stats.get("model")),
            "stats": data.get("stats") if isinstance(data.get("stats"), dict) else {},
            "sources": _sources(data),
            "started_at": data.get("started_at"),
            "completed_at": data.get("completed_at"),
        },
    )


# ---------------------------------------------------------------------------
# filenames
# ---------------------------------------------------------------------------

def _slug(text: str, limit: int) -> str:
    """An ASCII-ish slug: accents folded, everything else collapsed to "_".

    ``sanitize_export_filename`` alone would turn every accented letter into an
    underscore, so "¿Es eficaz la fisioterapia?" came out as a row of them.
    Folding the combining marks first keeps the words readable.
    """
    folded = unicodedata.normalize("NFKD", text or "")
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    folded = re.sub(r"[^A-Za-z0-9]+", "_", folded).strip("_")
    return sanitize_export_filename(folded[:limit]).strip("_")


def report_filename(data: Dict[str, Any], ext: str) -> str:
    """``research_<slug>_<YYYYMMDD_HHMMSS>.<ext>``.

    Stamped with the report's completion time, not the export's, so exporting
    the same report twice gives the same file rather than two that differ only
    by the second they were downloaded in.
    """
    ext = sanitize_export_filename(_text(ext).lstrip(".")) or "md"
    stamp = (_time_of(data, "completed_at", "started_at")
             or datetime.now()).strftime("%Y%m%d_%H%M%S")
    slug = _slug(_text(data.get("query")), 60)
    stem = "research_%s_%s" % (slug, stamp) if slug else "research_%s" % stamp
    return sanitize_export_filename("%s.%s" % (stem, ext))


def _resolve_filename(data: Dict[str, Any], ext: str, filename: str) -> str:
    safe = sanitize_export_filename(filename)
    if not safe:
        return report_filename(data, ext)
    return safe if "." in safe else "%s.%s" % (safe, ext)


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def _render_markdown(blocks: Sequence[Block]) -> str:
    """Serialize the blocks with a blank line between every one of them.

    Deliberately not ``_blocks_to_md``: that one keeps a list tight against the
    paragraph introducing it (one newline, no blank line), and the ``markdown``
    package's ``sane_lists`` extension then reads those "- " lines as a lazy
    continuation of the paragraph. A report whose bullets sit under a sentence
    would re-parse as one run-on paragraph — the md export has to survive being
    read back. Per-block serialization is still theirs.
    """
    return "\n\n".join(part for part in (_block_to_md(b) for b in blocks) if part)


def _render_html_document(transcript: Transcript, blocks: Sequence[Block]) -> str:
    """A standalone HTML page — the export stylesheet, none of the chat frame.

    ``render_html`` would wrap the report in a message card under a
    "Conversation" header; the blocks go straight into ``.msg-body`` here so
    they still pick up that stylesheet's typography.
    """
    from src.chat_export import _esc  # local: only this function needs it

    title = _esc(transcript.name or "Research report")
    return "\n".join([
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{title}</title>",
        f"<style>{_HTML_CSS}</style>",
        "</head>",
        "<body>",
        '<main class="doc">',
        '<article class="msg-body">',
        _blocks_to_html(blocks),
        "</article>",
        "</main>",
        "</body>",
        "</html>",
        "",
    ])


def render_report(data: Dict[str, Any], fmt: str, *,
                  filename: str = "") -> ExportResult:
    """Render a stored research report in *fmt*.

    Raises ``ValueError`` for an unknown format and ``ExportUnavailable`` when
    a binary format's optional dependency is missing — the caller turns the
    latter into a message for the user, not a 500.
    """
    key = normalize_format(fmt)
    if key not in REPORT_FORMATS:
        raise ValueError(
            "Unsupported export format: %r. Supported: %s"
            % (fmt, ", ".join(REPORT_FORMATS))
        )

    binary = key in ("docx", "pdf")
    transcript = build_report_transcript(data, title_in_body=not binary)
    out_name = _resolve_filename(data, key, filename)

    if binary:
        result = render(transcript, key, filename=out_name)
        return ExportResult(content=result.content, media_type=result.media_type,
                            filename=out_name)

    blocks = transcript.messages[0].blocks
    if key == "md":
        text = _render_markdown(blocks) + "\n"
    elif key == "txt":
        text = _blocks_to_txt(blocks) + "\n"
    elif key == "html":
        text = _render_html_document(transcript, blocks)
    else:
        text = render_json(transcript)

    return ExportResult(content=text.encode("utf-8"),
                        media_type=MEDIA_TYPES[key], filename=out_name)


# ---------------------------------------------------------------------------
# availability
# ---------------------------------------------------------------------------

#: Optional dependency behind each binary format.
_FORMAT_DEPENDENCY = {"docx": "docx", "pdf": "reportlab"}

_availability: Optional[Dict[str, bool]] = None


def available_formats(*, refresh: bool = False) -> Dict[str, bool]:
    """Which formats this process can actually produce right now.

    Probed by import and cached: the answer cannot change without a restart,
    and importing reportlab is not cheap enough to redo per request. The UI
    greys out what would otherwise come back as a 415.
    """
    global _availability
    if _availability is not None and not refresh:
        return dict(_availability)

    probed: Dict[str, bool] = {}
    for key in REPORT_FORMATS:
        module = _FORMAT_DEPENDENCY.get(key)
        if module is None:
            probed[key] = True
            continue
        try:
            importlib.import_module(module)
            probed[key] = True
        except Exception:
            probed[key] = False
    _availability = probed
    return dict(probed)
