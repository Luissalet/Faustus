"""
context_engine/recall.py — what the packet left out for budget, kept one call away.

OBJ-29.  The compiler already treats an omission as output rather than as a
log line: every candidate it discards produces a `ContextOmission` with a
reason.  What it could not do is give the omitted thing *back*.  When the
budget said no, the model saw nothing — not even that something existed — and
spent rounds rediscovering what the workspace already knew.

The pattern here is reversible compression: whatever did not fit is stored
locally under a short id, the id travels in the prompt (a compact footer in
the live packet), and one tool, `context_recall`, returns the full text with
its provenance on demand.

The trap is the one local backends already taught this codebase: the KV cache
keys off the prompt prefix byte for byte.  A footer that reshuffled itself
every round would invalidate the cache and cost more than it saves, so:

* the short id is a pure function of ``(owner, source_ref)`` — the same
  omitted source gets the same id on every round and every turn;
* the footer lists at most `FOOTER_ITEMS` entries, sorted by
  ``(section, source_ref)`` rather than by score, so a re-ranking among the
  same omitted set does not move a byte;
* the rendering is plain text with no timestamps, counts of tokens or scores.

Storage is the engine's own derived SQLite store (`store.py`), keyed by
``(owner, short_id)``.  Rows are owner-scoped on read — an id guessed from
another owner's footer resolves to nothing — and pruned by age and by count,
because a recall cache that grows forever is a second copy of every store.
A row whose body was not captured (an omission that did not pass through
`transforms.fit`) is reopened live through the same adapter registry the
fragment route uses (`candidates.fetch_ref`).
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from . import store
from .contracts import ContextCandidate, ContextPacket

logger = logging.getLogger(__name__)

#: How many omitted items the live footer names.  Enough to point at the
#: obvious misses; beyond that the footer itself becomes the budget problem.
FOOTER_ITEMS = 12

#: Characters of a short id.  40 bits per owner: collisions inside one
#: owner's recall table are not a practical concern.
SHORT_ID_CHARS = 10

#: `context_recall` returns at most this many items per call ...
MAX_RECALL_IDS = 10
#: ... and at most this much text per item (the tool result offload takes
#: over beyond that, so the cap is about one call, not about the store).
MAX_RECALL_CHARS = 20_000

#: Stored bodies are capped too: this is a recall cache, not an archive.
MAX_STORED_CHARS = 60_000

RETENTION_DAYS = 14
MAX_ROWS_PER_OWNER = 2_000

#: Omission reasons that are recoverable by recall.  Only budget: the other
#: reasons (unauthorised, quarantined, stale, policy...) are decisions, and
#: handing their content back on request would undo them.
RECALLABLE_REASONS = ("budget",)

_ID_RE = re.compile(r"^[0-9a-f]{%d}$" % SHORT_ID_CHARS)

store.register_schema("context_recall", (
    """CREATE TABLE IF NOT EXISTS context_recall (
        owner       TEXT NOT NULL DEFAULT '',
        short_id    TEXT NOT NULL,
        source_ref  TEXT NOT NULL,
        source_type TEXT NOT NULL DEFAULT '',
        section     TEXT NOT NULL DEFAULT '',
        title       TEXT NOT NULL DEFAULT '',
        body        TEXT NOT NULL DEFAULT '',
        body_chars  INTEGER NOT NULL DEFAULT 0,
        reason      TEXT NOT NULL DEFAULT 'budget',
        detail      TEXT NOT NULL DEFAULT '',
        packet_id   TEXT NOT NULL DEFAULT '',
        request_id  TEXT NOT NULL DEFAULT '',
        session_id  TEXT NOT NULL DEFAULT '',
        project_id  TEXT NOT NULL DEFAULT '',
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        PRIMARY KEY (owner, short_id)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_context_recall_updated "
    "ON context_recall(owner, updated_at)",
))


# ── ids ────────────────────────────────────────────────────────────────────

def short_id(owner: str, source_ref: str) -> str:
    """Deterministic, owner-scoped id for one omitted source (see module doc)."""
    raw = f"{owner or ''}\x00{source_ref or ''}".encode("utf-8", "replace")
    return hashlib.sha1(raw).hexdigest()[:SHORT_ID_CHARS]


