"""
context_engine/adapters/session_store.py — the persisted transcript, read-only and owner-checked.

`adapters/sessions.py` explains why the ``recent_messages`` source does not
reach for ``SessionManager.get_session``: it takes no owner, it mutates (cache
hydration, ``last_accessed``), and it is not the transcript a live turn is
sending.  The live path answers the third point with ``history_scope`` — the
turn hands over the exact messages it is about to send.

That leaves every compile that has *no* live transcript with no history at
all: ``POST /api/context/compile``, the MCP ``context_compile`` tool and shadow
compiles all asked "what would the agent know?" and were told nothing about
the conversation they were asking from.  This module is the process-wide
provider for those, and it answers the first two objections directly:

* **owner before read.**  The session row is looked up first and its owner
  compared with the caller's before a single message is selected.  The rule
  is the one ``routes/context_engine_routes.py::_mine`` applies to packets: an
  empty caller owner is single-user mode and may read any session; an
  owner-less (legacy) session is install-wide; anything else must match
  exactly.  A mismatch is an empty history, never an error that confirms the
  session exists.
* **no writes.**  A plain ``SELECT`` over ``sessions`` and ``chat_messages``
  through a short-lived SQLAlchemy session.  Nothing is cached, touched,
  reconciled or reordered.

It never wins over a ``history_scope``: ``sessions.history_provider()``
consults the scope first, so a live turn always describes its own transcript.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Sequence

logger = logging.getLogger(__name__)

#: How much of the tail is read.  The source keeps ``req.top()`` of these; a
#: planner asking for more than forty turns of verbatim history is asking for
#: a summary, which is a different source.
MAX_PERSISTED_MESSAGES = 40

#: Roles that are conversation.  Tool rows and system notes that a route
#: chose to persist are bookkeeping, and quoting them back as "what was said"
#: is how a stale tool error resurfaces as a fact.
CONVERSATION_ROLES = ("user", "assistant")


def owner_may_read(session_owner: Any, owner: str) -> bool:
    """The `_mine` rule, restated for sessions (see the module docstring)."""
    stored = str(session_owner or "").strip()
    caller = str(owner or "").strip()
    return not caller or not stored or stored == caller


def _timestamp(value: Any) -> str:
    try:
        return value.isoformat() if value is not None else ""
    except Exception:  # noqa: BLE001 - a bad timestamp is an absent one
        return ""


def persisted_history(session_id: str, owner: str, *,
                      limit: int = MAX_PERSISTED_MESSAGES
                      ) -> Sequence[Mapping[str, Any]]:
    """The last ``limit`` conversation messages of one session, oldest first.

    Returns ``()`` for an unknown session, a session owned by somebody else,
    or any database failure — the source treats all three as "no history",
    which is the only answer that does not leak which of them it was."""
    wanted = str(session_id or "").strip()
    if not wanted:
        return ()
    try:
        cap = max(1, min(int(limit or MAX_PERSISTED_MESSAGES), 200))
    except (TypeError, ValueError):
        cap = MAX_PERSISTED_MESSAGES
    try:
        from core.database import ChatMessage as DbChatMessage
        from core.database import Session as DbSession
        from core.database import SessionLocal
        from core.session_manager import _parse_msg_content
    except Exception:  # noqa: BLE001 - no database layer, no history
        logger.debug("context history: the session database is not importable")
        return ()

    db = None
    try:
        db = SessionLocal()
        row = db.query(DbSession.id, DbSession.owner).filter(DbSession.id == wanted).first()
        if row is None or not owner_may_read(row.owner, owner):
            return ()
        rows = (db.query(DbChatMessage.role, DbChatMessage.content,
                         DbChatMessage.timestamp)
                .filter(DbChatMessage.session_id == wanted)
                .filter(DbChatMessage.role.in_(CONVERSATION_ROLES))
                .order_by(DbChatMessage.timestamp.desc())
                .limit(cap)
                .all())
        out: List[Dict[str, Any]] = []
        for role, content, stamp in reversed(rows):
            out.append({
                "role": str(role or "user"),
                "content": _parse_msg_content(content),
                "timestamp": _timestamp(stamp),
            })
        return tuple(out)
    except Exception as exc:  # noqa: BLE001 - a compile may never fail on history
        logger.debug("context history: could not read session %s: %s", wanted, exc)
        return ()
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass


def install_default_history_provider() -> bool:
    """Install :func:`persisted_history` as the process-wide provider.

    Called once at app startup (``app.py``) and by the MCP server on first
    use.  Idempotent; returns whether the provider is in place."""
    try:
        from .sessions import set_history_provider

        set_history_provider(persisted_history)
        return True
    except Exception as exc:  # noqa: BLE001 - startup must not fail on this
        logger.warning("context history provider could not be installed: %s", exc)
        return False


__all__ = ["MAX_PERSISTED_MESSAGES", "CONVERSATION_ROLES", "owner_may_read",
           "persisted_history", "install_default_history_provider"]
