"""The `document` domain: blocks, headings, figures, references and tables. §10.

Plain text, Markdown, and structured JSON or YAML. Nothing else: a PDF, a
DOCX, an image of a page -- anything whose bytes are not text this module can
read -- comes back `unreadable()` with the reason named, which is a correct
answer and the only honest one available. A reader guessed from the shape of a
file returns plausible text from the wrong place, and every conclusion drawn
from it looks exactly like a real one.

**§10's rule about models is why this adapter is small.** "Los hechos extraídos
por un modelo son claims, no observaciones exactas": a fact a model reads out
of a document is a claim about it, and a claim is not an observation. There is
no model here, so there are no claims -- only structure (blocks, headings,
tables), figures, references, and arithmetic. That is a deliberate scope and
not an omission: the layer the plan describes for extracted facts belongs above
this one, where a claim can be labelled as such and its confidence capped at
`model`, and building it into an adapter that reports at tier `parser` would
launder an inference into evidence.

Five rules, each with the failure it prevents:

* **Reflow is not deletion.** A block whose hash appears at another position
  is `moved`. §10 names this the trap of the domain, and it is why alignment
  runs by CONTENT before it runs by address here: a block's number is its
  position, and a position is not an identity for a paragraph. Reported as
  `missing` + `added`, a repagination reads as the document losing and gaining
  everything it contains.

* **A changed figure is not a changed sentence.** A number that moves points at
  the content invariant, so it lands as `material` rather than as one more
  edited paragraph. Reformatting prose and rewriting a quantity are different
  events with different consequences.

* **A citation or a link that disappears is materially different even when the
  prose around it barely moved.** §10's own sentence. Prose similarity is not
  evidence about provenance, and the reference layer is addressed separately
  precisely so that a paragraph rewritten around a dropped source cannot come
  out as one `modified` row.

* **Totals are recomputed, not read.** The column is summed on BOTH sides and
  compared against the declared total. A total that stops adding up is
  arithmetic and not an opinion, which is why it is the one finding here that
  claims `exact`.

* **Text nobody could extract is `unknown`, never `preserved`.** The coverage
  says how much was read and the invariants say `unknown` with the reason.
  Rule 1 of `contracts.py`: not detected is not preserved.

`registry.py` finds this module by the module-level `ADAPTER_FACTORY` at the
bottom, and there is no list to add it to.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from src.context_engine.code_index import CERTAINTY
from src.delta_engine import alignment as alignment_mod
from src.delta_engine import confidence as confidence_mod
from src.delta_engine import coverage as coverage_mod
from src.delta_engine import sources
from src.delta_engine.adapters.base import (
    Element,
    Extraction,
    Finding,
    Scope,
    Snapshot,
    unreadable,
)
from src.delta_engine.contracts import (
    Alignment,
    Coverage,
    IntentContract,
    InvariantResult,
    RevisionRef,
)

try:  # pragma: no cover - exercised by whichever branch this machine has
    import yaml as _yaml
except Exception:  # noqa: BLE001 - a missing optional reader is a coverage note
    _yaml = None

logger = logging.getLogger(__name__)

__all__ = [
    "DocumentAdapter",
    "ADAPTER_FACTORY",
    "CITATIONS_INVARIANT",
    "FIGURES_INVARIANT",
    "TOTAL_ROW_LABELS",
]


# -- the invariants this adapter can be asked about ------------------------
#
# Both ids come from `invariants.DOMAIN_DEFAULTS["document"]`. They are written
# as constants here and asserted against that catalogue in
# `tests/test_delta_engine_document.py`, so a rename over there fails a test
# rather than leaving findings pointing at an invariant nobody declared.

#: §10: "citas desaparecidas son materialmente distintas aunque la prosa
#: parezca similar". Class `provenance`, whose severity floor is `material`.
CITATIONS_INVARIANT = "document.citations_kept"

#: A figure edited in passing. Class `content`: a number is not prose, and a
#: delta that files a changed quantity next to a reflowed paragraph has told
#: the reader nothing.
FIGURES_INVARIANT = "document.figures_kept"

#: There is no constant for a budget invariant here: `invariants.py` declares
#: none for this domain, and naming an id nobody defines would put findings in
#: a delta pointing at an invariant that never existed. The budget branch of
#: `check_invariants` dispatches on the invariant's CLASS instead, so a caller
#: that declares one under any id still gets the check.

#: First-cell labels that mark a totals row. Deliberately short and matched
#: whole (case-folded, punctuation stripped): a row whose label merely CONTAINS
#: "total" -- "total addressable market" -- is a data row, and summing the
#: column above it would invent an arithmetic error that is not there.
TOTAL_ROW_LABELS: Tuple[str, ...] = ("total", "totals", "sum", "subtotal",
                                     "suma", "totales")

#: A Markdown heading. ATX only (`## Title`): the Setext underline form is
#: ambiguous with a table separator row and with a horizontal rule, and a
#: heading detector that guesses wrong re-parents every block under it.
_HEADING_RE = re.compile(r"^(?P<level>#{1,6})\s+(?P<text>.+?)\s*#*\s*$")

#: A table separator (`| --- | ---: |`). A pipe row is only a table when the
#: row under it is one of these, which is what keeps a sentence containing a
#: pipe from becoming a one-row table.
_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$")

#: A fenced code block. Numbers inside one are code and not figures, so the
#: number layer skips them: reporting `timeout=30 -> 5` twice, once as a figure
#: and once as a changed line, is one fact wearing two addresses.
_FENCE_RE = re.compile(r"^\s*(```|~~~)")

#: `[text](url)` and bare `http(s)://...`. Both, because a Markdown document
#: routinely carries the two forms and a reader dropping one of them would
#: report half the links of a document as absent from the other half.
_MD_LINK_RE = re.compile(r"\[[^\]]*\]\((?P<url>[^)\s]+)[^)]*\)")
_BARE_URL_RE = re.compile(r"(?<![(\[])\b(?P<url>https?://[^\s<>)\]]+)")

#: `[@key]` (pandoc/BibTeX) and `[^key]` (footnote). Two spellings, one layer:
#: what matters downstream is that a source the document leaned on is gone.
_CITATION_RE = re.compile(r"\[(?P<marker>[@^])(?P<key>[^\]\s]+)\]")

#: A figure with an optional currency in front and an optional unit ATTACHED to
#: it. A unit separated by a space is not captured, on purpose: "3 dogs" would
#: give `dogs` as a unit and "in 2024 we grew" would give `we`, and a wrong
#: unit is worse than a missing one -- it makes two equal numbers compare as
#: different. The number itself is compared either way.
_NUMBER_RE = re.compile(
    r"(?P<currency>[$€£¥]?)\s?"
    r"(?P<value>\d{1,3}(?:[,  ]\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?P<unit>%|[A-Za-z°µ][A-Za-z°µ/]{0,7})?"
)

#: What a number in a cell has to look like before a column is summed. Same
#: shape as `_NUMBER_RE`, anchored: a cell holding "about 12" is not a number,
#: and treating it as 12 would make a recomputed total that nobody wrote.
_CELL_NUMBER_RE = re.compile(
    r"^[\s$€£¥]*(?P<value>-?\d{1,3}(?:[,  ]\d{3})+(?:\.\d+)?|-?\d+(?:\.\d+)?)"
    r"\s*[%A-Za-z°µ/]{0,8}\s*$"
)

#: How much of a block its `value` shows. §10 addresses blocks by hash; the
#: value is what a reader sees in a finding, and a value long enough to hold a
#: paragraph would make the delta as expensive to read as the document.
BLOCK_PREVIEW_CHARS = 80

#: Cut for any other rendered value (a cell, a heading, a URL).
MAX_VALUE_CHARS = 300

#: How many element keys a coverage lists, below `Coverage`'s own 1024 so the
#: cut can be reported rather than hidden at the contract's ceiling.
MAX_LISTED_REGIONS = 512

#: Suffixes read as structured data rather than as prose.
JSON_SUFFIXES: Tuple[str, ...] = (".json",)
YAML_SUFFIXES: Tuple[str, ...] = (".yaml", ".yml")

#: The address a literal with no usable name is compared under. Both ends get
#: the same one, so two stashed documents align by key instead of reading as a
#: deletion plus an addition.
LITERAL_NAME = "literal"


# -- small helpers ---------------------------------------------------------


def _norm(text: str) -> str:
    """Whitespace collapsed to single spaces, ends stripped.

    This is what a block is hashed on, and it is the whole of "reformatting is
    not a change": re-wrapping a paragraph at 80 columns moves every newline in
    it and moves nothing a reader would call content. Hashing the raw text
    instead would report a document reflowed by an editor as rewritten
    end to end.
    """
    return " ".join(str(text or "").split())


def _sha_text(text: str) -> str:
    """sha256 of a normalised string, as UTF-8. One spelling for both ends."""
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _short(value: Any, limit: int = MAX_VALUE_CHARS) -> str:
    """A rendered value, cut and saying so."""
    text = _norm(value)
    if len(text) <= limit:
        return text
    return text[:limit - 1] + "…"


def _unique(values: Iterable[Any]) -> Tuple[str, ...]:
    """Order-preserving de-duplication for lists assembled HERE.

    `Coverage.parse` refuses duplicates, and the repeats this removes are the
    ones produced by joining two snapshots' notes. A duplicate inside one
    snapshot's own list is still that snapshot's contradiction.
    """
    seen = set()
    out: List[str] = []
    for value in values:
        text = str(value)
        if text in seen:
            continue
        seen.add(text)
        out.append(text)
    return tuple(out)


def _slug(text: str, taken: Dict[str, int]) -> str:
    """A stable address for a heading, unique within one document.

    A repeated heading ("Notes" under three chapters) gets `notes-2`, `notes-3`.
    Without the suffix the two would share an address, `Snapshot.index()` would
    keep the last and alignment would compare one section against another --
    silently, and with `exact` confidence, because an identical address is
    identity.
    """
    base = re.sub(r"[^a-z0-9]+", "-", _norm(text).lower()).strip("-") or "section"
    taken[base] = taken.get(base, 0) + 1
    return base if taken[base] == 1 else f"{base}-{taken[base]}"


def _number(raw: str) -> Optional[float]:
    """A figure as a float, or `None` when the text is not one number.

    Thousands separators are dropped and nothing else is interpreted: a
    European decimal comma is NOT converted, because "1,5" and "1,500" cannot
    be told apart without knowing the document's locale, and guessing would
    turn one number into another with no trace.
    """
    text = str(raw or "").strip().replace(",", "").replace(" ", "").replace(" ", "")
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _render_number(currency: str, value: str, unit: str) -> str:
    """How a figure is rendered as an element value: the number and its unit."""
    return f"{currency}{value}{unit}".strip()


def _is_url(text: str) -> bool:
    """Whether a scalar is a link. Scheme-anchored, never guessed from a dot."""
    return str(text or "").strip().lower().startswith(("http://", "https://"))


def _key_parts(key: str) -> Tuple[str, str, str]:
    """`(layer, address, name)` out of an element key.

    One reader for the six key shapes this adapter emits, so `compare` and
    `check_invariants` cannot disagree about where a table index ends and a
    cell begins.
    """
    layer, _, rest = str(key or "").partition(":")
    address, _, name = rest.partition("#")
    return layer, address, name


def _identity(element: Element) -> str:
    """What makes two elements the same element: the hash, else the value.

    Hash first because a digest is identity and a rendered value is a preview
    cut at 80 characters -- two paragraphs that share an opening sentence would
    compare equal on the preview and be reported as unchanged.
    """
    return element.hash or element.value


def _render(element: Element) -> str:
    """A short `before`/`after` for one element."""
    return _short(element.value or (f"sha256:{element.hash[:12]}" if element.hash else ""))


def _declared(detail: Mapping[str, Any]) -> Dict[str, float]:
    """`column -> the total the table DECLARES`, ignoring what it adds up to.

    The two halves of a totals row answer different questions: what the
    document states (a figure, which may legitimately be edited) and whether
    the column agrees with it (arithmetic, which may not quietly stop holding).
    """
    return {column: entry["declared"]
            for column, entry in dict(detail.get("columns") or {}).items()}


def _render_total(detail: Mapping[str, Any]) -> str:
    """`declared X, column sums to Y` per totalled column. Both numbers, always.

    Both, because "the total changed" does not distinguish a corrected figure
    from a broken sum, and only the second is a regression. A reader who sees
    only one number has to open the document to find out which happened.
    """
    columns = dict(detail.get("columns") or {})
    if not columns:
        return "no totalled column"
    return "; ".join(
        f"c{column}: declared {entry['declared']:g}, column sums to {entry['computed']:g}"
        for column, entry in sorted(columns.items()))


# -- reading one document --------------------------------------------------


@dataclass
class _Parse:
    """What one document yielded, and the counts the coverage is built from."""

    elements: List[Element] = field(default_factory=list)
    links: Dict[str, int] = field(default_factory=dict)
    citations: Dict[str, int] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)
    blocks: int = 0
    headings: int = 0
    tables: int = 0
    numbers: int = 0
    structured: bool = False


def _parse_prose(text: str) -> _Parse:
    """Plain text or Markdown, as blocks, headings, tables and figures.

    The walk is deliberately line-based and small. What it must get right is
    the three boundaries that change what an element IS: a fenced code block
    (whose numbers are code, not figures), a table (whose cells are addressed
    per cell rather than as a paragraph), and a heading (which re-parents every
    figure after it). Everything else is a paragraph, and a paragraph is a
    hash.
    """
    out = _Parse()
    lines = str(text or "").split("\n")
    taken: Dict[str, int] = {}
    per_heading: Dict[str, int] = {}
    context = "document"
    buffer: List[str] = []
    fenced = False
    index = 0

    def _flush() -> None:
        nonlocal buffer
        body = "\n".join(buffer).strip()
        buffer = []
        if not body:
            return
        normalised = _norm(body)
        out.elements.append(Element(
            key=f"block:{out.blocks}",
            kind="block",
            hash=_sha_text(normalised),
            value=_short(normalised, BLOCK_PREVIEW_CHARS),
            detail={"index": out.blocks, "heading": context,
                    "chars": len(normalised)},
        ))
        out.blocks += 1
        _harvest(out, body, context, per_heading)

    while index < len(lines):
        line = lines[index]
        if _FENCE_RE.match(line):
            fenced = not fenced
            buffer.append(line)
            index += 1
            continue
        if fenced:
            buffer.append(line)
            index += 1
            continue
        if not line.strip():
            _flush()
            index += 1
            continue
        heading = _HEADING_RE.match(line)
        if heading is not None:
            _flush()
            slug = _slug(heading.group("text"), taken)
            out.elements.append(Element(
                key=f"heading:{slug}",
                kind="heading",
                hash=_sha_text(_norm(heading.group("text"))),
                value=_short(heading.group("text")),
                detail={"level": len(heading.group("level")),
                        "index": out.headings},
            ))
            out.headings += 1
            context = slug
            _harvest(out, heading.group("text"), context, per_heading)
            index += 1
            continue
        if ("|" in line and index + 1 < len(lines)
                and "|" in lines[index + 1]
                and _TABLE_SEPARATOR_RE.match(lines[index + 1])):
            _flush()
            rows, index = _collect_table(lines, index)
            out.elements.extend(_table_elements(out.tables, rows))
            out.tables += 1
            continue
        buffer.append(line)
        index += 1

    _flush()
    out.elements.extend(_reference_elements(out))
    return out


def _harvest(out: _Parse, body: str, context: str,
             per_heading: Dict[str, int]) -> None:
    """Figures, links and citations out of one piece of running text.

    Links and citations are removed from the text BEFORE the figures are read.
    A URL is full of digits and a footnote marker is one, and a figure layer
    that harvested them would report `example.com/2024` as a quantity that
    changed when the link moved -- a number nobody wrote about a fact nobody
    stated.
    """
    for match in _MD_LINK_RE.finditer(body):
        url = match.group("url").strip()
        out.links[url] = out.links.get(url, 0) + 1
    stripped = _MD_LINK_RE.sub(" ", body)
    for match in _BARE_URL_RE.finditer(stripped):
        url = match.group("url").strip().rstrip(".,;")
        out.links[url] = out.links.get(url, 0) + 1
    stripped = _BARE_URL_RE.sub(" ", stripped)
    for match in _CITATION_RE.finditer(stripped):
        key = match.group("key").strip()
        out.citations[key] = out.citations.get(key, 0) + 1
    stripped = _CITATION_RE.sub(" ", stripped)

    for match in _NUMBER_RE.finditer(stripped):
        value = _number(match.group("value"))
        if value is None:
            continue
        position = per_heading.get(context, 0)
        per_heading[context] = position + 1
        unit = (match.group("unit") or "").strip()
        currency = (match.group("currency") or "").strip()
        out.elements.append(Element(
            key=f"number:{context}#{position}",
            kind="number",
            value=_render_number(currency, match.group("value"), unit),
            detail={"value": value, "unit": unit, "currency": currency,
                    "heading": context,
                    # `lexical`: a regex found this in running prose. The
                    # comparison of two extracted figures is exact; the
                    # extraction is not, and the element says which is which.
                    "certainty": CERTAINTY[2]},
        ))
        out.numbers += 1


def _reference_elements(out: _Parse) -> List[Element]:
    """One element per distinct link and citation, with how often it appeared.

    Distinct and not per occurrence: a URL cited three times is one source, and
    three elements at one address would have alignment comparing a reference
    against itself. The count lives in `detail`, where a reader can see that a
    link went from three mentions to one without that reading as a removal.
    """
    # A reference read out of a JSON value was parsed; one found by a regex in
    # running prose was not. The word is `code_index`'s `CERTAINTY`, so the two
    # indexes of this repository grade their evidence on one scale, and it is
    # what decides the tier of every finding built on this element.
    certainty = CERTAINTY[0] if out.structured else CERTAINTY[2]
    elements: List[Element] = []
    for url in sorted(out.links):
        elements.append(Element(key=f"link:{url}", kind="link",
                                value=_short(url),
                                detail={"occurrences": out.links[url],
                                        "certainty": certainty}))
    for key in sorted(out.citations):
        elements.append(Element(key=f"citation:{key}", kind="citation",
                                value=_short(key),
                                detail={"occurrences": out.citations[key],
                                        "certainty": certainty}))
    return elements


def _collect_table(lines: Sequence[str], start: int) -> Tuple[List[List[str]], int]:
    """The rows of one pipe table, and the line after it.

    Row 0 is the header. The separator line is consumed and not stored: it is
    the format's own punctuation, and an element for it would be a cell nobody
    wrote that changes whenever somebody realigns the pipes.
    """
    rows: List[List[str]] = [_cells(lines[start])]
    index = start + 2
    while index < len(lines) and "|" in lines[index] and lines[index].strip():
        rows.append(_cells(lines[index]))
        index += 1
    return rows, index


def _cells(line: str) -> List[str]:
    """One table row as cells, without the outer pipes."""
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _cell_number(cell: str) -> Optional[float]:
    """A cell as a number, or `None` when the whole cell is not one.

    Anchored on purpose: "about 12" and "12 (est.)" are not quantities this
    module may sum, and adding them as 12 would produce a recomputed total that
    disagrees with the document for a reason the document never gave.
    """
    matched = _CELL_NUMBER_RE.match(str(cell or ""))
    if matched is None:
        return None
    return _number(matched.group("value"))


def _totals(rows: Sequence[Sequence[str]]) -> Optional[Dict[str, Any]]:
    """The declared totals of a table and what the column actually adds up to.

    Both numbers are kept. A finding that reported only "the total changed"
    would not distinguish a corrected figure from a broken sum, and only the
    second is a regression: §10 asks for totals to be validated
    deterministically, which means recomputing them on BOTH sides and comparing
    each declared total against its own column.

    A column with one non-numeric cell above the totals row is not summed and
    is listed as unsummed. Skipping a cell would produce a total that is
    arithmetically wrong about a table that is fine.
    """
    if len(rows) < 3:
        return None
    label_row = -1
    for index in range(1, len(rows)):
        first = re.sub(r"[^a-z]", "", _norm(rows[index][0]).lower()) if rows[index] else ""
        if first in TOTAL_ROW_LABELS:
            label_row = index
    if label_row < 2:
        return None
    data = rows[1:label_row]
    columns: Dict[str, Any] = {}
    unsummed: List[str] = []
    for column in range(1, max(len(row) for row in rows)):
        declared = _cell_number(rows[label_row][column]) if column < len(rows[label_row]) else None
        if declared is None:
            continue
        values = [_cell_number(row[column]) if column < len(row) else None for row in data]
        if not values or any(value is None for value in values):
            unsummed.append(f"c{column}")
            continue
        columns[str(column)] = {
            "declared": declared,
            "computed": round(sum(value for value in values if value is not None), 10),
        }
    if not columns:
        return None
    balanced = all(abs(entry["declared"] - entry["computed"]) < 1e-9
                   for entry in columns.values())
    return {"row": label_row, "columns": columns, "balanced": balanced,
            "unsummed": unsummed}


def _table_elements(index: int, rows: Sequence[Sequence[str]]) -> List[Element]:
    """Cells, and the totals row when the table has one.

    Cells carry no hash. A table is full of repeated values -- "0", "yes", a
    year -- and `alignment.by_hash` groups on the digest, so hashing cells
    would turn every column of repeats into a bucket of identical candidates
    and produce `uncertain` rows about cells that never moved. A cell's
    identity is its coordinate.
    """
    elements: List[Element] = []
    for row_index, row in enumerate(rows):
        for column_index, cell in enumerate(row):
            elements.append(Element(
                key=f"table:{index}#r{row_index}c{column_index}",
                kind="cell",
                value=_short(cell),
                detail={"row": row_index, "column": column_index,
                        "header": row_index == 0},
            ))
    totals = _totals(rows)
    if totals is None:
        return elements
    rendered = "; ".join(
        f"c{column}={entry['declared']:g}"
        for column, entry in sorted(totals["columns"].items()))
    elements.append(Element(
        key=f"table:{index}#total",
        kind="total",
        value=_short(rendered),
        detail=totals,
    ))
    return elements


def _parse_structured(data: Any) -> _Parse:
    """A JSON or YAML document as addressed leaves.

    Blocks are addressed by their dotted PATH here rather than by a position,
    which is the plan's `block:<n>` spelled the way this shape can support: the
    n-th leaf of a mapping is not an address, since adding one key renumbers
    every leaf after it and the whole document would read as rewritten. The
    path is the address a structured document actually has.

    Numbers keep `certainty="exact"`: a JSON number was read by a parser, not
    found by a regex in prose, and the figure layer's confidence should say so.
    """
    out = _Parse(structured=True)
    for path, value in _leaves(data):
        if isinstance(value, bool) or value is None:
            rendered = json.dumps(value)
            out.elements.append(Element(
                key=f"block:{path}", kind="block", hash=_sha_text(rendered),
                value=_short(rendered), detail={"path": path}))
            out.blocks += 1
        elif isinstance(value, (int, float)):
            out.elements.append(Element(
                key=f"number:{path}#0", kind="number", value=_short(f"{value:g}"),
                detail={"value": float(value), "unit": "", "currency": "",
                        "heading": path, "certainty": CERTAINTY[0]}))
            out.numbers += 1
        elif _is_url(str(value)):
            url = str(value).strip()
            out.links[url] = out.links.get(url, 0) + 1
        else:
            rendered = _norm(str(value))
            out.elements.append(Element(
                key=f"block:{path}", kind="block", hash=_sha_text(rendered),
                value=_short(rendered, BLOCK_PREVIEW_CHARS),
                detail={"path": path}))
            out.blocks += 1
    out.elements.extend(_reference_elements(out))
    return out


def _leaves(value: Any, prefix: str = "") -> Iterable[Tuple[str, Any]]:
    """`(dotted path, scalar)` for every leaf, in a deterministic order."""
    if isinstance(value, Mapping):
        for key in sorted(value, key=str):
            child = f"{prefix}.{key}" if prefix else str(key)
            yield from _leaves(value[key], child)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            child = f"{prefix}.{index}" if prefix else str(index)
            yield from _leaves(item, child)
    else:
        yield prefix or "<root>", value


# -- the adapter -----------------------------------------------------------


class DocumentAdapter:
    """The `document` domain. Implements the `DeltaAdapter` Protocol.

    What it reads: plain text, Markdown, JSON, and YAML when PyYAML is
    installed. What it refuses: everything else, by name. A PDF is not read
    here and is not silently decoded as text either -- `unreadable()` with the
    reason is a correct answer and a page of mojibake compared against another
    page of mojibake is not.
    """

    domain = "document"
    version = "1"

    def available(self) -> bool:
        """Always true: text, Markdown and JSON need nothing that can be missing.

        PyYAML is optional and its absence is reported in the coverage of the
        one comparison it affects, not as an unavailable domain: a registry
        that refused every document because one format has no reader would be a
        much larger answer than the facts justify.
        """
        return True

    # -- reading one end ---------------------------------------------------

    def snapshot(self, revision: RevisionRef, *, scope: Scope) -> Snapshot:
        """One document as blocks, headings, figures, references and tables.

        The three ways this can end without elements are kept apart, because
        they call for different actions: the revision could not be resolved
        (`sources` says why), the bytes are not text this module reads (it says
        so, naming the format), or the text is there and holds nothing
        addressable (a readable snapshot with zero elements and a note).
        """
        resolved = sources.resolve(revision, scope=scope)
        if not resolved.readable:
            return unreadable(revision, resolved.reason, tier="parser")

        raw = resolved.data()
        name = self._name(revision, scope=scope)
        text = self._decode(raw)
        if text is None:
            return unreadable(revision, self._why_not_text(name, resolved.media_type),
                              tier="parser")

        notes: List[str] = []
        excluded: List[str] = []
        if resolved.truncated:
            excluded.append(resolved.reason)
            notes.append("only part of this document was read, so the blocks "
                         "past the cut were never extracted and their absence "
                         "is not evidence that they are gone")
        parse = self._parse(text, name=name, media_type=resolved.media_type,
                            notes=notes)

        elements, cut = self._budgeted(parse.elements, scope=scope)
        if cut:
            excluded.append(cut)
        detail = dict(resolved.to_dict())
        detail.update({
            "name": name, "structured": parse.structured,
            "blocks": parse.blocks, "headings": parse.headings,
            "tables": parse.tables, "numbers": parse.numbers,
            "links": len(parse.links), "citations": len(parse.citations),
        })
        if not elements:
            notes.append("no block, figure, reference or table was extracted "
                         "from this document; it was readable and empty of "
                         "anything this adapter addresses")
        return Snapshot(
            revision=revision,
            elements=elements,
            readable=True,
            tier="parser",
            excluded=tuple(_unique(excluded)),
            notes=tuple(_unique(notes + parse.notes)),
            truncated=bool(cut or resolved.truncated),
            detail=detail,
        )

    def _decode(self, raw: bytes) -> Optional[str]:
        """The bytes as text, or `None` when they are not text.

        Strict UTF-8 and a NUL check. `errors="replace"` would turn a PDF into
        a page of replacement characters, hash it as a block and compare it
        against another one -- a `modified` row about a document nobody read.
        """
        if b"\x00" in raw:
            return None
        try:
            return raw.decode("utf-8", "strict")
        except UnicodeDecodeError:
            return None

    def _why_not_text(self, name: str, media_type: str) -> str:
        """The reason an unreadable document is unreadable, naming the format.

        The format is named because the next action differs: a PDF needs the
        page-render layer §10 describes and this phase does not have, while an
        unexpected binary in a document scope is usually a routing mistake.
        """
        suffix = os.path.splitext(name)[1].lower()
        if suffix == ".pdf" or "pdf" in str(media_type or "").lower():
            return ("this is a PDF: it is not text and this adapter does not "
                    "render pages. Per-page rendering and region comparison are "
                    "a later phase, and decoding the bytes as text here would "
                    "compare two pages of mojibake and report a difference "
                    "between them")
        return (f"the bytes of {name or 'this revision'} are not UTF-8 text "
                f"(media type {media_type or 'unknown'}); this adapter reads "
                f"plain text, Markdown, JSON and YAML, and refusing is the only "
                f"answer that does not invent content")

    def _name(self, revision: RevisionRef, *, scope: Scope) -> str:
        """The document's name, used for the format decision and for messages.

        Never for the element addresses: a document's elements are addressed
        inside it, so two ends carrying different filenames still align. The
        name only decides which reader runs, and a wrong guess there is visible
        as a note rather than as a delta full of `added` rows.
        """
        ref = revision.ref or ""
        if revision.kind == "checkpoint":
            matched = sources.CHECKPOINT_REF.match(ref)
            if matched:
                return matched.group("path")
        if revision.kind == "file":
            return ref
        label = str(revision.label or "").strip()
        if label and not any(char.isspace() for char in label):
            return label
        return LITERAL_NAME

    def _parse(self, text: str, *, name: str, media_type: str,
               notes: List[str]) -> _Parse:
        """Structured or prose, decided by the name and the media type.

        A file that CLAIMS to be JSON and does not parse is read as prose with
        a note, rather than refused: the text is still there and its blocks
        still compare, and refusing would turn a trailing comma into "the
        document could not be read".
        """
        suffix = os.path.splitext(name)[1].lower()
        media = str(media_type or "").lower()
        if suffix in JSON_SUFFIXES or "json" in media:
            try:
                loaded = json.loads(text)
            except ValueError as exc:
                logger.info("document adapter: %s is named as JSON and does "
                            "not parse (%s); reading it as prose", name, exc)
                notes.append(f"this document is named as JSON and does not "
                             f"parse ({exc}); it was read as prose instead")
            else:
                if isinstance(loaded, (Mapping, list)):
                    return _parse_structured(loaded)
                notes.append("this JSON document is a bare scalar; it was read "
                             "as prose, which is the same comparison")
        elif suffix in YAML_SUFFIXES or "yaml" in media:
            if _yaml is None:
                notes.append("PyYAML is not installed here, so this YAML "
                             "document was compared as prose: its blocks and "
                             "figures are addressed, its keys are not")
            else:
                try:
                    loaded = _yaml.safe_load(text)
                except Exception as exc:  # noqa: BLE001 - a broken YAML is a note
                    logger.info("document adapter: %s does not parse as YAML "
                                "(%s); reading it as prose", name, exc)
                    notes.append(f"this YAML document does not parse ({exc}); "
                                 f"it was read as prose instead")
                else:
                    if isinstance(loaded, (Mapping, list)):
                        return _parse_structured(loaded)
        return _parse_prose(text)

    def _budgeted(self, elements: Sequence[Element], *,
                  scope: Scope) -> Tuple[Tuple[Element, ...], str]:
        """The elements, cut to `budget.max_elements`, and the sentence for it.

        The cut falls at the end of the document in reading order, so what is
        missing is a contiguous tail a reader can name, rather than a sample
        from everywhere that makes the whole document look half-compared.
        """
        limit = scope.budget.max_elements
        if limit is None or len(elements) <= limit:
            return tuple(elements), ""
        dropped = len(elements) - limit
        return tuple(elements[:limit]), (
            f"{dropped} element(s) from the end of the document were not "
            f"addressed; `budget.max_elements` ({limit}) stopped the snapshot "
            f"and nothing past that point was compared")


    # -- comparing two ends ------------------------------------------------

    def _align(self, source: Snapshot, target: Snapshot, *, scope: Scope
               ) -> Tuple[Tuple[Tuple[Optional[Element], Optional[Element], Alignment], ...],
                          bool, float]:
        """Content first, address second. The inversion §10's trap requires.

        `alignment.align` runs by_key before by_hash, which is right wherever
        an address is an identity -- a file path, a JSON pointer, a symbol. A
        block's address is its POSITION, and a position is not an identity for
        a paragraph: pairing `block:0` with `block:0` after a reflow compares
        two unrelated paragraphs and reports both as rewritten.

        So the same two public steps of that module are composed in the order
        this domain needs. `by_hash` is written for exactly this call -- it
        answers `same` rather than `moved` when the two keys are identical, so
        running it first cannot invent a move -- and `by_key` then sees only
        what content could not decide. Nothing is reimplemented; the ladder is
        the same ladder, in the order the evidence supports here.
        """
        left_all, right_all = source.index(), target.index()
        limit = scope.budget.max_elements or 5000
        keys = sorted(set(left_all) | set(right_all))
        truncated = len(keys) > limit
        kept = set(keys[:limit]) if truncated else set(keys)
        left = {key: value for key, value in left_all.items() if key in kept}
        right = {key: value for key, value in right_all.items() if key in kept}

        considered = len(left) + len(right)
        pairs: List[Tuple[Optional[Element], Optional[Element], Alignment]] = []
        moved, left, right = alignment_mod.by_hash(left, right)
        pairs.extend(moved)
        matched, left, right = alignment_mod.by_key(left, right)
        pairs.extend(matched)
        for key in sorted(left):
            pairs.append((left[key], None, self._alignment("missing", key, "")))
        for key in sorted(right):
            pairs.append((None, right[key], self._alignment("added", "", key)))

        total = len(source.elements) + len(target.elements)
        ratio = 1.0 if total == 0 else round(considered / total, 4)
        return tuple(pairs), truncated, ratio

    def _alignment(self, relation: str, source_key: str, target_key: str) -> Alignment:
        """One `Alignment`, built THROUGH its parser and never by keyword.

        `Alignment.parse` is where an `uncertain` relation has its confidence
        pulled down and where a `same` missing one of its ends is refused. A
        module that built the dataclass directly would be a second door into
        the contract with none of the rules behind it.
        """
        payload: Dict[str, Any] = {"relation": relation, "confidence": "exact",
                                   "method": "element_key", "tier": "parser"}
        if source_key:
            payload["source_element"] = source_key
        if target_key:
            payload["target_element"] = target_key
        return Alignment.parse(payload, "alignment")

    def compare(self, source: Snapshot, target: Snapshot, *,
                scope: Scope) -> Extraction:
        """What differs between two documents, one addressed element at a time.

        An unreadable end produces no findings and a coverage that says which
        end failed. "We could not extract the text" and "the text is gone" are
        opposite facts, and a list of `missing` rows says the second.
        """
        limitations: List[str] = [
            "structure, figures, references and table arithmetic are compared. "
            "No model read this document, so nothing here is a claim about what "
            "it says -- only about what it contains and what adds up",
        ]
        if not (source.readable and target.readable):
            which = "source" if not source.readable else "target"
            limitations.append(
                f"the {which} revision could not be read as text, so nothing "
                f"was compared; this is not a report that nothing changed")
            return Extraction(
                findings=(),
                coverage=self._coverage(source, target, aligned=0.0, ratio=0.0,
                                        readable=False, regions=()),
                invariants=(),
                extractor_versions=self._versions(),
                limitations=tuple(_unique(limitations)),
            )

        pairs, truncated, ratio = self._align(source, target, scope=scope)
        findings: List[Finding] = []
        regions: List[str] = []
        paired = 0
        for left, right, relation in pairs:
            if left is not None and right is not None:
                paired += 2
            for element in (left, right):
                if element is not None:
                    regions.append(element.key)
            found = self._finding_for(left, right, relation)
            if found is not None:
                findings.append(found)

        total = len(source.elements) + len(target.elements)
        aligned = 0.0 if total == 0 else round(paired / total, 4)
        if truncated:
            limitations.append(
                "the alignment was cut by `budget.max_elements`, so some "
                "elements were never considered and their absence from this "
                "list is not evidence that they did not change")
        if _yaml is None:
            limitations.append("PyYAML is not installed here; a YAML document "
                               "in this comparison was read as prose")

        return Extraction(
            findings=tuple(findings),
            coverage=self._coverage(source, target, aligned=aligned, ratio=ratio,
                                    readable=True, regions=tuple(_unique(regions))),
            invariants=(),
            extractor_versions=self._versions(),
            limitations=tuple(_unique(limitations)),
        )

    def _versions(self) -> Dict[str, str]:
        """The §22 cache key's extractor half. The YAML reader is part of it.

        A document read as prose because PyYAML was missing is a different
        extraction from the same document read as a mapping, and a cache that
        could not tell them apart would keep answering with the weaker one
        after the library was installed.
        """
        return {self.domain: self.version,
                "yaml": getattr(_yaml, "__version__", "absent") if _yaml else "absent"}


    def _method_for(self, element: Element) -> Tuple[str, str]:
        """`(method, tier)` for one element.

        `text_extract` sits at tier `parser` in `confidence.METHOD_TIERS` --
        "decoders reading the format's own structure" -- and that is what a
        blank line, a pipe row and a JSON value are. A figure or a reference
        found by a regex in RUNNING PROSE is not: those elements carry
        `certainty="lexical"` and their findings are demoted to `algorithm`.
        Demotion against the table is always allowed; promotion is what §3.2
        forbids, and nothing here ever claims a tier stronger than the table's.
        """
        layer, _address, _name = _key_parts(element.key)
        certainty = str(dict(element.detail or {}).get("certainty") or "")
        method = "content_hash" if layer in ("block", "heading") and element.hash else "text_extract"
        tier = confidence_mod.tier_of(method)
        if layer in ("number", "link", "citation") and certainty == CERTAINTY[2]:
            tier = "algorithm"
        return method, tier

    def _finding_for(self, left: Optional[Element], right: Optional[Element],
                     relation: Alignment) -> Optional[Finding]:
        """One observation about one aligned pair. Never a classification.

        `unchanged` is emitted rather than skipped, for the reason rule 1 of
        `contracts.py` gives: it is what a later layer needs before it may say
        `preserved`, and without it the honest answer about an untouched
        paragraph is `unknown`.
        """
        element = left if left is not None else right
        if element is None:  # pragma: no cover - a pair always has one end
            return None
        layer, _address, name = _key_parts(element.key)
        method, tier = self._method_for(element)
        shared: Dict[str, Any] = {
            "method": method, "tier": tier,
            "confidence": confidence_mod.propagate("exact", alignment=relation),
            "alignment": relation, "element_kind": element.kind,
        }

        if relation.relation == "moved" and left is not None and right is not None:
            # §10's trap, answered. Reordering a paragraph is not losing it and
            # gaining another; a repagination that read as `missing` + `added`
            # would report a document as having lost everything it contains.
            return Finding(path=right.key, operation="moved",
                           before=left.key, after=right.key,
                           detail="identical content at a different position; "
                                  "reflow is not deletion",
                           **{**shared, "method": "content_hash", "tier": "hash"})

        if left is not None and right is not None:
            if layer == "table" and name == "total":
                return self._total_finding(left, right, shared)
            if layer == "number":
                return self._number_finding(left, right, shared)
            if _identity(left) == _identity(right):
                return Finding(path=element.key, operation="unchanged",
                               before=_render(left), after=_render(right), **shared)
            return Finding(path=element.key, operation="modified",
                           before=_render(left), after=_render(right), **shared)

        if left is not None:
            return Finding(path=left.key, operation="missing",
                           before=_render(left),
                           invariant_refs=self._refs_for(layer),
                           detail=self._loss_detail(layer), **shared)
        return Finding(path=right.key, operation="added", after=_render(right),
                       **shared)

    def _refs_for(self, layer: str) -> Tuple[str, ...]:
        """The invariant a disappearance points at, by layer.

        §10, literally: a citation or a link that disappears is materially
        different even when the prose around it barely moved. Pointing the
        finding at the provenance invariant is what carries that materiality
        through the classifier, which is the only place a severity is decided.
        """
        if layer in ("link", "citation"):
            return (CITATIONS_INVARIANT,)
        if layer == "number":
            return (FIGURES_INVARIANT,)
        return ()

    def _loss_detail(self, layer: str) -> str:
        """Why this disappearance is worth its own row."""
        if layer in ("link", "citation"):
            return ("a source the document leaned on is no longer referenced; "
                    "similar prose around it is not evidence that it is still "
                    "there")
        if layer == "number":
            return "a stated figure is no longer stated"
        return ""

    def _number_finding(self, left: Element, right: Element,
                        shared: Mapping[str, Any]) -> Finding:
        """A figure at the same address, compared as a NUMBER and not as text.

        `1000` and `1,000` are one quantity written twice, and a comparison of
        the two strings would report a formatting pass as a changed fact. What
        makes a figure `material` is the reference to the content invariant,
        not the wording of this row: rewriting a paragraph and rewriting a
        quantity are different events and only one of them changes what the
        document asserts.
        """
        before = dict(left.detail or {})
        after = dict(right.detail or {})
        same = (before.get("value") == after.get("value")
                and before.get("unit") == after.get("unit")
                and before.get("currency") == after.get("currency"))
        if same:
            return Finding(path=left.key, operation="unchanged",
                           before=_render(left), after=_render(right), **shared)
        return Finding(path=left.key, operation="modified",
                       before=_render(left), after=_render(right),
                       invariant_refs=(FIGURES_INVARIANT,),
                       detail="a stated figure changed", **shared)

    def _total_finding(self, left: Element, right: Element,
                       shared: Mapping[str, Any]) -> Finding:
        """The declared total against the column, on both sides. §10's arithmetic.

        This is the one finding here that claims `exact`, and the claim is
        about addition: the column was re-added on both ends and compared with
        what the table says. Nothing was estimated and no threshold was chosen,
        so two runs of this check cannot disagree.

        The tier is `parser` and not `algorithm`, which is a departure from the
        plan's wording and is forced by the contract: `TIER_CEILING` caps an
        `algorithm` finding at `high`, so "tier `algorithm`, confidence `exact`"
        cannot be expressed at all. `exact` is the half worth keeping -- the
        plan's own reason is that arithmetic is not an opinion -- and the tier
        that carries it is the one whose method actually ran: the cells were
        read from the format's own delimiters, which `METHOD_TIERS` files as
        `text_extract` at `parser`. Nothing is promoted above its method here.
        """
        before = dict(left.detail or {})
        after = dict(right.detail or {})
        rendered_before, rendered_after = _render_total(before), _render_total(after)
        base = {**dict(shared), "method": "text_extract", "tier": "parser",
                "confidence": "exact"}
        if bool(before.get("balanced")) and not bool(after.get("balanced")):
            return Finding(
                path=left.key, operation="modified",
                before=rendered_before, after=rendered_after,
                invariant_refs=(FIGURES_INVARIANT,),
                detail="the declared total no longer equals the sum of its "
                       "column; the column was re-added on both sides",
                **base)
        limitations: Tuple[str, ...] = ()
        if not bool(after.get("balanced")):
            limitations = ("the declared total does not equal the sum of its "
                           "column on either side, so this row says nothing "
                           "about which of the two is wrong",)
        if rendered_before == rendered_after:
            return Finding(path=left.key, operation="unchanged",
                           before=rendered_before, after=rendered_after,
                           limitations=limitations, **base)
        return Finding(path=left.key, operation="modified",
                       before=rendered_before, after=rendered_after,
                       invariant_refs=(FIGURES_INVARIANT,),
                       detail="a declared total changed",
                       limitations=limitations, **base)


    # -- how much of it we actually compared -------------------------------

    def _read_fraction(self, snapshot: Snapshot) -> float:
        """How much of one end's bytes were actually read, 0..1.

        Read from the snapshot's own detail rather than recomputed, so the
        number in the coverage is the number the resolver reported. A document
        with no size counts as fully read: nothing was left out of it.
        """
        detail = dict(snapshot.detail or {})
        size = int(detail.get("size") or 0)
        read = int(detail.get("read_bytes") or 0)
        if size <= 0:
            return 1.0
        return max(0.0, min(1.0, round(read / size, 4)))

    def _coverage(self, source: Snapshot, target: Snapshot, *, aligned: float,
                  ratio: float, readable: bool, regions: Sequence[str]) -> Coverage:
        """Two axes, and a note about the one nobody measured.

        `structural` is the fraction of elements that found a partner, lowered
        by whatever the budget kept alignment from considering. `semantic` is
        the fraction of the bytes that were extracted at all -- a document read
        only to its budget has text nobody compared, and §10's "texto no
        extraíble" is exactly that number being below one.

        `behavioral` is NOT reported. `Coverage.ratio` distinguishes "not
        measured" from "measured and covered none", and a document does not
        run: a 0.0 there would claim this adapter looked for behaviour and
        found none, which is a different and false statement.
        """
        structural = round(min(aligned, ratio), 4) if readable else 0.0
        semantic = 0.0
        if readable:
            semantic = round(min(self._read_fraction(source),
                                 self._read_fraction(target)), 4)
        notes = [
            "no model read either document: nothing in this comparison is a "
            "claim about what the text means, only about the blocks, figures, "
            "references and totals it contains",
        ]
        if not readable:
            which = "source" if not source.readable else "target"
            notes.append(f"the {which} revision could not be read as text, so "
                         f"none of it was compared")
        if semantic < 1.0 and readable:
            notes.append(f"only {semantic:g} of the bytes were extracted; the "
                         f"rest of the text was never compared and its content "
                         f"is unknown, not preserved")
        listed = tuple(regions)[:MAX_LISTED_REGIONS]
        if len(regions) > len(listed):
            notes.append(f"{len(regions) - len(listed)} further element keys "
                         f"were compared and are not listed here")
        return coverage_mod.build(
            source_readable=source.readable,
            target_readable=target.readable,
            dimensions={"structural": structural, "semantic": semantic},
            regions=listed,
            excluded=_unique(list(source.excluded) + list(target.excluded)),
            notes=_unique(notes + list(source.notes) + list(target.notes)),
        )

    # -- invariants --------------------------------------------------------

    def _by_layer(self, snapshot: Snapshot, layers: Sequence[str]) -> Dict[str, Element]:
        """The elements of one end whose key belongs to one of `layers`."""
        return {key: element for key, element in snapshot.index().items()
                if _key_parts(key)[0] in layers}

    def _lost_references(self, source: Snapshot,
                         target: Snapshot) -> Tuple[str, ...]:
        """Links and citations in the source that are absent from the target.

        By address and not by resemblance: a URL is its own identity, and a
        citation key is the one the document used. Nothing here compares the
        prose around them, because §10's whole point is that similar prose is
        not evidence that the reference survived.
        """
        after = self._by_layer(target, ("link", "citation"))
        return tuple(key for key in sorted(self._by_layer(source, ("link", "citation")))
                     if key not in after)

    def _changed_figures(self, source: Snapshot,
                         target: Snapshot) -> Tuple[str, ...]:
        """Figures at the same address whose quantity or unit is not the same.

        A DECLARED table total counts as a figure here, because that is what it
        is: a number the document states. It is kept apart from
        `_broken_totals`, which is about the arithmetic rather than about the
        quantity -- an edited table whose total was updated to match is a
        changed figure and not a broken sum, and a check that could not tell
        those apart would either cry regression at every edited table or miss
        the sums that stopped holding.
        """
        after = self._by_layer(target, ("number",))
        out: List[str] = []
        for key, element in sorted(self._by_layer(source, ("number",)).items()):
            other = after.get(key)
            if other is None:
                continue
            before, now = dict(element.detail or {}), dict(other.detail or {})
            if (before.get("value") != now.get("value")
                    or before.get("unit") != now.get("unit")
                    or before.get("currency") != now.get("currency")):
                out.append(key)
        after_totals = self._by_layer(target, ("table",))
        for key, element in sorted(self._by_layer(source, ("table",)).items()):
            if _key_parts(key)[2] != "total":
                continue
            other = after_totals.get(key)
            if other is None:
                continue
            if _declared(element.detail) != _declared(other.detail):
                out.append(key)
        return tuple(out)

    def _broken_totals(self, source: Snapshot,
                       target: Snapshot) -> Tuple[str, ...]:
        """Tables whose declared total stopped equalling its column.

        A total that never added up on either side is NOT listed: this reports
        a regression, and a document that shipped with a wrong sum did not
        acquire one here. It is still visible as a limitation on the finding.
        """
        before = self._by_layer(source, ("table",))
        out: List[str] = []
        for key, element in sorted(self._by_layer(target, ("table",)).items()):
            if _key_parts(key)[2] != "total":
                continue
            if bool(dict(element.detail or {}).get("balanced")):
                continue
            older = before.get(key)
            if older is None or bool(dict(older.detail or {}).get("balanced")):
                out.append(key)
        return tuple(out)


    def check_invariants(self, intent: IntentContract, source: Snapshot,
                         target: Snapshot, *,
                         scope: Scope) -> Tuple[InvariantResult, ...]:
        """One result per declared invariant, and three ways to say more than
        `unknown`.

        * **provenance** -- a link or a citation the source had and the target
          does not: `violated`. Extracted by a regex over prose, so tier
          `algorithm` and confidence `high` at best; extracted from JSON, the
          same comparison is a parser's.
        * **content** -- a figure that changed, or a total that stopped adding
          up. The second is `exact`: it is arithmetic performed on both sides.
        * **budget** -- whether a snapshot or a read was cut. A fact about this
          run rather than about the document.

        Everything else is `unknown` with the limitation named, and so is
        EVERYTHING when a document could not be extracted -- §10's "texto no
        extraíble produce unknown", which is rule 1 of `contracts.py` in the
        one place a document adapter is most tempted to break it: a page whose
        text nobody could read has not been shown to say the same thing.
        """
        readable = source.readable and target.readable
        complete = bool(readable and not source.truncated and not target.truncated)
        lost = self._lost_references(source, target)
        changed = self._changed_figures(source, target)
        broken = self._broken_totals(source, target)
        gap = self._gap(source, target)

        results: List[InvariantResult] = []
        for invariant in intent.invariants:
            payload: Dict[str, Any] = {"invariant_id": invariant.id,
                                       "severity": invariant.severity}
            if not readable:
                which = "source" if not source.readable else "target"
                payload.update({
                    "status": "unknown", "confidence": "unknown",
                    "tier": "parser", "method": "text_extract",
                    "limitations": [
                        f"the text of the {which} revision could not be "
                        f"extracted, so no property of it was observed; not "
                        f"extracted is not preserved"],
                })
            elif invariant.klass == "budget":
                payload.update(self._budget_result(source, target))
            elif invariant.klass == "provenance":
                payload.update(self._provenance_result(lost, source, complete=complete,
                                                       gap=gap))
            elif invariant.klass == "content":
                payload.update(self._content_result(changed, broken, source,
                                                    complete=complete, gap=gap))
            else:
                payload.update({
                    "status": "unknown", "confidence": "unknown",
                    "tier": "parser", "method": "text_extract",
                    "limitations": [
                        f"this adapter has no method for a `{invariant.klass}` "
                        f"property; it addresses blocks, headings, figures, "
                        f"references and tables, and nothing here observed that "
                        f"property"],
                })
            results.append(InvariantResult.parse(
                payload, f"invariant_result[{invariant.id}]"))
        return tuple(results)

    def _gap(self, source: Snapshot, target: Snapshot) -> str:
        """The one sentence naming why a check could not be complete."""
        if source.truncated or target.truncated:
            return ("a budget cut at least one document, so the text past the "
                    "cut was never extracted and nothing about it was checked")
        return ""

    def _budget_result(self, source: Snapshot, target: Snapshot) -> Dict[str, Any]:
        """Whether the comparison stayed inside its budget. Ours, and exact."""
        excluded = _unique(list(source.excluded) + list(target.excluded))
        if source.truncated or target.truncated:
            return {
                "status": "violated", "confidence": "exact", "tier": "parser",
                "method": "element_key",
                "observations": [_short(item, 480) for item in excluded][:8]
                                or ["a snapshot was truncated"],
            }
        return {
            "status": "preserved", "confidence": "exact", "tier": "parser",
            "method": "element_key",
            "observations": [
                f"neither document was truncated: {len(source.elements)} source "
                f"and {len(target.elements)} target elements were addressed "
                f"within the budget"],
        }

    def _provenance_result(self, lost: Sequence[str], source: Snapshot, *,
                           complete: bool, gap: str) -> Dict[str, Any]:
        """Links and citations, which are provenance and not prose."""
        if lost:
            return {
                "status": "violated", "confidence": "high", "tier": "algorithm",
                "method": "text_extract",
                "observations": [_short(f"no longer referenced: {key}", 480)
                                 for key in lost][:8],
                "limitations": [
                    "references are found by pattern in the text: a citation "
                    "written in a form this adapter does not recognise was "
                    "never counted on either side"] + ([gap] if gap else []),
            }
        if not complete:
            return {
                "status": "unknown", "confidence": "unknown", "tier": "algorithm",
                "method": "text_extract",
                "limitations": [gap or "the comparison was not complete"],
            }
        references = len(self._by_layer(source, ("link", "citation")))
        return {
            "status": "preserved", "confidence": "high", "tier": "algorithm",
            "method": "text_extract",
            "observations": [
                f"{references} distinct link(s) and citation(s) in the source; "
                f"every one of them is still referenced in the target"],
            "limitations": [
                "a reference written in a form this adapter does not recognise "
                "was never counted on either side"],
        }

    def _content_result(self, changed: Sequence[str], broken: Sequence[str],
                        source: Snapshot, *, complete: bool,
                        gap: str) -> Dict[str, Any]:
        """Figures and totals: what the document ASSERTS, as opposed to how it
        words it.

        A broken total is reported at tier `parser` and confidence `exact`
        because the column was re-added on both sides; a changed figure is
        `high` when the figures came out of prose, since a regex found them.
        Both are the same invariant and the strongest evidence in play names
        its own tier rather than the weakest -- the observations say which is
        which.
        """
        if broken or changed:
            observations = ([_short(f"the declared total no longer equals its "
                                    f"column: {key}", 480) for key in broken][:8]
                            + [_short(f"figure changed: {key}", 480)
                               for key in changed][:8])
            arithmetic = bool(broken)
            return {
                "status": "violated",
                "confidence": "exact" if arithmetic and not changed else "high",
                "tier": "parser" if arithmetic and not changed else "algorithm",
                "method": "text_extract",
                "observations": observations,
                "limitations": [gap] if gap else [],
            }
        if not complete:
            return {
                "status": "unknown", "confidence": "unknown", "tier": "algorithm",
                "method": "text_extract",
                "limitations": [gap or "the comparison was not complete"],
            }
        figures = len(self._by_layer(source, ("number",)))
        return {
            "status": "preserved", "confidence": "high", "tier": "algorithm",
            "method": "text_extract",
            "observations": [
                f"{figures} figure(s) in the source; every one of them is "
                f"stated identically in the target",
                "every declared table total that added up in the source still "
                "adds up in the target"],
            "limitations": [
                "a figure written in words rather than digits was never "
                "extracted on either side"],
        }


#: How `registry.py` finds this adapter. A module-level factory and NOT a line
#: in a tuple somewhere else -- the tuple is what once made five correct
#: adapters invisible to every sweep, with green tests and no warning. Writing
#: this line IS the registration.
ADAPTER_FACTORY = DocumentAdapter
