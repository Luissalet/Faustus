"""src/memory_conflicts.py — contradiction tracking for the learned memory
store (``src/memory_engine.py``).

``src/context_engine/conflicts.py`` already answers "which of these two
context candidates wins" on the turn path, with a deliberately blunt,
no-LLM polarity test. This module answers a narrower, earlier question for
the memory STORE itself: when a brand-new item is written, does it disagree
with something already sitting there as active ("the project uses Python"
next to "the project uses Rust"; "Luis prefers tabs" next to "Luis does not
prefer tabs")? Nothing before this module noticed that at write time, so
both stayed active forever and the model got handed both without being told
they disagreed.

Two ways a pair is flagged, both deterministic and both reusing
``context_engine.conflicts``'s own vocabulary where they overlap:

* ``negation`` — the same blunt negation/affirmation markers
  (``conflicts._NEGATIONS`` / ``conflicts._AFFIRMATIONS``) appear on exactly
  one side of an otherwise near-identical claim ("Luis prefers tabs" vs.
  "Luis does not prefer tabs"). A single shared polarity word is not enough
  on its own here — unlike ``conflicts.py``'s turn-path test, most memory
  sentences never carry an affirmation word at all ("Luis prefers tabs" has
  none), so this module keys off the ASYMMETRY of negation markers between
  otherwise-matching cores, not off both sides carrying an opposite marker.
* ``same_subject_different_value`` — the two texts share an explicit
  subject and one of a small, closed set of "single-valued" predicates
  (a project cannot use two languages, a person cannot have two "lives in"
  answers) but give a different object.

A third, ADVISORY way (``advise``, background only): pairs the two rules
above say nothing about, but that share enough vocabulary to be about the
same thing, may be put to a typed decision (``src/typed_decision.py``:
contradict / update / compatible, read from one prefill). A confident
"contradict" or "update" is stored as a ``suggested`` row the owner can
look at — never ``open`` (so never ranked down), never resolved.

What the write-path detection never does: call a model, guess a subject from wording
alone (it is extracted with an explicit, closed predicate list, the same
"never inferred" discipline ``conflicts.py`` applies to its own ``subject``
meta key), or touch either item's stored text. A conflict is a row in its
own table; resolving it (`resolve`) is what the owner does explicitly.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.context_engine.conflicts import _AFFIRMATIONS, _NEGATIONS
from src.memory import get_text_similarity, tokenize

logger = logging.getLogger(__name__)

REASONS: Tuple[str, ...] = ("negation", "same_subject_different_value", "model_suggested")
# MEM-TEMPORAL: "superseded" is a resolution like "kept_new"/"kept_old", but
# reached automatically (see `_maybe_supersede`) rather than by an explicit
# owner choice, and it is REVERSIBLE (`unsupersede`) — the other three never
# are, since they `forget()` an item outright.
#: "suggested" is the ADVISORY status of a pair only a typed decision
#: flagged (`advise`): listed on request (``status=suggested``), never
#: ranked down (`open_conflict_for` reads ``open`` rows only), never resolved
#: automatically — the owner may resolve it like an open one.
STATUSES: Tuple[str, ...] = ("open", "kept_new", "kept_old", "kept_both", "superseded", "suggested")

#: LEGACY: rows written before the `supersede` column existed carried the
#: OLD item's previous `valid_until` after this delimiter inside `detail`.
#: `detail` was cut to 500 chars AFTER that value was appended, so a long
#: detail lost it; new rows store it in `supersede` (JSON, never truncated)
#: and this marker is only ever READ, for rows written the old way.
_PREV_MARK = "␞"

#: The "cannot both be true" predicates this module will pattern-match on,
#: English and Spanish. Deliberately small and literal — a predicate list
#: that tried to be clever would start inferring subjects from wording,
#: which is exactly what the module docstring says this never does. Sorted
#: longest-first below so "is written in" is tried before a shorter
#: predicate could swallow part of it.
_PREDICATES: Tuple[str, ...] = (
    "uses", "is written in", "prefers", "lives in", "works at", "works as",
    "default is", "usa", "prefiere", "vive en", "trabaja en", "es",
)
_PRED_ORDER: Tuple[str, ...] = tuple(sorted(_PREDICATES, key=len, reverse=True))

#: Above this Jaccard similarity, two texts are the same statement said
#: twice — dedupe territory, not a conflict. Below it, texts are unrelated.
NEAR_DUPLICATE_SIMILARITY = 0.92
_CORE_SIMILARITY = 0.6

#: How much an open conflict's OLDER item is discounted in ranking
#: (`memory_engine.public_item`). The item is never hidden, only outranked.
RANKING_PENALTY = 0.5

#: How many existing items a fresh item is compared against (top-k by the
#: vector store when available, else token overlap). Pair count would
#: otherwise be O(n) DB comparisons for every single write.
DEFAULT_TOP_K = 8

_AUX_WORDS = frozenset({"do", "does", "did", "is", "are", "was", "were"})

_TABLE = "memory_conflicts"
_COLUMNS: Tuple[str, ...] = (
    "id", "owner", "new_id", "old_id", "reason", "detail", "status",
    "created_at", "resolved_at", "supersede",
)
_DB_LOCK = threading.RLock()

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id           TEXT PRIMARY KEY,
    owner        TEXT NOT NULL DEFAULT '',
    new_id       TEXT NOT NULL DEFAULT '',
    old_id       TEXT NOT NULL DEFAULT '',
    reason       TEXT NOT NULL DEFAULT '',
    detail       TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'open',
    created_at   TEXT NOT NULL DEFAULT '',
    resolved_at  TEXT NOT NULL DEFAULT '',
    supersede    TEXT NOT NULL DEFAULT ''
)
"""
#: Additive columns for tables created before they existed.
_ADDED_COLUMNS: Tuple[Tuple[str, str], ...] = (
    ("supersede", "TEXT NOT NULL DEFAULT ''"),
)
_INDEXES: Tuple[str, ...] = (
    f"CREATE INDEX IF NOT EXISTS idx_memconf_owner_status ON {_TABLE}(owner, status)",
    f"CREATE INDEX IF NOT EXISTS idx_memconf_old ON {_TABLE}(old_id, status)",
    f"CREATE INDEX IF NOT EXISTS idx_memconf_new ON {_TABLE}(new_id, status)",
)


