"""Numbered sources, citation repair and evidence grading for deep research.

Deterministic and pure: no network, no LLM, no database. Everything here can be
re-run on the same input a year from now and produce the same bytes, which is
the only reason a citation number is worth printing at all.

Three jobs:

1. :class:`SourceRegistry` hands every page a number the first time its URL is
   seen and never takes it back, so a ``[4]`` written during round two still
   points at the same page in the report written after round eight.
2. :func:`audit_citations` / :func:`repair_citations` parse the markers the
   model actually wrote, delete the ones that point at nothing, fold the second
   citation dialect (``[text](url)``) into the first, and print a sources list
   containing exactly the sources that were cited.
3. :func:`grade_claims` asks :mod:`src.claim_verify` whether the cited source's
   own text supports each cited sentence.

The honesty rule that shapes (3): the grade says *whether the source we stored
says this*. It says nothing about whether the claim is true, and nothing about
the quality of the study behind it. A report that printed "high evidence"
without that caveat would be making a claim about its own reliability that
nothing in this file could support, so :func:`build_legend` prints the caveat
in the same breath as the counts and is generated here rather than by a model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Grades are stable machine values, deliberately the Spanish words the spec
# fixed; the words a reader sees are localised in GRADE_WORDS below.
GRADE_HIGH = "alta"
GRADE_MEDIUM = "moderada"
GRADE_WEAK = "débil"
GRADES = (GRADE_HIGH, GRADE_MEDIUM, GRADE_WEAK)

MAX_TEXT_CHARS = 2_000_000
MAX_SOURCE_NUMBER = 999

# Query parameters that identify a campaign, not a page. Dropping them is what
# makes the same article arriving from two different search providers one
# numbered source instead of two.
_TRACKING_PARAMS = frozenset({
    "fbclid", "gclid", "gclsrc", "dclid", "msclkid", "mc_cid", "mc_eid",
    "igshid", "ref_src", "ref_url", "yclid", "twclid", "wbraid", "gbraid",
    "_hsenc", "_hsmi", "vero_id", "vero_conv", "s_cid", "spm",
})

_DEFAULT_PORTS = {"http": "80", "https": "443", "ftp": "21"}


# ---------------------------------------------------------------------------
# Text hygiene
# ---------------------------------------------------------------------------


def _as_text(value: Any) -> str:
    """Anything into a bounded string. This module runs at the end of a long,
    expensive research job — raising here would throw the whole run away."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return ""
    if not isinstance(value, str):
        try:
            value = str(value)
        except Exception:  # noqa: BLE001
            return ""
    return value[:MAX_TEXT_CHARS]


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------


def canonical_url(url: Any) -> str:
    """The identity of a page, for deciding whether two fetches are one source.

    Case-folds scheme and host, drops the default port, the fragment, a
    trailing slash and the usual tracking parameters, and sorts what remains so
    ``?a=1&b=2`` and ``?b=2&a=1`` are the same page. ``www.`` is deliberately
    NOT stripped: a handful of hosts really do serve different content there,
    and a wrong merge silently attributes a claim to a page that never made it.
    """
    raw = _as_text(url).strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw.lower()
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    if not host:
        # Not a URL we can take apart (a bare title, a file path) — fold case
        # so at least identical strings still collapse to one number.
        return raw.lower()
    netloc = host
    port = parts.port
    if port is not None and str(port) != _DEFAULT_PORTS.get(scheme, ""):
        netloc = f"{host}:{port}"
    path = parts.path or ""
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    if path == "/":
        path = ""
    try:
        pairs = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                 if not (k.lower().startswith("utm_") or k.lower() in _TRACKING_PARAMS)]
    except Exception:  # noqa: BLE001
        pairs = []
    query = urlencode(sorted(pairs))
    return urlunsplit((scheme, netloc, path, query, ""))


