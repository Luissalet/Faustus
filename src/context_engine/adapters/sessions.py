"""
context_engine/adapters/sessions.py — the transcript, handed in rather than fetched.

This adapter does not read the session database, and that is a deliberate
refusal rather than an omission.  What the rest of Faustus uses to get a
transcript is ``sess.get_context_messages()`` on a ``Session`` object the chat
route already holds (``routes/chat_helpers.py``).  Getting one of those from an
id alone means ``SessionManager.get_session(session_id)``, and reading that
method makes three problems obvious:

* **It does not take an owner.**  Any id, from anywhere — including one a model
  wrote into a message — resolves to that session's messages.  The isolation
  rule in this package is that scope is applied *before* the read, and a
  by-id API with no scope argument cannot honour it.
* **It mutates.**  It hydrates the in-memory cache from SQLite, reconciles the
  message count, and stamps ``last_accessed``.  Building a context packet
  should not reorder a user's session list.
* **It is not the transcript the turn is using.**  A chat turn filters,
  compacts and annotates history before it sends it; a second, independent read
  of the same session would produce a subtly different conversation, and the
  packet would describe neither.

``src/session_search.py`` is the clean by-owner reader, but it answers a
different question — "which past sessions mention X" — over the whole corpus,
not "what has been said in this one".  It belongs to a future
``past_experiences`` source, not to ``recent_messages``.

So the caller that already has the messages provides them, once per process::

    from src.context_engine.adapters.sessions import set_history_provider
    set_history_provider(lambda session_id, owner: sess.get_context_messages())

With no provider installed the source reports ``available() == False`` and the
retrieval simply has no ``recent_messages`` section — visibly absent, which is
the honest state, rather than silently wrong.

Trust follows the speaker, because it has to: a user's message is a human
statement and an instruction, an assistant's message is a model's claim, and a
tool result is something the runtime observed.  Flattening the three into one
class is how a model's guess from four turns ago comes back as fact.

``source_ref`` scheme: ``session:<session_id>#<index>``, where ``index`` is the
position in the list the provider returned.  It is stable for as long as that
history is, which is the same guarantee the turn itself has.
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable, Iterator, List, Mapping, Optional, Sequence, Tuple

from ..candidates import RetrievalRequest, ThreadedSource, make_candidate
from ..contracts import ContextCandidate

logger = logging.getLogger(__name__)

#: ``(session_id, owner) -> sequence of message mappings`` in the shape
#: ``ChatMessage.to_dict()`` produces: ``{"role", "content", "timestamp"?,
#: "metadata"?}``.  The provider is responsible for the owner check, because
#: only the caller knows which session object it is holding.
HistoryProvider = Callable[[str, str], Sequence[Mapping[str, Any]]]

_LOCK = threading.RLock()
_PROVIDER: Optional[HistoryProvider] = None

# The chat loop owns the authoritative, already-shaped transcript.  A global
# provider cannot safely point at that per-request value: two simultaneous
# users would race and one packet could read the other's messages.  ContextVar
# follows the coroutine (and ``asyncio.to_thread`` copies its context), so the
# threaded source sees exactly the history scoped around this compilation.
_SCOPED_HISTORY: ContextVar[
    Optional[Tuple[str, str, Tuple[Mapping[str, Any], ...]]]
] = ContextVar("context_engine_history", default=None)

#: A single message longer than this is a pasted file, not a turn of
#: conversation; the head is enough to know it happened.
MAX_MESSAGE_CHARS = 4_000

_TRUST_BY_ROLE = {
    "user": ("human_explicit", "user_instruction"),
    "assistant": ("agent_assertion", "agent_claim"),
    "system": ("human_explicit", "system_policy"),
    "tool": ("observed", "observed_state"),
}


def set_history_provider(fn: Optional[HistoryProvider]) -> None:
    """Install the function that hands this source a session's messages.

    Passing None is the same as :func:`reset_history_provider`.
    """
    global _PROVIDER
    with _LOCK:
        _PROVIDER = fn


def reset_history_provider() -> None:
    global _PROVIDER
    with _LOCK:
        _PROVIDER = None


def history_provider() -> Optional[HistoryProvider]:
    with _LOCK:
        explicit = _PROVIDER
    if explicit is not None:
        return explicit
    if _SCOPED_HISTORY.get() is None:
        return None
    return _scoped_history_provider


def _scoped_history_provider(session_id: str, owner: str) -> Sequence[Mapping[str, Any]]:
    scoped = _SCOPED_HISTORY.get()
    if scoped is None:
        return ()
    wanted_session, wanted_owner, messages = scoped
    if str(session_id or "") != wanted_session or str(owner or "") != wanted_owner:
        # Scope mismatches are authorization failures, not fuzzy lookups.
        return ()
    return messages


@contextmanager
def history_scope(session_id: str, owner: str,
                  messages: Sequence[Mapping[str, Any]]) -> Iterator[None]:
    """Expose one turn's transcript only for the duration of its compile.

    The mappings are copied so a source can never mutate the hot-path list.
    Nested and concurrent compiles are isolated by the ContextVar token.
    """
    snapshot = tuple(dict(row) for row in messages or ()
                     if isinstance(row, Mapping))
    token = _SCOPED_HISTORY.set((str(session_id or ""), str(owner or ""), snapshot))
    try:
        yield
    finally:
        _SCOPED_HISTORY.reset(token)


class SessionSource(ThreadedSource):
    """``recent_messages`` — the tail of the conversation this turn belongs to."""

    source_id = "sessions"
    sections = ("recent_messages",)
    handles = ("session:",)

    def available(self) -> bool:
        return history_provider() is not None

    def _gate(self, req: RetrievalRequest) -> str:
        if not req.wants("recent_messages"):
            return "recent_messages not requested"
        if not req.allows("temporal"):
            return "the temporal lane is not open"
        if not req.session_id:
            return "no session_id on the request"
        return ""

    def _messages(self, req: RetrievalRequest) -> List[Mapping[str, Any]]:
        provider = history_provider()
        if provider is None:
            return []
        raw = provider(req.session_id, str(req.owner or ""))
        return [m for m in list(raw or []) if isinstance(m, Mapping)]

    def _search(self, req: RetrievalRequest) -> Sequence[ContextCandidate]:
        messages = self._messages(req)
        if not messages:
            return ()
        limit = req.top()
        # The tail, oldest first: the compiler renders a section in the order
        # it is given, and a conversation read backwards is not a conversation.
        start = max(0, len(messages) - limit)
        out: List[ContextCandidate] = []
        for index in range(start, len(messages)):
            candidate = self._candidate(messages[index], index, len(messages), req)
            if candidate is not None:
                out.append(candidate)
        return tuple(out)

    def _candidate(self, message: Mapping[str, Any], index: int, total: int,
                   req: RetrievalRequest) -> Optional[ContextCandidate]:
        content = message.get("content")
        if not isinstance(content, str):
            # Multimodal turns arrive as a list of blocks; the compiler has a
            # renderer for those and this source has no business flattening
            # them into prose that would then be quoted as what the user said.
            return None
        body = content.strip()
        if not body:
            return None
        role = str(message.get("role") or "user").lower()
        trust, authority = _TRUST_BY_ROLE.get(role, ("agent_assertion", "agent_claim"))
        return make_candidate(
            source_type="message",
            source_ref=f"session:{req.session_id}#{index}",
            section="recent_messages",
            title=f"{role} message {index + 1}/{total}",
            body=body[:MAX_MESSAGE_CHARS],
            lanes=("temporal",),
            # Recency as a score, newest = 1.0, so a ranker that mixes this
            # section with another has a number instead of a position.
            scores={"recency": round((index + 1) / max(1, total), 6)},
            trust_class=trust,
            authority=authority,
            observed_at=message.get("timestamp") or "",
            owner=req.owner,
            project_id=req.project_id,
            meta={"role": role, "index": index,
                  "truncated": len(body) > MAX_MESSAGE_CHARS},
        )

    def _fetch(self, source_ref: str,
               req: RetrievalRequest) -> Optional[ContextCandidate]:
        ref = str(source_ref or "")
        if not ref.startswith("session:") or "#" not in ref:
            return None
        session_id, _, raw_index = ref[8:].partition("#")
        if session_id != req.session_id:
            # The provider was installed for the session this turn belongs to;
            # answering for another id would be inventing an authorisation.
            return None
        try:
            index = int(raw_index)
        except (TypeError, ValueError):
            return None
        messages = self._messages(req)
        if not 0 <= index < len(messages):
            return None
        return self._candidate(messages[index], index, len(messages), req)


__all__ = ["SessionSource", "set_history_provider", "reset_history_provider",
           "history_provider", "history_scope", "HistoryProvider",
           "MAX_MESSAGE_CHARS"]
