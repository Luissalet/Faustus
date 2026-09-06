"""
project_context/resolvers/base.py — the contract every source adapter signs.

A resolver is the only place in this subsystem that touches somebody's actual
bytes: a row in ``documents``, a file on the disk, an artifact in the store.
That makes it the only place a project-isolation bug can become a disclosure,
so the obligations below are stated once, here, and repeated in the docstring
of each resolver that implements them.

Obligations of every resolver (plan §8)
---------------------------------------
1. **Check the owner before touching the source.** Not after reading it and
   not while formatting the answer — a resolver that loads the row first has
   already put another user's content in a local variable that some later
   ``except`` clause can log.

2. **A refusal leaks nothing.** A source that does not exist is
   ``state="missing"``; one belonging to somebody else is ``state="forbidden"``;
   and *neither* carries the title, the path or the size. A ``forbidden`` that
   names the document has already given away the document. ``SourceMetadata``
   enforces this structurally — use ``SourceMetadata.denied()`` and the type
   cannot carry anything it should not.

3. **Compute a stable revision.** Same bytes, same revision string, on every
   machine and every process. It is what makes ``latest`` detectable as
   changed and ``pinned`` provably unchanged.

4. **Bound every read.** ``read()`` returns a window with ``truncated`` set
   honestly; nothing here loads a source whole because a caller asked nicely.

5. **Never trust a stored label as access control.** ``projects.json`` holds
   the owner an earlier version wrote there. The source is the authority, and
   it is re-asked on every call.

``revision()`` deliberately takes no owner — the protocol in the plan does not
give it one. That makes it an existence oracle if it is ever called on its own,
so the rule is: **the service validates before it revises**, and a resolver
returns ``""`` for anything it cannot compute rather than distinguishing "not
yours" from "not there".

The registry is a plain process-global dict behind an ``RLock``. Registration
happens in ``resolvers/__init__.py`` at import time; nothing here imports a
database module at module scope, so importing the package costs nothing.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Mapping, Optional, Protocol, runtime_checkable

from ..models import SourceContent, SourceMatch, SourceMetadata, SourceRef, ExtractedCorpus

logger = logging.getLogger(__name__)

__all__ = [
    "ContextSourceResolver", "MAX_READ_CHARS", "MAX_SEARCH_MATCHES",
    "MAX_SNIPPET_CHARS", "ResolverBase", "get_resolver", "register_resolver",
    "resolvers", "reset_resolvers",
]

#: The hard ceiling on a single ``read()``. A caller may ask for less; nobody
#: gets more in one call. Roughly 50k tokens of prose — already far past what
#: any turn should spend, and small enough that a runaway loop cannot page a
#: 4 GB file into memory one window at a time.
MAX_READ_CHARS = 200_000

MAX_SEARCH_MATCHES = 200
MAX_SNIPPET_CHARS = 400


@runtime_checkable
class ContextSourceResolver(Protocol):
    """One source kind, six questions. See the module docstring for the five
    obligations that come with implementing it."""

    kind: str

    def validate(self, ref: SourceRef, *, owner: str,
                 project: Mapping[str, Any]) -> SourceMetadata:
        """Owner + existence check performed *before* an attach writes anything."""
        ...

    def metadata(self, ref: SourceRef, *, owner: str,
                 project: Mapping[str, Any]) -> SourceMetadata:
        """The same check, re-run for an already-stored link."""
        ...

    def revision(self, ref: SourceRef, *, version_policy: str = "latest",
                 pinned_version: Optional[int] = None) -> str:
        """A stable string for the effective revision, or ``""``.

        Only meaningful after ``validate()``/``metadata()`` has authorised the
        ref — it has no owner argument and must not become an oracle.
        """
        ...

    def read(self, ref: SourceRef, *, start: int = 0, limit: int = 0,
             version_policy: str = "latest",
             pinned_version: Optional[int] = None) -> SourceContent:
        """A bounded window of the effective revision."""
        ...

    def search(self, ref: SourceRef, query: str, *, limit: int = 20) -> List[SourceMatch]:
        """Literal, case-insensitive matches with enough location to cite."""
        ...

    def extract(self, ref: SourceRef, *, version_policy: str = "latest",
                pinned_version: Optional[int] = None) -> ExtractedCorpus:
        """Everything the indexer can chunk, or a degraded corpus that says so."""
        ...


class ResolverBase:
    """Shared plumbing. Not a required base class — the protocol is structural —
    but it keeps the read clamp and the refusal helper written once."""

    kind: str = ""

    def policy_note(self, version_policy: str) -> str:
        """Why this resolver cannot honour a version policy, or ``""``.

        Not part of the six-question protocol — the plan's protocol gives
        ``validate()`` no policy argument — but the service needs somewhere to
        ask before it writes a link it can never resolve. A resolver that
        supports everything inherits the empty answer.
        """
        return ""

    def _denied(self, state: str, note: str = "") -> SourceMetadata:
        """The only way this codebase should build a refusal. Nothing about the
        source is passed in, so nothing about it can come out."""
        return SourceMetadata.denied(self.kind, state, note)

    @staticmethod
    def _window(body: str, start: int, limit: int) -> "tuple[str, int, int, int, bool]":
        """Clamp a request to a window of ``body``.

        Returns ``(text, start, end, total, truncated)``. A negative or absurd
        ``start`` becomes 0 rather than an exception: this runs on the retrieval
        path, and a bad offset should return the beginning of the file, not
        break a turn.
        """
        total = len(body)
        begin = max(0, min(int(start or 0), total))
        span = int(limit or 0)
        span = MAX_READ_CHARS if span <= 0 else min(span, MAX_READ_CHARS)
        end = min(total, begin + span)
        return body[begin:end], begin, end, total, end < total

    @staticmethod
    def _matches(body: str, query: str, limit: int, locate) -> List[SourceMatch]:
        """Literal case-insensitive line search shared by every text source.

        Literal on purpose: a user query is not a regular expression, and
        compiling one from it turns a search box into a way to hang the server
        on a pathological pattern.
        """
        needle = (query or "").strip().lower()
        if not needle:
            return []
        cap = MAX_SEARCH_MATCHES if limit <= 0 else min(int(limit), MAX_SEARCH_MATCHES)
        out: List[SourceMatch] = []
        for number, line in enumerate(body.splitlines(), start=1):
            if needle not in line.lower():
                continue
            snippet = line.strip()[:MAX_SNIPPET_CHARS]
            out.append(SourceMatch(location=locate(number), snippet=snippet,
                                   score=1.0, revision=""))
            if len(out) >= cap:
                break
        return out


# ── the registry ───────────────────────────────────────────────────────────

_LOCK = threading.RLock()
_REGISTRY: "Dict[str, ContextSourceResolver]" = {}


def register_resolver(resolver: "ContextSourceResolver") -> None:
    """Register (or replace) the resolver for its ``kind``.

    Replacing is what makes a module reload in a test suite harmless, and what
    lets a test swap in a fake without monkeypatching an import.
    """
    kind = str(getattr(resolver, "kind", "") or "").strip()
    if not kind:
        raise ValueError("a resolver must declare a non-empty kind")
    with _LOCK:
        _REGISTRY[kind] = resolver


def get_resolver(kind: str) -> "Optional[ContextSourceResolver]":
    """The resolver for a kind, or None. None is an answer, not a failure: an
    unknown kind is refused by the caller with a message naming it."""
    with _LOCK:
        return _REGISTRY.get(str(kind or "").strip())


def resolvers() -> "Dict[str, ContextSourceResolver]":
    """A copy of the registry, so a caller iterating cannot be surprised by a
    concurrent registration."""
    with _LOCK:
        return dict(_REGISTRY)


def reset_resolvers(replacement: "Optional[Mapping[str, ContextSourceResolver]]" = None) -> None:
    """Replace the whole registry. For tests; production registers at import."""
    with _LOCK:
        _REGISTRY.clear()
        for kind, resolver in dict(replacement or {}).items():
            _REGISTRY[str(kind)] = resolver
