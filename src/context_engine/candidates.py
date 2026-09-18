"""
context_engine/candidates.py — federating eleven stores without becoming one.

Faustus already knows things.  It knows them in ``memory_engine.db``, in
``<workspace>/.odysseus/*.md``, in ``objectives.jsonl``, in a Chroma
collection, in one ``index.json`` per expert, in a provenance graph rebuilt on
demand, and on disk in the files themselves.  The temptation, when a compiler
needs all of that at once, is to build a twelfth store that mirrors the eleven
— and then to spend the rest of the project explaining why the mirror is
stale.

So there is no twelfth store.  A :class:`ContextSource` is a thin adapter that
asks a store it does not own a question that store already knew how to answer,
and translates the reply into :class:`~.contracts.ContextCandidate` s.  Nothing
is copied and nothing is reindexed, so when a store is emptied the candidates
stop arriving on the next turn instead of after a rebuild nobody scheduled.

Four properties this module exists to guarantee.  Each is a failure someone
would otherwise ship:

**``gather()`` cannot fail.**  Nine sources on the turn path is nine chances to
raise, and a chat that dies because one expert's ``index.json`` was half
written is a far worse outcome than a chat with one section missing.  Every
source runs inside its own ``try`` and its own ``wait_for``; a source that
raises, hangs, or returns garbage becomes a :class:`SourceResult` that says so,
and the other eight arrive anyway.

**Blocking work stays off the event loop.**  Every store behind these adapters
is synchronous and at least one (``rag_vector.search``) can spend hundreds of
milliseconds inside Chroma.  :class:`ThreadedSource` puts that in
``asyncio.to_thread``, so nine slow sources cost the slowest one rather than
the sum.  The honest caveat, stated once so nobody has to rediscover it:
cancelling a ``to_thread`` task frees the *compiler*, not the *thread* — the
store keeps working until it returns on its own.  That is tolerable only
because every source here is a read; the day one of them writes, this stops
being true and the timeout stops being a safety net.

**Isolation is applied before the query, not to its results.**  ``owner``,
``project_id`` and ``workspace`` are read off ``request.execution`` and nowhere
else — never off the query text, which a model can write.  A source a policy
forbids is not called at all: ``memory_engine.search()`` bumps ``last_used`` on
the rows it returns and ``MemoryManager.increment_uses`` does the same, so
"search, then discard the results" leaves fingerprints on the store that "never
search" does not, and those fingerprints are exactly what incognito is supposed
to prevent.

**A candidate carries provenance or it does not exist.**  :func:`make_candidate`
is the only constructor used here, and it drops anything with no ``source_ref``
rather than letting ``contracts.py`` reject the whole packet three layers
later, where the error no longer names the source that produced it.  It also
clips every field to the contract's limits, because ``text()`` in
``src/contracts/base.py`` *raises* on an over-long string: a 600-character
title from a document chunk would otherwise turn a retrieval into a 500.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    runtime_checkable,
)

from .contracts import (
    AUTHORITY_ORDER,
    RETRIEVAL_LANES,
    SECTION_KINDS,
    SOURCE_TYPES,
    TRUST_CLASSES,
    ContextCandidate,
    ContextPolicy,
    ContextRequest,
    new_id,
)

logger = logging.getLogger(__name__)

#: Two seconds is the whole retrieval stage, not one source's share of it: the
#: sources run concurrently, so this is the wall clock a turn pays when the
#: slowest store is wedged.  Anything longer and a user watching a cursor blink
#: would rather have had the answer without that section.
DEFAULT_TIMEOUT_S = 2.0

# Contract limits, mirrored from `ContextCandidate.parse`.  They are repeated
# here (rather than imported, because `parse` states them inline) so that
# `make_candidate` can CLIP where `parse` would RAISE.  `tests/
# test_context_engine_sources.py` round-trips every produced candidate through
# `parse`, which is what keeps these numbers honest.
MAX_TITLE_CHARS = 512
MAX_REF_CHARS = 2048
MAX_BODY_CHARS = 1_000_000
MAX_REVISION_CHARS = 128
MAX_OWNER_CHARS = 256
MAX_PROJECT_CHARS = 128
MAX_LANES = 8


# ── the question one source is asked ───────────────────────────────────────

@dataclass(frozen=True)
class RetrievalRequest:
    """One retrieval round, derived from a :class:`ContextRequest`.

    The compiler may run several rounds against the same request — a cheap
    mandatory pass, then a wider one for whatever budget is left — so the
    round's own ``query``, ``sections``, ``limit`` and ``lanes`` live here and
    the immutable scope stays on ``request``.  Sources read the scope through
    the properties below and never reach for ``request.task.query``: a source
    that decided for itself what to search for would make the round's
    narrowing meaningless.
    """

    request: ContextRequest
    query: str = ""
    sections: Tuple[str, ...] = ()
    limit: int = 8
    lanes: Tuple[str, ...] = ()
    explicit_refs: Tuple[str, ...] = ()

    # ── scope: from the runtime, never from the text ───────────────────────

    @property
    def owner(self) -> str:
        return self.request.execution.owner

    @property
    def project_id(self) -> str:
        return self.request.execution.project_id

    @property
    def workspace(self) -> str:
        return self.request.execution.workspace

    @property
    def session_id(self) -> str:
        return self.request.execution.session_id

    @property
    def policy(self) -> ContextPolicy:
        return self.request.policy

    # ── round shape ────────────────────────────────────────────────────────

    def wants(self, section: str) -> bool:
        """Is this section in play this round?  An empty tuple means "all"."""
        return not self.sections or section in self.sections

    def allows(self, lane: str) -> bool:
        """Is this retrieval lane permitted?  An empty tuple means "all".

        ``semantic`` additionally answers to ``policy.allow_semantic_lane``, so
        a request that has switched embeddings off does not have to remember to
        list the lanes it still wants.
        """
        if lane == "semantic" and not self.policy.allow_semantic_lane:
            return False
        return not self.lanes or lane in self.lanes

    def top(self) -> int:
        """The per-source cap, clamped by ``policy.max_items_per_source``.

        A round may ask for fewer than the policy allows; it may never ask for
        more, because the policy is the actor's limit and the round is only a
        strategy.
        """
        try:
            wanted = int(self.limit or 0)
        except (TypeError, ValueError):
            wanted = 8
        ceiling = int(self.policy.max_items_per_source or 8)
        return max(1, min(wanted if wanted > 0 else 8, ceiling))

    def refs_for(self, prefix: str) -> Tuple[str, ...]:
        """Explicit references carrying ``prefix``, from the round and the
        request, in that order, deduplicated.

        This is the only channel by which a caller names a specific thing
        (``file:src/app.py``, ``expert:tolkien``, ``prov:memory:abc``).  It is
        deliberately not parsed out of the query: a reference the runtime did
        not put there is a reference a model chose.
        """
        seen: List[str] = []
        for ref in tuple(self.explicit_refs) + tuple(self.request.explicit_refs):
            value = str(ref or "").strip()
            if value.startswith(prefix) and value not in seen:
                seen.append(value)
        return tuple(seen)


@dataclass(frozen=True)
class SourceResult:
    """What one source did, whether or not it produced anything.

    ``error`` is filled for a failure *and* for a deliberate skip ("unavailable",
    "sections not requested"); ``degraded`` is what separates the two.  A source
    that was switched off did not degrade the packet, and marking it degraded
    would train everyone to ignore the flag.
    """

    source_id: str
    candidates: Tuple[ContextCandidate, ...] = ()
    degraded: bool = False
    error: str = ""
    elapsed_ms: int = 0

    def ok(self) -> bool:
        return not self.error and not self.degraded


@runtime_checkable
class ContextSource(Protocol):
    """What the compiler needs from a store, and nothing else.

    ``sections`` is a promise, not a filter: it tells :func:`gather` whether
    this source is worth waking for a round that only wants ``active_goal``.
    A source that produces two kinds of section lists both.
    """

    source_id: str
    sections: Tuple[str, ...]

    def available(self) -> bool: ...

    async def search(self, req: RetrievalRequest) -> Sequence[ContextCandidate]: ...

    async def fetch(self, source_ref: str,
                    req: RetrievalRequest) -> Optional[ContextCandidate]: ...


class ThreadedSource:
    """Base class for the synchronous stores, which today is all of them.

    Subclasses implement ``_search`` / ``_fetch`` as ordinary blocking code and
    get the ``asyncio.to_thread`` hop for free.  ``_gate`` runs *before* the
    hop, on the event loop, because a policy that forbids a source has to stop
    it from being consulted at all — see the module docstring on why "consulted
    and discarded" is not the same thing.
    """

    source_id: str = ""
    sections: Tuple[str, ...] = ()
    #: The ``source_ref`` prefixes this source can reopen, e.g. ``("mem:",)``.
    #: :func:`fetch_ref` routes on this; a source that lists none is
    #: search-only, which is the honest answer for a store with no read-by-id.
    handles: Tuple[str, ...] = ()

    def available(self) -> bool:
        """Is the underlying store present at all?  Cheap and side-effect free:
        :func:`gather` calls this on the event loop before dispatching."""
        return True

    def _gate(self, req: RetrievalRequest) -> str:
        """A non-empty reason means "do not consult this store for this
        request".  Evaluated on the loop, before any thread is spent."""
        return ""

    def _search(self, req: RetrievalRequest) -> Sequence[ContextCandidate]:
        return ()

    def _fetch(self, source_ref: str,
               req: RetrievalRequest) -> Optional[ContextCandidate]:
        return None

    async def search(self, req: RetrievalRequest) -> Sequence[ContextCandidate]:
        reason = self._gate(req)
        if reason:
            logger.debug("context source %s skipped: %s", self.source_id, reason)
            return ()
        return await asyncio.to_thread(self._search, req)

    async def fetch(self, source_ref: str,
                    req: RetrievalRequest) -> Optional[ContextCandidate]:
        reason = self._gate(req)
        if reason:
            logger.debug("context source %s skipped: %s", self.source_id, reason)
            return None
        return await asyncio.to_thread(self._fetch, source_ref, req)


