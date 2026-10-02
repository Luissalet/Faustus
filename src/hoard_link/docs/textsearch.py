"""Search text helpers: safe FTS5 queries, BM25 re-scoring that survives common words, highlights.

Standard library only. The Node twin is ``js/hoard-commons/docs.js`` (``ftsQuery``, ``ftsLadder``, ``tokens``,
``stem``, ``highlight``); both are checked against ``tests/vectors/docs_fts.json``.

* :func:`fts_query` / :func:`fts_ladder` — turn what a person typed into a MATCH expression that **cannot** be a
  syntax error or an injection: every term is a double-quoted string, so ``AND``, ``OR``, ``NEAR``, ``-``,
  ``*``, ``:``, ``^``, parentheses and stray quotes inside the text are just words (or are dropped).
* :func:`ensure_fts`, :func:`fts_available` — one probe, one ``CREATE VIRTUAL TABLE`` helper.
* :func:`bm25_rescore` — BM25 with a *smoothed* IDF. FTS5's ``bm25()`` clamps a non-positive IDF to ~1e-6, so a
  word on more than half the rows (a window title that is on screen for two hours) gets no weight at all;
  re-scoring the candidates here keeps every rank readable.
* :func:`highlight` / :func:`snippet` — excerpts around the first match; offsets are right because
  :func:`hoard_link.text.fold` keeps one character per character.

Replaces: Borges ``search.py`` (``fold``, ``STOPWORDS``, ``stem``, ``fts_query``, ``highlight``), Argus
``queries.fts_query`` and ``search.py`` (IDF), Funes ``audio_memory.store.fts_query``, Echo ``_fts_query``,
Kafka ``store.fts_query``, Hypatia/Vulcan/Vitruvius/Scheherazade ``_fts_query`` and the Faustus ``bm25_scores``
copies.
"""

from __future__ import annotations

import math
import re
import sqlite3
from typing import Any, Iterable, Mapping, Optional, Sequence, Union

from ..text import fold

__all__ = [
    "fold", "STOPWORDS", "STOPWORDS_FOLDED", "PREFIX_MIN_CHARS", "is_stopword", "tokens", "content_words",
    "stem", "query_terms", "fts_query", "fts_ladder", "fts_available", "ensure_fts", "bm25_rescore",
    "highlight", "snippet",
]

PREFIX_MIN_CHARS = 3        # terms shorter than this match whole words only (no prefix expansion)
DEFAULT_TOKENIZER = "unicode61 remove_diacritics 2"

# Spanish + English question and glue words (Borges' list). Matching is done on the folded form, so an
# entry written with an accent ("más") also removes "mas".
STOPWORDS = frozenset(
    "de la el los las un una unos unas y o u e en del al a que con por para se su sus lo le les es son no ni como más muy ya "
    "mi mis tu tus me te sobre acerca dónde donde qué cuál cuáles cómo cuándo quién hay he ha leí escribí dice dijo del este esta esto "
    "the of an and or in on to is are was were for with that this about where what which how when who did i my".split()
)
STOPWORDS_FOLDED = frozenset(fold(w) for w in STOPWORDS)

_WORD = re.compile(r"[^\W_]+", re.UNICODE)


def is_stopword(word: str) -> bool:
    return fold(word) in STOPWORDS_FOLDED


def tokens(text: Any, *, stop: Optional[Iterable[str]] = None) -> list[str]:
    """Folded words of ``text`` (letters and digits, accents removed, one character per character). ``stop``
    is an optional collection of **folded** words to drop (pass :data:`STOPWORDS_FOLDED`); the default keeps
    everything, which is what BM25 scoring wants."""
    words = _WORD.findall(fold(text))
    if stop:
        drop = stop if isinstance(stop, (set, frozenset)) else set(stop)
        return [w for w in words if w not in drop]
    return words


def stem(word: str) -> str:
    """Very light Spanish-friendly stem used only as an FTS prefix: hojas -> hoja, árboles -> árbol,
    ciudades -> ciudad."""
    w = word.lower()
    if len(w) > 5 and w.endswith("es"):
        return w[:-2]
    if len(w) > 4 and w.endswith("s"):
        return w[:-1]
    return w


def content_words(q: Any) -> list[str]:
    """The words of a question without stopwords and one-letter words; when nothing is left, all the words."""
    words = _WORD.findall("" if q is None else str(q))
    kept = [w for w in words if not is_stopword(w) and len(w) > 1]
    return kept or words


def _unique_terms(q: Any, max_terms: int) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for w in content_words(q):
        lw = w.lower()
        if lw in seen:
            continue
        seen.add(lw)
        out.append(lw)
        if len(out) >= max_terms:
            break
    return out