def domain_of(url: Any) -> str:
    """Host without ``www.`` or port — what the sources list prints."""
    raw = _as_text(url).strip()
    if not raw:
        return ""
    try:
        host = (urlsplit(raw).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


class SourceRegistry:
    """Stable 1-based numbers for the pages a research run actually read."""

    def __init__(self) -> None:
        self._entries: List[Dict[str, Any]] = []
        self._by_key: Dict[str, int] = {}

    def add(self, finding: Any) -> int:
        """Register a finding's URL and return its number.

        Returns the number it already had if the URL was seen before, filling
        in any field that was empty then and is populated now (a page first
        seen as a search hit and later extracted keeps its number and gains its
        text). Returns ``0`` for a finding with no usable URL — nothing to
        cite, and ``[0]`` is not a marker this module will ever parse.
        """
        if not isinstance(finding, dict):
            return 0
        url = _as_text(finding.get("url")).strip()
        key = canonical_url(url)
        if not key:
            return 0
        existing = self._by_key.get(key)
        if existing is not None:
            entry = self._entries[existing - 1]
            for field_name in ("title", "summary", "evidence"):
                if not entry.get(field_name):
                    entry[field_name] = _as_text(finding.get(field_name)).strip()
            return existing
        if len(self._entries) >= MAX_SOURCE_NUMBER:
            return 0
        number = len(self._entries) + 1
        self._entries.append({
            "n": number,
            "url": url,
            "title": _as_text(finding.get("title")).strip(),
            "summary": _as_text(finding.get("summary")).strip(),
            "evidence": _as_text(finding.get("evidence")).strip(),
            "domain": domain_of(url),
            "fetched_at": _as_text(finding.get("fetched_at")).strip() or _now(),
        })
        self._by_key[key] = number
        return number

    def number_for(self, url: Any) -> Optional[int]:
        return self._by_key.get(canonical_url(url))

    def source(self, n: Any) -> Optional[Dict[str, Any]]:
        try:
            index = int(n)
        except (TypeError, ValueError):
            return None
        if 1 <= index <= len(self._entries):
            return self._entries[index - 1]
        return None

    def all(self) -> List[Dict[str, Any]]:
        return list(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def __bool__(self) -> bool:  # a registry with no sources is still a registry
        return True


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Regions where a bracketed number is not a citation
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
_INLINE_CODE_RE = re.compile(r"(`+)[^\n]+?\1")


def _fenced_spans(text: str) -> List[Tuple[int, int]]:
    spans: List[Tuple[int, int]] = []
    pos = 0
    open_char = ""
    open_len = 0
    start = 0
    for line in text.splitlines(keepends=True):
        end = pos + len(line)
        match = _FENCE_RE.match(line)
        if not open_char:
            if match:
                open_char = match.group(1)[0]
                open_len = len(match.group(1))
                start = pos
        elif match and match.group(1)[0] == open_char and len(match.group(1)) >= open_len:
            spans.append((start, end))
            open_char = ""
        pos = end
    if open_char:
        # An unclosed fence swallows the rest: whatever follows was meant to be
        # code, and reading brackets there as citations would invent sources.
        spans.append((start, len(text)))
    return spans


def _in_spans(pos: int, spans: Sequence[Tuple[int, int]]) -> bool:
    return any(start <= pos < end for start, end in spans)


def _protected_spans(text: str) -> List[Tuple[int, int]]:
    """Regions no citation machinery may read or rewrite: code, plus the two
    sections this module generates itself (their brackets and links are ours,
    not the model's, and re-processing them would break idempotency)."""
    spans = _fenced_spans(text)
    for match in _INLINE_CODE_RE.finditer(text):
        if not _in_spans(match.start(), spans):
            spans.append((match.start(), match.end()))
    legend = _legend_span(text)
    if legend:
        spans.append(legend)
    sources = _sources_section_span(text)
    if sources:
        spans.append(sources)
    spans.sort()
    return spans


# ---------------------------------------------------------------------------
# Markers
# ---------------------------------------------------------------------------

_MARKER_RE = re.compile(r"\[\s*\d{1,3}(?:\s*[,;]\s*\d{1,3})*\s*\]")
_DIGITS_RE = re.compile(r"\d+")


@dataclass
class Marker:
    numbers: List[int]
    start: int
    end: int


def find_markers(report_md: Any) -> List[Marker]:
    """Every citation marker in the text, in order.

    A marker is ``[n]``, ``[n, m]`` or two adjacent ``[n][m]``. ``[n](`` is a
    markdown link, not a citation. Code fences and inline code are skipped
    entirely, so ``arr[3]`` in a snippet is an index expression and stays one.
    """
    text = _as_text(report_md)
    if not text:
        return []
    protected = _protected_spans(text)
    out: List[Marker] = []
    for match in _MARKER_RE.finditer(text):
        if _in_spans(match.start(), protected):
            continue
        if match.end() < len(text) and text[match.end()] == "(":
            continue  # markdown link
        numbers = [int(d) for d in _DIGITS_RE.findall(match.group(0))]
        numbers = [n for n in numbers if 1 <= n <= MAX_SOURCE_NUMBER]
        if not numbers:
            continue
        out.append(Marker(numbers=numbers, start=match.start(), end=match.end()))
    return out


# ---------------------------------------------------------------------------
# Sentence units
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s")
_LIST_RE = re.compile(r"^\s{0,6}(?:[-*+]|\d{1,3}[.)])\s+")
_QUOTE_RE = re.compile(r"^\s{0,3}>")
_TABLE_ROW_RE = re.compile(r"^\s{0,3}\|")
_TABLE_SEP_RE = re.compile(r"^\s{0,3}\|[\s\-:|]+\|?\s*$")
# A sentence ends at .!?… followed by whitespace and something that can start a
# new sentence — an uppercase letter, a digit, a Spanish opening ¿/¡, a quote,
# or a citation marker (``text. [1] Next`` is as common as ``text [1]. Next``).
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?…])[\"'”’)\]]*\s+(?=[\[\"'“¿¡(]|[A-ZÁÉÍÓÚÑÜÀÈÌÒÙÂÊÎÔÛÄÖÜÇ0-9])")
_LEADING_MARKERS_RE = re.compile(r"^\s*(?:\[\s*\d{1,3}(?:\s*[,;]\s*\d{1,3})*\s*\][\s.,;:]*)+")
_HAS_LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)
_WORDISH_RE = re.compile(r"[^\W_]+", re.UNICODE)
# Below this, a unit is a label, a fragment or a bare figure — a table cell
# reading "Alfredson", a wrapped "> preocupa." — not a sentence a reader would
# expect to carry a citation. Counting those would understate coverage.
_MIN_SENTENCE_WORDS = 4