def normalize_id(value: Any) -> str:
    """``"[ctx:ab12cd34ef]"``, ``"ctx:ab12cd34ef"`` and ``"ab12cd34ef"`` are
    the same id; anything else is not an id."""
    text = str(value or "").strip().strip("[]").strip()
    if text.lower().startswith("ctx:"):
        text = text[4:]
    text = text.strip().lower()
    return text if _ID_RE.match(text) else ""


# ── writing ────────────────────────────────────────────────────────────────

def _one_line(value: Any, limit: int = 120) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def recallable_omissions(packet: ContextPacket) -> List[Any]:
    """The packet's budget omissions, deduplicated by ref, in packet order."""
    seen = set()
    out = []
    for omission in packet.omissions or ():
        ref = str(getattr(omission, "source_ref", "") or "")
        if not ref or ref in seen:
            continue
        if getattr(omission, "reason", "") not in RECALLABLE_REASONS:
            continue
        if not getattr(omission, "recoverable", True):
            continue
        seen.add(ref)
        out.append(omission)
    return out


#: Source types that must never be written to the recall cache, whatever the
#: policy: `message` is a `recent_messages`/session turn, and it is already
#: sitting in the conversation the model is about to see. A second, longer-
#: lived copy of it in a store keyed by short id would outlive the reasons
#: (incognito, a deleted turn, a rotated session) the original might go away.
NEVER_STORED_SOURCE_TYPES = ("message",)


def remember(packet: ContextPacket, omitted: Mapping[str, ContextCandidate], *,
             owner: str, session_id: str = "", project_id: str = "",
             allow_personal_memory: bool = True) -> List[Dict[str, str]]:
    """Store every recallable omission of ``packet`` and return one entry per
    stored row: ``{"id", "source_ref", "source_type", "section", "title"}``.

    ``omitted`` is what `transforms.collect_budget_omissions` captured during
    the compile (``source_ref -> candidate``).  An omission with no captured
    candidate is stored as a reference only and reopened live on recall.

    ``allow_personal_memory`` is the caller's `ContextPolicy.allow_personal_memory`
    for this request — incognito and "no memory" both arrive as `False`. When
    it is `False` nothing is stored at all: a budget cut is reversible by
    design, an incognito cut must not become reversible as a side effect of
    that same cache. Independent of that flag, `NEVER_STORED_SOURCE_TYPES`
    omissions (recent messages) are never stored either.

    Never raises: a recall store that fails costs the footer, not the turn."""
    if not allow_personal_memory:
        return []
    rows = [o for o in recallable_omissions(packet)
            if str(getattr(o, "source_type", "") or "") not in NEVER_STORED_SOURCE_TYPES]
    if not rows:
        return []
    who = str(owner or "")
    stamp = store.now_iso()
    entries: List[Dict[str, str]] = []
    try:
        with store.db() as conn:
            for omission in rows:
                ref = str(omission.source_ref)
                candidate = omitted.get(ref) if isinstance(omitted, Mapping) else None
                body = str(getattr(candidate, "body", "") or "")[:MAX_STORED_CHARS]
                title = _one_line(getattr(candidate, "title", "") or "") or ""
                section = str(getattr(candidate, "section", "") or "")
                ident = short_id(who, ref)
                conn.execute(
                    """INSERT INTO context_recall
                           (owner, short_id, source_ref, source_type, section, title,
                            body, body_chars, reason, detail, packet_id, request_id,
                            session_id, project_id, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(owner, short_id) DO UPDATE SET
                           source_ref=excluded.source_ref,
                           source_type=excluded.source_type,
                           section=CASE WHEN excluded.section != '' THEN excluded.section
                                        ELSE context_recall.section END,
                           title=CASE WHEN excluded.title != '' THEN excluded.title
                                      ELSE context_recall.title END,
                           body=CASE WHEN excluded.body != '' THEN excluded.body
                                     ELSE context_recall.body END,
                           body_chars=CASE WHEN excluded.body != '' THEN excluded.body_chars
                                           ELSE context_recall.body_chars END,
                           reason=excluded.reason, detail=excluded.detail,
                           packet_id=excluded.packet_id, request_id=excluded.request_id,
                           session_id=excluded.session_id, project_id=excluded.project_id,
                           updated_at=excluded.updated_at""",
                    (who, ident, ref, str(omission.source_type or ""), section, title,
                     body, len(body), str(omission.reason or "budget"),
                     str(omission.detail or "")[:512], str(packet.packet_id or ""),
                     str(packet.request_id or ""), str(session_id or ""),
                     str(project_id or ""), stamp, stamp),
                )
                entries.append({"id": ident, "source_ref": ref,
                                "source_type": str(omission.source_type or ""),
                                "section": section, "title": title})
            _prune(conn, who)
    except Exception as exc:  # noqa: BLE001 - never end a turn over a cache
        logger.warning("context recall could not store omissions: %s", exc)
        return []
    return entries