class MemoryConflictError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Storage — the SAME sqlite file as memory_engine.py, its own table, opened
# and migrated the same additive way (`CREATE TABLE IF NOT EXISTS` on every
# connection, cheap and idempotent — following memory_engine._migrate_schema's
# "never destroy an existing row" discipline without needing its own
# migration list, since this table has had only one shape).
# ---------------------------------------------------------------------------


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.Error:
        pass
    conn.execute(_SCHEMA)
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({_TABLE})")}
    for name, ddl in _ADDED_COLUMNS:
        if name not in existing:
            try:
                conn.execute(f"ALTER TABLE {_TABLE} ADD COLUMN {name} {ddl}")
            except sqlite3.OperationalError:  # a concurrent connection added it first
                pass
    for stmt in _INDEXES:
        conn.execute(stmt)
    conn.commit()
    return conn


@contextlib.contextmanager
def _db():
    from src.memory_engine import db_path

    with _DB_LOCK:
        conn = _connect(db_path())
        try:
            yield conn
            conn.commit()
        finally:
            with contextlib.suppress(Exception):
                conn.close()


def _iso(dt: Optional[datetime] = None) -> str:
    dt = dt or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    out = {c: row[c] for c in _COLUMNS}
    out["supersede"] = _load_supersede(out.get("supersede"))
    return out


