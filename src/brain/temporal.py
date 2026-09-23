"""brain/temporal.py — reading "when" out of a plain sentence.

A memory item already has ``valid_from``/``valid_until`` columns
(``src/memory_engine.py``); nothing before this module ever filled them from
the TEXT itself. "Ada worked at Cordera Labs until March 2025" carries its
own validity window in the sentence — a human should never have to fill a
date picker to say something this ordinary.

``parse_temporal`` is a small, table-driven, deterministic parser (Spanish
+ English). It is deliberately conservative: a bare capitalized word that
happens to be a month name ("Marzo" as the first word of a sentence, e.g. a
person's name) must NEVER be read as a date. A month only counts once it
sits in date-ish company — a marker before it ("desde marzo", "since
March"), a year after it ("marzo de 2025", "March 2025"), or one of the
relative phrases below. No marker, no date-ish company at all -> every
field comes back ``None``: "never guess" is the whole point of this module.

Three independent things can be found in one sentence, and any combination
of them may be present at once:

* a **validity window** — ``valid_from``/``valid_until``, from an explicit
  date/month/year, a "desde"/"since"/"from" or "hasta"/"until"/"till"
  marker, an "entre X y Y"/"between X and Y" range, or a relative phrase
  ("el año pasado", "this month") relative to ``now``. A dated mention with
  no directional marker at all ("marzo de 2025", "en 2024") is read as the
  START of validity — the ordinary reading of "X happened in March 2025".
* a **state** — ``"past"`` for "ya no"/"no longer"/"used to"/"antes"/
  "solía" (a fact that stopped being true, but with NO invented date: the
  caller decides what to do with a state and no window), ``"current"`` for
  "ahora"/"now"/"currently"/"actualmente", ``"future"`` for a small set of
  forward-looking phrases.
* the raw ``markers`` that were matched, for anyone who wants to show what
  triggered the parse (a debug view, a tooltip) without re-parsing.

A four-digit number is a year only when nothing says it is a quantity: it
must lie in 1900..now+50, not follow an "up to" phrase ("acepta hasta",
"de hasta"), not precede a unit/plural/rate ("hasta 2048 tokens", "hasta
2000 al dia"), and a range partner ("from 2000 to 4000") must be a year too.
Dates inside code, links, paths, CLI flags and quoted literals are masked
out first. A bare month rolls to the right year relative to ``now``
(since -> most recent occurrence, until -> next one), and a window that
would end before it starts is dropped whole: a missed date costs little, a
wrong ``valid_until`` hides a memory.

Nothing here touches a store or calls a model — this is a pure function of
(``text``, ``now``), unit-tested in isolation.
"""

from __future__ import annotations

import calendar
import logging
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

_MONTHS: Dict[str, int] = {
    # Spanish
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
    # English
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
    "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
}
# Longest-first so "septiembre" is tried before a shorter alternative could
# swallow part of it (none currently overlap, but this keeps it safe as the
# table grows).
_MONTH_ALT = "|".join(sorted((re.escape(m) for m in _MONTHS), key=len, reverse=True))

_SINCE_WORDS = r"(?:a\s+partir\s+de|desde|since|from)"
_UNTIL_WORDS = r"(?:hasta|until|till)"
_BETWEEN_WORDS = r"(?:entre|between)"
# "from 2019 to 2021" / "de 2019 a 2021": a range written with a start word
# and an end word. Only explicit shapes and bare years live here — two bare
# months ("de marzo a junio") are exactly the "past episode or plan?" case
# this module refuses to guess.
_RANGE_FROM_WORDS = r"(?:from|desde|de)"
_RANGE_TO_WORDS = r"(?:to|until|till|hasta|a)"

_SPANISH_MONTHS = frozenset({
    "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
    "septiembre", "setiembre", "octubre", "noviembre", "diciembre",
})

