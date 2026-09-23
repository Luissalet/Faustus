"""brain/entities.py — typed entities and time-windowed relations.

The learned memory store (``memory_engine``) and the personal one
(``memory.json``) both hold sentences: "Ada works at Cordera Labs" sits
there as TEXT, indistinguishable from any other fact. This module pulls the
same information into a small graph — an ``Ada`` entity, a ``works_at``
relation to a ``Cordera Labs`` entity — with an explicit validity window on
every relation, so "worked at Cordera Labs until March" and "works at
Bluehaven" are both true statements about Ada, at different times, instead
of a contradiction nobody can resolve.

Nothing here replaces the sentence stores: a relation's ``evidence`` is a
list of the very ``source_ref`` strings (``mem:<id>``, ``pmem:<id>``,
``note:<path>``) the facts came from, so every edge in the graph can always
be traced back to the words that produced it.

Three concepts:

* **Entities** — a name, a type (person/project/organization/place/tool/
  concept/event/other), and a set of aliases folded for matching
  (``fold_name``, ``src.brain.db.fold``). ``upsert_entity`` is alias-aware:
  asking for a name that folds equal to an existing entity or one of its
  aliases returns THAT entity instead of creating a duplicate.
* **Relations** — ``(src, rel, dst_or_dst_value)`` with a validity window.
  A small closed set of relations is FUNCTIONAL (a person has exactly one
  current employer, one current home city): asserting a new value for one
  of these automatically closes the old one's window rather than leaving
  two "current" answers open at once.
* **The owner as an entity** — ``self_entity`` gives every owner a stable
  ``person`` entity aliased "yo"/"me" so "I use X" attaches to a graph node
  the same way "Ada uses X" does.

What may become an entity is decided here too (``valid_entity_name``,
``proper_noun_in_source`` — function words and sentence-opening capitals
are not names), as is the mapping of free relation wording onto the
vocabulary (``canonical_relation``). ``revalidate`` applies both to rows
written before they existed: it hides and retracts, never deletes, and can
be undone.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.brain.db import db, dumps, fold, loads, now_iso, parse_iso, register_schema

logger = logging.getLogger(__name__)

TYPES: Tuple[str, ...] = (
    "person", "project", "organization", "place", "tool", "concept", "event", "other",
)

KNOWN_RELATIONS: Tuple[str, ...] = (
    "works_at", "works_on", "uses", "prefers", "lives_in", "located_in",
    "part_of", "member_of", "knows", "owns", "created", "depends_on",
    "is_a", "related_to", "studied_at",
)

# A person has exactly one CURRENT employer, home and location — asserting a
# new one closes the old one's window. `prefers` is deliberately excluded
# (a preference is fuzzy across object categories: "prefers tabs" and
# "prefers coffee" do not compete) and so is `is_a` (a thing can be an
# instance of more than one category at once).
FUNCTIONAL_RELATIONS = frozenset({"works_at", "lives_in", "located_in"})

# Folded aliases that resolve a mention straight to `self_entity(owner)`
# rather than through the general name/alias match.
SELF_WORDS: Tuple[str, ...] = ("yo", "me", "i")
# How the owner is referred to in third person by memories and by the model
# ("the user prefers…", "el usuario quiere…"): the same node as "yo".
SELF_EXTRA_ALIASES: Tuple[str, ...] = ("user", "the user", "usuario", "el usuario")

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS entities (
        id                  TEXT PRIMARY KEY,
        owner               TEXT NOT NULL DEFAULT '',
        project             TEXT NOT NULL DEFAULT '',
        name                TEXT NOT NULL DEFAULT '',
        fold_name           TEXT NOT NULL DEFAULT '',
        type                TEXT NOT NULL DEFAULT 'other',
        aliases             TEXT NOT NULL DEFAULT '[]',
        summary             TEXT NOT NULL DEFAULT '',
        summary_sources     TEXT NOT NULL DEFAULT '[]',
        summary_locked      INTEGER NOT NULL DEFAULT 0,
        summary_updated_at  TEXT NOT NULL DEFAULT '',
        facts_hash          TEXT NOT NULL DEFAULT '',
        hidden              INTEGER NOT NULL DEFAULT 0,
        merged_into         TEXT NOT NULL DEFAULT '',
        created_at          TEXT NOT NULL DEFAULT '',
        updated_at          TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_entities_owner ON entities(owner, fold_name)",
    """
    CREATE TABLE IF NOT EXISTS relations (
        id            TEXT PRIMARY KEY,
        owner         TEXT NOT NULL DEFAULT '',
        project       TEXT NOT NULL DEFAULT '',
        src           TEXT NOT NULL DEFAULT '',
        rel           TEXT NOT NULL DEFAULT '',
        dst           TEXT NOT NULL DEFAULT '',
        dst_value     TEXT NOT NULL DEFAULT '',
        valid_from    TEXT NOT NULL DEFAULT '',
        valid_until   TEXT NOT NULL DEFAULT '',
        asserted_at   TEXT NOT NULL DEFAULT '',
        evidence      TEXT NOT NULL DEFAULT '[]',
        confidence    REAL NOT NULL DEFAULT 0.6,
        method        TEXT NOT NULL DEFAULT 'rule',
        status        TEXT NOT NULL DEFAULT 'active',
        superseded_by TEXT NOT NULL DEFAULT '',
        created_at    TEXT NOT NULL DEFAULT '',
        updated_at    TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_relations_owner_src ON relations(owner, src)",
    "CREATE INDEX IF NOT EXISTS idx_relations_owner_dst ON relations(owner, dst)",
    """
    CREATE TABLE IF NOT EXISTS mentions (
        owner       TEXT NOT NULL DEFAULT '',
        entity_id   TEXT NOT NULL DEFAULT '',
        source_ref  TEXT NOT NULL DEFAULT '',
        created_at  TEXT NOT NULL DEFAULT '',
        PRIMARY KEY(entity_id, source_ref)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_mentions_source ON mentions(source_ref)",
    # Small per-owner key/value store for one-off passes (the revalidation
    # version marker and what it last changed, so it can be undone).
    """
    CREATE TABLE IF NOT EXISTS brain_meta (
        owner       TEXT NOT NULL DEFAULT '',
        key         TEXT NOT NULL DEFAULT '',
        value       TEXT NOT NULL DEFAULT '',
        updated_at  TEXT NOT NULL DEFAULT '',
        PRIMARY KEY(owner, key)
    )
    """,
)
register_schema("brain_entities", _SCHEMA)


class BrainEntityError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Row <-> dict
# ---------------------------------------------------------------------------


def _row_to_entity(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"], "owner": row["owner"], "project": row["project"],
        "name": row["name"], "fold_name": row["fold_name"], "type": row["type"],
        "aliases": loads(row["aliases"], []), "summary": row["summary"] or "",
        "summary_sources": loads(row["summary_sources"], []),
        "summary_locked": bool(row["summary_locked"]),
        "summary_updated_at": row["summary_updated_at"] or "",
        "facts_hash": row["facts_hash"] or "",
        "hidden": bool(row["hidden"]), "merged_into": row["merged_into"] or "",
        "created_at": row["created_at"] or "", "updated_at": row["updated_at"] or "",
    }


def _row_to_relation(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"], "owner": row["owner"], "project": row["project"],
        "src": row["src"], "rel": row["rel"], "dst": row["dst"] or "",
        "dst_value": row["dst_value"] or "",
        "valid_from": row["valid_from"] or "", "valid_until": row["valid_until"] or "",
        "asserted_at": row["asserted_at"] or "", "evidence": loads(row["evidence"], []),
        "confidence": float(row["confidence"] or 0.0), "method": row["method"] or "rule",
        "status": row["status"] or "active", "superseded_by": row["superseded_by"] or "",
        "created_at": row["created_at"] or "", "updated_at": row["updated_at"] or "",
    }


def _clean_aliases(aliases: Any) -> List[str]:
    out: List[str] = []
    seen = set()
    for alias in (aliases or ()):
        text = " ".join(str(alias or "").split())
        if not text:
            continue
        key = fold(text)
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def _normalize_rel(value: Any) -> str:
    """Fold to a stable key. Free text is allowed (the vocabulary above is
    not enforced), but it must fold the same way every time it is typed."""
    return fold(value).replace(" ", "_")


# ---------------------------------------------------------------------------
# Name quality — what may become an entity at all
# ---------------------------------------------------------------------------
#
# A capital letter is weak evidence of a name: every sentence starts with
# one. Without this filter "En la carpeta de informes ..." made an entity
# called "En", and every later Spanish text containing "en" was then matched
# to it. Two layers, both deterministic:
#
# * a context-free check (`valid_entity_name`) — function words (articles,
#   prepositions, pronouns, conjunctions, common adverbs, auxiliaries; ES and
#   EN, folded) are never a name on their own, nor at either end of one;
#   single characters and bare numbers are never names;
# * evidence in the source text (`proper_noun_in_source`) — the name must
#   occur there capitalised, and a single capitalised word that only ever
#   opens a sentence needs a second capitalised occurrence (or one inside a
#   sentence) before it counts, unless the caller has independent evidence.

FUNCTION_WORDS = frozenset(fold(w) for w in """
    el la lo los las un una uno unos unas al del de a
    en para por con sin sobre tras entre hasta desde hacia ante bajo contra
    segun durante mediante excepto salvo
    y e o u ni pero sino aunque porque pues que como cuando donde mientras si no
    ya aun tambien tampoco siempre nunca jamas luego despues antes entonces ahora
    hoy ayer manana aqui alli ahi alla asi bien mal muy mas menos solo casi quiza
    quizas ademas incluso todavia apenas
    yo tu ella ellos ellas nosotros nosotras vosotros vosotras usted ustedes
    me te se nos os le les mi mis tus su sus nuestro nuestra nuestros nuestras
    este esta esto estos estas ese esa eso esos esas aquel aquella aquello
    aquellos aquellas
    quien quienes cual cuales cuyo cuya algo alguien nadie nada todo toda todos
    todas cada otro otra otros otras mismo misma ambos varios varias muchos
    muchas pocos pocas algun alguno alguna algunos algunas ningun ninguno
    ninguna cualquier demas tanto tanta tantos tantas
    hay es son era eran fue fueron ser estar estan estaba tiene tienen tengo
    hace puede pueden debe deben va voy
    nota ojo importante hola gracias
    the an this that these those it its i you he she we they him her us them
    my your his our their mine yours
    in on at for to from by with without of about into onto over under after
    before since until upon within via per
    and or but nor so yet if when where while whenever because although though
    unless whether then than also always never often sometimes usually only
    just not yes all any each every some none both either neither other another
    such much many more most less few here there now today tomorrow yesterday
    is are was were be been being am do does did have has had can could would
    should must might shall what which who whom whose how why please however
    therefore too very maybe perhaps everything something anything nothing
    everyone someone anyone nobody everybody somebody note
""".split())

# A single leading article is part of some real names ("El Salvador", "Las
# Palmeras"); a leading preposition or adverb never is ("En Cordera Labs").
_ARTICLES = frozenset({"el", "la", "los", "las", "the"})

_EDGE_PUNCT = " \t\r\n.,;:!?\"'`()[]{}<>«»“”‘’¿¡*_#|/\\-–—…"


def is_function_word(word: Any) -> bool:
    """True for an ES/EN article, preposition, pronoun, conjunction, common
    adverb or auxiliary — words that open sentences and are never a name
    by themselves."""
    return fold(str(word or "").strip(_EDGE_PUNCT)) in FUNCTION_WORDS


def _name_tokens(name: Any) -> List[str]:
    return [tok for tok in (t.strip(_EDGE_PUNCT) for t in str(name or "").split()) if tok]


def clean_name(name: Any) -> str:
    """`name` without leading/trailing function words or edge punctuation —
    "En Cordera Labs" -> "Cordera Labs"; "" when nothing is left."""
    tokens = _name_tokens(name)
    while tokens and is_function_word(tokens[0]):
        tokens.pop(0)
    while tokens and is_function_word(tokens[-1]):
        tokens.pop()
    return " ".join(tokens)


def _core_name_ok(tokens: Sequence[str]) -> bool:
    if not tokens or not any(ch.isalpha() for tok in tokens for ch in tok):
        return False
    if len(tokens) == 1:
        token = tokens[0]
        return len(token) >= 2 and not token.isdigit() and not is_function_word(token)
    return not all(is_function_word(tok) for tok in tokens)


def valid_entity_name(name: Any) -> bool:
    """Context-free: could `name` be an entity name at all? Rejects function
    words, single characters, bare numbers and names that start or end with
    a function word (other than one leading article in a multi-word name) —
    and, among those, an article followed only by ordinary lowercase words
    ("El modelo", "La carpeta", "The model" are common nouns, not names),
    keeping a real place name whose word after the article is itself
    capitalised ("El Salvador", "La Rioja", "The Hague")."""
    tokens = _name_tokens(name)
    if not tokens:
        return False
    cleaned = clean_name(" ".join(tokens)).split()
    if cleaned == tokens:
        return _core_name_ok(tokens)
    if (len(tokens) >= 2 and fold(tokens[0]) in _ARTICLES and cleaned == tokens[1:]):
        if not cleaned or not cleaned[0][:1].isupper():
            return False
        return _core_name_ok(cleaned)
    return False


def _strip_accents(text: str) -> str:
    """Accent-free text that KEEPS case, so capitalisation can still be read
    from a match (unlike `fold`, which lowercases)."""
    import unicodedata

    raw = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(ch for ch in raw if not unicodedata.combining(ch))


# Only whitespace, opening quotes/brackets, bullets or a list number between
# a sentence boundary (or the start of the text) and the word.
_SENTENCE_START_RE = re.compile(
    r"(?:^|[.!?:;…\n])[\s\"'“”‘«»(\[¿¡*#>•\-–—]*(?:\d+[.)]\s*)?$")


def _is_sentence_initial(text: str, start: int) -> bool:
    return _SENTENCE_START_RE.search(text[:start]) is not None


def _has_upper(token: str) -> bool:
    return any(ch.isupper() for ch in token)


def proper_noun_in_source(name: Any, text: Any, *, allow_sentence_initial: bool = False) -> bool:
    """Does `text` support `name` as a proper noun?

    `name` must pass :func:`valid_entity_name` and occur in `text` with
    every non-function word capitalised ("carpetas", "nombres raros" do
    not). A single word needs at least one capitalised occurrence that is
    not at the start of a sentence, or two capitalised occurrences — unless
    `allow_sentence_initial` (the caller has other evidence, e.g. it is the
    whole grammatical subject of a matched predicate)."""
    if not valid_entity_name(name):
        return False
    tokens = _name_tokens(_strip_accents(str(name)))
    haystack = _strip_accents(str(text or ""))
    if not tokens or not haystack:
        return False
    pattern = re.compile(
        r"(?<!\w)" + r"\s+".join(re.escape(tok) for tok in tokens) + r"(?!\w)", re.IGNORECASE)
    capitalised: List[bool] = []  # one entry per capitalised occurrence: sentence-initial?
    for match in pattern.finditer(haystack):
        words = match.group(0).split()
        content = [w for w in words if not is_function_word(w)] or words
        if all(_has_upper(w) for w in content):
            capitalised.append(_is_sentence_initial(haystack, match.start()))
    if not capitalised:
        return False
    if len(tokens) > 1:
        return True
    return (not all(capitalised)) or len(capitalised) >= 2 or allow_sentence_initial


# ---------------------------------------------------------------------------
# Relation vocabulary — synonyms a model (or a person) writes -> the key
# ---------------------------------------------------------------------------

_RELATION_SYNONYMS: Dict[str, str] = {
    fold(phrase): rel for phrase, rel in (
        ("works at", "works_at"), ("work at", "works_at"), ("works for", "works_at"),
        ("work for", "works_at"), ("employed at", "works_at"), ("employed by", "works_at"),
        ("trabaja en", "works_at"), ("trabaja para", "works_at"), ("trabajo en", "works_at"),
        ("works on", "works_on"), ("trabaja sobre", "works_on"), ("trabaja en el proyecto", "works_on"),
        ("uses", "uses"), ("use", "uses"), ("usa", "uses"), ("uso", "uses"),
        ("utiliza", "uses"), ("utilizo", "uses"), ("using", "uses"),
        ("prefers", "prefers"), ("prefer", "prefers"), ("prefiere", "prefers"),
        ("prefiero", "prefers"),
        ("lives in", "lives_in"), ("live in", "lives_in"), ("vive en", "lives_in"),
        ("vivo en", "lives_in"), ("resides in", "lives_in"), ("reside en", "lives_in"),
        ("located in", "located_in"), ("is located in", "located_in"),
        ("esta en", "located_in"), ("esta ubicado en", "located_in"),
        ("esta situado en", "located_in"), ("se encuentra en", "located_in"),
        ("part of", "part_of"), ("is part of", "part_of"), ("es parte de", "part_of"),
        ("forma parte de", "part_of"),
        ("member of", "member_of"), ("is member of", "member_of"),
        ("is a member of", "member_of"), ("belongs to", "member_of"),
        ("es miembro de", "member_of"), ("miembro de", "member_of"),
        ("pertenece a", "member_of"),
        ("knows", "knows"), ("know", "knows"), ("conoce", "knows"), ("conoce a", "knows"),
        ("conozco", "knows"),
        ("owns", "owns"), ("own", "owns"), ("posee", "owns"), ("es dueno de", "owns"),
        ("created", "created"), ("creo", "created"), ("creator of", "created"),
        ("depends on", "depends_on"), ("depende de", "depends_on"),
        ("is a", "is_a"), ("is an", "is_a"), ("es un", "is_a"), ("es una", "is_a"),
        ("related to", "related_to"), ("relacionado con", "related_to"),
        ("relacionada con", "related_to"), ("relates to", "related_to"),
        ("studied at", "studied_at"), ("studies at", "studied_at"),
        ("estudio en", "studied_at"), ("estudia en", "studied_at"),
    )
}


def canonical_relation(rel: Any) -> Optional[str]:
    """The :data:`KNOWN_RELATIONS` key `rel` means, or None when it maps to
    none of them ("works for" -> works_at, "trabaja en" -> works_at,
    "intocable" -> None). Case, accents, "_"/"-" and spacing are ignored."""
    text = fold(re.sub(r"[_\-]+", " ", str(rel or ""))).strip()
    if not text:
        return None
    key = text.replace(" ", "_")
    if key in KNOWN_RELATIONS:
        return key
    return _RELATION_SYNONYMS.get(text)


def _coerce_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    parsed = parse_iso(value)
    return parsed or datetime.now(timezone.utc)


def _norm_dt_arg(value: Any) -> str:
    """A caller-supplied valid_from/valid_until (datetime, ISO string, or
    None) -> a stored ISO string, or ``""`` when nothing usable was given."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if value.tzinfo else \
            value.replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    parsed = parse_iso(value)
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ") if parsed else ""


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------


def _match_entity(conn: sqlite3.Connection, owner: str, fold_name: str,
                  alias_folds: set) -> Optional[sqlite3.Row]:
    """The entity `fold_name`/`alias_folds` resolves to, following a merge
    chain to its final (non-merged) target. None if nothing matches."""
    rows = conn.execute("SELECT * FROM entities WHERE owner = ?", (owner,)).fetchall()
    for row in rows:
        row_folds = {row["fold_name"]} | {fold(a) for a in loads(row["aliases"], [])}
        if fold_name in row_folds or (row_folds & alias_folds):
            target = row
            seen = set()
            while target["merged_into"] and target["id"] not in seen:
                seen.add(target["id"])
                nxt = conn.execute(
                    "SELECT * FROM entities WHERE id = ?", (target["merged_into"],)
                ).fetchone()
                if not nxt:
                    break
                target = nxt
            return target
    return None


def upsert_entity(owner: Any, name: Any, *, type: str = "other",  # noqa: A002
                  aliases: Sequence[str] = (), project: str = "") -> Dict[str, Any]:
    """Alias/fold-aware create-or-find. A `name` (or alias) that folds equal
    to an existing entity's name or alias returns THAT entity — its aliases
    are extended with anything new, never duplicated."""
    owner = str(owner or "")
    name = " ".join(str(name or "").split())
    if not name:
        raise BrainEntityError("entity name must not be empty")
    etype = str(type or "other").strip().lower()
    if etype not in TYPES:
        etype = "other"
    alias_list = _clean_aliases(aliases)
    fold_name = fold(name)
    alias_folds = {fold(a) for a in alias_list}
    now = now_iso()
    with db() as conn:
        existing = _match_entity(conn, owner, fold_name, alias_folds)
        if existing:
            entity_id = str(existing["id"])
            current_aliases = loads(existing["aliases"], [])
            merged = list(current_aliases)
            merged_folds = {fold(a) for a in merged} | {existing["fold_name"]}
            if fold_name != existing["fold_name"] and fold_name not in merged_folds:
                merged.append(name)
                merged_folds.add(fold_name)
            for alias in alias_list:
                if fold(alias) not in merged_folds:
                    merged.append(alias)
                    merged_folds.add(fold(alias))
            if merged != current_aliases:
                conn.execute("UPDATE entities SET aliases = ?, updated_at = ? WHERE id = ?",
                            (dumps(merged), now, entity_id))
            return _row_to_entity(conn.execute(
                "SELECT * FROM entities WHERE id = ?", (entity_id,)).fetchone())

        entity_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO entities (id, owner, project, name, fold_name, type, aliases, "
            "summary, summary_sources, summary_locked, summary_updated_at, facts_hash, "
            "hidden, merged_into, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (entity_id, owner, str(project or ""), name, fold_name, etype,
            dumps(alias_list), "", "[]", 0, "", "", 0, "", now, now),
        )
        return _row_to_entity(conn.execute(
            "SELECT * FROM entities WHERE id = ?", (entity_id,)).fetchone())


