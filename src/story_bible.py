"""Story bible — the structured continuity state a *local* model needs to
catch the errors a generic assistant never catches.

The gap this closes
-------------------
ChatGPT and Claude fix your sentence. Neither warns you that the character
whose eyes you just described as blue had green eyes in chapter 3, because
neither is holding your novel's state — only the paragraph in the box. The
bible is that state, kept per project, written by hand or by confirmed
extraction, and read back on every review pass.

Why it is built this way
------------------------
* **Storage is one JSON file inside the workspace** (``<workspace>/.odysseus/
  story_bible.json``), beside the project's objectives and memory: greppable,
  hand-editable, travels with the folder, survives a database wipe.
* **Atomic writes** (tmp + ``os.replace``); a corrupt file is renamed to
  ``.corrupt`` and rebuilt empty. Every entry point here is read on a review
  hot path and must never raise — a broken bible costs the feature, not the
  turn (same posture as ``services/objectives.py``).
* **Extraction is deterministic and has NO LLM in it.** ``extract_candidates``
  is a recall-first proper-noun and attribute scanner; a human confirms what
  becomes a fact. ``check_continuity`` is lexical and deliberately
  conservative: same subject + same attribute key + a *different* value, and
  every finding names the bible fact it contradicts so the user can judge.
* **Edits are typed deltas** (ADD/EDIT/KILL with a rationale, human edits win)
  — the same shape ``services/objectives.py`` compiles. That compiler could
  not be reused as-is: it is welded to the objectives record (OBJ-n ids,
  title/status/priority/deps, a JSONL store with dependency-edge records) and
  generalizing it would mean editing that file, which this change may not do.
  So the compiler below is a small local one built to the same contract —
  validate each delta, record a conflict instead of failing the batch, keep
  killed records so history stays diffable.

Pure stdlib. Spanish and English patterns, because the first user writes in
both.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import unicodedata
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

BIBLE_DIRNAME = ".odysseus"
BIBLE_FILENAME = "story_bible.json"

SECTIONS = ("characters", "timeline", "facts", "places")
KINDS = ("character", "timeline", "fact", "place")

MAX_NAME_CHARS = 120
MAX_TEXT_CHARS = 600
MAX_SOURCE_CHARS = 400
MAX_BLOCK_CHARS = 2000          # cap for the prompt block fed to review()

_ID_PREFIX = {"character": "CHAR", "fact": "FACT", "timeline": "TL", "place": "PLACE"}
_SECTION_OF = {"character": "characters", "fact": "facts",
               "timeline": "timeline", "place": "places"}


class StoryBibleError(ValueError):
    """Invalid bible input or an unusable store — routes map this to a 400."""


# ----------------------------------------------------------------------
# Text normalization helpers (shared with the continuity check)
# ----------------------------------------------------------------------


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def strip_accents(text: str) -> str:
    """'Verdes' -> 'verdes', 'María' -> 'Maria'. Comparison only — never used
    to rewrite anything the user sees."""
    try:
        decomposed = unicodedata.normalize("NFD", str(text or ""))
    except (TypeError, ValueError):
        return str(text or "")
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _norm(text: str) -> str:
    return strip_accents(str(text or "")).strip().casefold()


# ----------------------------------------------------------------------
# Storage
# ----------------------------------------------------------------------


def empty_bible() -> Dict[str, Any]:
    return {"characters": [], "timeline": [], "facts": [], "places": []}


def bible_dir(project: Dict[str, Any]) -> str:
    ws = (project or {}).get("workspace") or ""
    return os.path.join(ws, BIBLE_DIRNAME) if ws else ""


def bible_path(project: Dict[str, Any]) -> str:
    base = bible_dir(project)
    return os.path.join(base, BIBLE_FILENAME) if base else ""


def _coerce_bible(raw: Any) -> Dict[str, Any]:
    """Force whatever was on disk into the documented shape. Anything that is
    not a list of objects in a known section is dropped, not raised over."""
    bible = empty_bible()
    if not isinstance(raw, dict):
        raise ValueError("story bible is not an object")
    for section in SECTIONS:
        rows = raw.get(section)
        if rows is None:
            continue
        if not isinstance(rows, list):
            raise ValueError(f"'{section}' is not a list")
        for row in rows:
            if isinstance(row, dict):
                bible[section].append(row)
    return bible


def load_bible(project: Dict[str, Any]) -> Dict[str, Any]:
    """The project's bible, or an empty one. Never raises: a corrupt file is
    renamed to ``.corrupt`` (nothing is silently destroyed) and we start over."""
    path = bible_path(project)
    if not path or not os.path.isfile(path):
        return empty_bible()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except OSError as e:
        logger.warning("story_bible.json unreadable (%s); treating as empty", e)
        return empty_bible()
    try:
        return _coerce_bible(json.loads(raw))
    except (ValueError, TypeError) as e:
        logger.warning("story_bible.json corrupt (%s); renaming to .corrupt", e)
        try:
            os.replace(path, path + ".corrupt")
        except OSError:
            pass
        return empty_bible()


def save_bible(project: Dict[str, Any], bible: Dict[str, Any]) -> None:
    """Atomic rewrite (tmp file + os.replace) — no half-written bible."""
    path = bible_path(project)
    if not path:
        raise StoryBibleError("Project has no folder bound, so it has no story bible")
    payload = empty_bible()
    for section in SECTIONS:
        for row in (bible or {}).get(section) or []:
            if isinstance(row, dict):
                payload[section].append(row)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except OSError as e:
        raise StoryBibleError(f"Could not save the story bible: {e}")


def _next_id(bible: Dict[str, Any], kind: str) -> str:
    prefix = _ID_PREFIX.get(kind, "ROW")
    top = 0
    for section in SECTIONS:
        for row in bible.get(section) or []:
            m = re.match(rf"^{prefix}-(\d+)$", str((row or {}).get("id") or ""))
            if m:
                top = max(top, int(m.group(1)))
    return f"{prefix}-{top + 1}"


# ----------------------------------------------------------------------
# Attribute vocabulary (the part that makes a contradiction detectable)
# ----------------------------------------------------------------------

# Nouns that name a stable, contradictable attribute, mapped to one key.
_ATTRIBUTE_NOUNS = {
    "ojos": "eyes", "ojo": "eyes", "eyes": "eyes", "eye": "eyes",
    "pelo": "hair", "cabello": "hair", "hair": "hair", "melena": "hair",
    "barba": "beard", "beard": "beard",
    "piel": "skin", "skin": "skin", "tez": "skin",
    "voz": "voice", "voice": "voice",
    "manos": "hands", "hands": "hands",
    "cicatriz": "scar", "scar": "scar",
}

# Values normalized across languages so 'verdes' and 'green' are one thing.
_VALUE_SYNONYMS = {
    "verde": "green", "verdes": "green", "green": "green",
    "azul": "blue", "azules": "blue", "blue": "blue",
    "marron": "brown", "marrones": "brown", "cafe": "brown", "brown": "brown",
    "castano": "brown", "castanos": "brown", "castana": "brown", "castanas": "brown",
    "negro": "black", "negros": "black", "negra": "black", "negras": "black",
    "black": "black",
    "gris": "grey", "grises": "grey", "grey": "grey", "gray": "grey",
    "rubio": "blond", "rubios": "blond", "rubia": "blond", "rubias": "blond",
    "blond": "blond", "blonde": "blond",
    "rojo": "red", "rojos": "red", "roja": "red", "rojas": "red",
    "pelirrojo": "red", "pelirroja": "red", "red": "red",
    "blanco": "white", "blancos": "white", "blanca": "white", "white": "white",
    "avellana": "hazel", "hazel": "hazel",
    "ambar": "amber", "amber": "amber", "miel": "honey", "honey": "honey",
    "oscuro": "dark", "oscuros": "dark", "oscura": "dark", "dark": "dark",
    "claro": "light", "claros": "light", "clara": "light", "light": "light",
    "largo": "long", "largos": "long", "larga": "long", "long": "long",
    "corto": "short", "cortos": "short", "corta": "short", "short": "short",
    "alta": "tall", "alto": "tall", "tall": "tall",
    "baja": "shortstature", "bajo": "shortstature",
}

# Words that are never an attribute value (articles, copulas, fillers).
_VALUE_STOPWORDS = {
    "de", "del", "la", "el", "los", "las", "un", "una", "unos", "unas", "y", "o",
    "que", "muy", "mas", "tan", "como", "su", "sus", "mi", "mis", "the", "a", "an",
    "of", "and", "or", "very", "her", "his", "its", "their", "my", "with", "in",
    "on", "was", "were", "is", "are", "had", "has", "eran", "era", "es", "son",
}

# Capitalized tokens that are not names — sentence starters and function words.
_NAME_STOPWORDS = {
    "el", "la", "los", "las", "un", "una", "unos", "unas", "y", "o", "pero",
    "cuando", "aunque", "porque", "si", "no", "si,", "entonces", "luego",
    "despues", "antes", "ahora", "hoy", "ayer", "manana", "todo", "toda",
    "todos", "todas", "este", "esta", "estos", "estas", "ese", "esa", "esos",
    "esas", "aquel", "aquella", "su", "sus", "mi", "mis", "tu", "tus", "yo",
    "el,", "ella", "ellos", "ellas", "nosotros", "usted", "ustedes", "hubo",
    "habia", "era", "fue", "sin", "con", "por", "para", "desde", "hasta",
    "the", "a", "an", "and", "or", "but", "when", "although", "because", "if",
    "then", "later", "before", "after", "now", "today", "yesterday", "tomorrow",
    "this", "that", "these", "those", "he", "she", "it", "they", "we", "you",
    "i", "his", "her", "its", "their", "our", "your", "my", "there", "here",
    "in", "on", "at", "to", "from", "with", "without", "for", "of", "by",
    "was", "were", "is", "are", "had", "has", "have", "did", "does", "do",
    "chapter", "capitulo", "señor", "senor", "señora", "senora", "don", "dona",
}

# Lowercase connectors kept inside a multi-word name: "María de la Cruz".
_NAME_CONNECTORS = {"de", "del", "la", "las", "los", "van", "von", "da", "di", "of"}

_PLACE_PREPS = {"en", "hacia", "desde", "hasta", "a", "in", "at", "to", "from",
                "into", "toward", "towards"}

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])[\s\n]+|\n{2,}")
_WORD_RE = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)?", re.UNICODE)

# Spanish "ojos verdes" / English "green eyes" — both directions of the pair.
_ES_ATTR_RE = re.compile(
    r"\b(" + "|".join(sorted(n for n in _ATTRIBUTE_NOUNS if n not in
                             ("eyes", "eye", "hair", "beard", "skin", "voice",
                              "hands", "scar"))) +
    r")\s+(?:muy\s+|bastante\s+)?([^\W\d_]{3,})",
    re.IGNORECASE | re.UNICODE)
_EN_ATTR_RE = re.compile(
    r"\b([^\W\d_]{3,})\s+(eyes|eye|hair|beard|skin|voice|hands|scar)\b",
    re.IGNORECASE | re.UNICODE)

# "X tenía/tiene/era/es ..." and "X was/is ..." — an explicit attribution.
_COPULA_RE = re.compile(
    r"\b(ten[íi]a|tiene|era|es|fue|son|eran|was|is|were|are|had|has)\b",
    re.IGNORECASE | re.UNICODE)

_TIME_RE = re.compile(
    r"\b(\d{4}|lunes|martes|mi[ée]rcoles|jueves|viernes|s[áa]bado|domingo|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"al d[íi]a siguiente|the next day|the following day|"
    r"\w+ d[íi]as (?:despu[ée]s|antes)|\w+ days (?:later|before|earlier)|"
    r"enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|octubre|"
    r"noviembre|diciembre|january|february|march|april|may|june|july|august|"
    r"september|october|november|december)\b",
    re.IGNORECASE | re.UNICODE)


def normalize_value(value: str) -> str:
    """Canonical form of an attribute value: 'verdes' and 'green' both become
    'green'. An unknown word keeps its accent-stripped, lowercased form with a
    light plural strip, so 'cicatrices' and 'cicatriz' do not read as two
    different values by accident."""
    token = _norm(value)
    if token in _VALUE_SYNONYMS:
        return _VALUE_SYNONYMS[token]
    if token.endswith("es") and len(token) > 5:
        stem = token[:-2]
    elif token.endswith("s") and len(token) > 4:
        stem = token[:-1]
    else:
        stem = token
    return _VALUE_SYNONYMS.get(stem, stem)


def _is_known_value(value: str) -> bool:
    return _norm(value) in _VALUE_SYNONYMS or normalize_value(value) in set(_VALUE_SYNONYMS.values())


def sentences(text: str) -> List[Tuple[int, int, str]]:
    """(start, end, sentence) over the ORIGINAL offsets. Never raises."""
    body = str(text or "")
    if not body.strip():
        return []
    out: List[Tuple[int, int, str]] = []
    pos = 0
    for piece in _SENTENCE_SPLIT_RE.split(body):
        if piece is None:
            continue
        idx = body.find(piece, pos)
        if idx < 0:
            idx = pos
        start = idx
        end = idx + len(piece)
        pos = end
        if piece.strip():
            out.append((start, end, piece))
    if not out:
        out.append((0, len(body), body))
    return out


# ----------------------------------------------------------------------
# Candidate extraction (deterministic, recall first — a human confirms)
# ----------------------------------------------------------------------


def _name_runs(sentence: str) -> List[Tuple[int, str]]:
    """Runs of capitalized tokens (with lowercase connectors kept inside),
    returned as (offset in the sentence, name).

    A run that starts at the sentence's first word also yields its tail
    separately: in "Entonces Marta entró" the capital on "Entonces" is
    punctuation, not a name, and "Marta" must still be found. Emitting both
    over-matches on purpose — recall beats precision here, and the sentence
    position is what later separates a name from a sentence starter.
    """
    runs: List[Tuple[int, str]] = []
    tokens = [(m.start(), m.group(0)) for m in _WORD_RE.finditer(sentence)]
    first_off = tokens[0][0] if tokens else -1
    i = 0
    while i < len(tokens):
        off, tok = tokens[i]
        if not tok[:1].isupper():
            i += 1
            continue
        parts = [(off, tok)]
        last = i
        j = i + 1
        while j < len(tokens):
            nxt_off, nxt = tokens[j]
            if nxt[:1].isupper():
                parts.append((nxt_off, nxt))
                last = j
                j += 1
            elif _norm(nxt) in _NAME_CONNECTORS and j + 1 < len(tokens) \
                    and tokens[j + 1][1][:1].isupper():
                parts.append((nxt_off, nxt))
                j += 1
            else:
                break
        runs.append((off, " ".join(p for _o, p in parts)))
        if len(parts) > 1 and (off == first_off or _norm(tok) in _NAME_STOPWORDS):
            runs.append((parts[1][0], " ".join(p for _o, p in parts[1:])))
        i = max(last + 1, i + 1)
    return runs


def _attribute_hits(sentence: str) -> List[Tuple[str, str, int, int]]:
    """(key, raw value, start, end) for every attribute phrase in a sentence."""
    hits: List[Tuple[str, str, int, int]] = []
    for m in _ES_ATTR_RE.finditer(sentence):
        noun, value = m.group(1), m.group(2)
        if _norm(value) in _VALUE_STOPWORDS:
            continue
        key = _ATTRIBUTE_NOUNS.get(_norm(noun))
        if key:
            hits.append((key, value, m.start(), m.end()))
    for m in _EN_ATTR_RE.finditer(sentence):
        value, noun = m.group(1), m.group(2)
        if _norm(value) in _VALUE_STOPWORDS:
            continue
        key = _ATTRIBUTE_NOUNS.get(_norm(noun))
        if key:
            hits.append((key, value, m.start(), m.end()))
    # Deterministic order, de-duplicated on (key, value, span).
    seen = set()
    out = []
    for hit in sorted(hits, key=lambda h: (h[2], h[3], h[0])):
        sig = (hit[0], normalize_value(hit[1]), hit[2], hit[3])
        if sig in seen:
            continue
        seen.add(sig)
        out.append(hit)
    return out


def known_names(bible: Dict[str, Any]) -> Dict[str, str]:
    """{normalized name or alias -> the character's canonical name}."""
    out: Dict[str, str] = {}
    for row in (bible or {}).get("characters") or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        out[_norm(name)] = name
        for alias in row.get("aliases") or []:
            alias = str(alias or "").strip()
            if alias:
                out[_norm(alias)] = name
    return out


def extract_candidates(text: str, existing: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Everything in ``text`` that *might* belong in the bible.

    Deterministic and LLM-free. Recall beats precision here on purpose: a
    human confirms the candidates, and a missed character is a continuity bug
    that ships while a spurious one costs a click.

    A name is a run of capitalized tokens that appears at least twice and at
    least once away from a sentence start (so "Entonces" at the head of three
    sentences is not a character). Facts come from the attribute patterns —
    ``ojos verdes`` / ``green eyes`` — and from explicit ``X tenía…`` /
    ``X was…`` attributions, each carrying its own sentence as the source.
    Never raises.
    """
    result: Dict[str, Any] = {"characters": [], "facts": [], "places": [], "timeline": []}
    try:
        body = str(text or "")
        if not body.strip():
            return result
        existing_names = known_names(existing or {})

        total: Dict[str, int] = {}
        mid: Dict[str, int] = {}
        display: Dict[str, str] = {}
        place_hint: Dict[str, int] = {}
        sentence_of: Dict[str, str] = {}

        for s_start, _s_end, sentence in sentences(body):
            first_word = _WORD_RE.search(sentence)
            first_off = first_word.start() if first_word else -1
            for off, name in _name_runs(sentence):
                key = _norm(name)
                if not key or key in _NAME_STOPWORDS:
                    continue
                # A run made only of stopwords is noise; so is one that merely
                # carries a stopword head ("Entonces Marta", "Don Quijote") —
                # _name_runs already emitted the tail, which is the real name.
                parts = [p for p in key.split() if p]
                if all(p in _NAME_STOPWORDS for p in parts):
                    continue
                if len(parts) > 1 and parts[0] in _NAME_STOPWORDS:
                    continue
                total[key] = total.get(key, 0) + 1
                display.setdefault(key, name)
                sentence_of.setdefault(key, sentence.strip()[:MAX_SOURCE_CHARS])
                if off != first_off:
                    mid[key] = mid.get(key, 0) + 1
                # Place hint: preceded by a locative preposition.
                before = sentence[:off].rstrip()
                prev = _WORD_RE.findall(before)
                if prev and _norm(prev[-1]) in _PLACE_PREPS:
                    place_hint[key] = place_hint.get(key, 0) + 1
            _ = s_start   # the character rows carry their sentence, not a span

        for key in sorted(total):
            if total[key] < 2 or mid.get(key, 0) < 1:
                continue
            name = display[key]
            row = {"name": name, "aliases": [], "mentions": total[key],
                   "source": sentence_of.get(key, ""),
                   "new": key not in existing_names}
            if place_hint.get(key, 0) >= 1:
                result["places"].append(dict(row))
            result["characters"].append(row)

        # Facts: an attribute phrase attributed to a name in the same sentence.
        candidate_names = {k: display[k] for k in total}
        candidate_names.update({k: v for k, v in existing_names.items()})
        for s_start, _s_end, sentence in sentences(body):
            names_here = [n for _off, n in _name_runs(sentence)
                          if _norm(n) in candidate_names]
            # A name the bible already knows wins the subject slot; failing
            # that, a run without a stopword head ("Marta", not "Entonces
            # Marta") is the better guess.
            resolved = [existing_names[_norm(n)] for n in names_here
                        if _norm(n) in existing_names]
            if not resolved:
                resolved = [n for n in names_here
                            if _norm(n).split()[0] not in _NAME_STOPWORDS] or names_here
            for key, value, a_start, a_end in _attribute_hits(sentence):
                subject = resolved[0] if resolved else ""
                result["facts"].append({
                    "subject": subject,
                    "key": key,
                    "value": normalize_value(value),
                    "raw_value": value,
                    "text": sentence[a_start:a_end].strip(),
                    "source": sentence.strip()[:MAX_SOURCE_CHARS],
                    "span": {"start": s_start + a_start, "end": s_start + a_end},
                    "new": True,
                })
            # Explicit copula attributions become free-text facts.
            if resolved and _COPULA_RE.search(sentence):
                result["facts"].append({
                    "subject": resolved[0], "key": "", "value": "",
                    "raw_value": "",
                    "text": sentence.strip()[:MAX_TEXT_CHARS],
                    "source": sentence.strip()[:MAX_SOURCE_CHARS],
                    "span": {"start": s_start, "end": s_start + len(sentence)},
                    "new": True,
                })
            when = _TIME_RE.search(sentence)
            if when:
                result["timeline"].append({
                    "when": when.group(0),
                    "what": sentence.strip()[:MAX_TEXT_CHARS],
                    "source": sentence.strip()[:MAX_SOURCE_CHARS],
                    "span": {"start": s_start, "end": s_start + len(sentence)},
                    "new": True,
                })
        return result
    except Exception as e:  # noqa: BLE001 - review hot path, never raise
        logger.debug("extract_candidates failed: %s", e)
        return result


# ----------------------------------------------------------------------
# Continuity check (the payoff)
# ----------------------------------------------------------------------


def _recorded_attributes(bible: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every (subject, key, value) the bible records, from character facts and
    the flat facts list alike. A stored fact may carry ``key``/``value``
    already; otherwise they are re-derived from its text."""
    out: List[Dict[str, Any]] = []

    def _push(subject: str, fact: Dict[str, Any]) -> None:
        text = str(fact.get("text") or "")
        key = str(fact.get("key") or "").strip().lower()
        value = str(fact.get("value") or "").strip()
        if key and value:
            out.append({"subject": subject, "key": key, "value": normalize_value(value),
                        "raw_value": value, "text": text,
                        "source": str(fact.get("source") or ""),
                        "id": str(fact.get("id") or "")})
            return
        for hit_key, hit_value, _a, _b in _attribute_hits(text):
            out.append({"subject": subject, "key": hit_key,
                        "value": normalize_value(hit_value), "raw_value": hit_value,
                        "text": text, "source": str(fact.get("source") or ""),
                        "id": str(fact.get("id") or "")})

    for row in (bible or {}).get("characters") or []:
        if not isinstance(row, dict):
            continue
        subject = str(row.get("name") or "").strip()
        if not subject:
            continue
        for fact in row.get("facts") or []:
            if isinstance(fact, dict):
                _push(subject, fact)
    for fact in (bible or {}).get("facts") or []:
        if isinstance(fact, dict):
            subject = str(fact.get("subject") or "").strip()
            if subject:
                _push(subject, fact)
    return out


def check_continuity(text: str, bible: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Findings about ``text`` given the recorded state. Never raises.

    Kinds:
      * ``contradiction`` — the text states an attribute for a character the
        bible already records with a *different* value. Same subject, same
        attribute key, different canonical value; the bible fact it
        contradicts is named in ``bible_fact`` so the user can judge.
      * ``unknown_character`` — a repeated proper noun that is in no character
        name or alias. Low confidence by construction; it is a prompt to add
        someone, not an accusation.
      * ``timeline`` — the text places a recorded event at a different time
        than the bible's entry for it.

    Confidence is not a probability, it is how much of the check was carried
    by known vocabulary: 0.85 when both values resolve to known attribute
    values, 0.55 when one of them is a word we do not know.
    """
    findings: List[Dict[str, Any]] = []
    try:
        body = str(text or "")
        if not body.strip():
            return findings
        names = known_names(bible or {})
        recorded = _recorded_attributes(bible or {})

        for s_start, _s_end, sentence in sentences(body):
            present = []
            for _off, name in _name_runs(sentence):
                canon = names.get(_norm(name))
                if canon and canon not in present:
                    present.append(canon)
            if not present:
                continue
            for key, value, a_start, a_end in _attribute_hits(sentence):
                stated = normalize_value(value)
                for fact in recorded:
                    if fact["subject"] not in present or fact["key"] != key:
                        continue
                    if fact["value"] == stated:
                        continue
                    both_known = _is_known_value(value) and _is_known_value(fact["raw_value"])
                    findings.append({
                        "kind": "contradiction",
                        "detail": (f"{fact['subject']}: the text says "
                                   f"{key} = {stated}, the bible records {key} = {fact['value']}"),
                        "subject": fact["subject"],
                        "key": key,
                        "stated_value": stated,
                        "bible_value": fact["value"],
                        "bible_fact": {"subject": fact["subject"], "text": fact["text"],
                                       "source": fact["source"], "key": fact["key"],
                                       "value": fact["value"],
                                       **({"id": fact["id"]} if fact.get("id") else {})},
                        "text_span": {"start": s_start + a_start, "end": s_start + a_end,
                                      "quote": sentence[a_start:a_end]},
                        "confidence": 0.85 if both_known else 0.55,
                    })

        # Unknown characters: repeated proper nouns nobody recorded.
        if names or (bible or {}).get("characters"):
            for cand in extract_candidates(body, bible).get("characters") or []:
                if not cand.get("new"):
                    continue
                idx = body.find(cand["name"])
                findings.append({
                    "kind": "unknown_character",
                    "detail": (f"'{cand['name']}' appears {cand.get('mentions', 0)} times "
                               "and is in no character record"),
                    "subject": cand["name"],
                    "bible_fact": None,
                    "text_span": ({"start": idx, "end": idx + len(cand["name"]),
                                   "quote": cand["name"]} if idx >= 0 else None),
                    "confidence": 0.4,
                })

        # Timeline: a recorded event mentioned with a different time marker.
        for entry in (bible or {}).get("timeline") or []:
            if not isinstance(entry, dict):
                continue
            when = str(entry.get("when") or "").strip()
            what = str(entry.get("what") or "").strip()
            if not when or not what:
                continue
            anchors = [w for w in _WORD_RE.findall(what)
                       if len(w) >= 5 and _norm(w) not in _NAME_STOPWORDS]
            if not anchors:
                continue
            for s_start, _s_end, sentence in sentences(body):
                low = _norm(sentence)
                if not any(_norm(a) in low for a in anchors):
                    continue
                stated = _TIME_RE.search(sentence)
                if not stated or _norm(stated.group(0)) == _norm(when):
                    continue
                findings.append({
                    "kind": "timeline",
                    "detail": (f"the text places this at '{stated.group(0)}', "
                               f"the bible records '{when}'"),
                    "subject": what[:80],
                    "bible_fact": {"subject": what[:80], "text": what, "when": when,
                                   "source": str(entry.get("source") or ""),
                                   **({"id": str(entry.get("id"))} if entry.get("id") else {})},
                    "text_span": {"start": s_start + stated.start(),
                                  "end": s_start + stated.end(),
                                  "quote": stated.group(0)},
                    "confidence": 0.45,
                })
                break
        return findings
    except Exception as e:  # noqa: BLE001 - review hot path, never raise
        logger.debug("check_continuity failed: %s", e)
        return findings


# ----------------------------------------------------------------------
# The typed-delta compiler (deterministic — no LLM anywhere)
# ----------------------------------------------------------------------


def _find_row(bible: Dict[str, Any], row_id: str) -> Tuple[str, Optional[Dict[str, Any]]]:
    for section in SECTIONS:
        for row in bible.get(section) or []:
            if isinstance(row, dict) and str(row.get("id") or "") == row_id:
                return section, row
    return "", None


def _character_by_name(bible: Dict[str, Any], name: str) -> Optional[Dict[str, Any]]:
    target = _norm(name)
    for row in bible.get("characters") or []:
        if not isinstance(row, dict):
            continue
        if _norm(row.get("name")) == target:
            return row
        if any(_norm(a) == target for a in row.get("aliases") or []):
            return row
    return None


def _clean_list(value: Any, cap: int = MAX_NAME_CHARS) -> List[str]:
    out: List[str] = []
    if isinstance(value, (list, tuple)):
        for item in value:
            text = str(item or "").strip()[:cap]
            if text and text not in out:
                out.append(text)
    return out


def apply_deltas(project: Dict[str, Any], deltas: Iterable[Dict[str, Any]],
                 actor: str, session_id: Optional[str] = None) -> Dict[str, Any]:
    """Validate and apply typed bible deltas; conflicts are recorded, never raised.

    Delta shape::

        {"op": "ADD|EDIT|KILL", "kind": "character|fact|timeline|place",
         "id": "CHAR-1"            (EDIT/KILL),
         "name": "Marta",          (character/place)
         "aliases": ["la doctora"],
         "subject": "Marta",       (fact)
         "text": "ojos verdes",    (fact)
         "key": "eyes", "value": "verde",
         "when": "1998", "what": "…",   (timeline)
         "source": "chapter 3, p. 41",
         "rationale": "…", "base_updated_at": "…"}

    Apply order is ADDs, then EDITs, then KILLs (input order inside each), so
    a batch can create a character and immediately edit it. Human edits win:
    an agent EDIT based on a state older than a user's change is a conflict,
    not a silent overwrite. KILL keeps the record with ``"killed": true`` so
    history stays diffable.

    Returns ``{"applied": [...], "conflicts": [...], "bible": {...}}``.
    """
    actor = "agent" if actor == "agent" else "user"
    bible = load_bible(project)
    applied: List[Dict[str, Any]] = []
    conflicts: List[Dict[str, Any]] = []
    now = _now_iso()

    buckets: Dict[str, List[Dict[str, Any]]] = {"ADD": [], "EDIT": [], "KILL": []}
    for delta in deltas or []:
        if not isinstance(delta, dict):
            conflicts.append({"op": None, "id": None, "reason": "delta is not an object"})
            continue
        op = str(delta.get("op") or "").strip().upper()
        if op in buckets:
            buckets[op].append(delta)
        else:
            conflicts.append({"op": op or None, "id": delta.get("id"),
                              "reason": f"unknown op '{delta.get('op')}' (use ADD, EDIT or KILL)"})

    def _kind_of(delta: Dict[str, Any], default: str = "") -> str:
        kind = str(delta.get("kind") or default or "").strip().lower().rstrip("s")
        if kind == "characters":
            kind = "character"
        return kind if kind in KINDS else ""

    for delta in buckets["ADD"]:
        kind = _kind_of(delta, "fact" if delta.get("text") else "character")
        if not kind:
            conflicts.append({"op": "ADD", "id": None,
                              "reason": f"unknown kind '{delta.get('kind')}' "
                                        f"(use {', '.join(KINDS)})"})
            continue
        row_id = _next_id(bible, kind)
        base = {"id": row_id, "created_at": now, "updated_at": now,
                "last_actor": actor, "killed": False,
                "source": str(delta.get("source") or "")[:MAX_SOURCE_CHARS],
                "rationale": str(delta.get("rationale") or "")[:MAX_TEXT_CHARS]}
        if kind in ("character", "place"):
            name = str(delta.get("name") or "").strip()[:MAX_NAME_CHARS]
            if not name:
                conflicts.append({"op": "ADD", "id": None,
                                  "reason": f"ADD {kind} requires a name"})
                continue
            if kind == "character" and _character_by_name(bible, name):
                conflicts.append({"op": "ADD", "id": None,
                                  "reason": f"character '{name}' already exists"})
                continue
            row = {**base, "name": name, "aliases": _clean_list(delta.get("aliases")),
                   "facts": []}
            for fact in delta.get("facts") or []:
                if isinstance(fact, dict) and str(fact.get("text") or "").strip():
                    row["facts"].append({
                        "text": str(fact["text"]).strip()[:MAX_TEXT_CHARS],
                        "key": str(fact.get("key") or "").strip().lower(),
                        "value": normalize_value(fact.get("value") or "") if fact.get("value") else "",
                        "source": str(fact.get("source") or "")[:MAX_SOURCE_CHARS],
                        "first_seen": now, "updated_at": now})
            bible[_SECTION_OF[kind]].append(row)
        elif kind == "fact":
            text = str(delta.get("text") or "").strip()[:MAX_TEXT_CHARS]
            if not text:
                conflicts.append({"op": "ADD", "id": None, "reason": "ADD fact requires text"})
                continue
            row = {**base, "subject": str(delta.get("subject") or "").strip()[:MAX_NAME_CHARS],
                   "text": text,
                   "key": str(delta.get("key") or "").strip().lower(),
                   "value": normalize_value(delta.get("value") or "") if delta.get("value") else ""}
            bible["facts"].append(row)
        else:  # timeline
            what = str(delta.get("what") or delta.get("text") or "").strip()[:MAX_TEXT_CHARS]
            if not what:
                conflicts.append({"op": "ADD", "id": None,
                                  "reason": "ADD timeline requires 'what'"})
                continue
            row = {**base, "when": str(delta.get("when") or "").strip()[:MAX_NAME_CHARS],
                   "what": what}
            bible["timeline"].append(row)
        applied.append({"op": "ADD", "id": row_id, "kind": kind,
                        "rationale": str(delta.get("rationale") or "")})

    for delta in buckets["EDIT"]:
        row_id = str(delta.get("id") or "")
        section, row = _find_row(bible, row_id)
        if not row:
            conflicts.append({"op": "EDIT", "id": row_id or None,
                              "reason": f"'{row_id}' does not exist in the story bible"})
            continue
        base_seen = str(delta.get("base_updated_at") or "")
        if (actor == "agent" and base_seen
                and str(row.get("updated_at") or "") > base_seen
                and row.get("last_actor") == "user"):
            conflicts.append({"op": "EDIT", "id": row_id,
                              "reason": "human edit wins: the entry was edited by a user "
                                        "after the state this delta was based on"})
            continue
        changed: Dict[str, Any] = {}
        for field in ("name", "subject", "text", "what", "when", "source", "key"):
            # A field is editable when the row already has it (so a timeline
            # delta cannot graft a "name" onto a character-shaped hole) or
            # when it is one of the two every row may carry.
            if field in delta and (field in row or field in ("source", "key")):
                value = str(delta.get(field) or "").strip()
                cap = MAX_TEXT_CHARS if field in ("text", "what") else MAX_NAME_CHARS
                changed[field] = value[:cap]
        if "value" in delta:
            changed["value"] = normalize_value(delta.get("value") or "")
        if "aliases" in delta and "aliases" in row:
            changed["aliases"] = _clean_list(delta.get("aliases"))
        if "facts" in delta and "facts" in row:
            facts = []
            for fact in delta.get("facts") or []:
                if isinstance(fact, dict) and str(fact.get("text") or "").strip():
                    facts.append({
                        "text": str(fact["text"]).strip()[:MAX_TEXT_CHARS],
                        "key": str(fact.get("key") or "").strip().lower(),
                        "value": normalize_value(fact.get("value") or "") if fact.get("value") else "",
                        "source": str(fact.get("source") or "")[:MAX_SOURCE_CHARS],
                        "first_seen": str(fact.get("first_seen") or now),
                        "updated_at": now})
            changed["facts"] = facts
        if not changed:
            continue        # an empty edit is a no-op, not a conflict
        if "name" in changed and not changed["name"]:
            conflicts.append({"op": "EDIT", "id": row_id, "reason": "name cannot be empty"})
            continue
        row.update(changed)
        row["updated_at"] = now
        row["last_actor"] = actor
        applied.append({"op": "EDIT", "id": row_id, "kind": section,
                        "fields": sorted(changed),
                        "rationale": str(delta.get("rationale") or "")})

    for delta in buckets["KILL"]:
        row_id = str(delta.get("id") or "")
        section, row = _find_row(bible, row_id)
        if not row:
            conflicts.append({"op": "KILL", "id": row_id or None,
                              "reason": f"'{row_id}' does not exist in the story bible"})
            continue
        rationale = str(delta.get("rationale") or "").strip()
        if actor == "agent" and not rationale:
            conflicts.append({"op": "KILL", "id": row_id,
                              "reason": "KILL requires a rationale when the agent proposes it"})
            continue
        row["killed"] = True
        row["updated_at"] = now
        row["last_actor"] = actor
        applied.append({"op": "KILL", "id": row_id, "kind": section,
                        "rationale": rationale})

    if applied:
        save_bible(project, bible)      # may raise StoryBibleError on a dead disk
    return {"applied": applied, "conflicts": conflicts,
            "bible": live_bible(bible), "session": session_id}


def live_bible(bible: Dict[str, Any]) -> Dict[str, Any]:
    """The bible without killed rows — what a reader (or a review pass) sees."""
    out = empty_bible()
    for section in SECTIONS:
        for row in (bible or {}).get(section) or []:
            if isinstance(row, dict) and not row.get("killed"):
                out[section].append(row)
    return out


# ----------------------------------------------------------------------
# Prompt rendering
# ----------------------------------------------------------------------


def render_findings(findings: Iterable[Dict[str, Any]], cap: int = MAX_BLOCK_CHARS) -> str:
    """The continuity findings as the block fed into a review prompt. Empty
    string when there is nothing to say — the prompt then has no section for
    it at all, which keeps the KV cache stable across passes."""
    lines: List[str] = []
    for finding in findings or []:
        if not isinstance(finding, dict):
            continue
        kind = str(finding.get("kind") or "?")
        detail = str(finding.get("detail") or "").strip()
        quote = ((finding.get("text_span") or {}) or {}).get("quote") or ""
        line = f"- [{kind}] {detail}"
        if quote:
            line += f' — in the text: "{str(quote)[:120]}"'
        fact = finding.get("bible_fact") or {}
        if isinstance(fact, dict) and fact.get("text"):
            src = str(fact.get("source") or "").strip()
            line += f' — bible: "{str(fact["text"])[:120]}"'
            if src:
                line += f" ({src[:80]})"
        lines.append(line)
    if not lines:
        return ""
    text = "\n".join(lines)
    if len(text) > cap:
        kept: List[str] = []
        used = 0
        for line in lines:
            if used + len(line) + 1 > cap - 60:
                break
            kept.append(line)
            used += len(line) + 1
        kept.append(f"[{len(lines) - len(kept)} more continuity findings not shown]")
        text = "\n".join(kept)
    return text


def bible_summary(bible: Dict[str, Any], cap: int = MAX_BLOCK_CHARS) -> str:
    """A compact roster of who and what is recorded, for the review prompt."""
    live = live_bible(bible or {})
    lines: List[str] = []
    for row in live.get("characters") or []:
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        aliases = ", ".join(str(a) for a in row.get("aliases") or [])
        facts = "; ".join(str((f or {}).get("text") or "") for f in row.get("facts") or []
                          if isinstance(f, dict))
        line = f"- {name}"
        if aliases:
            line += f" (aka {aliases})"
        if facts:
            line += f": {facts}"
        lines.append(line)
    for row in live.get("facts") or []:
        subject = str(row.get("subject") or "").strip()
        text = str(row.get("text") or "").strip()
        if text:
            lines.append(f"- {subject + ': ' if subject else ''}{text}")
    for row in live.get("timeline") or []:
        when = str(row.get("when") or "").strip()
        what = str(row.get("what") or "").strip()
        if what:
            lines.append(f"- [{when or 'when unknown'}] {what}")
    if not lines:
        return ""
    text = "\n".join(lines)
    return text if len(text) <= cap else text[:cap - 3].rstrip() + "..."


def story_block(project: Dict[str, Any], text: str, cap: int = MAX_BLOCK_CHARS) -> str:
    """The whole story-bible section for a review prompt: what is recorded,
    plus what this passage seems to contradict. Never raises."""
    try:
        bible = load_bible(project)
        findings = check_continuity(text, bible)
        parts = []
        summary = bible_summary(bible, cap)
        if summary:
            parts.append("Recorded so far:\n" + summary)
        rendered = render_findings(findings, cap)
        if rendered:
            parts.append("Possible continuity problems in this passage:\n" + rendered)
        return "\n\n".join(parts)
    except Exception as e:  # noqa: BLE001 - review hot path, never raise
        logger.debug("story_block failed: %s", e)
        return ""


def payload(project: Dict[str, Any]) -> Dict[str, Any]:
    """The tool/API view of the bible: live rows plus the counts."""
    bible = load_bible(project)
    live = live_bible(bible)
    return {"bible": live,
            "counts": {section: len(live.get(section) or []) for section in SECTIONS}}