def _load_supersede(raw: Any) -> Dict[str, Any]:
    """The structured record of what a supersede changed (see
    `_apply_supersede`), ``{}`` for rows that never superseded anything."""
    if isinstance(raw, dict):
        return raw
    try:
        data = json.loads(str(raw or "") or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# Detection — text logic only, no I/O
# ---------------------------------------------------------------------------


def _norm_text(text: Any) -> str:
    return " ".join(str(text or "").split()).casefold()


def _near_duplicate(a: str, b: str) -> bool:
    if _norm_text(a) == _norm_text(b):
        return True
    try:
        return get_text_similarity(a, b) >= NEAR_DUPLICATE_SIMILARITY
    except Exception:  # noqa: BLE001 - a detector may never break its caller
        return False


def _has_negation(words: set) -> bool:
    return bool(words & _NEGATIONS)


def _has_affirmation(words: set) -> bool:
    return bool(words & _AFFIRMATIONS)


def _core_tokens(text: str) -> set:
    """Tokens with auxiliaries and negation markers removed, light suffix
    stripping so "prefers"/"prefer" compare equal. This is deliberately a
    much cruder normalisation than a real stemmer — it exists only to see
    past "does not" / verb agreement, not to understand the sentence."""
    out = set()
    for tok in tokenize(text.lower()):
        tok = tok.strip()
        if not tok or tok in _AUX_WORDS or tok in _NEGATIONS:
            continue
        if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss"):
            tok = tok[:-1]
        out.add(tok)
    return out


def _negation_conflict(a: str, b: str) -> Optional[str]:
    """Reason 1: an otherwise-matching claim with a negation marker on
    exactly one side. Returns a detail string, or None."""
    words_a = set(tokenize(a.lower()))
    words_b = set(tokenize(b.lower()))
    neg_a, neg_b = _has_negation(words_a), _has_negation(words_b)
    if neg_a == neg_b:
        return None  # symmetric — both or neither negate; not this check
    core_a, core_b = _core_tokens(a), _core_tokens(b)
    if not core_a or not core_b:
        return None
    union = core_a | core_b
    similarity = len(core_a & core_b) / len(union) if union else 0.0
    if similarity < _CORE_SIMILARITY:
        # Also allow the classic opposite-polarity-word case
        # (conflicts.py's own test): both sides carry a strong marker and
        # the markers disagree.
        if _has_affirmation(words_a) and neg_b:
            return "opposite polarity: affirmed then negated"
        if _has_affirmation(words_b) and neg_a:
            return "opposite polarity: negated then affirmed"
        return None
    return f"negation asymmetry over a shared claim (core overlap {similarity:.2f})"


def _pred_pattern(pred: str) -> str:
    words = pred.split()
    return r"\s+".join(re.escape(w) for w in words)


def _extract_svo(text: str) -> Optional[Tuple[str, str, str]]:
    """(subject, predicate, object), only when one of the closed-set
    predicates appears literally and with something on both sides of it.
    Never a guess — a text with no listed predicate yields None."""
    norm = _norm_text(text).rstrip(".!?")
    if not norm:
        return None
    for pred in _PRED_ORDER:
        pattern = rf"^(.+?)\b{_pred_pattern(pred)}\b(.+)$"
        match = re.search(pattern, norm)
        if match:
            subject = match.group(1).strip(" ,;:-")
            obj = match.group(2).strip(" ,;:-")
            if subject and obj:
                return subject, pred, obj
    return None


def _same_subject_different_value(a: str, b: str) -> Optional[str]:
    """Reason 2: same normalised subject, same predicate, different object,
    for the closed set of single-valued predicates."""
    svo_a, svo_b = _extract_svo(a), _extract_svo(b)
    if not svo_a or not svo_b:
        return None
    subject_a, pred_a, obj_a = svo_a
    subject_b, pred_b, obj_b = svo_b
    if subject_a != subject_b or pred_a != pred_b:
        return None
    if _norm_text(obj_a) == _norm_text(obj_b):
        return None
    return f"{subject_a!r} {pred_a!r}: {obj_a!r} vs {obj_b!r}"


def classify(text_a: Any, text_b: Any) -> Optional[Tuple[str, str]]:
    """The one conflict reason a pair of memory TEXTS is, or None.

    Public so tests (and anything else that wants a pure check with no DB)
    can call it directly, same shape as ``conflicts.classify`` upstream.
    """
    a, b = str(text_a or "").strip(), str(text_b or "").strip()
    if not a or not b:
        return None
    if _near_duplicate(a, b):
        return None  # dedupe's job, not this module's
    detail = _negation_conflict(a, b)
    if detail:
        return "negation", detail
    detail = _same_subject_different_value(a, b)
    if detail:
        return "same_subject_different_value", detail
    return None


# ---------------------------------------------------------------------------
# Candidate selection
# ---------------------------------------------------------------------------


def _candidates(item: Dict[str, Any]) -> List[Dict[str, Any]]:
    from src.memory_engine import list_items

    owner = str(item.get("owner") or "")
    project = str(item.get("project") or "")
    item_id = str(item.get("id") or "")
    rows = list_items(owner=owner, status="active", limit=2000)
    out = []
    for row in rows:
        if str(row.get("id") or "") == item_id:
            continue
        if row.get("suppressed"):
            continue
        if project and str(row.get("project") or "") != project:
            continue
        out.append(row)
    return out


def _top_k(text: str, candidates: List[Dict[str, Any]], k: int) -> List[Dict[str, Any]]:
    if len(candidates) <= k:
        return candidates
    try:
        from src.memory_engine import vector_store

        store = vector_store()
    except Exception:  # noqa: BLE001
        store = None
    if store:
        try:
            hits = store.search(text, k=max(k, DEFAULT_TOP_K)) or []
            order = {str(h.get("memory_id")): i for i, h in enumerate(hits) if isinstance(h, dict)}
            ranked = sorted(candidates, key=lambda c: order.get(str(c.get("id")), 10**9))
            return ranked[:k]
        except Exception:  # noqa: BLE001
            pass
    query_tokens = set(tokenize(text.lower()))

    def _overlap(cand: Dict[str, Any]) -> int:
        cand_tokens = set(tokenize(str(cand.get("text") or "").lower()))
        return -len(query_tokens & cand_tokens)

    return sorted(candidates, key=_overlap)[:k]


# ---------------------------------------------------------------------------
# CRUD on the conflicts table
# ---------------------------------------------------------------------------


def _insert(owner: str, new_id: str, old_id: str, reason: str, detail: str,
           now: datetime, *, supersede: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    row = {
        "id": uuid.uuid4().hex,
        "owner": owner,
        "new_id": new_id,
        "old_id": old_id,
        "reason": reason,
        "detail": str(detail or "")[:500],
        "status": "open",
        "created_at": _iso(now),
        "resolved_at": "",
        "supersede": dict(supersede or {}),
    }
    values = [json.dumps(row[c], ensure_ascii=False) if c == "supersede" else row[c]
              for c in _COLUMNS]
    with _db() as conn:
        conn.execute(
            f"INSERT INTO {_TABLE} ({', '.join(_COLUMNS)}) "
            f"VALUES ({', '.join('?' for _ in _COLUMNS)})",
            values,
        )
    return row


#: Of the closed predicate set above, only the ones describing a state that
#: naturally has exactly ONE current value for a given subject over time —
#: where someone lives, where they work — are ever superseded
#: AUTOMATICALLY. "the project uses Python" -> "uses Rust", "X prefers A" ->
#: "prefers B", "X is Y" -> "X is Z" stay `open`, the same contradictions a
#: human should look at they always were: a technology choice or an
#: identity claim does not "expire" the way an employer or a home town
#: does, and every pre-existing conflict test is written against exactly
#: that assumption. `resolve(id, keep="superseded")` still applies this
#: resolution to ANY pair by hand, since that is an explicit human choice
#: rather than a guess.
_SUPERSEDABLE_PREDICATES = frozenset({"lives in", "works at", "works as", "vive en", "trabaja en"})


def _is_supersedable_pair(text_a: str, text_b: str) -> bool:
    svo = _extract_svo(text_a) or _extract_svo(text_b)
    return bool(svo) and svo[1] in _SUPERSEDABLE_PREDICATES


def _supersede_enabled() -> bool:
    try:
        from src.settings import get_setting
        return bool(get_setting("memory_temporal_supersede", True))
    except Exception:  # noqa: BLE001
        return True


def _split_prev(detail: str) -> Tuple[str, str]:
    """LEGACY rows only: split `detail` into (human text, saved valid_until)."""
    detail = str(detail or "")
    if _PREV_MARK in detail:
        base, _, prev = detail.partition(_PREV_MARK)
        return base, prev
    return detail, ""


def _parse_dt(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _start_of(item: Dict[str, Any]) -> Tuple[Optional[datetime], str, bool]:
    """(start instant, start as stored, explicit?) of an item's validity.
    `memory_engine.add_item` stamps an undated item's `valid_from` with its
    `created_at`, so "explicit" means a `valid_from` that differs from it."""
    created_raw = str(item.get("created_at") or "")
    vf_raw = str(item.get("valid_from") or "")
    created = _parse_dt(created_raw)
    vf = _parse_dt(vf_raw)
    if vf is None:
        return created, created_raw, False
    return vf, vf_raw, created is None or vf != created


def _supersede_order(new_item: Dict[str, Any], old_item: Dict[str, Any]) -> Optional[str]:
    new_start, _, new_explicit = _start_of(new_item)
    old_start, _, old_explicit = _start_of(old_item)
    try:
        from src.brain.temporal import supersede_order
    except Exception:  # noqa: BLE001 - without the helper, never guess
        return None
    return supersede_order(new_start, old_start, new_explicit=new_explicit,
                           old_explicit=old_explicit)


def _apply_supersede(closed: Dict[str, Any], at: str, now: datetime) -> Dict[str, Any]:
    """Close `closed` at `at` unless it already ended by then (a window is
    never extended, rewritten later, or made to end before it starts).
    Returns the structured, never-truncated record `unsupersede` needs:
    ``{"closed_id", "prev_valid_until", "set_valid_until"}`` — with an empty
    ``closed_id`` when nothing had to change."""
    from src.memory_engine import save_item

    at_dt = _parse_dt(at)
    start, _, _ = _start_of(closed)
    previous = str(closed.get("valid_until") or "")
    prev_dt = _parse_dt(previous)
    if at_dt is None or (start is not None and at_dt < start) or (
            prev_dt is not None and prev_dt <= at_dt):
        return {"closed_id": "", "prev_valid_until": "", "set_valid_until": ""}
    closed = dict(closed)
    closed["valid_until"] = at
    closed["updated_at"] = _iso(now)
    save_item(closed)
    return {"closed_id": str(closed.get("id") or ""), "prev_valid_until": previous,
            "set_valid_until": at}


def _plan_and_apply(new_item: Dict[str, Any], old_item: Dict[str, Any], now: datetime, *,
                    manual: bool = False) -> Optional[Dict[str, Any]]:
    """Close whichever side chronology says is outdated, at the start of the
    other side. None when the order is unknowable (the conflict stays open)
    — unless `manual`: an owner who asked for "superseded" gets the OLDER
    stored item closed, at the later of the two starts, so the window can
    never end before it begins."""
    order = _supersede_order(new_item, old_item)
    if order is None and not manual:
        return None
    if order == "new":
        _, at, _ = _start_of(old_item)
        return _apply_supersede(new_item, at, now)
    new_start, new_raw, _ = _start_of(new_item)
    old_start, old_raw, _ = _start_of(old_item)
    at = new_raw or _iso(now)
    if order is None and new_start is not None and old_start is not None and new_start < old_start:
        at = old_raw
    return _apply_supersede(old_item, at, now)


def detect_for(item: Dict[str, Any], *, k: int = DEFAULT_TOP_K,
               now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Compare a just-written item against active items already in its
    scope and record any open conflicts found. NEVER raises — called from
    `memory_engine.add_item`/`correct` right after the write, and a broken
    detector must cost the conflict, not the write.

    The just-written item is always the "new" side: it was, by
    construction, stored after everything it is compared against.

    MEM-TEMPORAL: a ``same_subject_different_value`` pair, with
    ``memory_temporal_supersede`` on, is resolved immediately instead of
    left ``open`` — "Ada works at Bluehaven" after "Ada works at Cordera
    Labs" is not a contradiction, it is the SAME fact at a later time. The
    older item's ``valid_until`` is set to the new item's ``valid_from``
    (or its ``created_at``), the conflict is recorded as ``superseded``
    (not ``open``), and — because `memory_engine.public_item`'s ranking
    penalty only ever applies to an ``open`` conflict — the older item is
    never marked "contradicted" for this. Negation conflicts are never
    superseded: they stay ``open``, exactly as before this feature.
    """
    try:
        item_id = str(item.get("id") or "")
        text = str(item.get("text") or "").strip()
        if not item_id or not text:
            return []
        owner = str(item.get("owner") or "")
        candidates = _candidates(item)
        if not candidates:
            return []
        candidates = _top_k(text, candidates, max(1, int(k or DEFAULT_TOP_K)))
        now = now or datetime.now(timezone.utc)
        supersede_on = _supersede_enabled()
        created: List[Dict[str, Any]] = []
        for cand in candidates:
            result = classify(text, cand.get("text"))
            if result is None:
                continue
            reason, detail = result
            old_id = str(cand.get("id") or "")
            record: Optional[Dict[str, Any]] = None
            if (reason == "same_subject_different_value" and supersede_on
                    and _is_supersedable_pair(text, cand.get("text"))):
                try:
                    record = _plan_and_apply(item, dict(cand), now)
                except Exception as exc:  # noqa: BLE001 - nothing closed: leave it open
                    logger.debug("memory_conflicts: supersede close failed (%s)", exc)
                    record = None
            if record is not None:
                if record.get("closed_id") == item_id:
                    # the just-written item was the historical side: every
                    # later candidate must see its new window
                    item = dict(item, valid_until=record["set_valid_until"])
                row = _insert(owner, item_id, old_id, reason, detail, now, supersede=record)
                stamp = _iso(now)
                with _db() as conn:
                    conn.execute(
                        f"UPDATE {_TABLE} SET status = 'superseded', resolved_at = ? "
                        f"WHERE id = ?", (stamp, row["id"]),
                    )
                row["status"] = "superseded"
                row["resolved_at"] = stamp
                created.append(row)
            else:
                created.append(_insert(owner, item_id, old_id, reason, detail, now))
        return created
    except Exception as exc:  # noqa: BLE001 - hot-ish path, must never raise
        logger.debug("memory_conflicts: detect_for failed (%s)", exc)
        return []


def list_conflicts(owner: Optional[str] = None, status: Optional[str] = "open",
                   limit: int = 200) -> List[Dict[str, Any]]:
    where: List[str] = []
    params: List[Any] = []
    if owner is not None:
        where.append("owner = ?")
        params.append(str(owner))
    if status is not None:
        where.append("status = ?")
        params.append(str(status))
    sql = f"SELECT * FROM {_TABLE}"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(max(1, min(2000, int(limit or 200))))
    try:
        with _db() as conn:
            rows = conn.execute(sql, params).fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.debug("memory_conflicts: list_conflicts failed (%s)", exc)
        return []
    return [_row_to_dict(r) for r in rows]


def get_conflict(conflict_id: Any) -> Optional[Dict[str, Any]]:
    try:
        with _db() as conn:
            row = conn.execute(
                f"SELECT * FROM {_TABLE} WHERE id = ?", (str(conflict_id or ""),)
            ).fetchone()
    except Exception as exc:  # noqa: BLE001
        logger.debug("memory_conflicts: get_conflict failed (%s)", exc)
        return None
    return _row_to_dict(row) if row else None


def open_conflict_for(item_id: Any, owner: Any = "") -> Optional[Dict[str, Any]]:
    """The open conflict, if any, in which `item_id` is the OLDER (losing
    ranking) side. Used by `memory_engine.public_item` to apply the ranking
    penalty and the "contradicted by a newer memory" marker. Owner-scoped
    the same way `scoped_items` is: the caller's own rows plus unscoped
    (global) ones — never raises."""
    item_id = str(item_id or "")
    if not item_id:
        return None
    owner = str(owner or "")
    try:
        with _db() as conn:
            row = conn.execute(
                f"SELECT * FROM {_TABLE} WHERE old_id = ? AND status = 'open' "
                f"AND (owner = ? OR owner = '') LIMIT 1",
                (item_id, owner),
            ).fetchone()
    except Exception as exc:  # noqa: BLE001
        logger.debug("memory_conflicts: open_conflict_for failed (%s)", exc)
        return None
    return _row_to_dict(row) if row else None


def resolve(conflict_id: Any, keep: Any, *, owner: Optional[str] = None,
           now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """Apply the owner's choice. ``keep``:

    * ``"new"``  — the newer item stands; the older one is `forget()`-ed.
    * ``"old"``  — the older item stands; the newer one is `forget()`-ed.
    * ``"both"`` — neither is removed; the conflict is just marked resolved.
    * ``"superseded"`` — MEM-TEMPORAL: the same resolution `detect_for`
      applies automatically (close the older item's window instead of
      forgetting either side), triggered by hand — for a pair the automatic
      pass left ``open`` because ``memory_temporal_supersede`` was off at
      the time, or that the owner wants resolved this way regardless.

    Owner isolation: with `owner` given, a conflict recorded under a
    DIFFERENT owner is treated as not found rather than resolved — the same
    "not visible, not touchable" boundary `scoped_items` enforces for reads.
    Returns the updated conflict row, or None if it does not exist, is
    already resolved, or is not this owner's to resolve.
    """
    keep = str(keep or "").strip().lower()
    if keep not in ("new", "old", "both", "superseded"):
        raise MemoryConflictError("keep must be 'new', 'old', 'both' or 'superseded'")
    conflict = get_conflict(conflict_id)
    if not conflict or conflict.get("status") not in ("open", ADVISORY_STATUS):
        return None
    if owner is not None and str(conflict.get("owner") or "") != str(owner):
        return None

    now = now or datetime.now(timezone.utc)
    from src.memory_engine import forget, get_item

    detail = conflict.get("detail", "")
    record: Optional[Dict[str, Any]] = _load_supersede(conflict.get("supersede"))
    if keep == "new":
        forget(conflict["old_id"], reason=f"superseded by {conflict['new_id']}")
        status = "kept_new"
    elif keep == "old":
        forget(conflict["new_id"], reason=f"superseded by {conflict['old_id']}")
        status = "kept_old"
    elif keep == "superseded":
        new_item = get_item(conflict["new_id"])
        old_item = get_item(conflict["old_id"])
        if not new_item or not old_item:
            return None
        record = _plan_and_apply(new_item, old_item, now, manual=True)
        status = "superseded"
    else:
        status = "kept_both"

    stamp = _iso(now)
    with _db() as conn:
        conn.execute(
            f"UPDATE {_TABLE} SET status = ?, resolved_at = ?, detail = ?, supersede = ? "
            f"WHERE id = ?",
            (status, stamp, detail, json.dumps(record or {}, ensure_ascii=False), conflict["id"]),
        )
    conflict["status"] = status
    conflict["resolved_at"] = stamp
    conflict["detail"] = detail
    conflict["supersede"] = dict(record or {})
    return conflict


def unsupersede(conflict_id: Any, *, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """Reverse a ``superseded`` resolution: reopen the conflict and restore
    the older item's ``valid_until`` to what it was just before it was
    closed. None if the conflict does not exist or is not currently
    ``superseded``."""
    conflict = get_conflict(conflict_id)
    if not conflict or conflict.get("status") != "superseded":
        return None

    from src.memory_engine import get_item, save_item

    stamp = _iso(now or datetime.now(timezone.utc))
    record = _load_supersede(conflict.get("supersede"))
    base_detail = str(conflict.get("detail") or "")
    if record:
        closed = get_item(record.get("closed_id")) if record.get("closed_id") else None
        # restore only a window this supersede set and nobody changed since
        if closed is not None and str(closed.get("valid_until") or "") == str(
                record.get("set_valid_until") or ""):
            closed["valid_until"] = str(record.get("prev_valid_until") or "")
            closed["updated_at"] = stamp
            save_item(closed)
    else:
        base_detail, previous_until = _split_prev(base_detail)
        old_item = get_item(conflict["old_id"])
        if old_item is not None:
            old_item["valid_until"] = previous_until
            old_item["updated_at"] = stamp
            save_item(old_item)

    with _db() as conn:
        conn.execute(
            f"UPDATE {_TABLE} SET status = 'open', resolved_at = '', detail = ?, supersede = '' "
            f"WHERE id = ?",
            (base_detail, conflict["id"]),
        )
    conflict["status"] = "open"
    conflict["resolved_at"] = ""
    conflict["detail"] = base_detail
    conflict["supersede"] = {}
    return conflict


# ---------------------------------------------------------------------------
# Advisory pass — a typed decision over pairs the closed rules say nothing
# about. Background only, never on the write path, never a resolution.
# ---------------------------------------------------------------------------

ADVISORY_STATUS = "suggested"
ADVISORY_REASON = "model_suggested"
#: `advise()` writes its confidence into `detail`'s prose ("typed decision:
#: contradict (p=0.87, mass=0.42); advisory, not resolved") rather than a
#: column of its own -- there being exactly one producer of that text. This
#: is the one place that reads it back out, so a caller (the API route) can
#: hand the Studio panel a plain float instead of parsing English prose.
_PROBABILITY_RE = re.compile(r"p=([0-9]*\.?[0-9]+)")


def probability_of(detail: Any) -> Optional[float]:
    """The confidence `advise()` recorded for a ``suggested`` row's typed
    decision, or `None` for a row `detail` does not carry one (every
    non-advisory reason, or an advisory row written before this existed).
    Never raises on odd input."""
    match = _PROBABILITY_RE.search(str(detail or ""))
    if not match:
        return None
    try:
        value = float(match.group(1))
    except ValueError:
        return None
    return value if 0.0 <= value <= 1.0 else None
ADVICE_CHOICES: Tuple[str, ...] = ("contradict", "update", "compatible")
ADVICE_DESCRIPTIONS: Dict[str, str] = {
    "contradict": "they cannot both be true at the same time",
    "update": "the newer statement replaces the older one: something changed over time",
    "compatible": "both can be true together, or they are about different things",
}
ADVICE_QUESTION = "How do the older and the newer statement relate?"
#: Two texts must share at least this much of their core vocabulary to be
#: worth a question at all: a pair about different subjects is compatible
#: by construction and asking about it only spends the runner.
_ADVICE_MIN_OVERLAP = 0.25
_ADVICE_TABLE = "memory_conflict_advice"
_ADVICE_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {_ADVICE_TABLE} (
    owner      TEXT NOT NULL DEFAULT '',
    pair       TEXT NOT NULL DEFAULT '',
    verdict    TEXT NOT NULL DEFAULT '',
    confidence REAL,
    mass       REAL,
    asked_at   TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(owner, pair)
)
"""
_ADVICE_TIMEOUT_S = 8.0


def _pair_key(a_id: str, b_id: str) -> str:
    return "|".join(sorted((str(a_id), str(b_id))))


def _core_overlap(a: str, b: str) -> float:
    core_a, core_b = _core_tokens(a), _core_tokens(b)
    union = core_a | core_b
    return len(core_a & core_b) / len(union) if union else 0.0


def advice_candidates(items: Sequence[Dict[str, Any]], *, per_item: int = 4,
                      recent: int = 20) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """(older, newer) pairs among `items` that the deterministic detector
    finds NOTHING for, that are not the same statement twice, and that share
    enough core vocabulary to plausibly be about the same thing. Pure: the
    caller supplies the active items (dicts with ``id``, ``text``,
    ``created_at``); the newest `recent` are compared against their
    `per_item` closest neighbours."""
    rows = [i for i in items if str(i.get("text") or "").strip() and i.get("id")]
    rows.sort(key=lambda i: str(i.get("created_at") or ""))
    seen: set = set()
    out: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    for newer in reversed(rows[-max(1, recent):]):
        text = str(newer.get("text") or "")
        project = str(newer.get("project") or "")
        others = [o for o in rows if o.get("id") != newer.get("id")
                  and (not project or str(o.get("project") or "") in ("", project))]
        scored = sorted(((_core_overlap(text, str(o.get("text") or "")), o) for o in others),
                        key=lambda pair: -pair[0])
        for overlap, other in scored[:per_item]:
            if overlap < _ADVICE_MIN_OVERLAP:
                break
            key = _pair_key(newer["id"], other["id"])
            if key in seen:
                continue
            seen.add(key)
            if classify(text, other.get("text")) is not None:
                continue  # the rules already have a verdict
            if _near_duplicate(text, str(other.get("text") or "")):
                continue
            older, newest = sorted((other, newer), key=lambda i: str(i.get("created_at") or ""))
            out.append((older, newest))
    return out


def advice_context(older: Dict[str, Any], newer: Dict[str, Any]) -> str:
    def _when(item: Dict[str, Any]) -> str:
        stamp = str(item.get("valid_from") or item.get("created_at") or "")[:10]
        return f" ({stamp})" if stamp else ""
    return (f"Older statement{_when(older)}: {str(older.get('text') or '').strip()}\n"
            f"Newer statement{_when(newer)}: {str(newer.get('text') or '').strip()}")


def advice_field() -> Any:
    from src.typed_decision import Field
    return Field(name="relation", question=ADVICE_QUESTION, choices=list(ADVICE_CHOICES),
                 descriptions=ADVICE_DESCRIPTIONS)


def _asked_pairs(owner: str) -> set:
    with _db() as conn:
        conn.execute(_ADVICE_SCHEMA)
        rows = conn.execute(f"SELECT pair FROM {_ADVICE_TABLE} WHERE owner = ?", (owner,)).fetchall()
        existing = conn.execute(
            f"SELECT new_id, old_id FROM {_TABLE} WHERE owner = ?", (owner,)).fetchall()
    return {r["pair"] for r in rows} | {_pair_key(r["new_id"], r["old_id"]) for r in existing}


def _record_advice(owner: str, pair: str, verdict: str, confidence: Optional[float],
                   mass: Optional[float]) -> None:
    with _db() as conn:
        conn.execute(_ADVICE_SCHEMA)
        conn.execute(
            f"INSERT INTO {_ADVICE_TABLE} (owner, pair, verdict, confidence, mass, asked_at) "
            f"VALUES (?,?,?,?,?,?) ON CONFLICT(owner, pair) DO UPDATE SET verdict = excluded.verdict, "
            f"confidence = excluded.confidence, mass = excluded.mass, asked_at = excluded.asked_at",
            (owner, pair, verdict, confidence, mass, _iso()),
        )


async def advise(owner: Any, *, limit: int = 6, budget_s: float = 20.0,
                 background: bool = True) -> Dict[str, Any]:
    """Ask a typed decision about pairs the closed rules found nothing for
    ("contradict" / "update" / "compatible"). A confident "contradict" or
    "update" becomes a ``suggested`` row (reason ``model_suggested``) for the
    owner to look at; nothing is resolved, ranked down or forgotten because
    of it. Every asked pair is remembered so it is asked once. With
    `background`, the brain's model etiquette (a turn in flight, a model
    that would have to load, a busy runner) is checked before each call.
    Never raises; returns a small report."""
    owner = str(owner or "")
    report: Dict[str, Any] = {"asked": 0, "suggested": 0, "skipped": ""}
    start = time.monotonic()
    try:
        from src import typed_decision
        from src.settings import get_setting
        if not typed_decision.enabled() or not bool(get_setting("typed_decision_memory_conflicts", True)):
            report["skipped"] = "disabled"
            return report
        from src.memory_engine import list_items
        items = [i for i in list_items(owner=owner, status="active", limit=2000)
                 if not i.get("suppressed")]
        asked = _asked_pairs(owner)
        pairs = [(o, n) for o, n in advice_candidates(items)
                 if _pair_key(o["id"], n["id"]) not in asked]
        if not pairs:
            return report
        gate = None
        url = model = None
        if background:
            from src.brain.extract import background_llm_gate as gate
            from src.endpoint_resolver import resolve_endpoint
            url, model, _headers = resolve_endpoint("utility", owner=owner)
        min_conf = typed_decision.default_min_confidence()
        for older, newer in pairs[:max(0, int(limit))]:
            if time.monotonic() - start > budget_s:
                break
            if gate is not None:
                reason = gate(url, model)
                if reason:
                    report["skipped"] = reason
                    break
            decisions = await typed_decision.decide(
                advice_context(older, newer), [advice_field()], owner=owner,
                timeout_s=max(typed_decision.default_timeout_s(), _ADVICE_TIMEOUT_S),
                caller="memory_conflict",
            )
            decision = decisions.get("relation")
            if decision is None or decision.method == "unavailable":
                report["skipped"] = (decision.reason if decision else "") or "unavailable"
                break
            report["asked"] += 1
            pair = _pair_key(older["id"], newer["id"])
            _record_advice(owner, pair, str(decision.best or decision.value or ""),
                           decision.confidence, decision.mass)
            if (decision.method == "logprobs" and decision.value in ("contradict", "update")
                    and decision.confidence is not None and decision.confidence >= min_conf):
                row = _insert(owner, str(newer["id"]), str(older["id"]), ADVISORY_REASON,
                              f"typed decision: {decision.value} (p={decision.confidence:.2f}, "
                              f"mass={decision.mass:.2f}); advisory, not resolved",
                              datetime.now(timezone.utc))
                with _db() as conn:
                    conn.execute(f"UPDATE {_TABLE} SET status = ? WHERE id = ?",
                                 (ADVISORY_STATUS, row["id"]))
                report["suggested"] += 1
    except Exception as exc:  # noqa: BLE001 - a background pass never raises
        logger.debug("memory_conflicts: advise failed (%s)", exc)
        report["skipped"] = report.get("skipped") or "error"
    return report


__all__ = [
    "REASONS", "STATUSES", "RANKING_PENALTY", "MemoryConflictError",
    "classify", "detect_for", "list_conflicts", "get_conflict",
    "open_conflict_for", "resolve", "unsupersede",
    "ADVISORY_STATUS", "ADVISORY_REASON", "advise", "advice_candidates",
    "probability_of",
]