def get_entity(entity_id: Any) -> Optional[Dict[str, Any]]:
    with db() as conn:
        row = conn.execute("SELECT * FROM entities WHERE id = ?", (str(entity_id or ""),)).fetchone()
    return _row_to_entity(row) if row else None


_ENTITY_JSON_FIELDS = {"aliases", "summary_sources"}
_ENTITY_BOOL_FIELDS = {"summary_locked", "hidden"}
_ENTITY_SETTABLE = {
    "name", "type", "aliases", "summary", "summary_locked", "hidden",
    "summary_sources", "facts_hash", "project", "merged_into",
}


def update_entity(entity_id: Any, **fields: Any) -> Optional[Dict[str, Any]]:
    """Generic field setter (name/type/aliases/summary/summary_locked/hidden
    plus the wiki-maintained `summary_sources`/`facts_hash`). Unknown keys
    are ignored rather than raising, so a caller can pass a whole partial
    dict through. Returns the updated entity, or None if it does not exist."""
    entity_id = str(entity_id or "")
    entity = get_entity(entity_id)
    if not entity:
        return None
    sets: List[str] = []
    params: List[Any] = []
    for key, value in fields.items():
        if key not in _ENTITY_SETTABLE:
            continue
        if key == "name":
            name = " ".join(str(value or "").split())
            if not name:
                raise BrainEntityError("name must not be empty")
            sets.append("name = ?"); params.append(name)
            sets.append("fold_name = ?"); params.append(fold(name))
        elif key == "type":
            etype = str(value or "other").strip().lower()
            if etype not in TYPES:
                raise BrainEntityError(f"type must be one of {', '.join(TYPES)}")
            sets.append("type = ?"); params.append(etype)
        elif key in _ENTITY_JSON_FIELDS:
            cleaned = _clean_aliases(value) if key == "aliases" else [str(v) for v in (value or [])]
            sets.append(f"{key} = ?"); params.append(dumps(cleaned))
        elif key in _ENTITY_BOOL_FIELDS:
            sets.append(f"{key} = ?"); params.append(1 if value else 0)
        else:
            sets.append(f"{key} = ?"); params.append(str(value or ""))
    if not sets:
        return entity
    if "summary" in fields:
        sets.append("summary_updated_at = ?"); params.append(now_iso())
    sets.append("updated_at = ?"); params.append(now_iso())
    params.append(entity_id)
    with db() as conn:
        conn.execute(f"UPDATE entities SET {', '.join(sets)} WHERE id = ?", params)
    return get_entity(entity_id)


