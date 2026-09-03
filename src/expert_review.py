"""Expert review — typed span deltas, corpus anchoring, and the honesty rule.

What this module is for
-----------------------
An expert (a profile plus its own indexed corpus, ``services/experts.py``)
reviews a piece of the user's text. The result is never rewritten prose. It is
a list of **typed deltas over character spans of the original**, each one
either **anchored to the corpus** — with the book and page it came from — or
explicitly labelled *the model's opinion, not the corpus*.

That distinction is the whole point. A local model asked to "review this with
Brenner's rules" will happily invent a rule and attribute it to Brenner. Here
a correction may only present itself as coming from a book when a cheap,
deterministic check says the cited chunk actually supports it; otherwise it is
still shown — the user may well want it — but it is marked as opinion. And a
page number is never fabricated: a chunk whose page is unknown renders
"source, page unknown".

The three moving parts
----------------------
1. ``parse_corrections`` — read the model's deltas, liberally, and **validate**
   every one. A delta that fails validation goes to ``rejected`` with a reason;
   nothing is dropped silently, because a silently dropped correction is
   indistinguishable from a model that had nothing to say. The important check
   is the quote: models miscount character offsets constantly, so every delta
   carries the text it thinks it is replacing, and when the offsets are wrong
   but the quote occurs exactly once we relocate the span to the quote.
2. ``verify_anchoring`` — three cheap layers (literal, fuzzy, nothing), no LLM
   anywhere. The layer that passes sets the confidence.
3. ``review`` — the pass itself, chunked **by scene/paragraph** because a
   short-context local model must correct by scenes, not by novel, with the
   per-chunk deltas merged back into original-document offsets.

``llm_call`` is injected, so none of this needs a model to be tested.

Pure stdlib. ``services.experts`` is imported lazily inside functions and its
absence degrades to a clear error instead of an ImportError at module load.

The delta wire format
---------------------
What the prompt asks the model for (and what ``parse_corrections`` prefers)::

    :::deltas
    [
      {"op": "EDIT", "start": 12, "end": 31,
       "quote": "the exact original text between those offsets",
       "replacement": "the corrected text",
       "rationale": "why, in one sentence",
       "rule": "the rubric item this comes from",
       "severity": "medium",
       "cite": ["C1"],
       "confidence": 0.8}
    ]
    :::

Also accepted, because models improvise: a fenced ```json array, a bare JSON
array anywhere in the answer, individual ``:::delta{...}:::`` blocks, one JSON
object per line, ``span: {"start": .., "end": ..}`` instead of flat offsets,
and ``citations`` instead of ``cite``. Trailing commas and stray code fences
are repaired before parsing.

What a caller (the UI, the agent tool) gets back for each delta::

    {"id": "D1", "op": "EDIT", "span": {"start": 12, "end": 31},
     "quote": "...", "replacement": "...", "rationale": "...", "rule": "...",
     "severity": "medium",
     "citations": [{"marker": "C1", "chunk_id": "...", "source": "Brenner.pdf",
                    "page": 42, "page_label": "page 42",
                    "ref": "Brenner.pdf, page 42", "known": true}],
     "anchored": true, "anchor_layer": "literal", "label": "corpus",
     "confidence": 0.9, "model_confidence": 0.8,
     "relocated": false, "notes": [], "unknown_markers": [], "chunk": 0}

``label`` is exactly one of ``"corpus"`` or ``"model's opinion, not the
corpus"`` — the renderer should print it verbatim rather than deciding for
itself what ``anchored`` means.
"""

from __future__ import annotations

import inspect
import json
import logging
import re
import unicodedata
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

OPS = ("EDIT", "ADD", "KILL")
SEVERITIES = ("low", "medium", "high")
_SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3}

LABEL_CORPUS = "corpus"
LABEL_OPINION = "model's opinion, not the corpus"
PAGE_UNKNOWN = "page unknown"

# Scene chunking: a local model with a short context corrects by scenes.
DEFAULT_CHUNK_CHARS = 3000
MIN_CHUNK_CHARS = 400
DEFAULT_BLOCK_BUDGET = 2400      # char budget asked of expert_block per chunk
MAX_DELTAS = 200                 # a batch beyond this is a runaway, not a review

# Anchoring thresholds. Deliberately low: this decides "may this claim cite a
# book", and the cost of a false negative (shown as opinion) is a label, while
# the cost of a false positive is a fabricated citation.
LITERAL_TERM_RATIO = 0.60
FUZZY_TERM_RATIO = 0.34
CONFIDENCE_BY_LAYER = {"literal": 0.9, "fuzzy": 0.6, "none": 0.25,
                       "no_citations": 0.3, "unknown_marker": 0.2}

_MARKER_RE = re.compile(r"^[Cc](\d{1,3})$")
_BLOCK_MARKER_RE = re.compile(r"\[C(\d{1,3})\]")
_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")
_FENCE_RE = re.compile(r"```[a-zA-Z0-9_-]*\s*\n(.*?)```", re.DOTALL)
_DELTA_BLOCK_RE = re.compile(r":::\s*deltas?\s*(.*?):::", re.DOTALL | re.IGNORECASE)

# Function words carry no evidence, in either language the first user writes in.
_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "your", "you",
    "are", "was", "were", "have", "has", "had", "not", "but", "its", "it's",
    "their", "there", "here", "when", "what", "which", "than", "then", "them",
    "should", "would", "could", "must", "make", "makes", "made", "more", "most",
    "very", "such", "each", "also", "because", "about", "over", "under",
    "una", "unos", "unas", "los", "las", "del", "que", "por", "para", "como",
    "con", "sin", "esta", "este", "estos", "estas", "pero", "porque", "cuando",
    "donde", "sobre", "entre", "hasta", "desde", "muy", "mas", "menos", "todo",
    "todos", "toda", "todas", "ser", "estar", "hace", "hacer", "tiene", "tener",
    "debe", "deben", "puede", "pueden", "aqui", "alli", "asi",
}


class ExpertReviewError(ValueError):
    """Unusable input or an expert that cannot be loaded — routes map to 400."""