def _prune(conn: Any, owner: str) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
              ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    conn.execute("DELETE FROM context_recall WHERE owner = ? AND updated_at < ?",
                 (owner, cutoff))
    conn.execute(
        """DELETE FROM context_recall WHERE owner = ? AND short_id IN (
               SELECT short_id FROM context_recall WHERE owner = ?
               ORDER BY updated_at DESC, short_id LIMIT -1 OFFSET ?)""",
        (owner, owner, MAX_ROWS_PER_OWNER),
    )


# ── the footer ─────────────────────────────────────────────────────────────

def footer_lines(entries: Sequence[Mapping[str, Any]], *,
                 limit: int = FOOTER_ITEMS) -> List[str]:
    """``[ctx:<id>] <title> (<source_ref>)`` for up to ``limit`` entries.

    The first ``limit`` entries in packet order are chosen (the compiler's
    own priority), then rendered sorted by ``(section, source_ref)`` so the
    text is byte-stable whenever the same set is omitted."""
    chosen = [dict(e) for e in list(entries or ())[: max(0, int(limit))]
              if e.get("id") and e.get("source_ref")]
    chosen.sort(key=lambda e: (str(e.get("section") or ""), str(e["source_ref"])))
    lines: List[str] = []
    for entry in chosen:
        ref = _one_line(entry["source_ref"], 160)
        title = _one_line(entry.get("title") or "")
        label = f"{title} ({ref})" if title and title != ref else ref
        lines.append(f"[ctx:{entry['id']}] {label}")
    return lines


def render_footer(entries: Sequence[Mapping[str, Any]], *,
                  limit: int = FOOTER_ITEMS) -> str:
    """The footer block appended to the live packet, or ``""``."""
    lines = footer_lines(entries, limit=limit)
    if not lines:
        return ""
    total = len([e for e in entries or () if e.get("id")])
    head = ("\n## omitted_for_budget\n"
            "Left out of this packet to fit the budget, still available. Call "
            "`context_recall` with the ids to read one in full.")
    more = (f"\n(+{total - len(lines)} more omitted, not listed)"
            if total > len(lines) else "")
    return head + "\n" + "\n".join(lines) + more


# ── reading ────────────────────────────────────────────────────────────────

def _row(owner: str, ident: str) -> Optional[Dict[str, Any]]:
    with store.db() as conn:
        rows = store.rows(conn.execute(
            "SELECT * FROM context_recall WHERE owner = ? AND short_id = ?",
            (str(owner or ""), ident)))
    return rows[0] if rows else None


def _shape(ident: str, row: Mapping[str, Any], body: str, provenance: str) -> Dict[str, Any]:
    text = str(body or "")
    return {
        "id": ident,
        "found": True,
        "source_ref": row.get("source_ref") or "",
        "source_type": row.get("source_type") or "",
        "section": row.get("section") or "",
        "title": row.get("title") or "",
        "content": text[:MAX_RECALL_CHARS],
        "chars": len(text),
        "truncated": len(text) > MAX_RECALL_CHARS,
        "omitted_because": row.get("reason") or "budget",
        "omission_detail": row.get("detail") or "",
        "packet_id": row.get("packet_id") or "",
        "session_id": row.get("session_id") or "",
        "project_id": row.get("project_id") or "",
        "stored_at": row.get("updated_at") or "",
        "provenance": provenance,
    }