def set_hidden(entity_id: Any, hidden: bool) -> Optional[Dict[str, Any]]:
    return update_entity(entity_id, hidden=bool(hidden))


def list_entities(owner: Any, *, q: str = "", type: str = "", limit: int = 200,  # noqa: A002
                  include_hidden: bool = False) -> List[Dict[str, Any]]:
    owner = str(owner or "")
    where = ["owner = ?", "merged_into = ''"]
    params: List[Any] = [owner]
    if not include_hidden:
        where.append("hidden = 0")
    if type:
        where.append("type = ?"); params.append(str(type))
    sql = f"SELECT * FROM entities WHERE {' AND '.join(where)} ORDER BY updated_at DESC LIMIT ?"
    params.append(max(1, min(2000, int(limit or 200))))
    with db() as conn:
        rows = conn.execute(sql, params).fetchall()
    q_fold = fold(q) if q else ""
    out: List[Dict[str, Any]] = []
    for row in rows:
        entity = _row_to_entity(row)
        if q_fold:
            haystacks = {entity["fold_name"]} | {fold(a) for a in entity["aliases"]}
            if not any(q_fold in h for h in haystacks):
                continue
        entity["mentions"] = sources_for(entity["id"])
        entity["relations"] = list_relations(owner, entity_id=entity["id"], include_closed=False)
        out.append(entity)
    return out