# A four-digit number is only a YEAR when nothing around it says it is a
# quantity. These are the words that, right after the number, make it a
# count/size/price instead ("hasta 2048 tokens", "hasta 2000 MB").
_UNIT_WORDS = frozenset({
    "token", "tokens", "llamada", "llamadas", "call", "calls", "vez", "veces", "time", "times",
    "request", "requests", "peticion", "peticiones", "consulta", "consultas", "query", "queries",
    "fila", "filas", "row", "rows", "linea", "lineas", "line", "lines", "caracter", "caracteres",
    "character", "characters", "char", "chars", "palabra", "palabras", "word", "words",
    "byte", "bytes", "bit", "bits", "kb", "mb", "gb", "tb", "kib", "mib", "gib", "tib",
    "kbps", "mbps", "gbps", "ms", "s", "seg", "segundo", "segundos", "second", "seconds", "sec",
    "secs", "minuto", "minutos", "minute", "minutes", "min", "mins", "hora", "horas", "hour",
    "hours", "h", "hr", "hrs", "dia", "dias", "day", "days", "px", "pixel", "pixeles", "pixels",
    "dpi", "rpm", "fps", "hz", "khz", "mhz", "ghz", "w", "kw", "v", "mah", "km", "m", "cm", "mm",
    "metro", "metros", "meter", "meters", "kg", "g", "gr", "gramo", "gramos", "gram", "grams",
    "l", "litro", "litros", "liter", "liters", "usd", "eur", "euro", "euros", "dolar",
    "dolares", "dollar", "dollars", "peso", "pesos", "libra", "libras", "pound", "pounds",
    "usuario", "usuarios", "user", "users", "cliente", "clientes", "customer", "customers",
    "persona", "personas", "people", "item", "items", "elemento", "elementos", "entrada",
    "entradas", "entry", "entries", "registro", "registros", "record", "records", "archivo",
    "archivos", "file", "files", "fichero", "ficheros", "pagina", "paginas", "page", "pages",
    "mensaje", "mensajes", "message", "messages", "gpu", "gpus", "cpu", "cpus", "nucleo",
    "nucleos", "core", "cores", "hilo", "hilos", "thread", "threads", "worker", "workers",
    "paso", "pasos", "step", "steps", "iteracion", "iteraciones", "iteration", "iterations",
    "epoch", "epochs", "lote", "lotes", "batch", "batches", "muestra", "muestras", "sample",
    "samples", "punto", "puntos", "point", "points", "unidad", "unidades", "unit", "units",
    "intento", "intentos", "attempt", "attempts", "retry", "retries", "reintento", "reintentos",
    "nodo", "nodos", "node", "nodes", "instancia", "instancias", "instance", "instances",
    "parametro", "parametros", "param", "params", "parameter", "parameters", "columna",
    "columnas", "column", "columns", "campo", "campos", "field", "fields", "capa", "capas",
    "layer", "layers", "x", "k", "kbit", "mbit",
})
# Words right after the number that make it a rate/limit ("hasta 2000 al
# dia", "hasta 2000 por usuario", "hasta 2000 o mas").
_QUANTITY_FOLLOW = frozenset({
    "al", "por", "per", "cada", "each", "every", "o", "or", "mas", "more", "max", "min",
    "maximo", "minimo", "aprox", "approx", "approximately", "aproximadamente",
})
# Lowercase words ending in "s" that are NOT plural count nouns — anything
# else ending in "s" right after a four-digit number is read as a unit.
_S_FUNCTION_WORDS = frozenset({
    "is", "was", "has", "does", "as", "its", "his", "this", "thus", "us", "yes", "always",
    "perhaps", "unless", "whereas", "besides", "afterwards", "sometimes", "los", "las", "les",
    "nos", "sus", "tus", "mis", "ellos", "ellas", "nosotros", "vosotros", "ustedes", "despues",
    "antes", "entonces", "tras", "pues", "mientras", "ademas", "luego",
})
# Words (folded) right before the marker that make "hasta N" mean "up to N":
# "acepta hasta 2048", "soporta hasta 2000", "un maximo de hasta 2048".
_QUANTITY_BEFORE_STEMS: Tuple[str, ...] = (
    "permit", "admit", "acept", "accept", "soport", "support", "aguant", "alcanz", "reach",
    "escal", "scale", "manej", "handl", "proces", "almacen", "store", "stores", "hold", "allow",
    "limit", "maxim", "minim", "cuest", "costo", "coste", "weigh", "cobr", "charg", "consum",
    "crec", "grow", "aument", "increas", "reduc", "descuent", "discount", "rebaj", "precio",
    "price", "tamano", "size", "capacidad", "capacit", "rango", "range", "valor", "value",
    "numero", "number", "cantidad", "amount", "total", "reint", "retr", "repit", "repeat",
)
_QUANTITY_BEFORE_WORDS = frozenset({
    "cost", "costs", "sube", "suben", "subir", "baja", "bajan", "bajar", "pesa", "pesan",
    "max", "min", "de", "of", "upto",
})
# English "may" is a month only when it is clearly not the modal verb:
# followed by nothing, punctuation, or one of these words.
_MAY_FOLLOW = frozenset({
    "and", "or", "to", "until", "till", "through", "at", "in", "on", "with", "for", "when",
    "but", "then", "onward", "onwards", "while", "because", "so", "she", "he", "they", "we",
    "i", "you", "it", "her", "his", "their", "our", "my", "the", "this", "last", "next",
})

