"""Conversation export — the core the /session/{sid}/export route renders with.

The export used to live inline in ``routes/session_routes.py`` as ~90 lines of
string concatenation, and it lost almost everything that matters: the HTML
format escaped the message and replaced ``\\n`` with ``<br>``, so fenced code
blocks, lists and tables were flattened into one grey wall of text; nothing
carried timestamps, the per-message model, or the agent's tool calls.

This module replaces it with one pipeline, shared by every format:

    Session         -> build_transcript()   -> Transcript
    message text    -> markdown_to_blocks() -> list[Block]
    Transcript      -> render()             -> ExportResult

``markdown_to_blocks`` runs the text through the ``markdown`` package (a hard
dependency already, pinned at 3.10) with the ``fenced_code``, ``tables`` and
``sane_lists`` extensions, then walks the resulting HTML with
``html.parser.HTMLParser`` into the ``Block`` model. No regex parsing of
markdown, and no new dependency.

Security note — the message text is *user and model* input, so it is data, never
markup:

* The ``markdown`` package deliberately passes raw HTML straight through
  (``<script>alert(1)</script>`` survives its output verbatim). The walker below
  is therefore an allowlist: only the tags it understands become structure;
  every other tag is kept as *literal text* in a span, so it can be shown to the
  reader without ever becoming markup again.
* ``render_html`` builds the document from those blocks and escapes every text
  node, so no user-controlled tag or attribute can reach the page by
  construction. URLs are scheme-checked (``javascript:`` is dropped).
* Nothing in the exported HTML is fetched from the network: the CSS is
  embedded, there is no script, and only absolute ``http(s)``/``data:image``
  image sources are emitted as ``<img>``.

Message metadata keys this module reads (all optional; a missing key leaves the
corresponding field empty rather than inventing a value):

``timestamp``
    ISO-8601 string with a ``Z`` suffix. Written by
    ``core.session_manager.SessionManager._persist_message`` and re-attached on
    load in ``_db_to_session``.
``model`` / ``requested_model``
    The model that actually answered / the one that was asked for. Written by
    ``routes.chat_helpers.save_assistant_response``.
``tool_events``
    The agent's tool calls, as saved by ``src.agent_loop`` (and mirrored by
    ``src.bg_monitor`` / ``src.agent_runs``). Each event is a dict with
    ``tool``, ``desc``, ``command``, ``output``, ``exit_code`` and a ``round``;
    ``tool == "mcp"`` events name the real tool inside ``desc``/``command``/
    ``output``, which is resolved here the same way ``agent_loop`` does.
``attachments``
    Upload references on user turns: dicts with ``id``, ``name``, ``mime``,
    ``size`` (see ``routes.chat_helpers.add_user_message``).
``hidden``
    Internal bookkeeping turns (e.g. the compaction summary written by
    ``routes/history/history_routes.py``). The UI never shows them, so the
    export never does either.
"""

from __future__ import annotations

import html as _html
import importlib
import json as _json
import re
import threading
from datetime import datetime
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Sequence

import markdown as _markdown

from src.chat_export_model import (
    MEDIA_TYPES,
    SUPPORTED_FORMATS,
    Block,
    ExportMessage,
    ExportResult,
    ExportUnavailable,
    Span,
    ToolCall,
    Transcript,
)

__all__ = [
    "build_transcript",
    "content_to_text",
    "export_session",
    "markdown_to_blocks",
    "render",
    "render_md",
    "render_txt",
    "render_json",
    "render_html",
    "normalize_format",
    "sanitize_export_filename",
    "default_filename",
]


# ---------------------------------------------------------------------------
# message content
# ---------------------------------------------------------------------------