def merge_entities(keep_id: Any, merge_id: Any) -> Dict[str, Any]:
    """Fold `merge_id` into `keep_id`: relations and mentions are
    re-pointed, aliases combined, and `merge_id` is hidden with
    `merged_into` set so any later lookup by its name/alias resolves
    straight through to `keep_id`."""
    keep_id, merge_id = str(keep_id or ""), str(merge_id or "")
    if not keep_id or not merge_id or keep_id == merge_id:
        raise BrainEntityError("merge_entities needs two different entity ids")
    keep = get_entity(keep_id)
    merged = get_entity(merge_id)
    if not keep or not merged:
        raise BrainEntityError("merge_entities: entity not found")
    now = now_iso()
    with db() as conn:
        conn.execute("UPDATE relations SET src = ?, updated_at = ? WHERE src = ?",
                    (keep_id, now, merge_id))
        conn.execute("UPDATE relations SET dst = ?, updated_at = ? WHERE dst = ?",
                    (keep_id, now, merge_id))
        conn.execute("UPDATE relations SET superseded_by = ? WHERE superseded_by = ?",
                    (keep_id, merge_id))
        rows = conn.execute(
            "SELECT source_ref FROM mentions WHERE entity_id = ?", (merge_id,)).fetchall()
        for row in rows:
            conn.execute(
                "INSERT INTO mentions (owner, entity_id, source_ref, created_at) "
                "VALUES (?,?,?,?) ON CONFLICT(entity_id, source_ref) DO NOTHING",
                (merged["owner"], keep_id, row["source_ref"], now),
            )
        conn.execute("DELETE FROM mentions WHERE entity_id = ?", (merge_id,))

        combined = list(keep.get("aliases") or [])
        combined_folds = {fold(a) for a in combined} | {keep["fold_name"]}
        for candidate in [merged["name"], *(merged.get("aliases") or [])]:
            if fold(candidate) not in combined_folds:
                combined.append(candidate)
                combined_folds.add(fold(candidate))
        conn.execute("UPDATE entities SET aliases = ?, updated_at = ? WHERE id = ?",
                    (dumps(combined), now, keep_id))
        conn.execute(
            "UPDATE entities SET merged_into = ?, hidden = 1, updated_at = ? WHERE id = ?",
            (keep_id, now, merge_id),
        )
    return get_entity(keep_id)


def self_entity(owner: Any) -> Dict[str, Any]:
    """The owner's own `person` entity, created on first use. "I use X" /
    "uso X" attach to this node the same way "Ada uses X" attaches to
    Ada's."""
    display = ""
    try:
        from src.settings import get_setting
        display = str(get_setting("owner_display_name", "") or "").strip()
    except Exception:  # noqa: BLE001
        display = ""
    aliases = list(SELF_WORDS) + list(SELF_EXTRA_ALIASES)
    me = _find_self(owner)
    if me is None:
        me = upsert_entity(owner, display or "Yo", type="person", aliases=aliases)
    else:
        missing = [a for a in aliases if fold(a) not in {fold(x) for x in me.get("aliases") or []}]
        if missing:
            me = update_entity(me["id"], aliases=list(me.get("aliases") or []) + missing) or me
    # Person nodes that are really the owner — created from the owner's name
    # before it was set ("Ada prefers tabs"), or from "the user" before the
    # self node carried that alias — fold into it.
    self_names = {fold(a) for a in SELF_EXTRA_ALIASES}
    if display:
        self_names.add(fold(display))
    for other in list_entities(owner, type="person", limit=500):
        if other.get("id") != me.get("id") and fold(other.get("name")) in self_names:
            me = merge_entities(me["id"], other["id"]) or me
    # The node may have been born as "Yo", or adopted a stray "User" row:
    # name it after the owner when we know it, else "Yo".
    current = fold(me.get("name"))
    wanted = display or "Yo"
    if current != fold(wanted) and (current in SELF_WORDS or current in {fold(a) for a in SELF_EXTRA_ALIASES}):
        me = update_entity(me["id"], name=wanted) or me
    return me


def _find_self(owner: str) -> Optional[Dict[str, Any]]:
    """The oldest live person node carrying every self word as an alias."""
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM entities WHERE owner = ? AND type = 'person' AND "
            "(merged_into IS NULL OR merged_into = '') ORDER BY created_at, id",
            (owner,),
        ).fetchall()
        for row in rows:
            if _is_self_row(row):
                return _row_to_entity(row)
    return None


# ---------------------------------------------------------------------------
# Relations
# ---------------------------------------------------------------------------


def _close_relation(relation_id: str, valid_until: str, *, status: str, superseded_by: str = "",
                    now: Optional[str] = None) -> None:
    stamp = now or now_iso()
    with db() as conn:
        conn.execute(
            "UPDATE relations SET valid_until = ?, status = ?, superseded_by = ?, "
            "updated_at = ? WHERE id = ?",
            (valid_until, status, superseded_by, stamp, relation_id),
        )


def _relation_start(relation: Dict[str, Any]) -> Tuple[Optional[datetime], bool]:
    """(start, explicit?) — a relation asserted without a date gets its
    assertion instant as `valid_from`, so "explicit" means they differ."""
    start = parse_iso(relation.get("valid_from"))
    asserted = parse_iso(relation.get("asserted_at"))
    if start is None:
        return asserted, False
    return start, asserted is None or start != asserted


def _supersede_functional(new_rel: Dict[str, Any], current: Dict[str, Any],
                          now: str) -> Optional[str]:
    """Close whichever of `new_rel`/`current` is outdated; returns the id
    that was closed, or None when nothing changed."""
    from src.brain.temporal import supersede_order

    new_start, new_explicit = _relation_start(new_rel)
    cur_start, cur_explicit = _relation_start(current)
    order = supersede_order(new_start, cur_start, new_explicit=new_explicit,
                            old_explicit=cur_explicit)
    if order == "old":
        target, other = current, new_rel
    elif order == "new":
        target, other = new_rel, current
    else:
        return None
    if str(target.get("method") or "rule") == "rule" and str(other.get("method") or "") == "llm":
        return None  # a model's claim never ends a fact read deterministically from the text
    at = str(other.get("valid_from") or "")
    at_dt = parse_iso(at)
    target_start, _ = _relation_start(target)
    target_end = parse_iso(target.get("valid_until"))
    if at_dt is None or (target_start is not None and at_dt < target_start):
        return None
    if target_end is not None and target_end <= at_dt:
        # already ended by then: its window is never extended nor rewritten,
        # it is only marked as followed by the other one
        _close_relation(str(target["id"]), str(target.get("valid_until") or ""),
                        status="superseded", superseded_by=str(other["id"]), now=now)
        return str(target["id"])
    _close_relation(str(target["id"]), at, status="superseded",
                    superseded_by=str(other["id"]), now=now)
    return str(target["id"])