# State markers never invent a date on their own. "antes" and "used to"
# have ordinary non-state senses ("antes de hacer push", "cuanto antes",
# "is used to parse") that `_find_state` filters out.
_PAST_RE = re.compile(
    r"\b(?:ya\s+no|no\s+longer|used\s+to|antes|soli(?:a|as|amos|ais|an))\b", re.IGNORECASE
)
_CURRENT_RE = re.compile(r"\b(?:ahora|now|currently|actualmente)\b", re.IGNORECASE)
_FUTURE_RE = re.compile(
    r"\b(?:en\s+el\s+futuro|proximamente|in\s+the\s+future|soon)\b", re.IGNORECASE
)
_ANTES_NOT_STATE_AFTER = re.compile(r"\s+(?:de|del|que|posible)\b")
_ANTES_NOT_STATE_BEFORE = frozenset({
    "cuanto", "lo", "poco", "justo", "mucho", "bastante", "rato", "momentos", "dias", "horas",
    "minutos", "semanas", "meses", "anos", "segundos",
})
_USED_TO_PASSIVE_BEFORE = frozenset({
    "is", "are", "was", "were", "be", "been", "being", "get", "gets", "got", "getting",
    "gotten", "am", "become", "becomes", "became", "not",
})

# Guards around every numeric date shape: a date glued to a word, a path,
# a quote, an operator or a currency sign is part of something else
# ("backup-2024-03-10.tar", "'2024-01-01'", "1920x1080", "2000/min").
_NUM_BEFORE = r"(?<![\w'\"=<>/.:#@$€£+\-])"
_NUM_AFTER = r"(?![\w'\"/:@%$€£+\-]|[.,]\w)"


def _date_alt_pattern(tag: str, *, bare_year: bool = False, bare_month: bool = False,
                       year_needs_context: bool = False) -> str:
    """One alternation of every date SHAPE this module understands, with
    every named group suffixed by ``tag`` so several copies (e.g. the two
    sides of "entre X y Y") can live in the same compiled regex without
    colliding on group names.

    ``bare_year``/``bare_month`` control the two forms that are only safe in
    certain grammatical positions: a bare month name ("marzo" with no year)
    is only ever allowed right after a since/until marker (the marker IS
    the date-ish context); a bare year is allowed standalone only when
    ``year_needs_context`` also requires an immediately preceding "en"/
    "in"/"on" — this is what keeps "2024" from being read out of an
    unrelated sentence containing a stray four-digit number. Even then a
    bare year is only a CANDIDATE: `_bare_number_is_year` still rejects
    quantities ("hasta 2048 tokens").
    """
    parts = [
        rf"{_NUM_BEFORE}(?P<iso_{tag}>\d{{4}}-\d{{2}}-\d{{2}}){_NUM_AFTER}",
        rf"{_NUM_BEFORE}(?P<sfd_{tag}>\d{{1,2}})/(?P<sfm_{tag}>\d{{1,2}})/(?P<sfy_{tag}>\d{{4}}){_NUM_AFTER}",
        rf"{_NUM_BEFORE}(?P<smm_{tag}>\d{{1,2}})/(?P<smy_{tag}>\d{{4}}){_NUM_AFTER}",
        rf"(?P<mym_{tag}>{_MONTH_ALT})(?:\s+(?:de|of))?\s+(?P<myy_{tag}>\d{{4}}){_NUM_AFTER}",
        rf"(?P<rly_{tag}>el\s+ano\s+pasado|last\s+year)\b",
        rf"(?P<rty_{tag}>este\s+ano|this\s+year)\b",
        rf"(?P<rtm_{tag}>este\s+mes|this\s+month)\b",
        rf"(?P<rlm_{tag}>el\s+mes\s+pasado|last\s+month)\b",
    ]
    if bare_year:
        if year_needs_context:
            parts.append(rf"(?:en|in|on)\s+(?P<by_{tag}>\d{{4}}){_NUM_AFTER}")
        else:
            parts.append(rf"(?P<by_{tag}>\d{{4}}){_NUM_AFTER}")
    if bare_month:
        parts.append(rf"(?P<mo_{tag}>{_MONTH_ALT})\b")
    return "(?:" + "|".join(parts) + ")"