def _missing(ident: str, raw: Any, note: str) -> Dict[str, Any]:
    return {"id": ident or str(raw or ""), "found": False, "content": "", "note": note}


def _owner_mismatch(entry_owner: Any, owner: str) -> bool:
    stored = str(entry_owner or "")
    return bool(stored and owner and stored != owner)


#: `source_ref` prefixes this module knows how to re-check safely before
#: handing back a body it cached earlier. Anything else keeps today's
#: behaviour: a stored body for a prefix outside this list is returned as
#: before, and a prefix with no stored body already goes through
#: `_reopen_live`, which has its own access checks.
_RECHECKED_PREFIXES = ("mem:", "pmem:", "ent:", "note:")


def _source_still_available(ref: str, owner: str) -> bool:
    """Re-check one omission's source right before a cached body is handed
    back (see module doc, requirement (b) of the recall-cache privacy fix).

    A stored body is a snapshot: the packet compiled it once, and nothing
    since has re-read the source. If the owner forgot the memory, suppressed
    it, marked it secret, or deleted/hid the note or entity that produced it
    in the meantime, the cache must not be the one place that memory still
    answers. Only the prefixes this function understands are checked; every
    other kind of reference is treated as still available (unchanged
    behaviour), because this module does not know how to open it safely."""
    if ref.startswith("mem:"):
        try:
            import src.memory_engine as engine

            item = engine.get_item(ref[4:])
        except Exception as exc:  # noqa: BLE001 - an unreadable store is not "gone"
            logger.debug("context recall could not recheck %s: %s", ref, exc)
            return True
        if not item:
            return False
        if item.get("suppressed"):
            return False
        if str(item.get("sensitivity") or "") == "secret":
            return False
        return not _owner_mismatch(item.get("owner"), owner)
    if ref.startswith("pmem:"):
        wanted = ref[5:]
        try:
            from src.constants import DATA_DIR
            from src.memory import MemoryManager

            entries = MemoryManager(DATA_DIR).load(owner) or []
        except Exception as exc:  # noqa: BLE001
            logger.debug("context recall could not recheck %s: %s", ref, exc)
            return True
        return any(str(entry.get("id") or "") == wanted for entry in entries)
    if ref.startswith("ent:"):
        try:
            from src.brain import entities

            entity = entities.get_entity(ref[4:])
        except Exception as exc:  # noqa: BLE001
            logger.debug("context recall could not recheck %s: %s", ref, exc)
            return True
        if not entity or entity.get("hidden"):
            return False
        return not _owner_mismatch(entity.get("owner"), owner)
    if ref.startswith("note:"):
        try:
            from src.brain import notes

            notes.read_note(owner, ref[5:])
        except (FileNotFoundError, ValueError):
            return False
        except Exception as exc:  # noqa: BLE001
            logger.debug("context recall could not recheck %s: %s", ref, exc)
            return True
        return True
    return True


def _delete_row(owner: str, ident: str) -> None:
    try:
        with store.db() as conn:
            conn.execute("DELETE FROM context_recall WHERE owner = ? AND short_id = ?",
                        (owner, ident))
    except Exception as exc:  # noqa: BLE001 - a stale row left behind is a cache
                              # miss later, not a leak: the check above already
                              # refused to return its body.
        logger.warning("context recall could not purge stale row %s: %s", ident, exc)