def add_relation(owner: Any, src_id: Any, rel: Any, *, dst_id: Any = None,
                 dst_value: str = "", valid_from: Any = None, valid_until: Any = None,
                 evidence: Sequence[str] = (), confidence: float = 0.6, method: str = "rule",
                 project: str = "") -> Dict[str, Any]:
    """Assert one relation. When `rel` is functional
    (:data:`FUNCTIONAL_RELATIONS`) and `src_id` already has a DIFFERENT
    current value for it, the OUTDATED one of the two is closed
    (`valid_until` set to the other one's `valid_from`, status
    `superseded`, `superseded_by` the other) rather than both left open.

    Chronology decides which one is outdated, not arrival order
    (`temporal.supersede_order`): asserting "works at Cordera Labs since
    2019" after "works at Bluehaven since 2024" closes the 2019 relation at
    2024, never the current one at 2019. A window is never made to end
    before it starts, and one that already ended is never rewritten. When
    the order is unknowable (equal dated starts; a dated start earlier than
    an undated one) both stay open for a human to look at. A model-derived
    relation (method "llm") never closes a rule-derived one."""
    owner = str(owner or "")
    src_id = str(src_id or "")
    rel_key = _normalize_rel(rel)
    if not src_id:
        raise BrainEntityError("add_relation needs src_id")
    if not rel_key:
        raise BrainEntityError("add_relation needs rel")
    dst_id = str(dst_id or "")
    dst_value = "" if dst_id else " ".join(str(dst_value or "").split())[:400]
    if not dst_id and not dst_value:
        raise BrainEntityError("add_relation needs dst_id or dst_value")

    now = now_iso()
    valid_from_iso = _norm_dt_arg(valid_from) or now
    valid_until_iso = _norm_dt_arg(valid_until)

    duplicate = _find_same_active_relation(owner, src_id, rel_key, dst_id, dst_value)
    if duplicate is not None:
        # The same fact asserted again (another memory, or the model pass
        # restating a rule-found relation): one edge, more evidence.
        return _merge_into_relation(duplicate, evidence=evidence, confidence=confidence,
                                    method=str(method or "rule"), valid_from=_norm_dt_arg(valid_from),
                                    now=now)

    relation_id = uuid.uuid4().hex
    with db() as conn:
        conn.execute(
            "INSERT INTO relations (id, owner, project, src, rel, dst, dst_value, "
            "valid_from, valid_until, asserted_at, evidence, confidence, method, "
            "status, superseded_by, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (relation_id, owner, str(project or ""), src_id, rel_key, dst_id, dst_value,
            valid_from_iso, valid_until_iso, now, dumps(list(evidence)),
            float(confidence), str(method or "rule"), "active", "", now, now),
        )

    if rel_key in FUNCTIONAL_RELATIONS:
        new_rel = {"id": relation_id, "valid_from": valid_from_iso, "valid_until": valid_until_iso,
                   "asserted_at": now, "method": str(method or "rule")}
        for current in list_relations(owner, entity_id=src_id, include_closed=False):
            if current["id"] == relation_id or current["src"] != src_id or current["rel"] != rel_key:
                continue
            same_dst = (dst_id and current["dst"] == dst_id) or \
                       (not dst_id and current["dst_value"] == dst_value)
            if same_dst:
                continue
            closed = _supersede_functional(new_rel, current, now)
            if closed is not None and closed == relation_id:
                with db() as conn:
                    row = conn.execute("SELECT valid_until FROM relations WHERE id = ?",
                                       (relation_id,)).fetchone()
                new_rel["valid_until"] = (row["valid_until"] if row else "") or ""

    with db() as conn:
        row = conn.execute("SELECT * FROM relations WHERE id = ?", (relation_id,)).fetchone()
    return _row_to_relation(row)


def _find_same_active_relation(owner: str, src_id: str, rel_key: str, dst_id: Any,
                               dst_value: str) -> Optional[Dict[str, Any]]:
    with db() as conn:
        if dst_id:
            row = conn.execute(
                "SELECT * FROM relations WHERE owner = ? AND src = ? AND rel = ? AND dst = ? "
                "AND status = 'active' ORDER BY created_at LIMIT 1",
                (owner, src_id, rel_key, dst_id)).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM relations WHERE owner = ? AND src = ? AND rel = ? "
                "AND (dst IS NULL OR dst = '') AND dst_value = ? AND status = 'active' "
                "ORDER BY created_at LIMIT 1",
                (owner, src_id, rel_key, dst_value)).fetchone()
    return _row_to_relation(row) if row else None


def _merge_into_relation(existing: Dict[str, Any], *, evidence: Sequence[str], confidence: float,
                         method: str, valid_from: Optional[str], now: str) -> Dict[str, Any]:
    merged_evidence = list(existing.get("evidence") or [])
    for ref in evidence or ():
        if ref and ref not in merged_evidence:
            merged_evidence.append(ref)
    new_method = existing.get("method") or "rule"
    new_from = existing.get("valid_from") or ""
    if method == "rule" and new_method != "rule":
        # A rule-found restatement outranks the model's copy, date included.
        new_method = "rule"
        if valid_from:
            new_from = valid_from
    with db() as conn:
        conn.execute(
            "UPDATE relations SET evidence = ?, confidence = ?, method = ?, valid_from = ?, "
            "updated_at = ? WHERE id = ?",
            (dumps(merged_evidence), max(float(existing.get("confidence") or 0), float(confidence)),
             new_method, new_from, now, existing["id"]))
        row = conn.execute("SELECT * FROM relations WHERE id = ?", (existing["id"],)).fetchone()
    return _row_to_relation(row)


def _relation_window_covers(relation: Dict[str, Any], instant: datetime) -> bool:
    valid_from = parse_iso(relation.get("valid_from"))
    if valid_from and instant < valid_from:
        return False
    valid_until = parse_iso(relation.get("valid_until"))
    if valid_until and instant > valid_until:
        return False
    return True


def list_relations(owner: Any, *, entity_id: Any = None, as_of: Any = None,
                   include_closed: bool = True,
                   include_retracted: bool = False) -> List[Dict[str, Any]]:
    """Relations of `owner` (optionally touching `entity_id`). A RETRACTED
    relation (status "retracted": a model claim withdrawn by
    :func:`revalidate`) is kept in the table for the record and for
    :func:`undo_revalidate`, but is not part of the graph any more, so it is
    left out unless `include_retracted`."""
    owner = str(owner or "")
    with db() as conn:
        if entity_id:
            eid = str(entity_id)
            rows = conn.execute(
                "SELECT * FROM relations WHERE owner = ? AND (src = ? OR dst = ?) "
                "ORDER BY created_at",
                (owner, eid, eid),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM relations WHERE owner = ? ORDER BY created_at", (owner,)
            ).fetchall()
    items = [_row_to_relation(row) for row in rows]
    if not include_retracted:
        items = [r for r in items if r["status"] != "retracted"]
    if as_of is not None:
        instant = _coerce_dt(as_of)
        items = [r for r in items if _relation_window_covers(r, instant)]
    elif not include_closed:
        items = [r for r in items if r["status"] == "active"]
    return items


# ---------------------------------------------------------------------------
# Mentions
# ---------------------------------------------------------------------------


