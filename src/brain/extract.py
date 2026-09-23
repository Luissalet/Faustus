"""brain/extract.py — turning sentences already in memory into a graph.

Two passes, cheapest first, exactly the "deterministic first" rule this
whole feature follows:

1. :func:`extract_source` — a RULE pass over one piece of text: known
   entities (already in the graph) matched by name/alias, brand-new
   multi-word proper nouns discovered via
   ``memory_grounding.extract_specifics``, and a closed set of
   subject-predicate-object patterns (extending the technique
   ``memory_conflicts._extract_svo`` already uses, but keeping the
   ORIGINAL casing of the sentence — a conflict comparison can afford to
   lowercase everything, an entity name cannot). 100% deterministic,
   stdlib only, safe to run inline.

2. :func:`extract_pending` — the background sweep over everything the rule
   pass has not yet seen (memory items, personal memories, and free vault
   notes when Lot A's ``notes`` module is importable), incremental via
   ``extraction_log`` text hashes. When ``brain_llm_extraction`` is on and a
   utility model endpoint resolves, it ALSO asks that model for entities
   and relations it can find — but keeps only the ones the rule pass could
   have found too if it had looked harder: an entity only if its name or an
   alias actually occurs in the source text, a relation only if both ends
   were kept (or the destination is a literal value) AND both occur in the
   SAME source — the model sees a batch of unrelated sources, and one end
   from each is how a relation nobody stated gets invented. Such a relation
   cites only that source, keeps a model date only when that source's own
   text yields the same day through :mod:`temporal`, and — being a model's
   reading — never closes a rule-derived relation. A model that names
   something never mentioned in the text is simply ignored, not stored —
   the grounding check IS the safety net that lets this pass run
   unsupervised in the background.

Nothing here ever raises out to a caller on the chat hot path: this module
is only ever invoked from a background sweep or an explicit "extract now"
action, never from the turn itself.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.brain import temporal
from src.brain.db import db, fold, now_iso, parse_iso, register_schema, sha
from src.brain.entities import (
    TYPES,
    add_mention,
    add_relation,
    entities_in_text,
    self_entity,
    upsert_entity,
)

logger = logging.getLogger(__name__)

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS extraction_log (
        owner        TEXT NOT NULL DEFAULT '',
        source_ref   TEXT NOT NULL DEFAULT '',
        hash         TEXT NOT NULL DEFAULT '',
        method       TEXT NOT NULL DEFAULT 'rule',
        extracted_at TEXT NOT NULL DEFAULT '',
        PRIMARY KEY(owner, source_ref, method)
    )
    """,
)
register_schema("brain_extract", _SCHEMA)

# ---------------------------------------------------------------------------
# Relation vocabulary — surface phrase (ES/EN) -> canonical relation key.
# Longest-first so "esta ubicado en" is tried before a shorter predicate
# could swallow part of it. Deliberately literal, same discipline
# `memory_conflicts._PREDICATES` uses: no predicate here ever guesses a
# subject from wording, it only matches a fixed phrase.
# ---------------------------------------------------------------------------