def _counts_as_sentence(text: str) -> bool:
    return len(_WORDISH_RE.findall(strip_markers(text))) >= _MIN_SENTENCE_WORDS


def _line_spans(text: str) -> List[Tuple[int, int]]:
    spans = []
    pos = 0
    for line in text.splitlines(keepends=True):
        spans.append((pos, pos + len(line)))
        pos += len(line)
    return spans


def _sentence_units(text: str, protected: Sequence[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """Char spans of the smallest units a citation can sensibly belong to.

    A list item, a heading, a table cell and a sentence inside a paragraph are
    each their own unit; a soft-wrapped paragraph is one unit, not one per
    line, because splitting there would cut a claim away from its marker.
    """
    groups: List[Tuple[int, int]] = []
    current: Optional[List[int]] = None
    lines = _line_spans(text)

    def flush() -> None:
        nonlocal current
        if current is not None:
            groups.append((current[0], current[1]))
            current = None

    for index, (start, end) in enumerate(lines):
        line = text[start:end]
        if _in_spans(start, protected):
            flush()
            continue
        if not line.strip():
            flush()
            continue
        if _TABLE_SEP_RE.match(line):
            flush()
            continue
        if _TABLE_ROW_RE.match(line):
            flush()
            # The row above the |---| separator holds column labels, not
            # claims. Treating "Dose" as a sentence would pad the denominator
            # of the coverage figure with words nobody could cite.
            if _is_table_header(text, lines, index):
                continue
            groups.extend(_table_cell_spans(text, start, end))
            continue
        if _HEADING_RE.match(line) or _LIST_RE.match(line) or _QUOTE_RE.match(line):
            flush()
            current = [start, end]
            continue
        if current is None:
            current = [start, end]
        else:
            current[1] = end
    flush()

    units: List[Tuple[int, int]] = []
    for start, end in groups:
        units.extend(_split_sentences(text, start, end))
    return _attach_orphan_markers(text, units)


def _is_table_header(text: str, lines: Sequence[Tuple[int, int]], index: int) -> bool:
    for start, end in lines[index + 1:]:
        line = text[start:end]
        if not line.strip():
            return False
        return bool(_TABLE_SEP_RE.match(line))
    return False


def _table_cell_spans(text: str, start: int, end: int) -> List[Tuple[int, int]]:
    spans = []
    pos = start
    for cell in text[start:end].split("|"):
        cell_start, cell_end = pos, pos + len(cell)
        pos = cell_end + 1  # the '|' we split on
        if cell.strip():
            spans.append((cell_start, cell_end))
    return spans


def _split_sentences(text: str, start: int, end: int) -> List[Tuple[int, int]]:
    chunk = text[start:end]
    spans = []
    cursor = 0
    for match in _SENT_SPLIT_RE.finditer(chunk):
        if match.end() <= cursor:
            continue
        spans.append((start + cursor, start + match.start()))
        cursor = match.end()
    spans.append((start + cursor, end))
    return [(s, e) for s, e in spans if text[s:e].strip()]


def _attach_orphan_markers(text: str, units: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """Move a leading ``[1]`` onto the previous unit.

    ``Pain fell by 40%. [1] Recovery varied.`` splits after the full stop, which
    would hand the citation to the sentence it does not support — exactly the
    misattribution the audit exists to prevent.
    """
    out: List[Tuple[int, int]] = []
    for start, end in units:
        match = _LEADING_MARKERS_RE.match(text[start:end])
        if match and out and match.end() > 0:
            carry = start + match.end()
            prev_start, _prev_end = out[-1]
            out[-1] = (prev_start, carry)
            start = carry
            if not text[start:end].strip():
                continue
        out.append((start, end))
    return out


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


@dataclass
class Claim:
    text: str
    numbers: List[int]
    start: int
    end: int


@dataclass
class CitationAudit:
    used: List[int] = field(default_factory=list)
    dangling: List[int] = field(default_factory=list)
    uncited: List[int] = field(default_factory=list)
    claims: List[Claim] = field(default_factory=list)
    removed: List[int] = field(default_factory=list)
    total_sentences: int = 0
    cited_sentences: int = 0
    coverage: Dict[str, Any] = field(default_factory=dict)


def audit_citations(report_md: Any, registry: Optional[SourceRegistry] = None) -> CitationAudit:
    """What the report cites, what it cites that does not exist, and what it
    never cited. Never raises; junk in gives an empty audit."""
    text = _as_text(report_md)
    audit = CitationAudit()
    if not text.strip():
        return audit

    protected = _protected_spans(text)
    markers = [m for m in find_markers(text) if not _in_spans(m.start, protected)]
    # Headings are navigation, not assertions: counting them as sentences would
    # depress the coverage figure for a well-structured report and reward a wall
    # of prose. They are also not graded, so a marker in one is only repaired.
    units = [u for u in _sentence_units(text, protected)
             if not _HEADING_RE.match(text[u[0]:u[1]])]
    counted = [u for u in units
               if _HAS_LETTER_RE.search(text[u[0]:u[1]])
               and _counts_as_sentence(text[u[0]:u[1]])]
    audit.total_sentences = len(counted)

    by_unit: Dict[Tuple[int, int], List[int]] = {}
    for marker in markers:
        for unit in units:
            if unit[0] <= marker.start < unit[1]:
                by_unit.setdefault(unit, []).extend(marker.numbers)
                break

    for unit in units:
        numbers = by_unit.get(unit)
        if not numbers:
            continue
        seen: List[int] = []
        for n in numbers:
            if n not in seen:
                seen.append(n)
        audit.claims.append(Claim(text=text[unit[0]:unit[1]].strip(), numbers=seen,
                                  start=unit[0], end=unit[1]))

    audit.cited_sentences = sum(1 for unit in counted if unit in by_unit)

    for claim in audit.claims:
        for n in claim.numbers:
            if n not in audit.used:
                audit.used.append(n)

    total_sources = len(registry) if registry is not None else 0
    audit.dangling = [n for n in audit.used if n > total_sources]
    audit.uncited = [n for n in range(1, total_sources + 1) if n not in audit.used]
    return audit


# ---------------------------------------------------------------------------
# Repair
# ---------------------------------------------------------------------------

_LINK_RE = re.compile(r"(?<!!)\[([^\]\[]*)\]\(\s*([^)\s]+)(?:\s+\"[^\"]*\")?\s*\)")


def repair_citations(report_md: Any, registry: Optional[SourceRegistry] = None,
                     language: str = "en") -> Tuple[str, CitationAudit]:
    """Make every marker in the report resolve, then print the sources list.

    Deletes markers that point at no source rather than leaving a lie in the
    text, folds ``[text](url)`` citations into ``text [n]`` when the URL is a
    known source, and appends a numbered list of exactly the sources cited.
    Never adds a citation to a sentence that had none: an uncited paragraph
    stays uncited, and the coverage figure says so.
    """
    registry = registry if registry is not None else SourceRegistry()
    text = _as_text(report_md)
    if not text.strip():
        return "", CitationAudit()

    text = _drop_sources_section(text)
    text = _fold_link_citations(text, registry)
    text, removed = _drop_dangling_markers(text, len(registry))

    body = text.rstrip()
    audit = audit_citations(body, registry)
    audit.removed = removed

    cited = [n for n in sorted(set(audit.used)) if registry.source(n)]
    if not cited:
        return body + "\n", audit
    return body + "\n\n" + _sources_section(cited, registry, language), audit


def _fold_link_citations(text: str, registry: SourceRegistry) -> str:
    protected = _protected_spans(text)
    out: List[str] = []
    cursor = 0
    for match in _LINK_RE.finditer(text):
        if _in_spans(match.start(), protected):
            continue
        number = registry.number_for(match.group(2))
        if not number:
            continue
        label = match.group(1).strip()
        out.append(text[cursor:match.start()])
        out.append(f"{label} [{number}]" if label else f"[{number}]")
        cursor = match.end()
    out.append(text[cursor:])
    return "".join(out)


def _drop_dangling_markers(text: str, total_sources: int) -> Tuple[str, List[int]]:
    removed: List[int] = []
    out: List[str] = []
    cursor = 0
    for marker in find_markers(text):
        bad = [n for n in marker.numbers if n > total_sources]
        if not bad:
            continue
        for n in bad:
            if n not in removed:
                removed.append(n)
        good = [n for n in marker.numbers if n <= total_sources]
        start = marker.start
        if good:
            replacement = "[" + ", ".join(str(n) for n in good) + "]"
        else:
            replacement = ""
            # Absorb the space in front so `claim [7].` becomes `claim.` rather
            # than `claim .` — a stray space before punctuation is the visible
            # scar of a deleted citation.
            after = text[marker.end] if marker.end < len(text) else ""
            if start > 0 and text[start - 1] == " " and (after == "" or after in " \n.,;:!?)]}»"):
                start -= 1
        out.append(text[cursor:start])
        out.append(replacement)
        cursor = marker.end
    out.append(text[cursor:])
    return "".join(out), sorted(removed)


def _sources_section(numbers: Iterable[int], registry: SourceRegistry,
                     language: str) -> str:
    lines = [sources_heading(language), ""]
    for n in numbers:
        entry = registry.source(n)
        if not entry:
            continue
        title = entry.get("title") or entry.get("domain") or entry.get("url") or f"Source {n}"
        url = entry.get("url") or ""
        domain = entry.get("domain") or ""
        line = f"{n}. [{title}]({url})"
        if domain:
            line += f" — {domain}"
        lines.append(line)
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------


@dataclass
class GradedClaim:
    claim: Claim
    number: int
    grade: str
    layer: Optional[int]
    why: str


_GRADE_RANK = {GRADE_WEAK: 0, GRADE_MEDIUM: 1, GRADE_HIGH: 2}


def strip_markers(text: Any) -> str:
    """The sentence without its citation markers.

    Load-bearing: ``claim_verify``'s layer 4 checks that every number in the
    claim occurs in the source, so leaving ``[12]`` in the text makes it hunt
    for "12" in the page and grade an otherwise perfect sentence weak.
    """
    body = _as_text(text)
    cleaned, _ = _drop_dangling_markers_all(body)
    return re.sub(r"\s{2,}", " ", cleaned).strip()


def _drop_dangling_markers_all(text: str) -> Tuple[str, List[int]]:
    """:func:`_drop_dangling_markers` with every marker treated as dangling."""
    return _drop_dangling_markers(text, 0)


def grade_claims(claims: Sequence[Claim],
                 registry: Optional[SourceRegistry] = None) -> List[GradedClaim]:
    """Grade each cited sentence against the text of the source it cites.

    ``alta`` when ``claim_verify`` settled it at layer 1 or 2 (the sentence is
    in the source, verbatim or modulo case/accents/punctuation), ``moderada``
    at layer 3 (enough of its content words are there and none of its figures
    or names are missing), ``débil`` otherwise — including the case where the
    cited number has no source at all.

    Note the asymmetry inherited from the ladder: layer 4 only ever settles a
    claim *against* the source, so "supported at layer 3/4" is layer 3 in
    practice. A claim nothing settled (layer ``None``) is ``débil``, because
    the cited source's own text did not support it.
    """
    from src.claim_verify import verify  # imported here: grading is optional work

    registry = registry if registry is not None else SourceRegistry()
    out: List[GradedClaim] = []
    for claim in claims or []:
        sentence = strip_markers(getattr(claim, "text", ""))
        best: Optional[GradedClaim] = None
        for number in getattr(claim, "numbers", []) or []:
            entry = registry.source(number)
            if not entry:
                candidate = GradedClaim(claim=claim, number=number, grade=GRADE_WEAK,
                                        layer=None,
                                        why=f"source [{number}] is not in the registry")
            else:
                result = verify(sentence, _source_text(entry))
                candidate = GradedClaim(
                    claim=claim, number=number, grade=_grade_of(result),
                    layer=result.get("layer"), why=str(result.get("why", "")))
            if best is None or _GRADE_RANK[candidate.grade] > _GRADE_RANK[best.grade]:
                best = candidate
        if best is None:
            best = GradedClaim(claim=claim, number=0, grade=GRADE_WEAK, layer=None,
                               why="the sentence carries no resolvable citation")
        out.append(best)
    return out


def _source_text(entry: Dict[str, Any]) -> str:
    """What we actually hold of a page — the extraction, not the whole page.

    This bounds what any grade can mean, and the legend says so: we can only
    check the sentence against the excerpt we stored.
    """
    return "\n".join(p for p in (entry.get("summary"), entry.get("evidence"),
                                 entry.get("title")) if p)


def _grade_of(result: Dict[str, Any]) -> str:
    if not result.get("supported"):
        return GRADE_WEAK
    layer = result.get("layer")
    if layer in (1, 2):
        return GRADE_HIGH
    if layer in (3, 4):
        return GRADE_MEDIUM
    return GRADE_WEAK


def compute_coverage(audit: CitationAudit,
                     graded: Sequence[GradedClaim]) -> Dict[str, Any]:
    counts = {grade: 0 for grade in GRADES}
    for item in graded or []:
        if item.grade in counts:
            counts[item.grade] += 1
    return {
        # Not len(audit.claims): a marker in a two-word table cell is repaired
        # and graded, but it is not a sentence, so it belongs in neither half
        # of the coverage ratio.
        "cited_sentences": audit.cited_sentences,
        "total_sentences": audit.total_sentences,
        # Every graded citation, sentences and table cells alike. The legend
        # prints it beside the grade counts so the two add up for the reader.
        "citations": len(graded or []),
        "graded": counts,
    }


# ---------------------------------------------------------------------------
# Language
# ---------------------------------------------------------------------------

LANGUAGE_NAMES = {
    "es": "Spanish", "en": "English", "fr": "French",
    "de": "German", "pt": "Portuguese", "it": "Italian",
}
_LANGUAGE_ORDER = ("en", "es", "pt", "fr", "de", "it")

_STOPWORDS = {
    "en": """the a an and or of to in is are was were that this these those for with as
             it its on at from by be been what how why when which who not no do does did
             have has had can could should would will about between than more most best
             long take usually""".split(),
    "es": """el la los las un una unos unas y de del que en por para con como es son era
             sobre según qué cuál cuáles cuánto cuánta cuántos dónde cómo porqué al se no
             ni hay más entre desde tienen tiene sus su este esta estos estas necesito""".split(),
    "pt": """o os as um uma e de do da dos das que em por para com como é são era sobre
             segundo qual quais quanto quanta quantos onde não há mais entre desde têm tem
             seus sua este esta estes estas você são melhores leva""".split(),
    "fr": """le la les un une des du de et que en pour avec dans qui est sont était sur
             selon quel quelle quels quelles combien où comment pourquoi ne pas ce cette
             ces plus entre depuis leur leurs chez sont meilleurs""".split(),
    "de": """der die das den dem des ein eine einer und oder von zu in ist sind war waren
             für mit auf als bei aus nach über welche welcher welches wie was warum wann
             nicht kein mehr zwischen seit ihre ihr am besten lange""".split(),
    "it": """il lo la i gli le un uno una di del della dei delle e che in per con come è
             sono era su secondo quale quali quanto quanta dove perché non più tra da loro
             questo questa questi queste negli migliori richiede""".split(),
}
_STOPWORD_SETS = {code: frozenset(words) for code, words in _STOPWORDS.items()}
_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)

# es and pt share most of their function words; these letters do not appear in
# the other's orthography and settle the pair when the stopwords are a wash.
_CHAR_HINTS = {"ñ": "es", "¿": "es", "¡": "es", "ã": "pt", "õ": "pt",
               "ß": "de", "ê": "fr", "œ": "fr"}


def detect_language(text: Any) -> str:
    """Which of es/en/fr/de/pt/it a question is written in, by stopword share.

    Each hit is weighted ``1 / (number of languages that share the word)``, so
    ``de`` — which four of the six use — settles nothing and ``combien`` settles
    a lot. Defaults to English, which is what the engine did before and is the
    safe answer for a question with no function words at all.
    """
    body = _as_text(text).lower()
    if not body.strip():
        return "en"
    words = _WORD_RE.findall(body)
    shared: Dict[str, int] = {}
    for code, vocabulary in _STOPWORD_SETS.items():
        for word in vocabulary:
            shared[word] = shared.get(word, 0) + 1
    scores = {code: 0.0 for code in _STOPWORD_SETS}
    for word in words:
        for code, vocabulary in _STOPWORD_SETS.items():
            if word in vocabulary:
                scores[code] += 1.0 / shared[word]
    for char, code in _CHAR_HINTS.items():
        if char in body:
            scores[code] += 0.75
    best = max(scores.values()) if scores else 0.0
    if best <= 0:
        return "en"
    return min((c for c in scores if scores[c] == best), key=_LANGUAGE_ORDER.index)


# ---------------------------------------------------------------------------
# The deterministic sections
# ---------------------------------------------------------------------------

_SOURCES_HEADINGS = {
    "es": "## Fuentes", "en": "## Sources", "fr": "## Sources",
    "de": "## Quellen", "pt": "## Fontes", "it": "## Fonti",
}
_LEGEND_TITLES = {
    "es": "## Cómo leer este informe",
    "en": "## How to read this report",
    "fr": "## Comment lire ce rapport",
    "de": "## Wie dieser Bericht zu lesen ist",
    "pt": "## Como ler este relatório",
    "it": "## Come leggere questo rapporto",
}
IMPLICATION_LABELS = {
    "es": "Implicación práctica", "en": "Practical implication",
    "fr": "Implication pratique", "de": "Praktische Konsequenz",
    "pt": "Implicação prática", "it": "Implicazione pratica",
}
GRADE_WORDS = {
    "es": {GRADE_HIGH: "alta", GRADE_MEDIUM: "moderada", GRADE_WEAK: "débil"},
    "en": {GRADE_HIGH: "high", GRADE_MEDIUM: "moderate", GRADE_WEAK: "weak"},
    "fr": {GRADE_HIGH: "élevé", GRADE_MEDIUM: "modéré", GRADE_WEAK: "faible"},
    "de": {GRADE_HIGH: "hoch", GRADE_MEDIUM: "mittel", GRADE_WEAK: "schwach"},
    "pt": {GRADE_HIGH: "alta", GRADE_MEDIUM: "moderada", GRADE_WEAK: "fraca"},
    "it": {GRADE_HIGH: "alta", GRADE_MEDIUM: "moderata", GRADE_WEAK: "debole"},
}
_LEGEND_HEADINGS = tuple(_LEGEND_TITLES.values())


def sources_heading(language: str) -> str:
    return _SOURCES_HEADINGS.get((language or "").lower(), _SOURCES_HEADINGS["en"])


def legend_heading(language: str) -> str:
    return _LEGEND_TITLES.get((language or "").lower(), _LEGEND_TITLES["en"])


def implication_label(language: str) -> str:
    return IMPLICATION_LABELS.get((language or "").lower(), IMPLICATION_LABELS["en"])


_LEGEND_BODY = {
    "es": ("Cada frase con datos lleva un marcador `[n]` que remite a la fuente "
           "numerada en «Fuentes». {cited} de las {total} frases del informe "
           "({pct} %) llevan cita.\n\n"
           "Respaldo de la fuente citada en las {citations} citas del informe — {counts}.\n\n"
           "Estas etiquetas solo indican si el texto que recogimos de la fuente "
           "citada contiene la frase; no dicen si la afirmación es cierta en el "
           "mundo, ni qué calidad tiene el estudio que hay detrás."),
    "en": ("Every factual sentence carries a `[n]` marker pointing at the "
           "numbered entry under \"Sources\". {cited} of this report's {total} "
           "sentences ({pct}%) carry one.\n\n"
           "Support from the cited source, across this report's {citations} citations — {counts}.\n\n"
           "These labels only say whether the text we collected from the cited "
           "source contains the sentence — not whether the claim is true in the "
           "world, and not how good the study behind it is."),
    "fr": ("Chaque phrase factuelle porte un marqueur `[n]` renvoyant à la source "
           "numérotée dans « Sources ». {cited} des {total} phrases du rapport "
           "({pct} %) en portent un.\n\n"
           "Appui de la source citée, sur les {citations} citations du rapport — {counts}.\n\n"
           "Ces étiquettes indiquent seulement si le texte recueilli de la source "
           "citée contient la phrase ; elles ne disent pas si l'affirmation est "
           "vraie, ni quelle est la qualité de l'étude derrière elle."),
    "de": ("Jeder Sachsatz trägt eine Markierung `[n]`, die auf den nummerierten "
           "Eintrag unter „Quellen“ verweist. {cited} der {total} Sätze dieses "
           "Berichts ({pct} %) tragen eine.\n\n"
           "Deckung durch die zitierte Quelle, über die {citations} Zitate des Berichts — {counts}.\n\n"
           "Diese Kennzeichnungen sagen nur, ob der von uns erfasste Text der "
           "zitierten Quelle den Satz enthält — nicht, ob die Aussage wahr ist, "
           "und nicht, wie gut die Studie dahinter ist."),
    "pt": ("Cada frase com dados leva um marcador `[n]` que remete para a fonte "
           "numerada em «Fontes». {cited} das {total} frases do relatório "
           "({pct} %) levam citação.\n\n"
           "Apoio da fonte citada, nas {citations} citações do relatório — {counts}.\n\n"
           "Estas etiquetas só indicam se o texto que recolhemos da fonte citada "
           "contém a frase; não dizem se a afirmação é verdadeira, nem qual é a "
           "qualidade do estudo por trás dela."),
    "it": ("Ogni frase con dati porta un marcatore `[n]` che rimanda alla fonte "
           "numerata in «Fonti». {cited} delle {total} frasi del rapporto "
           "({pct} %) portano una citazione.\n\n"
           "Sostegno della fonte citata, sulle {citations} citazioni del rapporto — {counts}.\n\n"
           "Queste etichette dicono solo se il testo raccolto dalla fonte citata "
           "contiene la frase; non dicono se l'affermazione è vera, né quale sia "
           "la qualità dello studio che c'è dietro."),
}


def build_legend(coverage: Dict[str, Any], language: str = "en") -> str:
    """The report's opening legend, written here rather than by the model.

    A model-written note about how reliable its own report is would be one more
    generated claim; these numbers are counted, and the sentence about what
    they do not mean is fixed text that no sampling temperature can soften.
    Returns "" when nothing was cited — there is nothing to explain, and a
    legend over an uncited report would flatter it.
    """
    cited = int((coverage or {}).get("cited_sentences") or 0)
    total = int((coverage or {}).get("total_sentences") or 0)
    if cited <= 0:
        return ""
    counts = (coverage or {}).get("graded") or {}
    words = GRADE_WORDS.get((language or "").lower(), GRADE_WORDS["en"])
    rendered = " · ".join(f"{words[g]}: {int(counts.get(g) or 0)}" for g in GRADES)
    citations = int((coverage or {}).get("citations")
                    or sum(int(counts.get(g) or 0) for g in GRADES))
    pct = int(round(100.0 * cited / total)) if total else 0
    body = _LEGEND_BODY.get((language or "").lower(), _LEGEND_BODY["en"])
    return (legend_heading(language) + "\n\n"
            + body.format(cited=cited, total=total, pct=pct, counts=rendered,
                          citations=citations))


# ---------------------------------------------------------------------------
# Section surgery — so running the pipeline twice changes nothing
# ---------------------------------------------------------------------------

_HEADING_LINE_RE = re.compile(r"^\s{0,3}##\s+(.+?)\s*$")


def _section_span(text: str, headings: Sequence[str]) -> Optional[Tuple[int, int]]:
    """Span of the last ``##`` section whose title matches, heading to the next
    ``##`` (or end of text)."""
    wanted = {h.lstrip("# ").strip().casefold() for h in headings}
    found: Optional[int] = None
    bounds: List[Tuple[int, int, str]] = []
    for start, end in _line_spans(text):
        match = _HEADING_LINE_RE.match(text[start:end])
        if match:
            bounds.append((start, end, match.group(1).casefold()))
    for index, (start, _end, title) in enumerate(bounds):
        if title in wanted:
            found = index
    if found is None:
        return None
    start = bounds[found][0]
    end = bounds[found + 1][0] if found + 1 < len(bounds) else len(text)
    return start, end


# The legend body is exactly this many paragraphs. It has to be counted rather
# than bounded by "the next ## heading": the legend sits directly above the
# report's own prose, which need not open with a heading, and a greedy boundary
# would swallow — and on the next run delete — the whole report.
_LEGEND_BLOCKS = 3


def _legend_span(text: str) -> Optional[Tuple[int, int]]:
    """Span of the legend section this module generated, heading and body."""
    wanted = {h.lstrip("# ").strip().casefold() for h in _LEGEND_HEADINGS}
    lines = _line_spans(text)
    found: Optional[int] = None
    for index, (start, end) in enumerate(lines):
        match = _HEADING_LINE_RE.match(text[start:end])
        if match and match.group(1).casefold() in wanted:
            found = index
    if found is None:
        return None
    span_start, span_end = lines[found]
    index, blocks = found + 1, 0
    while index < len(lines) and blocks < _LEGEND_BLOCKS:
        start, end = lines[index]
        line = text[start:end]
        if not line.strip():
            span_end = end
            index += 1
            continue
        if line.lstrip().startswith("#"):
            break
        while index < len(lines) and text[lines[index][0]:lines[index][1]].strip():
            span_end = lines[index][1]
            index += 1
        blocks += 1
    return span_start, span_end


def _sources_section_span(text: str) -> Optional[Tuple[int, int]]:
    """Span of a generated sources section — but only if what follows really is
    a numbered source list, so a report whose own analysis lives under a
    ``## Sources`` heading is never silently deleted."""
    span = _section_span(text, tuple(_SOURCES_HEADINGS.values()))
    if not span:
        return None
    start, end = span
    body = text[start:end].splitlines()[1:]
    for line in body:
        if line.strip() and not re.match(r"^\d{1,3}\.\s", line.strip()):
            return None
    return span


def _drop_sources_section(text: str) -> str:
    span = _sources_section_span(text)
    if not span:
        return text
    return text[:span[0]] + text[span[1]:]


def _drop_legend_section(text: str) -> str:
    span = _legend_span(text)
    if not span:
        return text
    head, tail = text[:span[0]], text[span[1]:]
    if head.strip() and tail.strip():
        return head.rstrip() + "\n\n" + tail.lstrip("\n")
    return (head + tail).lstrip("\n")


def _insert_legend(text: str, legend: str) -> str:
    """Put the legend directly under the report's title, or at the very top."""
    if not legend:
        return text
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.strip():
            if line.lstrip().startswith("# "):
                head = "".join(lines[:index + 1]).rstrip()
                tail = "".join(lines[index + 1:]).lstrip("\n")
                return head + "\n\n" + legend + "\n\n" + tail
            break
    return legend + "\n\n" + text.lstrip("\n")


# ---------------------------------------------------------------------------
# The seam the research engine calls
# ---------------------------------------------------------------------------


def finalize_report(report_md: Any, registry: Optional[SourceRegistry] = None,
                    language: str = "en") -> Tuple[str, CitationAudit, List[GradedClaim]]:
    """Repair the citations, grade them, print the legend and the sources list.

    Idempotent: the legend and the sources section are stripped before the work
    starts and regenerated after it, so finalizing an already-finalized report
    produces the same bytes.
    """
    registry = registry if registry is not None else SourceRegistry()
    text = _drop_legend_section(_as_text(report_md))
    repaired, audit = repair_citations(text, registry, language=language)
    graded = grade_claims(audit.claims, registry)
    coverage = compute_coverage(audit, graded)

    final = _insert_legend(repaired, build_legend(coverage, language))
    # Re-audit so the spans point into the text we are actually returning. The
    # legend and the sources list are protected regions, so the counts are the
    # ones the legend just printed.
    final_audit = audit_citations(final, registry)
    final_audit.removed = audit.removed
    final_audit.coverage = coverage
    return final, final_audit, graded