def add_mention(owner: Any, entity_id: Any, source_ref: Any) -> None:
    entity_id = str(entity_id or "")
    source_ref = str(source_ref or "")
    if not entity_id or not source_ref:
        return
    with db() as conn:
        conn.execute(
            "INSERT INTO mentions (owner, entity_id, source_ref, created_at) VALUES (?,?,?,?) "
            "ON CONFLICT(entity_id, source_ref) DO NOTHING",
            (str(owner or ""), entity_id, source_ref, now_iso()),
        )


def mentions_for(source_ref: Any) -> List[Dict[str, Any]]:
    with db() as conn:
        rows = conn.execute(
            "SELECT entity_id FROM mentions WHERE source_ref = ?", (str(source_ref or ""),)
        ).fetchall()
    out = []
    for row in rows:
        entity = get_entity(row["entity_id"])
        if entity:
            out.append(entity)
    return out


def sources_for(entity_id: Any) -> List[str]:
    with db() as conn:
        rows = conn.execute(
            "SELECT source_ref FROM mentions WHERE entity_id = ? ORDER BY created_at",
            (str(entity_id or ""),),
        ).fetchall()
    return [row["source_ref"] for row in rows]


def repoint_source(owner: Any, old_ref: Any, new_ref: Any) -> Dict[str, int]:
    """A source moved without its facts changing meaning — most often a
    memory correction (`memory_engine.correct`): the sentence lives on under
    a NEW id, the old one is gone for good. Every mention of `old_ref` and
    every relation-evidence citation of it now cite `new_ref` instead, so a
    correction never turns an entity's already-known facts and relations
    into orphaned, unresolvable citations. A mention already citing
    `new_ref` is left alone (no duplicate row); an evidence list gets
    `old_ref` replaced and de-duplicated. A no-op when either ref is empty
    or they are equal. Never raises; returns how many rows of each kind
    moved."""
    owner = str(owner or "")
    old_ref = str(old_ref or "")
    new_ref = str(new_ref or "")
    moved = {"mentions": 0, "relations": 0}
    if not owner or not old_ref or not new_ref or old_ref == new_ref:
        return moved
    try:
        with db() as conn:
            rows = conn.execute(
                "SELECT entity_id FROM mentions WHERE owner = ? AND source_ref = ?",
                (owner, old_ref),
            ).fetchall()
            now = now_iso()
            for row in rows:
                conn.execute(
                    "INSERT INTO mentions (owner, entity_id, source_ref, created_at) "
                    "VALUES (?,?,?,?) ON CONFLICT(entity_id, source_ref) DO NOTHING",
                    (owner, row["entity_id"], new_ref, now),
                )
            cur = conn.execute(
                "DELETE FROM mentions WHERE owner = ? AND source_ref = ?", (owner, old_ref))
            moved["mentions"] = cur.rowcount or 0

            rel_rows = conn.execute(
                "SELECT id, evidence FROM relations WHERE owner = ?", (owner,)).fetchall()
            for row in rel_rows:
                evidence = loads(row["evidence"], [])
                if old_ref not in evidence:
                    continue
                updated: List[str] = []
                seen: set = set()
                for ref in evidence:
                    ref = new_ref if ref == old_ref else ref
                    if ref in seen:
                        continue
                    seen.add(ref)
                    updated.append(ref)
                conn.execute("UPDATE relations SET evidence = ?, updated_at = ? WHERE id = ?",
                            (dumps(updated), now, row["id"]))
                moved["relations"] += 1
    except Exception as exc:  # noqa: BLE001 - a cleanup pass must never raise
        logger.debug("brain.entities: repoint_source(%s -> %s) failed (%s)", old_ref, new_ref, exc)
    return moved


def forget_source(owner: Any, source_ref: Any) -> int:
    """`source_ref` is gone for good (forgotten, a deleted personal memory, a
    deleted free note) with no successor to repoint to: drop every mention
    of it and strip it out of any relation's evidence list. A relation is
    never deleted just because one of its citations went stale — evidence is
    a record of what supported it, not a validity gate; :func:`revalidate`
    and the vault's own gone-source handling are what retire a relation or
    entity outright. Never raises; returns how many mentions were removed."""
    owner = str(owner or "")
    source_ref = str(source_ref or "")
    if not owner or not source_ref:
        return 0
    removed = 0
    try:
        with db() as conn:
            cur = conn.execute(
                "DELETE FROM mentions WHERE owner = ? AND source_ref = ?", (owner, source_ref))
            removed = cur.rowcount or 0
            rel_rows = conn.execute(
                "SELECT id, evidence FROM relations WHERE owner = ?", (owner,)).fetchall()
            now = now_iso()
            for row in rel_rows:
                evidence = loads(row["evidence"], [])
                if source_ref not in evidence:
                    continue
                updated = [ref for ref in evidence if ref != source_ref]
                conn.execute("UPDATE relations SET evidence = ?, updated_at = ? WHERE id = ?",
                            (dumps(updated), now, row["id"]))
    except Exception as exc:  # noqa: BLE001 - a cleanup pass must never raise
        logger.debug("brain.entities: forget_source(%s) failed (%s)", source_ref, exc)
    return removed


# ---------------------------------------------------------------------------
# Mention detection in free text
# ---------------------------------------------------------------------------

# Names shorter than this never match on their own: too easy to collide
# with an ordinary word ("Yo", any single letter). Two-letter aliases like
# "yo"/"me" still pass; the bare "i" from SELF_WORDS deliberately does not
# (self-reference through "I" alone is resolved explicitly by callers that
# know they are looking at a sentence subject, not by this general sweep).
_MIN_MATCH_LEN = 2


def entities_in_text(owner: Any, text: Any, *, limit: int = 8) -> List[Dict[str, Any]]:
    """Known entities (by name or alias) mentioned in `text` — word-boundary,
    fold-insensitive, longest name first."""
    owner = str(owner or "")
    text_fold = fold(text)
    if not text_fold:
        return []
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM entities WHERE owner = ? AND merged_into = ''", (owner,)
        ).fetchall()
    candidates: List[Tuple[str, sqlite3.Row]] = []
    for row in rows:
        for name in [row["name"], *loads(row["aliases"], [])]:
            name = str(name or "").strip()
            if len(name) < _MIN_MATCH_LEN:
                continue
            # A name that is a function word ("En", "Todo" — written before
            # the name filter existed) would match nearly every text; only
            # the owner's own self-words are allowed to be that short/common.
            if fold(name) not in SELF_WORDS and not valid_entity_name(name):
                continue
            candidates.append((fold(name), row))
    candidates.sort(key=lambda pair: -len(pair[0]))

    matched: List[str] = []
    seen = set()
    for folded_name, row in candidates:
        if row["id"] in seen or not folded_name:
            continue
        if re.search(rf"\b{re.escape(folded_name)}\b", text_fold):
            seen.add(row["id"])
            matched.append(row["id"])
        if len(matched) >= max(1, int(limit or 8)):
            break
    return [get_entity(eid) for eid in matched]


# ---------------------------------------------------------------------------
# Revalidation — the one-off cleanup of rows written before the name filter
# ---------------------------------------------------------------------------
#
# Rows written before `valid_entity_name` and `canonical_relation` existed
# stay in the store until something looks at them again. `revalidate` is
# that look: it HIDES (never deletes) entities whose names fail the filter
# and RETRACTS (never deletes) model relations outside the vocabulary.
# Anything a person touched is left alone — a locked summary, an entity
# something was merged into, the owner's own entity, or an entity a person
# brought back after an earlier revalidation hid it. What it changed is
# stored so `undo_revalidate` can put it back. Bump REVALIDATE_VERSION when
# the filter changes enough to deserve another pass.

REVALIDATE_VERSION = 3

_META_VERSION = "revalidate_version"
_META_LAST = "revalidate_last"
_META_HIDDEN_EVER = "revalidate_hidden_ever"


def _meta_get(conn: sqlite3.Connection, owner: str, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM brain_meta WHERE owner = ? AND key = ?",
                       (owner, key)).fetchone()
    return loads(row["value"], default) if row else default