def purge_source(owner: str, source_ref: str) -> int:
    """Delete every cached recall row this owner holds for ``source_ref``.

    Not called anywhere yet — see ``R_wiring.md`` for where the memory,
    entity and note deletion/suppression paths should call it so a short id
    minted before the removal cannot hand the body back afterward. Recall's
    own re-check (`_source_still_available`) is the safety net for the ids
    that slip through before those call sites are wired; this is the cleanup
    that keeps the cache from ever growing rows for things that are gone.
    Never raises: returns 0 on a store it could not reach."""
    who = str(owner or "")
    ref = str(source_ref or "")
    if not ref:
        return 0
    try:
        with store.db() as conn:
            cursor = conn.execute(
                "DELETE FROM context_recall WHERE owner = ? AND source_ref = ?",
                (who, ref))
            return int(cursor.rowcount or 0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("context recall could not purge %s for %s: %s", ref, who, exc)
        return 0


async def _reopen_live(row: Mapping[str, Any], *, owner: str) -> str:
    """The body of a row stored without one, through the adapter registry."""
    ref = str(row.get("source_ref") or "")
    if not ref:
        return ""
    try:
        from .candidates import RetrievalRequest, fetch_ref
        from .wiring import build_request

        request = build_request(owner=owner, session_id=str(row.get("session_id") or ""),
                                model="", project_id=str(row.get("project_id") or ""),
                                consumer="agent")
        candidate = await fetch_ref(ref, RetrievalRequest(request=request,
                                                          explicit_refs=(ref,)))
    except Exception as exc:  # noqa: BLE001 - a miss, not a failure
        logger.debug("context recall could not reopen %s: %s", ref, exc)
        return ""
    if candidate is None:
        return ""
    stored = str(candidate.owner or "")
    if stored and owner and stored != owner:
        return ""
    return str(candidate.body or "")


async def recall(ids: Iterable[Any], *, owner: str) -> List[Dict[str, Any]]:
    """Full content, with provenance, for each short id this owner holds.

    Unknown, malformed and other owners' ids come back ``found: False`` with
    the same note, so a probe learns nothing about what exists.  Never raises."""
    out: List[Dict[str, Any]] = []
    who = str(owner or "")
    seen = set()
    for raw in list(ids or ())[:MAX_RECALL_IDS]:
        ident = normalize_id(raw)
        if not ident:
            out.append(_missing("", raw, "not a context id (expected ctx:<10 hex>)"))
            continue
        if ident in seen:
            continue
        seen.add(ident)
        try:
            row = _row(who, ident)
        except Exception as exc:  # noqa: BLE001
            logger.warning("context recall could not read %s: %s", ident, exc)
            row = None
        if row is None:
            out.append(_missing(ident, raw, "no omitted item with this id for this owner "
                                            "(it may have expired)"))
            continue
        body = str(row.get("body") or "")
        if body:
            ref = str(row.get("source_ref") or "")
            if ref.startswith(_RECHECKED_PREFIXES) and not _source_still_available(ref, who):
                _delete_row(who, ident)
                item = _shape(ident, row, "", "the source no longer exists")
                item["found"] = False
                item["note"] = "the source this id points at is no longer available"
                out.append(item)
                continue
            out.append(_shape(ident, row, body, "stored when the packet omitted it"))
            continue
        live = await _reopen_live(row, owner=who)
        if live:
            out.append(_shape(ident, row, live, "reopened live from its source"))
        else:
            item = _shape(ident, row, "", "reference only; the source did not answer")
            item["found"] = False
            item["note"] = "the source this id points at could not be reopened"
            out.append(item)
    return out


def render_recall(results: Sequence[Mapping[str, Any]]) -> str:
    """The tool's text output: one block per id, provenance first."""
    blocks: List[str] = []
    for item in results or ():
        if not item.get("found"):
            blocks.append(f"[ctx:{item.get('id') or '?'}] not recalled: "
                          f"{item.get('note') or 'unknown id'}")
            continue
        head = (f"[ctx:{item['id']}] {item.get('title') or item.get('source_ref')}\n"
                f"source: {item.get('source_ref')} ({item.get('source_type')}"
                f"{', section ' + item['section'] if item.get('section') else ''})\n"
                f"provenance: {item.get('provenance')}; omitted from packet "
                f"{item.get('packet_id') or '?'} because {item.get('omitted_because')}")
        body = str(item.get("content") or "")
        if item.get("truncated"):
            body += f"\n… [{item.get('chars')} chars total; truncated at {MAX_RECALL_CHARS}]"
        blocks.append(head + "\n\n" + body)
    return "\n\n---\n\n".join(blocks)


__all__ = [
    "FOOTER_ITEMS", "SHORT_ID_CHARS", "MAX_RECALL_IDS", "MAX_RECALL_CHARS",
    "RECALLABLE_REASONS", "NEVER_STORED_SOURCE_TYPES", "short_id", "normalize_id",
    "recallable_omissions", "remember", "footer_lines", "render_footer", "recall",
    "render_recall", "purge_source",
]
