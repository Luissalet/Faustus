"""
context_engine/standby.py — legacy context blocks held in reserve for the packet.

With `agent_context_engine` on, the live packet selects saved memory
(`pmem:` refs, memory.json) and documents (`doc:` refs, the RAG index) on its
own. The chat preface (`ChatProcessor.build_context_preface`) still builds its
classic blocks for the very same stores. Sending both shows the model the same
fact twice and bills the tokens twice; simply dropping the preface would leave
a round whose packet did not arrive (None, timeout, crash, no room) with no
saved memory and no documents at all — silently.

So these blocks get the treatment the learned-memory block already has in
`agent_loop` (see `_LEGACY_MEMORY_STANDBY_KEY` there): they are built exactly
as today and, only when the engine is on, tagged as a *standby*. A model call
that really carries a packet drops them; one that does not keeps them, or gets
them back in the same position with the same content and tag. Never both,
never neither.

With the flag off nothing here is ever called on a request path, so the
prompt stays byte-identical (the tag itself is a private `_` key that
`llm_core._sanitize_llm_messages` strips before any provider sees it).

Pure functions over message lists; nothing here raises on odd input.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: The private key the standby tag lives under. Shared with `agent_loop`'s
#: learned-memory standby (`_LEGACY_MEMORY_STANDBY_KEY`); the *value* says
#: which block it is.
STANDBY_KEY = "_context_engine_standby"

LEARNED_MEMORY = "learned_memory"
SAVED_MEMORY = "saved_memory"
DOCUMENTS = "documents"

#: `metadata.source` of a chat-preface block -> its standby kind. Only the
#: blocks whose content the packet can carry itself belong here: saved memory
#: (the memory adapter's `pmem:` refs) and retrieved documents (the documents
#: adapter's `doc:` refs). Web results, fetched pages, transcripts and the
#: skills index have no packet equivalent and are never put on standby.
PREFACE_STANDBY_SOURCES: Mapping[str, str] = {
    "saved memory: pinned context": SAVED_MEMORY,
    "saved memory: retrieved context": SAVED_MEMORY,
    "retrieved documents": DOCUMENTS,
}

PREFACE_KINDS: Tuple[str, ...] = (SAVED_MEMORY, DOCUMENTS)


def standby_kind(message: Any) -> str:
    """The standby kind of a message, or "" when it is not on standby."""
    if not isinstance(message, Mapping):
        return ""
    value = message.get(STANDBY_KEY)
    if not value:
        return ""
    return value if isinstance(value, str) else LEARNED_MEMORY


def is_standby(message: Any, kinds: Optional[Sequence[str]] = None) -> bool:
    """True for a standby block (of one of ``kinds`` when given)."""
    kind = standby_kind(message)
    if not kind:
        return False
    return kinds is None or kind in kinds


def mark_preface_standby(messages: List[Dict[str, Any]]) -> int:
    """Tag, in place, the preface blocks the packet can replace.

    Returns how many were tagged. Call it only when the engine is on: the tag
    is what makes the live path remove the block, so an untagged block is
    exactly today's behaviour."""
    tagged = 0
    for message in messages or ():
        if not isinstance(message, dict) or message.get(STANDBY_KEY):
            continue
        metadata = message.get("metadata")
        if not isinstance(metadata, Mapping):
            continue
        kind = PREFACE_STANDBY_SOURCES.get(str(metadata.get("source") or ""))
        if kind:
            message[STANDBY_KEY] = kind
            tagged += 1
    return tagged


def without_standby(messages: Sequence[Mapping[str, Any]],
                    kinds: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
    """The messages minus every standby block (of ``kinds`` when given)."""
    return [message for message in messages or ()
            if isinstance(message, Mapping) and not is_standby(message, kinds)]


def _fingerprint(message: Mapping[str, Any]) -> Tuple[str, str]:
    content = message.get("content")
    return (str(message.get("role") or ""),
            content if isinstance(content, str) else repr(content))


def capture(messages: Sequence[Mapping[str, Any]],
            kinds: Sequence[str] = PREFACE_KINDS) -> List[Dict[str, Any]]:
    """Remember the standby blocks of ``kinds`` and where each one sits.

    The anchor is the message right before the block (role + content), not an
    index or an object: the agent loop copies its message dicts every round
    and appends tool results at the end, but it never rewrites what comes
    before the conversation. The index is only the fallback for a prompt that
    was reshaped (compaction) in between."""
    stash: List[Dict[str, Any]] = []
    previous: Optional[Tuple[str, str]] = None
    for index, message in enumerate(messages or ()):
        if not isinstance(message, Mapping):
            continue
        if is_standby(message, kinds):
            stash.append({"message": dict(message), "after": previous, "index": index})
        previous = _fingerprint(message)
    return stash


def _latest_user_index(messages: Sequence[Mapping[str, Any]]) -> int:
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.get("role") == "user" and not message.get("_agent_injected"):
            return index
    return len(messages)


def restore(messages: Sequence[Mapping[str, Any]],
            stash: Sequence[Mapping[str, Any]]) -> Tuple[List[Dict[str, Any]], bool]:
    """Put captured standby blocks back where they were.

    Returns ``(messages, inserted)``. A block that is already present (same
    kind, same content) is not inserted again, so calling this twice — or on
    a round that never lost the blocks — is a no-op."""
    out: List[Dict[str, Any]] = [m for m in messages or () if isinstance(m, Mapping)]
    inserted = False
    for entry in stash or ():
        block = entry.get("message") if isinstance(entry, Mapping) else None
        if not isinstance(block, Mapping):
            continue
        kind = standby_kind(block)
        wanted = _fingerprint(block)
        if any(standby_kind(m) == kind and _fingerprint(m) == wanted for m in out):
            continue
        anchor = entry.get("after")
        position: Optional[int] = None
        if anchor is None:
            position = 0
        else:
            anchor = tuple(anchor)
            for index, message in enumerate(out):
                if _fingerprint(message) == anchor:
                    position = index + 1
                    break
        if position is None:
            try:
                position = int(entry.get("index") or 0)
            except (TypeError, ValueError):
                position = 0
        position = max(0, min(position, _latest_user_index(out)))
        out.insert(position, dict(block))
        inserted = True
    return out, inserted


__all__ = [
    "STANDBY_KEY", "LEARNED_MEMORY", "SAVED_MEMORY", "DOCUMENTS",
    "PREFACE_STANDBY_SOURCES", "PREFACE_KINDS",
    "standby_kind", "is_standby", "mark_preface_standby", "without_standby",
    "capture", "restore",
]