def _meta_set(conn: sqlite3.Connection, owner: str, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO brain_meta (owner, key, value, updated_at) VALUES (?,?,?,?) "
        "ON CONFLICT(owner, key) DO UPDATE SET value = excluded.value, "
        "updated_at = excluded.updated_at",
        (owner, key, dumps(value), now_iso()),
    )


def _name_fits_type(name: Any, etype: Any) -> bool:
    """A name with no capital letter and no digit ("chats", "projects") is a
    common noun unless it names software, where lowercase is the norm
    ("npm", "pytest")."""
    text = str(name or "")
    if any(ch.isupper() or ch.isdigit() for ch in text):
        return True
    return str(etype or "") == "tool"


def _is_self_row(row: sqlite3.Row) -> bool:
    folds = {row["fold_name"]} | {fold(a) for a in loads(row["aliases"], [])}
    return row["type"] == "person" and set(SELF_WORDS) <= folds


def revalidate(owner: Any, *, dry_run: bool = False) -> Dict[str, Any]:
    """Hide entities of `owner` whose names fail :func:`valid_entity_name`
    and retract ``method == "llm"`` relations whose ``rel`` is outside
    :data:`KNOWN_RELATIONS`. Idempotent, reversible (:func:`undo_revalidate`),
    never raises; returns what it did (or would do, with `dry_run`)."""
    owner = str(owner or "")
    report: Dict[str, Any] = {
        "owner": owner, "version": REVALIDATE_VERSION, "dry_run": bool(dry_run),
        "hidden": [], "hidden_names": [], "hidden_count": 0,
        "retracted": [], "retracted_count": 0, "kept_human": 0, "errors": 0,
    }
    if not owner:
        return report
    try:
        with db() as conn:
            rows = conn.execute("SELECT * FROM entities WHERE owner = ?", (owner,)).fetchall()
            merge_targets = {row["merged_into"] for row in rows if row["merged_into"]}
            hidden_ever = set(_meta_get(conn, owner, _META_HIDDEN_EVER, []) or [])
            to_hide: List[sqlite3.Row] = []
            for row in rows:
                if row["merged_into"] or row["hidden"] or (
                        valid_entity_name(row["name"]) and _name_fits_type(row["name"], row["type"])):
                    continue
                if (row["summary_locked"] or row["id"] in merge_targets or _is_self_row(row)
                        or row["id"] in hidden_ever):
                    report["kept_human"] += 1
                    continue
                to_hide.append(row)

            rel_rows = conn.execute(
                "SELECT id, rel, status FROM relations WHERE owner = ? AND method = 'llm' "
                "AND status != 'retracted'", (owner,)).fetchall()
            to_retract = [r for r in rel_rows if r["rel"] not in KNOWN_RELATIONS]
            # A model restatement of an edge that already exists (possibly
            # under a synonym: "works_for" next to "works_at") is one fact
            # drawn twice: keep the rule-found (or oldest) edge.
            retract_ids = {r["id"] for r in to_retract}
            live = conn.execute(
                "SELECT id, src, rel, dst, dst_value, method, status FROM relations "
                "WHERE owner = ? AND status = 'active' ORDER BY created_at, id", (owner,)).fetchall()
            seen: Dict[Tuple[str, str, str], sqlite3.Row] = {}
            for row in live:
                if row["id"] in retract_ids:
                    continue
                canonical = canonical_relation(row["rel"]) or row["rel"]
                key = (row["src"], canonical, row["dst"] or ("=" + fold(row["dst_value"])))
                keeper = seen.get(key)
                if keeper is None:
                    seen[key] = row
                    continue
                loser = row
                if keeper["method"] == "llm" and row["method"] != "llm":
                    seen[key], loser = row, keeper
                if loser["method"] == "llm":
                    to_retract.append(loser)
                    retract_ids.add(loser["id"])

            report["hidden"] = [row["id"] for row in to_hide]
            report["hidden_names"] = [row["name"] for row in to_hide]
            report["retracted"] = [r["id"] for r in to_retract]
            report["hidden_count"] = len(to_hide)
            report["retracted_count"] = len(to_retract)
            if dry_run:
                return report

            now = now_iso()
            for row in to_hide:
                conn.execute("UPDATE entities SET hidden = 1, updated_at = ? WHERE id = ?",
                             (now, row["id"]))
            for rel in to_retract:
                conn.execute("UPDATE relations SET status = 'retracted', updated_at = ? "
                             "WHERE id = ?", (now, rel["id"]))
            _meta_set(conn, owner, _META_LAST, {
                "version": REVALIDATE_VERSION, "at": now,
                "hidden": report["hidden"],
                "retracted": {r["id"]: r["status"] for r in to_retract},
            })
            _meta_set(conn, owner, _META_HIDDEN_EVER,
                      sorted(hidden_ever | set(report["hidden"])))
    except Exception as exc:  # noqa: BLE001 - a cleanup pass must never raise
        logger.debug("brain.entities: revalidate(%s) failed (%s)", owner, exc)
        report["errors"] += 1
    if report["hidden_count"] or report["retracted_count"]:
        logger.info("brain revalidate: hid %d entit(y/ies), retracted %d relation(s)",
                    report["hidden_count"], report["retracted_count"])
    return report


def revalidate_if_needed(owner: Any) -> Optional[Dict[str, Any]]:
    """Run :func:`revalidate` once per :data:`REVALIDATE_VERSION` for
    `owner` (a stored marker remembers it ran). None when it had already run
    for this version, or on error; the report otherwise."""
    owner = str(owner or "")
    if not owner:
        return None
    try:
        with db() as conn:
            if str(_meta_get(conn, owner, _META_VERSION, "")) == str(REVALIDATE_VERSION):
                return None
        report = revalidate(owner)
        if report.get("errors"):
            return report  # no marker: try again next time
        with db() as conn:
            _meta_set(conn, owner, _META_VERSION, str(REVALIDATE_VERSION))
        return report
    except Exception as exc:  # noqa: BLE001
        logger.debug("brain.entities: revalidate_if_needed(%s) failed (%s)", owner, exc)
        return None


def undo_revalidate(owner: Any) -> Dict[str, Any]:
    """Put back what the last :func:`revalidate` of `owner` changed: un-hide
    the entities it hid (unless merged since) and give each retracted
    relation its previous status (unless something changed it since)."""
    owner = str(owner or "")
    report: Dict[str, Any] = {"owner": owner, "unhidden": [], "unhidden_count": 0,
                              "restored": [], "restored_count": 0, "errors": 0}
    if not owner:
        return report
    try:
        with db() as conn:
            last = _meta_get(conn, owner, _META_LAST, {}) or {}
            now = now_iso()
            for entity_id in last.get("hidden") or []:
                cur = conn.execute(
                    "UPDATE entities SET hidden = 0, updated_at = ? WHERE id = ? AND owner = ? "
                    "AND hidden = 1 AND merged_into = ''", (now, entity_id, owner))
                if cur.rowcount:
                    report["unhidden"].append(entity_id)
            for rel_id, previous in (last.get("retracted") or {}).items():
                cur = conn.execute(
                    "UPDATE relations SET status = ?, updated_at = ? WHERE id = ? AND owner = ? "
                    "AND status = 'retracted'", (str(previous or "active"), now, rel_id, owner))
                if cur.rowcount:
                    report["restored"].append(rel_id)
            _meta_set(conn, owner, _META_LAST, {})
    except Exception as exc:  # noqa: BLE001
        logger.debug("brain.entities: undo_revalidate(%s) failed (%s)", owner, exc)
        report["errors"] += 1
    report["unhidden_count"] = len(report["unhidden"])
    report["restored_count"] = len(report["restored"])
    return report


# ---------------------------------------------------------------------------
# Profile / graph / stats
# ---------------------------------------------------------------------------


def _entity_name(entity_id: str) -> str:
    if not entity_id:
        return ""
    entity = get_entity(entity_id)
    return entity["name"] if entity else ""