# Argument of a since/until marker: the marker word is already the context,
# so a bare month or a bare year need no extra prefix here.
_SINCE_RE = re.compile(
    rf"\b(?P<marker>{_SINCE_WORDS})\s+" + _date_alt_pattern("x", bare_year=True, bare_month=True),
    re.IGNORECASE,
)
_UNTIL_RE = re.compile(
    rf"\b(?P<marker>{_UNTIL_WORDS})\s+" + _date_alt_pattern("x", bare_year=True, bare_month=True),
    re.IGNORECASE,
)
_BETWEEN_RE = re.compile(
    rf"\b(?P<marker>{_BETWEEN_WORDS})\s+"
    + _date_alt_pattern("a", bare_year=True, bare_month=True)
    + r"\s+(?P<conn>y|and)\s+"
    + _date_alt_pattern("b", bare_year=True, bare_month=True),
    re.IGNORECASE,
)
_FROM_TO_RE = re.compile(
    rf"\b(?P<marker>{_RANGE_FROM_WORDS})\s+"
    + _date_alt_pattern("a", bare_year=True)
    + rf"\s+(?P<conn>{_RANGE_TO_WORDS})\s+"
    + _date_alt_pattern("b", bare_year=True),
    re.IGNORECASE,
)
# Standalone mention, no directional marker at all ("marzo de 2025", "en
# 2024"): month+year and the explicit date shapes stand on their own; a
# bare month WITHOUT a year never matches here (that is exactly the "Marzo"
# person's-name case the module must stay conservative about), and a bare
# year is only recognised right after "en"/"in"/"on".
_STANDALONE_RE = re.compile(
    r"\b" + _date_alt_pattern("s", bare_year=True, bare_month=False, year_needs_context=True),
    re.IGNORECASE,
)

# Spans that are code, links, paths, CLI flags or literals: a date inside
# one of them belongs to that artefact, never to the sentence's validity.
_MASK_RES: Tuple["re.Pattern[str]", ...] = (
    re.compile(r"```.*?(?:```|\Z)", re.DOTALL),
    re.compile(r"`[^`\n]*`"),
    re.compile(r"\b(?:[a-z][a-z0-9+.\-]*://|www\.)\S+"),
    re.compile(r"(?<!\S)\S+@\S+"),
    re.compile(r"(?<!\S)(?=\S*[/\\])(?=\S*[a-z])\S+"),
    re.compile(r"(?<![\w\-])--?[a-z][\w\-]*(?:=\S*|\s+\S+)?"),
    re.compile(r"(?<!\S)\S*=\S*"),
    re.compile(r"(?<!\w)['\"][^'\"\s]+['\"]"),
)
_MASK_CHAR = "\x00"


def _fold_for_match(text: str) -> str:
    """Lowercase, accent-stripped, SAME LENGTH as ``text`` (so a regex match
    span on this copy slices the same characters out of the original)."""
    out = []
    for ch in text:
        decomposed = unicodedata.normalize("NFKD", ch)
        base = "".join(c for c in decomposed if not unicodedata.combining(c)).lower()
        out.append(base if len(base) == 1 else ch.lower()[:1] or ch)
    return "".join(out)


def _mask(norm: str) -> str:
    """Blank out code/links/paths/flags/literals (same length)."""
    for pattern in _MASK_RES:
        norm = pattern.sub(lambda m: _MASK_CHAR * (m.end() - m.start()), norm)
    return norm


# ---------------------------------------------------------------------------
# Period arithmetic
# ---------------------------------------------------------------------------


def _day_period(year: int, month: int, day: int) -> Tuple[datetime, datetime]:
    start = datetime(year, month, day, tzinfo=timezone.utc)
    return start, start.replace(hour=23, minute=59, second=59)


def _month_period(year: int, month: int) -> Tuple[datetime, datetime]:
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    last_day = calendar.monthrange(year, month)[1]
    return start, datetime(year, month, last_day, 23, 59, 59, tzinfo=timezone.utc)


def _year_period(year: int) -> Tuple[datetime, datetime]:
    return (datetime(year, 1, 1, tzinfo=timezone.utc),
            datetime(year, 12, 31, 23, 59, 59, tzinfo=timezone.utc))


def _year_ok(year: int, now: datetime) -> bool:
    """A plausible calendar year for a personal/technical memory. Outside
    this window a four-digit number is a quantity (1024, 4096, 65535)."""
    return 1900 <= year <= now.year + 50


def _roll_month(month: int, now: datetime, direction: str) -> Tuple[datetime, datetime]:
    """A bare month name resolved relative to ``now``: "since"/"desde" means
    its most recent occurrence (this month included), "until"/"hasta" its
    next one (this month included). So "hasta marzo" said in October is next
    March — never an already-expired window — and "desde mayo" said in
    February is last May — never a window that starts in the future."""
    if direction == "past":
        year = now.year if month <= now.month else now.year - 1
    else:
        year = now.year if month >= now.month else now.year + 1
    return _month_period(year, month)


