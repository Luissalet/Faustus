"""Contract criteria: the normative statements of a long task message, kept as
a book of requirements the turn has to show evidence for.

A long unattended task is judged on what its statement asks for, clause by
clause. The completion audit used to ask the model to list those clauses
itself, and the model grouped them ("put/get/compact stream" became one line
that a measurement of `put` alone "proved"). This module extracts the clauses
deterministically, one criterion per subject, so each one needs its own
evidence:

    source_text(message)                  -> the text to read (typed request + text attachments)
    extract(text, max_items=60)           -> (criteria, dropped)
    CriteriaBook(criteria, ...)           -> per-turn state: the calls of the turn,
                                             links to them, persistence, notes

Pure and deterministic: regular expressions over the user's own words, no
model call, no settings read. The loop (src/agent_loop.py) builds the book,
records every tool call of the turn into it and writes the audit and gap
notes from it; the `criteria_check` tool (src/agent_tools/contract_tools.py)
links criteria to calls; the state of a criterion is decided by
`src.verification.verify_contract_link`, never by the model.

States are the `contracts.task.ACCEPTANCE_STATES` names this needs:
pending / proven / unmet / not_applicable.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

STATES = ("pending", "proven", "unmet", "not_applicable")
OPEN_STATES = ("pending", "unmet")
MODES = ("off", "unattended", "always")
DEFAULT_MAX = 60
DEFAULT_MIN_CHARS = 1500
#: Fewer criteria than this and the message is not treated as a contract: a
#: long message with one "must" is a request, not a specification.
MIN_CRITERIA = 3
QUOTE_MAX = 400
#: Calls kept per turn for linking (oldest dropped first).
CALLS_MAX = 400
_CALL_TEXT_HEAD = 3000
_CALL_TEXT_TAIL = 3000
#: A sentence longer than this whose comma-separated parts carry their own
#: normative marks is split into one criterion per part.
_LONG_SENTENCE = 200

# ---------------------------------------------------------------------------
# Markers
# ---------------------------------------------------------------------------

_NORMATIVE_RE = re.compile(
    r"\b(?:must(?:\s+not)?|mustn't|shall(?:\s+not)?|never|only|exactly|at\s+most|at\s+least|within|"
    r"required|always|no\s+more\s+than|no\s+fewer\s+than|no\s+later\s+than|"
    r"no\s+debe(?:n|r[áa]n?)?|debe(?:n|r[áa]n?)?|s[óo]lo|solamente|[úu]nicamente|nunca|jam[áa]s|"
    r"exactamente|como\s+m[áa]ximo|como\s+m[íi]nimo|al\s+menos|siempre|obligatori[oa]s?|requerid[oa]s?)\b",
    re.I,
)
_UNITS = (
    r"(?:[kmgt]i?b|bytes?|ms|µs|us|ns|s|secs?|seconds?|mins?|minutes?|h|hrs?|hours?|days?|%|"
    r"chars?|characters?|lines?|rows?|items?|entries|records?|requests?|tokens?|px|"
    r"segundos?|minutos?|horas?|d[íi]as?|caracteres|l[íi]neas|filas|elementos|registros|peticiones)"
)
_LIMIT_RE = re.compile(r"(?<![\w.])(\d+(?:[.,]\d+)?)\s?" + _UNITS + r"(?![\w])", re.I)
_COMPARE_RE = re.compile(
    r"(?:\b(?:under|over|below|above|less\s+than|more\s+than|fewer\s+than|up\s+to|max(?:imum)?|min(?:imum)?|"
    r"within|at\s+most|at\s+least|exceed\w*|no\s+more|no\s+less|menos\s+de|m[áa]s\s+de|hasta|"
    r"m[áa]ximo|m[íi]nimo|como\s+m[áa]ximo|al\s+menos|no\s+super\w*|por\s+debajo|por\s+encima)\b|[<>≤≥])",
    re.I,
)
_TICK_RE = re.compile(r"`([^`\n]{1,80})`")
_ERROR_CODE_RE = re.compile(
    r"(?:\b(?:code|error|status|fails?\s+with|exits?(?:\s+with)?|returns?|rejects?(?:\s+with)?|"
    r"c[óo]digo|falla\s+con|devuelve|sale\s+con)\s*[:=]?\s*(?:code\s+|c[óo]digo\s+)?`[^`\n]{1,40}`"
    r"|\(`[A-Za-z_][\w.-]{0,40}`\)"
    r"|\bexits?\s+(?:with\s+)?(?:code\s+)?\d{1,3}\b|\bsale\s+con\s+(?:c[óo]digo\s+)?\d{1,3}\b)",
    re.I,
)
_FORMAT_TICK_RE = re.compile(r"YYYY|yyyy|HH:MM|hh:mm|MM-DD|DD/MM|\.\.\.|\[0-9\]|\\d|%[YmdHMSf]|^\^|\$$")

_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^\s{0,3}(```|~~~)")
_ITEM_RE = re.compile(r"^(\s*)(?:[-*+•]|(\d{1,3})[.)])\s+")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{3,}")
_ATTACH_RE = re.compile(r"(?:^|\n)=== (File|ZIP archive): (.+?) ===\n?")
_TEXT_EXT_RE = re.compile(r"(?:\.(?:md|markdown|txt|rst|text)|^[^.]+)$", re.I)

_STOPWORDS = frozenset("""
about above after again against all also although among another any are because been before being
below between both but cannot could does doing down during each either every from further have having
here into itself just like made make many more most much must never none only other otherwise over
same shall should some such than that their them then there these they this those through under until
upon very what when where whether which while with within without would your exactly always least most
required value values given using used every
antes como cada cual cuando debe deben desde donde entre esta este esto estos hasta mientras mismo
nunca otra otro para pero porque puede sobre solo sólo siempre sino tambien también tiene todos
""".split())

_COMPOUND_RE = re.compile(
    r"^\s*(?:(?:both|the\s+commands?|the\s+functions?|the\s+endpoints?|los\s+comandos|las\s+funciones|tanto)\s+)?"
    r"(`[^`\n]{1,60}`(?:\s*,\s*`[^`\n]{1,60}`)*\s*,?\s*(?:and|or|y|e|o|u|&)\s+`[^`\n]{1,60}`)\s+\S",
    re.I,
)
_ENUM_RE = re.compile(
    r"\b(?:errors?|cases?|failures?|conditions?|inputs?|situations?|errores|casos|fallos?|condiciones|entradas)\b"
    r"[^()]{0,30}\(([^()]{3,240})\)",
    re.I,
)
_PAIR_RE = re.compile(r"^(\w+)\s+(?:or|and|o|u|y|e)\s+(\w+)\s+(\w[\w -]*)$", re.I)


# ---------------------------------------------------------------------------
# Reading the text
# ---------------------------------------------------------------------------

def source_text(message: str) -> List[Tuple[str, str]]:
    """The parts of a user message to read: the typed request and every
    inlined text attachment (`=== File: spec.md ===`), as `(source, text)`.
    Code files and ZIP archives are not task statements and are skipped."""
    body = message or ""
    marks = list(_ATTACH_RE.finditer(body))
    if not marks:
        return [("message", body)] if body.strip() else []
    parts: List[Tuple[str, str]] = []
    head = body[:marks[0].start()]
    if head.strip():
        parts.append(("message", head))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(body)
        name = m.group(2).strip()
        base = name.replace("\\", "/").rsplit("/", 1)[-1]
        if m.group(1) == "File" and _TEXT_EXT_RE.search(base):
            text = body[m.end():end]
            if text.strip():
                parts.append((name, text))
    return parts


def _blocks(text: str) -> List[Dict[str, Any]]:
    """Paragraphs, list items and table rows, with the line each one of its
    physical lines starts at and the heading path it sits under."""
    out: List[Dict[str, Any]] = []
    heads: List[Tuple[int, str]] = []
    num: Optional[str] = None
    cur: Optional[Dict[str, Any]] = None
    in_fence = False

    def section() -> str:
        path = " › ".join(t for _, t in heads)
        if num:
            path = (path + " › " + num) if path else num
        return path

    def flush() -> None:
        nonlocal cur
        if cur and cur["text"].strip():
            out.append(cur)
        cur = None

    for lineno, raw in enumerate((text or "").splitlines(), start=1):
        line = raw.rstrip()
        if _FENCE_RE.match(line):
            flush()
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if not line.strip():
            flush()
            continue
        h = _HEADING_RE.match(line)
        if h:
            flush()
            level = len(h.group(1))
            while heads and heads[-1][0] >= level:
                heads.pop()
            heads.append((level, h.group(2).strip()))
            num = None
            continue
        if line.lstrip().startswith("|"):
            flush()
            if _TABLE_SEP_RE.match(line.replace("|", " ", 1)) or set(line.strip()) <= set("|:- "):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            out.append({"text": " | ".join(c for c in cells if c), "segs": [(0, lineno)], "section": section()})
            continue
        item = _ITEM_RE.match(line)
        if item:
            flush()
            if item.group(2) and not item.group(1):
                num = item.group(2)
            body = line[item.end():].strip()
            cur = {"text": body, "segs": [(0, lineno)], "section": section()}
            continue
        if cur is None:
            if not raw.startswith((" ", "\t")):
                num = None
            cur = {"text": line.strip(), "segs": [(0, lineno)], "section": section()}
        else:
            cur["segs"].append((len(cur["text"]) + 1, lineno))
            cur["text"] += " " + line.strip()
    flush()
    return out


def _line_at(segs: Sequence[Tuple[int, int]], offset: int) -> int:
    line = segs[0][1]
    for start, no in segs:
        if start <= offset:
            line = no
        else:
            break
    return line


def _sentences(text: str) -> List[Tuple[int, str]]:
    """Split one block into sentences: `.`/`!`/`?` before a capital, a digit or
    a backtick, `;`, and `:` before a capital or a backtick. Never inside
    backticks or parentheses."""
    out: List[Tuple[int, str]] = []
    start = 0
    tick = False
    depth = 0
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        if ch == "`":
            tick = not tick
        elif not tick:
            if ch in "([":
                depth += 1
            elif ch in ")]" and depth:
                depth -= 1
            elif depth == 0 and ch in ".;:!?" and i + 1 < n and text[i + 1] == " ":
                j = i + 1
                while j < n and text[j] == " ":
                    j += 1
                nxt = text[j] if j < n else ""
                cut = False
                if ch == ";":
                    cut = True
                elif ch == ":":
                    cut = bool(nxt) and (nxt.isupper() or nxt == "`")
                else:
                    cut = bool(nxt) and (nxt.isupper() or nxt.isdigit() or nxt in "`\"'(¿¡")
                if cut:
                    piece = text[start:i + 1].strip()
                    if piece:
                        out.append((start, piece))
                    start = j
                    i = j
                    continue
        i += 1
    piece = text[start:].strip()
    if piece:
        out.append((start, piece))
    return out


def _split_top(text: str, sep: str = ",") -> List[str]:
    """Split on `sep` outside backticks, parentheses and brackets."""
    parts: List[str] = []
    buf: List[str] = []
    tick = False
    depth = 0
    for ch in text:
        if ch == "`":
            tick = not tick
        elif not tick:
            if ch in "([{":
                depth += 1
            elif ch in ")]}" and depth:
                depth -= 1
            elif ch == sep and depth == 0:
                parts.append("".join(buf))
                buf = []
                continue
        buf.append(ch)
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


# ---------------------------------------------------------------------------
# Classifying and expanding one sentence
# ---------------------------------------------------------------------------

def _has_limit(text: str) -> bool:
    return bool(_LIMIT_RE.search(text)) and bool(_COMPARE_RE.search(text))


def _has_format(text: str) -> bool:
    return any(_FORMAT_TICK_RE.search(t) for t in _TICK_RE.findall(text))


def is_normative(text: str) -> bool:
    """A requirement, not a description: a modal or quantifier (English or
    Spanish), a numeric limit with a unit and a comparison, an error code in
    backticks, or a literal format. Questions never are."""
    s = (text or "").strip()
    if len(s) < 12 or s.endswith("?") or s.startswith("¿"):
        return False
    return bool(_NORMATIVE_RE.search(s) or _has_limit(s) or _ERROR_CODE_RE.search(s) or _has_format(s))


def _kind(text: str) -> str:
    if _has_limit(text) or re.search(r"\b(?:at\s+most|at\s+least|como\s+m[áa]ximo|al\s+menos)\b", text, re.I):
        return "limit"
    if _ERROR_CODE_RE.search(text):
        return "error_code"
    if _has_format(text):
        return "format"
    return "behaviour"


def _marked(text: str) -> bool:
    """Carries its own mark: normative word, limit, backtick literal or code."""
    return bool(_NORMATIVE_RE.search(text) or _LIMIT_RE.search(text) or _TICK_RE.search(text)
                or _ERROR_CODE_RE.search(text))


def _clip(text: str, focus: Optional[str] = None, limit: int = QUOTE_MAX) -> str:
    s = re.sub(r"\s+", " ", text or "").strip()
    if len(s) <= limit:
        return s
    if focus:
        at = s.lower().find(focus.lower())
        if at > limit - 60:
            lo = max(0, at - limit // 2)
            return "…" + s[lo:lo + limit - 2].strip() + "…"
    return s[:limit - 1].rstrip() + "…"


def _words(text: str, limit: int = 8) -> List[str]:
    out: List[str] = []
    plain = _TICK_RE.sub(" ", text)
    for w in re.findall(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]{5,}", plain):
        lw = w.lower()
        if lw in _STOPWORDS or _NORMATIVE_RE.fullmatch(lw) or lw in out:
            continue
        out.append(lw)
        if len(out) >= limit:
            break
    return out


def _keys(text: str, subject: Optional[str], exclude: Sequence[str] = ()) -> List[str]:
    """Distinctive tokens a check of this criterion is expected to mention:
    backtick literals, limits (number and unit), quoted strings, dotted/snake
    names. `exclude`: the sibling subjects of a compound sentence (a check of
    `get` need not name `put`)."""
    out: List[str] = []
    skip = {s for s in exclude if s and s != subject}

    def add(tok: str) -> None:
        tok = (tok or "").strip()
        if len(tok) >= 1 and tok not in out and tok not in skip and len(out) < 12:
            out.append(tok)

    if subject:
        add(subject)
    for t in _TICK_RE.findall(text):
        add(t)
    for m in _LIMIT_RE.finditer(text):
        add(m.group(1))
        unit = m.group(0)[len(m.group(1)):].strip()
        if len(unit) >= 2:
            add(unit)
    for t in re.findall(r'"([^"\n]{2,40})"', text):
        add(t)
    for t in re.findall(r"\b[A-Za-z][A-Za-z0-9]*(?:[_.-][A-Za-z0-9]+)+\b", _TICK_RE.sub(" ", text)):
        add(t)
    return out


def _compound_subjects(sentence: str) -> List[str]:
    m = _COMPOUND_RE.match(sentence)
    if not m:
        return []
    subs = [t.strip() for t in _TICK_RE.findall(m.group(1)) if t.strip()]
    return subs if len(subs) >= 2 else []


def _enum_cases(sentence: str) -> List[str]:
    """`a usage error (unknown command, missing or malformed argument: code
    `invalid`)` -> unknown command / missing argument / malformed argument."""
    m = _ENUM_RE.search(sentence)
    if not m:
        return []
    inner = m.group(1).split(":", 1)[0]
    items = [re.sub(r"^(?:and|or|y|o|e|u)\s+", "", p, flags=re.I).strip(" .") for p in _split_top(inner)]
    cases: List[str] = []
    for it in items:
        if not it:
            continue
        if len(it.split()) > 6:
            return []
        pm = _PAIR_RE.match(it)
        if pm:
            cases.append(f"{pm.group(1)} {pm.group(3)}".strip())
            cases.append(f"{pm.group(2)} {pm.group(3)}".strip())
        else:
            cases.append(it)
    out: List[str] = []
    for c in cases:
        if c.lower() not in [o.lower() for o in out]:
            out.append(c)
    return out if len(out) >= 2 else []


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def _criterion(quote: str, *, line: int, section: str, source: str, subject: Optional[str],
               basis: str, siblings: Sequence[str] = ()) -> Dict[str, Any]:
    q = _clip(quote, subject)
    return {
        "id": "",
        "quote": q,
        "line": line,
        "section": section,
        "source": source,
        "subject": subject or None,
        "kind": _kind(basis),
        "keys": _keys(basis, subject, siblings),
        "words": _words(basis),
        "state": "pending",
        "evidence_refs": [],
        "detail": "",
    }


def _from_sentence(sentence: str, *, line: int, section: str, source: str) -> List[Dict[str, Any]]:
    """One normative sentence -> one or more criteria."""
    # A long step that packs several rules into one comma list (`Shape:
    # exactly those five keys, `version` is the integer 1, instants in the
    # canonical `...Z` form, ...`) becomes one criterion per marked part.
    if len(sentence) > _LONG_SENTENCE and sentence.count(",") >= 3:
        lead, rest = "", sentence
        colon = sentence.find(": ")
        if 0 < colon < 60:
            lead, rest = sentence[:colon].strip(), sentence[colon + 2:]
        parts = [re.sub(r"^(?:and|y)\s+", "", p, flags=re.I) for p in _split_top(rest)]
        marked = [p for p in parts if _marked(p) and len(p) >= 8]
        if len(marked) >= 2:
            out: List[Dict[str, Any]] = []
            for p in marked:
                text = (lead + ": " + p) if lead else p
                out.extend(_from_sentence(text, line=line, section=section, source=source)
                           if len(text) <= _LONG_SENTENCE else
                           [_criterion(text, line=line, section=section, source=source, subject=None, basis=text)])
            return out
    subjects = _compound_subjects(sentence)
    if subjects:
        return [_criterion(sentence, line=line, section=section, source=source, subject=s, basis=sentence,
                           siblings=subjects)
                for s in subjects]
    cases = _enum_cases(sentence)
    if cases:
        return [_criterion(sentence, line=line, section=section, source=source, subject=c, basis=sentence)
                for c in cases]
    return [_criterion(sentence, line=line, section=section, source=source, subject=None, basis=sentence)]


def extract(text: str, *, max_items: int = DEFAULT_MAX, source: str = "message") -> Tuple[List[Dict[str, Any]], int]:
    """The normative statements of `text` as criteria, in document order, with
    ids `C01`... Returns `(criteria, dropped)`: when there are more than
    `max_items`, limits, error codes and literal formats (the most checkable
    and the most often missed) are kept first and `dropped` counts the rest."""
    return extract_parts([(source, text)], max_items=max_items)


def extract_parts(parts: Iterable[Tuple[str, str]], *, max_items: int = DEFAULT_MAX) -> Tuple[List[Dict[str, Any]], int]:
    found: List[Dict[str, Any]] = []
    seen = set()
    for source, text in parts:
        for block in _blocks(text):
            for offset, sentence in _sentences(block["text"]):
                if not is_normative(sentence):
                    continue
                line = _line_at(block["segs"], offset)
                for c in _from_sentence(sentence, line=line, section=block["section"], source=source):
                    key = (_norm(c["quote"]), _norm(c["subject"] or ""))
                    if key in seen:
                        continue
                    seen.add(key)
                    found.append(c)
    max_items = max(0, int(max_items))
    dropped = 0
    if len(found) > max_items:
        order = sorted(range(len(found)), key=lambda i: (found[i]["kind"] == "behaviour", i))
        keep = sorted(order[:max_items])
        dropped = len(found) - max_items
        found = [found[i] for i in keep]
    width = 2 if len(found) < 100 else 3
    for n, c in enumerate(found, start=1):
        c["id"] = "C" + str(n).zfill(width)
    return found, dropped


def text_key(parts: Sequence[Tuple[str, str]]) -> str:
    h = hashlib.sha256()
    for source, text in parts:
        h.update(source.encode("utf-8", "replace"))
        h.update(b"\0")
        h.update(_norm(text).encode("utf-8", "replace"))
    return h.hexdigest()[:16]


def mode_applies(mode: Any, *, unattended: bool) -> bool:
    m = str(mode or "").strip().lower()
    if m == "always":
        return True
    if m == "unattended":
        return bool(unattended)
    return False


# ---------------------------------------------------------------------------
# The turn's book
# ---------------------------------------------------------------------------

_DOC_ONLY_RE = re.compile(r"\.(?:md|markdown|txt|rst)$", re.I)


def _result_text(result: Any) -> str:
    if isinstance(result, str):
        return result
    if not isinstance(result, dict):
        return ""
    pieces = []
    for k in ("output", "stdout", "stderr", "error", "content", "result"):
        v = result.get(k)
        if isinstance(v, str) and v:
            pieces.append(v)
        elif isinstance(v, (dict, list)) and v:
            try:
                pieces.append(json.dumps(v, ensure_ascii=False, default=str))
            except Exception:  # noqa: BLE001
                pass
    if not pieces:
        try:
            pieces.append(json.dumps(result, ensure_ascii=False, default=str))
        except Exception:  # noqa: BLE001
            return ""
    return "\n".join(pieces)


def _bounded(text: str) -> str:
    text = text or ""
    if len(text) <= _CALL_TEXT_HEAD + _CALL_TEXT_TAIL:
        return text
    return text[:_CALL_TEXT_HEAD] + "\n…\n" + text[-_CALL_TEXT_TAIL:]


def _command_text(tool: str, content: str) -> str:
    try:
        args = json.loads(content)
    except (TypeError, ValueError):
        return str(content or "")
    if isinstance(args, dict):
        for k in ("command", "cmd", "code", "script", "pattern", "path", "query"):
            v = args.get(k)
            if isinstance(v, str) and v.strip():
                rest = {kk: vv for kk, vv in args.items() if kk != k}
                return v + ((" " + json.dumps(rest, ensure_ascii=False, default=str)) if rest else "")
    return json.dumps(args, ensure_ascii=False, default=str)


class CriteriaBook:
    """The criteria of one task statement and the evidence linked to them.

    Built once per turn; `record_call` is fed every tool call the turn runs
    (command and bounded output), so a link can be checked against what the
    call really showed. Persisted beside the plan tracker of the same scope
    (`data/plan_tracker/<scope>/criteria/<key>.json`), so a continuation of
    the same task keeps the states it already earned."""

    def __init__(self, criteria: List[Dict[str, Any]], *, key: str = "", scope: Optional[str] = None,
                 dropped: int = 0, strict: bool = True, created_at: Optional[float] = None) -> None:
        self.criteria = criteria
        self.key = key
        self.scope = scope
        self.dropped = int(dropped or 0)
        self.strict = bool(strict)
        self.created_at = created_at or time.time()
        self.calls: List[Dict[str, Any]] = []
        self.seq = 0
        self.last_mutation_seq = -1
        self.loaded = False

    # -- building ------------------------------------------------------------
    @classmethod
    def from_message(cls, message: str, *, max_items: int = DEFAULT_MAX, min_chars: int = DEFAULT_MIN_CHARS,
                     scope: Optional[str] = None, strict: bool = True,
                     min_criteria: int = MIN_CRITERIA) -> Optional["CriteriaBook"]:
        """None when the message is too short or states too few requirements."""
        parts = source_text(message)
        if sum(len(t) for _, t in parts) < max(0, int(min_chars)):
            return None
        criteria, dropped = extract_parts(parts, max_items=max_items)
        if len(criteria) < max(1, int(min_criteria)):
            return None
        book = cls(criteria, key=text_key(parts), scope=scope, dropped=dropped, strict=strict)
        if scope:
            saved = load(scope, book.key)
            if saved is not None:
                book.merge_states(saved)
                book.loaded = True
        return book

    def merge_states(self, other: "CriteriaBook") -> None:
        """Keep the states an earlier run of the same statement earned."""
        by_key = {(_norm(c.get("quote")), _norm(c.get("subject") or "")): c for c in other.criteria}
        for c in self.criteria:
            old = by_key.get((_norm(c.get("quote")), _norm(c.get("subject") or "")))
            if old and old.get("state") in STATES:
                c["state"] = old["state"]
                c["evidence_refs"] = list(old.get("evidence_refs") or [])
                c["detail"] = str(old.get("detail") or "")

    # -- reading -------------------------------------------------------------
    def get(self, cid: str) -> Optional[Dict[str, Any]]:
        cid = str(cid or "").strip().upper()
        for c in self.criteria:
            if c["id"] == cid:
                return c
        return None

    def open(self) -> List[Dict[str, Any]]:
        return [c for c in self.criteria if c.get("state") in OPEN_STATES]

    def counts(self) -> Dict[str, int]:
        out = {s: 0 for s in STATES}
        for c in self.criteria:
            out[c.get("state", "pending")] = out.get(c.get("state", "pending"), 0) + 1
        out["total"] = len(self.criteria)
        return out

    def summary(self) -> Dict[str, Any]:
        """`harness_summary.criteria`: counts and ids per state."""
        counts = self.counts()
        ids = {s: [c["id"] for c in self.criteria if c.get("state") == s] for s in STATES}
        return {
            **counts,
            "ids": ids,
            "dropped": self.dropped,
            "key": self.key,
            "open": [{"id": c["id"], "line": c.get("line"), "subject": c.get("subject"),
                      "quote": _clip(c.get("quote", ""), c.get("subject"), 160)}
                     for c in self.open()][:20],
        }

    @staticmethod
    def _label(c: Dict[str, Any], quote_chars: int) -> str:
        where = f"L{c.get('line')}"
        if c.get("source") and c.get("source") != "message":
            where = f"{c['source']}:{where}"
        subj = f" [{c['subject']}]" if c.get("subject") else ""
        return f"{c['id']} {where}{subj}: {_clip(c.get('quote', ''), c.get('subject'), quote_chars)}"

    def compact_list(self, *, max_chars: int = 3000, quote_chars: int = 160, only_open: bool = False) -> str:
        rows = self.open() if only_open else self.criteria
        lines: List[str] = []
        used = 0
        for i, c in enumerate(rows):
            line = self._label(c, quote_chars)
            if used + len(line) + 1 > max_chars and lines:
                lines.append(f"... and {len(rows) - i} more (criteria_check with {{\"list\": true}} shows them all)")
                break
            lines.append(line)
            used += len(line) + 1
        return "\n".join(lines)

    # -- the turn's calls ----------------------------------------------------
    def record_call(self, call_id: str, tool: str, content: str, result: Any, *,
                    kind: str = "", ok: bool = True, paths: Sequence[str] = (), round_num: int = 0) -> None:
        """One tool call of the turn. `kind`/`ok`/`paths` come from the turn
        ledger's own record of it (src/agent_harness.py TurnLedger.record): a
        successful mutation or effect makes every earlier call stale, except a
        write that only touched documentation."""
        if tool == "criteria_check":
            return
        self.seq += 1
        exit_code = result.get("exit_code") if isinstance(result, dict) else None
        self.calls.append({
            "call_id": str(call_id or f"call_{round_num}_{self.seq}"),
            "seq": self.seq,
            "round": round_num,
            "tool": tool,
            "command": _bounded(_command_text(tool, content)),
            "output": _bounded(_result_text(result)),
            "exit_code": exit_code if isinstance(exit_code, int) and not isinstance(exit_code, bool) else None,
            "mutation": bool(ok and kind in ("mutation", "effect")),
        })
        if ok and kind in ("mutation", "effect"):
            docs_only = bool(paths) and all(_DOC_ONLY_RE.search(str(p or "")) for p in paths)
            if not docs_only:
                self.last_mutation_seq = self.seq
        if len(self.calls) > CALLS_MAX:
            del self.calls[: len(self.calls) - CALLS_MAX]

    def find_call(self, *, call_id: str = "", command: str = "", test: str = "") -> Optional[Dict[str, Any]]:
        """A call of this turn by its id, else the LATEST call whose command
        contains `command`, else the latest whose command or output names
        `test`."""
        cid = str(call_id or "").strip()
        if cid:
            for c in reversed(self.calls):
                if c["call_id"] == cid:
                    return c
            return None
        cmd = _norm(command)
        if cmd:
            for c in reversed(self.calls):
                if cmd in _norm(c["command"]):
                    return c
            return None
        t = str(test or "").strip().lower()
        if t:
            leaf = t.split("::")[-1]
            for c in reversed(self.calls):
                hay = (c["command"] + "\n" + c["output"]).lower()
                if t in hay or (leaf and leaf in hay):
                    return c
        return None

    def recent_calls(self, n: int = 8, chars: int = 90) -> List[str]:
        out = []
        for c in self.calls[-n:]:
            cmd = re.sub(r"\s+", " ", c["command"]).strip()
            out.append(f"{c['call_id']} {c['tool']}: {cmd[:chars]}")
        return out

    # -- linking -------------------------------------------------------------
    def apply_links(self, links: Sequence[Any], *, round_num: int = 0) -> List[Dict[str, Any]]:
        """Apply the model's links. The model proposes; the harness decides:
        `proven` needs a call of this turn that passes
        `verification.verify_contract_link`; `unmet` is taken as said (a
        failure the model admits needs no proof); `not_applicable` needs a
        reason. Returns one row per link."""
        from src import verification as _ver
        rows: List[Dict[str, Any]] = []
        for raw in links or []:
            if not isinstance(raw, dict):
                rows.append({"id": None, "state": None, "error": "each link must be an object"})
                continue
            crit = self.get(str(raw.get("id") or ""))
            if crit is None:
                rows.append({"id": raw.get("id"), "state": None, "error": "no such criterion"})
                continue
            status = str(raw.get("status") or "proven").strip().lower()
            if status not in ("proven", "unmet", "not_applicable"):
                rows.append({"id": crit["id"], "state": crit["state"],
                             "error": "status must be proven, unmet or not_applicable"})
                continue
            note = " | ".join(f"{k}: {str(raw.get(k))[:160]}" for k in ("expected", "observed")
                              if str(raw.get(k) or "").strip())
            if status == "not_applicable":
                try:
                    verdict = _ver.mark_not_verifiable(crit, reason=str(raw.get("reason") or ""))
                except ValueError:
                    rows.append({"id": crit["id"], "state": crit["state"],
                                 "error": "not_applicable needs a reason"})
                    continue
            elif status == "unmet":
                verdict = _ver.verify_criterion(crit, evidence_ok=False,
                                                detail=str(raw.get("reason") or raw.get("observed") or "")[:300])
            else:
                call = self.find_call(call_id=str(raw.get("call_id") or ""), command=str(raw.get("command") or ""),
                                      test=str(raw.get("test") or ""))
                verdict = _ver.verify_contract_link(crit, call, last_mutation_seq=self.last_mutation_seq,
                                                    strict=self.strict, test=str(raw.get("test") or ""))
            state = _ver.contract_state(verdict.state)
            # A proof that does not hold leaves an earlier state alone: a
            # weak link never undoes a criterion already proven or admitted.
            if not (verdict.state == "pending" and crit["state"] != "pending"):
                crit["state"] = state
                crit["evidence_refs"] = list(verdict.evidence_refs)
                crit["detail"] = (verdict.detail + (" | " + note if note else ""))[:400]
            row = {"id": crit["id"], "state": crit["state"], "detail": verdict.detail}
            if verdict.state == "pending":
                row["accepted"] = False
            rows.append(row)
        return rows

    # -- what the model is told ----------------------------------------------
    def start_note(self, *, max_chars: int = 3000) -> str:
        """Once, at the start of a long unattended turn: the criteria in
        compact form (id, line, quote), bounded."""
        open_rows = self.open()
        head = (
            "[Contract criteria - automatic runtime message] The task statement was read as "
            f"{len(self.criteria)} checkable requirements"
            + (f" ({len(self.criteria) - len(open_rows)} already settled earlier)" if len(open_rows) < len(self.criteria) else "")
            + ". Before you finish, show each one with a tool call (a command or test whose output shows it, "
            "run after your last change) and link it with `criteria_check` "
            "(`{\"links\": [{\"id\": \"C01\", \"call_id\": \"...\"}]}` or `\"command\": \"<text of the command you ran>\"`); "
            "mark one not_applicable only with a reason.\n"
        )
        return head + self.compact_list(max_chars=max(400, max_chars - len(head)), only_open=True)

    def audit_note(self, *, max_chars: int = 5000) -> str:
        """Replaces the generic completion audit when criteria exist: the open
        criteria with their quotes, one check per subject."""
        rows = self.open()
        lines = [
            "[Harness check - automatic runtime message, not a new user request] Before this run ends, audit the "
            "work against the task statement: nobody will review it after you, and it is judged clause by clause.",
            f"These {len(rows)} requirements of the task statement have no accepted evidence yet "
            "(id, line, [subject], quote):",
            self.compact_list(max_chars=max(800, max_chars - 1600), quote_chars=220, only_open=True),
            "For each one:",
            "1. Write down the observable the quote demands (exact output, code, limit, file).",
            "2. Run a command or test that shows it now, after your last change - one check per subject: a check "
            "of one command or case does not count for another.",
            "3. Fix whatever fails, then check it again.",
            "4. Call `criteria_check` linking each id to the call that showed it (`call_id`, or `command` with the "
            "text of the command you ran), or `status: \"unmet\"` if it still fails, or `status: "
            "\"not_applicable\"` with a reason.",
            "Remove scratch files you created that the task did not ask for, unless they are needed to run the "
            "result. Then give your final answer: one line per requirement with how you checked it.",
        ]
        recent = self.recent_calls()
        if recent:
            lines.append("Recent tool calls of this turn: " + " ; ".join(recent))
        return "\n".join(lines)

    def gap_note(self, *, max_chars: int = 3000) -> str:
        rows = self.open()
        return (
            "[Harness check - automatic runtime message, not a new user request] "
            f"{len(rows)} requirements of the task statement still have no accepted evidence:\n"
            + self.compact_list(max_chars=max(600, max_chars - 600), quote_chars=160, only_open=True)
            + "\nCheck each one with a tool now and link it with `criteria_check` (a link the harness did not "
            "accept says why), or mark it unmet / not_applicable with a reason. This is the last reminder; "
            "then give your final answer."
        )

    # -- persistence -----------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {"version": 1, "key": self.key, "scope": self.scope, "dropped": self.dropped,
                "strict": self.strict, "created_at": self.created_at, "updated_at": time.time(),
                "criteria": self.criteria}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Optional["CriteriaBook"]:
        if not isinstance(data, dict) or not isinstance(data.get("criteria"), list):
            return None
        crit = [c for c in data["criteria"] if isinstance(c, dict) and c.get("id")]
        for c in crit:
            if c.get("state") not in STATES:
                c["state"] = "pending"
            c.setdefault("evidence_refs", [])
            c.setdefault("keys", [])
            c.setdefault("words", [])
        return cls(crit, key=str(data.get("key") or ""), scope=data.get("scope"),
                   dropped=int(data.get("dropped") or 0), strict=bool(data.get("strict", True)),
                   created_at=data.get("created_at"))

    def save(self) -> bool:
        if not self.scope or not self.key:
            return False
        try:
            path = _book_path(self.scope, self.key)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.to_dict(), f, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
            return True
        except Exception:  # noqa: BLE001 - bookkeeping never breaks a turn
            return False


def _book_dir(scope: str) -> str:
    # Beside the plan tracker of the same scope, in a subfolder so
    # plan_tracker.active() (which lists `<scope>/*.json`) never reads a book
    # as a plan.
    from src import plan_tracker as _pt
    return os.path.join(_pt._scope_dir(scope), "criteria")


def _book_path(scope: str, key: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9]+", "", str(key or ""))[:32] or "unknown"
    return os.path.join(_book_dir(scope), safe + ".json")


def load(scope: str, key: str) -> Optional[CriteriaBook]:
    try:
        with open(_book_path(scope, key), "r", encoding="utf-8") as f:
            book = CriteriaBook.from_dict(json.load(f))
    except Exception:  # noqa: BLE001
        return None
    if book is not None:
        book.scope = scope
        book.loaded = True
    return book


def latest(scope: str, *, max_age_s: float = 7 * 24 * 3600) -> Optional[CriteriaBook]:
    """The most recently updated book of the scope (a "continue" turn resumes
    the statement it continues), or None when there is none recent enough."""
    d = _book_dir(scope)
    try:
        names = [n for n in os.listdir(d) if n.endswith(".json")]
    except Exception:  # noqa: BLE001
        return None
    best: Optional[Tuple[float, str]] = None
    for n in names:
        try:
            m = os.path.getmtime(os.path.join(d, n))
        except OSError:
            continue
        if best is None or m > best[0]:
            best = (m, n)
    if best is None or (time.time() - best[0]) > max_age_s:
        return None
    return load(scope, best[1][:-5])
