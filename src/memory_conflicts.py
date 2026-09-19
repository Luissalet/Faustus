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

What this module never does: call a model, guess a subject from wording
alone (it is extracted with an explicit, closed predicate list, the same
"never inferred" discipline ``conflicts.py`` applies to its own ``subject``
meta key), or touch either item's stored text. A conflict is a row in its
own table; resolving it (`resolve`) is what the owner does explicitly.
"""

from __future__ import annotations

import contextlib
import logging
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.context_engine.conflicts import _AFFIRMATIONS, _NEGATIONS
from src.memory import get_text_similarity, tokenize

logger = logging.getLogger(__name__)

REASONS: Tuple[str, ...] = ("negation", "same_subject_different_value")
STATUSES: Tuple[str, ...] = ("open", "kept_new", "kept_old", "kept_both")

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
    "created_at", "resolved_at",
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
    resolved_at  TEXT NOT NULL DEFAULT ''
)
"""
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
    return {c: row[c] for c in _COLUMNS}


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
           now: datetime) -> Dict[str, Any]:
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
    }
    with _db() as conn:
        conn.execute(
            f"INSERT INTO {_TABLE} ({', '.join(_COLUMNS)}) "
            f"VALUES ({', '.join('?' for _ in _COLUMNS)})",
            [row[c] for c in _COLUMNS],
        )
    return row


def detect_for(item: Dict[str, Any], *, k: int = DEFAULT_TOP_K,
               now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Compare a just-written item against active items already in its
    scope and record any open conflicts found. NEVER raises — called from
    `memory_engine.add_item`/`correct` right after the write, and a broken
    detector must cost the conflict, not the write.

    The just-written item is always the "new" side: it was, by
    construction, stored after everything it is compared against.
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
        created: List[Dict[str, Any]] = []
        for cand in candidates:
            result = classify(text, cand.get("text"))
            if result is None:
                continue
            reason, detail = result
            created.append(_insert(owner, item_id, str(cand.get("id") or ""),
                                   reason, detail, now))
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

    Owner isolation: with `owner` given, a conflict recorded under a
    DIFFERENT owner is treated as not found rather than resolved — the same
    "not visible, not touchable" boundary `scoped_items` enforces for reads.
    Returns the updated conflict row, or None if it does not exist, is
    already resolved, or is not this owner's to resolve.
    """
    keep = str(keep or "").strip().lower()
    if keep not in ("new", "old", "both"):
        raise MemoryConflictError("keep must be 'new', 'old' or 'both'")
    conflict = get_conflict(conflict_id)
    if not conflict or conflict.get("status") != "open":
        return None
    if owner is not None and str(conflict.get("owner") or "") != str(owner):
        return None

    from src.memory_engine import forget

    if keep == "new":
        forget(conflict["old_id"], reason=f"superseded by {conflict['new_id']}")
        status = "kept_new"
    elif keep == "old":
        forget(conflict["new_id"], reason=f"superseded by {conflict['old_id']}")
        status = "kept_old"
    else:
        status = "kept_both"

    stamp = _iso(now)
    with _db() as conn:
        conn.execute(
            f"UPDATE {_TABLE} SET status = ?, resolved_at = ? WHERE id = ?",
            (status, stamp, conflict["id"]),
        )
    conflict["status"] = status
    conflict["resolved_at"] = stamp
    return conflict


__all__ = [
    "REASONS", "STATUSES", "RANKING_PENALTY", "MemoryConflictError",
    "classify", "detect_for", "list_conflicts", "get_conflict",
    "open_conflict_for", "resolve",
]