def _period_from_values(*, iso=None, sfd=None, sfm=None, sfy=None, smm=None, smy=None,
                        mym=None, myy=None, rly=None, rty=None, rtm=None, rlm=None,
                        by=None, mo=None, now: datetime,
                        roll: str = "past") -> Optional[Tuple[datetime, datetime]]:
    """The (start, end) instants covered by whichever single date-shape
    matched — exactly one of these arguments is ever non-empty per call.
    Every explicit year must pass `_year_ok`."""
    try:
        if iso:
            y, m, d = (int(x) for x in iso.split("-"))
            return _day_period(y, m, d) if _year_ok(y, now) else None
        if sfy and sfm and sfd:
            return _day_period(int(sfy), int(sfm), int(sfd)) if _year_ok(int(sfy), now) else None
        if smy and smm:
            return _month_period(int(smy), int(smm)) if _year_ok(int(smy), now) else None
        if myy and mym:
            month = _MONTHS.get(mym.lower())
            if not month or not _year_ok(int(myy), now):
                return None
            return _month_period(int(myy), month)
        if rly:
            return _year_period(now.year - 1)
        if rty:
            return _year_period(now.year)
        if rtm:
            return _month_period(now.year, now.month)
        if rlm:
            year, month = now.year, now.month - 1
            if month == 0:
                month, year = 12, year - 1
            return _month_period(year, month)
        if by:
            return _year_period(int(by)) if _year_ok(int(by), now) else None
        if mo:
            month = _MONTHS.get(mo.lower())
            return _roll_month(month, now, roll) if month else None
    except ValueError:
        return None
    return None


_GROUP_NAMES = ("iso", "sfd", "sfm", "sfy", "smm", "smy", "mym", "myy",
                "rly", "rty", "rtm", "rlm", "by", "mo")


def _extract_period(groups: Dict[str, Optional[str]], tag: str, now: datetime,
                    roll: str = "past") -> Optional[Tuple[datetime, datetime]]:
    return _period_from_values(now=now, roll=roll,
                               **{name: groups.get(f"{name}_{tag}") for name in _GROUP_NAMES})


def _to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# "Is this really a date?" checks
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[^\W\d_]+")
_NEXT_TOKEN_RE = re.compile(r"\s*(?:(?P<word>[^\W\d_]+)|(?P<digit>\d)|(?P<sym>[^\s\w]))")
_BARE_MONTH_RANGE_TAIL_RE = re.compile(
    rf"\s+(?:-|\u2013|to|a|hasta|until|till|y|and)\s+(?:{_MONTH_ALT})\b(?!\s+(?:de\s+|of\s+)?\d{{4}})"
)
_RANGE_PARTNER_RE = re.compile(
    r"\s*(?:-|–|to|a|hasta|until|till|y|and)\s+(?P<num>\d+)(?![\d])"
)


def _quantity_before(norm: str, marker_start: int) -> bool:
    """True when the words right before the marker make "hasta N" read as
    "up to N" ("acepta hasta", "un maximo de hasta")."""
    words = _WORD_RE.findall(norm[max(0, marker_start - 80):marker_start])[-3:]
    if not words:
        return False
    if words[-1] in ("de", "of", "up"):
        return True
    for word in words:
        if word in _QUANTITY_BEFORE_WORDS and word not in ("de", "of"):
            return True
        if len(word) >= 4 and any(word.startswith(stem) for stem in _QUANTITY_BEFORE_STEMS):
            return True
    return False


def _follow_is_quantity(norm: str, raw: str, pos: int) -> bool:
    """True when what follows a four-digit number makes it a count, size or
    price: a unit word, a plural noun, a rate word, a symbol or digit."""
    match = _NEXT_TOKEN_RE.match(norm, pos)
    if not match:
        return False
    if match.group("digit"):
        return True
    sym = match.group("sym")
    if sym is not None:
        return sym in "%€$£/*×+=<>#~^" or sym == _MASK_CHAR
    word = match.group("word")
    if word in _UNIT_WORDS or word in _QUANTITY_FOLLOW:
        return True
    original = raw[match.start("word"):match.end("word")]
    if (original == original.lower() and len(word) >= 4 and word.endswith("s")
            and word not in _S_FUNCTION_WORDS):
        return True  # a lowercase plural right after the number: a count
    return False


def _bare_number_is_year(norm: str, raw: str, marker_start: int, start: int, end: int,
                         now: datetime, *, check_partner: bool = True) -> bool:
    """A bare four-digit number after a marker is a year only if it is a
    plausible year, nothing before the marker reads "up to", nothing after
    it reads as a unit, and a range partner ("from 2000 to 4000") is itself
    a plausible year."""
    try:
        year = int(norm[start:end])
    except ValueError:
        return False
    if not _year_ok(year, now):
        return False
    if _quantity_before(norm, marker_start):
        return False
    partner = _RANGE_PARTNER_RE.match(norm, end)
    if partner and check_partner:
        num = partner.group("num")
        if len(num) != 4:
            return False
        return _bare_number_is_year(norm, raw, marker_start, partner.start("num"),
                                    partner.end("num"), now, check_partner=False)
    return not _follow_is_quantity(norm, raw, end)


