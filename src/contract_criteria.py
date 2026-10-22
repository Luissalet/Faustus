"""Contract criteria: the normative statements of a long task message, kept as
a book of requirements the turn has to show evidence for.

A long unattended task is judged on what its statement asks for, clause by
clause. The completion audit used to ask the model to list those clauses
itself, and the model grouped them ("put/get/compact stream" became one line
that a measurement of `put` alone "proved"). This module extracts the clauses
deterministically, one criterion per subject, so each one needs its own
evidence:

    source_text(message)                  -> the text to read (typed request + spec-like attachments)
    extract(text, max_items=500)          -> (criteria, dropped)
    CriteriaBook(criteria, ...)           -> per-turn state: the calls of the turn,
                                             links to them, persistence, notes

Every criterion stays in the book (only a high safety cap protects against
pathological input) and counts toward the completion gate. What the model is
SHOWN is bounded instead: the start, audit and gap notes and
`criteria_check {"list": true}` page through the book in priority order,
taking the statement's sections in turn.

Pure and deterministic: regular expressions over the user's own words, no
model call, no settings read. The loop (src/agent_loop.py) builds the book off
the event loop, records every tool call of the turn into it and writes the
audit and gap notes from it; the `criteria_check` tool
(src/agent_tools/contract_tools.py) links criteria to calls; the state of a
criterion is decided by `src.verification.verify_contract_link`, never by the
model.

States are the `contracts.task.ACCEPTANCE_STATES` names this needs:
pending / proven / unmet / not_applicable.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

STATES = ("pending", "proven", "unmet", "not_applicable")
OPEN_STATES = ("pending", "unmet")
MODES = ("off", "unattended", "always")
#: Safety cap per statement (protection against pathological input only; the
#: setting `agent_contract_criteria_max` may lower or raise it up to
#: SAFETY_CAP_MAX). Criteria beyond it are counted as `dropped`.
DEFAULT_MAX = 500
SAFETY_CAP_MAX = 1000
DEFAULT_MIN_CHARS = 1500
#: Fewer criteria than this and the message is not treated as a contract: a
#: long message with one "must" is a request, not a specification.
MIN_CRITERIA = 3
QUOTE_MAX = 400
#: Statement text read per message (typed request plus attachments).
MAX_TEXT_CHARS = 200_000
#: A compound subject list or case list longer than this is one criterion.
MAX_SUBJECTS = 12
#: A long comma-packed step with more marked parts than this is one criterion.
MAX_SPLIT_PARTS = 20
#: Seconds the loop waits for extraction (run in a thread) before it skips
#: the feature for the turn.
TIME_BUDGET_S = 5.0
#: More than this fraction of the criteria marked not_applicable and those
#: claims no longer settle anything: they stay open for the audit and gate.
NA_FRACTION = 0.25
#: ...and more than this many: a small statement may legitimately have a few.
NA_FLOOR = 5
#: The guidance given whenever the rule above applies.
NA_MASS_GUIDANCE = (
    "More than a quarter of the requirements are claimed not applicable, so those claims are kept open: more "
    "not-applicable reasons will not settle them. Settle each one with proof (criteria_check with the call_id or "
    "command of a call whose output shows it) or mark it unmet.")
#: Books older than this are pruned when the scope is listed.
BOOK_MAX_AGE_S = 30 * 24 * 3600
#: Calls kept per turn for linking (oldest dropped first).
CALLS_MAX = 400
_CALL_TEXT_HEAD = 3000
_CALL_TEXT_TAIL = 3000
#: A sentence longer than this whose comma-separated parts carry their own
#: normative marks is split into one criterion per part.
_LONG_SENTENCE = 200
#: Budgets of the injected notes (characters).
START_NOTE_CHARS = 3000
AUDIT_NOTE_CHARS = 5000
GAP_NOTE_CHARS = 3000
LIST_PAGE_CHARS = 12000

# ---------------------------------------------------------------------------
# Markers
# ---------------------------------------------------------------------------

_NORMATIVE_RE = re.compile(
    r"\b(?:must(?:\s+not)?|mustn't|shall(?:\s+not)?|should(?:\s+not)?|shouldn't|never|only|exactly|"
    r"at\s+most|at\s+least|within|required|always|no\s+more\s+than|no\s+fewer\s+than|no\s+later\s+than|"
    r"has\s+to|have\s+to|needs?\s+to|"
    r"no\s+debe(?:n|r[áa]n?)?|debe(?:n|r[áa]n?)?|deber[íi]an?|tienen?\s+que|hay\s+que|s[óo]lo|solamente|"
    r"[úu]nicamente|nunca|jam[áa]s|exactamente|como\s+m[áa]ximo|como\s+m[íi]nimo|al\s+menos|siempre|"
    r"obligatori[oa]s?|requerid[oa]s?)\b",
    re.I,
)
#: The modals that make a rule strong: negative or universal.
_STRONG_RE = re.compile(
    r"\b(?:never|must\s+not|mustn't|shall\s+not|should\s+not|only|exactly|always|nunca|jam[áa]s|"
    r"no\s+debe(?:n)?|s[óo]lo|[úu]nicamente|exactamente|siempre)\b",
    re.I,
)
_MODAL_RE = re.compile(r"\b(?:must|shall|required|debe(?:n)?|tienen?\s+que|has\s+to|have\s+to)\b", re.I)
_WEAK_ONLY_RE = re.compile(r"\b(?:within|only|s[óo]lo)\b", re.I)
_UNITS = (
    r"(?:[kmgt]i?b|bytes?|ms|µs|us|ns|s|secs?|seconds?|mins?|minutes?|h|hrs?|hours?|days?|%|"
    r"chars?|characters?|lines?|rows?|items?|entries|records?|requests?|tokens?|px|pixels?|"
    r"segundos?|minutos?|horas?|d[íi]as?|caracteres|l[íi]neas|filas|elementos|registros|peticiones)"
)
_LIMIT_RE = re.compile(r"(?<![\w.])(\d+(?:[.,]\d+)?)\s?(" + _UNITS + r")(?![\w])", re.I)
_COMPARE_RE = re.compile(
    r"(?:\b(?:under|over|below|above|less\s+than|more\s+than|fewer\s+than|up\s+to|max(?:imum)?|min(?:imum)?|"
    r"within|at\s+most|at\s+least|exceed\w*|no\s+more|no\s+less|menos\s+de|m[áa]s\s+de|hasta|"
    r"m[áa]ximo|m[íi]nimo|como\s+m[áa]ximo|al\s+menos|no\s+super\w*|por\s+debajo|por\s+encima)\b|[<>≤≥])",
    re.I,
)
_TICK_RE = re.compile(r"`([^`\n]{1,80})`")
#: A process exit status (0-255): "exits with status 2", "exit code 1",
#: "returns exit code 3", "sale con código 1".
_EXIT_RE = re.compile(
    r"\b(?:exits?|exit\s+(?:code|status)|returns?\s+exit\s+(?:code|status)|(?:sale|termina)\s+con)\s+"
    r"(?:with\s+)?(?:the\s+|el\s+)?(?:exit\s+)?(?:status|code|c[óo]digo|estado)?\s*(?:of\s+|de\s+)?(\d{1,3})\b",
    re.I,
)
#: An HTTP status written in prose: "returns status 404", "HTTP 404",
#: "status code 422", "answers status 404", "código HTTP 404".
_HTTP_RE = re.compile(
    r"(?<!exit )\b(?:returns?\s+(?:status|code)|answers?\s+(?:with\s+)?status|status(?:\s+code)?|HTTP(?:\s+status)?|"
    r"responde\s+con(?:\s+(?:el\s+)?(?:estado|c[óo]digo))?|c[óo]digo\s+HTTP)\s*(?:of\s+|de\s+)?([1-5]\d\d)\b",
    re.I,
)
#: An explicit ordering or precedence rule: shown early, never demoted.
_ORDER_RE = re.compile(
    r"\b(?:in\s+(?:this|that|the\s+following|the\s+given|file|document)\s+order|in\s+order|first\b[^.;]{0,120}\bthen\b|"
    r"order\s+of\s+(?:checks|precedence|evaluation)|precedence|takes\s+priority|wins\b|the\s+first\s+(?:failing|failure|"
    r"one|match)|en\s+(?:este|ese|el\s+siguiente)\s+orden|primero\b[^.;]{0,120}\b(?:luego|despu[ée]s)\b|prioridad|gana\b)",
    re.I,
)
_ERROR_CODE_RE = re.compile(
    r"(?:\b(?:code|error|status|fails?\s+with|exits?(?:\s+with)?|returns?|rejects?(?:\s+with)?|"
    r"c[óo]digo|falla\s+con|devuelve|sale\s+con)\s*[:=]?\s*(?:code\s+|c[óo]digo\s+)?`[^`\n]{1,40}`"
    r"|\(`[A-Za-z_][\w.-]{0,40}`\))",
    re.I,
)
_FORMAT_TICK_RE = re.compile(r"YYYY|yyyy|HH:MM|hh:mm|MM-DD|DD/MM|\.\.\.|\[0-9\]|\\d|%[YmdHMSf]|^\^|\$$")
#: An imperative requirement: a sentence that starts with an instruction verb.
_IMPERATIVE_RE = re.compile(
    r"^(?:Create|Implement|Write|Build|Add|Use|Provide|Return|Print|Store|Support|Make|Include|Keep|"
    r"Ensure|Reject|Accept|Expose|Generate|Produce|Save|Output|Emit|Log|Validate|Handle|Document|Ship|"
    r"Place|Name|Install|Package|Serve|Refuse|Report|Sort|Format|Encode|"
    r"Crea|Implementa|Escribe|Construye|A[ñn]ade|Agrega|Usa|Utiliza|Devuelve|Imprime|Guarda|Genera|"
    r"Incluye|Asegura|Aseg[úu]rate|Rechaza|Acepta|Valida|Documenta|Mant[ée]n|Proporciona|Ordena|Sirve)\b"
)
#: ...counted only with something concrete in it (a literal, a file name, a
#: number) or in a section that states what to deliver.
_CONCRETE_RE = re.compile(r"`|\b[\w./-]+\.[A-Za-z][A-Za-z0-9]{0,5}\b|\d")
_SPEC_SECTION_RE = re.compile(
    r"deliverable|requirement|spec|task|output|interface|command|\bcli\b|\bapi\b|what to build|"
    r"entregable|requisito|especificaci|tarea|salida|interfaz|comando",
    re.I,
)

_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^\s{0,3}(```|~~~)")
_ITEM_RE = re.compile(r"^(\s*)(?:[-*+•]|(\d{1,3})[.)])\s+")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{3,}")
_ATTACH_RE = re.compile(r"(?:^|\n)=== (File|ZIP archive): (.+?) ===\n?")
#: Attachments read as statements: text documents whose name looks like one.
_SPEC_EXT_RE = re.compile(r"\.(?:md|markdown|txt|rst)$", re.I)
_SPEC_NAME_RE = re.compile(
    r"task|spec|readme|require|enunciado|encargo|contrat|contract|brief|assignment|instruc|plan|tarea|requisit",
    re.I,
)

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

def _is_spec_attachment(name: str) -> bool:
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    return bool(_SPEC_EXT_RE.search(base)) and bool(_SPEC_NAME_RE.search(base))


def source_text(message: str, *, max_chars: int = MAX_TEXT_CHARS) -> List[Tuple[str, str]]:
    """The parts of a user message to read: the typed request and every
    inlined attachment that looks like a statement (`.md`/`.markdown`/`.rst`/
    `.txt` named TASK, SPEC, README, requirements, enunciado...), as
    `(source, text)`. Code, logs, licences and ZIP archives are skipped. At
    most `max_chars` characters in all are read."""
    body = message or ""
    marks = list(_ATTACH_RE.finditer(body))
    parts: List[Tuple[str, str]] = []
    if not marks:
        if body.strip():
            parts.append(("message", body))
    else:
        head = body[:marks[0].start()]
        if head.strip():
            parts.append(("message", head))
        for i, m in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(body)
            name = m.group(2).strip()
            if m.group(1) == "File" and _is_spec_attachment(name):
                text = body[m.end():end]
                if text.strip():
                    parts.append((name, text))
    out: List[Tuple[str, str]] = []
    left = max(0, int(max_chars))
    for source, text in parts:
        if left <= 0:
            break
        out.append((source, text[:left]))
        left -= len(text)
    return out


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
            if set(line.strip()) <= set("|:- "):
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
                if ch == ";":
                    cut = True
                elif ch == ":":
                    cut = bool(nxt) and (nxt.isupper() or nxt == "`")
                elif ch == "." and re.search(r"(?:\be\.g|\bi\.e|\betc|\bvs|\bcf)$", text[max(0, i - 4):i], re.I):
                    cut = False                           # an abbreviation, not the end of a sentence
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
# Classifying one sentence
# ---------------------------------------------------------------------------

def _has_limit(text: str) -> bool:
    return bool(_LIMIT_RE.search(text)) and bool(_COMPARE_RE.search(text))


def _has_format(text: str) -> bool:
    return any(_FORMAT_TICK_RE.search(t) for t in _TICK_RE.findall(text))


def _imperative(text: str, section: str = "") -> bool:
    s = text.strip().lstrip("*_ ")
    if not _IMPERATIVE_RE.match(s):
        return False
    return bool(_CONCRETE_RE.search(s)) or bool(section and _SPEC_SECTION_RE.search(section))


def is_normative(text: str, section: str = "") -> bool:
    """A requirement, not a description: a modal or quantifier (English or
    Spanish, including "should"), a numeric limit with a unit and a
    comparison, an error code in backticks, an exit status, a literal format,
    or an imperative instruction with something concrete in it (`Create
    `out.csv`...`) or in a deliverable/specification section. Questions never
    are."""
    s = (text or "").strip()
    if len(s) < 12 or s.endswith("?") or s.startswith("¿"):
        return False
    return bool(_NORMATIVE_RE.search(s) or _has_limit(s) or _ERROR_CODE_RE.search(s) or _EXIT_RE.search(s)
                or _HTTP_RE.search(s) or _ORDER_RE.search(s) or _has_format(s) or _imperative(s, section))


def _kind(text: str) -> str:
    if _has_limit(text) or re.search(r"\b(?:at\s+most|at\s+least|como\s+m[áa]ximo|al\s+menos)\b", text, re.I):
        return "limit"
    if _ERROR_CODE_RE.search(text) or _EXIT_RE.search(text) or _HTTP_RE.search(text):
        return "error_code"
    if _has_format(text):
        return "format"
    return "behaviour"


def _priority(text: str, kind: str, subject: Optional[str]) -> int:
    """Exposure priority (higher first): negative or universal rules and
    explicit ordering/precedence rules, then limits, codes and formats, then
    anything with a literal; a descriptive `within`/`only` with nothing
    concrete ranks low - unless it states an order or uses a strong modal."""
    score = 0
    strong = bool(_STRONG_RE.search(text))
    ordering = bool(_ORDER_RE.search(text))
    if strong:
        score += 3
    if ordering:
        score += 3
    if kind != "behaviour":
        score += 2
    if "`" in text:
        score += 1
    if _MODAL_RE.search(text):
        score += 1
    if (kind == "behaviour" and not subject and not ordering and not strong and "`" not in text
            and not _MODAL_RE.search(text) and _WEAK_ONLY_RE.search(text)):
        score -= 2
    return score


def _marked(text: str) -> bool:
    """Carries its own mark: normative word, limit, backtick literal or code."""
    return bool(_NORMATIVE_RE.search(text) or _LIMIT_RE.search(text) or _TICK_RE.search(text)
                or _ERROR_CODE_RE.search(text))


def _clip(text: str, focus: Optional[str] = None, limit: int = QUOTE_MAX) -> str:
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(s) <= limit:
        return s
    if focus:
        at = s.lower().find(str(focus).lower())
        if at > limit - 60:
            lo = max(0, at - limit // 2)
            return "…" + s[lo:lo + limit - 2].strip() + "…"
    return s[:max(1, limit - 1)].rstrip() + "…"


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


_JUNK_DOTTED_RE = re.compile(r"^(?:e\.g|i\.e|etc|vs|cf|a\.k\.a|n\.b)$", re.I)


def _keys(text: str) -> List[str]:
    """Distinctive tokens a check of this criterion is expected to show:
    backtick literals, quoted strings, dotted/snake names. Numbers are NOT
    keys on their own (see `_limits`); format and command literals become
    matchers instead (see `_literal_matcher`)."""
    out: List[str] = []

    def add(tok: str) -> None:
        tok = (tok or "").strip()
        # A key with no letter or digit (`&`, `|`, `...`) would match almost
        # any output.
        if tok and tok not in out and len(out) < 24 and re.search(r"[A-Za-z0-9]", tok):
            out.append(tok)

    for t in _TICK_RE.findall(text):
        add(t)
    for t in re.findall(r'"([A-Za-z_][\w .:/-]{1,39})"', text):
        add(t)
    for t in re.findall(r"\b[A-Za-z][A-Za-z0-9]*(?:[_.-][A-Za-z0-9]+)+\b", _TICK_RE.sub(" ", text)):
        parts = re.split(r"[_.-]", t)
        if _JUNK_DOTTED_RE.match(t) or all(len(p) <= 1 for p in parts):
            continue
        add(t)
    return out


def _limits(text: str) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for m in _LIMIT_RE.finditer(text):
        row = {"num": m.group(1), "unit": m.group(2)}
        if row not in out and len(out) < 8:
            out.append(row)
    return out


def _exit_codes(text: str) -> List[int]:
    return sorted({int(m.group(1)) for m in _EXIT_RE.finditer(text) if int(m.group(1)) <= 255})[:4]


def _http_statuses(text: str) -> List[int]:
    return sorted({int(m.group(1)) for m in _HTTP_RE.finditer(text)})[:4]


# -- literals that describe a form rather than a value ----------------------

_HTTP_METHOD_RE = re.compile(r"^(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+(/\S*)")
_PYTHON_CMD_RE = re.compile(r"^(?:<python>|python[0-9.]*|py)\s+(?:-m\s+\S+|\S+\.py)", re.I)
_PLACEHOLDER_TOKEN_RE = re.compile(r"^(?:<[^<>]+>|\[.*\]|\.\.\.|…|[A-Z][A-Z0-9_]*(?:\.\.\.)?)$")
_FORMAT_HINT_RE = re.compile(r"YYYY|HH:MM|hh:mm|MM-DD|DD/MM|\.\.\.Z|…Z|<[^<>]+>")
_REGEX_HINT_RE = re.compile(r"\[[^\]]*(?:0-9|a-z|A-Z|\\d)[^\]]*\]|\\[dws]")
_UNSAFE_REGEX_RE = re.compile(r"\([^()]*[+*][^()]*\)[+*{]|\\[1-9]")
_EXAMPLE_INSTANT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})?)?$")


def _command_tokens(lit: str) -> Optional[List[str]]:
    """The fixed tokens of a command template (`python -m ledger <command>
    ...`, `ingest DB FILE...`, `report --format csv`, `--tz`): placeholders
    and optional parts dropped. None when `lit` is not a command template."""
    s = lit.strip()
    toks = s.split()
    if not toks:
        return None
    is_cmd = bool(_PYTHON_CMD_RE.match(s))
    if not is_cmd and re.match(r"^--?[a-z][\w-]*$", toks[0]) and len(toks) <= 3:
        is_cmd = True                                    # an option: `--tz`, `--from`
    if (not is_cmd and re.match(r"^[a-z][a-z0-9_-]*$", toks[0]) and len(toks) >= 2
            and any(_PLACEHOLDER_TOKEN_RE.match(t) or t.startswith("--") for t in toks[1:])):
        is_cmd = True
    if not is_cmd:
        return None
    s = re.sub(r"\[[^\]]*\]", " ", s)                   # optional parts
    fixed = []
    for t in s.split():
        if _PLACEHOLDER_TOKEN_RE.match(t) or t.startswith("<"):
            continue
        t = t.rstrip(".…")
        if t and t not in fixed:
            fixed.append(t)
    return fixed or None


def _format_regex(lit: str) -> Optional[str]:
    """A regex for the form a literal describes, to be matched against output:
    `YYYY-MM-DD` -> `\\d{4}-\\d{2}-\\d{2}`, `...Z` -> an instant ending in Z,
    `<series_id>:<YYYY-MM-DD>` -> `\\S+?:\\d{4}-\\d{2}-\\d{2}`, an example
    instant with its digits generalised, or a literal that is itself a safe
    regex (`-?[0-9]+`). None for anything else."""
    s = lit.strip()
    if not s or len(s) > 120:
        return None
    if _REGEX_HINT_RE.search(s) and not _FORMAT_HINT_RE.search(s):
        if _UNSAFE_REGEX_RE.search(s):
            return None
        try:
            re.compile(s)
        except re.error:
            return None
        return r"(?<![\w.])(?:" + s + r")(?![\w])"
    if _EXAMPLE_INSTANT_RE.match(s):
        return r"(?<!\d)" + re.sub(r"\d", r"\\d", re.escape(s)) + r"(?!\d)"
    if not _FORMAT_HINT_RE.search(s):
        return None
    out: List[str] = []
    fixed = 0
    i, n = 0, len(s)
    while i < n:
        if s.startswith("...Z", i) or s.startswith("…Z", i):
            out.append(r"\d{2}Z")
            fixed += 1
            i += 4 if s[i] == "." else 2
        elif s[i] == "<" and s.find(">", i) > i:
            j = s.find(">", i)
            inner = _format_regex(s[i + 1:j]) if _FORMAT_HINT_RE.search(s[i + 1:j]) else None
            if inner:
                out.append(inner.replace(r"(?<!\d)", "").replace(r"(?!\d)", ""))
                fixed += 1
            else:
                out.append(r"\S+?")
            i = j + 1
        elif s.startswith("YYYY", i):
            out.append(r"\d{4}")
            fixed += 1
            i += 4
        elif s[i:i + 2] in ("MM", "DD", "HH", "SS", "hh", "mm", "ss"):
            out.append(r"\d{2}")
            fixed += 1
            i += 2
        elif s.startswith("...", i) or s[i] == "…":
            out.append(r".*?")
            i += 3 if s[i] == "." else 1
        elif s[i].isspace():
            out.append(r"\s+")
            while i < n and s[i].isspace():
                i += 1
        else:
            out.append(re.escape(s[i]))
            if s[i].isalnum():
                fixed += 1
            i += 1
    if fixed == 0:
        return None
    pat = "".join(out)
    if pat.startswith(r"\d"):
        pat = r"(?<!\d)" + pat
    if pat.endswith("}") or pat.endswith("Z"):
        pat = pat + r"(?!\d)"
    return pat


def _json_keys(lit: str) -> Optional[List[str]]:
    s = lit.strip()
    if not s.startswith("{"):
        return None
    keys = []
    for k in re.findall(r"[\"']?([A-Za-z_][\w-]*)[\"']?\s*:", s):
        if k not in keys:
            keys.append(k)
    return keys[:8] or None


def _literal_matcher(lit: str) -> Optional[Dict[str, Any]]:
    """How a literal is shown when it describes a FORM, not a value:
    a route or command template by the command that ran it (the command
    demonstrates the invocation), a JSON shape by its keys in the output, a
    date/instant/regex format by a regex over the output."""
    s = (lit or "").strip()
    if not s:
        return None
    m = _HTTP_METHOD_RE.match(s)
    if m:
        path = re.split(r"[?<{]|\.\.\.|…", m.group(1))[0].rstrip("/") or "/"
        return {"on": "command", "tokens": [path], "src": s}
    toks = _command_tokens(s)
    if toks:
        return {"on": "command", "tokens": toks, "src": s}
    keys = _json_keys(s)
    if keys:
        return {"on": "output", "json": keys, "src": s}
    pat = _format_regex(s)
    if pat:
        return {"on": "output", "re": pat, "src": s}
    return None


def _matchers(text: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for t in _TICK_RE.findall(text):
        m = _literal_matcher(t)
        if m and all(x["src"] != m["src"] for x in out) and len(out) < 8:
            out.append(m)
    return out


def matcher_shown(matcher: Dict[str, Any], *, output: str, command: str) -> bool:
    """Does a call show a form matcher (see `_literal_matcher`)?"""
    try:
        if matcher.get("on") == "command":
            cmd = command or ""
            # A route path follows a host (`http://127.0.0.1:8000/api/x`); other
            # tokens stand on their own.
            return all(re.search(("" if str(t).startswith("/") else r"(?<![\w-])") + re.escape(str(t))
                                 + r"(?![\w-])", cmd, re.I)
                       for t in matcher.get("tokens") or [])
        if matcher.get("json"):
            return all(re.search(r"[\"']?" + re.escape(str(k)) + r"[\"']?\s*:", output or "")
                       for k in matcher["json"])
        if matcher.get("re"):
            return re.search(str(matcher["re"]), (output or "")[:20000]) is not None
    except re.error:
        return False
    return False


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
    seen = set()
    for c in cases:
        if c.lower() not in seen:
            seen.add(c.lower())
            out.append(c)
    return out if len(out) >= 2 else []


def _norm(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "").lower()).strip()


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

class _Basis:
    """What one sentence says, computed once and shared by every criterion
    expanded from it (the subjects of a compound list, the cases of a list)."""

    __slots__ = ("text", "kind", "keys", "words", "limits", "exits", "statuses", "matchers")

    def __init__(self, text: str) -> None:
        self.text = text
        self.kind = _kind(text)
        self.matchers = _matchers(text)
        forms = {m["src"] for m in self.matchers}
        # A literal that describes a form is shown through its matcher, never
        # verbatim: `YYYY-MM-DD` does not occur in a correct program's output.
        self.keys = [k for k in _keys(text) if k not in forms]
        self.words = _words(text)
        self.limits = _limits(text)
        self.exits = _exit_codes(text)
        self.statuses = _http_statuses(text)


def _criterion(quote: str, basis: _Basis, *, line: int, section: str, source: str,
               subject: Optional[str] = None, siblings: Sequence[str] = ()) -> Dict[str, Any]:
    skip = {s for s in siblings if s and s != subject}
    subject_matcher = _literal_matcher(subject) if subject else None
    keys = ([subject] if subject and not subject_matcher else []) + [
        k for k in basis.keys if k not in skip and k != subject]
    matchers = [m for m in basis.matchers if m["src"] not in skip and m["src"] != subject]
    return {
        "id": "",
        "quote": _clip(quote, subject),
        "line": line,
        "section": section,
        "source": source,
        "subject": subject or None,
        "kind": basis.kind,
        "priority": _priority(basis.text, basis.kind, subject),
        "keys": keys[:12],
        "words": list(basis.words),
        "limits": list(basis.limits),
        "exits": list(basis.exits),
        "statuses": list(basis.statuses),
        "matchers": matchers,
        "subject_matcher": subject_matcher,
        "state": "pending",
        "evidence_refs": [],
        "detail": "",
    }


def _from_sentence(sentence: str, *, line: int, section: str, source: str) -> List[Dict[str, Any]]:
    """One normative sentence -> one or more criteria."""
    subjects = _compound_subjects(sentence)
    if subjects:
        basis = _Basis(sentence)
        if len(subjects) > MAX_SUBJECTS:
            return [_criterion(sentence, basis, line=line, section=section, source=source)]
        return [_criterion(sentence, basis, line=line, section=section, source=source, subject=s,
                           siblings=subjects) for s in subjects]
    # A long step that packs several rules into one comma list (`Shape:
    # exactly those five keys, `version` is the integer 1, instants in the
    # canonical `...Z` form, ...`) becomes one criterion per marked part.
    if len(sentence) > _LONG_SENTENCE and sentence.count(",") >= 3:
        lead, rest = "", sentence
        colon = sentence.find(": ")
        if 0 < colon < 60:
            lead, rest = sentence[:colon].strip(), sentence[colon + 2:]
        parts = [re.sub(r"^(?:and|y)\s+", "", p, flags=re.I) for p in _split_top(rest)]
        marked = [p for p in parts if len(p) >= 8 and _marked(p)]
        if 2 <= len(marked) <= MAX_SPLIT_PARTS:
            out: List[Dict[str, Any]] = []
            for p in marked:
                text = (lead + ": " + p) if lead else p
                if len(text) <= _LONG_SENTENCE:
                    out.extend(_from_sentence(text, line=line, section=section, source=source))
                else:
                    out.append(_criterion(text, _Basis(text), line=line, section=section, source=source))
            return out
    basis = _Basis(sentence)
    cases = _enum_cases(sentence)
    if cases and len(cases) <= MAX_SUBJECTS:
        return [_criterion(sentence, basis, line=line, section=section, source=source, subject=c)
                for c in cases]
    return [_criterion(sentence, basis, line=line, section=section, source=source)]


def extract(text: str, *, max_items: int = DEFAULT_MAX, source: str = "message") -> Tuple[List[Dict[str, Any]], int]:
    """The normative statements of `text` as criteria, in document order, with
    ids `C01`... Returns `(criteria, dropped)`: `dropped` counts the criteria
    beyond the safety cap `max_items` (limits, error codes and formats are
    kept first when it bites)."""
    return extract_parts([(source, text)], max_items=max_items)


def extract_parts(parts: Iterable[Tuple[str, str]], *, max_items: int = DEFAULT_MAX) -> Tuple[List[Dict[str, Any]], int]:
    found: List[Dict[str, Any]] = []
    seen = set()
    hard_stop = max(0, int(max_items)) * 4 + 100
    for source, text in parts:
        for block in _blocks(text[:MAX_TEXT_CHARS]):
            for offset, sentence in _sentences(block["text"]):
                if not is_normative(sentence, block["section"]):
                    continue
                line = _line_at(block["segs"], offset)
                for c in _from_sentence(sentence, line=line, section=block["section"], source=source):
                    key = (_norm(c["quote"]), _norm(c["subject"] or ""))
                    if key in seen:
                        continue
                    seen.add(key)
                    found.append(c)
                if len(found) >= hard_stop:
                    break
    max_items = max(0, int(max_items))
    dropped = 0
    if len(found) > max_items:
        order = sorted(range(len(found)), key=lambda i: (found[i]["kind"] == "behaviour", -found[i]["priority"], i))
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


def exposure_order(rows: Sequence[Dict[str, Any]], positions: Optional[Dict[str, int]] = None) -> List[Dict[str, Any]]:
    """Priority order for showing criteria, sections taken in turn: every
    section's first criterion comes before any section's second one; within a
    section, higher priority first, then document order."""
    pos = positions or {}
    groups: Dict[str, List[Dict[str, Any]]] = {}
    order: List[str] = []
    for i, c in enumerate(rows):
        sec = str(c.get("section") or "")
        if sec not in groups:
            groups[sec] = []
            order.append(sec)
        groups[sec].append(c)
    for sec in order:
        groups[sec].sort(key=lambda c: (-int(c.get("priority") or 0), pos.get(c.get("id"), 0)))
    out: List[Dict[str, Any]] = []
    depth = 0
    while len(out) < len(rows):
        for sec in order:
            if depth < len(groups[sec]):
                out.append(groups[sec][depth])
        depth += 1
    return out


# ---------------------------------------------------------------------------
# The turn's book
# ---------------------------------------------------------------------------

_DOC_ONLY_RE = re.compile(r"\.(?:md|markdown|txt|rst)$", re.I)
#: A command that only prints what the model wrote is not evidence of anything.
_ECHO_RE = re.compile(r"^\s*(?:(?:@?echo|printf|write-output|write-host|echo\.)(?:\s|$|\()|cat\s*<<)", re.I)


def is_echo_only(tool: str, command: str) -> bool:
    """True when every step of the command just prints literal text (`echo`,
    `printf`, `Write-Output`, `Write-Host`, a here-doc `cat`), or a python
    snippet is nothing but print() calls."""
    cmd = str(command or "").strip()
    if not cmd:
        return False
    if tool == "python":
        return _print_only_code(cmd)
    # A here-doc body is data, not steps.
    cmd = re.sub(r"(<<-?\s*['\"]?(\w+)['\"]?)[^\n]*\n.*?^\s*\2\s*$", r"\1", cmd, flags=re.S | re.M)
    # Separators outside quotes only: `python -c "print('a'); print('b')"` is one step.
    steps = [s for s in _split_steps(cmd) if s.strip()]
    if not steps:
        return False
    # The first step prints literal text; every later step only carries or
    # stores it (`| tee out.txt`, `| Out-File`, `> log`, `| cat`).
    return _echo_step(steps[0]) and all(_echo_step(s) or _CARRIER_RE.match(s) for s in steps[1:])


#: Steps that only pass on or store what an earlier step printed.
_CARRIER_RE = re.compile(
    r"^\s*(?:tee-object|tee|cat|more|less|head|tail|sort|uniq|tr|out-file|set-content|add-content|out-string|out-host|"
    r"out-null|format-table|format-list|select-object|write-output|write-host)\b(?![\w.-]*\s*\()",
    re.I,
)
_INLINE_CODE_RE = re.compile(
    r"^\s*(?:python[0-9.]*(?:\.exe)?|py)\s+-c\s+(['\"])(.*)\1\s*$|^\s*node(?:\.exe)?\s+-e\s+(['\"])(.*)\3\s*$",
    re.S | re.I,
)


def _split_steps(cmd: str) -> List[str]:
    parts: List[str] = []
    buf: List[str] = []
    quote = ""
    i = 0
    while i < len(cmd):
        ch = cmd[i]
        if quote:
            if ch == quote:
                quote = ""
            buf.append(ch)
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if cmd.startswith("&&", i) or cmd.startswith("||", i):
            parts.append("".join(buf))
            buf = []
            i += 2
            continue
        if ch in ";|\n":
            parts.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return parts


_LITERAL_PRINT_RE = re.compile(
    r"""^(?:print|console\.log)\s*\(\s*(?:(?:[rbuf]?'[^'{}\n]*'|[rbuf]?"[^"{}\n]*"|-?\d+(?:\.\d+)?)\s*,?\s*)*\)\s*$""",
    re.I,
)


def _print_only_code(code: str) -> bool:
    """A code snippet made only of print()/console.log() calls of literal
    strings or numbers (a print of a computed value is a real check)."""
    stmts = [s.strip() for s in re.split(r"[;\n]", code) if s.strip() and not s.strip().startswith(("#", "//"))]
    return bool(stmts) and all(_LITERAL_PRINT_RE.match(s) for s in stmts)


def _echo_step(step: str) -> bool:
    s = step.strip()
    if _ECHO_RE.match(s):
        return True
    m = _INLINE_CODE_RE.match(s)
    if m:
        return _print_only_code(m.group(2) if m.group(2) is not None else (m.group(4) or ""))
    return False


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


def _coerce(c: Dict[str, Any]) -> Dict[str, Any]:
    """A stored criterion with the types the book relies on."""
    def _list(v: Any) -> List[Any]:
        return list(v) if isinstance(v, (list, tuple)) else []
    try:
        line = int(c.get("line") or 0)
    except (TypeError, ValueError):
        line = 0
    try:
        prio = int(c.get("priority") or 0)
    except (TypeError, ValueError):
        prio = 0
    state = c.get("state") if c.get("state") in STATES else "pending"
    subject = c.get("subject")
    return {
        **c,
        "id": str(c.get("id") or "").strip().upper(),
        "quote": str(c.get("quote") or ""),
        "line": line,
        "section": str(c.get("section") or ""),
        "source": str(c.get("source") or "message"),
        "subject": str(subject) if subject not in (None, "") else None,
        "kind": str(c.get("kind") or "behaviour"),
        "priority": prio,
        "keys": [str(k) for k in _list(c.get("keys"))],
        "words": [str(w) for w in _list(c.get("words"))],
        "limits": [d for d in _list(c.get("limits")) if isinstance(d, dict)],
        "exits": [int(x) for x in _list(c.get("exits")) if isinstance(x, int) and not isinstance(x, bool)],
        "statuses": [int(x) for x in _list(c.get("statuses")) if isinstance(x, int) and not isinstance(x, bool)],
        "matchers": [m for m in _list(c.get("matchers")) if isinstance(m, dict) and m.get("on") in ("output", "command")],
        "subject_matcher": (c.get("subject_matcher") if isinstance(c.get("subject_matcher"), dict)
                            and c["subject_matcher"].get("on") in ("output", "command") else None),
        "state": state,
        "evidence_refs": [str(r) for r in _list(c.get("evidence_refs"))],
        "detail": str(c.get("detail") or ""),
    }


class CriteriaBook:
    """The criteria of one task statement and the evidence linked to them.

    Built once per turn; `record_call` is fed every tool call the turn runs
    (command and bounded output), so a link can be checked against what the
    call really showed. Persisted beside the plan tracker of the same scope
    (`data/plan_tracker/<scope>/criteria/<key>.json`). Across turns only the
    model's own statements carry over (`unmet`, `not_applicable`); `proven`
    has to be earned again in the new turn."""

    def __init__(self, criteria: List[Dict[str, Any]], *, key: str = "", scope: Optional[str] = None,
                 dropped: int = 0, strict: bool = True, created_at: Optional[float] = None,
                 session_id: Optional[str] = None) -> None:
        self.criteria = criteria
        self.key = key
        self.scope = scope
        self.session_id = session_id
        self.dropped = int(dropped or 0)
        self.strict = bool(strict)
        self.created_at = created_at or time.time()
        self.persist = True
        self.calls: List[Dict[str, Any]] = []
        self.seq = 0
        self.last_mutation_seq = -1
        self.loaded = False
        #: Whether the model was shown the criteria this turn (start note or
        #: audit). The completion gate only counts criteria it was shown.
        self.shown = False
        self.shown_audit: set = set()
        self._pos = {c["id"]: i for i, c in enumerate(criteria)}

    # -- building ------------------------------------------------------------
    @classmethod
    def from_message(cls, message: str, *, max_items: int = DEFAULT_MAX, min_chars: int = DEFAULT_MIN_CHARS,
                     scope: Optional[str] = None, strict: bool = True, session_id: Optional[str] = None,
                     min_criteria: int = MIN_CRITERIA) -> Optional["CriteriaBook"]:
        """None when the message is too short or states too few requirements.
        A book of the same statement saved earlier in the scope lends it the
        states that carry over (see `merge_states`)."""
        parts = source_text(message)
        if sum(len(t) for _, t in parts) < max(0, int(min_chars)):
            return None
        criteria, dropped = extract_parts(parts, max_items=min(SAFETY_CAP_MAX, max(1, int(max_items))))
        if len(criteria) < max(1, int(min_criteria)):
            return None
        book = cls(criteria, key=text_key(parts), scope=scope, dropped=dropped, strict=strict,
                   session_id=session_id)
        if scope:
            saved = load(scope, book.key)
            if saved is not None:
                book.merge_states(saved)
                book.loaded = True
        return book

    def merge_states(self, other: "CriteriaBook") -> None:
        """Carry over what the model stated in an earlier turn (`unmet`,
        `not_applicable` with its reason). A `proven` state is not carried:
        its evidence was a call of another turn, possibly on other code."""
        by_key = {(_norm(c.get("quote")), _norm(c.get("subject") or "")): c for c in other.criteria}
        for c in self.criteria:
            old = by_key.get((_norm(c.get("quote")), _norm(c.get("subject") or "")))
            if not old:
                continue
            if old.get("state") in ("unmet", "not_applicable"):
                c["state"] = old["state"]
                c["evidence_refs"] = list(old.get("evidence_refs") or [])
                c["detail"] = str(old.get("detail") or "")
                c["updated_at"] = old.get("updated_at") or 0
            elif old.get("state") == "proven":
                c["detail"] = "proven in an earlier turn - check again"

    def start_new_turn(self) -> None:
        """A book resumed for a new turn: `proven` goes back to `pending`."""
        for c in self.criteria:
            if c.get("state") == "proven":
                c["state"] = "pending"
                c["evidence_refs"] = []
                c["detail"] = "proven in an earlier turn - check again"
        self.calls = []
        self.seq = 0
        self.last_mutation_seq = -1
        self.shown = False
        self.shown_audit = set()

    # -- reading -------------------------------------------------------------
    def get(self, cid: str) -> Optional[Dict[str, Any]]:
        cid = str(cid or "").strip().upper()
        for c in self.criteria:
            if c["id"] == cid:
                return c
        return None

    def na_mass(self) -> bool:
        """More than NA_FRACTION of the criteria (and more than NA_FLOOR of
        them) claimed not applicable: those claims no longer settle anything;
        only proof or `unmet` moves them."""
        total = len(self.criteria)
        na = sum(1 for c in self.criteria if c.get("state") == "not_applicable")
        return total > 0 and na > NA_FRACTION * total and na > NA_FLOOR

    def open(self) -> List[Dict[str, Any]]:
        """Criteria still needing evidence: pending, unmet, and - when the
        model claimed too many - the ones claimed not applicable."""
        states = OPEN_STATES + (("not_applicable",) if self.na_mass() else ())
        return [c for c in self.criteria if c.get("state") in states]

    def ordered(self, rows: Optional[Sequence[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
        return exposure_order(self.criteria if rows is None else rows, self._pos)

    def counts(self) -> Dict[str, int]:
        out = {s: 0 for s in STATES}
        for c in self.criteria:
            out[c.get("state", "pending")] = out.get(c.get("state", "pending"), 0) + 1
        out["total"] = len(self.criteria)
        return out

    def summary(self) -> Dict[str, Any]:
        """`harness_summary.criteria`: counts and ids per state, every
        criterion counted."""
        counts = self.counts()
        ids = {s: [c["id"] for c in self.criteria if c.get("state") == s] for s in STATES}
        open_rows = self.ordered(self.open())
        mass = self.na_mass()
        return {
            **counts,
            "ids": ids,
            "open_total": len(open_rows),
            "mass_not_applicable": mass,
            "claimed_not_applicable": ids["not_applicable"] if mass else [],
            "shown": bool(self.shown),
            "dropped": self.dropped,
            "key": self.key,
            "open": [{"id": c["id"], "line": c.get("line"), "subject": c.get("subject"),
                      "quote": _clip(c.get("quote", ""), c.get("subject"), 160)}
                     for c in open_rows[:20]],
        }

    def summary_safe(self) -> Optional[Dict[str, Any]]:
        try:
            return self.summary()
        except Exception:  # noqa: BLE001 - a report never breaks the turn
            return None

    @staticmethod
    def _label(c: Dict[str, Any], quote_chars: int) -> str:
        where = f"L{c.get('line')}"
        if c.get("source") and c.get("source") != "message":
            where = f"{c['source']}:{where}"
        subj = f" [{c['subject']}]" if c.get("subject") else ""
        if quote_chars <= 0:
            return f"{c['id']} {where}{subj}"
        return f"{c['id']} {where}{subj}: {_clip(c.get('quote', ''), c.get('subject'), quote_chars)}"

    def _fill(self, rows: Sequence[Dict[str, Any]], budget: int, quote_chars: int) -> Tuple[List[str], int]:
        """As many rows as fit in `budget` characters; returns (lines, used rows)."""
        lines: List[str] = []
        used = 0
        for c in rows:
            line = self._label(c, quote_chars)
            if used + len(line) + 1 > budget:
                break
            lines.append(line)
            used += len(line) + 1
        return lines, len(lines)

    def compact_list(self, *, max_chars: int = START_NOTE_CHARS, quote_chars: int = 160,
                     only_open: bool = False) -> str:
        rows = self.ordered(self.open() if only_open else self.criteria)
        lines, n = self._fill(rows, max(200, max_chars - 120), quote_chars)
        if n < len(rows):
            lines.append(f"... and {len(rows) - n} more (criteria_check with {{\"list\": true, \"page\": 2}} "
                         "or a \"section\" filter shows them)")
        return "\n".join(lines)

    def sections(self, rows: Optional[Sequence[Dict[str, Any]]] = None) -> List[Tuple[str, int]]:
        """(short section name, count): the innermost heading (a numbered item
        folds into its heading), at most 40 characters."""
        counts: Dict[str, int] = {}
        for c in (self.criteria if rows is None else rows):
            path = [p for p in str(c.get("section") or "").split(" › ") if p and not p.isdigit()]
            sec = (path[-1] if path else "(no section)")[:40]
            counts[sec] = counts.get(sec, 0) + 1
        return list(counts.items())

    def list_page(self, *, section: str = "", kind: str = "", page: int = 1, only_open: bool = True,
                  page_chars: int = LIST_PAGE_CHARS, quote_chars: int = 300) -> Dict[str, Any]:
        """One page of the book for `criteria_check {"list": true}`: filtered
        by section (substring) and kind, in priority order, at most
        `page_chars` characters per page."""
        rows = self.open() if only_open else list(self.criteria)
        if section:
            s = section.strip().lower()
            rows = [c for c in rows if s in str(c.get("section") or "").lower()]
        if kind:
            rows = [c for c in rows if str(c.get("kind") or "") == kind.strip().lower()]
        rows = self.ordered(rows)
        pages: List[List[str]] = []
        cur: List[str] = []
        used = 0
        for c in rows:
            line = self._label(c, quote_chars) + f" ({c.get('state')}, {c.get('kind')})"
            if cur and used + len(line) + 1 > page_chars:
                pages.append(cur)
                cur, used = [], 0
            cur.append(line)
            used += len(line) + 1
        if cur:
            pages.append(cur)
        total_pages = max(1, len(pages))
        page = min(max(1, int(page or 1)), total_pages)
        body = pages[page - 1] if pages else []
        return {"text": "\n".join(body), "page": page, "pages": total_pages, "rows": len(rows),
                "ids": [ln.split(" ", 1)[0] for ln in body]}

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
        `test`. (Locating only: what counts as evidence is the output.)"""
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
        reason (and past NA_FRACTION of the book it no longer settles the
        criterion, see `open`). Returns one row per link."""
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
                crit["updated_at"] = time.time()
            row = {"id": crit["id"], "state": crit["state"], "detail": verdict.detail}
            if verdict.state == "pending":
                row["accepted"] = False
            rows.append(row)
        return rows

    # -- what the model is told ----------------------------------------------
    def start_note(self, *, max_chars: int = START_NOTE_CHARS) -> str:
        """Once, at the start of a long unattended turn: counts per section and
        the first open criteria in priority order (sections in turn), bounded."""
        open_rows = self.ordered(self.open())
        head = (
            "[Contract criteria - automatic runtime message] The task statement was read as "
            f"{len(self.criteria)} checkable requirements ({len(open_rows)} open). Before you finish, show each "
            "one with a tool call whose OUTPUT shows it, run after your last change, and link it with "
            "`criteria_check` (`{\"links\": [{\"id\": \"C01\", \"call_id\": \"...\"}]}` or `\"command\": \"<text "
            "of the command you ran>\"`); an echo of your own words is not evidence; mark one not_applicable "
            "only with a real reason.\n"
        )
        secs = " · ".join(f"{s}: {n}" for s, n in self.sections(open_rows))
        if len(secs) > 600:
            secs = secs[:597] + "..."
        head += ("Open by section: " + secs + "\n") if secs else ""
        tail_room = 200
        lines, n = self._fill(open_rows, max(300, max_chars - len(head) - tail_room), 120)
        text = head + "\n".join(lines)
        if n < len(open_rows):
            text += (f"\n... {len(open_rows) - n} more open: criteria_check {{\"list\": true, \"section\": "
                     "\"...\"}} or {\"list\": true, \"page\": 2}.")
        return text[:max_chars]

    def audit_addendum(self, *, max_chars: int = AUDIT_NOTE_CHARS) -> str:
        """Appended to the generic completion audit when criteria are open: the
        open criteria by priority (sections in turn) with their quotes while
        they fit, then the rest by id and line, then the "claimed not
        applicable" ones when there are too many of them."""
        rows = self.ordered([c for c in self.open() if c.get("state") in OPEN_STATES])
        claimed = self.ordered([c for c in self.open() if c.get("state") == "not_applicable"])
        head = (
            f"In particular, these {len(rows)} requirements extracted from the task statement have no accepted "
            "evidence yet (id, line, [subject], quote; one check per subject - a check of one command or case "
            "does not count for another):"
        ) if rows else "In particular, requirements extracted from the task statement still lack evidence:"
        foot = (
            "For each: run a command or test whose OUTPUT shows it, after your last change, fix what fails, then "
            "call `criteria_check` with the call (`call_id` or `command`), or `status: \"unmet\"` if it still "
            "fails, or `status: \"not_applicable\"` with a real reason."
        )
        recent = self.recent_calls(n=6, chars=70)
        foot_full = foot + (("\nRecent tool calls of this turn: " + " ; ".join(recent)) if recent else "")
        budget = max(600, max_chars - len(head) - len(foot_full) - 220)
        quoted, nq = self._fill(rows, int(budget * 0.75), 140)
        self.shown_audit = {c["id"] for c in rows[:nq]}
        rest = rows[nq:]
        id_lines, ni = self._fill(rest, budget - sum(len(x) + 1 for x in quoted), 0)
        parts = [head] + quoted
        if id_lines:
            parts.append("Also open (id, line): " + ", ".join(id_lines))
        missing = len(rest) - ni
        if claimed:
            c_lines, nc = self._fill(claimed, 600, 0)
            parts.append(f"Claimed not applicable ({len(claimed)}): " + ", ".join(c_lines)
                         + (" ..." if nc < len(claimed) else "") + ". " + NA_MASS_GUIDANCE)
        if missing > 0 or self.dropped:
            extra = f"{missing} more open were not listed" if missing > 0 else ""
            if self.dropped:
                extra += ("; " if extra else "") + f"{self.dropped} beyond the safety cap"
            parts.append(extra + ": criteria_check {\"list\": true, \"page\": 2} or a \"section\" filter.")
        parts.append(foot_full)
        return "\n".join(parts)[:max_chars]

    def audit_note(self, base: str, *, max_chars: int = AUDIT_NOTE_CHARS + 600) -> str:
        """The generic audit text `base`, unchanged, with the open criteria
        appended; the whole note stays within `max_chars`."""
        self.shown = True
        return base + "\n\n" + self.audit_addendum(max_chars=max(1500, max_chars - len(base) - 2))

    def gap_note(self, *, max_chars: int = GAP_NOTE_CHARS) -> str:
        """The single reminder after the audit: open criteria the audit did NOT
        quote come first, so it covers new ground."""
        rows = self.ordered(self.open())
        fresh = [c for c in rows if c["id"] not in self.shown_audit]
        seen = [c for c in rows if c["id"] in self.shown_audit]
        head = ("[Harness check - automatic runtime message, not a new user request] "
                f"{len(rows)} requirements of the task statement still have no accepted evidence"
                + (f" ({len(fresh)} of them not listed in the audit, shown first)" if fresh and seen else "") + ":\n")
        foot = ("\nCheck each one with a tool now and link it with `criteria_check` (a link the harness did not "
                "accept says why), or mark it unmet / not_applicable with a reason. "
                + (NA_MASS_GUIDANCE + " " if self.na_mass() else "")
                + "This is the last reminder; then give your final answer.")
        lines, n = self._fill(fresh + seen, max(400, max_chars - len(head) - len(foot) - 120), 120)
        more = f"\n... {len(rows) - n} more: criteria_check {{\"list\": true}}." if n < len(rows) else ""
        return (head + "\n".join(lines) + more + foot)[:max_chars]

    # -- persistence -----------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {"version": 2, "key": self.key, "scope": self.scope, "session_id": self.session_id,
                "dropped": self.dropped, "strict": self.strict, "created_at": self.created_at,
                "updated_at": time.time(), "criteria": self.criteria}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Optional["CriteriaBook"]:
        if not isinstance(data, dict) or not isinstance(data.get("criteria"), list):
            return None
        crit = [_coerce(c) for c in data["criteria"] if isinstance(c, dict) and c.get("id")]
        try:
            dropped = int(data.get("dropped") or 0)
        except (TypeError, ValueError):
            dropped = 0
        sid = data.get("session_id")
        return cls(crit, key=str(data.get("key") or ""), scope=data.get("scope"), dropped=dropped,
                   strict=bool(data.get("strict", True)), created_at=data.get("created_at"),
                   session_id=str(sid) if sid else None)

    def save(self) -> bool:
        """Atomic write (unique temp file). States changed meanwhile by another
        session on the same statement are merged: the newer per criterion wins.
        Nothing is written for an incognito turn (`persist` False)."""
        if not self.persist or not self.scope or not self.key:
            return False
        try:
            path = _book_path(self.scope, self.key)
            d = os.path.dirname(path)
            os.makedirs(d, exist_ok=True)
            other = load(self.scope, self.key)
            if other is not None:
                theirs = {c["id"]: c for c in other.criteria}
                for c in self.criteria:
                    t = theirs.get(c["id"])
                    if (t and _norm(t.get("quote")) == _norm(c.get("quote"))
                            and float(t.get("updated_at") or 0) > float(c.get("updated_at") or 0)
                            and t.get("state") != "proven"):
                        c["state"], c["detail"] = t["state"], t.get("detail", "")
                        c["evidence_refs"] = list(t.get("evidence_refs") or [])
                        c["updated_at"] = t.get("updated_at")
            _prune(d, keep=os.path.basename(path))
            fd, tmp = tempfile.mkstemp(prefix=".book-", suffix=".tmp", dir=d)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.to_dict(), f, ensure_ascii=False, indent=1)
            for attempt in range(3):
                try:
                    os.replace(tmp, path)
                    return True
                except PermissionError:
                    time.sleep(0.05 * (attempt + 1))
            try:
                os.remove(tmp)
            except OSError:
                pass
            return False
        except Exception:  # noqa: BLE001 - bookkeeping never breaks a turn
            return False


def _book_dir(scope: str) -> str:
    # Beside the plan tracker of the same scope, in a subfolder so
    # plan_tracker.active() (which lists `<scope>/*.json`) never reads a book
    # as a plan.
    from src import plan_tracker as _pt
    return os.path.join(_pt._scope_dir(scope), "criteria")


def _prune(d: str, *, keep: str = "") -> None:
    """Delete books (and stray temp files) older than BOOK_MAX_AGE_S in `d`.
    Runs on every save and every resume, so a scope that never resumes is
    pruned too."""
    try:
        names = os.listdir(d)
    except Exception:  # noqa: BLE001
        return
    now = time.time()
    for n in names:
        if n == keep or not (n.endswith(".json") or n.endswith(".tmp")):
            continue
        p = os.path.join(d, n)
        try:
            if now - os.path.getmtime(p) > BOOK_MAX_AGE_S:
                os.remove(p)
        except OSError:
            pass


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


def latest(scope: str, *, session_id: Optional[str] = None,
           max_age_s: float = BOOK_MAX_AGE_S) -> Optional[CriteriaBook]:
    """The most recently updated book of the scope written by `session_id`
    (when given), for a "continue" turn whose conversation holds no statement
    of its own. Books older than BOOK_MAX_AGE_S are pruned on the way."""
    d = _book_dir(scope)
    try:
        names = [n for n in os.listdir(d) if n.endswith(".json")]
    except Exception:  # noqa: BLE001
        return None
    now = time.time()
    candidates: List[Tuple[float, str]] = []
    for n in names:
        p = os.path.join(d, n)
        try:
            m = os.path.getmtime(p)
        except OSError:
            continue
        if now - m > BOOK_MAX_AGE_S:
            try:
                os.remove(p)
            except OSError:
                pass
            continue
        if now - m <= max_age_s:
            candidates.append((m, n))
    for _, n in sorted(candidates, reverse=True):
        book = load(scope, n[:-5])
        if book is None:
            continue
        if session_id and book.session_id and book.session_id != str(session_id):
            continue
        if session_id and not book.session_id:
            continue
        return book
    return None


def message_text(content: Any) -> str:
    """The text of a user message whose content may be a list of parts
    (text and images)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for p in content:
            if isinstance(p, dict) and isinstance(p.get("text"), str):
                out.append(p["text"])
            elif isinstance(p, str):
                out.append(p)
        return "\n".join(out)
    return ""


def build_for_turn(messages: Sequence[Dict[str, Any]], *, scope: Optional[str], session_id: Optional[str],
                   is_continue: bool, max_items: int = DEFAULT_MAX, min_chars: int = DEFAULT_MIN_CHARS,
                   strict: bool = True) -> Optional[CriteriaBook]:
    """The turn's book (meant to run in a worker thread). The current request
    is the latest real user message, whatever its content type. On a
    "continue" turn without a statement of its own, the conversation's own
    earlier statement is used, then the latest book this session saved in the
    scope. A resumed book starts the new turn: `proven` must be earned again."""
    users = [m for m in reversed(list(messages or [])) if m.get("role") == "user"
             and not m.get("_agent_injected") and not m.get("_harness_note")]
    book = None
    if users:
        book = CriteriaBook.from_message(message_text(users[0].get("content")), max_items=max_items,
                                         min_chars=min_chars, scope=scope, strict=strict, session_id=session_id)
    if book is None and is_continue:
        for m in users[1:12]:
            book = CriteriaBook.from_message(message_text(m.get("content")), max_items=max_items,
                                             min_chars=min_chars, scope=scope, strict=strict,
                                             session_id=session_id)
            if book is not None:
                break
        if book is None and scope:
            book = latest(scope, session_id=session_id)
            if book is not None:
                book.strict = strict
                book.session_id = session_id or book.session_id
        if book is not None:
            book.loaded = True
    if book is not None:
        book.start_new_turn()
    return book
