"""
project_context/references.py — what "this document" means, decided rather than guessed.

The problem, precisely
----------------------
Today the only thing in Faustus that answers "this document" is
``src/agent_tools/document_tools.py::_active_document_id`` — a **module-level
global for the whole process**, set by ``set_active_document`` and cleared by
``clear_active_document`` — plus an ``active_document`` value passed along as a
turn parameter. A process-wide pointer is right often enough to be dangerous:
two chats in the same process share it, and a turn that creates two documents
leaves it holding whichever one happened to be written last.

So this module records, per ``(owner, session_id)``, the entities a turn
actually produced or touched, and resolves a reference against that record with
a stated priority (plan §10):

1. an id the **user** gave explicitly;
2. the session's **active entity**;
3. a result **created in the current turn**;
4. the last compatible result from the **previous turn**;
5. a **unique title match** inside the session;
6. otherwise: return the candidates and **ask**.

Rule 3 has a hard edge worth spelling out: **two documents created in the same
operation are two candidates, not a race won by the later one.** Choosing by
recency there is how "add this document to the project" attaches the wrong
half of a pair, and the user has no way to notice. ``resolve()`` returns
``(None, [a, b])`` and the tool layer asks which.

This registry is a convenience, never a authority. It is in-process, TTL'd and
capped; if it is empty the caller degrades to asking for the id. Nothing that
matters is stored only here, and nothing it returns skips validation — a
resolved reference is still checked for ownership against the source itself.

Isolation is by ``(owner, session_id)`` and an **empty owner is its own scope,
never a wildcard**. A blank owner matching every owner is precisely the bug
this subsystem exists to avoid.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_MAX_ENTRIES", "DEFAULT_MAX_SCOPES", "DEFAULT_TTL_SECONDS",
    "TurnReference", "TurnReferenceRegistry", "note_created", "registry",
]

#: An hour. A reference older than the conversation that produced it is a worse
#: guess than asking, and holding them forever turns a helper into a leak.
DEFAULT_TTL_SECONDS = 3600.0

#: Per ``(owner, session_id)``. A chat that produced 200 entities has long
#: stopped being resolvable by "this document" anyway.
DEFAULT_MAX_ENTRIES = 200

#: Total live scopes. Bounds the registry in a long-running process.
DEFAULT_MAX_SCOPES = 500

#: Relations that count as "produced by this turn" for priority 3.
_PRODUCED = ("created", "generated")


@dataclass(frozen=True)
class TurnReference:
    """One entity a turn produced or touched, with enough provenance to pick
    between two of them."""

    kind: str
    ref_id: str
    label: str = ""
    source_tool: str = ""
    turn_id: str = ""
    run_id: str = ""
    session_id: str = ""
    owner: str = ""
    relation: str = "created"
    created_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind, "ref_id": self.ref_id, "label": self.label,
            "source_tool": self.source_tool, "turn_id": self.turn_id,
            "run_id": self.run_id, "session_id": self.session_id,
            "owner": self.owner, "relation": self.relation,
            "created_at": self.created_at,
        }


def _default_active(session_id: str, owner: str) -> "Tuple[str, str]":
    """The process-global active document, read late and defensively.

    Deliberately the *only* coupling to ``document_tools``: importing it at
    module scope would drag the tool runtime into anything that imports this,
    and the pointer is a fallback, not the record.
    """
    try:
        from src.agent_tools.document_tools import get_active_document
        return (str(get_active_document() or "").strip(), "document")
    except Exception as exc:  # noqa: BLE001 - hot path, never raises
        logger.debug("active-document lookup failed: %s", exc)
        return "", ""


class TurnReferenceRegistry:
    """In-process, per-``(owner, session_id)``, TTL'd and capped.

    ``active_provider`` is injectable so a test can state what "active" means
    without touching a global, and so a future session-scoped pointer can be
    dropped in without changing a caller.
    """

    def __init__(self, *, ttl_seconds: float = DEFAULT_TTL_SECONDS,
                 max_entries: int = DEFAULT_MAX_ENTRIES,
                 max_scopes: int = DEFAULT_MAX_SCOPES,
                 clock=None, active_provider=None) -> None:
        self._ttl = float(ttl_seconds)
        self._max_entries = int(max_entries)
        self._max_scopes = int(max_scopes)
        self._clock = clock or time.time
        self._active_provider = active_provider or _default_active
        self._lock = threading.RLock()
        self._scopes: "Dict[Tuple[str, str], List[TurnReference]]" = {}
        self._touched: "Dict[Tuple[str, str], float]" = {}

    # ── recording ──────────────────────────────────────────────────────────

    @staticmethod
    def _scope(owner: str, session_id: str) -> "Tuple[str, str]":
        return (str(owner or ""), str(session_id or ""))

    def note(self, ref: TurnReference) -> None:
        """Record one reference. Never raises: a lost note costs a clarifying
        question, and a raise on the turn path costs the turn."""
        try:
            if not isinstance(ref, TurnReference) or not str(ref.ref_id or "").strip():
                logger.debug("project_context: ignoring an unusable turn reference %r", ref)
                return
            now = float(self._clock())
            if not ref.created_at:
                ref = TurnReference(**{**ref.to_dict(), "created_at": now})
            key = self._scope(ref.owner, ref.session_id)
            with self._lock:
                entries = self._scopes.setdefault(key, [])
                entries.append(ref)
                if len(entries) > self._max_entries:
                    del entries[: len(entries) - self._max_entries]
                self._touched[key] = now
                self._evict_locked(now)
        except Exception as exc:  # noqa: BLE001
            logger.debug("project_context: note() failed: %s", exc)

    def _evict_locked(self, now: float) -> None:
        """Drop expired entries, then the least-recently-touched scopes."""
        cutoff = now - self._ttl
        for key in list(self._scopes):
            kept = [e for e in self._scopes[key] if e.created_at >= cutoff]
            if kept:
                self._scopes[key] = kept
            else:
                self._scopes.pop(key, None)
                self._touched.pop(key, None)
        if len(self._scopes) <= self._max_scopes:
            return
        stale = sorted(self._scopes, key=lambda k: self._touched.get(k, 0.0))
        for key in stale[: len(self._scopes) - self._max_scopes]:
            self._scopes.pop(key, None)
            self._touched.pop(key, None)

    # ── reading ────────────────────────────────────────────────────────────

    def for_turn(self, session_id: str, *, turn_id: str = "",
                 owner: str = "") -> List[TurnReference]:
        """References for one ``(owner, session_id)``, oldest first.

        ``owner=""`` is the blank-owner scope, not every owner.
        """
        key = self._scope(owner, session_id)
        with self._lock:
            self._evict_locked(float(self._clock()))
            entries = list(self._scopes.get(key, ()))
        if turn_id:
            entries = [e for e in entries if e.turn_id == turn_id]
        return sorted(entries, key=lambda e: e.created_at)

    def clear_turn(self, session_id: str, turn_id: str) -> int:
        """Forget one turn's references across every owner scope for that
        session. Returns how many went."""
        removed = 0
        with self._lock:
            for key in list(self._scopes):
                if key[1] != str(session_id or ""):
                    continue
                before = len(self._scopes[key])
                kept = [e for e in self._scopes[key] if e.turn_id != str(turn_id or "")]
                removed += before - len(kept)
                if kept:
                    self._scopes[key] = kept
                else:
                    self._scopes.pop(key, None)
                    self._touched.pop(key, None)
        return removed

    def clear_session(self, session_id: str) -> int:
        removed = 0
        with self._lock:
            for key in list(self._scopes):
                if key[1] != str(session_id or ""):
                    continue
                removed += len(self._scopes.pop(key, ()))
                self._touched.pop(key, None)
        return removed

    # ── resolution ─────────────────────────────────────────────────────────

    def _synthetic(self, ref_id: str, kind: str, *, session_id: str, owner: str,
                   source_tool: str, relation: str) -> TurnReference:
        """A reference for an entity the registry never saw.

        An id the user typed outranks anything recorded here, and an active
        pointer set before this registry existed is still an answer. Both are
        revalidated against the source by the service, so synthesising one
        grants nothing.
        """
        return TurnReference(kind=kind, ref_id=ref_id, source_tool=source_tool,
                             session_id=session_id, owner=owner, relation=relation,
                             created_at=float(self._clock()))

    def _active(self, hint: Mapping[str, Any], session_id: str,
                owner: str) -> "Tuple[str, str]":
        """The session's active entity. An explicit ``active_id`` in the hint —
        even an empty one — wins, so a caller can say "there is no active
        entity" instead of falling back to a process global."""
        if "active_id" in hint:
            return (str(hint.get("active_id") or "").strip(),
                    str(hint.get("active_kind") or "").strip())
        try:
            return self._active_provider(session_id, owner)
        except Exception as exc:  # noqa: BLE001
            logger.debug("active provider failed: %s", exc)
            return "", ""

    def resolve(self, hint: Mapping[str, Any], *, session_id: str,
                owner: str) -> "Tuple[Optional[TurnReference], List[TurnReference]]":
        """``(chosen, candidates)`` under the priority in the module docstring.

        ``chosen`` is None whenever the answer is genuinely ambiguous, and then
        ``candidates`` is what to ask about. Unknown keys in ``hint`` are
        ignored rather than rejected — this runs on the turn path, where a
        rejection costs the user their message and a stray key costs nothing.
        """
        try:
            return self._resolve(dict(hint or {}), session_id=session_id, owner=owner)
        except Exception as exc:  # noqa: BLE001 - never raises on the turn path
            logger.debug("project_context: resolve() failed: %s", exc)
            return None, []

    def _resolve(self, hint: Dict[str, Any], *, session_id: str,
                 owner: str) -> "Tuple[Optional[TurnReference], List[TurnReference]]":
        kind = str(hint.get("kind") or "").strip()
        explicit = str(hint.get("id") or hint.get("ref_id") or "").strip()
        label = str(hint.get("label") or hint.get("title") or "").strip()
        turn_id = str(hint.get("turn_id") or "").strip()

        entries = self.for_turn(session_id, owner=owner)
        compatible = [e for e in entries if not kind or e.kind == kind]

        # 1. An id the user gave. Authoritative even when unrecorded.
        if explicit:
            for entry in compatible:
                if entry.ref_id == explicit:
                    return entry, []
            return self._synthetic(explicit, kind, session_id=session_id, owner=owner,
                                   source_tool="explicit", relation="mentioned"), []

        # 2. The session's active entity.
        active_id, active_kind = self._active(hint, session_id, owner)
        if active_id and (not kind or not active_kind or active_kind == kind):
            for entry in compatible:
                if entry.ref_id == active_id:
                    return entry, []
            return self._synthetic(active_id, kind or active_kind, session_id=session_id,
                                   owner=owner, source_tool="active_pointer",
                                   relation="opened"), []

        # An ambiguous step does not end the search: it records what it could
        # not choose between and lets a *more discriminating* signal try. A
        # title the user typed is stronger evidence than "the previous turn",
        # so stopping at the first tie would refuse a question it can answer.
        # The first tie recorded is the narrowest scope, so it is the set worth
        # asking about if nothing later resolves.
        pending: List[TurnReference] = []

        def tie(group: List[TurnReference]) -> None:
            if not pending:
                pending.extend(group)

        # 3. Produced by the current turn. Two of them is a question, not a race.
        if turn_id:
            made = [e for e in compatible
                    if e.turn_id == turn_id and e.relation in _PRODUCED]
            if len(made) == 1:
                return made[0], []
            if len(made) > 1:
                tie(made)

        # 4. The most recent earlier turn that produced something compatible.
        prior = [e for e in compatible if e.turn_id and e.turn_id != turn_id]
        if prior:
            newest = max(prior, key=lambda e: e.created_at).turn_id
            group = [e for e in prior if e.turn_id == newest]
            if len(group) == 1:
                return group[0], []
            if len(group) > 1:
                tie(group)

        # 5. A unique title match inside the session. Exact before substring:
        # "Voice" must not be ambiguous just because "Voice architecture" also
        # contains it.
        if label:
            needle = label.lower()
            exact = [e for e in compatible if (e.label or "").lower() == needle]
            named = exact or [e for e in compatible if needle in (e.label or "").lower()]
            if len(named) == 1:
                return named[0], []
            if len(named) > 1:
                tie(named)

        # 6. Ask, about the narrowest set that was actually ambiguous.
        return None, (pending or compatible)


# ── the process singleton ──────────────────────────────────────────────────

_REGISTRY: Optional[TurnReferenceRegistry] = None
_REGISTRY_LOCK = threading.RLock()


def registry() -> TurnReferenceRegistry:
    """The process-wide registry. One per process, like the pointer it replaces
    — but scoped by ``(owner, session_id)`` inside, which is the whole point."""
    global _REGISTRY
    with _REGISTRY_LOCK:
        if _REGISTRY is None:
            _REGISTRY = TurnReferenceRegistry()
        return _REGISTRY


def note_created(kind: str, ref_id: str, *, label: str = "", tool: str = "",
                 session_id: str = "", turn_id: str = "", run_id: str = "",
                 owner: str = "") -> None:
    """Record something a tool just made. The one-liner the harness calls."""
    registry().note(TurnReference(
        kind=str(kind or ""), ref_id=str(ref_id or ""), label=str(label or ""),
        source_tool=str(tool or ""), turn_id=str(turn_id or ""), run_id=str(run_id or ""),
        session_id=str(session_id or ""), owner=str(owner or ""), relation="created",
    ))