def _content_to_text(content) -> str:
    """Flatten a message's content to plain text for text-based exports.

    History entries carry three shapes: a plain string, a multimodal list of
    content blocks (vision/image attachments), or None (assistant turns that
    persisted only native tool_calls). The txt/html/md exporters join and
    string-munge this value, so a list crashed the export (TypeError on join,
    AttributeError on .replace) and None rendered as the literal "None".
    Coerce to the text blocks, returning "" for anything without text.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("text")
        )
    return ""


#: Public name for the helper above. It used to live in
#: ``routes/session_routes.py`` (as ``_content_to_text``); it belongs to the
#: exporter, so the route imports it from here. Both names are kept so either
#: spelling works at the call site.
content_to_text = _content_to_text


# ---------------------------------------------------------------------------
# markdown -> blocks
# ---------------------------------------------------------------------------

MARKDOWN_EXTENSIONS = ("fenced_code", "tables", "sane_lists")

_md_local = threading.local()

_HEADING_TAGS = {f"h{n}": n for n in range(1, 7)}
_STYLE_TAGS = {
    "strong": "bold", "b": "bold",
    "em": "italic", "i": "italic",
    "del": "strike", "s": "strike", "strike": "strike",
}
_VOID_TAGS = {"br", "hr", "img"}
_IGNORED_TAGS = {"tbody", "tfoot", "colgroup", "col", "caption"}
# Tags the walker turns into structure. Anything outside this set is kept as
# literal text — that is the allowlist that keeps user HTML inert.
_KNOWN_TAGS = (
    {"p", "pre", "code", "ul", "ol", "li", "blockquote", "table", "thead",
     "tr", "th", "td", "a"}
    | set(_HEADING_TAGS)
    | set(_STYLE_TAGS)
    | _VOID_TAGS
    | _IGNORED_TAGS
)

_SAFE_URL_SCHEMES = ("http", "https", "mailto", "tel")
_SCHEME_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.-]*):")
_MCP_TOOL_RE = re.compile(r"\bmcp__[\w_]+\b")


def _markdown_html(text: str) -> str:
    """Render *text* to HTML, reusing one Markdown instance per thread."""
    instance = getattr(_md_local, "md", None)
    if instance is None:
        instance = _markdown.Markdown(
            extensions=list(MARKDOWN_EXTENSIONS), output_format="html",
        )
        _md_local.md = instance
    try:
        return instance.reset().convert(text)
    except Exception:                       # pragma: no cover - defensive
        # A transcript must export even if one message defeats the parser.
        _md_local.md = None
        return "<p>" + _html.escape(text) + "</p>"


def _safe_url(url: str) -> str:
    """Return *url* if it cannot execute script, else ""."""
    url = (url or "").strip()
    if not url:
        return ""
    # Control characters are how ``java\tscript:`` style bypasses are built.
    url = "".join(ch for ch in url if ord(ch) > 0x20 or ch == " ").strip()
    match = _SCHEME_RE.match(url)
    if not match:
        return url                          # relative / fragment: inert
    scheme = match.group(1).lower()
    if scheme in _SAFE_URL_SCHEMES:
        return url
    if scheme == "data" and url[:11].lower() == "data:image/":
        return url
    return ""


def _is_embeddable_url(url: str) -> bool:
    """Whether a standalone document could actually load this source."""
    lowered = (url or "").lower()
    return lowered.startswith(("http://", "https://", "data:image/"))


def _lang_from_class(value: str) -> str:
    for token in (value or "").split():
        for prefix in ("language-", "lang-"):
            if token.startswith(prefix) and len(token) > len(prefix):
                return token[len(prefix):]
    tokens = [t for t in (value or "").split() if t not in ("highlight", "codehilite")]
    return tokens[0] if len(tokens) == 1 else ""


def _clean_spans(spans: List[Span]) -> List[Span]:
    """Merge adjacent same-styled runs and trim the edges of the run."""
    merged: List[Span] = []
    for span in spans:
        if not span.text:
            continue
        if merged:
            last = merged[-1]
            if (last.bold, last.italic, last.code, last.strike, last.href) == (
                    span.bold, span.italic, span.code, span.strike, span.href):
                last.text += span.text
                continue
        merged.append(Span(text=span.text, bold=span.bold, italic=span.italic,
                           code=span.code, strike=span.strike, href=span.href))
    while merged and not merged[0].text.strip():
        merged.pop(0)
    while merged and not merged[-1].text.strip():
        merged.pop()
    if merged:
        merged[0].text = merged[0].text.lstrip()
        merged[-1].text = merged[-1].text.rstrip()
    return [s for s in merged if s.text]


class _BlockBuilder(HTMLParser):
    """Walk python-markdown's HTML into the shared Block model.

    Only the tags in ``_KNOWN_TAGS`` become structure. Everything else — the
    raw ``<script>``/``<div onclick=...>`` a user can type into a chat — is
    re-emitted as literal text, so it survives into the transcript as something
    the reader can see but no renderer can execute.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: List[Block] = []
        self._stack: List[List[Block]] = [self.blocks]
        # What each push onto _stack was for, so a stray closing tag can never
        # pop somebody else's container.
        self._stack_kinds: List[str] = []
        self._spans: List[Span] = []
        self._saved_spans: List[List[Span]] = []
        self._cell_depth = 0
        self._style: List[Dict[str, Any]] = []
        self._headings: List[int] = []
        self._lists: List[Block] = []
        self._tables: List[Block] = []
        self._row: Optional[List[List[Span]]] = None
        self._row_is_header = False
        self._thead_depth = 0
        self._in_pre = False
        self._pre_parts: List[str] = []
        self._pre_lang = ""

    # -- helpers ---------------------------------------------------------
    def _container(self) -> List[Block]:
        return self._stack[-1]

    def _span(self, text: str) -> Span:
        style = {"bold": False, "italic": False, "code": False,
                 "strike": False, "href": ""}
        for layer in self._style:
            for key, value in layer.items():
                if key == "href":
                    if value:
                        style["href"] = value
                elif value:
                    style[key] = True
        return Span(text=text, **style)

    def _pop_style(self, key: str) -> None:
        for index in range(len(self._style) - 1, -1, -1):
            if key in self._style[index]:
                self._style.pop(index)
                return

    def _flush_inline(self) -> None:
        spans = _clean_spans(self._spans)
        self._spans = []
        if not spans:
            return
        if self._headings:
            self._container().append(
                Block(kind="heading", spans=spans, level=self._headings[-1]))
        else:
            self._container().append(Block(kind="para", spans=spans))

    # -- HTMLParser hooks -------------------------------------------------
    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attributes = {k.lower(): (v or "") for k, v in attrs}

        if self._in_pre:
            if tag == "code":
                self._pre_lang = self._pre_lang or _lang_from_class(
                    attributes.get("class", ""))
            elif tag == "br":
                self._pre_parts.append("\n")
            else:
                self._pre_parts.append(self.get_starttag_text() or "")
            return

        if tag == "p":
            self._flush_inline()
        elif tag in _HEADING_TAGS:
            self._flush_inline()
            self._headings.append(_HEADING_TAGS[tag])
        elif tag == "pre":
            self._flush_inline()
            self._in_pre = True
            self._pre_parts = []
            self._pre_lang = ""
        elif tag == "code":
            self._style.append({"code": True})
        elif tag in ("ul", "ol"):
            self._flush_inline()
            block = Block(kind="list", ordered=(tag == "ol"))
            self._container().append(block)
            self._lists.append(block)
        elif tag == "li":
            if not self._lists:
                return
            self._flush_inline()
            item: List[Block] = []
            self._lists[-1].items.append(item)
            self._stack.append(item)
            self._stack_kinds.append("li")
            self._saved_spans.append(self._spans)
            self._spans = []
        elif tag == "blockquote":
            self._flush_inline()
            block = Block(kind="quote")
            self._container().append(block)
            self._stack.append(block.children)
            self._stack_kinds.append("quote")
        elif tag == "hr":
            self._flush_inline()
            self._container().append(Block(kind="hr"))
        elif tag == "br":
            if self._spans:
                self._spans.append(self._span("\n"))
        elif tag == "img":
            self._flush_inline()
            self._container().append(Block(
                kind="image",
                href=_safe_url(attributes.get("src", "")),
                text=attributes.get("alt", ""),
            ))
        elif tag == "table":
            self._flush_inline()
            block = Block(kind="table")
            self._container().append(block)
            self._tables.append(block)
        elif tag == "thead":
            self._thead_depth += 1
        elif tag == "tr":
            self._row = []
            self._row_is_header = self._thead_depth > 0
        elif tag in ("th", "td"):
            if self._row is None:
                self._row = []
            if tag == "th":
                self._row_is_header = True
            self._cell_depth += 1
            self._saved_spans.append(self._spans)
            self._spans = []
        elif tag == "a":
            self._style.append({"href": _safe_url(attributes.get("href", ""))})
        elif tag in _STYLE_TAGS:
            self._style.append({_STYLE_TAGS[tag]: True})
        elif tag in _IGNORED_TAGS:
            return
        else:
            # Unknown tag: data, not markup.
            self._spans.append(self._span(self.get_starttag_text() or f"<{tag}>"))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        lowered = tag.lower()
        if lowered in _KNOWN_TAGS and lowered not in _VOID_TAGS:
            self.handle_endtag(lowered)

    def handle_endtag(self, tag):
        tag = tag.lower()

        if self._in_pre:
            if tag == "pre":
                self._in_pre = False
                self._container().append(Block(
                    kind="code",
                    text="".join(self._pre_parts).strip("\n"),
                    lang=self._pre_lang,
                ))
                self._pre_parts = []
                self._pre_lang = ""
            elif tag != "code":
                self._pre_parts.append(f"</{tag}>")
            return

        if tag == "p":
            self._flush_inline()
        elif tag in _HEADING_TAGS:
            self._flush_inline()
            if self._headings:
                self._headings.pop()
        elif tag == "code":
            self._pop_style("code")
        elif tag in ("ul", "ol"):
            self._flush_inline()
            if self._lists:
                self._lists.pop()
        elif tag == "li":
            if self._stack_kinds and self._stack_kinds[-1] == "li":
                self._flush_inline()
                self._stack.pop()
                self._stack_kinds.pop()
                self._spans = self._saved_spans.pop() if self._saved_spans else []
        elif tag == "blockquote":
            self._flush_inline()
            if self._stack_kinds and self._stack_kinds[-1] == "quote":
                self._stack.pop()
                self._stack_kinds.pop()
        elif tag == "thead":
            self._thead_depth = max(0, self._thead_depth - 1)
        elif tag in ("th", "td"):
            if not self._cell_depth:
                return                      # stray </td>: nothing to close
            self._cell_depth -= 1
            cell = _clean_spans(self._spans)
            self._spans = self._saved_spans.pop() if self._saved_spans else []
            if self._tables and self._row is not None:
                self._row.append(cell)
            else:
                # A cell outside any table (hand-written HTML in a message):
                # keep its text in the running paragraph rather than lose it.
                self._spans.extend(cell)
        elif tag == "tr":
            if self._tables and self._row:
                table = self._tables[-1]
                if self._row_is_header and not table.rows:
                    table.header = True
                table.rows.append(self._row)
            self._row = None
            self._row_is_header = False
        elif tag == "table":
            self._flush_inline()
            if self._tables:
                self._tables.pop()
        elif tag == "a":
            self._pop_style("href")
        elif tag in _STYLE_TAGS:
            self._pop_style(_STYLE_TAGS[tag])
        elif tag in _VOID_TAGS or tag in _IGNORED_TAGS:
            return
        else:
            self._spans.append(self._span(f"</{tag}>"))

    def handle_data(self, data):
        if self._in_pre:
            self._pre_parts.append(data)
            return
        if not data:
            return
        if not data.strip() and not self._spans:
            return                          # whitespace between block tags
        self._spans.append(self._span(data))

    def handle_comment(self, data):         # noqa: D401 - comments are not content
        return

    def finish(self) -> List[Block]:
        # Close whatever the input left open, without losing its text.
        while self._in_pre:
            self.handle_endtag("pre")
        self._flush_inline()
        return self.blocks


