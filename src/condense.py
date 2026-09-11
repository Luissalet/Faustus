"""src/condense.py — manual condense of a turn range (CONTRATO_CABLES2 F3).

Unlike `src.context_compactor.maybe_compact` (an automatic, background
compaction that fires when a PROMPT nears the model's context window), this
is a user-requested, one-time collapse of an explicit range of
`session.history` ROWS into a single summary row — "I know these turns are
settled, fold them so every future turn stops paying to resend them", not a
context-budget safety valve. It shares the exact same self-summary prompt,
model resolution and privacy gate (`src.context_compactor.summarize_rows`),
so a manually condensed range and an automatically compacted one read the
same to the model — but it operates directly on the durable
`session.history` list by row index, never on a prompt already filtered and
annotated by `build_chat_context`, since the whole point here is to shrink
the STORED transcript, not just what one turn's prompt happens to send.

Owner-scoped like `src.side_threads`: a session belonging to someone else is
treated as not found, never distinguished from "does not exist". Errors are
the same flat `{"error", "error_class"}` shape (`condense.<reason>`) via
`CondenseError`, raised for every route (`routes/condense_routes.py`) to
hand back verbatim.

Undo: `condense()` embeds the FULL original rows (role/content/metadata,
minus the DB-assigned `_db_id`) in the summary row's own
`metadata["condensed_from"]`; `expand()` reads that back. CONTRATO_CABLES2
mentions `src.chat_versions` as an optional extra safety net ("si la API lo
permite sin cambios"); this module does not wire it in — that helper is
shaped around SAVING A TRUNCATED TAIL for `/truncate`'s "keep_count" undo,
not an arbitrary MID-history range, and stretching it to fit here would
make `condensed_from` redundant with a second, differently-shaped copy of
the same rows. `condensed_from` alone is a complete, exact undo already
(round-tripped byte-for-byte by `test_condense.py`), so it stays the single
mechanism.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.models import ChatMessage
from src.context_compactor import SUMMARY_MAX_TOKENS, summarize_rows
from src.model_context import estimate_tokens

logger = logging.getLogger(__name__)

CONDENSE_LLM_TIMEOUT_S = 300

_TEMPLATE_HEADER = re.compile(
    r"^\s*\*\*Turns summarized:\*\*[^\n]*\n+", re.IGNORECASE,
)


def _strip_template_header(summary: str) -> str:
    """The compactor prompt opens with a bookkeeping line (`**Turns
    summarized:** N | **Compactions so far:** M`) that models echo back.
    Inside the live compactor it is harmless; on a card the user reads, it
    is noise that also lies (there was no "compaction"). Drop that line
    only — the rest of the summary is the model's."""
    return _TEMPLATE_HEADER.sub("", summary or "", count=1).lstrip()



class CondenseError(Exception):
    """A request the caller must fix — `routes/condense_routes.py` hands
    this back verbatim as the flat `{"error", "error_class"}` body every
    other CONTRATO_CABLES2/CONTRATO_EXCURSOS route already uses."""

    def __init__(self, message: str, error_class: str, status: int = 400):
        super().__init__(message)
        self.error_class = error_class
        self.status = status


def _owned_session(session_manager, owner: Optional[str], session_id: Optional[str]):
    """`session_manager.get_session(session_id)`, owner-checked exactly like
    `src.side_threads._owned_session_via_manager` — a session belonging to
    someone else is treated as not found, never distinguished from "does
    not exist". `owner=None` is unscoped (single-user mode)."""
    if session_manager is None or not session_id:
        return None
    try:
        sess = session_manager.get_session(session_id)
    except KeyError:
        return None
    except Exception:
        logger.debug("condense: get_session(%s) failed", session_id, exc_info=True)
        return None
    if sess is None:
        return None
    if owner is not None and getattr(sess, "owner", None) != owner:
        return None
    return sess


def _row_metadata(row) -> Dict[str, Any]:
    meta = getattr(row, "metadata", None)
    return meta if isinstance(meta, dict) else {}


def _validate_range(history: List[Any], start: Any, end: Any) -> None:
    """The range rules shared by `preview` and `condense`.

    - `start`/`end` are plain (non-bool) ints.
    - `0 <= start < end <= len(history) - 2` — the LAST row of `history` is
      NEVER eligible: it is the live turn the user is mid-conversation
      with, not settled history.
    - The range covers at least 2 rows (implied by `start < end`, checked
      explicitly for a clearer error when it somehow is not).
    - No row in `[start, end]` is already condensed (`metadata.condensed`)
      or already folded by automatic compaction (`metadata.compacted`) —
      condensing a condensed row would silently nest summaries; the fix is
      to expand it first.
    """
    n = len(history)
    if (
        not isinstance(start, int) or isinstance(start, bool)
        or not isinstance(end, int) or isinstance(end, bool)
        or not (0 <= start < end <= n - 2)
    ):
        raise CondenseError(
            f"Range [{start}, {end}] is not valid for a {n}-row history "
            "(0 <= start < end <= len(history) - 2 — the last row is always protected)",
            "condense.range_invalid",
            400,
        )
    if end - start + 1 < 2:
        raise CondenseError(
            "A condensed range must cover at least 2 rows", "condense.range_invalid", 400
        )
    for row in history[start:end + 1]:
        meta = _row_metadata(row)
        if meta.get("condensed"):
            raise CondenseError(
                "That range contains an already-condensed row — expand it first",
                "condense.nested",
                409,
            )
        if meta.get("compacted"):
            raise CondenseError(
                "That range contains a row already folded by automatic compaction",
                "condense.nested",
                409,
            )


def _excerpt(content: Any, limit: int = 140) -> str:
    text = content if isinstance(content, str) else str(content if content is not None else "")
    text = " ".join(text.split())
    return text[:limit] + ("…" if len(text) > limit else "")