# ── building a candidate that the contract will accept ─────────────────────

def iso_or_blank(value: Any) -> str:
    """An ISO-8601 UTC string, or ``""`` when the value cannot be read as a
    time.

    Every store dates its rows differently: ``memory_engine`` writes ISO,
    ``memory.json`` writes an epoch int, a file has an ``st_mtime`` float, and
    a half-migrated row has whatever was there before.  ``ContextCandidate``
    validates ``observed_at`` through ``contracts.base.timestamp``, which
    *raises* on anything it cannot parse — so a stray ``1699999999`` in one
    memory row would take down the whole retrieval.  Unreadable becomes
    "not recorded", which is what the contract says ``""`` means.
    """
    if value is None or value == "":
        return ""
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).replace(microsecond=0)\
            .isoformat().replace("+00:00", "Z")
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        try:
            return iso_or_blank(datetime.fromtimestamp(float(value), timezone.utc))
        except (OverflowError, OSError, ValueError):
            return ""
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ""
        try:
            return iso_or_blank(datetime.fromisoformat(text.replace("Z", "+00:00")))
        except ValueError:
            return ""
    return ""


def _clip(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    return text[:limit] if len(text) > limit else text


def _lanes(raw: Any) -> Tuple[str, ...]:
    out: List[str] = []
    for lane in tuple(raw or ()):
        name = str(lane or "").strip()
        if name in RETRIEVAL_LANES and name not in out:
            out.append(name)
    return tuple(out[:MAX_LANES])


def _scores(raw: Any) -> Dict[str, float]:
    out: Dict[str, float] = {}
    if not isinstance(raw, Mapping):
        return out
    for name, value in raw.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number != number:            # NaN never survives a JSON round-trip
            continue
        out[str(name)] = round(number, 6)
    return out


def _vocab(value: Any, choices: Sequence[str], fallback: str, field_name: str) -> str:
    name = str(value or "").strip()
    if name in choices:
        return name
    if name:
        logger.debug("candidate %s %r is not in the closed vocabulary; using %r",
                     field_name, name, fallback)
    return fallback


def make_candidate(*, source_type: str, source_ref: str, section: str,
                   title: str = "", body: str = "",
                   lanes: Sequence[str] = (),
                   scores: Optional[Mapping[str, float]] = None,
                   trust_class: str = "agent_assertion",
                   authority: str = "agent_claim",
                   source_revision: Any = "", observed_at: Any = "",
                   owner: str = "", project_id: str = "",
                   degraded: bool = False,
                   meta: Optional[Mapping[str, Any]] = None
                   ) -> Optional[ContextCandidate]:
    """Build a candidate that ``ContextCandidate.parse`` will accept, or None.

    None means "there was nothing here worth a receipt": a row with no
    ``source_ref`` cannot be reopened, cannot be invalidated later, and is
    therefore exactly the sentence rule 4 of ``contracts.py`` exists to keep
    out of a packet.  Dropping it here — where the adapter that produced it is
    still on the stack — is how the log names the culprit.

    Everything else is clipped or defaulted rather than rejected, because a
    document chunk with a 900-character heading is a bad title, not a bad turn.
    """
    ref = _clip(source_ref, MAX_REF_CHARS)
    if not ref:
        logger.debug("dropping a %s candidate with no source_ref", source_type)
        return None
    return ContextCandidate(
        candidate_id=new_id("ctxcand"),
        source_type=_vocab(source_type, SOURCE_TYPES, "memory", "source_type"),
        source_ref=ref,
        title=_clip(title, MAX_TITLE_CHARS),
        body=_clip(body, MAX_BODY_CHARS),
        section=_vocab(section, SECTION_KINDS, "retrieved_memory", "section"),
        lanes=_lanes(lanes),
        scores=_scores(scores),
        trust_class=_vocab(trust_class, tuple(TRUST_CLASSES),
                           "agent_assertion", "trust_class"),
        authority=_vocab(authority, tuple(AUTHORITY_ORDER), "agent_claim", "authority"),
        source_revision=_clip(source_revision, MAX_REVISION_CHARS),
        observed_at=iso_or_blank(observed_at),
        owner=_clip(owner, MAX_OWNER_CHARS),
        project_id=_clip(project_id, MAX_PROJECT_CHARS),
        degraded=bool(degraded),
        meta=dict(meta or {}),
    )


# ── running many sources at once, safely ───────────────────────────────────

def _source_id(source: Any) -> str:
    return str(getattr(source, "source_id", "") or "").strip() or "unnamed_source"


def _ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


def _normalise(raw: Any, req: RetrievalRequest) -> Tuple[List[ContextCandidate], int]:
    """Keep the real candidates, count the rest.

    A source is allowed to be wrong about its own output; it is not allowed to
    put a dict, a None or a 600-item list into a packet.  The cap is applied
    here as well as in the adapters so that ``limit`` is a property of the
    round rather than a convention nine modules have to remember.
    """
    if raw is None:
        return [], 0
    if isinstance(raw, ContextCandidate):
        raw = (raw,)
    try:
        items = list(raw)
    except TypeError:
        return [], 1
    good: List[ContextCandidate] = []
    dropped = 0
    for item in items:
        if isinstance(item, ContextCandidate) and item.source_ref:
            good.append(item)
        else:
            dropped += 1
    return good[:req.top()], dropped


async def _run_one(source: ContextSource, req: RetrievalRequest,
                   timeout_s: float) -> SourceResult:
    source_id = _source_id(source)
    started = time.monotonic()

    try:
        usable = bool(source.available())
    except Exception as exc:                                   # noqa: BLE001
        logger.warning("context source %s: available() raised: %s", source_id, exc)
        return SourceResult(source_id=source_id, degraded=True,
                            error=_clip(f"available() raised {type(exc).__name__}: {exc}", 500),
                            elapsed_ms=_ms(started))
    if not usable:
        return SourceResult(source_id=source_id, error="unavailable",
                            elapsed_ms=_ms(started))

    produces = tuple(getattr(source, "sections", ()) or ())
    if req.sections and produces and not (set(produces) & set(req.sections)):
        return SourceResult(source_id=source_id, error="sections not requested",
                            elapsed_ms=_ms(started))

    try:
        raw = await asyncio.wait_for(source.search(req), timeout=timeout_s)
    except asyncio.TimeoutError:
        # The task is cancelled, but a `to_thread` worker already in Chroma
        # will finish on its own time; see the module docstring.
        logger.warning("context source %s timed out after %.2fs", source_id, timeout_s)
        return SourceResult(source_id=source_id, degraded=True,
                            error=f"timeout after {timeout_s:.2f}s",
                            elapsed_ms=_ms(started))
    except asyncio.CancelledError:
        # Explicit, and not redundant with the clause below: someone will
        # eventually widen that to BaseException, and swallowing a cancellation
        # is how a shutdown turns into a hang.
        raise
    except Exception as exc:                                   # noqa: BLE001
        logger.warning("context source %s failed: %s", source_id, exc, exc_info=True)
        return SourceResult(source_id=source_id, degraded=True,
                            error=_clip(f"{type(exc).__name__}: {exc}", 500),
                            elapsed_ms=_ms(started))

    candidates, dropped = _normalise(raw, req)
    degraded = bool(dropped) or any(c.degraded for c in candidates)
    error = f"dropped {dropped} malformed candidate(s)" if dropped else ""
    return SourceResult(source_id=source_id, candidates=tuple(candidates),
                        degraded=degraded, error=error, elapsed_ms=_ms(started))


async def gather(sources: Sequence[ContextSource], req: RetrievalRequest, *,
                 timeout_s: float = DEFAULT_TIMEOUT_S) -> List[SourceResult]:
    """Ask every source at once and return one row per source, in input order.

    Never raises, never returns short: a caller can index the result against
    the sources it passed, and every row says what happened.  A source that
    fails or times out is marked ``degraded`` and the rest of the retrieval
    proceeds — the compiler's job is to build the best packet it can from
    whatever arrived, and to say in the packet what did not.
    """
    ordered = [s for s in (sources or []) if s is not None]
    if not ordered:
        return []
    try:
        limit_s = float(timeout_s)
    except (TypeError, ValueError):
        limit_s = DEFAULT_TIMEOUT_S
    if limit_s <= 0:
        limit_s = DEFAULT_TIMEOUT_S
    return list(await asyncio.gather(*(_run_one(s, req, limit_s) for s in ordered)))


async def fetch_ref(source_ref: str, req: RetrievalRequest, *,
                    sources: Optional[Sequence[ContextSource]] = None,
                    timeout_s: float = DEFAULT_TIMEOUT_S) -> Optional[ContextCandidate]:
    """Reopen one ``source_ref`` through whichever source owns its prefix.

    This is the other half of rule 4: a ``source_ref`` is only provenance if
    something can still resolve it.  Returns None rather than raising when no
    source claims the prefix, when the referenced thing is gone, or when the
    owning source is unavailable.
    """
    ref = str(source_ref or "").strip()
    if not ref:
        return None
    pool = list(sources) if sources is not None else default_sources()
    prefix = ref.split(":", 1)[0] + ":"
    for source in pool:
        if prefix not in tuple(getattr(source, "handles", ()) or ()):
            continue
        try:
            if not bool(source.available()):
                continue
            return await asyncio.wait_for(source.fetch(ref, req), timeout=timeout_s)
        except asyncio.CancelledError:
            raise
        except Exception as exc:                               # noqa: BLE001
            logger.warning("context source %s could not fetch %s: %s",
                           _source_id(source), ref, exc)
            return None
    return None


# ── the registry ───────────────────────────────────────────────────────────
#
# A process-wide registry, and not a parameter threaded through the compiler,
# for one reason: the adapters are stateful in the boring way (a memoised
# `RAGManager`, a provenance graph cached for a few seconds, a history
# provider installed by the chat route at startup), and building a fresh set
# per turn would throw all of that away every time.  `reset_sources()` exists
# so a test never inherits another test's doubles.

_LOCK = threading.RLock()
_REGISTRY: "Dict[str, ContextSource]" = {}
_DEFAULTS_BUILT = False


def register_source(source: ContextSource) -> None:
    """Add or replace a source by its ``source_id``.

    Registering before the first :func:`default_sources` call is the supported
    way to substitute a double or a differently configured adapter: the
    defaults are only filled in for ids nobody has claimed.
    """
    source_id = _source_id(source)
    if source_id == "unnamed_source":
        raise ValueError("a context source needs a non-empty source_id")
    with _LOCK:
        _REGISTRY[source_id] = source


def get_source(source_id: str) -> Optional[ContextSource]:
    with _LOCK:
        return _REGISTRY.get(str(source_id or "").strip())


def registered_sources() -> Tuple[str, ...]:
    with _LOCK:
        return tuple(sorted(_REGISTRY))


def default_sources() -> List[ContextSource]:
    """Every adapter, instantiated once and memoised in the registry.

    The import of ``.adapters`` is deliberately deferred to the first call:
    ``import src.context_engine`` must stay cheap for the processes that only
    wanted a dataclass, and an adapter whose module fails to import must cost
    that adapter, not the retrieval.
    """
    global _DEFAULTS_BUILT
    with _LOCK:
        if not _DEFAULTS_BUILT:
            for factory in _adapter_factories():
                try:
                    source = factory()
                except Exception as exc:                       # noqa: BLE001
                    logger.warning("context source factory %r failed: %s", factory, exc)
                    continue
                source_id = _source_id(source)
                if source_id != "unnamed_source":
                    _REGISTRY.setdefault(source_id, source)
            _DEFAULTS_BUILT = True
        return list(_REGISTRY.values())


def reset_sources() -> None:
    """Forget every source, defaults included.  For tests, and for a process
    that has just been reconfigured (a new data dir, a new workspace)."""
    global _DEFAULTS_BUILT
    with _LOCK:
        _REGISTRY.clear()
        _DEFAULTS_BUILT = False


def _adapter_factories() -> Tuple[Callable[[], ContextSource], ...]:
    try:
        from .adapters import SOURCE_FACTORIES
    except Exception as exc:                                   # noqa: BLE001
        logger.warning("context engine adapters could not be imported: %s", exc)
        return ()
    return tuple(SOURCE_FACTORIES)


__all__ = [
    "DEFAULT_TIMEOUT_S",
    "MAX_TITLE_CHARS", "MAX_REF_CHARS", "MAX_BODY_CHARS", "MAX_REVISION_CHARS",
    "MAX_OWNER_CHARS", "MAX_PROJECT_CHARS", "MAX_LANES",
    "RetrievalRequest", "SourceResult", "ContextSource", "ThreadedSource",
    "make_candidate", "iso_or_blank",
    "gather", "fetch_ref",
    "register_source", "get_source", "registered_sources",
    "default_sources", "reset_sources",
]
