"""Learned memory — layer 1: the durable, explainable store (FAUSTUS).

The existing memory (``src/memory.py`` + ``memory.json``) is a flat list of
facts a human or an extractor wrote down. It never changes its mind. This
module is the other half: memory that LEARNS from outcomes and FORGETS on its
own, without touching a single row of the old store.

What makes an item different from a memory
------------------------------------------
* **A level with a half-life.** ``working`` (1 day), ``episodic`` (30),
  ``semantic`` (180) fade on their own; ``procedural`` — the rules the agent
  is supposed to follow — never fade with time. A rule only dies by being
  contradicted, which is the honest model: "always run the tests" does not
  become less true in June.
* **A trust class, not a confidence number the model invented.** Who said it
  decides the ceiling: a human explicitly (0.85), a validated agent claim
  (0.65), a bare agent assertion (0.50), a legacy import (0.30).
* **Evidence and feedback are kept, not summarised.** Every helpful/harmful
  event carries its own timestamp, weight, reason and ref, so the score is a
  pure function of the record and can always be explained.
* **Harm outweighs help 4:1.** ``effective_score = trust × freshness
  − 4 × decayed(harmful) + decayed(helpful)``. A rule that broke three turns
  is gone long before eight good ones could have saved it.
* **Inversion.** When a rule is mostly harmful (>50% of the decayed weight,
  at least 3 harmful events) it is not deleted — it is *inverted* into an
  anti-pattern: ``AVOID: <the original text>``. What the system learned the
  hard way is worth more as a warning than as a gap.

Retrieval is hybrid and degrades explicitly: lexical BM25-lite (0.45) +
semantic vectors (0.45) + evidence-graph overlap (0.10); with no vector store
the lexical lane is renormalised to 0.90 and the result says ``degraded``.
Never an error, because the vector store may simply not be installed.

Storage is its own SQLite file (``DATA_DIR/memory_engine.db``, WAL,
short-lived connections under a module lock) so this feature can never take
the app's database with it. A corrupt file is moved aside and recreated.
:func:`pack`, :func:`note_injected` and :func:`record_outcome` sit on the chat
hot path and NEVER raise — a broken store costs the block, not the turn.

Layer 2 (the deterministic Curator) lives in ``src/memory_curator.py``.
Pure stdlib.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import re
import sqlite3
import threading
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from src.memory import tokenize

logger = logging.getLogger(__name__)

try:  # pragma: no cover - constants always import in the app
    from src.constants import DATA_DIR as _DEFAULT_DATA_DIR
except Exception:  # noqa: BLE001 - standalone use (tests, tooling)
    _DEFAULT_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")

# Module-level so tests can point the store somewhere disposable.
DATA_DIR = _DEFAULT_DATA_DIR
DB_FILENAME = "memory_engine.db"


# ---------------------------------------------------------------------------
# Vocabulary and constants
# ---------------------------------------------------------------------------

LEVELS: Tuple[str, ...] = ("working", "episodic", "semantic", "procedural")

# Time decay of the ITEM itself. `procedural` is None on purpose: a rule does
# not get less true with age, it only dies by contradiction.
HALF_LIFE_DAYS: Dict[str, Optional[float]] = {
    "working": 1.0,
    "episodic": 30.0,
    "semantic": 180.0,
    "procedural": None,
}

TRUST_CLASSES: Dict[str, float] = {
    "human_explicit": 0.85,
    "agent_validated": 0.65,
    "agent_assertion": 0.50,
    "legacy_import": 0.30,
}

STATUSES: Tuple[str, ...] = ("active", "deprecated", "anti_pattern")
MATURITIES: Tuple[str, ...] = ("candidate", "established", "proven", "deprecated")
EVIDENCE_KINDS: Tuple[str, ...] = ("chat", "file", "dispatch")
FEEDBACK_KINDS: Tuple[str, ...] = ("helpful", "harmful")

# Feedback events decay with their own half-life, independent of the item's.
FEEDBACK_HALF_LIFE_DAYS = 90.0
# One harmful event cancels four helpful ones.
HARM_WEIGHT = 4.0
# Floor used when a (possibly negative) effective score multiplies relevance,
# and the line under which the Curator deprecates an item.
SCORE_FLOOR = 0.05

# Maturity ladder.
ESTABLISHED_MIN_REFS = 3
PROVEN_MIN_REFS = 8
PROVEN_MAX_HARM_RATIO = 0.20
# Inversion: mostly harmful, and enough events to mean it.
INVERT_HARM_RATIO = 0.50
INVERT_MIN_HARMFUL = 3
# Deprecated items untouched this long are deleted by the Curator.
PRUNE_AFTER_DAYS = 90.0
# Jaccard similarity above which two items are the same item.
DEDUPE_SIMILARITY = 0.85
# A procedural rule with a helpful event this recent survives deprecation.
RECENT_HELPFUL_DAYS = 30.0

# Hybrid retrieval weights, and the renormalised lexical weight used when the
# semantic lane is absent (explicit degradation, never an error).
W_LEXICAL = 0.45
W_SEMANTIC = 0.45
W_GRAPH = 0.10
W_LEXICAL_DEGRADED = 0.90

BM25_K1 = 1.5
BM25_B = 0.75

MAX_TEXT_CHARS = 2000
MAX_EXCERPT_CHARS = 200
MAX_REF_CHARS = 400
MAX_REASON_CHARS = 300
# A row must not grow without bound: keep the newest N of each list.
MAX_EVENTS = 200
DEFAULT_PACK_CHARS = 1800

PACK_RULES_HEADER = "## Learned rules"
PACK_MEMORIES_HEADER = "## Relevant memories"
PACK_ANTI_HEADER = "## Known anti-patterns"


class MemoryEngineError(ValueError):
    """Invalid input or an unusable store — routes map this to a 400."""


# ---------------------------------------------------------------------------
# Time helpers (every scoring function takes an injectable `now`)
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso() -> str:
    return _iso(_utcnow())


def parse_iso(text: Any) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _days_since(then: Any, now: Optional[datetime] = None) -> float:
    """Whole-and-fractional days between `then` and `now`, never negative.

    An unparseable timestamp counts as "just now" rather than "infinitely
    old": a bad string must not silently delete an item.
    """
    parsed = parse_iso(then)
    if parsed is None:
        return 0.0
    return max(0.0, ((now or _utcnow()) - parsed).total_seconds() / 86400.0)


# ---------------------------------------------------------------------------
# Scoring — pure functions over an item dict, unit-testable, no I/O
# ---------------------------------------------------------------------------


def decayed(events: Optional[Sequence[Dict[str, Any]]],
            now: Optional[datetime] = None) -> float:
    """Σ weight × 0.5 ^ (days_since / 90) over a feedback list."""
    total = 0.0
    for event in events or []:
        if not isinstance(event, dict):
            continue
        try:
            weight = float(event.get("weight", 1.0))
        except (TypeError, ValueError):
            weight = 1.0
        days = _days_since(event.get("ts"), now)
        total += weight * (0.5 ** (days / FEEDBACK_HALF_LIFE_DAYS))
    return total


def freshness(level: str, updated_at: Any, now: Optional[datetime] = None) -> float:
    """0.5 ^ (days_since_updated / half_life); always 1.0 for procedural."""
    half_life = HALF_LIFE_DAYS.get(str(level or ""), HALF_LIFE_DAYS["semantic"])
    if half_life is None:          # procedural: no time decay at all
        return 1.0
    return 0.5 ** (_days_since(updated_at, now) / half_life)


def effective_score(item: Dict[str, Any], now: Optional[datetime] = None) -> float:
    """trust × freshness − 4 × decayed(harmful) + decayed(helpful)."""
    try:
        trust = float(item.get("trust", 0.0) or 0.0)
    except (TypeError, ValueError):
        trust = 0.0
    fresh = freshness(item.get("level"), item.get("updated_at"), now)
    return (trust * fresh
            - HARM_WEIGHT * decayed(item.get("harmful"), now)
            + decayed(item.get("helpful"), now))


def harmful_ratio(item: Dict[str, Any], now: Optional[datetime] = None) -> float:
    """decayed(harmful) / (decayed(helpful) + decayed(harmful)); 0.0 with no
    feedback at all — "no evidence of harm", not "maximum harm"."""
    good = decayed(item.get("helpful"), now)
    bad = decayed(item.get("harmful"), now)
    total = good + bad
    return (bad / total) if total > 0 else 0.0


def distinct_refs(events: Optional[Sequence[Dict[str, Any]]]) -> int:
    """How many DIFFERENT sources vouched for an item. Eight helpful events
    from one runaway loop are one ref, not eight."""
    refs = set()
    for event in events or []:
        if not isinstance(event, dict):
            continue
        ref = str(event.get("ref") or "").strip()
        if ref:
            refs.add(ref)
    return len(refs)


# ---------------------------------------------------------------------------
# Storage — DATA_DIR/memory_engine.db, its own file, never a core migration
# ---------------------------------------------------------------------------

_DB_LOCK = threading.RLock()

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS items (
        id            TEXT PRIMARY KEY,
        owner         TEXT NOT NULL DEFAULT '',
        project       TEXT NOT NULL DEFAULT '',
        level         TEXT NOT NULL DEFAULT 'semantic',
        text          TEXT NOT NULL DEFAULT '',
        category      TEXT NOT NULL DEFAULT '',
        trust_class   TEXT NOT NULL DEFAULT 'agent_assertion',
        trust         REAL NOT NULL DEFAULT 0.5,
        confidence    REAL NOT NULL DEFAULT 0.5,
        status        TEXT NOT NULL DEFAULT 'active',
        maturity      TEXT NOT NULL DEFAULT 'candidate',
        evidence      TEXT NOT NULL DEFAULT '[]',
        helpful       TEXT NOT NULL DEFAULT '[]',
        harmful       TEXT NOT NULL DEFAULT '[]',
        inverted_from TEXT NOT NULL DEFAULT '',
        created_at    TEXT NOT NULL DEFAULT '',
        updated_at    TEXT NOT NULL DEFAULT '',
        last_accessed TEXT NOT NULL DEFAULT '',
        access_count  INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_items_scope ON items(owner, project)",
    "CREATE INDEX IF NOT EXISTS idx_items_status ON items(status, level)",
)

_COLUMNS = (
    "id", "owner", "project", "level", "text", "category", "trust_class", "trust",
    "confidence", "status", "maturity", "evidence", "helpful", "harmful",
    "inverted_from", "created_at", "updated_at", "last_accessed", "access_count",
)


def db_path() -> str:
    return os.path.join(DATA_DIR, DB_FILENAME)


def _quarantine(path: str, reason: Any) -> None:
    """Move an unreadable database aside so a fresh one can be created.

    Nothing is deleted while it can be kept: a `.corrupt` copy stays on disk
    for anyone who wants to try `sqlite3 .recover` on it later.
    """
    for suffix in ("", "-wal", "-shm"):
        victim = path + suffix
        if not os.path.exists(victim):
            continue
        try:
            os.replace(victim, victim + ".corrupt")
        except OSError:
            try:
                os.unlink(victim)
            except OSError:
                pass
    logger.warning("memory_engine.db was unusable (%s); moved aside and recreated", reason)


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10.0)
    try:
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.Error:
            # A filesystem that refuses WAL (some network mounts) is not a
            # reason to lose the feature — the default journal works fine.
            pass
        for statement in _SCHEMA:
            conn.execute(statement)
        conn.execute("SELECT COUNT(*) FROM items").fetchone()
        conn.commit()
        return conn
    except Exception:
        with contextlib.suppress(Exception):
            conn.close()
        raise


def _open() -> sqlite3.Connection:
    path = db_path()
    with contextlib.suppress(OSError):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    try:
        return _connect(path)
    except sqlite3.DatabaseError as exc:
        # "file is not a database", a truncated header, a half-written copy:
        # recreate rather than fail every call from here to the next restart.
        _quarantine(path, exc)
        try:
            return _connect(path)
        except sqlite3.Error as exc2:      # pragma: no cover - dead disk
            raise MemoryEngineError(f"memory engine store unusable: {exc2}") from exc2


@contextlib.contextmanager
def _db():
    """Short-lived connection under the module lock, committed on success."""
    with _DB_LOCK:
        conn = _open()
        try:
            yield conn
            conn.commit()
        finally:
            with contextlib.suppress(Exception):
                conn.close()


def _loads(raw: Any) -> List[Dict[str, Any]]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _row_to_item(row: sqlite3.Row) -> Dict[str, Any]:
    item = {key: row[key] for key in _COLUMNS}
    for key in ("evidence", "helpful", "harmful"):
        item[key] = _loads(item.get(key))
    item["access_count"] = int(item.get("access_count") or 0)
    for key in ("trust", "confidence"):
        try:
            item[key] = float(item.get(key) or 0.0)
        except (TypeError, ValueError):
            item[key] = 0.0
    return item


def public_item(item: Dict[str, Any], now: Optional[datetime] = None) -> Dict[str, Any]:
    """The item as the API/tool/UI sees it: stored fields plus the COMPUTED
    score fields (never stored — they are a function of the clock)."""
    out = dict(item)
    out["id8"] = str(item.get("id") or "")[:8]
    out["effective_score"] = round(effective_score(item, now), 4)
    out["harmful_ratio"] = round(harmful_ratio(item, now), 4)
    out["helpful_count"] = len(item.get("helpful") or [])
    out["harmful_count"] = len(item.get("harmful") or [])
    out["distinct_helpful_refs"] = distinct_refs(item.get("helpful"))
    return out


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _clean_text(value: Any) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        raise MemoryEngineError("text must not be empty")
    return text[:MAX_TEXT_CHARS]


def _valid_level(value: Any, default: str = "semantic") -> str:
    level = str(value or "").strip().lower()
    if not level:
        return default
    if level not in LEVELS:
        raise MemoryEngineError(f"level must be one of {', '.join(LEVELS)}")
    return level


def _valid_trust_class(value: Any, default: str = "agent_assertion") -> str:
    trust_class = str(value or "").strip().lower()
    if not trust_class:
        return default
    if trust_class not in TRUST_CLASSES:
        raise MemoryEngineError(
            f"trust_class must be one of {', '.join(TRUST_CLASSES)}")
    return trust_class


def _valid_status(value: Any) -> str:
    status = str(value or "").strip().lower()
    if status not in STATUSES:
        raise MemoryEngineError(f"status must be one of {', '.join(STATUSES)}")
    return status


def normalize_evidence(spans: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for span in (spans if isinstance(spans, (list, tuple)) else []):
        if not isinstance(span, dict):
            continue
        kind = str(span.get("kind") or "chat").strip().lower()
        record: Dict[str, Any] = {"kind": kind if kind in EVIDENCE_KINDS else "chat"}
        if span.get("session_id"):
            record["session_id"] = str(span["session_id"])[:120]
        if span.get("ref"):
            record["ref"] = str(span["ref"])[:MAX_REF_CHARS]
        excerpt = " ".join(str(span.get("excerpt") or "").split())[:MAX_EXCERPT_CHARS]
        if excerpt:
            record["excerpt"] = excerpt
        out.append(record)
    return out[-MAX_EVENTS:]


def _feedback_event(reason: str = "", ref: str = "", weight: float = 1.0,
                    now: Optional[datetime] = None) -> Dict[str, Any]:
    try:
        weight = float(weight)
    except (TypeError, ValueError):
        weight = 1.0
    return {
        "ts": _iso(now or _utcnow()),
        "weight": max(0.0, min(10.0, weight)),
        "reason": " ".join(str(reason or "").split())[:MAX_REASON_CHARS],
        "ref": str(ref or "")[:MAX_REF_CHARS],
    }


# ---------------------------------------------------------------------------
# The semantic lane — present only when the vector store is importable AND
# initialized. Absence is a documented degradation, never an error.
# ---------------------------------------------------------------------------

_vector_state: Dict[str, Any] = {"tried": False, "store": None}


def set_vector_store(store: Any) -> None:
    """Install (or clear, with None) the semantic lane explicitly."""
    _vector_state["tried"] = True
    _vector_state["store"] = store


def reset_vector_store() -> None:
    _vector_state["tried"] = False
    _vector_state["store"] = None


def vector_store() -> Any:
    """The engine's own vector collection, or None. Tried once per process."""
    if _vector_state["tried"]:
        return _vector_state["store"]
    _vector_state["tried"] = True
    _vector_state["store"] = None
    try:
        from src.memory_vector import MemoryVectorStore

        class _EngineVectors(MemoryVectorStore):
            # Its own collection: learned items and plain memories must never
            # answer each other's queries.
            COLLECTION_NAME = "odysseus_memory_engine"

        store = _EngineVectors(DATA_DIR)
        if getattr(store, "healthy", False):
            _vector_state["store"] = store
        else:
            logger.info("memory engine: semantic lane unavailable, lexical only")
    except Exception as exc:  # noqa: BLE001 - optional dependency, never fatal
        logger.info("memory engine: semantic lane unavailable (%s), lexical only", exc)
    return _vector_state["store"]


