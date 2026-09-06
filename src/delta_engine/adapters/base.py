"""What every domain adapter has to look like, and the shapes they share.

Section 3.1: universal in the contract, specific in the extraction. This module
is the universal half. An adapter's whole job is to turn a `RevisionRef` into a
`Snapshot` of addressable `Element`s and to say what differs between two of
them; it does NOT classify, because classification needs the frozen intent and
an adapter that could see the intent could be tempted to look for what the
intent hoped for. That separation is §3.3 written as a module boundary:

    adapter  -> observed    (`Finding`, with method and tier)
    classifier -> classified (`DeltaAssertion`, with a classification)
    verdict  -> assessed     (`UniversalDelta.assessment`)

An adapter reports what it could not do as loudly as what it did. `Snapshot`
carries `readable`, `excluded` and `notes`, and an adapter that hit a budget
says so there rather than returning a shorter list of elements that looks like
a complete one -- which is the whole failure mode of §31's "no analizar cada
frame si basta localizar segmentos" read the wrong way round.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Protocol, Sequence, Tuple, runtime_checkable

from src.delta_engine.contracts import (
    Budget,
    Coverage,
    DeltaError,
    EvidenceRef,
    IntentContract,
    InvariantResult,
    Privacy,
    RevisionRef,
    cap_confidence,
)

logger = logging.getLogger(__name__)

__all__ = [
    "AdapterError",
    "Scope",
    "Element",
    "Snapshot",
    "Finding",
    "Extraction",
    "DeltaAdapter",
    "unreadable",
]


class AdapterError(DeltaError):
    """An adapter could not do its job. Names the field and the value."""


@dataclass(frozen=True)
class Scope:
    """Who is asking, about what, and what they may spend.

    Handed to every adapter call. `workspace` is the absolute path when the
    domain has one -- and it is the DISPLAY name that is not wanted here, a
    distinction that cost the State Mirror a whole afternoon and a clean sweep
    that observed nothing.

    `privacy` and `budget` are not advisory. An adapter that would need a tier
    the budget excludes reports the gap in its coverage and returns; an adapter
    that would need to send bytes somewhere checks `privacy.allow_external`
    first and refuses rather than asking forgiveness.
    """

    owner: str
    project_id: str = ""
    workspace: str = ""
    budget: Budget = field(default_factory=Budget)
    privacy: Privacy = field(default_factory=Privacy)
    correlation_id: str = ""
    run_id: str = ""

    def allows(self, tier: str) -> bool:
        return self.budget.allows(tier)


@dataclass(frozen=True)
class Element:
    """One addressable thing inside a revision.

    `key` is the domain's own address and is what alignment matches on: a path
    for a file, `path#symbol` for a symbol, a JSON pointer for a node, an
    `entity#field` for state. `hash` is the content digest when the adapter can
    get one cheaply, and it is what turns a rename into a `moved` rather than a
    delete plus an add.

    `value` is a SHORT rendering -- a colour, a signature, a boolean -- never a
    blob. An element holding a file's contents would make a snapshot as
    expensive as the revision it describes, and the assertion that quotes it
    would be unreadable.
    """

    key: str
    kind: str = ""
    hash: str = ""
    value: str = ""
    detail: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"key": self.key}
        for name in ("kind", "hash", "value"):
            value = getattr(self, name)
            if value:
                out[name] = value
        if self.detail:
            out["detail"] = dict(self.detail)
        return out


@dataclass(frozen=True)
class Snapshot:
    """One side of a comparison, as this adapter could see it.

    `readable=False` is a first-class result and not an exception: "the source
    checkpoint is gone" is an ANSWER, it makes the delta `inconclusive`, and
    raising instead would lose the target-side work already done and tell the
    caller nothing about which end failed.
    """

    revision: RevisionRef
    elements: Tuple[Element, ...] = ()
    readable: bool = True
    tier: str = "parser"
    excluded: Tuple[str, ...] = ()
    notes: Tuple[str, ...] = ()
    truncated: bool = False
    detail: Mapping[str, Any] = field(default_factory=dict)

    def index(self) -> Dict[str, Element]:
        """`key -> element`. Last one wins, and a repeat is logged.

        A duplicate key means the adapter's addressing is not unique, which
        makes alignment silently arbitrary. Logging rather than raising because
        one ambiguous symbol should not lose an entire comparison.
        """
        out: Dict[str, Element] = {}
        for element in self.elements:
            if element.key in out:
                logger.warning("delta adapter: duplicate element key %r in %s",
                               element.key, self.revision.identity())
            out[element.key] = element
        return out

    def by_hash(self) -> Dict[str, Tuple[Element, ...]]:
        """`hash -> elements`. Empty hashes are skipped, never grouped.

        Grouping every hashless element under `""` would make them all look
        like copies of each other, and a rename detector built on that would
        pair unrelated files with confidence.
        """
        out: Dict[str, list] = {}
        for element in self.elements:
            if not element.hash:
                continue
            out.setdefault(element.hash, []).append(element)
        return {key: tuple(value) for key, value in out.items()}


def unreadable(revision: RevisionRef, reason: str, *, tier: str = "hash") -> Snapshot:
    """The snapshot for an end that could not be read. Reason always named."""
    return Snapshot(revision=revision, elements=(), readable=False, tier=tier,
                    notes=(str(reason or "unreadable"),))


@dataclass(frozen=True)
class Finding:
    """What an adapter observed about one element, before anyone interpreted it.

    Deliberately not a `DeltaAssertion`: there is no `classification` here and
    no `severity`, because both need the frozen intent and the adapter does not
    get to see it. `classification.classify_all()` turns a tuple of these into
    assertions, and that is the only place the two axes meet.

    `confidence` is what the METHOD justifies. It is capped by `tier` on
    construction, so an adapter cannot promote a guess by writing `exact`.
    """

    path: str
    operation: str
    before: str = ""
    after: str = ""
    method: str = ""
    tier: str = "parser"
    confidence: str = "high"
    evidence_refs: Tuple[EvidenceRef, ...] = ()
    invariant_refs: Tuple[str, ...] = ()
    limitations: Tuple[str, ...] = ()
    detail: str = ""
    alignment: Optional[Any] = None
    element_kind: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "confidence", cap_confidence(self.confidence, self.tier))

    def to_dict(self) -> Dict[str, Any]:
        """The mapping `DeltaAssertion.parse` accepts, minus what it decides.

        `classification` and `severity` are absent on purpose: they are the
        classifier's to fill, and a dict from here that carried them would let
        an adapter pre-empt the one step it is not allowed to take.
        """
        out: Dict[str, Any] = {
            "path": self.path, "operation": self.operation,
            "confidence": self.confidence, "tier": self.tier,
        }
        for name in ("before", "after", "method", "detail"):
            value = getattr(self, name)
            if value:
                out[name] = value
        if self.evidence_refs:
            out["evidence_refs"] = [e.to_dict() for e in self.evidence_refs]
        for name in ("invariant_refs", "limitations"):
            value = getattr(self, name)
            if value:
                out[name] = list(value)
        if self.alignment is not None:
            out["alignment"] = (self.alignment.to_dict()
                                if hasattr(self.alignment, "to_dict") else self.alignment)
        return out


@dataclass(frozen=True)
class Extraction:
    """Everything one adapter produced for one comparison.

    `extractor_versions` is part of the §22 cache key and is therefore not
    decoration: when a parser improves, every delta computed with the old one
    has a different fingerprint and is recomputed instead of being trusted.
    An adapter that forgets to bump it makes stale conclusions immortal.
    """

    findings: Tuple[Finding, ...] = ()
    coverage: Coverage = field(default_factory=Coverage)
    invariants: Tuple[InvariantResult, ...] = ()
    extractor_versions: Mapping[str, str] = field(default_factory=dict)
    evidence_refs: Tuple[EvidenceRef, ...] = ()
    limitations: Tuple[str, ...] = ()


@runtime_checkable
class DeltaAdapter(Protocol):
    """The five things a domain has to be able to do.

    `available()` is separate from `compare()` because "this machine has no
    Python parser" and "these two revisions are identical" are different
    answers and only the second is a delta. The registry asks `available()`
    before routing, and a domain whose adapter says no is reported as
    unavailable rather than silently handled by the binary fallback -- the
    fallback would answer `reencoded` about a Python file and be believed.
    """

    domain: str
    version: str

    def available(self) -> bool: ...

    def snapshot(self, revision: RevisionRef, *, scope: Scope) -> Snapshot: ...

    def compare(self, source: Snapshot, target: Snapshot, *, scope: Scope) -> Extraction: ...

    def check_invariants(
        self,
        intent: IntentContract,
        source: Snapshot,
        target: Snapshot,
        *,
        scope: Scope,
    ) -> Tuple[InvariantResult, ...]: ...
