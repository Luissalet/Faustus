"""src/voice_brain.py — how a voice turn reaches the Second Brain (Darwin #992).

A voice turn is an ordinary chat turn: Studio's voice mode sends the
transcript through the same ``send()`` as the composer, and a streaming voice
session (``services/voice_stream.py``, the voice MCP server, a Hoard panel)
posts it once to ``/api/chat_stream``. So the brain is read and written on
exactly the text chat's path:

* **consulted** — personal memory recall (``routes/chat_helpers.py``,
  ``mem_enabled``) and, when the Context Engine is on, its ``brain`` source
  (``src/context_engine/adapters/brain.py``: vault notes and typed entities);
* **fed** — the background memory extraction after a turn
  (``chat_helpers.fire_post_response``, ``auto_memory``), whose items
  ``src/brain/extract.py`` turns into entities and relations.

The one switch is the chat's own ``no_memory`` flag (the composer's "Skip
memory recall"); a voice session carries it as ``brain`` (default on). This
module only REPORTS the link — the switches it reads are the text chat's.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def _setting(key: str, default: Any) -> Any:
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:  # noqa: BLE001 - settings unavailable: report defaults
        return default


def _prefs(owner: Optional[str]) -> Dict[str, Any]:
    try:
        from routes.prefs_routes import _load_for_user
        return dict(_load_for_user(owner) or {})
    except Exception:  # noqa: BLE001
        return {}


def _brain_counts(owner: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {"notes": 0, "entities": 0, "relations": 0, "pending_extraction": 0}
    try:
        from src.brain import entities, extract, notes
        out["notes"] = len(notes.tree(owner).get("notes") or [])
        stats = entities.stats(owner)
        out["entities"] = int(stats.get("entities") or 0)
        out["relations"] = int(stats.get("relations_total") or 0)
        out["pending_extraction"] = int(extract.count_pending(owner) or 0)
    except Exception as exc:  # noqa: BLE001 - the brain is optional
        logger.debug("voice_brain: brain counts unavailable: %s", exc)
    return out


def _chat_memories(owner: str, session_id: str) -> int:
    """Memory items this chat produced (the brain's raw material), if the
    store records where an item came from."""
    if not session_id:
        return 0
    try:
        from src import memory_engine
        items = memory_engine.list_items(owner=owner, status="active", limit=2000)
    except Exception:  # noqa: BLE001
        return 0
    keys = ("session_id", "source_session", "source_session_id")
    return sum(1 for item in items
               if any(str(item.get(k) or "") == session_id for k in keys)
               or session_id in str(item.get("source") or ""))


def link_status(owner: Optional[str], session_id: str = "", *, voice_brain: bool = True,
                voice_sessions: int = 0) -> Dict[str, Any]:
    """What a voice turn on `session_id` does with the Second Brain right now.

    `voice_brain` is the voice session's own switch (False = the turn goes
    out with ``no_memory``). Every other value is the text chat's setting."""
    owner_key = str(owner or "")
    prefs = _prefs(owner)
    brain_enabled = bool(_setting("brain_enabled", True))
    memory_pref = bool(prefs.get("memory_enabled", True))
    auto_memory = bool(prefs.get("auto_memory", True))
    engine_on = bool(_setting("agent_context_engine", False))
    brain_source = bool(_setting("brain_context_source", True))
    recall = bool(voice_brain) and memory_pref
    consulted_brain = recall and brain_enabled and engine_on and brain_source
    fed = bool(voice_brain) and auto_memory
    return {
        "session_id": session_id,
        "path": "chat_stream",
        "same_as_text_chat": True,
        "voice_brain": bool(voice_brain),
        "voice_sessions": int(voice_sessions),
        "brain_enabled": brain_enabled,
        "consulted": {
            "memory_recall": recall,
            "brain_context_source": consulted_brain,
            "context_engine": engine_on,
        },
        "fed": {
            "memory_capture": fed,
            "brain_extraction": fed and brain_enabled,
            "llm_extraction": bool(_setting("brain_llm_extraction", True)),
        },
        "brain": _brain_counts(owner_key),
        "chat_memories": _chat_memories(owner_key, session_id),
        "switch": "no_memory",
    }