def _bare_month_ok(norm: str, raw: str, start: int, end: int) -> bool:
    """A bare month name after a marker: a Spanish month written with a
    capital initial is a NAME ("desde Marzo" — Spanish never capitalises
    months; an all-caps line is fine), and English "may" is the modal verb
    unless what follows is clearly not a verb."""
    word = norm[start:end]
    original = raw[start:end]
    if word in _SPANISH_MONTHS and original[:1].isupper() and not original.isupper():
        return False
    if word == "may":
        match = _NEXT_TOKEN_RE.match(norm, end)
        if not match or match.group("sym") is not None:
            return True
        nxt = match.group("word")
        return bool(nxt) and nxt in _MAY_FOLLOW
    return True


def _year_group_follow_ok(norm: str, raw: str, match: "re.Match[str]", tag: str) -> bool:
    """For explicit shapes with a year at the end ("marzo 2025", "03/2025"),
    reject a trailing unit ("mayo 2000 usuarios")."""
    for name in ("myy", "smy", "sfy", "iso"):
        if match.group(f"{name}_{tag}"):
            return not _follow_is_quantity(norm, raw, match.end(f"{name}_{tag}"))
    return True


def _side_ok(norm: str, raw: str, match: "re.Match[str]", tag: str, now: datetime,
             *, check_partner: bool = True) -> bool:
    """Is the date argument ``tag`` of this marker match really a date?"""
    if match.group(f"by_{tag}"):
        return _bare_number_is_year(norm, raw, match.start(), match.start(f"by_{tag}"),
                                    match.end(f"by_{tag}"), now, check_partner=check_partner)
    if (f"mo_{tag}" in match.re.groupindex) and match.group(f"mo_{tag}"):
        return _bare_month_ok(norm, raw, match.start(f"mo_{tag}"), match.end(f"mo_{tag}"))
    return _year_group_follow_ok(norm, raw, match, tag)


def _is_bare_month(match: "re.Match[str]", tag: str) -> bool:
    return f"mo_{tag}" in match.re.groupindex and bool(match.group(f"mo_{tag}"))


def _month_of(match: "re.Match[str]", tag: str) -> int:
    return _MONTHS.get(str(match.group(f"mo_{tag}")).lower(), 0)


def _anchor_after(month: int, start: datetime) -> Tuple[datetime, datetime]:
    """First occurrence of ``month`` at or after ``start``."""
    year = start.year if month >= start.month else start.year + 1
    return _month_period(year, month)


def _anchor_before(month: int, end: datetime) -> Tuple[datetime, datetime]:
    """Last occurrence of ``month`` at or before ``end``."""
    year = end.year if month <= end.month else end.year - 1
    return _month_period(year, month)


def _range_window(match: "re.Match[str]", norm: str, raw: str,
                  now: datetime) -> Optional[Tuple[datetime, datetime]]:
    """Window of a two-sided range match ("entre X y Y", "from X to Y").
    A bare month on one side is anchored to the explicit other side; two
    bare months give nothing (a past episode or a plan — unknowable)."""
    if not (_side_ok(norm, raw, match, "a", now, check_partner=False)
            and _side_ok(norm, raw, match, "b", now, check_partner=False)):
        return None
    a_bare, b_bare = _is_bare_month(match, "a"), _is_bare_month(match, "b")
    if a_bare and b_bare:
        return None
    gd = match.groupdict()
    if b_bare:
        period_a = _extract_period(gd, "a", now)
        period_b = _anchor_after(_month_of(match, "b"), period_a[0]) if period_a else None
    elif a_bare:
        period_b = _extract_period(gd, "b", now)
        period_a = _anchor_before(_month_of(match, "a"), period_b[1]) if period_b else None
    else:
        period_a = _extract_period(gd, "a", now)
        period_b = _extract_period(gd, "b", now)
    if not period_a or not period_b or period_b[1] < period_a[0]:
        return None
    return period_a[0], period_b[1]


def _first_valid(pattern: "re.Pattern[str]", norm: str, raw: str, now: datetime,
                 ) -> Optional["re.Match[str]"]:
    for match in pattern.finditer(norm):
        if _side_ok(norm, raw, match, "x", now):
            return match
    return None