# ----------------------------------------------------------------------
# Small text helpers (comparison only — never used to rewrite user text)
# ----------------------------------------------------------------------


def _fold(text: str) -> str:
    try:
        decomposed = unicodedata.normalize("NFD", str(text or ""))
    except (TypeError, ValueError):
        return str(text or "").casefold()
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()


def _terms(text: str) -> List[str]:
    """Content words worth matching on: folded, >= 4 characters, not function
    words. Order preserved so 2-grams stay meaningful."""
    out: List[str] = []
    for word in _WORD_RE.findall(_fold(text)):
        if len(word) >= 4 and word not in _STOPWORDS:
            out.append(word)
    return out


def _clamp01(value: Any, default: float = 0.5) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    if num != num:                    # NaN
        return default
    return max(0.0, min(1.0, num))


# ----------------------------------------------------------------------
# Parsing the model's answer
# ----------------------------------------------------------------------


def _json_candidates(model_output: str) -> List[str]:
    """Every substring of the answer that might be the delta payload, in the
    order we would like to trust them: the documented ``:::deltas`` block
    first, then fenced blocks, then whatever bare JSON is lying around."""
    text = str(model_output or "")
    out: List[str] = []
    for m in _DELTA_BLOCK_RE.finditer(text):
        out.append(m.group(1))
    for m in re.finditer(r":::\s*delta\s*(\{.*?\})\s*:::", text, re.DOTALL | re.IGNORECASE):
        out.append(m.group(1))
    for m in _FENCE_RE.finditer(text):
        out.append(m.group(1))
    out.append(text)
    return out


def _loads_lenient(raw: str) -> Any:
    """json.loads, after the repairs models actually need: strip code fences,
    drop trailing commas, and tolerate smart quotes around nothing important."""
    text = str(raw or "").strip()
    if not text:
        return None
    text = _FENCE_RE.sub(lambda m: m.group(1), text)
    text = text.strip().strip("`").strip()
    for attempt in (text, _TRAILING_COMMA_RE.sub(r"\1", text)):
        try:
            return json.loads(attempt)
        except (ValueError, TypeError):
            continue
    return None


def _balanced_objects(text: str) -> List[str]:
    """Every balanced ``{...}`` run in the text, quote-aware. The last-resort
    reader for an answer that is prose with objects sprinkled through it."""
    out: List[str] = []
    depth = 0
    start = -1
    in_str = False
    escape = False
    quote = ""
    for i, ch in enumerate(str(text or "")):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == quote:
                in_str = False
            continue
        if ch in ('"', "'"):
            in_str = True
            quote = ch
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    out.append(text[start:i + 1])
                    start = -1
    return out


def _raw_deltas(model_output: str) -> List[Dict[str, Any]]:
    """The model's delta objects, in emission order, from whichever shape the
    answer happens to be in. Never raises."""
    for candidate in _json_candidates(model_output):
        parsed = _loads_lenient(candidate)
        rows = _as_delta_list(parsed)
        if rows:
            return rows
    # One object per line, or objects embedded in prose.
    rows: List[Dict[str, Any]] = []
    for chunk in _balanced_objects(str(model_output or "")):
        parsed = _loads_lenient(chunk)
        if isinstance(parsed, dict) and _looks_like_delta(parsed):
            rows.append(parsed)
    return rows


def _as_delta_list(parsed: Any) -> List[Dict[str, Any]]:
    if isinstance(parsed, dict):
        for key in ("deltas", "corrections", "edits", "items"):
            if isinstance(parsed.get(key), list):
                return [d for d in parsed[key] if isinstance(d, dict)]
        return [parsed] if _looks_like_delta(parsed) else []
    if isinstance(parsed, list):
        return [d for d in parsed if isinstance(d, dict) and _looks_like_delta(d)]
    return []


def _looks_like_delta(row: Dict[str, Any]) -> bool:
    if not isinstance(row, dict):
        return False
    if "op" in row:
        return True
    return any(k in row for k in ("span", "start", "quote", "replacement"))


def _span_of(row: Dict[str, Any]) -> Tuple[Optional[int], Optional[int]]:
    span = row.get("span")
    start = end = None
    if isinstance(span, dict):
        start, end = span.get("start"), span.get("end")
    elif isinstance(span, (list, tuple)) and len(span) == 2:
        start, end = span[0], span[1]
    if start is None:
        start = row.get("start")
    if end is None:
        end = row.get("end")
    def _int(value):
        if isinstance(value, bool):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return _int(start), _int(end)


def _markers_of(row: Dict[str, Any]) -> List[str]:
    """The citation markers the model named, normalized to ``C1`` form."""
    raw: List[Any] = []
    for key in ("cite", "citations", "citation", "markers", "marker", "sources"):
        value = row.get(key)
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            raw.extend(value)
        else:
            raw.append(value)
    out: List[str] = []
    for item in raw:
        token = item
        if isinstance(item, dict):
            token = item.get("marker") or item.get("id") or item.get("chunk_id")
        token = str(token or "").strip().strip("[]").strip()
        m = _MARKER_RE.match(token)
        if m:
            marker = f"C{int(m.group(1))}"
            if marker not in out:
                out.append(marker)
    return out


def block_markers(block_chunk_ids: Optional[Sequence[Any]]) -> Dict[str, str]:
    """``[C1]`` is the first chunk id in the block, ``[C2]`` the second — the
    contract ``expert_block`` documents. Returns {marker: chunk_id}."""
    out: Dict[str, str] = {}
    for i, chunk_id in enumerate(list(block_chunk_ids or []), start=1):
        out[f"C{i}"] = str(chunk_id)
    return out


def _severity_of(row: Dict[str, Any]) -> str:
    value = str(row.get("severity") or "").strip().lower()
    if value in SEVERITIES:
        return value
    if value in ("critical", "blocker", "major"):
        return "high"
    if value in ("minor", "nit", "trivial"):
        return "low"
    return "medium"


def _find_all(haystack: str, needle: str) -> List[int]:
    out: List[int] = []
    if not needle:
        return out
    start = haystack.find(needle)
    while start >= 0:
        out.append(start)
        start = haystack.find(needle, start + 1)
    return out