_REL_PHRASES: Tuple[Tuple[str, str], ...] = (
    ("esta ubicado en", "located_in"), ("esta situado en", "located_in"),
    ("is located in", "located_in"), ("located in", "located_in"),
    ("es miembro de", "member_of"), ("member of", "member_of"),
    ("forma parte de", "part_of"), ("es parte de", "part_of"), ("part of", "part_of"),
    ("depende de", "depends_on"), ("depends on", "depends_on"),
    ("estudio en", "studied_at"), ("studied at", "studied_at"),
    ("relacionado con", "related_to"), ("related to", "related_to"),
    ("trabaja en", "works_at"), ("works at", "works_at"),
    ("trabaja sobre", "works_on"), ("works on", "works_on"),
    ("vive en", "lives_in"), ("lives in", "lives_in"),
    ("conoce a", "knows"), ("conoce", "knows"), ("knows", "knows"),
    ("es dueno de", "owns"), ("posee", "owns"), ("owns", "owns"),
    ("creo", "created"), ("created", "created"),
    ("prefiere", "prefers"), ("prefers", "prefers"),
    ("usa", "uses"), ("uses", "uses"),
    ("es una", "is_a"), ("es un", "is_a"), ("is an", "is_a"), ("is a", "is_a"),
    # Base (non-third-person) verb forms, so "I use X"/"yo uso X" match too —
    # the third-person forms above are tried first where both could apply.
    ("trabajo en", "works_at"), ("work at", "works_at"),
    ("vivo en", "lives_in"), ("live in", "lives_in"),
    ("uso", "uses"), ("use", "uses"),
    ("know", "knows"), ("conozco", "knows"),
    ("prefiero", "prefers"), ("prefer", "prefers"),
)
_REL_ORDER = tuple(sorted(_REL_PHRASES, key=lambda pair: len(pair[0]), reverse=True))
_REL_REGEXES: Tuple[Tuple[re.Pattern, str], ...] = tuple(
    (re.compile(r"\b" + r"\s+".join(re.escape(w) for w in phrase.split()) + r"\b", re.IGNORECASE), rel)
    for phrase, rel in _REL_ORDER
)

_ARTICLE_RE = re.compile(r"^(?:el|la|los|las|un|una|unos|unas|the|a|an)\s+", re.IGNORECASE)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")


def _split_sentences(text: str) -> List[str]:
    return [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]


def _strip_leading_article(phrase: str) -> str:
    return _ARTICLE_RE.sub("", phrase.strip()).strip()


def _leading_proper_phrase(phrase: str) -> str:
    """The longest PREFIX of `phrase` made of capitalized words — "Python"
    out of "Python for everything", "Cordera Labs" out of "Cordera Labs
    yesterday". A relation's object is "everything after the predicate", not
    a parsed noun phrase, so this is what keeps a whole sentence tail from
    becoming one entity name."""
    out: List[str] = []
    for word in phrase.split():
        core = word.strip(" .,;:!?\"'")
        if core and core[0].isalpha() and core[0].isupper():
            out.append(word.strip(" .,;:!?\"'"))
            continue
        break
    return " ".join(out)


def _extract_relations(text: str) -> List[Tuple[str, str, str]]:
    """(subject, canonical_rel, object) triples, ORIGINAL casing preserved
    (unlike `memory_conflicts._extract_svo`, which lowercases for
    comparison) — the whole point here is that a capitalised subject/object
    is the signal a NEW entity name exists to be created from."""
    out: List[Tuple[str, str, str]] = []
    for sentence in _split_sentences(text):
        for pattern, rel in _REL_REGEXES:
            match = pattern.search(sentence)
            if not match:
                continue
            subject = _strip_leading_article(sentence[:match.start()].strip(" ,;:-"))
            obj = sentence[match.end():].strip(" ,;:.!?-")
            if subject and obj:
                out.append((subject, rel, obj))
            break  # first (longest) matching predicate per sentence only
    return out


def _resolve_entity_ref(owner: str, phrase: str, *, project: str,
                        allow_create: bool) -> Tuple[Optional[Dict[str, Any]], str]:
    """(entity, "") when `phrase` resolves to one (self, known, or newly
    created); (None, literal_text) when it is kept as a plain dst_value."""
    phrase = _strip_leading_article(phrase).strip(" .,;:!?\"'")
    if not phrase:
        return None, ""
    if fold(phrase) in ("yo", "me", "i"):
        return self_entity(owner), ""
    matches = entities_in_text(owner, phrase, limit=1)
    if matches:
        return matches[0], ""
    if allow_create:
        candidate = _leading_proper_phrase(phrase)
        if candidate and len(candidate) <= 80:
            return upsert_entity(owner, candidate, type="other", project=project), ""
    return None, phrase