def _quote(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def fts_query(q: Any, *, mode: str = "and", max_terms: int = 12) -> str:
    """A MATCH expression for ``q``, or ``""`` when there is nothing to search for.

    * ``mode="and"`` — every content word, quoted, must be present as a whole word (``"hojas" AND "otoño"``).
    * ``mode="prefix"`` — as ``and``, but words of 3+ letters match as prefixes of their stem (``"hoja"*``).
    * ``mode="or"`` — any of the words, prefix-expanded (the widest net).

    Stopwords (Spanish and English) are dropped unless the query is only stopwords. At most ``max_terms``
    distinct words are used. Everything is quoted, so the result is always valid FTS5.
    """
    if mode not in ("and", "or", "prefix"):
        raise ValueError(f"mode must be 'and', 'or' or 'prefix', not {mode!r}")
    terms = _unique_terms(q, max(1, int(max_terms)))
    if not terms:
        return ""
    if mode == "and":
        return " AND ".join(_quote(t) for t in terms)
    parts = [_quote(stem(t)) + "*" if len(t) >= PREFIX_MIN_CHARS else _quote(t) for t in terms]
    return (" OR " if mode == "or" else " AND ").join(parts)


def fts_ladder(q: Any, *, max_terms: int = 12) -> list[str]:
    """Queries to try in order until one returns rows: ``[AND, AND+prefix, OR]``, without empties or repeats."""
    ladder: list[str] = []
    for mode in ("and", "prefix", "or"):
        expr = fts_query(q, mode=mode, max_terms=max_terms)
        if expr and expr not in ladder:
            ladder.append(expr)
    return ladder


def query_terms(q: Any, *, max_terms: int = 12) -> list[str]:
    """Folded stems of the content words, for :func:`highlight` and :func:`bm25_rescore` (matches at word starts)."""
    return [fold(stem(t)) if len(t) >= PREFIX_MIN_CHARS else fold(t) for t in _unique_terms(q, max_terms)]


# ---- SQLite ----------------------------------------------------------------------------------------

def fts_available(conn: sqlite3.Connection) -> bool:
    """True when this SQLite build has FTS5 (a throw-away temp table is created and dropped)."""
    try:
        conn.execute("CREATE VIRTUAL TABLE temp.__hoard_fts_probe USING fts5(x)")
    except sqlite3.Error:
        return False
    try:
        conn.execute("DROP TABLE temp.__hoard_fts_probe")
    except sqlite3.Error:
        pass
    return True


_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def ensure_fts(conn: sqlite3.Connection, table: str, columns: Sequence[str], *,
               tokenize: str = DEFAULT_TOKENIZER, content: Optional[str] = None) -> bool:
    """``CREATE VIRTUAL TABLE IF NOT EXISTS table USING fts5(columns…)``. Returns ``False`` (and creates nothing)
    when FTS5 is not compiled in, so the caller can fall back to ``LIKE``; other SQLite errors propagate.
    ``content`` makes it an external-content table (``content='docs'``). Names are checked, not quoted."""
    for name in (table, *columns, *([content] if content else [])):
        if not _IDENT.match(name or ""):
            raise ValueError(f"not a plain SQL identifier: {name!r}")
    if not columns:
        raise ValueError("at least one column is needed")
    if not fts_available(conn):
        return False
    options = [f"tokenize = '{tokenize.replace(chr(39), chr(39) * 2)}'"]
    if content:
        options.append(f"content = '{content}'")
    conn.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS {table} USING fts5({', '.join(columns)}, {', '.join(options)})")
    return True


# ---- BM25 ------------------------------------------------------------------------------------------

Term = Union[str, tuple]


def _norm_terms(terms: Iterable[Term]) -> list[tuple[str, bool]]:
    out: list[tuple[str, bool]] = []
    seen: set[tuple[str, bool]] = set()
    for t in terms:
        if isinstance(t, (tuple, list)):
            word, prefix = str(t[0]), bool(t[1]) if len(t) > 1 else False
        else:
            word, prefix = str(t), False
            if word.endswith("*"):
                word, prefix = word[:-1], True
        for piece in tokens(word):
            key = (piece, prefix)
            if key not in seen:
                seen.add(key)
                out.append(key)
    return out


def _row_columns(row: Any, weights: Optional[Mapping[str, float]]) -> dict[str, list[str]]:
    if isinstance(row, str):
        return {"text": tokens(row)}
    if weights:
        return {c: tokens(row.get(c, "") if hasattr(row, "get") else getattr(row, c, "")) for c in weights}
    return {c: tokens(v) for c, v in row.items() if isinstance(v, str)}


def bm25_rescore(rows: Sequence[Any], query_terms: Iterable[Term], *, k1: float = 1.2, b: float = 0.75,
                 weights: Optional[Mapping[str, float]] = None, idf_floor: float = 0.1,
                 idfs: Optional[Mapping[str, float]] = None, length_column: Optional[str] = None) -> list[tuple[Any, float]]:
    """Re-score candidate rows with BM25 and a smoothed IDF. Returns ``[(row, score), …]`` best first (higher is
    better; ties keep the input order).

    ``rows`` are mappings ``{column: text}`` (or plain strings, one ``text`` column). ``query_terms`` are
    folded words, ``"stem*"`` prefix words or ``(word, is_prefix)`` pairs (what :func:`query_terms` returns
    works). ``weights`` is ``{column: weight}`` (Argus uses ``{"window_title": 3, "app": 2, "text": 1}``);
    without it every text column counts 1. Only ``length_column`` (default ``"text"``, else the last column)
    is length-normalised. IDF is ``max(idf_floor, ln(1 + (N - n + .5) / (n + .5)))`` over ``rows`` unless
    ``idfs`` supplies it (Argus passes whole-database counts).
    """
    terms = _norm_terms(query_terms)
    if not rows:
        return []
    if not terms:
        return [(r, 0.0) for r in rows]
    cols = [_row_columns(r, weights) for r in rows]
    names = list(weights) if weights else list(dict.fromkeys(c for d in cols for c in d))
    wmap = {c: float(weights[c]) if weights else 1.0 for c in names}
    lcol = length_column if length_column in names else ("text" if "text" in names else (names[-1] if names else None))
    avg = (sum(len(d.get(lcol, [])) for d in cols) / len(cols)) if lcol else 0.0

    def tf(words: list[str], term: str, prefix: bool) -> int:
        return sum(1 for w in words if (w.startswith(term) if prefix else w == term))

    total = len(rows)
    idf: dict[str, float] = {}
    for term, prefix in terms:
        if idfs is not None and term in idfs:
            idf[term] = float(idfs[term])
            continue
        hits = sum(1 for d in cols if any(tf(d.get(c, []), term, prefix) for c in names))
        idf[term] = max(idf_floor, math.log(1.0 + (total - hits + 0.5) / (hits + 0.5)))

    scored: list[tuple[int, Any, float]] = []
    for index, (row, d) in enumerate(zip(rows, cols)):
        n_len = len(d.get(lcol, [])) if lcol else 0
        norm = k1 * (1 - b + b * (n_len / avg if avg else 1.0))
        score = 0.0
        for term, prefix in terms:
            weighted = 0.0
            for c in names:
                f = tf(d.get(c, []), term, prefix)
                if f:
                    weighted += wmap[c] * f * (k1 + 1) / (f + (norm if c == lcol else k1))
            if weighted:
                score += idf[term] * weighted
        scored.append((index, row, score))
    scored.sort(key=lambda item: (-item[2], item[0]))
    return [(row, score) for _, row, score in scored]


# ---- highlight and snippet -------------------------------------------------------------------------

def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _excerpt(text: str, terms: Iterable[str], window: int) -> tuple[int, int, list[tuple[int, int]]]:
    folded = fold(text)                      # same length as text: offsets map straight back
    positions: list[tuple[int, int]] = []
    for term in terms:
        t = fold(term)
        if not t:
            continue
        for match in re.finditer(re.escape(t), folded):
            start = match.start()
            if start > 0 and folded[start - 1].isalnum():
                continue                     # only word starts
            end = match.end()
            while end < len(folded) and folded[end].isalnum():
                end += 1                     # mark the whole word, not just the stem
            positions.append((start, end))
    positions.sort()
    start = max(0, positions[0][0] - window // 3) if positions else 0
    end = min(len(text), start + window)
    if start > 0:                            # snap to word boundaries
        space = text.rfind(" ", max(0, start - 20), start)
        start = space + 1 if space != -1 else (0 if start < 20 else start)
    if end < len(text):
        space = text.find(" ", end, min(len(text), end + 20))
        end = space if space != -1 else end
    return start, end, positions


def highlight(text: Any, terms: Iterable[str], *, window: int = 160, tag: Optional[str] = "mark", escape: bool = True) -> str:
    """An excerpt of about ``window`` characters centred on the first match, with every match (the whole word)
    wrapped in ``<tag>…</tag>`` and ``…`` where it was cut. ``terms`` are words or stems (use
    :func:`query_terms`); matching ignores case and accents. ``escape`` HTML-escapes the text, so the result is
    safe to put in a page; ``tag=None`` marks nothing."""
    s = "" if text is None else str(text)
    start, end, positions = _excerpt(s, terms, window)
    if tag and not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", tag):
        raise ValueError("tag must be a plain element name")
    esc = _esc if escape else (lambda x: x)
    pieces: list[str] = []
    cursor = start
    for a, z in positions:
        if a < start or z > end or a < cursor or not tag:
            continue
        pieces.append(esc(s[cursor:a]))
        pieces.append(f"<{tag}>{esc(s[a:z])}</{tag}>")
        cursor = z
    pieces.append(esc(s[cursor:end]))
    excerpt = "".join(pieces).replace("\n", " ")
    return ("…" if start > 0 else "") + excerpt + ("…" if end < len(s) else "")


def snippet(text: Any, terms: Iterable[str] = (), *, window: int = 160) -> str:
    """The same excerpt as :func:`highlight` as plain text (no markup, whitespace collapsed)."""
    s = "" if text is None else str(text)
    start, end, _ = _excerpt(s, terms, window)
    body = re.sub(r"\s+", " ", s[start:end]).strip()
    return ("…" if start > 0 else "") + body + ("…" if end < len(s) else "")