def markdown_to_blocks(text: Optional[str]) -> List[Block]:
    """Parse markdown *text* into the shared block model.

    Covers paragraphs, ``h1``-``h6``, fenced code (with the language from
    ``class="language-x"``), ordered/unordered lists with nesting, blockquotes,
    horizontal rules, tables with a header row, images, and the inline styles
    bold / italic / code / strike / link.

    Empty or whitespace-only text yields ``[]``. Raw HTML in the message is
    never treated as markup — see the module docstring.
    """
    if not isinstance(text, str) or not text.strip():
        return []
    builder = _BlockBuilder()
    try:
        builder.feed(_markdown_html(text))
        builder.close()
    except Exception:                       # pragma: no cover - defensive
        return [Block(kind="para", spans=[Span(text=text)])]
    return builder.finish()


# ---------------------------------------------------------------------------
# session -> transcript
# ---------------------------------------------------------------------------

def _attr(obj, key, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _metadata(message) -> Dict[str, Any]:
    metadata = _attr(message, "metadata", None)
    return metadata if isinstance(metadata, dict) else {}


def _text_value(value) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _human_size(size: Any) -> str:
    try:
        size = int(size)
    except (TypeError, ValueError):
        return ""
    if size <= 0:
        return ""
    for unit in ("B", "kB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return ""


def _resolved_tool_name(event: Dict[str, Any]) -> str:
    """Mirror ``agent_loop._resolved_tool_event_name``: "mcp" names the real
    tool inside desc/command/output."""
    tool = _text_value(event.get("tool")).strip()
    if tool != "mcp":
        return tool
    for key in ("desc", "command", "output"):
        match = _MCP_TOOL_RE.search(_text_value(event.get(key)))
        if match:
            return match.group(0)
    return tool


def _tool_calls_from_metadata(metadata: Dict[str, Any]) -> List[ToolCall]:
    events = metadata.get("tool_events")
    if not isinstance(events, list):
        return []
    calls: List[ToolCall] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        name = _resolved_tool_name(event) or "tool"
        arguments = _text_value(event.get("command")).strip()
        if not arguments:
            arguments = _text_value(event.get("desc")).strip()
        exit_code = event.get("exit_code")
        if exit_code in (None, 0):
            status = "ok"
        else:
            status = f"error ({exit_code})"
        duration = event.get("duration_s", event.get("duration"))
        try:
            duration_s = float(duration) if duration is not None else None
        except (TypeError, ValueError):
            duration_s = None
        calls.append(ToolCall(
            name=name,
            arguments=arguments,
            result=_text_value(event.get("output")),
            status=status,
            duration_s=duration_s,
        ))
    return calls


def _attachments_from_metadata(metadata: Dict[str, Any]) -> List[str]:
    raw = metadata.get("attachments")
    if not isinstance(raw, list):
        return []
    labels: List[str] = []
    for item in raw:
        if isinstance(item, str):
            if item.strip():
                labels.append(item.strip())
            continue
        if not isinstance(item, dict):
            continue
        name = _text_value(
            item.get("name") or item.get("original_name") or item.get("id")).strip()
        if not name:
            continue
        details = [d for d in (_text_value(item.get("mime")).strip(),
                               _human_size(item.get("size"))) if d]
        labels.append(f"{name} ({', '.join(details)})" if details else name)
    return labels


def build_transcript(session, *, include_tools: bool = True,
                     include_system: bool = False) -> Transcript:
    """Convert a ``Session`` (or any object/dict shaped like one) to a Transcript.

    ``system`` turns are skipped unless *include_system* is set, and tool calls
    are dropped when *include_tools* is false. Messages flagged
    ``metadata["hidden"]`` (the compaction bookkeeping the UI hides) are never
    exported. See the module docstring for the metadata keys read here.
    """
    history = _attr(session, "history", None) or []
    messages: List[ExportMessage] = []
    for entry in history:
        role = _text_value(_attr(entry, "role", "")).strip().lower()
        metadata = _metadata(entry)
        if metadata.get("hidden"):
            continue
        if role == "system" and not include_system:
            continue
        raw_text = _content_to_text(_attr(entry, "content", None))
        model = _text_value(metadata.get("model") or metadata.get("requested_model")).strip()
        messages.append(ExportMessage(
            role=role or "user",
            blocks=markdown_to_blocks(raw_text),
            raw_text=raw_text,
            timestamp=_text_value(metadata.get("timestamp")).strip(),
            model=model,
            tool_calls=_tool_calls_from_metadata(metadata) if include_tools else [],
            attachments=_attachments_from_metadata(metadata),
        ))

    return Transcript(
        name=_text_value(_attr(session, "name", "")),
        model=_text_value(_attr(session, "model", "")),
        exported_at=datetime.now(),
        messages=messages,
        session_id=_text_value(_attr(session, "id", "")),
    )


# ---------------------------------------------------------------------------
# shared rendering helpers
# ---------------------------------------------------------------------------

_ROLE_LABELS = {
    "user": "User",
    "assistant": "Assistant",
    "system": "System",
    "tool": "Tool",
}


def _role_label(role: str) -> str:
    return _ROLE_LABELS.get(role, (role or "message").replace("_", " ").title())


def _plain_text(spans: Sequence[Span]) -> str:
    return "".join(span.text for span in spans)


def _longest_backtick_run(text: str) -> int:
    return max((len(m) for m in re.findall(r"`+", text or "")), default=0)


def _fence_for(text: str) -> str:
    """A fence long enough to wrap *text* without being closed early."""
    return "`" * max(3, _longest_backtick_run(text) + 1)


def _inline_ticks_for(text: str) -> str:
    """The shortest delimiter that can carry *text* as an inline code span."""
    return "`" * max(1, _longest_backtick_run(text) + 1)


# ---------------------------------------------------------------------------
# markdown renderer
# ---------------------------------------------------------------------------

_MD_INLINE_ESCAPE = set("\\`*[]<")


def _md_escape(text: str) -> str:
    """Escape the characters that would otherwise re-parse as markup.

    Deliberately narrow: ``_`` is only escaped where python-markdown would read
    it as emphasis (not inside a word), so ``snake_case`` survives unharmed.
    """
    out: List[str] = []
    for index, char in enumerate(text):
        if char in _MD_INLINE_ESCAPE:
            out.append("\\" + char)
        elif char == "_":
            previous = text[index - 1] if index else ""
            following = text[index + 1] if index + 1 < len(text) else ""
            out.append("_" if (previous.isalnum() and following.isalnum()) else "\\_")
        else:
            out.append(char)
    return "".join(out)


def _md_escape_line_starts(text: str) -> str:
    lines = []
    for line in text.split("\n"):
        stripped = line.lstrip()
        indent = line[:len(line) - len(stripped)]
        if stripped[:1] in ("#", ">", "-", "+", "=") or re.match(r"\d+[.)]\s", stripped):
            stripped = "\\" + stripped
        lines.append(indent + stripped)
    return "\n".join(lines)


def _span_to_md(span: Span) -> str:
    text = span.text
    lead = text[:len(text) - len(text.lstrip())]
    trail = text[len(text.rstrip()):]
    core = text.strip()
    if not core:
        return text
    if span.code:
        ticks = _inline_ticks_for(core)
        pad = " " if (core.startswith("`") or core.endswith("`")) else ""
        body = f"{ticks}{pad}{core}{pad}{ticks}"
    else:
        body = _md_escape(core)
    if span.bold:
        body = f"**{body}**"
    if span.italic:
        body = f"*{body}*"
    if span.strike:
        # python-markdown has no strikethrough extension in our set, but it does
        # pass <del> through — and the walker above turns it back into a strike
        # span, so this round-trips.
        body = f"<del>{body}</del>"
    if span.href:
        body = f"[{body}]({span.href})"
    return f"{lead}{body}{trail}"


def _spans_to_md(spans: Sequence[Span]) -> str:
    return "".join(_span_to_md(span) for span in spans)


def _table_to_md(block: Block) -> str:
    rows = [[_spans_to_md(cell).replace("|", "\\|").replace("\n", " ")
             for cell in row] for row in block.rows]
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    if block.header:
        header, body = rows[0], rows[1:]
    else:
        header, body = [""] * width, rows
    lines = ["| " + " | ".join(header) + " |",
             "| " + " | ".join(["---"] * width) + " |"]
    lines += ["| " + " | ".join(row) + " |" for row in body]
    return "\n".join(lines)


def _blocks_to_md(blocks: Sequence[Block]) -> str:
    parts: List[str] = []
    previous_kind = ""
    for block in blocks:
        text = _block_to_md(block)
        if not text:
            continue
        if parts:
            # Keep a list tight against the paragraph that introduces it.
            parts.append("\n" if (block.kind == "list" and previous_kind == "para")
                         else "\n\n")
        parts.append(text)
        previous_kind = block.kind
    return "".join(parts)


def _block_to_md(block: Block) -> str:
    kind = block.kind
    if kind == "para":
        return _md_escape_line_starts(_spans_to_md(block.spans))
    if kind == "heading":
        level = min(max(block.level or 1, 1), 6)
        title = _spans_to_md(block.spans).replace("\n", " ").strip()
        return f"{'#' * level} {title}" if title else ""
    if kind == "code":
        fence = _fence_for(block.text)
        return f"{fence}{block.lang}\n{block.text}\n{fence}"
    if kind == "list":
        lines: List[str] = []
        for index, item in enumerate(block.items, 1):
            marker = f"{index}. " if block.ordered else "- "
            body = _blocks_to_md(item) or ""
            item_lines = body.split("\n")
            lines.append(marker + (item_lines[0] if item_lines else ""))
            for line in item_lines[1:]:
                lines.append("    " + line if line else "")
        return "\n".join(lines)
    if kind == "quote":
        body = _blocks_to_md(block.children)
        return "\n".join(("> " + line if line else ">") for line in body.split("\n"))
    if kind == "table":
        return _table_to_md(block)
    if kind == "hr":
        return "---"
    if kind == "image":
        alt = (block.text or "").replace("]", "\\]")
        return f"![{alt}]({block.href})" if block.href else f"![{alt}]()"
    return ""


def _tool_calls_to_md(calls: Sequence[ToolCall]) -> str:
    if not calls:
        return ""
    lines = ["<details>",
             f"<summary>Tool calls ({len(calls)})</summary>",
             ""]
    for index, call in enumerate(calls, 1):
        head = f"**{index}. `{call.name}`**"
        if call.status:
            head += f" — {call.status}"
        if call.duration_s is not None:
            head += f" — {call.duration_s:.2f}s"
        lines += [head, ""]
        if call.arguments:
            fence = _fence_for(call.arguments)
            lines += ["Input:", "", f"{fence}text", call.arguments, fence, ""]
        if call.result:
            fence = _fence_for(call.result)
            lines += ["Output:", "", f"{fence}text", call.result, fence, ""]
    lines.append("</details>")
    return "\n".join(lines)


def render_md(transcript: Transcript) -> str:
    """Render the transcript as clean, re-parseable markdown."""
    out: List[str] = [f"# Conversation: {transcript.name or 'Untitled'}", ""]
    meta = [f"- **Model:** {transcript.model}" if transcript.model else "",
            f"- **Exported:** {transcript.exported_at:%Y-%m-%d %H:%M:%S}",
            f"- **Session:** {transcript.session_id}" if transcript.session_id else "",
            f"- **Messages:** {len(transcript.messages)}"]
    out += [line for line in meta if line]

    for message in transcript.messages:
        out += ["", "---", ""]
        head = f"### {_role_label(message.role).upper()}"
        details = [d for d in (message.timestamp, message.model) if d]
        if details:
            head += " — " + " · ".join(details)
        out += [head, ""]
        body = _blocks_to_md(message.blocks)
        out.append(body if body else "*(no text content)*")
        if message.attachments:
            out += ["", "**Attachments:** " + ", ".join(message.attachments)]
        tools = _tool_calls_to_md(message.tool_calls)
        if tools:
            out += ["", tools]
    out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# plain-text renderer
# ---------------------------------------------------------------------------

_TXT_WIDTH = 80


def _display_width(text: str) -> int:
    return len(text)


def _spans_to_txt(spans: Sequence[Span]) -> str:
    """Plain text, keeping a link's target — a text export has no other place
    to put it."""
    out: List[str] = []
    for span in spans:
        text = span.text
        if span.href and span.href not in text:
            trail = text[len(text.rstrip()):]
            text = f"{text.rstrip()} <{span.href}>{trail}"
        out.append(text)
    return "".join(out)


def _table_to_txt(block: Block) -> str:
    rows = [[_spans_to_txt(cell).replace("\n", " ") for cell in row]
            for row in block.rows]
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    widths = [max(_display_width(row[i]) for row in rows) for i in range(width)]
    lines = []
    for index, row in enumerate(rows):
        lines.append(" | ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
        if index == 0 and block.header:
            lines.append("-+-".join("-" * widths[i] for i in range(width)))
    return "\n".join(lines)


def _blocks_to_txt(blocks: Sequence[Block], indent: str = "") -> str:
    out: List[str] = []
    previous_kind = ""
    for block in blocks:
        text = _block_to_txt(block, indent)
        if not text:
            continue
        if out:
            # A nested list stays tight against the line that introduces it.
            out.append("\n" if (block.kind == "list" and previous_kind == "para")
                       else "\n\n")
        out.append(text)
        previous_kind = block.kind
    return "".join(out)


def _block_to_txt(block: Block, indent: str = "") -> str:
    kind = block.kind

    def _pad(text: str, extra: str = "") -> str:
        prefix = indent + extra
        return "\n".join((prefix + line) if line else "" for line in text.split("\n"))

    if kind == "para":
        return _pad(_spans_to_txt(block.spans))
    if kind == "heading":
        title = _spans_to_txt(block.spans).replace("\n", " ").strip()
        underline = ("=" if (block.level or 1) <= 2 else "-") * max(len(title), 3)
        return _pad(f"{title}\n{underline}")
    if kind == "code":
        return _pad(block.text, "    ")
    if kind == "list":
        lines: List[str] = []
        for index, item in enumerate(block.items, 1):
            marker = f"{index}. " if block.ordered else "- "
            body = _blocks_to_txt(item)
            item_lines = body.split("\n") if body else [""]
            lines.append(indent + marker + item_lines[0].lstrip())
            for line in item_lines[1:]:
                lines.append((indent + "    " + line.lstrip()) if line.strip() else "")
        return "\n".join(lines)
    if kind == "quote":
        return _pad(_blocks_to_txt(block.children), "> ")
    if kind == "table":
        return _pad(_table_to_txt(block))
    if kind == "hr":
        return indent + "-" * 40
    if kind == "image":
        alt = block.text or "image"
        return _pad(f"[image: {alt}{' — ' + block.href if block.href else ''}]")
    return ""


def render_txt(transcript: Transcript) -> str:
    """Render the transcript as plain text that reads without a renderer."""
    out: List[str] = [f"Conversation: {transcript.name or 'Untitled'}"]
    if transcript.model:
        out.append(f"Model: {transcript.model}")
    out.append(f"Exported: {transcript.exported_at:%Y-%m-%d %H:%M:%S}")
    if transcript.session_id:
        out.append(f"Session: {transcript.session_id}")
    out.append(f"Messages: {len(transcript.messages)}")

    for message in transcript.messages:
        out += ["", "=" * _TXT_WIDTH, ""]
        head = _role_label(message.role).upper()
        if message.timestamp:
            head += f"  [{message.timestamp}]"
        if message.model:
            head += f"  ({message.model})"
        out += [head, "-" * _TXT_WIDTH]
        body = _blocks_to_txt(message.blocks)
        out.append(body if body else "(no text content)")
        if message.attachments:
            out += ["", "Attachments:"]
            out += [f"  - {label}" for label in message.attachments]
        if message.tool_calls:
            out += ["", f"Tool calls ({len(message.tool_calls)}):"]
            for index, call in enumerate(message.tool_calls, 1):
                head = f"  [{index}] {call.name}"
                if call.status:
                    head += f"  ({call.status})"
                if call.duration_s is not None:
                    head += f"  {call.duration_s:.2f}s"
                out.append(head)
                for line in (call.arguments or "").split("\n"):
                    if line.strip():
                        out.append(f"      $ {line}")
                for line in (call.result or "").split("\n"):
                    if line.strip():
                        out.append(f"      | {line}")
    out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# json renderer
# ---------------------------------------------------------------------------

def _span_to_dict(span: Span) -> Dict[str, Any]:
    data: Dict[str, Any] = {"text": span.text}
    for flag in ("bold", "italic", "code", "strike"):
        if getattr(span, flag):
            data[flag] = True
    if span.href:
        data["href"] = span.href
    return data


def _block_to_dict(block: Block) -> Dict[str, Any]:
    data: Dict[str, Any] = {"kind": block.kind}
    if block.spans:
        data["spans"] = [_span_to_dict(span) for span in block.spans]
    if block.level:
        data["level"] = block.level
    if block.lang:
        data["lang"] = block.lang
    if block.text:
        data["text"] = block.text
    if block.items:
        data["items"] = [[_block_to_dict(b) for b in item] for item in block.items]
    if block.children:
        data["children"] = [_block_to_dict(b) for b in block.children]
    if block.ordered:
        data["ordered"] = True
    if block.rows:
        data["rows"] = [[[_span_to_dict(s) for s in cell] for cell in row]
                        for row in block.rows]
    if block.header:
        data["header"] = True
    if block.href:
        data["href"] = block.href
    return data


def transcript_to_dict(transcript: Transcript) -> Dict[str, Any]:
    """The JSON payload: the whole model, with the legacy keys preserved.

    ``name``, ``model``, ``exported`` and ``messages[].role`` /
    ``messages[].content`` keep the shape the old ``fmt=json`` export produced,
    so existing consumers keep working; everything else is new.
    """
    return {
        "name": transcript.name,
        "model": transcript.model,
        "exported": transcript.exported_at.isoformat(),
        "session_id": transcript.session_id,
        "project": transcript.project,
        "workspace": transcript.workspace,
        "message_count": len(transcript.messages),
        "extra": transcript.extra,
        "messages": [
            {
                "role": message.role,
                "content": message.raw_text,
                "timestamp": message.timestamp,
                "model": message.model,
                "attachments": list(message.attachments),
                "tool_calls": [
                    {
                        "name": call.name,
                        "arguments": call.arguments,
                        "result": call.result,
                        "status": call.status,
                        "duration_s": call.duration_s,
                    }
                    for call in message.tool_calls
                ],
                "blocks": [_block_to_dict(block) for block in message.blocks],
            }
            for message in transcript.messages
        ],
    }


def render_json(transcript: Transcript) -> str:
    """Render the transcript as structured JSON."""
    return _json.dumps(transcript_to_dict(transcript), indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# html renderer
# ---------------------------------------------------------------------------

_HTML_CSS = """
:root{
  color-scheme: light dark;
  --bg:#f6f7f9; --fg:#1c1d21; --muted:#6a6c76; --border:#e1e3e9;
  --card:#ffffff; --user-bg:#eef2ff; --user-border:#c7d2fe;
  --ai-bg:#ffffff; --ai-border:#e1e3e9;
  --code-bg:#f2f3f6; --code-fg:#1c1d21; --accent:#4f46e5; --ok:#1a7f4b;
  --err:#b3261e;
}
@media (prefers-color-scheme: dark){
  :root{
    --bg:#0f1013; --fg:#e6e7ea; --muted:#9a9da8; --border:#2a2c33;
    --card:#16171b; --user-bg:#1b2140; --user-border:#3a4480;
    --ai-bg:#16171b; --ai-border:#2a2c33;
    --code-bg:#0b0c0f; --code-fg:#e6e7ea; --accent:#a5b4fc; --ok:#6ee7a8;
    --err:#ff9a92;
  }
}
*{box-sizing:border-box}
body{
  margin:0; padding:2rem 1rem 4rem; background:var(--bg); color:var(--fg);
  font:16px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,
       "Helvetica Neue",Arial,"Noto Sans",sans-serif;
}
.doc{max-width:52rem;margin:0 auto}
.doc-head{border-bottom:1px solid var(--border);padding-bottom:1rem;margin-bottom:2rem}
.doc-head h1{font-size:1.6rem;line-height:1.3;margin:0 0 .5rem;word-wrap:break-word}
.doc-meta{margin:0;color:var(--muted);font-size:.85rem}
.doc-meta span+span::before{content:"·";margin:0 .5rem;opacity:.6}
.msg{border:1px solid var(--ai-border);background:var(--ai-bg);border-radius:12px;
     padding:1rem 1.2rem;margin:0 0 1.1rem}
.msg-user{background:var(--user-bg);border-color:var(--user-border)}
.msg-system{opacity:.85;border-style:dashed}
.msg-head{display:flex;flex-wrap:wrap;gap:.5rem;align-items:baseline;
          margin-bottom:.6rem;font-size:.78rem;color:var(--muted)}
.msg-head .role{font-weight:700;letter-spacing:.06em;text-transform:uppercase;
                color:var(--accent)}
.msg-body>*:first-child{margin-top:0}
.msg-body>*:last-child{margin-bottom:0}
.msg-body h1{font-size:1.35rem}
.msg-body h2{font-size:1.2rem}
.msg-body h3{font-size:1.05rem}
.msg-body h4,.msg-body h5,.msg-body h6{font-size:1rem}
.msg-body p{margin:.65rem 0}
.msg-body img{max-width:100%;height:auto;border-radius:8px}
.msg-body a{color:var(--accent)}
code{font-family:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,
     "Liberation Mono",monospace;font-size:.88em}
:not(pre)>code{background:var(--code-bg);padding:.12em .35em;border-radius:4px}
pre{background:var(--code-bg);color:var(--code-fg);border:1px solid var(--border);
    border-radius:8px;padding:.85rem 1rem;overflow-x:auto;margin:.8rem 0}
pre code{background:none;padding:0;white-space:pre}
blockquote{margin:.8rem 0;padding:.1rem 0 .1rem 1rem;border-left:3px solid var(--border);
           color:var(--muted)}
table{border-collapse:collapse;margin:.9rem 0;display:block;overflow-x:auto;
      max-width:100%}
th,td{border:1px solid var(--border);padding:.4rem .7rem;text-align:left}
th{background:var(--code-bg);font-weight:600}
hr{border:0;border-top:1px solid var(--border);margin:1.2rem 0}
ul,ol{padding-left:1.4rem;margin:.65rem 0}
li{margin:.2rem 0}
.attachments{margin-top:.8rem;font-size:.82rem;color:var(--muted)}
.tools{margin-top:.9rem;border:1px solid var(--border);border-radius:8px;
       background:var(--code-bg)}
.tools>summary{cursor:pointer;padding:.5rem .8rem;font-size:.82rem;
               color:var(--muted);font-weight:600}
.tool-list{margin:0;padding:0 .9rem .6rem 2rem}
.tool-name{font-weight:700}
.tool-status{font-size:.78rem;margin-left:.5rem}
.tool-status.ok{color:var(--ok)}
.tool-status.err{color:var(--err)}
.tools pre{background:var(--card);margin:.4rem 0}
.img-alt{color:var(--muted);font-style:italic}
@media print{
  body{background:#fff;color:#000;padding:0}
  .msg{break-inside:avoid;border-color:#ccc}
  .tools{display:block}
}
""".strip()


def _esc(text: Any) -> str:
    return _html.escape(_text_value(text), quote=True)


def _span_to_html(span: Span) -> str:
    body = _esc(span.text)
    if span.code:
        body = f"<code>{body}</code>"
    if span.bold:
        body = f"<strong>{body}</strong>"
    if span.italic:
        body = f"<em>{body}</em>"
    if span.strike:
        body = f"<del>{body}</del>"
    href = _safe_url(span.href)
    if href:
        body = f'<a href="{_esc(href)}" rel="noopener noreferrer">{body}</a>'
    return body


def _spans_to_html(spans: Sequence[Span]) -> str:
    return "".join(_span_to_html(span) for span in spans)


def _blocks_to_html(blocks: Sequence[Block]) -> str:
    return "".join(_block_to_html(block) for block in blocks)


def _block_to_html(block: Block) -> str:
    kind = block.kind
    if kind == "para":
        return f"<p>{_spans_to_html(block.spans)}</p>"
    if kind == "heading":
        level = min(max(block.level or 1, 1), 6)
        return f"<h{level}>{_spans_to_html(block.spans)}</h{level}>"
    if kind == "code":
        attribute = f' class="language-{_esc(block.lang)}"' if block.lang else ""
        return f"<pre><code{attribute}>{_esc(block.text)}</code></pre>"
    if kind == "list":
        tag = "ol" if block.ordered else "ul"
        items = []
        for item in block.items:
            if len(item) == 1 and item[0].kind == "para":
                items.append(f"<li>{_spans_to_html(item[0].spans)}</li>")
            elif item and item[0].kind == "para":
                rest = _blocks_to_html(item[1:])
                items.append(f"<li>{_spans_to_html(item[0].spans)}{rest}</li>")
            else:
                items.append(f"<li>{_blocks_to_html(item)}</li>")
        return f"<{tag}>{''.join(items)}</{tag}>"
    if kind == "quote":
        return f"<blockquote>{_blocks_to_html(block.children)}</blockquote>"
    if kind == "table":
        return _table_to_html(block)
    if kind == "hr":
        return "<hr>"
    if kind == "image":
        alt = _esc(block.text)
        source = _safe_url(block.href)
        if source and _is_embeddable_url(source):
            return f'<p><img src="{_esc(source)}" alt="{alt}"></p>'
        # A standalone document cannot resolve anything else — and this is also
        # where a raw <img src=x onerror=...> from the chat lands: as text.
        label = block.text or block.href or "image"
        return f'<p><span class="img-alt">[image: {_esc(label)}]</span></p>'
    return ""


def _table_to_html(block: Block) -> str:
    if not block.rows:
        return ""
    parts = ["<table>"]
    rows = list(block.rows)
    if block.header:
        head = rows.pop(0)
        parts.append("<thead><tr>"
                     + "".join(f"<th>{_spans_to_html(cell)}</th>" for cell in head)
                     + "</tr></thead>")
    parts.append("<tbody>")
    for row in rows:
        parts.append("<tr>"
                     + "".join(f"<td>{_spans_to_html(cell)}</td>" for cell in row)
                     + "</tr>")
    parts.append("</tbody></table>")
    return "".join(parts)


def _tool_calls_to_html(calls: Sequence[ToolCall]) -> str:
    if not calls:
        return ""
    plural = "" if len(calls) == 1 else "s"
    parts = ['<details class="tools">',
             f"<summary>{len(calls)} tool call{plural}</summary>",
             '<ol class="tool-list">']
    for call in calls:
        status_class = "ok" if call.status.startswith("ok") else "err"
        head = f'<code class="tool-name">{_esc(call.name)}</code>'
        if call.status:
            head += f'<span class="tool-status {status_class}">{_esc(call.status)}</span>'
        if call.duration_s is not None:
            head += f'<span class="tool-status">{call.duration_s:.2f}s</span>'
        parts.append(f"<li>{head}")
        if call.arguments:
            parts.append(f"<pre><code>{_esc(call.arguments)}</code></pre>")
        if call.result:
            parts.append(f"<pre><code>{_esc(call.result)}</code></pre>")
        parts.append("</li>")
    parts.append("</ol></details>")
    return "".join(parts)


def render_html(transcript: Transcript) -> str:
    """Render the transcript as one standalone, themable HTML document.

    Built from the block model, escaping every text node — user HTML can never
    become markup here. The CSS is embedded and nothing is loaded from the
    network beyond images the conversation itself linked.
    """
    title = _esc(transcript.name or "Conversation")
    meta_bits = [f"<span>{_esc(transcript.model)}</span>" if transcript.model else "",
                 f"<span>{len(transcript.messages)} messages</span>",
                 f"<span>Exported {transcript.exported_at:%Y-%m-%d %H:%M:%S}</span>"]
    if transcript.session_id:
        meta_bits.append(f"<span>Session {_esc(transcript.session_id)}</span>")

    parts: List[str] = [
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
        '<header class="doc-head">',
        f"<h1>{title}</h1>",
        '<p class="doc-meta">' + "".join(b for b in meta_bits if b) + "</p>",
        "</header>",
    ]

    for message in transcript.messages:
        role_class = re.sub(r"[^a-z0-9_-]", "", message.role.lower()) or "other"
        parts.append(f'<article class="msg msg-{role_class}">')
        head = [f'<span class="role">{_esc(_role_label(message.role))}</span>']
        if message.timestamp:
            head.append(f"<time>{_esc(message.timestamp)}</time>")
        if message.model:
            head.append(f'<span class="model">{_esc(message.model)}</span>')
        parts.append('<div class="msg-head">' + "".join(head) + "</div>")
        body = _blocks_to_html(message.blocks)
        parts.append('<div class="msg-body">'
                     + (body or '<p class="img-alt">(no text content)</p>')
                     + "</div>")
        if message.attachments:
            items = ", ".join(_esc(label) for label in message.attachments)
            parts.append(f'<div class="attachments">Attachments: {items}</div>')
        parts.append(_tool_calls_to_html(message.tool_calls))
        parts.append("</article>")

    parts += ["</main>", "</body>", "</html>"]
    return "\n".join(part for part in parts if part)


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------

_FORMAT_ALIASES = {
    "markdown": "md", "md": "md", "mdown": "md",
    "text": "txt", "txt": "txt", "plain": "txt",
    "json": "json",
    "html": "html", "htm": "html",
    "pdf": "pdf",
    "docx": "docx", "word": "docx",
}

_TEXT_RENDERERS = {
    "md": render_md,
    "txt": render_txt,
    "json": render_json,
    "html": render_html,
}

_LAZY_MODULES = {
    "pdf": "src.chat_export_pdf",
    "docx": "src.chat_export_docx",
}


def normalize_format(fmt: Any) -> str:
    """Map a caller's ``fmt=`` value onto a supported format key."""
    key = _text_value(fmt).strip().lower().lstrip(".")
    return _FORMAT_ALIASES.get(key, key)


def sanitize_export_filename(name: Any) -> str:
    """Return a conservative filename safe for Content-Disposition.

    Same allowlist as the route's ``_sanitize_export_filename`` (which this
    replaces), plus two hardenings: parent-directory hops (``..``) are
    collapsed, and a leading ``.``/``-`` is stripped so the result can be
    neither a hidden file nor mistaken for a flag.
    """
    name = name if isinstance(name, str) else ""
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    name = re.sub(r"\.{2,}", ".", name)
    name = name.lstrip("._-")
    return name[:128]


def default_filename(transcript: Transcript, ext: str) -> str:
    """``conversation_<sanitized-name>_<YYYYMMDD_HHMMSS>.<ext>``."""
    stamp = transcript.exported_at.strftime("%Y%m%d_%H%M%S")
    safe_name = sanitize_export_filename(transcript.name)[:60].strip("_")
    stem = f"conversation_{safe_name}_{stamp}" if safe_name else f"conversation_{stamp}"
    return f"{stem}.{ext}"


def _resolve_filename(transcript: Transcript, ext: str, filename: str) -> str:
    safe = sanitize_export_filename(filename)
    if not safe:
        return default_filename(transcript, ext)
    return safe if "." in safe else f"{safe}.{ext}"


def _render_binary(key: str, transcript: Transcript) -> bytes:
    """Hand off to the optional pdf/docx renderer, or explain its absence."""
    module_name = _LAZY_MODULES[key]
    label = key.upper()
    try:
        module = importlib.import_module(module_name)
    except ExportUnavailable:
        raise
    except Exception as exc:
        raise ExportUnavailable(
            f"{label} export is unavailable: could not import {module_name} "
            f"({exc}). Export as md, html, txt or json instead."
        ) from exc

    renderer = getattr(module, "render", None)
    if not callable(renderer):
        raise ExportUnavailable(
            f"{label} export is unavailable: {module_name} does not provide "
            f"render(transcript) -> bytes. Export as md, html, txt or json instead."
        )

    try:
        data = renderer(transcript)
    except ExportUnavailable:
        raise
    except Exception as exc:
        raise ExportUnavailable(
            f"{label} export failed in {module_name}: {exc}. "
            f"Export as md, html, txt or json instead."
        ) from exc

    if not isinstance(data, (bytes, bytearray)):
        raise ExportUnavailable(
            f"{label} export is unavailable: {module_name}.render returned "
            f"{type(data).__name__}, expected bytes."
        )
    return bytes(data)


def render(transcript: Transcript, fmt: str, *, filename: str = "") -> ExportResult:
    """Render *transcript* in *fmt* and wrap it with its media type and filename.

    ``md``/``txt``/``json``/``html`` are produced here; ``pdf``/``docx`` are
    delegated to ``src.chat_export_pdf`` / ``src.chat_export_docx``, imported
    lazily so a missing optional dependency costs nothing on the common path and
    surfaces as ``ExportUnavailable`` with a message meant for the user.
    """
    key = normalize_format(fmt)
    if key not in SUPPORTED_FORMATS:
        raise ValueError(
            f"Unsupported export format: {fmt!r}. "
            f"Supported: {', '.join(SUPPORTED_FORMATS)}"
        )
    out_name = _resolve_filename(transcript, key, filename)
    if key in _TEXT_RENDERERS:
        content = _TEXT_RENDERERS[key](transcript).encode("utf-8")
    else:
        content = _render_binary(key, transcript)
    return ExportResult(content=content, media_type=MEDIA_TYPES[key],
                        filename=out_name)


def export_session(session, fmt: str = "md", *, filename: str = "",
                   include_tools: bool = True,
                   include_system: bool = False) -> ExportResult:
    """Convenience one-liner for the route: session in, ExportResult out."""
    transcript = build_transcript(session, include_tools=include_tools,
                                  include_system=include_system)
    return render(transcript, fmt, filename=filename)