def preview(
    session_manager, owner: Optional[str], session_id: str, start: int, end: int
) -> Dict[str, Any]:
    """No LLM call. Validates the range and reports what condensing it would
    cost — `routes/condense_routes.py`'s `GET .../condense/preview` calls
    this on every start/end edit in the Studio's `CondenseDialog`.
    """
    sess = _owned_session(session_manager, owner, session_id)
    if sess is None:
        raise CondenseError(f"Session {session_id} not found", "condense.not_found", 404)

    history = list(sess.history or [])
    _validate_range(history, start, end)

    rows = history[start:end + 1]
    row_dicts = [{"role": r.role, "content": r.content} for r in rows]
    tokens_before = estimate_tokens(row_dicts)
    turns = [
        {"index": start + i, "role": r.role, "excerpt": _excerpt(r.content)}
        for i, r in enumerate(rows)
    ]
    return {
        "rows": len(rows),
        "tokens_before": tokens_before,
        "tokens_after_estimate": min(tokens_before, SUMMARY_MAX_TOKENS),
        "turns": turns,
    }


async def condense(
    session_manager, owner: Optional[str], session_id: str, start: int, end: int
) -> Dict[str, Any]:
    """Summarize `history[start:end+1]` and replace it with ONE `system` row
    carrying the summary plus a complete, exact record of what it replaced
    (`metadata.condensed_from`) — `expand()`'s only input.

    Persists through `SessionManager.replace_messages` — the same durable
    write `src.context_compactor._update_session_history` and
    `routes/history/history_routes.py`'s version-restore already use — so a
    condensed range is not a display-only trick; it actually shrinks what
    every future turn resends.
    """
    sess = _owned_session(session_manager, owner, session_id)
    if sess is None:
        raise CondenseError(f"Session {session_id} not found", "condense.not_found", 404)

    history = list(sess.history or [])
    _validate_range(history, start, end)

    rows = history[start:end + 1]
    row_dicts = [{"role": r.role, "content": r.content} for r in rows]
    tokens_before = estimate_tokens(row_dicts)

    try:
        summary_text = await summarize_rows(
            row_dicts,
            endpoint_url=sess.endpoint_url,
            model=sess.model,
            headers=getattr(sess, "headers", None),
            owner=owner,
            # A person asked for this and is waiting on it: unlike the
            # mid-turn compactor's 30s, give a cold local model time to load
            # (the route is exempt from app.py's 45s hard timeout for the
            # same reason).
            timeout=CONDENSE_LLM_TIMEOUT_S,
        )
    except Exception as e:
        raise CondenseError(f"Condense summary failed: {e}", "condense.summary_failed", 502) from e
    summary_text = _strip_template_header(summary_text)

    # Complete, exact undo material — role/content/metadata, minus `_db_id`
    # (a DB-assigned key `replace_messages` reissues fresh for every row it
    # writes; carrying the old one forward would make a restored row look
    # like it still had its original database identity).
    condensed_from: List[Dict[str, Any]] = []
    for row in rows:
        meta = _row_metadata(row)
        clean_meta = {k: v for k, v in meta.items() if k != "_db_id"} or None
        condensed_from.append({"role": row.role, "content": row.content, "metadata": clean_meta})

    summary_msg = ChatMessage(
        role="system",
        content=f"[Condensed: turns {start + 1}–{end + 1}]\n{summary_text}",
        metadata={
            "condensed": True,
            "condensed_from": condensed_from,
            "range": [start, end],
            "model": sess.model,
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
    )

    new_history = history[:start] + [summary_msg] + history[end + 1:]
    if not session_manager.replace_messages(session_id, new_history):
        raise CondenseError("Could not persist the condensed history", "condense.persist_failed", 500)

    tokens_after = estimate_tokens([{"role": "system", "content": summary_msg.content}])
    return {
        "summary_index": start,
        "removed": len(rows) - 1,
        "tokens_before": tokens_before,
        "tokens_after": tokens_after,
    }


def expand(
    session_manager, owner: Optional[str], session_id: str, summary_index: int
) -> Dict[str, Any]:
    """Restore the rows a condensed summary at `summary_index` replaced,
    byte-for-byte (role/content/metadata) from its own
    `metadata.condensed_from`. 404 `condense.not_condensed` when the row at
    that index is not a condensed summary at all (wrong index, already
    expanded, or never condensed)."""
    sess = _owned_session(session_manager, owner, session_id)
    if sess is None:
        raise CondenseError(f"Session {session_id} not found", "condense.not_found", 404)

    history = list(sess.history or [])
    if (
        not isinstance(summary_index, int) or isinstance(summary_index, bool)
        or not (0 <= summary_index < len(history))
    ):
        raise CondenseError(f"No row at index {summary_index}", "condense.not_condensed", 404)

    row = history[summary_index]
    meta = _row_metadata(row)
    condensed_from = meta.get("condensed_from")
    if not isinstance(condensed_from, list) or not condensed_from:
        raise CondenseError(
            f"Row {summary_index} is not a condensed summary", "condense.not_condensed", 404
        )

    restored = [
        ChatMessage(
            role=str(entry.get("role") or "assistant") if isinstance(entry, dict) else "assistant",
            content=entry.get("content") if isinstance(entry, dict) else None,
            metadata=entry.get("metadata") if isinstance(entry, dict) and isinstance(entry.get("metadata"), dict) else None,
        )
        for entry in condensed_from
    ]
    new_history = history[:summary_index] + restored + history[summary_index + 1:]
    if not session_manager.replace_messages(session_id, new_history):
        raise CondenseError("Could not restore the condensed range", "condense.persist_failed", 500)
    return {"restored": len(restored)}