def _fact_from_source(owner: str, source_ref: str) -> Optional[Dict[str, Any]]:
    """Best-effort resolution of a mention's source back to its text and
    validity window — None when that source is gone for good (forgotten,
    corrected away, suppressed, marked secret, a deleted personal memory or
    a deleted free note): a stale citation must cost the fact, not linger on
    the entity page. Unknown prefixes degrade to None too, never a stub."""
    if source_ref.startswith("mem:"):
        try:
            from src import memory_engine
        except Exception:  # noqa: BLE001
            return None
        item = memory_engine.get_item(source_ref[4:])
        if not item or item.get("suppressed") or item.get("sensitivity") == "secret":
            return None
        return {
            "source_ref": source_ref, "text": item.get("text", ""),
            "valid_from": item.get("valid_from") or "",
            "valid_until": item.get("valid_until") or "",
            "created_at": item.get("created_at") or "",
        }
    if source_ref.startswith("pmem:"):
        try:
            from src.memory import MemoryManager
            from src.constants import DATA_DIR
        except Exception:  # noqa: BLE001
            return None
        entry_id = source_ref[5:]
        try:
            for entry in MemoryManager(DATA_DIR).load_all():
                if str(entry.get("id")) == entry_id:
                    return {
                        "source_ref": source_ref, "text": entry.get("text", ""),
                        "valid_from": "", "valid_until": "",
                        "created_at": "",
                    }
        except Exception:  # noqa: BLE001
            return None
        return None
    if source_ref.startswith("note:"):
        # A free note round-trips nowhere: the file itself, if it is still
        # there, is the only copy of its text (Lot A's `notes` module, kept
        # optional the same way the vault export treats it).
        try:
            from src.brain import notes
        except Exception:  # noqa: BLE001
            return None
        path = source_ref[len("note:"):]
        try:
            note = notes.read_note(owner, path)
        except Exception:  # noqa: BLE001
            return None
        text = str(note.get("user_zone") or note.get("content") or "").strip()
        if not text:
            return None
        return {"source_ref": source_ref, "text": text, "valid_from": "", "valid_until": "",
                "created_at": ""}
    return None


def _fact_valid_at(fact: Dict[str, Any], instant: datetime) -> bool:
    valid_from = parse_iso(fact.get("valid_from"))
    if valid_from and instant < valid_from:
        return False
    valid_until = parse_iso(fact.get("valid_until"))
    if valid_until and instant > valid_until:
        return False
    return True


def _build_timeline(entity: Dict[str, Any], facts: List[Dict[str, Any]],
                    relations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    if entity.get("created_at"):
        events.append({"at": entity["created_at"], "kind": "created",
                       "text": f"Entity created: {entity.get('name', '')}", "source_ref": ""})
    for fact in facts:
        if fact.get("created_at"):
            events.append({"at": fact["created_at"], "kind": "mention",
                           "text": fact.get("text", ""), "source_ref": fact.get("source_ref", "")})
    for rel in relations:
        label = rel.get("dst_name") or rel.get("dst_value") or ""
        text = f"{rel['rel']} {label}".strip()
        if rel.get("valid_from"):
            events.append({"at": rel["valid_from"], "kind": "valid_from", "text": text,
                           "source_ref": rel.get("id", "")})
        if rel.get("valid_until"):
            events.append({"at": rel["valid_until"], "kind": "valid_until", "text": text,
                           "source_ref": rel.get("id", "")})
    # Newest first, de-duplicated: a fact re-mentioned by more than one
    # source, or a relation window read twice (open then closed), must not
    # print the same line twice.
    seen: set = set()
    deduped: List[Dict[str, Any]] = []
    for event in sorted(events, key=lambda e: e["at"], reverse=True):
        key = (event["at"], event["kind"], event["text"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(event)
    return deduped


def profile(entity_id: Any, *, as_of: Any = None) -> Dict[str, Any]:
    entity = get_entity(entity_id)
    if not entity:
        raise BrainEntityError(f"unknown entity {entity_id!r}")
    owner = entity["owner"]
    instant = _coerce_dt(as_of) if as_of is not None else datetime.now(timezone.utc)

    facts: List[Dict[str, Any]] = []
    for source_ref in sources_for(entity["id"]):
        fact = _fact_from_source(owner, source_ref)
        if not fact:
            continue
        fact["valid_now"] = _fact_valid_at(fact, instant)
        facts.append(fact)
    facts.sort(key=lambda f: f.get("created_at") or "")

    all_rels = list_relations(owner, entity_id=entity["id"], include_closed=True)
    relations: List[Dict[str, Any]] = []
    history: List[Dict[str, Any]] = []
    for rel in all_rels:
        enriched = dict(rel)
        enriched["src_name"] = _entity_name(rel["src"])
        enriched["dst_name"] = _entity_name(rel["dst"]) if rel["dst"] else ""
        enriched["valid_at"] = _relation_window_covers(rel, instant)
        relations.append(enriched)
        if rel["status"] != "active":
            history.append(enriched)

    return {
        "entity": entity,
        "facts": facts,
        "relations": relations,
        "history": history,
        "timeline": _build_timeline(entity, facts, relations),
        "summary": entity.get("summary", ""),
        "summary_sources": entity.get("summary_sources", []),
    }


def graph(owner: Any, *, limit: int = 500) -> Dict[str, Any]:
    owner = str(owner or "")
    entities = list_entities(owner, limit=limit, include_hidden=False)
    id_set = {e["id"] for e in entities}
    nodes = [{"id": f"ent:{e['id']}", "label": e["name"], "kind": e["type"], "degree": 0}
            for e in entities]
    degree: Dict[str, int] = {}
    edges: List[Dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    for rel in list_relations(owner, include_closed=True):
        if rel["src"] not in id_set or not rel["dst"] or rel["dst"] not in id_set:
            continue
        edges.append({
            "from": f"ent:{rel['src']}", "to": f"ent:{rel['dst']}", "kind": rel["rel"],
            "valid_now": rel["status"] == "active" and _relation_window_covers(rel, now),
        })
        degree[rel["src"]] = degree.get(rel["src"], 0) + 1
        degree[rel["dst"]] = degree.get(rel["dst"], 0) + 1
    for node in nodes:
        node["degree"] = degree.get(node["id"].split(":", 1)[1], 0)
    return {"nodes": nodes, "edges": edges}


def stats(owner: Any) -> Dict[str, Any]:
    owner = str(owner or "")
    with db() as conn:
        total = conn.execute(
            "SELECT COUNT(*) c FROM entities WHERE owner = ? AND merged_into = '' AND hidden = 0",
            (owner,),
        ).fetchone()["c"]
        hidden = conn.execute(
            "SELECT COUNT(*) c FROM entities WHERE owner = ? AND merged_into = '' AND hidden = 1",
            (owner,),
        ).fetchone()["c"]
        by_type_rows = conn.execute(
            "SELECT type, COUNT(*) c FROM entities WHERE owner = ? AND merged_into = '' "
            "AND hidden = 0 GROUP BY type", (owner,),
        ).fetchall()
        relations_active = conn.execute(
            "SELECT COUNT(*) c FROM relations WHERE owner = ? AND status = 'active'", (owner,)
        ).fetchone()["c"]
        relations_total = conn.execute(
            "SELECT COUNT(*) c FROM relations WHERE owner = ?", (owner,)
        ).fetchone()["c"]
    return {
        "entities": total, "hidden": hidden,
        "by_type": {row["type"]: row["c"] for row in by_type_rows},
        "relations_active": relations_active, "relations_total": relations_total,
    }


__all__ = [
    "TYPES", "KNOWN_RELATIONS", "FUNCTIONAL_RELATIONS", "SELF_WORDS", "BrainEntityError",
    "FUNCTION_WORDS", "is_function_word", "clean_name", "valid_entity_name",
    "proper_noun_in_source", "canonical_relation", "REVALIDATE_VERSION", "revalidate",
    "revalidate_if_needed", "undo_revalidate",
    "upsert_entity", "get_entity", "update_entity", "set_hidden", "list_entities",
    "merge_entities", "self_entity", "add_relation", "list_relations", "add_mention",
    "mentions_for", "sources_for", "repoint_source", "forget_source",
    "entities_in_text", "profile", "graph", "stats",
]
