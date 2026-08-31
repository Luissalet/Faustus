"""Shared block model for chat export.

Every export format renders from the *same* intermediate model, so a
transcript reads the same whether it lands as Markdown, HTML, PDF or DOCX.
The pipeline is:

    session -> build_transcript() -> Transcript
    message text -> markdown_to_blocks() -> list[Block]
    Transcript -> a renderer -> ExportResult

`markdown_to_blocks` goes through the `markdown` package (already a hard
dependency) rather than a hand-rolled parser: fenced code, tables and lists
are exactly the constructs a model emits most, and they are exactly the ones
a hand-rolled parser gets wrong. The resulting HTML is walked into the blocks
below, so the non-HTML formats inherit that parser's correctness for free.

This module holds *only* the data contract. It imports nothing from the app
so that every renderer can depend on it without a cycle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

# Formats the exporter can produce. `md` stays the default for backwards
# compatibility with the existing /export?fmt= callers.
SUPPORTED_FORMATS = ("md", "txt", "json", "html", "pdf", "docx")

MEDIA_TYPES = {
    "md": "text/markdown; charset=utf-8",
    "txt": "text/plain; charset=utf-8",
    "json": "application/json",
    "html": "text/html; charset=utf-8",
    "pdf": "application/pdf",
    "docx": ("application/vnd.openxmlformats-officedocument"
             ".wordprocessingml.document"),
    "zip": "application/zip",
}


@dataclass
class Span:
    """A run of inline text with uniform styling.

    A paragraph is a list of these. `code` and `href` are mutually usable:
    a linked code span keeps both.
    """

    text: str
    bold: bool = False
    italic: bool = False
    code: bool = False
    strike: bool = False
    href: str = ""


@dataclass
class Block:
    """One block-level element.

    kind:
      "para"    — spans
      "heading" — spans + level (1-6)
      "code"    — text (raw, newlines intact) + lang ("" when unlabelled)
      "list"    — items (each a list of Blocks) + ordered
      "quote"   — children (a list of Blocks)
      "table"   — rows: list of rows, each a list of cells, each a list of
                  Spans. `header` marks the first row as a header row.
      "hr"      — nothing else
      "image"   — href (src) + text (alt)
    """

    kind: str
    spans: List[Span] = field(default_factory=list)
    level: int = 0
    lang: str = ""
    text: str = ""
    items: List[List["Block"]] = field(default_factory=list)
    children: List["Block"] = field(default_factory=list)
    ordered: bool = False
    rows: List[List[List[Span]]] = field(default_factory=list)
    header: bool = False
    href: str = ""


@dataclass
class ToolCall:
    """One tool the agent ran inside a message.

    These live in message metadata today and appear in *no* export format,
    which is the single biggest gap: an agent transcript without its tool
    calls is not a record of what happened.
    """

    name: str
    arguments: str = ""
    result: str = ""
    status: str = ""
    duration_s: Optional[float] = None


@dataclass
class ExportMessage:
    role: str                                   # user | assistant | system | tool
    blocks: List[Block] = field(default_factory=list)
    raw_text: str = ""                          # the original markdown source
    timestamp: str = ""                         # ISO 8601, or "" when unknown
    model: str = ""                             # per-message model, when recorded
    tool_calls: List[ToolCall] = field(default_factory=list)
    attachments: List[str] = field(default_factory=list)


@dataclass
class Transcript:
    name: str
    model: str
    exported_at: datetime
    messages: List[ExportMessage] = field(default_factory=list)
    session_id: str = ""
    project: str = ""
    workspace: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ExportResult:
    """What a renderer hands back to the route."""

    content: bytes
    media_type: str
    filename: str


class ExportUnavailable(RuntimeError):
    """A format whose optional dependency is not installed.

    Carries a message meant to be shown to the user verbatim, naming the
    package to install — never a bare ImportError traceback.
    """