def _locate_quote(original: str, quote: str) -> Tuple[List[int], str]:
    """Occurrences of the quote in the original, exact first and then with the
    edges trimmed (models love to include a leading space). Returns the offsets
    and the form of the quote that matched."""
    for form in (quote, quote.strip()):
        if not form:
            continue
        hits = _find_all(original, form)
        if hits:
            return hits, form
    return [], quote


def _overlaps(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """Two spans conflict when they cover a common character. An insertion
    (start == end) conflicts only when it lands strictly inside another span —
    inserting at a boundary is well defined, so it is allowed."""
    a0, a1 = a["span"]["start"], a["span"]["end"]
    b0, b1 = b["span"]["start"], b["span"]["end"]
    if a0 == a1:
        return b0 < a0 < b1
    if b0 == b1:
        return a0 < b0 < a1
    return a0 < b1 and b0 < a1


def parse_corrections(model_output: str, original_text: str,
                      block_chunk_ids: Optional[Sequence[Any]] = None,
                      *, id_start: int = 1) -> Dict[str, Any]:
    """Parse, validate and de-conflict the model's corrections.

    Returns ``{"deltas": [...], "rejected": [...]}``. Every input delta ends up
    in exactly one of the two lists — a rejected delta carries the reason it
    failed, because silence here is indistinguishable from a model that found
    nothing.

    Validation, in order:

    * the op must be EDIT, ADD or KILL; ADD must have an empty span and KILL an
      empty replacement (a non-empty one is dropped with a note rather than
      rejected — the intent is unambiguous);
    * the span must be within bounds with ``start <= end``;
    * **the quote must match**. ``original[start:end] == quote`` passes. If it
      does not, the quote is looked up in the original: exactly one occurrence
      relocates the span to it (``relocated: true``); more than one is rejected
      as ambiguous; none is rejected as not found. An EDIT or KILL without a
      quote is rejected — offsets alone are not trustworthy enough to change
      someone's prose. An ADD may omit the quote (its span is empty); if it
      carries one, the quote is treated as the anchor to insert *after*.
    * overlapping spans: the higher severity wins, then the higher confidence,
      then the longer span; the loser is rejected as ``overlaps <id>``.

    Ids are assigned in the model's emission order (``D1``, ``D2``, ...) so a
    rejection reason can name the delta that beat it; the surviving list is
    returned sorted by start offset and is guaranteed non-overlapping.
    """
    original = str(original_text or "")
    markers = block_markers(block_chunk_ids)
    accepted: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []

    try:
        rows = _raw_deltas(model_output)
    except Exception as e:  # noqa: BLE001 - a weird answer is not a crash
        logger.debug("parse_corrections could not read the answer: %s", e)
        rows = []

    for index, row in enumerate(rows[:MAX_DELTAS]):
        delta_id = f"D{id_start + index}"
        raw = {"id": delta_id, "raw": row}
        op = str(row.get("op") or "").strip().upper()
        if op in ("DELETE", "REMOVE", "CUT"):
            op = "KILL"
        elif op in ("INSERT", "APPEND"):
            op = "ADD"
        elif op in ("REPLACE", "CHANGE", "EDIT"):
            op = "EDIT"
        if op not in OPS:
            rejected.append({**raw, "reason": f"unknown op '{row.get('op')}' "
                                              f"(use {', '.join(OPS)})"})
            continue

        notes: List[str] = []
        start, end = _span_of(row)
        quote = row.get("quote")
        quote = "" if quote is None else str(quote)
        replacement = row.get("replacement")
        if replacement is None:
            replacement = row.get("text") if op != "KILL" else ""
        replacement = "" if replacement is None else str(replacement)

        if op == "KILL" and replacement:
            notes.append("KILL carried a replacement; it was dropped")
            replacement = ""
        if op == "ADD" and not replacement:
            rejected.append({**raw, "op": op, "reason": "ADD has nothing to insert"})
            continue

        if start is None and quote:
            # No offsets at all but a quote — treat it as a pure quote anchor.
            start = end = None
        elif start is None:
            rejected.append({**raw, "op": op, "reason": "no span and no quote"})
            continue

        relocated = False
        if start is not None:
            if end is None:
                end = start + (len(quote) if op != "ADD" else 0)
            if start < 0 or end < 0 or start > len(original) or end > len(original):
                rejected.append({**raw, "op": op,
                                 "reason": f"span {start}-{end} is outside the text "
                                           f"(0-{len(original)})"})
                continue
            if start > end:
                rejected.append({**raw, "op": op,
                                 "reason": f"span start {start} is after end {end}"})
                continue

        if op == "ADD":
            if quote.strip():
                hits, form = _locate_quote(original, quote)
                if len(hits) == 1:
                    point = hits[0] + len(form)
                    if start != point:
                        relocated = True
                        notes.append("insertion point moved to the end of its quote")
                    start = end = point
                elif len(hits) > 1:
                    rejected.append({**raw, "op": op,
                                     "reason": f"quote is ambiguous ({len(hits)} occurrences)"})
                    continue
                else:
                    rejected.append({**raw, "op": op,
                                     "reason": "quote not found in the original text"})
                    continue
            elif start is None:
                rejected.append({**raw, "op": op, "reason": "no span and no quote"})
                continue
            if start != end:
                notes.append(f"ADD span {start}-{end} collapsed to an insertion point")
                end = start
            quote = ""
        else:
            if not quote.strip():
                rejected.append({**raw, "op": op,
                                 "reason": "missing the quote of the original span "
                                           "(offsets alone are not trusted)"})
                continue
            if start is not None and original[start:end] == quote:
                pass
            else:
                hits, form = _locate_quote(original, quote)
                if len(hits) == 1:
                    start, end = hits[0], hits[0] + len(form)
                    quote = form
                    relocated = True
                    notes.append("span relocated to the unique occurrence of the quote")
                elif len(hits) > 1:
                    rejected.append({**raw, "op": op,
                                     "reason": f"quote is ambiguous ({len(hits)} occurrences); "
                                               "the offsets do not match it either"})
                    continue
                else:
                    rejected.append({**raw, "op": op,
                                     "reason": "quote does not match the text at those "
                                               "offsets and is not found elsewhere"})
                    continue

        cited = _markers_of(row)
        unknown = [m for m in cited if m not in markers]
        if unknown:
            notes.append("cites a marker that is not in this corpus block: "
                         + ", ".join(unknown))

        accepted.append({
            "id": delta_id,
            "op": op,
            "span": {"start": int(start), "end": int(end)},
            "quote": quote,
            "replacement": replacement,
            "rationale": str(row.get("rationale") or row.get("reason") or "").strip(),
            "rule": str(row.get("rule") or row.get("rubric") or "").strip(),
            "severity": _severity_of(row),
            "markers": cited,
            "unknown_markers": unknown,
            "model_confidence": _clamp01(row.get("confidence"), 0.5),
            "confidence": _clamp01(row.get("confidence"), 0.5),
            "anchored": False,
            "anchor_layer": "unchecked",
            "label": LABEL_OPINION,
            "citations": [],
            "relocated": relocated,
            "notes": notes,
        })

    kept = resolve_overlaps(accepted, rejected)
    return {"deltas": kept, "rejected": rejected}


def resolve_overlaps(deltas: List[Dict[str, Any]],
                     rejected: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """Keep a non-overlapping set, highest severity/confidence first, and record
    each loser in ``rejected`` as ``overlaps <id>``. Deterministic: ties break
    on the longer span, then on the id, so the same batch always resolves the
    same way."""
    rejected = rejected if rejected is not None else []
    order = sorted(
        deltas,
        key=lambda d: (
            -_SEVERITY_RANK.get(d.get("severity"), 2),
            -float(d.get("model_confidence", d.get("confidence", 0.5)) or 0.0),
            -(d["span"]["end"] - d["span"]["start"]),
            d["span"]["start"],
            d["id"],
        ),
    )
    kept: List[Dict[str, Any]] = []
    for delta in order:
        clash = next((k for k in kept if _overlaps(delta, k)), None)
        if clash is not None:
            rejected.append({"id": delta["id"], "op": delta["op"],
                             "raw": {"span": delta["span"], "quote": delta.get("quote", "")},
                             "reason": f"overlaps {clash['id']}"})
            continue
        kept.append(delta)
    kept.sort(key=lambda d: (d["span"]["start"], d["span"]["end"], d["id"]))
    return kept


# ----------------------------------------------------------------------
# Applying deltas
# ----------------------------------------------------------------------


def apply_deltas(original: str, deltas: Iterable[Dict[str, Any]],
                 accept_ids: Optional[Iterable[str]] = None) -> str:
    """Apply the accepted deltas to the original, right to left.

    Right to left is not a style choice: every splice shifts the offsets after
    it, so applying in reverse order is what keeps the remaining spans valid
    without a fixup pass. ``accept_ids`` of ``None`` means all of them.
    Deterministic, and a delta with an unusable span is skipped rather than
    corrupting the text.
    """
    text = str(original or "")
    wanted = None if accept_ids is None else {str(i) for i in accept_ids}
    chosen: List[Dict[str, Any]] = []
    for delta in deltas or []:
        if not isinstance(delta, dict):
            continue
        if wanted is not None and str(delta.get("id") or "") not in wanted:
            continue
        span = delta.get("span") or {}
        try:
            start, end = int(span.get("start")), int(span.get("end"))
        except (TypeError, ValueError):
            continue
        if not (0 <= start <= end <= len(text)):
            continue
        chosen.append({"start": start, "end": end,
                       "replacement": "" if delta.get("op") == "KILL"
                                      else str(delta.get("replacement") or "")})
    chosen.sort(key=lambda d: (d["start"], d["end"]), reverse=True)
    for delta in chosen:
        text = text[:delta["start"]] + delta["replacement"] + text[delta["end"]:]
    return text


# ----------------------------------------------------------------------
# Anchoring: corpus rule vs model opinion
# ----------------------------------------------------------------------


def split_block_excerpts(block_text: str) -> Dict[str, str]:
    """``[C1] …text… [C2] …text…`` -> ``{"C1": "…", "C2": "…"}``.

    The block the expert already handed us contains the excerpt text, so
    anchoring costs no extra lookup and no model call.
    """
    text = str(block_text or "")
    hits = list(_BLOCK_MARKER_RE.finditer(text))
    out: Dict[str, str] = {}
    for i, m in enumerate(hits):
        end = hits[i + 1].start() if i + 1 < len(hits) else len(text)
        marker = f"C{int(m.group(1))}"
        out[marker] = text[m.end():end].strip()
    return out


def _supports(claim: str, chunk_text: str) -> Tuple[bool, str]:
    """Does the chunk support the claim? Three condensed layers, no LLM.

    1. **literal / near-literal** — a phrase the claim quotes appears in the
       chunk, or two consecutive key terms do, or most of the key terms do.
    2. **fuzzy** — enough of the claim's key terms appear anywhere in the
       chunk (token containment above a threshold).
    3. **nothing** — say so.

    Only the rationale and the rule are used as the claim. The replacement is
    the user's own prose and would anchor a correction to itself.
    """
    chunk = _fold(chunk_text)
    if not chunk.strip():
        return False, "none"
    claim_text = str(claim or "")
    terms = _terms(claim_text)
    if not terms:
        return False, "none"

    for quoted in re.findall(r"[\"“”'‘’«»]([^\"“”'‘’«»]{6,})[\"“”'‘’«»]", claim_text):
        if _fold(quoted).strip() and _fold(quoted).strip() in chunk:
            return True, "literal"

    chunk_terms = set(_terms(chunk_text))
    for a, b in zip(terms, terms[1:]):
        if f"{a} {b}" in chunk:
            return True, "literal"
    present = [t for t in terms if t in chunk_terms or t in chunk]
    ratio = len(present) / float(len(terms))
    if ratio >= LITERAL_TERM_RATIO:
        return True, "literal"
    if ratio >= FUZZY_TERM_RATIO:
        return True, "fuzzy"
    return False, "none"


def _citation_record(marker: str, chunk_id: Optional[str], slug: str,
                     known: bool, excerpt: str = "",
                     lookup: Optional[Callable[[str, str], Dict[str, Any]]] = None
                     ) -> Dict[str, Any]:
    """One rendered citation. A page is copied, never invented: a chunk whose
    page is unknown renders "source, page unknown" and ``page`` stays None."""
    source, page = "", None
    if known and chunk_id:
        try:
            fetch = lookup or _citation_fn()
            if fetch is not None:
                info = fetch(slug, chunk_id) or {}
                source = str(info.get("source") or "")
                raw_page = info.get("page")
                if isinstance(raw_page, bool):
                    raw_page = None
                if raw_page is not None and str(raw_page).strip() != "":
                    page = raw_page
                if not excerpt:
                    excerpt = str(info.get("excerpt") or "")
        except Exception as e:  # noqa: BLE001 - a missing citation is not a crash
            logger.debug("citation(%s, %s) failed: %s", slug, chunk_id, e)
    label = f"page {page}" if page is not None else PAGE_UNKNOWN
    ref_source = source or (str(chunk_id) if chunk_id else "unknown source")
    return {"marker": marker, "chunk_id": chunk_id, "source": source,
            "page": page, "page_label": label, "ref": f"{ref_source}, {label}",
            "excerpt": excerpt[:400], "known": bool(known)}


def verify_anchoring(delta: Dict[str, Any],
                     block_chunk_ids: Optional[Sequence[Any]] = None,
                     slug: str = "",
                     *, chunk_texts: Optional[Dict[str, str]] = None,
                     lookup: Optional[Callable[[str, str], Dict[str, Any]]] = None
                     ) -> Dict[str, Any]:
    """Decide whether this correction may claim to come from the corpus.

    A delta is ``anchored`` only when it names a marker that is really in the
    block **and** the text of that chunk supports its rationale by layer 1 or
    2. Everything else — no citation at all, a hallucinated ``[C9]``, or a
    citation whose chunk says nothing of the sort — is ``anchored: false`` and
    labelled *the model's opinion, not the corpus*. It is not dropped: the user
    may well want the correction. It just may never present itself as a book.

    ``confidence`` is set from the layer that passed, not from what the model
    claimed (kept alongside as ``model_confidence``).

    The chunk text comes from the block that was already fetched
    (``chunk_texts``, built by ``split_block_excerpts``), so nothing here calls
    a model or re-queries the corpus.
    """
    markers = block_markers(block_chunk_ids)
    cited = list(delta.get("markers") or [])
    if not cited:
        cited = _markers_of(delta)
    texts = chunk_texts or {}
    citations: List[Dict[str, Any]] = []
    anchored = False

    for marker in cited:
        known = marker in markers
        chunk_id = markers.get(marker)
        excerpt = texts.get(marker) or ""
        record = _citation_record(marker, chunk_id, slug, known, excerpt, lookup)
        if known:
            claim = " ".join(x for x in (delta.get("rationale"), delta.get("rule")) if x)
            supported, layer = _supports(claim, excerpt or record.get("excerpt") or "")
            record["supports"] = bool(supported)
            record["layer"] = layer
            if supported:
                anchored = True
        else:
            record["supports"] = False
            record["layer"] = "unknown_marker"
        citations.append(record)

    if anchored:
        layer = "literal" if any(c.get("layer") == "literal" and c.get("supports")
                                 for c in citations) else "fuzzy"
    elif not cited:
        layer = "no_citations"
    elif all(not c.get("known") for c in citations):
        layer = "unknown_marker"
    else:
        layer = "none"

    return {"anchored": bool(anchored), "anchor_layer": layer,
            "confidence": CONFIDENCE_BY_LAYER.get(layer, 0.25),
            "label": LABEL_CORPUS if anchored else LABEL_OPINION,
            "citations": citations}


def anchor_deltas(deltas: List[Dict[str, Any]], block: Optional[Dict[str, Any]],
                  slug: str = "",
                  lookup: Optional[Callable[[str, str], Dict[str, Any]]] = None
                  ) -> List[Dict[str, Any]]:
    """Run ``verify_anchoring`` over a batch, in place, and return it."""
    block = block or {}
    chunk_ids = list(block.get("chunk_ids") or [])
    texts = split_block_excerpts(block.get("text") or "")
    for delta in deltas:
        verdict = verify_anchoring(delta, chunk_ids, slug,
                                   chunk_texts=texts, lookup=lookup)
        delta.update(verdict)
        delta.pop("markers", None)
    return deltas


# ----------------------------------------------------------------------
# Scene chunking
# ----------------------------------------------------------------------

_SCENE_BREAK_RE = re.compile(
    r"\n[ \t]*(?:\*[ \t]*\*[ \t]*\*|#{1,6}[ \t]+\S[^\n]*|-{3,}|—{3,}|~{3,})[ \t]*\n")
_PARA_BREAK_RE = re.compile(r"\n[ \t]*\n")
_SENTENCE_END_RE = re.compile(r"(?<=[.!?…])[ \t]+|\n")


def _segments(text: str) -> List[Tuple[int, int]]:
    """The text cut at scene breaks first, paragraph breaks second. Segments
    are contiguous and cover the whole text, so a local offset plus the
    segment start is always a valid document offset."""
    breaks = {0, len(text)}
    for m in _SCENE_BREAK_RE.finditer(text):
        breaks.add(m.start() + 1)
        breaks.add(m.end())
    for m in _PARA_BREAK_RE.finditer(text):
        breaks.add(m.end())
    points = sorted(b for b in breaks if 0 <= b <= len(text))
    return [(points[i], points[i + 1]) for i in range(len(points) - 1)
            if points[i] < points[i + 1]]


def _hard_split(text: str, start: int, end: int, max_chars: int) -> List[Tuple[int, int]]:
    """A single paragraph longer than the budget, cut at sentence ends when
    possible and mid-word only as a last resort."""
    out: List[Tuple[int, int]] = []
    cursor = start
    while end - cursor > max_chars:
        window = text[cursor:cursor + max_chars]
        cut = -1
        for m in _SENTENCE_END_RE.finditer(window):
            if m.end() > max_chars * 0.4:
                cut = m.end()
        if cut <= 0:
            cut = max_chars
        out.append((cursor, cursor + cut))
        cursor += cut
    if cursor < end:
        out.append((cursor, end))
    return out


def split_scenes(text: str, max_chars: int = DEFAULT_CHUNK_CHARS) -> List[Dict[str, Any]]:
    """Cut the text into review-sized pieces at scene and paragraph borders.

    A short-context local model cannot correct a novel; it can correct a
    scene. The pieces are contiguous and carry their document offsets, which
    is what lets the per-chunk deltas merge back into one global list without
    a re-alignment pass. Never raises: a pathological input still comes back
    as one chunk covering the whole text.
    """
    body = str(text or "")
    try:
        budget = max(MIN_CHUNK_CHARS, int(max_chars or DEFAULT_CHUNK_CHARS))
    except (TypeError, ValueError):
        budget = DEFAULT_CHUNK_CHARS
    if not body:
        return []
    if len(body) <= budget:
        return [{"start": 0, "end": len(body), "text": body}]
    try:
        pieces: List[Tuple[int, int]] = []
        for seg_start, seg_end in _segments(body):
            if seg_end - seg_start > budget:
                pieces.extend(_hard_split(body, seg_start, seg_end, budget))
            else:
                pieces.append((seg_start, seg_end))
        merged: List[List[int]] = []
        for start, end in pieces:
            if merged and (end - merged[-1][0]) <= budget:
                merged[-1][1] = end
            else:
                merged.append([start, end])
        return [{"start": s, "end": e, "text": body[s:e]} for s, e in merged if e > s]
    except Exception as e:  # noqa: BLE001 - chunking must never lose the text
        logger.debug("split_scenes fell back to one chunk: %s", e)
        return [{"start": 0, "end": len(body), "text": body}]


# ----------------------------------------------------------------------
# The prompt
# ----------------------------------------------------------------------

DELTA_FORMAT_HELP = """\
Answer with NOTHING but one block in exactly this shape:

:::deltas
[
  {"op": "EDIT", "start": 0, "end": 0,
   "quote": "the exact text you are replacing, copied character for character",
   "replacement": "what it should say",
   "rationale": "one sentence saying why",
   "rule": "the checklist item this comes from",
   "severity": "low|medium|high",
   "cite": ["C1"],
   "confidence": 0.0}
]
:::

Rules for the block:
- op is EDIT (replace the span), ADD (insert; start == end, no quote needed) or
  KILL (delete the span; replacement must be "").
- start/end are character offsets into the text as given to you, end exclusive.
- quote is REQUIRED for EDIT and KILL and must be the text at those offsets. If
  your offsets are off, the quote is what saves the correction; if the quote is
  wrong the correction is thrown away.
- cite lists the [Cn] markers from the reference passages that support the
  correction. Cite the marker for EVERY correction that comes from the corpus.
- A correction that is your own judgement must have an empty cite list and say
  so plainly in the rationale. Never attribute your own opinion to a source.
- Do not invent a marker that is not in the reference passages.
- Return an empty array if the text needs no corrections."""

STANDING_RULES = """\
Standing rules:
- Work ONLY inside the text you are given. Do not continue it, summarize it, or
  rewrite it as a whole.
- Never return rewritten prose. Every observation is a span delta.
- Cite the [Cn] marker for every correction that comes from the reference
  passages; say plainly when a correction is your own judgement.
- Respect what the profile says not to touch.
- Preserve the author's voice: correct what is wrong, not what is unlike you."""


def build_review_prompt(expert: Dict[str, Any], text: str,
                        block: Optional[Dict[str, Any]] = None,
                        story_bible_block: str = "") -> List[Dict[str, str]]:
    """The messages for one review pass: the expert's instructions, its rubric
    as an ordered checklist, the corpus block with its ``[Cn]`` markers, the
    story-bible facts that bear on this passage, the delta format, and the
    standing rules.

    Returned as chat messages so ``llm_call`` can be any async callable that
    takes messages and returns text.
    """
    expert = expert or {}
    name = str(expert.get("name") or expert.get("slug") or "the expert")
    parts: List[str] = [f"You are {name}, reviewing a passage of the author's own text."]
    instructions = str(expert.get("instructions") or "").strip()
    if instructions:
        parts.append(instructions)

    rubric = expert.get("rubric")
    items: List[str] = []
    if isinstance(rubric, str):
        items = [line.strip(" -*\t") for line in rubric.splitlines() if line.strip()]
    elif isinstance(rubric, (list, tuple)):
        for item in rubric:
            if isinstance(item, dict):
                label = str(item.get("rule") or item.get("title") or item.get("text") or "").strip()
            else:
                label = str(item or "").strip()
            if label:
                items.append(label)
    if items:
        checklist = "\n".join(f"{i}. {item}" for i, item in enumerate(items, start=1))
        parts.append("Work through this checklist in order; name the item you "
                     "used in each correction's `rule`:\n" + checklist)

    parts.append(STANDING_RULES)
    system = "\n\n".join(parts)

    user_parts: List[str] = []
    block_text = str((block or {}).get("text") or "").strip()
    if block_text:
        user_parts.append("Reference passages from your corpus (cite them by "
                          "their [Cn] marker):\n" + block_text)
    elif block is not None:
        user_parts.append("No reference passages were retrieved for this "
                          "passage. Any correction you make is your own "
                          "judgement — say so and leave `cite` empty.")
    if str(story_bible_block or "").strip():
        user_parts.append("Story bible — what this project has already "
                          "established:\n" + str(story_bible_block).strip())
    user_parts.append("TEXT TO REVIEW (character offsets start at 0):\n"
                      + str(text or ""))
    user_parts.append(DELTA_FORMAT_HELP)

    return [{"role": "system", "content": system},
            {"role": "user", "content": "\n\n".join(user_parts)}]


# ----------------------------------------------------------------------
# services.experts, lazily and defensively
# ----------------------------------------------------------------------


def _experts_module():
    try:
        import services.experts as experts_mod   # noqa: PLC0415 - lazy on purpose
        return experts_mod
    except Exception as e:  # noqa: BLE001 - the module may not exist yet
        logger.debug("services.experts unavailable: %s", e)
        return None


def _experts_fn(name: str):
    module = _experts_module()
    fn = getattr(module, name, None) if module is not None else None
    return fn if callable(fn) else None


def _citation_fn():
    return _experts_fn("citation")


def load_expert(slug: str) -> Dict[str, Any]:
    """The expert profile, or an ExpertReviewError naming what is missing."""
    slug = str(slug or "").strip()
    if not slug:
        raise ExpertReviewError("An expert slug is required")
    fn = _experts_fn("load_expert")
    if fn is None:
        raise ExpertReviewError(
            "Experts are not configured on this instance (services.experts is "
            "unavailable), so there is nothing to review against")
    try:
        expert = fn(slug)
    except Exception as e:  # noqa: BLE001 - surface as a 400, never a 500
        raise ExpertReviewError(f"Could not load expert '{slug}': {e}")
    if not isinstance(expert, dict) or not expert:
        raise ExpertReviewError(f"No expert named '{slug}'")
    return expert


def fetch_block(slug: str, query: str, char_budget: int) -> Dict[str, Any]:
    """``expert_block``, degrading to an empty (degraded) block. Never raises —
    a corpus that cannot be read costs the citations, not the review."""
    fn = _experts_fn("expert_block")
    if fn is None:
        return {"text": "", "chunk_ids": [], "degraded": True}
    try:
        block = fn(slug, query, char_budget)
    except Exception as e:  # noqa: BLE001 - hot path
        logger.debug("expert_block(%s) failed: %s", slug, e)
        return {"text": "", "chunk_ids": [], "degraded": True}
    if not isinstance(block, dict):
        return {"text": "", "chunk_ids": [], "degraded": True}
    return {"text": str(block.get("text") or ""),
            "chunk_ids": list(block.get("chunk_ids") or []),
            "degraded": bool(block.get("degraded"))}


def record_feedback(slug: str, accepted: int = 0, rejected: int = 0) -> Dict[str, Any]:
    """Report review outcomes back to the expert so its retrieval can learn.
    Never raises: feedback is a nice-to-have, not part of the answer."""
    try:
        accepted = max(0, int(accepted or 0))
        rejected = max(0, int(rejected or 0))
    except (TypeError, ValueError):
        return {"recorded": False, "error": "accepted/rejected must be integers"}
    fn = _experts_fn("record_feedback")
    if fn is None:
        return {"recorded": False, "accepted": accepted, "rejected": rejected,
                "error": "Experts are not configured on this instance"}
    try:
        fn(slug, accepted, rejected)
        return {"recorded": True, "slug": slug, "accepted": accepted, "rejected": rejected}
    except Exception as e:  # noqa: BLE001 - never raise on a feedback report
        logger.debug("record_feedback(%s) failed: %s", slug, e)
        return {"recorded": False, "accepted": accepted, "rejected": rejected,
                "error": str(e)}


# ----------------------------------------------------------------------
# The review pass
# ----------------------------------------------------------------------


def _story_text_for(story: Any, text: str, start: int, end: int) -> str:
    """The story-bible section for one chunk.

    ``story`` may be a ready-made string (used as-is), a list of continuity
    findings (filtered to the ones that land inside this chunk), or a bible
    dict (checked against this chunk's text). Never raises.
    """
    try:
        if story is None:
            return ""
        if isinstance(story, str):
            return story.strip()
        from src import story_bible as bible_mod   # noqa: PLC0415 - lazy
        if isinstance(story, dict) and any(k in story for k in
                                           ("characters", "facts", "timeline", "places")):
            findings = bible_mod.check_continuity(text[start:end], story)
            summary = bible_mod.bible_summary(story)
            rendered = bible_mod.render_findings(findings)
            parts = []
            if summary:
                parts.append("Recorded so far:\n" + summary)
            if rendered:
                parts.append("Possible continuity problems in this passage:\n" + rendered)
            return "\n\n".join(parts)
        if isinstance(story, (list, tuple)):
            here = []
            for finding in story:
                if not isinstance(finding, dict):
                    continue
                span = finding.get("text_span") or {}
                pos = span.get("start") if isinstance(span, dict) else None
                if pos is None or (start <= int(pos) < end):
                    here.append(finding)
            rendered = bible_mod.render_findings(here)
            return ("Possible continuity problems in this passage:\n" + rendered
                    ) if rendered else ""
    except Exception as e:  # noqa: BLE001 - the bible is optional context
        logger.debug("story block for %s-%s failed: %s", start, end, e)
    return ""


async def _call(llm_call: Callable, messages: List[Dict[str, str]]) -> str:
    """Call the injected model function, sync or async, and return its text."""
    result = llm_call(messages)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, dict):
        result = result.get("content") or result.get("text") or ""
    return str(result or "")


async def review(slug: str, text: str, *, llm_call: Callable,
                 story: Any = None, max_chars: Optional[int] = None,
                 char_budget: int = DEFAULT_BLOCK_BUDGET) -> Dict[str, Any]:
    """Run one expert review over ``text`` and return the typed deltas.

    The text is cut into scenes (``split_scenes``); each scene gets its own
    corpus block and its own model call, and the deltas come back merged into
    **original-document offsets**. A scene whose model call fails is recorded
    in ``errors`` and marks the result ``degraded`` — the rest of the review
    still lands, because losing four scenes because the fifth timed out is
    worse than an incomplete pass.

    ``llm_call`` takes the messages and returns the answer text (sync or
    async), so this is fully testable without a model.

    Returns::

        {"expert": {...}, "deltas": [...], "rejected": [...],
         "anchored_count": int, "opinion_count": int, "degraded": bool,
         "citations": [...], "chunks": int, "errors": [...], "text_chars": int}
    """
    body = str(text or "")
    if not body.strip():
        raise ExpertReviewError("There is no text to review")
    if not callable(llm_call):
        raise ExpertReviewError("review() needs an llm_call to talk to a model")
    expert = load_expert(slug)

    chunks = split_scenes(body, max_chars or DEFAULT_CHUNK_CHARS)
    deltas: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    degraded = False
    next_id = 1

    for index, chunk in enumerate(chunks):
        block = fetch_block(slug, chunk["text"], char_budget)
        degraded = degraded or bool(block.get("degraded"))
        messages = build_review_prompt(
            expert, chunk["text"], block,
            _story_text_for(story, body, chunk["start"], chunk["end"]))
        try:
            answer = await _call(llm_call, messages)
        except Exception as e:  # noqa: BLE001 - one bad scene is not the pass
            logger.warning("expert review chunk %s failed: %s", index, e)
            errors.append({"chunk": index, "error": str(e)})
            degraded = True
            continue
        parsed = parse_corrections(answer, chunk["text"], block.get("chunk_ids"),
                                   id_start=next_id)
        next_id += len(parsed["deltas"]) + len(parsed["rejected"])
        anchor_deltas(parsed["deltas"], block, slug)
        for delta in parsed["deltas"]:
            delta["span"] = {"start": delta["span"]["start"] + chunk["start"],
                             "end": delta["span"]["end"] + chunk["start"]}
            delta["chunk"] = index
            deltas.append(delta)
        for row in parsed["rejected"]:
            row["chunk"] = index
            rejected.append(row)

    deltas = resolve_overlaps(deltas, rejected)

    citations: List[Dict[str, Any]] = []
    seen = set()
    for delta in deltas:
        for citation in delta.get("citations") or []:
            key = (citation.get("chunk_id"), citation.get("marker"))
            if key in seen:
                continue
            seen.add(key)
            citations.append(citation)

    anchored_count = sum(1 for d in deltas if d.get("anchored"))
    return {
        "expert": {"slug": str(expert.get("slug") or slug),
                   "name": str(expert.get("name") or slug),
                   "model": str(expert.get("model") or "")},
        "deltas": deltas,
        "rejected": rejected,
        "anchored_count": anchored_count,
        "opinion_count": len(deltas) - anchored_count,
        "degraded": bool(degraded),
        "citations": citations,
        "chunks": len(chunks),
        "errors": errors,
        "text_chars": len(body),
    }


# ----------------------------------------------------------------------
# Rendering (the compact answer the agent tool returns)
# ----------------------------------------------------------------------


def _shorten(text: str, cap: int = 60) -> str:
    one_line = " ".join(str(text or "").split())
    return one_line if len(one_line) <= cap else one_line[:cap - 1] + "…"


def format_delta(delta: Dict[str, Any]) -> str:
    """One delta, one line. ``D1 EDIT 12-31 high · corpus [C1 Brenner.pdf,
    page 42] "was walking" -> "walked" — passive voice (rule 2)``"""
    span = delta.get("span") or {}
    head = (f"{delta.get('id')} {delta.get('op')} "
            f"{span.get('start')}-{span.get('end')} {delta.get('severity')}")
    label = delta.get("label") or (LABEL_CORPUS if delta.get("anchored") else LABEL_OPINION)
    cites = [c for c in delta.get("citations") or [] if c.get("supports")]
    if cites:
        marks = "; ".join(f"{c.get('marker')} {c.get('ref')}" for c in cites)
        tail = f"· {label} [{marks}]"
    else:
        tail = f"· {label}"
    body = ""
    if delta.get("op") == "EDIT":
        body = f' "{_shorten(delta.get("quote"))}" -> "{_shorten(delta.get("replacement"))}"'
    elif delta.get("op") == "ADD":
        body = f' insert "{_shorten(delta.get("replacement"))}"'
    elif delta.get("op") == "KILL":
        body = f' cut "{_shorten(delta.get("quote"))}"'
    why = _shorten(delta.get("rationale"), 120)
    rule = delta.get("rule")
    if rule:
        why = f"{why} ({_shorten(rule, 40)})" if why else f"({_shorten(rule, 40)})"
    line = f"{head} {tail}{body}"
    if why:
        line += f" — {why}"
    if delta.get("relocated"):
        line += " [span relocated to its quote]"
    return line


def compact_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """The review as the agent tool returns it: the same structure plus a
    rendered ``summary``, with the bulk trimmed.

    Each delta keeps its citation's source, page and marker — everything a
    caller needs to show where a correction came from — but not the excerpt
    text, which is already in the top-level ``citations`` list and would
    otherwise be repeated once per correction.
    """
    result = dict(result or {})
    deltas = []
    for delta in result.get("deltas") or []:
        delta = dict(delta)
        delta["citations"] = [{k: v for k, v in (c or {}).items() if k != "excerpt"}
                              for c in delta.get("citations") or []]
        deltas.append(delta)
    result["deltas"] = deltas
    result["rejected"] = [
        {"id": row.get("id"), "op": row.get("op"), "chunk": row.get("chunk"),
         "reason": row.get("reason"),
         "quote": _shorten(str((row.get("raw") or {}).get("quote") or ""), 80)}
        for row in result.get("rejected") or []]
    result["rejected_count"] = len(result["rejected"])
    result["summary"] = format_review(result)
    return result


def format_review(result: Dict[str, Any], max_lines: int = 60) -> str:
    """The compact review: the counts, then one line per delta. The honesty
    rule is visible in the text itself, not left to the reader to infer."""
    result = result or {}
    deltas = result.get("deltas") or []
    head = (f"{len(deltas)} correction(s): {result.get('anchored_count', 0)} anchored "
            f"to the corpus, {result.get('opinion_count', 0)} the model's own opinion"
            f" — {len(result.get('rejected') or [])} rejected")
    if result.get("degraded"):
        head += " (corpus degraded)"
    lines = [head]
    for delta in deltas[:max_lines]:
        lines.append(format_delta(delta))
    if len(deltas) > max_lines:
        lines.append(f"[{len(deltas) - max_lines} more corrections not shown]")
    for row in (result.get("rejected") or [])[:20]:
        lines.append(f"rejected {row.get('id') or '?'}: {row.get('reason')}")
    return "\n".join(lines)