def extract_source(owner: Any, source_ref: Any, text: Any, *, project: str = "",
                   created_at: Optional[str] = None) -> Dict[str, Any]:
    """Deterministic extraction over one piece of text. Records mentions and
    an `extraction_log` row (method="rule") so `extract_pending` can skip it
    next time unless the text changes."""
    owner = str(owner or "")
    source_ref = str(source_ref or "")
    text = str(text or "")
    if not owner or not source_ref or not text.strip():
        return {"entities": [], "relations": []}

    result_entities: List[Dict[str, Any]] = []
    result_relations: List[Dict[str, Any]] = []
    seen_ids: set = set()

    def _remember(entity: Optional[Dict[str, Any]]) -> None:
        if entity and entity["id"] not in seen_ids:
            seen_ids.add(entity["id"])
            result_entities.append(entity)

    known = entities_in_text(owner, text, limit=16)
    known_folds = {fold(e["name"]) for e in known}
    for e in known:
        known_folds |= {fold(a) for a in (e.get("aliases") or [])}
    for entity in known:
        add_mention(owner, entity["id"], source_ref)
        _remember(entity)

    try:
        from src.memory_grounding import extract_specifics
        specifics = extract_specifics(text)
    except Exception:  # noqa: BLE001 - a broken lint helper costs discovery, not the pass
        specifics = []
    for spec in specifics:
        if spec.get("type") != "proper_noun":
            continue
        name = str(spec.get("value") or "").strip()
        if not name or fold(name) in known_folds:
            continue
        try:
            entity = upsert_entity(owner, name, type="other", project=project)
        except Exception:  # noqa: BLE001
            continue
        known_folds.add(fold(entity["name"]))
        known_folds |= {fold(a) for a in (entity.get("aliases") or [])}
        add_mention(owner, entity["id"], source_ref)
        _remember(entity)

    # Relative phrases and bare months resolve against when the source was
    # WRITTEN ("hasta marzo" in an October memory), not when this runs.
    window = temporal.parse_temporal(text, now=parse_iso(created_at) if created_at else None)
    valid_from, valid_until = window.get("valid_from"), window.get("valid_until")

    for subject, rel_key, obj in _extract_relations(text):
        src_entity, _ = _resolve_entity_ref(owner, subject, project=project, allow_create=True)
        if not src_entity:
            continue
        dst_entity, dst_literal = _resolve_entity_ref(owner, obj, project=project, allow_create=True)
        if not dst_entity and not dst_literal:
            continue
        try:
            relation = add_relation(
                owner, src_entity["id"], rel_key,
                dst_id=(dst_entity["id"] if dst_entity else None),
                dst_value=("" if dst_entity else dst_literal),
                valid_from=valid_from, valid_until=valid_until,
                evidence=(source_ref,), confidence=0.6, method="rule", project=project,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("brain.extract: add_relation failed (%s)", exc)
            continue
        add_mention(owner, src_entity["id"], source_ref)
        if dst_entity:
            add_mention(owner, dst_entity["id"], source_ref)
        _remember(src_entity)
        _remember(dst_entity)
        result_relations.append(relation)

    try:
        _record_extraction(owner, source_ref, sha(text), "rule", created_at)
    except Exception as exc:  # noqa: BLE001
        logger.debug("brain.extract: could not record extraction_log (%s)", exc)

    return {"entities": result_entities, "relations": result_relations}


# ---------------------------------------------------------------------------
# extraction_log bookkeeping
# ---------------------------------------------------------------------------


def _extraction_hash(owner: str, source_ref: str, method: str) -> Optional[str]:
    with db() as conn:
        row = conn.execute(
            "SELECT hash FROM extraction_log WHERE owner = ? AND source_ref = ? AND method = ?",
            (owner, source_ref, method),
        ).fetchone()
    return row["hash"] if row else None


def _record_extraction(owner: str, source_ref: str, text_hash: str, method: str,
                       extracted_at: Optional[str] = None) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO extraction_log (owner, source_ref, hash, method, extracted_at) "
            "VALUES (?,?,?,?,?) ON CONFLICT(owner, source_ref, method) "
            "DO UPDATE SET hash = excluded.hash, extracted_at = excluded.extracted_at",
            (owner, source_ref, text_hash, method, extracted_at or now_iso()),
        )


# ---------------------------------------------------------------------------
# Sources for the background sweep
# ---------------------------------------------------------------------------