def _index(item_id: str, text: str) -> None:
    store = vector_store()
    if not store:
        return
    try:
        store.add(item_id, text)
    except Exception as exc:  # noqa: BLE001
        logger.debug("memory engine: index of %s failed: %s", item_id, exc)


def _unindex(item_id: str) -> None:
    store = _vector_state.get("store")
    if not store:
        return
    try:
        store.remove(item_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("memory engine: unindex of %s failed: %s", item_id, exc)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def add_item(
    text: Any,
    *,
    owner: str = "",
    project: str = "",
    level: Any = "semantic",
    category: Any = "",
    trust_class: Any = "agent_assertion",
    confidence: Optional[float] = None,
    evidence: Any = None,
    status: Any = "active",
    maturity: Any = "candidate",
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Store one item. Raises MemoryEngineError on invalid input."""
    clean = _clean_text(text)
    level = _valid_level(level)
    trust_class = _valid_trust_class(trust_class)
    status = _valid_status(status)
    maturity = str(maturity or "candidate").strip().lower()
    if maturity not in MATURITIES:
        raise MemoryEngineError(f"maturity must be one of {', '.join(MATURITIES)}")
    trust = TRUST_CLASSES[trust_class]
    try:
        conf = float(confidence) if confidence is not None else trust
    except (TypeError, ValueError):
        conf = trust
    stamp = _iso(now or _utcnow())
    item = {
        "id": uuid.uuid4().hex,
        "owner": str(owner or ""),
        "project": str(project or ""),
        "level": level,
        "text": clean,
        "category": str(category or "")[:80],
        "trust_class": trust_class,
        "trust": trust,
        "confidence": max(0.0, min(1.0, conf)),
        "status": status,
        "maturity": maturity,
        "evidence": normalize_evidence(evidence),
        "helpful": [],
        "harmful": [],
        "inverted_from": "",
        "created_at": stamp,
        "updated_at": stamp,
        "last_accessed": "",
        "access_count": 0,
    }
    save_item(item)
    _index(item["id"], clean)
    return item


def save_item(item: Dict[str, Any]) -> Dict[str, Any]:
    """Full upsert of an item dict (the Curator's write path too)."""
    row = dict(item)
    for key in ("evidence", "helpful", "harmful"):
        value = row.get(key) or []
        row[key] = json.dumps(list(value)[-MAX_EVENTS:], ensure_ascii=False)
    row.setdefault("inverted_from", "")
    row.setdefault("last_accessed", "")
    row.setdefault("access_count", 0)
    values = [row.get(column) for column in _COLUMNS]
    placeholders = ", ".join("?" for _ in _COLUMNS)
    with _db() as conn:
        conn.execute(
            f"INSERT OR REPLACE INTO items ({', '.join(_COLUMNS)}) VALUES ({placeholders})",
            values,
        )
    return item


def get_item(item_id: Any) -> Optional[Dict[str, Any]]:
    with _db() as conn:
        row = conn.execute("SELECT * FROM items WHERE id = ?", (str(item_id or ""),)).fetchone()
    return _row_to_item(row) if row else None


def resolve_id(prefix: Any) -> Optional[str]:
    """Full id for an exact id or an unambiguous ``id8`` prefix.

    pack() shows items as ``[id8]``, so that is what the model will quote
    back. An ambiguous prefix resolves to nothing rather than to a guess.
    """
    prefix = str(prefix or "").strip()
    if not prefix:
        return None
    with _db() as conn:
        row = conn.execute("SELECT id FROM items WHERE id = ?", (prefix,)).fetchone()
        if row:
            return str(row["id"])
        rows = conn.execute("SELECT id FROM items WHERE id LIKE ? LIMIT 2",
                            (prefix.replace("%", "") + "%",)).fetchall()
    return str(rows[0]["id"]) if len(rows) == 1 else None


def delete_item(item_id: Any) -> bool:
    item_id = str(item_id or "")
    with _db() as conn:
        cursor = conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
        deleted = cursor.rowcount > 0
    if deleted:
        _unindex(item_id)
    return deleted


def list_items(
    *,
    owner: Optional[str] = None,
    project: Optional[str] = None,
    status: Optional[str] = None,
    level: Optional[str] = None,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """Exact-match filtering (this is the dashboard's list). ``None`` for a
    filter means "any"; ``""`` means "the global/unscoped ones only"."""
    where: List[str] = []
    params: List[Any] = []
    for column, value in (("owner", owner), ("project", project),
                          ("status", status), ("level", level)):
        if value is not None:
            where.append(f"{column} = ?")
            params.append(str(value))
    sql = "SELECT * FROM items"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY updated_at DESC, id ASC LIMIT ?"
    params.append(max(1, min(2000, int(limit or 200))))
    with _db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_item(row) for row in rows]


def scoped_items(
    owner: Optional[str] = None,
    project: Optional[str] = None,
    statuses: Sequence[str] = ("active", "anti_pattern"),
) -> List[Dict[str, Any]]:
    """Everything visible from one seat: the caller's own items plus the
    unscoped ones. A rule with no project is a rule everywhere."""
    where: List[str] = []
    params: List[Any] = []
    if owner is not None:
        where.append("(owner = ? OR owner = '')")
        params.append(str(owner))
    if project is not None:
        where.append("(project = ? OR project = '')")
        params.append(str(project))
    if statuses:
        where.append("status IN (%s)" % ", ".join("?" for _ in statuses))
        params.extend(str(s) for s in statuses)
    sql = "SELECT * FROM items"
    if where:
        sql += " WHERE " + " AND ".join(where)
    with _db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_item(row) for row in rows]


def add_feedback(
    item_id: Any,
    kind: Any,
    *,
    reason: str = "",
    ref: str = "",
    weight: float = 1.0,
    now: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    """Append one helpful/harmful event. Returns the updated item, or None
    when the item is gone. Does NOT re-evaluate maturity — that is the
    Curator's job, so scoring stays a pure function of the record."""
    kind = str(kind or "").strip().lower()
    if kind not in FEEDBACK_KINDS:
        raise MemoryEngineError("kind must be 'helpful' or 'harmful'")
    item = get_item(item_id)
    if not item:
        return None
    events = list(item.get(kind) or [])
    events.append(_feedback_event(reason=reason, ref=ref, weight=weight, now=now))
    item[kind] = events[-MAX_EVENTS:]
    # updated_at is the freshness clock: feedback IS a touch of the item.
    item["updated_at"] = _iso(now or _utcnow())
    save_item(item)
    return item


def add_evidence(item_id: Any, spans: Any) -> Optional[Dict[str, Any]]:
    item = get_item(item_id)
    if not item:
        return None
    item["evidence"] = (list(item.get("evidence") or []) + normalize_evidence(spans))[-MAX_EVENTS:]
    save_item(item)
    return item


def touch(ids: Iterable[str], now: Optional[datetime] = None) -> int:
    """Bump last_accessed / access_count for items actually surfaced."""
    ids = [str(i) for i in (ids or []) if i]
    if not ids:
        return 0
    stamp = _iso(now or _utcnow())
    with _db() as conn:
        cursor = conn.execute(
            "UPDATE items SET last_accessed = ?, access_count = access_count + 1 "
            "WHERE id IN (%s)" % ", ".join("?" for _ in ids),
            [stamp, *ids],
        )
        return cursor.rowcount


# ---------------------------------------------------------------------------
# Hybrid retrieval
# ---------------------------------------------------------------------------


def _tokens(text: Any) -> List[str]:
    return [tok.lower() for tok in tokenize(str(text or "")) if tok and tok.strip()]


def bm25_scores(query: str, docs: Sequence[Tuple[str, str]]) -> Dict[str, float]:
    """BM25-lite over ``tokenize()``, normalised to 0..1 by the top hit.

    Stdlib only and deliberately small: the corpus is one user's learned
    items, not a web index.
    """
    query_tokens = _tokens(query)
    if not query_tokens or not docs:
        return {}
    tokenised = [(doc_id, _tokens(text)) for doc_id, text in docs]
    lengths = [len(tokens) for _, tokens in tokenised]
    total_docs = len(tokenised)
    avgdl = (sum(lengths) / total_docs) if total_docs else 0.0
    if avgdl <= 0:
        return {}
    df: Dict[str, int] = {}
    for _, tokens in tokenised:
        for term in set(tokens):
            df[term] = df.get(term, 0) + 1
    scores: Dict[str, float] = {}
    for doc_id, tokens in tokenised:
        if not tokens:
            continue
        length = len(tokens)
        total = 0.0
        for term in set(query_tokens):
            freq = tokens.count(term)
            if not freq:
                continue
            idf = math.log(1.0 + (total_docs - df.get(term, 0) + 0.5) / (df.get(term, 0) + 0.5))
            total += idf * (freq * (BM25_K1 + 1.0)) / (
                freq + BM25_K1 * (1.0 - BM25_B + BM25_B * length / avgdl))
        if total > 0:
            scores[doc_id] = total
    top = max(scores.values()) if scores else 0.0
    return {doc_id: value / top for doc_id, value in scores.items()} if top > 0 else {}


_OBJ_RE = re.compile(r"\bOBJ-\d+\b", re.IGNORECASE)
_PATHY_RE = re.compile(r"^[\w./\\-]+\.[A-Za-z0-9]{1,6}$")


def graph_keys(text: Any) -> set:
    """The OBJ-ids and file-path tokens in a string — the join key between a
    query and an item's evidence refs."""
    raw = str(text or "")
    keys = {match.upper() for match in _OBJ_RE.findall(raw)}
    for token in re.split(r"[\s,;:()\[\]{}\"'<>]+", raw):
        token = token.strip().rstrip(".,;:")
        if not token or len(token) < 3:
            continue
        if "/" in token or "\\" in token or _PATHY_RE.match(token):
            lowered = token.lower()
            keys.add(lowered)
            base = re.split(r"[/\\]", lowered)[-1]
            if base:
                keys.add(base)
    return keys


def _item_graph_keys(item: Dict[str, Any]) -> set:
    keys: set = set()
    for span in item.get("evidence") or []:
        if not isinstance(span, dict):
            continue
        keys |= graph_keys(span.get("ref"))
    return keys


def _semantic_scores(query: str, ids: Sequence[str]) -> Tuple[Dict[str, float], bool]:
    """(scores, available). ``available`` False = the lane is simply absent."""
    store = vector_store()
    if not store or not query.strip() or not ids:
        return {}, bool(store)
    try:
        hits = store.search(query, k=max(8, min(64, len(ids) * 2))) or []
    except Exception as exc:  # noqa: BLE001 - a sick vector store is a degradation
        logger.debug("memory engine: semantic lane failed (%s); lexical only", exc)
        return {}, False
    wanted = set(ids)
    scores: Dict[str, float] = {}
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        mid = str(hit.get("memory_id") or "")
        if mid not in wanted:
            continue
        try:
            score = float(hit.get("score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        scores[mid] = max(0.0, min(1.0, score))
    return scores, True


def search(
    query: Any,
    owner: Optional[str] = None,
    project: Optional[str] = None,
    k: int = 8,
    *,
    now: Optional[datetime] = None,
    levels: Optional[Sequence[str]] = None,
    statuses: Sequence[str] = ("active", "anti_pattern"),
    touch_hits: bool = True,
) -> List[Dict[str, Any]]:
    """Hybrid retrieval: lexical 0.45 + semantic 0.45 + evidence graph 0.10,
    multiplied by the item's own ``max(effective_score, 0.05)``.

    With no vector store the lexical lane is renormalised to 0.90 and every
    row carries ``degraded: True`` — the caller can SEE the lane is missing
    instead of silently getting worse answers.
    """
    now = now or _utcnow()
    items = scoped_items(owner, project, statuses)
    if levels:
        wanted_levels = {str(level) for level in levels}
        items = [item for item in items if item.get("level") in wanted_levels]
    if not items:
        return []
    ids = [item["id"] for item in items]
    lexical = bm25_scores(str(query or ""), [(item["id"], item["text"]) for item in items])
    semantic, semantic_available = _semantic_scores(str(query or ""), ids)
    degraded = not semantic_available
    w_lex = W_LEXICAL_DEGRADED if degraded else W_LEXICAL
    w_sem = 0.0 if degraded else W_SEMANTIC
    query_keys = graph_keys(query)

    scored: List[Dict[str, Any]] = []
    for item in items:
        lex = lexical.get(item["id"], 0.0)
        sem = semantic.get(item["id"], 0.0)
        graph = 0.0
        if query_keys:
            overlap = query_keys & _item_graph_keys(item)
            graph = len(overlap) / len(query_keys)
        relevance = w_lex * lex + w_sem * sem + W_GRAPH * graph
        if relevance <= 0:
            continue
        row = public_item(item, now)
        row["relevance"] = round(relevance, 6)
        row["lexical"] = round(lex, 6)
        row["semantic"] = round(sem, 6)
        row["graph"] = round(graph, 6)
        row["degraded"] = degraded
        row["score"] = round(relevance * max(row["effective_score"], SCORE_FLOOR), 6)
        scored.append(row)

    scored.sort(key=lambda row: (-row["score"], row["id"]))
    hits = scored[:max(1, min(100, int(k or 8)))]
    if touch_hits and hits:
        with contextlib.suppress(Exception):
            touch([row["id"] for row in hits], now)
    return hits


# ---------------------------------------------------------------------------
# pack() — the deterministic context block
# ---------------------------------------------------------------------------


def _pack_line(item: Dict[str, Any]) -> str:
    """``- [id8] text (maturity)``. Anti-pattern text already reads
    ``AVOID: ...`` because inversion rewrote it, so nothing special here."""
    return f"- [{str(item.get('id') or '')[:8]}] {item.get('text') or ''} ({item.get('maturity')})"


def pack_detail(
    owner: Optional[str] = None,
    project: Optional[str] = None,
    query: Any = "",
    char_budget: int = DEFAULT_PACK_CHARS,
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Build the block AND report which item ids went into it.

    Deterministic: same store + same clock → byte-identical output, which is
    what makes it safe to put in a prompt (KV-cache) and to attribute the
    turn's outcome back to exactly these ids.
    """
    now = now or _utcnow()
    budget = max(0, int(char_budget or 0))
    if budget <= 0:
        return {"text": "", "ids": [], "items": [], "degraded": False}

    everything = scoped_items(owner, project, ("active", "anti_pattern"))
    by_id = {item["id"]: item for item in everything}

    rules = [public_item(item, now) for item in everything
             if item.get("status") == "active" and item.get("level") == "procedural"]
    rules = [row for row in rules if row["effective_score"] > 0]
    rules.sort(key=lambda row: (-row["effective_score"], row["id"]))

    # Anti-patterns are never score-filtered: an inverted rule has a deeply
    # negative score BY CONSTRUCTION (that is why it was inverted), and the
    # warning is the whole point of keeping it.
    antis = [public_item(item, now) for item in everything
             if item.get("status") == "anti_pattern"]
    antis.sort(key=lambda row: (-row["effective_score"], row["id"]))

    hits: List[Dict[str, Any]] = []
    degraded = False
    if str(query or "").strip():
        hits = search(query, owner, project, k=8, now=now,
                      levels=("semantic", "episodic", "working"),
                      statuses=("active",), touch_hits=False)
        degraded = bool(hits and hits[0].get("degraded"))
        hits = [row for row in hits if row["effective_score"] > 0]

    sections = (
        (PACK_RULES_HEADER, rules),
        (PACK_MEMORIES_HEADER, hits),
        (PACK_ANTI_HEADER, antis),
    )
    lines: List[str] = []
    ids: List[str] = []
    for header, entries in sections:
        pending_header = True
        for row in entries:
            extra = ([""] if lines else []) + ([header] if pending_header else [])
            extra.append(_pack_line(row))
            if len("\n".join(lines + extra)) > budget:
                break               # this section is full; a shorter one may still fit
            lines.extend(extra)
            ids.append(row["id"])
            pending_header = False

    if not ids:
        return {"text": "", "ids": [], "items": [], "degraded": degraded}
    with contextlib.suppress(Exception):
        touch(ids, now)
    return {
        "text": "\n".join(lines),
        "ids": ids,
        "degraded": degraded,
        "items": [by_id[i] for i in ids if i in by_id],
    }


def pack(
    owner: Optional[str] = None,
    project: Optional[str] = None,
    query: Any = "",
    char_budget: int = DEFAULT_PACK_CHARS,
    *,
    now: Optional[datetime] = None,
) -> str:
    """The exact block the model sees, or ``""``. NEVER raises: this is on
    the chat hot path, and a broken store must cost the block, not the turn."""
    try:
        return pack_detail(owner, project, query, char_budget, now=now)["text"]
    except Exception as exc:  # noqa: BLE001 - hot path
        logger.debug("memory engine: pack failed (%s); no block this turn", exc)
        return ""


# ---------------------------------------------------------------------------
# The learning loop — which ids went into a turn, and how the turn ended
# ---------------------------------------------------------------------------

INJECTED_TTL_S = 3600.0
INJECTED_MAX_KEYS = 256

_injected_lock = threading.Lock()
_INJECTED: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()


def _sweep_injected(now_ts: float) -> None:
    """Caller holds the lock. Drop expired keys, then the oldest overflow."""
    for key in [k for k, v in _INJECTED.items()
                if now_ts - float(v.get("ts") or 0) > INJECTED_TTL_S]:
        _INJECTED.pop(key, None)
    while len(_INJECTED) > INJECTED_MAX_KEYS:
        _INJECTED.popitem(last=False)


def note_injected(key: Any, ids: Iterable[str], *, now_ts: Optional[float] = None) -> None:
    """Remember that these item ids were put in front of the model for `key`
    (the session/run id). Never raises."""
    try:
        key = str(key or "").strip()
        ids = [str(i) for i in (ids or []) if i]
        if not key or not ids:
            return
        stamp = float(now_ts if now_ts is not None else time.time())
        with _injected_lock:
            _INJECTED[key] = {"ids": ids, "ts": stamp}
            _INJECTED.move_to_end(key)
            _sweep_injected(stamp)
    except Exception as exc:  # noqa: BLE001 - prompt path
        logger.debug("memory engine: note_injected failed: %s", exc)


def peek_injected(key: Any) -> List[str]:
    with _injected_lock:
        entry = _INJECTED.get(str(key or ""))
        return list(entry.get("ids") or []) if entry else []


def take_injected(key: Any) -> List[str]:
    """Pop the ids for one turn — an outcome is attributed exactly once."""
    with _injected_lock:
        entry = _INJECTED.pop(str(key or ""), None)
        return list(entry.get("ids") or []) if entry else []


def clear_injected() -> None:
    with _injected_lock:
        _INJECTED.clear()


def outcome_from_harness(summary: Optional[Dict[str, Any]]) -> Optional[str]:
    """Read the turn's verification signal: ``"pass"``, ``"fail"`` or None.

    The project's tests are the strongest signal and win; the auto-reviewer's
    verdict is the fallback. Anything inconclusive, skipped or unparsed
    returns None — an unmeasured turn must never manufacture feedback.
    """
    if not isinstance(summary, dict):
        return None
    tests = summary.get("tests")
    if isinstance(tests, dict) and tests.get("ran") and not tests.get("inconclusive"):
        return "pass" if tests.get("ok") else "fail"
    review = summary.get("review")
    if isinstance(review, dict):
        verdict = str(review.get("verdict") or "")
        if verdict == "ok":
            return "pass"
        if verdict == "issues":
            return "fail"
    return None


def record_outcome(
    key: Any,
    outcome: Any,
    *,
    ref: Optional[str] = None,
    reason: str = "",
    weight: float = 1.0,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Attribute one turn's verification result to the rules that were in its
    prompt. Feedback lands on PROCEDURAL items only — those are the rules the
    model was asked to follow, so they are the ones a pass or a fail is
    evidence about. Never raises."""
    result: Dict[str, Any] = {"kind": None, "ids": [], "applied": 0}
    try:
        if outcome in (True, "pass", "passed", "ok", "helpful"):
            kind = "helpful"
        elif outcome in (False, "fail", "failed", "issues", "harmful"):
            kind = "harmful"
        else:
            # No signal is not a neutral signal: record nothing at all.
            take_injected(key)
            return result
        ids = take_injected(key)
        if not ids:
            return result
        result["kind"] = kind
        applied: List[str] = []
        for item_id in ids:
            item = get_item(item_id)
            if not item or item.get("level") != "procedural":
                continue
            if add_feedback(item_id, kind, reason=reason,
                            ref=str(ref or key or ""), weight=weight, now=now):
                applied.append(item_id)
        result["ids"] = applied
        result["applied"] = len(applied)
    except Exception as exc:  # noqa: BLE001 - turn-end hook
        logger.debug("memory engine: record_outcome failed: %s", exc)
    return result


# ---------------------------------------------------------------------------
# Settings + the Curator entry point
# ---------------------------------------------------------------------------


def injection_enabled() -> bool:
    try:
        from src.settings import get_setting
        return bool(get_setting("agent_learned_memory", True))
    except Exception:  # noqa: BLE001
        return True


def injection_budget() -> int:
    try:
        from src.settings import get_setting
        value = int(get_setting("agent_learned_memory_chars", DEFAULT_PACK_CHARS))
    except Exception:  # noqa: BLE001
        value = DEFAULT_PACK_CHARS
    return max(0, min(20000, value))


def curate(owner: Optional[str] = None, project: Optional[str] = None,
           now: Optional[datetime] = None) -> Dict[str, Any]:
    """Run layer 2 (src/memory_curator.py). Imported lazily so the store
    module never depends on the curator at import time."""
    from src.memory_curator import curate as _curate
    return _curate(owner=owner, project=project, now=now)


def stats(owner: Optional[str] = None, project: Optional[str] = None) -> Dict[str, Any]:
    """Counts for the dashboard header. Never raises."""
    try:
        items = scoped_items(owner, project, STATUSES)
    except Exception:  # noqa: BLE001
        return {"total": 0, "active": 0, "anti_pattern": 0, "deprecated": 0,
                "semantic_lane": False}
    return {
        "total": len(items),
        "active": len([i for i in items if i.get("status") == "active"]),
        "anti_pattern": len([i for i in items if i.get("status") == "anti_pattern"]),
        "deprecated": len([i for i in items if i.get("status") == "deprecated"]),
        "semantic_lane": bool(_vector_state.get("store")),
    }