def _find_state(norm: str, raw: str) -> Optional[Tuple[str, int, int]]:
    for match in _PAST_RE.finditer(norm):
        text = match.group(0)
        before = _WORD_RE.findall(norm[max(0, match.start() - 40):match.start()])
        prev = before[-1] if before else ""
        if text == "antes":
            if _ANTES_NOT_STATE_AFTER.match(norm, match.end()) or prev in _ANTES_NOT_STATE_BEFORE:
                continue
        elif text.startswith("used"):
            tail = norm[max(0, match.start() - 12):match.start()].rstrip()
            if prev in _USED_TO_PASSIVE_BEFORE or re.search(r"['’](?:s|m|re)$", tail):
                continue
        return "past", match.start(), match.end()
    for state, pattern in (("current", _CURRENT_RE), ("future", _FUTURE_RE)):
        match = pattern.search(norm)
        if match:
            return state, match.start(), match.end()
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_temporal(text: Any, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Read a validity window and/or a state out of one piece of text.

    Returns ``{"valid_from": iso|None, "valid_until": iso|None,
    "state": "past"|"current"|"future"|None, "markers": [str]}``. Every
    field is independently optional; a text with nothing date-ish AND no
    state word in it comes back with every field ``None``/empty — this
    function never invents a date.

    Conservative by design: a wrong ``valid_until`` hides a memory from
    recall, a missed one costs nothing. So quantities ("hasta 2048 tokens",
    "from 1024 to 4096"), dates inside code/links/paths/flags, capitalised
    Spanish month names (names, not dates) and two bare months with no year
    all yield no window, and a window whose end precedes its start is
    dropped whole.
    """
    now = now if (now is not None and now.tzinfo) else (
        now.replace(tzinfo=timezone.utc) if now is not None else datetime.now(timezone.utc)
    )
    raw = str(text or "")
    if not raw.strip():
        return {"valid_from": None, "valid_until": None, "state": None, "markers": []}
    norm = _fold_for_match(raw)
    if len(norm) != len(raw):  # defensive: spans must slice the original
        norm = raw.lower()
    masked = _mask(norm)

    markers: List[str] = []
    valid_from: Optional[datetime] = None
    valid_until: Optional[datetime] = None

    for pattern in (_BETWEEN_RE, _FROM_TO_RE):
        for match in pattern.finditer(masked):
            window = _range_window(match, masked, raw, now)
            if window:
                valid_from, valid_until = window
                markers.append(raw[match.start():match.end()])
                break
        if valid_from is not None:
            break

    if valid_from is None and valid_until is None:
        since = _first_valid(_SINCE_RE, masked, raw, now)
        until = _first_valid(_UNTIL_RE, masked, raw, now)
        since_bare = bool(since) and _is_bare_month(since, "x")
        until_bare = bool(until) and _is_bare_month(until, "x")
        if since and until and since_bare and until_bare:
            since = until = None  # "desde marzo hasta junio": episode or plan? unknown
        if since and since_bare and _BARE_MONTH_RANGE_TAIL_RE.match(masked, since.end()):
            since = None  # "from March to June": same two-bare-months case
        since_period = until_period = None
        if since and until and since_bare and not until_bare:
            # "desde marzo hasta junio de 2025": the bare side is anchored
            # to the explicit one, not to `now`.
            until_period = _extract_period(until.groupdict(), "x", now, roll="next")
            if until_period:
                since_period = _anchor_before(_month_of(since, "x"), until_period[1])
        else:
            if since:
                since_period = _extract_period(since.groupdict(), "x", now, roll="past")
            if until:
                if until_bare and since_period:
                    until_period = _anchor_after(_month_of(until, "x"), since_period[0])
                else:
                    until_period = _extract_period(until.groupdict(), "x", now, roll="next")
        if since_period:
            valid_from = since_period[0]
            markers.append(raw[since.start():since.end()])
        if until_period:
            valid_until = until_period[1]
            markers.append(raw[until.start():until.end()])
        if valid_from is not None and valid_until is not None and valid_until < valid_from:
            valid_from = valid_until = None
            markers = []

    if valid_from is None and valid_until is None:
        for match in _STANDALONE_RE.finditer(masked):
            if match.group("by_s"):
                # the "en"/"in"/"on" before the year is the marker here
                ok = _bare_number_is_year(masked, raw, match.start(), match.start("by_s"),
                                          match.end("by_s"), now)
            else:
                ok = _year_group_follow_ok(masked, raw, match, "s")
            if not ok:
                continue
            period = _extract_period(match.groupdict(), "s", now)
            if period and period[0] > now:
                # "La demo es el 2026-10-01" said before that day: with no
                # "desde"/"since" the date is WHEN something happens, not
                # when the memory starts being true — reading it as the
                # start would hide the memory exactly while it is useful.
                continue
            if period:
                valid_from = period[0]
                markers.append(raw[match.start():match.end()])
                break

    state: Optional[str] = None
    found = _find_state(masked, raw)
    if found:
        state, start, end = found
        markers.append(raw[start:end])

    return {
        "valid_from": _to_iso(valid_from) if valid_from else None,
        "valid_until": _to_iso(valid_until) if valid_until else None,
        "state": state,
        "markers": markers,
    }


def supersede_order(new_start: Optional[datetime], old_start: Optional[datetime], *,
                    new_explicit: bool, old_explicit: bool) -> Optional[str]:
    """Which side of a one-value-at-a-time change-over ("works at X" vs
    "works at Y") is the OUTDATED one: ``"old"``, ``"new"`` or ``None``
    when the order cannot be known.

    ``*_start`` is each side's ``valid_from`` (or, for an undated item, when
    it was stored); ``*_explicit`` says whether that start came from a real
    date. Chronology — not arrival order — decides: a historical fact added
    after the current one ("since 2019" stored after "since 2024") is the
    outdated side. Two dated starts, or two undated ones, are comparable; a
    dated start EARLIER than an undated one is not (the undated fact may
    have been true long before it was stored), and two equal dated starts
    are a genuine contradiction for a human to look at. Pure function.
    """
    if new_start is None or old_start is None:
        return None
    if new_start > old_start:
        return "old"
    if new_start < old_start:
        return "new" if new_explicit == old_explicit else None
    return "old" if not new_explicit and not old_explicit else None


def timeline(owner: Any, *, query: str = "", limit: int = 200) -> List[Dict[str, Any]]:
    """A cross-entity timeline for `GET /api/brain/timeline` when no single
    entity is named: memory items (created/valid_from/valid_until/corrected)
    plus every relation's validity window, newest first.

    Lot B's own note (see B_wiring.md) is that `memory_engine` has no
    store-wide timeline of its own — reconstructing one from tombstoned rows
    would need text `correct()` already discards. This composes from what
    IS still readable: `memory_engine.list_items` (each item carries its own
    `created_at`/`valid_from`/`valid_until`, and a `provenance.corrected_from`
    when it replaced an older item) and `entities.list_relations` (each
    relation's own window). `entities.profile(id)["timeline"]` remains the
    per-entity view this composes nothing from — it is already complete.

    Never raises: one store being unavailable costs its half of the
    timeline, not the whole answer.
    """
    owner = str(owner or "")
    events: List[Dict[str, Any]] = []
    if not owner:
        return events

    from .db import fold

    q_fold = fold(query) if query else ""

    try:
        from src import memory_engine as engine

        for item in engine.list_items(owner=owner, limit=2000):
            text = str(item.get("text") or "")
            if q_fold and q_fold not in fold(text):
                continue
            item_id = str(item.get("id") or "")
            source_ref = f"mem:{item_id}" if item_id else ""
            if item.get("created_at"):
                events.append({"at": item["created_at"], "kind": "created",
                               "item_id": item_id, "text": text, "source_ref": source_ref})
            if item.get("valid_from"):
                events.append({"at": item["valid_from"], "kind": "valid_from",
                               "item_id": item_id, "text": text, "source_ref": source_ref})
            if item.get("valid_until"):
                events.append({"at": item["valid_until"], "kind": "valid_until",
                               "item_id": item_id, "text": text, "source_ref": source_ref})
            provenance = item.get("provenance") or {}
            if isinstance(provenance, dict) and provenance.get("corrected_from"):
                at = item.get("created_at") or item.get("updated_at") or ""
                if at:
                    events.append({"at": at, "kind": "corrected", "item_id": item_id,
                                   "text": text, "source_ref": source_ref})
    except Exception:  # noqa: BLE001 - half a timeline beats none
        logger.debug("brain.temporal: timeline could not read memory_engine", exc_info=True)

    try:
        from .entities import list_relations

        for rel in list_relations(owner, include_closed=True):
            label = f"{rel.get('rel', '')} {rel.get('dst_value') or rel.get('dst') or ''}".strip()
            if q_fold and q_fold not in fold(label):
                continue
            rel_id = str(rel.get("id") or "")
            source_ref = f"rel:{rel_id}" if rel_id else ""
            if rel.get("valid_from"):
                events.append({"at": rel["valid_from"], "kind": "valid_from",
                               "item_id": rel_id, "text": label, "source_ref": source_ref})
            if rel.get("valid_until"):
                events.append({"at": rel["valid_until"], "kind": "valid_until",
                               "item_id": rel_id, "text": label, "source_ref": source_ref})
    except Exception:  # noqa: BLE001
        logger.debug("brain.temporal: timeline could not read entities", exc_info=True)

    events = [e for e in events if e.get("at")]
    events.sort(key=lambda e: e["at"], reverse=True)
    return events[: max(1, int(limit or 200))]


__all__ = ["parse_temporal", "supersede_order", "timeline"]