def _gather_sources(owner: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []

    try:
        from src import memory_engine
        for item in memory_engine.list_items(owner=owner, status="active", limit=2000):
            if item.get("sensitivity") == "secret":
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            out.append({"source_ref": f"mem:{item['id']}", "text": text,
                       "created_at": item.get("created_at") or "",
                       "project": item.get("project") or ""})
    except Exception as exc:  # noqa: BLE001
        logger.debug("brain.extract: memory_engine unavailable (%s)", exc)

    try:
        from src.constants import DATA_DIR
        from src.memory import MemoryManager
        for entry in MemoryManager(DATA_DIR).load(owner):
            text = str(entry.get("text") or "").strip()
            if not text:
                continue
            out.append({"source_ref": f"pmem:{entry.get('id')}", "text": text,
                       "created_at": "", "project": ""})
    except Exception as exc:  # noqa: BLE001
        logger.debug("brain.extract: personal memory unavailable (%s)", exc)

    try:
        from src.brain import notes  # Lot A, optional — tolerate its absence
    except Exception:
        notes = None
    if notes is not None:
        try:
            tree = notes.tree(owner)
            for note in tree.get("notes", []):
                if note.get("source") or not str(note.get("path") or "").startswith("Notes/"):
                    continue
                full = notes.read_note(owner, note["path"])
                text = str(full.get("user_zone") or full.get("content") or "").strip()
                if not text:
                    continue
                out.append({"source_ref": f"note:{note['path']}", "text": text,
                           "created_at": "", "project": ""})
        except Exception as exc:  # noqa: BLE001
            logger.debug("brain.extract: vault notes unavailable (%s)", exc)

    return out


def _get_setting(key: str, default: Any) -> Any:
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:  # noqa: BLE001
        return default


# ---------------------------------------------------------------------------
# LLM pass — background only, grounding-checked, fails closed
# ---------------------------------------------------------------------------


def _parse_json_object(raw: Any) -> Optional[Dict[str, Any]]:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except (TypeError, ValueError):
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
        return data if isinstance(data, dict) else None
    except (TypeError, ValueError):
        return None


def _build_llm_prompt(batch: Sequence[Dict[str, Any]]) -> str:
    lines = [
        "Extract entities and relations ACTUALLY PRESENT in the text below.",
        "Return ONLY a JSON object, no prose, shaped exactly like:",
        '{"entities":[{"name":"...","type":"person|project|organization|place|'
        'tool|concept|event|other","aliases":[]}],'
        '"relations":[{"src":"...","rel":"...","dst":"...","valid_from":null,"valid_until":null}]}',
        "Every entity name/alias and every relation src/dst MUST be words that "
        "literally occur in the text below (or a plain quoted value for dst). "
        "Never invent a name that is not in the text.",
        "",
    ]
    for src in batch:
        lines.append(f"[{src['source_ref']}] {src['text']}")
    return "\n".join(lines)


def _occurs(term: str, text_fold: str) -> bool:
    return bool(term) and re.search(rf"\b{re.escape(term)}\b", text_fold) is not None


def _same_day(a: Any, b: Any) -> bool:
    da, db_ = parse_iso(a), parse_iso(b)
    return bool(da and db_) and da.date() == db_.date()


def _pick_grounding_source(sources: Sequence[Dict[str, Any]], model_from: Any,
                           model_until: Any) -> Tuple[Dict[str, Any], Optional[str], Optional[str]]:
    """The one source a model relation will cite, and the dates it keeps.

    A model date survives only when that source's OWN text yields the same
    day through the deterministic parser — a date the model inferred,
    rounded or invented is dropped (``None``), never stored. Among several
    grounding sources the one supporting the most model dates wins, ties
    to the first."""
    best: Tuple[int, int, Dict[str, Any], Optional[str], Optional[str]] = (-1, 0, sources[0], None, None)
    for index, source in enumerate(sources):
        created = source.get("created_at")
        window = temporal.parse_temporal(source["text"],
                                         now=parse_iso(created) if created else None)
        valid_from = str(model_from) if model_from and _same_day(model_from, window.get("valid_from")) \
            else None
        valid_until = str(model_until) if model_until and \
            _same_day(model_until, window.get("valid_until")) else None
        score = int(valid_from is not None) + int(valid_until is not None)
        if score > best[0]:
            best = (score, index, source, valid_from, valid_until)
    return best[2], best[3], best[4]


async def _extract_llm_batch(owner: str, batch: Sequence[Dict[str, Any]],
                             report: Dict[str, Any]) -> bool:
    """One background model call over `batch`. Returns True iff the model
    actually answered (used for `report['llm_used']`), regardless of how
    many — possibly zero — entities/relations survived grounding."""
    try:
        from src.endpoint_resolver import resolve_endpoint
        url, model, headers = resolve_endpoint("utility", owner=owner)
    except Exception:  # noqa: BLE001
        return False
    if not url or not model:
        return False

    try:
        from src.llm_core import llm_call_async
        raw = await llm_call_async(
            url=url, model=model, messages=[{"role": "user", "content": _build_llm_prompt(batch)}],
            headers=headers, temperature=0.1, max_tokens=900, timeout=45,
            max_retries=1, workload="background",
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("brain.extract: llm call failed (%s)", exc)
        report["errors"] += 1
        return False
    if isinstance(raw, tuple):
        raw = raw[0]

    data = _parse_json_object(raw)
    for src in batch:
        _record_extraction(owner, src["source_ref"], sha(src["text"]), "llm")
    if not isinstance(data, dict):
        return True

    batch_fold = fold(" \n ".join(src["text"] for src in batch))
    source_folds = [fold(src["text"]) for src in batch]
    kept: Dict[str, Dict[str, Any]] = {}
    # entity id -> every folded spelling the model used for it (name +
    # aliases) that literally occurs somewhere in the batch
    spellings: Dict[str, set] = {}

    for raw_entity in data.get("entities") or []:
        if not isinstance(raw_entity, dict):
            continue
        name = str(raw_entity.get("name") or "").strip()
        if not name:
            continue
        aliases = [str(a).strip() for a in (raw_entity.get("aliases") or []) if str(a).strip()]
        candidate_folds = [fold(name)] + [fold(a) for a in aliases]
        if not any(cf and re.search(rf"\b{re.escape(cf)}\b", batch_fold) for cf in candidate_folds):
            continue  # not grounded in the batch text — drop it, never store
        etype = str(raw_entity.get("type") or "other").strip().lower()
        if etype not in TYPES:
            etype = "other"
        try:
            entity = upsert_entity(owner, name, type=etype, aliases=aliases)
        except Exception:  # noqa: BLE001
            continue
        kept[fold(name)] = entity
        for alias in aliases:
            kept[fold(alias)] = entity
        spellings.setdefault(entity["id"], set()).update(
            cf for cf in candidate_folds if cf and _occurs(cf, batch_fold))
        for src in batch:
            if re.search(rf"\b{re.escape(fold(name))}\b", fold(src["text"])):
                add_mention(owner, entity["id"], src["source_ref"])
        report["entities"] += 1

    for raw_rel in data.get("relations") or []:
        if not isinstance(raw_rel, dict):
            continue
        src_name = str(raw_rel.get("src") or "").strip()
        dst_name = str(raw_rel.get("dst") or "").strip()
        rel_key = fold(raw_rel.get("rel")).replace(" ", "_")
        if not src_name or not dst_name or not rel_key:
            continue
        src_entity = kept.get(fold(src_name))
        if not src_entity:
            continue  # source end was not kept -> drop the relation, never guess
        dst_entity = kept.get(fold(dst_name))
        dst_value = "" if dst_entity else dst_name
        src_terms = {fold(src_name)} | spellings.get(src_entity["id"], set())
        dst_terms = ({fold(dst_name)} | spellings.get(dst_entity["id"], set())) if dst_entity \
            else {fold(dst_name)}
        # BOTH ends must occur in ONE source: a batch mixes unrelated
        # sources, and an end from each is exactly how a model invents a
        # relation nobody ever stated.
        grounding = [i for i, text_fold in enumerate(source_folds)
                     if any(_occurs(t, text_fold) for t in src_terms)
                     and any(_occurs(t, text_fold) for t in dst_terms)]
        if not grounding:
            continue
        source, valid_from, valid_until = _pick_grounding_source(
            [batch[i] for i in grounding], raw_rel.get("valid_from"), raw_rel.get("valid_until"))
        try:
            add_relation(
                owner, src_entity["id"], rel_key,
                dst_id=(dst_entity["id"] if dst_entity else None), dst_value=dst_value,
                valid_from=valid_from, valid_until=valid_until,
                evidence=(source["source_ref"],),
                confidence=0.55, method="llm",
            )
            report["relations"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.debug("brain.extract: llm add_relation failed (%s)", exc)
            report["errors"] += 1
    return True


async def extract_pending(owner: Any, *, limit: Optional[int] = None, budget_s: float = 20.0,
                          use_llm: Optional[bool] = None) -> Dict[str, Any]:
    """The background sweep: rule pass over everything changed since its
    last extraction, then (optionally) one utility-model pass over what the
    LLM extraction log has not seen yet. Never raises."""
    start = time.monotonic()
    owner = str(owner or "")
    report: Dict[str, Any] = {
        "processed": 0, "entities": 0, "relations": 0, "skipped": 0,
        "errors": 0, "llm_used": False,
    }
    if not owner:
        return report

    try:
        if not _get_setting("brain_entity_extraction", True):
            return report

        sources = _gather_sources(owner)
        pending = []
        for src in sources:
            text_hash = sha(src["text"])
            if _extraction_hash(owner, src["source_ref"], "rule") == text_hash:
                report["skipped"] += 1
                continue
            pending.append(src)

        cap = len(pending) if limit is None else max(0, int(limit))
        for src in pending[:cap]:
            if time.monotonic() - start > budget_s:
                break
            try:
                result = extract_source(owner, src["source_ref"], src["text"],
                                        project=src.get("project", ""),
                                        created_at=src.get("created_at") or None)
                report["entities"] += len(result["entities"])
                report["relations"] += len(result["relations"])
                report["processed"] += 1
            except Exception as exc:  # noqa: BLE001
                logger.debug("brain.extract: extract_source failed for %s (%s)",
                            src.get("source_ref"), exc)
                report["errors"] += 1

        use_llm_flag = bool(_get_setting("brain_llm_extraction", True)) if use_llm is None else bool(use_llm)
        if use_llm_flag and time.monotonic() - start <= budget_s:
            llm_sources = [s for s in sources
                          if _extraction_hash(owner, s["source_ref"], "llm") != sha(s["text"])]
            batch_size = max(1, int(_get_setting("brain_llm_extraction_batch", 12)))
            for i in range(0, len(llm_sources), batch_size):
                if time.monotonic() - start > budget_s:
                    break
                if await _extract_llm_batch(owner, llm_sources[i:i + batch_size], report):
                    report["llm_used"] = True
    except Exception as exc:  # noqa: BLE001 - a background sweep must never raise
        logger.debug("brain.extract: extract_pending failed (%s)", exc)
        report["errors"] += 1
    return report


def count_pending(owner: Any) -> int:
    """How many sources have not been through the rule pass since their text
    last changed — the cheap number `GET /api/brain/status` shows, without
    running the extraction itself. Same source list and hash comparison
    `extract_pending` uses; never raises."""
    owner = str(owner or "")
    if not owner:
        return 0
    try:
        sources = _gather_sources(owner)
    except Exception as exc:  # noqa: BLE001 - a status number must not 500
        logger.debug("brain.extract: count_pending could not gather sources (%s)", exc)
        return 0
    count = 0
    for src in sources:
        try:
            if _extraction_hash(owner, src["source_ref"], "rule") != sha(src["text"]):
                count += 1
        except Exception:  # noqa: BLE001
            continue
    return count


__all__ = ["extract_source", "extract_pending", "count_pending"]
