"""Universal Delta Engine: what changed, what was asked for, and how we know.

This module holds the shapes and the closed vocabularies of the comparison
described in `inspiration/PLAN_UNIVERSAL_DELTA_ENGINE_FAUSTUS.md`. Nothing here
reads a file, opens a socket or calls a model. It is the contract every
adapter, extractor, classifier and route has to agree on, and it is
deliberately the only place where the words `requested`, `preserved` and
`inconclusive` are defined.

The six rules this file exists to enforce, each with the failure it prevents:

1.  **Not detected is not preserved.** `preserved` is a conclusion that costs
    an observation and a threshold, so `DeltaAssertion.parse` refuses
    `classification="preserved"` with `confidence="unknown"`, and
    `InvariantResult.parse` refuses `status="preserved"` with no observation
    behind it. A detector that found nothing has found nothing; saying it
    "preserved the background" is the single most expensive lie this subsystem
    could tell, because the whole product promise is "changed only what you
    asked".

2.  **Source and target are immutable or there is no comparison.** Every
    `RevisionRef` carries a sha256, required, never defaulted. Comparing
    against a moving `latest` produces a result that was true for nobody, and
    the caller cannot tell afterwards which bytes it saw.

3.  **Intent is frozen before the result is seen.** `IntentContract` requires
    `frozen_at`, and a revision is a NEW contract with `supersedes` pointing
    back. Editing the contract after seeing the target is how every evaluation
    scores 100%.

4.  **Observation and interpretation are separate fields.** `operation` is what
    the extractor saw (`added`, `modified`, `moved`...). `classification` is
    what it means against the frozen intent (`requested`, `incidental`,
    `regression`...). They are two axes, they disagree often, and collapsing
    them into one list -- which is how the plan's prose writes it -- is what
    makes "it changed" and "it should not have changed" indistinguishable.

5.  **Coverage is not confidence.** `Coverage` says how much could be compared;
    `CONFIDENCE` says how much to believe one comparison. Full coverage with a
    weak extractor, and a certain answer about one small region, are different
    situations and a single percentage hides both.

6.  **This is not `prove`.** `ASSESSMENTS` is a five-word vocabulary
    (`matched|partial|mismatched|regressed|inconclusive`) about the CHANGE, and
    `prove.VERDICTS` (`proved|partial|unproved|contradicted`) stays the
    authority about the RUN. `partial` appears in both and means different
    things, so nothing in this package ever assigns one to the other; see
    `integrations/prove.py`, which maps explicitly and only downwards.

What is deliberately NOT here: no blobs, no pixels, no diff text, no model
output. Evidence is referenced, never inlined -- a delta must be cheap to read
or nobody will read it before acting.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from src.contracts.base import (
    SCHEMA_VERSION,
    ContractError,
    as_mapping,
    fingerprint,
    flag,
    now_iso,
    one_of,
    reject_unknown,
    text,
    text_list,
    timestamp,
    whole,
)

__all__ = [
    "SCHEMA_VERSION",
    "DeltaError",
    "DOMAINS",
    "OPERATIONS",
    "CHANGE_OPERATIONS",
    "CLASSIFICATIONS",
    "MATERIAL_CLASSIFICATIONS",
    "SEVERITIES",
    "SEVERITY_ORDER",
    "BLOCKING_SEVERITIES",
    "CONFIDENCE",
    "CONFIDENCE_ORDER",
    "EXTRACTION_TIERS",
    "TIER_CEILING",
    "INVARIANT_CLASSES",
    "INVARIANT_SOURCES",
    "INVARIANT_STATUSES",
    "CONDITION_KINDS",
    "REVISION_KINDS",
    "EVIDENCE_KINDS",
    "COVERAGE_DIMENSIONS",
    "ASSESSMENTS",
    "SENSITIVITIES",
    "ALIGNMENT_RELATIONS",
    "RevisionRef",
    "EvidenceRef",
    "RequestedChange",
    "Invariant",
    "ScopeRule",
    "Budget",
    "Privacy",
    "IntentContract",
    "DeltaRequest",
    "DeltaAssertion",
    "InvariantResult",
    "Coverage",
    "Alignment",
    "UniversalDelta",
    "UniversalDeltaRef",
    "new_id",
    "confidence_rank",
    "weakest_confidence",
    "severity_rank",
    "strongest_severity",
    "tier_rank",
    "ceiling_for_tier",
    "cap_confidence",
    "revision_hash",
]


class DeltaError(ContractError):
    """A rejection from this module. Names the field and the value, always."""


# -- what can be compared --------------------------------------------------
#
# Section 3.1: universal in the contract, specific in the extraction. The
# vocabulary below is closed because a domain nothing routes on is a string in
# a database: `registry.adapter_for()` refuses a domain it cannot serve rather
# than falling back to a generic comparer that would answer `unchanged` about a
# video because all it knows how to do is hash bytes.

#: Section 1.3, plus the two the architecture in §21 adds (`skill` beside
#: `workflow`, `binary` as the honest floor). `binary` exists so that "all we
#: could compare was the hash" is a domain with a name, rather than a code
#: adapter quietly degrading into one.
DOMAINS: Tuple[str, ...] = (
    "code", "document", "image", "video", "audio", "workflow", "skill",
    "state", "binary",
)

# -- the two axes ----------------------------------------------------------
#
# Section 4 writes one list. It is two, and the whole classifier depends on
# keeping them apart:
#
#   operation      -- what the extractor SAW happen to an element.
#   classification -- what that means AGAINST the frozen intent.
#
# `modified` + `requested` is success. `modified` + `regression` is a bug
# shipped. Same observation, opposite conclusion, and only the second needed
# the IntentContract to compute.

#: What happened to one aligned element. `unchanged` is a real observation and
#: is not the same as the absence of an assertion: the first says we looked,
#: the second says nothing about whether we did.
OPERATIONS: Tuple[str, ...] = (
    "added", "missing", "modified", "moved", "reencoded", "unchanged",
)

#: The operations that assert a difference. `reencoded` is in here on purpose:
#: a re-encode changes the bytes and is usually semantically empty, but calling
#: it `unchanged` would hide a re-compression that destroyed a gradient.
CHANGE_OPERATIONS: Tuple[str, ...] = (
    "added", "missing", "modified", "moved", "reencoded",
)

#: What the observation means against the request. `unknown` is what everything
#: falls back to and it is never a failure of nerve: it is the only honest
#: answer when the frozen intent says nothing about this path.
CLASSIFICATIONS: Tuple[str, ...] = (
    "requested", "required", "incidental", "regression", "preserved", "unknown",
)

#: The classes a caller has to read before acting. `requested` and `preserved`
#: are the good news; these three are the reason anyone opens a delta at all.
MATERIAL_CLASSIFICATIONS: Tuple[str, ...] = ("regression", "incidental", "unknown")

#: Section 4. Ordered weakest to strongest; `severity_rank` reads this order.
SEVERITIES: Tuple[str, ...] = ("info", "minor", "material", "blocking")
SEVERITY_ORDER: Dict[str, int] = {name: i for i, name in enumerate(SEVERITIES)}

#: What stops a caller. A `blocking` assertion must never be summarised away,
#: and a `material` one has to have been seen by whoever accepts the result.
BLOCKING_SEVERITIES: Tuple[str, ...] = ("material", "blocking")

#: Section 4. Ordered strongest to weakest -- the opposite direction from
#: SEVERITIES, on purpose: the operation that matters for confidence is "take
#: the WEAKEST of these", and a list that reads downhill makes that obvious.
CONFIDENCE: Tuple[str, ...] = ("exact", "high", "medium", "low", "unknown")
CONFIDENCE_ORDER: Dict[str, int] = {name: i for i, name in enumerate(CONFIDENCE)}

# -- the determinism ladder ------------------------------------------------
#
# Section 3.2. The order is a claim about how much a method can be trusted, and
# `TIER_CEILING` turns it into arithmetic the classifier cannot argue with: a
# perceptual metric may report `high` about itself and is still capped at
# `medium`, because a perceptual hash is not identity (§31).

#: Preference order. Later layers complement earlier ones; they never overturn
#: them, and `ceiling_for_tier` is what makes that structural rather than a
#: rule in a docstring nobody reads.
EXTRACTION_TIERS: Tuple[str, ...] = (
    "hash", "parser", "algorithm", "perceptual", "model", "human",
)

#: The best confidence a finding from each tier may claim. `human` is capped at
#: `high` rather than `exact` deliberately: a person reading a rendered page is
#: a strong witness and not a checksum, and the only thing worse than an
#: over-confident model is an over-confident review nobody can re-run.
TIER_CEILING: Dict[str, str] = {
    "hash": "exact",
    "parser": "exact",
    "algorithm": "high",
    "perceptual": "medium",
    "model": "medium",
    "human": "high",
}

# -- invariants ------------------------------------------------------------

#: Section 7. What KIND of property is being held still. The class decides the
#: floor on the severity of a violation: `security` and `permissions` are
#: blocking wherever they appear, which is §16's rule that security outranks
#: linguistic similarity, written where it can be enforced.
INVARIANT_CLASSES: Tuple[str, ...] = (
    "identity", "structure", "content", "format", "behavior", "security",
    "permissions", "performance", "provenance", "compatibility", "scope",
    "budget",
)

#: Where the invariant came from. Kept because §16 requires a `required` change
#: to be traceable to something other than the executor's own opinion, and
#: because an invariant the USER named is not negotiable by an evaluation
#: profile the way a `domain_default` is.
INVARIANT_SOURCES: Tuple[str, ...] = (
    "request", "capability", "artifact", "project_policy", "security",
    "operation", "decision", "domain_default",
)

#: Section 5.3. `not_applicable` is separate from `unknown` because "this
#: invariant does not apply to a PNG" and "we could not check it" lead to
#: different next actions, and folding them loses the difference forever.
INVARIANT_STATUSES: Tuple[str, ...] = (
    "preserved", "violated", "unknown", "not_applicable",
)

#: What a requested change asserts about a path. Small and parseable on
#: purpose: an intent language rich enough to need its own interpreter is an
#: intent language nobody can audit. `absent` and `removed` differ -- `removed`
#: says it was there and must go, `absent` says it must not appear at all.
CONDITION_KINDS: Tuple[str, ...] = (
    "becomes", "changes", "preserved", "added", "removed", "absent",
)

# -- references ------------------------------------------------------------

#: What a `RevisionRef` points at. `literal` is for a value handed in by the
#: caller (a config blob, a transcript) and still requires its hash, so that
#: two runs comparing "the same" literal can be told apart when they were not.
REVISION_KINDS: Tuple[str, ...] = (
    "checkpoint", "file", "artifact", "document", "state", "workflow",
    "skill", "blob", "literal",
)

#: Section 18. What kind of thing an evidence pointer is. Evidence is always a
#: REFERENCE: the rendered overlay, the diff hunk and the transcript span live
#: in the stores that already hold them, under their own retention.
EVIDENCE_KINDS: Tuple[str, ...] = (
    "byte_range", "path", "line_range", "symbol", "ast_node", "json_pointer",
    "page_span", "table_cell", "region", "mask", "overlay", "timestamp_range",
    "keyframe", "waveform_span", "transcript_span", "test_result", "metric",
    "state_observation", "artifact", "run", "command", "checkpoint",
)

#: Section 17. The named axes a delta reports coverage along. Closed so that
#: two adapters cannot invent `visual` and `pixels` for the same thing and make
#: the numbers incomparable.
COVERAGE_DIMENSIONS: Tuple[str, ...] = (
    "structural", "semantic", "temporal", "identity", "spatial", "behavioral",
)

#: Section 19. The verdict about the CHANGE. See rule 6 in the module
#: docstring: this is not `prove.VERDICTS` and never becomes it.
ASSESSMENTS: Tuple[str, ...] = (
    "matched", "partial", "mismatched", "regressed", "inconclusive",
)

#: Section 25. Inherited from the most restrictive source and never widened by
#: a derived artifact. The same four words the State Mirror uses, so a delta
#: about a state revision does not have to translate.
SENSITIVITIES: Tuple[str, ...] = ("public", "internal", "private", "secret")

#: Section 8. How a source element relates to a target element once alignment
#: has run. `uncertain` is the one that propagates: every assertion built on an
#: uncertain alignment is capped at `low`.
ALIGNMENT_RELATIONS: Tuple[str, ...] = (
    "same", "split", "merged", "moved", "replaced", "missing", "added",
    "uncertain",
)

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


# -- small helpers ---------------------------------------------------------


def new_id(prefix: str) -> str:
    """A short, sortable-enough id. Same shape as the rest of the repo.

    `delta_`, `assertion_`, `intent_`, `delta_request_`: the prefix is part of
    the id and not decoration, because these ids travel between subsystems and
    a bare hex string in a payload tells the reader nothing about what it can
    be handed to.
    """
    clean = str(prefix or "").strip().rstrip("_")
    if not clean:
        raise DeltaError("prefix", "is empty; an id without a prefix is a hex "
                                   "string nobody can route", got=prefix)
    return f"{clean}_{uuid.uuid4().hex[:20]}"


def confidence_rank(value: Any) -> int:
    """Position in `CONFIDENCE`; unknown words rank as `unknown` (weakest).

    Ranking an unrecognised word as the WEAKEST and not raising is deliberate.
    This is called on the hot path while folding many findings, and the failure
    mode of raising there is a delta that dies because one extractor emitted a
    typo; the failure mode of ranking low is a delta that under-claims. Only
    one of those two is safe.
    """
    return CONFIDENCE_ORDER.get(str(value or ""), len(CONFIDENCE) - 1)


def weakest_confidence(values: Sequence[Any]) -> str:
    """The weakest of several confidences. The whole propagation rule, once.

    Section 8: "una alineación dudosa propaga incertidumbre a las afirmaciones
    dependientes". An assertion is exactly as believable as the weakest link
    that produced it -- the alignment, the extraction and the threshold -- so
    combining is a `min` over the ladder and never an average. An average of
    `exact` and `unknown` is `medium`, which is a number nobody observed.
    """
    if not values:
        return "unknown"
    worst = max(confidence_rank(v) for v in values)
    return CONFIDENCE[worst]


def severity_rank(value: Any) -> int:
    """Position in `SEVERITIES`; an unrecognised word ranks as `info`."""
    return SEVERITY_ORDER.get(str(value or ""), 0)


def strongest_severity(values: Sequence[Any]) -> str:
    """The strongest of several severities -- how a delta rolls up.

    The opposite direction from `weakest_confidence`, and for the same reason:
    the honest summary of a set of findings is the worst thing in it, not the
    average of them. A delta with one blocking permission change and forty
    cosmetic ones is a blocking delta.
    """
    if not values:
        return "info"
    return SEVERITIES[max(severity_rank(v) for v in values)]


def tier_rank(value: Any) -> int:
    """Position in `EXTRACTION_TIERS`; an unknown method ranks last (weakest).

    Last, not first: a method this module does not recognise gets the treatment
    of the least trusted tier it knows, which is the direction that cannot
    launder a guess into a certainty.
    """
    name = str(value or "")
    if name in EXTRACTION_TIERS:
        return EXTRACTION_TIERS.index(name)
    return len(EXTRACTION_TIERS) - 1


def ceiling_for_tier(tier: Any) -> str:
    """The best confidence a finding from `tier` may claim. Unknown -> `low`."""
    return TIER_CEILING.get(str(tier or ""), "low")


def cap_confidence(confidence: Any, tier: Any) -> str:
    """Confidence, never stronger than its tier allows. §3.2, mechanised.

    This is the function that stops "the model was very sure" from becoming
    "exact". It is applied in `DeltaAssertion.parse`, so a caller cannot route
    around it by building the dataclass directly with keyword arguments -- and
    that is the point of doing it in the contract rather than in the adapter
    that happened to be written most carefully.
    """
    claimed = confidence_rank(confidence)
    allowed = confidence_rank(ceiling_for_tier(tier))
    return CONFIDENCE[max(claimed, allowed)]


def revision_hash(parts: Sequence[Tuple[str, Any]]) -> str:
    """A sha256 for a revision that has no natural content hash.

    A State Mirror revision is an integer and an artifact version is a row.
    Neither is a hash, and rule 2 in the module docstring does not bend for
    them: the caller fingerprints whatever identifies the revision and passes
    the result. Same length-prefixed scheme as `prove.identity_of`, so two
    subsystems describing the same revision agree by construction.
    """
    return fingerprint(parts)


def _sha(data: Mapping[str, Any], key: str, path: str, *, required: bool = True) -> str:
    """A sha256 hex digest. Rule 2 lives here.

    Deliberately not `base.sha256_hex` with `required=False` everywhere: the
    default in this module is that a missing hash is an ERROR, because a
    revision without one is the moving `latest` the whole subsystem exists to
    refuse.
    """
    raw = data.get(key, "")
    value = str(raw or "").strip().lower()
    if not value:
        if required:
            raise DeltaError(
                f"{path}.{key}",
                "is required; a revision without a hash is a moving target and "
                "a comparison against one was true for nobody",
                got=raw,
            )
        return ""
    if not _HEX64.match(value):
        raise DeltaError(f"{path}.{key}", "is not a sha256 hex digest", got=raw)
    return value


def _ratio(data: Mapping[str, Any], key: str, path: str, *, default: float = 0.0) -> float:
    """A 0..1 coverage ratio. Rejects the out-of-range rather than clamping.

    Clamping a 1.4 to 1.0 would turn an extractor's arithmetic bug into a claim
    of complete coverage, which is the one direction that must never happen
    silently.
    """
    raw = data.get(key, None)
    if raw is None:
        return float(default)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise DeltaError(f"{path}.{key}", "expected a number between 0 and 1", got=raw)
    value = float(raw)
    if value < 0.0 or value > 1.0:
        raise DeltaError(f"{path}.{key}", "is outside 0..1", got=raw)
    return round(value, 4)


def _mapping(data: Mapping[str, Any], key: str, path: str) -> Dict[str, Any]:
    """An open sub-mapping (thresholds, budgets, extractor versions).

    Open on purpose and the only place in this module that is: a threshold is
    domain-specific (`{"delta_e": 2.0}` for colour, `{"iou": 0.9}` for a mask)
    and a closed vocabulary here would mean this file has to change before any
    new extractor can express what it measured. The keys are stringified and
    the values are stored as given.
    """
    raw = data.get(key, None)
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise DeltaError(f"{path}.{key}", "expected an object", got=raw)
    return {str(k): v for k, v in raw.items()}


# -- references ------------------------------------------------------------


@dataclass(frozen=True)
class RevisionRef:
    """One immutable end of a comparison. Rule 2 of the module docstring.

    `ref` is how the owning system names it (`checkpoint:<sha>`,
    `artifact:art_...`, `state:project://admin/real/abc#7`) and `hash` is what
    makes it immutable. Both are required: the ref alone can be re-pointed, and
    the hash alone cannot be fetched.
    """

    kind: str
    ref: str
    hash: str
    label: str = ""
    at: str = ""
    sensitivity: str = "internal"
    schema_version: int = SCHEMA_VERSION

    _KEYS = ("kind", "ref", "hash", "label", "at", "sensitivity", "schema_version")

    @classmethod
    def parse(cls, raw: Any, path: str = "revision") -> "RevisionRef":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        return cls(
            kind=one_of(data, "kind", path, choices=REVISION_KINDS) or "",
            ref=text(data, "ref", path, max_len=1024),
            hash=_sha(data, "hash", path),
            label=text(data, "label", path, required=False, max_len=200),
            at=timestamp(data, "at", path) or "",
            sensitivity=one_of(data, "sensitivity", path, choices=SENSITIVITIES,
                               required=False, default="internal") or "internal",
            schema_version=whole(data, "schema_version", path, default=SCHEMA_VERSION,
                                 minimum=1) or SCHEMA_VERSION,
        )

    def to_dict(self) -> Dict[str, Any]:
        # `at` is omitted rather than emitted empty, because `base.timestamp`
        # refuses `""` -- correctly, since an unreadable date is `None` and not
        # the moment we happened to read it -- and a `to_dict` whose output its
        # own `parse` rejects is a contract that cannot survive a round trip
        # through the database it is stored in.
        out: Dict[str, Any] = {
            "kind": self.kind, "ref": self.ref, "hash": self.hash,
            "label": self.label, "sensitivity": self.sensitivity,
            "schema_version": self.schema_version,
        }
        if self.at:
            out["at"] = self.at
        return out

    def identity(self) -> str:
        """`kind:hash` -- what makes two refs the same revision.

        `ref` is excluded: the same artifact reached through two paths is one
        revision, and a cache keyed on the path would compare it against itself
        twice and report a difference of nothing.
        """
        return f"{self.kind}:{self.hash}"


@dataclass(frozen=True)
class EvidenceRef:
    """A pointer to what was looked at. Section 18.

    Never the evidence itself. An overlay PNG is an artifact with a hash and a
    retention policy; a diff hunk is a checkpoint and a path. Inlining either
    would make a delta expensive to read, and a delta nobody reads before
    acting is a delta that changed nothing about how the system behaves.
    """

    kind: str
    ref: str
    detail: str = ""
    hash: str = ""
    sensitivity: str = "internal"

    _KEYS = ("kind", "ref", "detail", "hash", "sensitivity")

    @classmethod
    def parse(cls, raw: Any, path: str = "evidence") -> "EvidenceRef":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        return cls(
            kind=one_of(data, "kind", path, choices=EVIDENCE_KINDS) or "",
            ref=text(data, "ref", path, max_len=1024),
            detail=text(data, "detail", path, required=False, max_len=500),
            hash=_sha(data, "hash", path, required=False),
            sensitivity=one_of(data, "sensitivity", path, choices=SENSITIVITIES,
                               required=False, default="internal") or "internal",
        )

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"kind": self.kind, "ref": self.ref}
        if self.detail:
            out["detail"] = self.detail
        if self.hash:
            out["hash"] = self.hash
        out["sensitivity"] = self.sensitivity
        return out


# -- intent ----------------------------------------------------------------


@dataclass(frozen=True)
class RequestedChange:
    """One thing the user asked to be different, as a checkable condition.

    `path` is domain-addressed (`character.jacket.color`, `src/auth.py#consume_state`,
    `nodes.publish.permissions.network`) and this module does not interpret it:
    the adapter for the domain does. What this module enforces is that a
    request has a CONDITION, because "change the jacket" with no condition
    cannot be checked against a result and turns every outcome into a match.
    """

    path: str
    condition: str
    value: str = ""
    detail: str = ""
    priority: int = 1

    _KEYS = ("path", "condition", "value", "detail", "priority")

    @classmethod
    def parse(cls, raw: Any, path: str = "requested") -> "RequestedChange":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        condition = one_of(data, "condition", path, choices=CONDITION_KINDS) or ""
        value = text(data, "value", path, required=False, max_len=1024)
        if condition == "becomes" and not value:
            raise DeltaError(
                f"{path}.value",
                "is required when the condition is `becomes`; a target value "
                "nobody named cannot be compared against the result",
                got=data.get("value"),
            )
        return cls(
            path=text(data, "path", path, max_len=512),
            condition=condition,
            value=value,
            detail=text(data, "detail", path, required=False, max_len=500),
            priority=whole(data, "priority", path, default=1, minimum=0, maximum=9) or 0,
        )

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"path": self.path, "condition": self.condition}
        if self.value:
            out["value"] = self.value
        if self.detail:
            out["detail"] = self.detail
        out["priority"] = self.priority
        return out


@dataclass(frozen=True)
class Invariant:
    """A property that must hold across the change. Section 7.

    The severity floor is not stored here as a computed field: `default_severity()`
    reads the class, so that a caller cannot ship an invariant of class
    `security` with severity `info` and have a violation of it summarised away.
    """

    id: str
    klass: str
    path: str = ""
    description: str = ""
    source: str = "domain_default"
    severity: str = "material"
    threshold: Mapping[str, Any] = None  # type: ignore[assignment]

    _KEYS = ("id", "klass", "class", "path", "description", "source", "severity",
             "threshold")

    def __post_init__(self) -> None:
        if self.threshold is None:
            object.__setattr__(self, "threshold", {})

    @classmethod
    def parse(cls, raw: Any, path: str = "invariant") -> "Invariant":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        # `class` is the word the plan uses in YAML and `klass` is what Python
        # allows as an attribute. Accepting both on the way in, and emitting
        # `class` on the way out, keeps the plan's documents loadable without
        # asking anyone to remember a Python keyword rule.
        payload = dict(data)
        if "class" in payload and "klass" not in payload:
            payload["klass"] = payload["class"]
        klass = one_of(payload, "klass", path, choices=INVARIANT_CLASSES) or ""
        severity = one_of(payload, "severity", path, choices=SEVERITIES,
                          required=False, default="") or ""
        floor = _CLASS_SEVERITY_FLOOR.get(klass, "info")
        if not severity:
            severity = "blocking" if floor == "blocking" else "material"
        elif severity_rank(severity) < severity_rank(floor):
            raise DeltaError(
                f"{path}.severity",
                f"is below the floor for a `{klass}` invariant (`{floor}`); a "
                f"security or permission property that can be waived by "
                f"lowering its severity is not an invariant",
                got=payload.get("severity"),
            )
        return cls(
            id=text(payload, "id", path, max_len=120),
            klass=klass,
            path=text(payload, "path", path, required=False, max_len=512),
            description=text(payload, "description", path, required=False, max_len=1000),
            source=one_of(payload, "source", path, choices=INVARIANT_SOURCES,
                          required=False, default="domain_default") or "domain_default",
            severity=severity,
            threshold=_mapping(payload, "threshold", path),
        )

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "id": self.id, "class": self.klass, "source": self.source,
            "severity": self.severity,
        }
        if self.path:
            out["path"] = self.path
        if self.description:
            out["description"] = self.description
        if self.threshold:
            out["threshold"] = dict(self.threshold)
        return out


#: Section 16: "seguridad y permisos tienen prioridad sobre similitud
#: lingüística". A violation of one of these cannot be filed as `info` and
#: scrolled past, so the floor is enforced at parse time.
_CLASS_SEVERITY_FLOOR: Dict[str, str] = {
    "security": "blocking",
    "permissions": "blocking",
    "provenance": "material",
    "identity": "material",
    "compatibility": "material",
    "behavior": "material",
    "budget": "material",
    "scope": "material",
}


@dataclass(frozen=True)
class ScopeRule:
    """Where change is allowed. Section 6's `scope`, and §16's fourth question.

    `allowed` and `forbidden` are both here because they answer different
    questions and a system with only one of them gets a common case wrong.
    `allowed` is "the jacket mask" -- everything outside it that changed is
    incidental at best. `forbidden` is "never touch `settings.py`" -- inside
    the allowed area and still off-limits. With only `allowed`, a forbidden
    path that happens to sit inside the editable region reads as requested.
    """

    allowed: Tuple[str, ...] = ()
    forbidden: Tuple[str, ...] = ()
    note: str = ""

    _KEYS = ("allowed", "forbidden", "note")

    @classmethod
    def parse(cls, raw: Any, path: str = "scope") -> "ScopeRule":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        return cls(
            allowed=text_list(data, "allowed", path, default=(), max_len=512),
            forbidden=text_list(data, "forbidden", path, default=(), max_len=512),
            note=text(data, "note", path, required=False, max_len=500),
        )

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        if self.allowed:
            out["allowed"] = list(self.allowed)
        if self.forbidden:
            out["forbidden"] = list(self.forbidden)
        if self.note:
            out["note"] = self.note
        return out

    @property
    def bounded(self) -> bool:
        """True when the request named an editable region at all.

        An unbounded scope is not an error -- most code requests have none --
        but it changes what `incidental` means, and the classifier needs to
        know which of the two situations it is in rather than treating an empty
        tuple as "nothing is allowed".
        """
        return bool(self.allowed)


@dataclass(frozen=True)
class Budget:
    """What the comparison may spend. Section 22 and §25's limits.

    `tiers` is the interesting field: it is the list of extraction tiers this
    request may use, and it is how `literal` and `professional` completion
    modes differ from `maximalist` without anyone rewriting an adapter. A
    budget that excludes `model` does not make the model's answer wrong; it
    means this delta will report `unknown` where a model would have guessed,
    which is the trade the caller chose.
    """

    max_seconds: Optional[int] = None
    max_bytes: Optional[int] = None
    max_elements: Optional[int] = None
    tiers: Tuple[str, ...] = EXTRACTION_TIERS

    _KEYS = ("max_seconds", "max_bytes", "max_elements", "tiers")

    @classmethod
    def parse(cls, raw: Any, path: str = "budget") -> "Budget":
        data = as_mapping(raw, path) if raw is not None else {}
        reject_unknown(data, cls._KEYS, path)
        tiers = text_list(data, "tiers", path, default=EXTRACTION_TIERS,
                          choices=EXTRACTION_TIERS, max_items=len(EXTRACTION_TIERS))
        return cls(
            max_seconds=whole(data, "max_seconds", path, minimum=1, maximum=86_400),
            max_bytes=whole(data, "max_bytes", path, minimum=1),
            max_elements=whole(data, "max_elements", path, minimum=1),
            tiers=tuple(tiers) or EXTRACTION_TIERS,
        )

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"tiers": list(self.tiers)}
        for key in ("max_seconds", "max_bytes", "max_elements"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        return out

    def allows(self, tier: Any) -> bool:
        """Whether an extractor of this tier may run under this budget."""
        return str(tier or "") in self.tiers


@dataclass(frozen=True)
class Privacy:
    """Where the bytes may go. Section 3.7 and §25.

    `local_only` defaults to True and `allow_external` to False, and that pair
    is not redundant: the first is about where extraction RUNS, the second
    about whether a third-party evaluator may be handed the content. A future
    local evaluator with a network dependency needs the first to be true and
    the second to stay false.
    """

    local_only: bool = True
    allow_external: bool = False
    redact_secrets: bool = True
    sensitivity: str = "internal"

    _KEYS = ("local_only", "allow_external", "redact_secrets", "sensitivity")

    @classmethod
    def parse(cls, raw: Any, path: str = "privacy") -> "Privacy":
        data = as_mapping(raw, path) if raw is not None else {}
        reject_unknown(data, cls._KEYS, path)
        local_only = flag(data, "local_only", path, default=True)
        allow_external = flag(data, "allow_external", path, default=False)
        if local_only and allow_external:
            raise DeltaError(
                f"{path}.allow_external",
                "is true while `local_only` is also true; one of the two is a "
                "mistake and guessing which would be a decision about where "
                "someone's private artifact goes",
                got=data.get("allow_external"),
            )
        return cls(
            local_only=local_only,
            allow_external=allow_external,
            redact_secrets=flag(data, "redact_secrets", path, default=True),
            sensitivity=one_of(data, "sensitivity", path, choices=SENSITIVITIES,
                               required=False, default="internal") or "internal",
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "local_only": self.local_only,
            "allow_external": self.allow_external,
            "redact_secrets": self.redact_secrets,
            "sensitivity": self.sensitivity,
        }


@dataclass(frozen=True)
class IntentContract:
    """What was asked, frozen before the result was seen. Rule 3.

    `frozen_at` is required and there is no `unfreeze`. A change of mind is a
    NEW contract whose `supersedes` names this one, so the audit trail keeps
    both and the second cannot pretend to have been the first. §31: "no cambiar
    IntentContract después de ver target" -- written here as a field that
    cannot be absent rather than as advice.

    `unknowns` is the field that keeps this honest in the other direction: a
    request the compiler could not turn into a checkable condition is listed
    there, not silently dropped and not invented into a condition. A contract
    with unknowns can still run; what it cannot do is produce `matched`.
    """

    id: str
    owner: str
    domain: str
    requested: Tuple[RequestedChange, ...] = ()
    invariants: Tuple[Invariant, ...] = ()
    scope: ScopeRule = None  # type: ignore[assignment]
    tolerances: Mapping[str, Any] = None  # type: ignore[assignment]
    acceptance: Tuple[str, ...] = ()
    unknowns: Tuple[str, ...] = ()
    source_text: str = ""
    evaluation_profile: str = "default"
    project_id: str = ""
    supersedes: str = ""
    frozen_at: str = ""
    created_at: str = ""
    schema_version: int = SCHEMA_VERSION

    _KEYS = ("id", "owner", "domain", "requested", "invariants", "scope",
             "tolerances", "acceptance", "unknowns", "source_text",
             "evaluation_profile", "project_id", "supersedes", "frozen_at",
             "created_at", "schema_version")

    def __post_init__(self) -> None:
        if self.scope is None:
            object.__setattr__(self, "scope", ScopeRule())
        if self.tolerances is None:
            object.__setattr__(self, "tolerances", {})

    @classmethod
    def parse(cls, raw: Any, path: str = "intent") -> "IntentContract":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        frozen_at = timestamp(data, "frozen_at", path) or ""
        if not frozen_at:
            raise DeltaError(
                f"{path}.frozen_at",
                "is required; an intent that was not frozen before the result "
                "was observed can be edited to match it, and then every "
                "evaluation scores full marks",
                got=data.get("frozen_at"),
            )
        requested = tuple(
            RequestedChange.parse(item, f"{path}.requested[{i}]")
            for i, item in enumerate(_seq(data, "requested", path))
        )
        invariants = tuple(
            Invariant.parse(item, f"{path}.invariants[{i}]")
            for i, item in enumerate(_seq(data, "invariants", path))
        )
        seen: Dict[str, int] = {}
        for i, inv in enumerate(invariants):
            if inv.id in seen:
                raise DeltaError(
                    f"{path}.invariants[{i}].id",
                    f"repeats the id at index {seen[inv.id]}; two invariants "
                    f"with one id produce two results that overwrite each other",
                    got=inv.id,
                )
            seen[inv.id] = i
        return cls(
            id=text(data, "id", path, max_len=120),
            owner=text(data, "owner", path, max_len=200),
            domain=one_of(data, "domain", path, choices=DOMAINS) or "",
            requested=requested,
            invariants=invariants,
            scope=ScopeRule.parse(data.get("scope") or {}, f"{path}.scope"),
            tolerances=_mapping(data, "tolerances", path),
            acceptance=text_list(data, "acceptance", path, default=(), max_len=500),
            unknowns=text_list(data, "unknowns", path, default=(), max_len=500),
            source_text=text(data, "source_text", path, required=False, max_len=8000),
            evaluation_profile=text(data, "evaluation_profile", path, required=False,
                                    default="default", max_len=120),
            project_id=text(data, "project_id", path, required=False, max_len=200),
            supersedes=text(data, "supersedes", path, required=False, max_len=120),
            frozen_at=frozen_at,
            created_at=timestamp(data, "created_at", path) or now_iso(),
            schema_version=whole(data, "schema_version", path, default=SCHEMA_VERSION,
                                 minimum=1) or SCHEMA_VERSION,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "owner": self.owner, "domain": self.domain,
            "requested": [r.to_dict() for r in self.requested],
            "invariants": [i.to_dict() for i in self.invariants],
            "scope": self.scope.to_dict(),
            "tolerances": dict(self.tolerances),
            "acceptance": list(self.acceptance),
            "unknowns": list(self.unknowns),
            "source_text": self.source_text,
            "evaluation_profile": self.evaluation_profile,
            "project_id": self.project_id,
            "supersedes": self.supersedes,
            "frozen_at": self.frozen_at,
            "created_at": self.created_at,
            "schema_version": self.schema_version,
        }

    def fingerprint(self) -> str:
        """What was asked, independent of when it was asked.

        `id`, `created_at` and `frozen_at` are excluded so that two contracts
        expressing the same request share a fingerprint -- which is what makes
        the §22 cache key (`source + target + intent + profile`) hit at all.
        `supersedes` is excluded for the same reason: a re-frozen identical
        contract is the same question.
        """
        return fingerprint([
            ("domain", self.domain),
            ("requested", [r.to_dict() for r in self.requested]),
            ("invariants", [i.to_dict() for i in self.invariants]),
            ("scope", self.scope.to_dict()),
            ("tolerances", dict(self.tolerances)),
            ("acceptance", list(self.acceptance)),
            ("profile", self.evaluation_profile),
        ])

    def invariant(self, invariant_id: str) -> Optional[Invariant]:
        for inv in self.invariants:
            if inv.id == invariant_id:
                return inv
        return None


def _seq(data: Mapping[str, Any], key: str, path: str) -> Sequence[Any]:
    """A list of sub-objects, or `[]`. A bare object is an error, not one item.

    Accepting a single mapping where a list belongs is the kind of politeness
    that hides a caller bug until the day the caller sends two.
    """
    raw = data.get(key, None)
    if raw is None:
        return ()
    if isinstance(raw, Mapping) or isinstance(raw, (str, bytes)):
        raise DeltaError(f"{path}.{key}", "expected a list", got=raw)
    try:
        return list(raw)
    except TypeError as exc:  # noqa: BLE001 - the message has to name the field
        raise DeltaError(f"{path}.{key}", "expected a list", got=raw) from exc


# -- the request -----------------------------------------------------------


@dataclass(frozen=True)
class DeltaRequest:
    """Compare these two revisions, under this intent, within this budget.

    The request carries `intent_contract_id` OR the raw material to compile one
    (`intent_text`, `requested`, `invariants`), never both. Allowing both would
    mean the service has to decide which wins, and the two situations are
    genuinely different: a caller re-running a comparison names the contract it
    already froze; a caller asking for the first time hands over prose.
    """

    id: str
    owner: str
    domain: str
    source: RevisionRef
    target: RevisionRef
    intent_contract_id: str = ""
    intent_text: str = ""
    requested: Tuple[RequestedChange, ...] = ()
    invariants: Tuple[Invariant, ...] = ()
    evaluation_profile: str = "default"
    budget: Budget = None  # type: ignore[assignment]
    privacy: Privacy = None  # type: ignore[assignment]
    project_id: str = ""
    session_id: str = ""
    run_id: str = ""
    correlation_id: str = ""
    created_at: str = ""
    schema_version: int = SCHEMA_VERSION

    _KEYS = ("id", "owner", "domain", "source", "target", "intent_contract_id",
             "intent_text", "requested", "invariants", "evaluation_profile",
             "budget", "privacy", "project_id", "session_id", "run_id",
             "correlation_id", "created_at", "schema_version")

    def __post_init__(self) -> None:
        if self.budget is None:
            object.__setattr__(self, "budget", Budget())
        if self.privacy is None:
            object.__setattr__(self, "privacy", Privacy())

    @classmethod
    def parse(cls, raw: Any, path: str = "delta_request") -> "DeltaRequest":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        contract_id = text(data, "intent_contract_id", path, required=False, max_len=120)
        intent_text = text(data, "intent_text", path, required=False, max_len=8000)
        requested = tuple(
            RequestedChange.parse(item, f"{path}.requested[{i}]")
            for i, item in enumerate(_seq(data, "requested", path))
        )
        invariants = tuple(
            Invariant.parse(item, f"{path}.invariants[{i}]")
            for i, item in enumerate(_seq(data, "invariants", path))
        )
        if contract_id and (intent_text or requested or invariants):
            raise DeltaError(
                f"{path}.intent_contract_id",
                "is set alongside raw intent material; a frozen contract and a "
                "fresh request are two different asks and picking one for the "
                "caller would silently discard the other",
                got=contract_id,
            )
        source = RevisionRef.parse(data.get("source"), f"{path}.source")
        target = RevisionRef.parse(data.get("target"), f"{path}.target")
        if source.identity() == target.identity():
            # Not an error: "nothing changed" is a legitimate and useful answer,
            # and refusing it here would push callers into skipping the delta
            # exactly when they most want the record that they checked.
            pass
        return cls(
            id=text(data, "id", path, required=False, max_len=120) or new_id("delta_request"),
            owner=text(data, "owner", path, max_len=200),
            domain=one_of(data, "domain", path, choices=DOMAINS) or "",
            source=source,
            target=target,
            intent_contract_id=contract_id,
            intent_text=intent_text,
            requested=requested,
            invariants=invariants,
            evaluation_profile=text(data, "evaluation_profile", path, required=False,
                                    default="default", max_len=120),
            budget=Budget.parse(data.get("budget"), f"{path}.budget"),
            privacy=Privacy.parse(data.get("privacy"), f"{path}.privacy"),
            project_id=text(data, "project_id", path, required=False, max_len=200),
            session_id=text(data, "session_id", path, required=False, max_len=200),
            run_id=text(data, "run_id", path, required=False, max_len=200),
            correlation_id=text(data, "correlation_id", path, required=False, max_len=200),
            created_at=timestamp(data, "created_at", path) or now_iso(),
            schema_version=whole(data, "schema_version", path, default=SCHEMA_VERSION,
                                 minimum=1) or SCHEMA_VERSION,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "owner": self.owner, "domain": self.domain,
            "source": self.source.to_dict(), "target": self.target.to_dict(),
            "intent_contract_id": self.intent_contract_id,
            "intent_text": self.intent_text,
            "requested": [r.to_dict() for r in self.requested],
            "invariants": [i.to_dict() for i in self.invariants],
            "evaluation_profile": self.evaluation_profile,
            "budget": self.budget.to_dict(),
            "privacy": self.privacy.to_dict(),
            "project_id": self.project_id, "session_id": self.session_id,
            "run_id": self.run_id, "correlation_id": self.correlation_id,
            "created_at": self.created_at, "schema_version": self.schema_version,
        }


# -- the findings ----------------------------------------------------------


@dataclass(frozen=True)
class Alignment:
    """Which source element is which target element. Section 8.

    Kept as its own object rather than two fields on an assertion because a
    `split` or a `merged` is one alignment and several assertions, and because
    the confidence of the alignment is an input to every one of them.
    `assertion.confidence` is already capped by this via `weakest_confidence`,
    so an assertion built on an `uncertain` alignment cannot claim `exact` even
    if its own extractor was a hash.
    """

    relation: str
    source_element: str = ""
    target_element: str = ""
    confidence: str = "unknown"
    method: str = ""
    tier: str = "algorithm"
    evidence_refs: Tuple[EvidenceRef, ...] = ()

    _KEYS = ("relation", "source_element", "target_element", "confidence",
             "method", "tier", "evidence_refs")

    @classmethod
    def parse(cls, raw: Any, path: str = "alignment") -> "Alignment":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        relation = one_of(data, "relation", path, choices=ALIGNMENT_RELATIONS) or ""
        tier = one_of(data, "tier", path, choices=EXTRACTION_TIERS,
                      required=False, default="algorithm") or "algorithm"
        confidence = one_of(data, "confidence", path, choices=CONFIDENCE,
                            required=False, default="unknown") or "unknown"
        if relation == "uncertain":
            # An uncertain alignment that reports high confidence is a
            # contradiction the rest of the pipeline would propagate as fact.
            confidence = weakest_confidence([confidence, "low"])
        source_element = text(data, "source_element", path, required=False, max_len=512)
        target_element = text(data, "target_element", path, required=False, max_len=512)
        if relation in ("same", "moved", "replaced") and not (source_element and target_element):
            raise DeltaError(
                f"{path}.relation",
                f"is `{relation}` but one of the two elements is missing; a "
                f"correspondence needs both ends or it is an addition or a "
                f"removal wearing the wrong word",
                got={"source_element": source_element, "target_element": target_element},
            )
        return cls(
            relation=relation,
            source_element=source_element,
            target_element=target_element,
            confidence=cap_confidence(confidence, tier),
            method=text(data, "method", path, required=False, max_len=200),
            tier=tier,
            evidence_refs=tuple(
                EvidenceRef.parse(item, f"{path}.evidence_refs[{i}]")
                for i, item in enumerate(_seq(data, "evidence_refs", path))
            ),
        )

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"relation": self.relation, "confidence": self.confidence,
                               "tier": self.tier}
        if self.source_element:
            out["source_element"] = self.source_element
        if self.target_element:
            out["target_element"] = self.target_element
        if self.method:
            out["method"] = self.method
        if self.evidence_refs:
            out["evidence_refs"] = [e.to_dict() for e in self.evidence_refs]
        return out


@dataclass(frozen=True)
class DeltaAssertion:
    """One statement about one element. Section 5.2, and rule 1 of this module.

    Three refusals live in `parse`, and each of them is a lie this contract has
    decided not to be able to tell:

    * `preserved` with `confidence="unknown"` -- "we did not look" rendered as
      "it is fine". This is the failure the entire subsystem exists to prevent.
    * `preserved` with a change operation -- a word saying it stayed and a word
      saying it moved, in the same row.
    * a confidence stronger than the method's tier allows -- see
      `cap_confidence`; here it is applied rather than raised, because an
      over-confident extractor is common and a delta that dies on one is worse
      than a delta that quietly tells the truth about it.
    """

    id: str
    path: str
    operation: str
    classification: str = "unknown"
    severity: str = "info"
    confidence: str = "unknown"
    before: str = ""
    after: str = ""
    method: str = ""
    tier: str = "algorithm"
    alignment: Optional[Alignment] = None
    evidence_refs: Tuple[EvidenceRef, ...] = ()
    intent_refs: Tuple[str, ...] = ()
    invariant_refs: Tuple[str, ...] = ()
    limitations: Tuple[str, ...] = ()
    detail: str = ""

    _KEYS = ("id", "path", "operation", "classification", "severity", "confidence",
             "before", "after", "method", "tier", "alignment", "evidence_refs",
             "intent_refs", "invariant_refs", "limitations", "detail")

    @classmethod
    def parse(cls, raw: Any, path: str = "assertion") -> "DeltaAssertion":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        operation = one_of(data, "operation", path, choices=OPERATIONS) or ""
        classification = one_of(data, "classification", path, choices=CLASSIFICATIONS,
                                required=False, default="unknown") or "unknown"
        tier = one_of(data, "tier", path, choices=EXTRACTION_TIERS,
                      required=False, default="algorithm") or "algorithm"
        confidence = one_of(data, "confidence", path, choices=CONFIDENCE,
                            required=False, default="unknown") or "unknown"
        alignment_raw = data.get("alignment")
        alignment = (Alignment.parse(alignment_raw, f"{path}.alignment")
                     if alignment_raw else None)
        confidence = cap_confidence(confidence, tier)
        if alignment is not None:
            confidence = weakest_confidence([confidence, alignment.confidence])
        if classification == "preserved":
            if confidence == "unknown":
                raise DeltaError(
                    f"{path}.confidence",
                    "is `unknown` on an assertion classified `preserved`; not "
                    "detected is not preserved, and this row is the shape that "
                    "claim would take",
                    got=data.get("confidence"),
                )
            if operation in CHANGE_OPERATIONS:
                raise DeltaError(
                    f"{path}.operation",
                    f"is `{operation}` on an assertion classified `preserved`; "
                    f"the observation and the conclusion contradict each other "
                    f"in the same row",
                    got=data.get("operation"),
                )
        severity = one_of(data, "severity", path, choices=SEVERITIES,
                          required=False, default="") or ""
        if not severity:
            severity = "material" if classification == "regression" else "info"
        elif classification == "regression" and severity_rank(severity) < severity_rank("material"):
            raise DeltaError(
                f"{path}.severity",
                "is below `material` on a regression; a regression that can be "
                "filed as `info` is a regression nobody will read",
                got=data.get("severity"),
            )
        return cls(
            id=text(data, "id", path, required=False, max_len=120) or new_id("assertion"),
            path=text(data, "path", path, max_len=1024),
            operation=operation,
            classification=classification,
            severity=severity,
            confidence=confidence,
            before=text(data, "before", path, required=False, max_len=2000, allow_blank=True),
            after=text(data, "after", path, required=False, max_len=2000, allow_blank=True),
            method=text(data, "method", path, required=False, max_len=200),
            tier=tier,
            alignment=alignment,
            evidence_refs=tuple(
                EvidenceRef.parse(item, f"{path}.evidence_refs[{i}]")
                for i, item in enumerate(_seq(data, "evidence_refs", path))
            ),
            intent_refs=text_list(data, "intent_refs", path, default=(), max_len=512),
            invariant_refs=text_list(data, "invariant_refs", path, default=(), max_len=120),
            limitations=text_list(data, "limitations", path, default=(), max_len=500),
            detail=text(data, "detail", path, required=False, max_len=1000),
        )

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "id": self.id, "path": self.path, "operation": self.operation,
            "classification": self.classification, "severity": self.severity,
            "confidence": self.confidence, "tier": self.tier,
        }
        for key in ("before", "after", "method", "detail"):
            value = getattr(self, key)
            if value:
                out[key] = value
        if self.alignment is not None:
            out["alignment"] = self.alignment.to_dict()
        if self.evidence_refs:
            out["evidence_refs"] = [e.to_dict() for e in self.evidence_refs]
        for key in ("intent_refs", "invariant_refs", "limitations"):
            value = getattr(self, key)
            if value:
                out[key] = list(value)
        return out

    @property
    def material(self) -> bool:
        """Whether a caller has to have seen this before acting."""
        return self.severity in BLOCKING_SEVERITIES

    @property
    def changed(self) -> bool:
        return self.operation in CHANGE_OPERATIONS


@dataclass(frozen=True)
class InvariantResult:
    """What happened to one invariant. Section 5.3, and rule 1 again.

    `preserved` requires at least one observation AND a confidence better than
    `unknown`, for the same reason it does on an assertion: this is the row a
    reader scans to decide the face is still the same face, and a detector that
    ran and found nothing is not a witness that nothing happened.
    """

    invariant_id: str
    status: str
    observations: Tuple[str, ...] = ()
    threshold: Mapping[str, Any] = None  # type: ignore[assignment]
    confidence: str = "unknown"
    method: str = ""
    tier: str = "algorithm"
    severity: str = "material"
    evidence_refs: Tuple[EvidenceRef, ...] = ()
    limitations: Tuple[str, ...] = ()

    _KEYS = ("invariant_id", "status", "observations", "threshold", "confidence",
             "method", "tier", "severity", "evidence_refs", "limitations")

    def __post_init__(self) -> None:
        if self.threshold is None:
            object.__setattr__(self, "threshold", {})

    @classmethod
    def parse(cls, raw: Any, path: str = "invariant_result") -> "InvariantResult":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        status = one_of(data, "status", path, choices=INVARIANT_STATUSES) or ""
        tier = one_of(data, "tier", path, choices=EXTRACTION_TIERS,
                      required=False, default="algorithm") or "algorithm"
        confidence = cap_confidence(
            one_of(data, "confidence", path, choices=CONFIDENCE,
                   required=False, default="unknown") or "unknown",
            tier,
        )
        observations = text_list(data, "observations", path, default=(), max_len=500,
                                 unique=False)
        if status == "preserved":
            if not observations:
                raise DeltaError(
                    f"{path}.observations",
                    "is empty on an invariant reported `preserved`; a property "
                    "held still is a measurement, and with no measurement the "
                    "answer is `unknown`",
                    got=data.get("observations"),
                )
            if confidence == "unknown":
                raise DeltaError(
                    f"{path}.confidence",
                    "is `unknown` on an invariant reported `preserved`; the "
                    "status and the confidence contradict each other",
                    got=data.get("confidence"),
                )
        return cls(
            invariant_id=text(data, "invariant_id", path, max_len=120),
            status=status,
            observations=observations,
            threshold=_mapping(data, "threshold", path),
            confidence=confidence,
            method=text(data, "method", path, required=False, max_len=200),
            tier=tier,
            severity=one_of(data, "severity", path, choices=SEVERITIES,
                            required=False, default="material") or "material",
            evidence_refs=tuple(
                EvidenceRef.parse(item, f"{path}.evidence_refs[{i}]")
                for i, item in enumerate(_seq(data, "evidence_refs", path))
            ),
            limitations=text_list(data, "limitations", path, default=(), max_len=500),
        )

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "invariant_id": self.invariant_id, "status": self.status,
            "confidence": self.confidence, "tier": self.tier,
            "severity": self.severity,
        }
        if self.observations:
            out["observations"] = list(self.observations)
        if self.threshold:
            out["threshold"] = dict(self.threshold)
        if self.method:
            out["method"] = self.method
        if self.evidence_refs:
            out["evidence_refs"] = [e.to_dict() for e in self.evidence_refs]
        if self.limitations:
            out["limitations"] = list(self.limitations)
        return out

    @property
    def blocking(self) -> bool:
        return self.status == "violated" and self.severity == "blocking"


@dataclass(frozen=True)
class Coverage:
    """How much of the comparison could actually be made. Section 17.

    Rule 5: this is not confidence. `structural: 1.0` with a `perceptual` tier
    says every region was looked at by a method that cannot be certain about
    any of them. `semantic: 0.2` with `tier="parser"` says the opposite. A
    reader who is handed one number instead of these two cannot tell the two
    situations apart, and they call for opposite next actions.

    `source_readable` and `target_readable` are first-class rather than a
    dimension because a delta where one end could not be read at all is
    `inconclusive` no matter what the ratios say, and `verdict.assess()` reads
    exactly these two fields to decide it.
    """

    source_readable: bool = False
    target_readable: bool = False
    dimensions: Mapping[str, float] = None  # type: ignore[assignment]
    regions_analyzed: Tuple[str, ...] = ()
    excluded: Tuple[str, ...] = ()
    notes: Tuple[str, ...] = ()

    _KEYS = ("source_readable", "target_readable", "dimensions",
             "regions_analyzed", "excluded", "notes")

    def __post_init__(self) -> None:
        if self.dimensions is None:
            object.__setattr__(self, "dimensions", {})

    @classmethod
    def parse(cls, raw: Any, path: str = "coverage") -> "Coverage":
        data = as_mapping(raw, path) if raw is not None else {}
        reject_unknown(data, cls._KEYS, path)
        raw_dims = data.get("dimensions") or {}
        if not isinstance(raw_dims, Mapping):
            raise DeltaError(f"{path}.dimensions", "expected an object", got=raw_dims)
        dims: Dict[str, float] = {}
        for key in raw_dims:
            name = str(key)
            if name not in COVERAGE_DIMENSIONS:
                raise DeltaError(
                    f"{path}.dimensions.{name}",
                    f"is not a known coverage dimension; two adapters inventing "
                    f"their own names make the numbers incomparable. Known: "
                    f"{list(COVERAGE_DIMENSIONS)}",
                    got=name,
                )
            dims[name] = _ratio(raw_dims, name, f"{path}.dimensions")
        return cls(
            source_readable=flag(data, "source_readable", path, default=False),
            target_readable=flag(data, "target_readable", path, default=False),
            dimensions=dims,
            regions_analyzed=text_list(data, "regions_analyzed", path, default=(),
                                       max_len=512, max_items=1024),
            excluded=text_list(data, "excluded", path, default=(), max_len=512),
            notes=text_list(data, "notes", path, default=(), max_len=500),
        )

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "source_readable": self.source_readable,
            "target_readable": self.target_readable,
            "dimensions": dict(self.dimensions),
        }
        for key in ("regions_analyzed", "excluded", "notes"):
            value = getattr(self, key)
            if value:
                out[key] = list(value)
        return out

    @property
    def both_readable(self) -> bool:
        return bool(self.source_readable and self.target_readable)

    def ratio(self, dimension: str) -> Optional[float]:
        """The ratio for one dimension, or `None` when it was not reported.

        `None` and `0.0` are different answers -- "we did not measure this axis"
        against "we measured it and covered none of it" -- and a default of
        zero would turn the first into the second on every read.
        """
        value = self.dimensions.get(str(dimension))
        return None if value is None else float(value)


# -- the result ------------------------------------------------------------


@dataclass(frozen=True)
class UniversalDelta:
    """The canonical answer. Section 5.4.

    `assessment`, not `verdict`: rule 6. The plan allows the internal object to
    keep `verdict` for compatibility and this implementation declines, because
    the compatibility being preserved would be with a word that already means
    something else two modules away, and `partial` is in both vocabularies.

    The summaries (`requested_summary`, `incidental_summary`, `regressions`,
    `unknowns`) are computed from `assertions` and `invariants` rather than
    stored, so that they cannot drift from the rows they summarise. A stored
    summary is a second source of truth about the same facts, and the day they
    disagree the reader believes the shorter one.
    """

    id: str
    request_id: str
    owner: str
    domain: str
    source: RevisionRef
    target: RevisionRef
    assessment: str
    intent_contract_id: str = ""
    intent_fingerprint: str = ""
    assertions: Tuple[DeltaAssertion, ...] = ()
    invariants: Tuple[InvariantResult, ...] = ()
    coverage: Coverage = None  # type: ignore[assignment]
    evidence_refs: Tuple[EvidenceRef, ...] = ()
    extractor_versions: Mapping[str, Any] = None  # type: ignore[assignment]
    limitations: Tuple[str, ...] = ()
    project_id: str = ""
    session_id: str = ""
    run_id: str = ""
    correlation_id: str = ""
    proof_ref: str = ""
    elapsed_ms: int = 0
    created_at: str = ""
    schema_version: int = SCHEMA_VERSION

    _KEYS = ("id", "request_id", "owner", "domain", "source", "target",
             "assessment", "intent_contract_id", "intent_fingerprint",
             "assertions", "invariants", "coverage", "evidence_refs",
             "extractor_versions", "limitations", "project_id", "session_id",
             "run_id", "correlation_id", "proof_ref", "elapsed_ms",
             "created_at", "schema_version")

    def __post_init__(self) -> None:
        if self.coverage is None:
            object.__setattr__(self, "coverage", Coverage())
        if self.extractor_versions is None:
            object.__setattr__(self, "extractor_versions", {})

    @classmethod
    def parse(cls, raw: Any, path: str = "delta") -> "UniversalDelta":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        assertions = tuple(
            DeltaAssertion.parse(item, f"{path}.assertions[{i}]")
            for i, item in enumerate(_seq(data, "assertions", path))
        )
        invariants = tuple(
            InvariantResult.parse(item, f"{path}.invariants[{i}]")
            for i, item in enumerate(_seq(data, "invariants", path))
        )
        return cls(
            id=text(data, "id", path, required=False, max_len=120) or new_id("delta"),
            request_id=text(data, "request_id", path, required=False, max_len=120),
            owner=text(data, "owner", path, max_len=200),
            domain=one_of(data, "domain", path, choices=DOMAINS) or "",
            source=RevisionRef.parse(data.get("source"), f"{path}.source"),
            target=RevisionRef.parse(data.get("target"), f"{path}.target"),
            assessment=one_of(data, "assessment", path, choices=ASSESSMENTS) or "",
            intent_contract_id=text(data, "intent_contract_id", path, required=False,
                                    max_len=120),
            intent_fingerprint=text(data, "intent_fingerprint", path, required=False,
                                    max_len=120),
            assertions=assertions,
            invariants=invariants,
            coverage=Coverage.parse(data.get("coverage"), f"{path}.coverage"),
            evidence_refs=tuple(
                EvidenceRef.parse(item, f"{path}.evidence_refs[{i}]")
                for i, item in enumerate(_seq(data, "evidence_refs", path))
            ),
            extractor_versions=_mapping(data, "extractor_versions", path),
            limitations=text_list(data, "limitations", path, default=(), max_len=500),
            project_id=text(data, "project_id", path, required=False, max_len=200),
            session_id=text(data, "session_id", path, required=False, max_len=200),
            run_id=text(data, "run_id", path, required=False, max_len=200),
            correlation_id=text(data, "correlation_id", path, required=False, max_len=200),
            proof_ref=text(data, "proof_ref", path, required=False, max_len=200),
            elapsed_ms=whole(data, "elapsed_ms", path, default=0, minimum=0) or 0,
            created_at=timestamp(data, "created_at", path) or now_iso(),
            schema_version=whole(data, "schema_version", path, default=SCHEMA_VERSION,
                                 minimum=1) or SCHEMA_VERSION,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "request_id": self.request_id, "owner": self.owner,
            "domain": self.domain,
            "source": self.source.to_dict(), "target": self.target.to_dict(),
            "assessment": self.assessment,
            "intent_contract_id": self.intent_contract_id,
            "intent_fingerprint": self.intent_fingerprint,
            "assertions": [a.to_dict() for a in self.assertions],
            "invariants": [i.to_dict() for i in self.invariants],
            "coverage": self.coverage.to_dict(),
            "evidence_refs": [e.to_dict() for e in self.evidence_refs],
            "extractor_versions": dict(self.extractor_versions),
            "limitations": list(self.limitations),
            "project_id": self.project_id, "session_id": self.session_id,
            "run_id": self.run_id, "correlation_id": self.correlation_id,
            "proof_ref": self.proof_ref, "elapsed_ms": self.elapsed_ms,
            "created_at": self.created_at, "schema_version": self.schema_version,
        }

    # -- derived views, never stored ---------------------------------------

    def by_classification(self, name: str) -> Tuple[DeltaAssertion, ...]:
        return tuple(a for a in self.assertions if a.classification == name)

    def regressions(self) -> Tuple[DeltaAssertion, ...]:
        return self.by_classification("regression")

    def incidental(self) -> Tuple[DeltaAssertion, ...]:
        return self.by_classification("incidental")

    def unknowns(self) -> Tuple[DeltaAssertion, ...]:
        return self.by_classification("unknown")

    def violated(self) -> Tuple[InvariantResult, ...]:
        return tuple(i for i in self.invariants if i.status == "violated")

    def material_assertions(self) -> Tuple[DeltaAssertion, ...]:
        """What a caller must have seen. §12's "no summarise away" rule.

        Sorted strongest-first so that a truncated render loses the least
        important rows. A delta with three hundred assertions gets read down to
        the first ten by every consumer, and which ten those are is a decision
        this method makes rather than leaving to insertion order.
        """
        return tuple(sorted(
            (a for a in self.assertions if a.material),
            key=lambda a: (-severity_rank(a.severity), confidence_rank(a.confidence), a.path),
        ))

    def summary(self) -> Dict[str, Any]:
        """Counts, for a card. Cheap enough to compute on every read."""
        counts = {name: 0 for name in CLASSIFICATIONS}
        for assertion in self.assertions:
            counts[assertion.classification] = counts.get(assertion.classification, 0) + 1
        invariant_counts = {name: 0 for name in INVARIANT_STATUSES}
        for result in self.invariants:
            invariant_counts[result.status] = invariant_counts.get(result.status, 0) + 1
        return {
            "assertions": len(self.assertions),
            "classifications": counts,
            "invariants": invariant_counts,
            "material": len(self.material_assertions()),
            "severity": strongest_severity([a.severity for a in self.assertions]),
            "confidence": weakest_confidence(
                [a.confidence for a in self.assertions if a.changed] or ["unknown"]
            ),
        }

    def fingerprint(self) -> str:
        """What question this delta answered: source, target, intent, domain.

        The §22 cache key is written as `source + target + intent + profile +
        extractor_versions`, and this leaves the extractor versions OUT. That is
        a deliberate change and it is worth the paragraph.

        A key containing the extractor versions can only be computed AFTER the
        extraction, which is the expensive half -- so it would dedupe storage
        and save no work at all. Worse, a parser upgrade would produce a silent
        MISS: a second row appears, the first stays live, and nothing anywhere
        says why the same comparison was answered twice.

        Identifying a delta by the QUESTION instead means the second answer
        collides with the first, and the service compares the stored
        `extractor_versions` against the current ones and supersedes on
        purpose, with a reason a reader can see. Invalidation becomes an event
        rather than the absence of one. `evaluation_profile` is inside
        `intent_fingerprint` already, so it is still in the key.

        `id`, `created_at` and `elapsed_ms` are excluded so that recomputing
        the same comparison recognises itself.
        """
        return fingerprint([
            ("source", self.source.identity()),
            ("target", self.target.identity()),
            ("intent", self.intent_fingerprint),
            ("domain", self.domain),
        ])

    def ref(self) -> "UniversalDeltaRef":
        return UniversalDeltaRef(
            delta_id=self.id,
            domain=self.domain,
            source_revision=self.source.identity(),
            target_revision=self.target.identity(),
            intent_contract_id=self.intent_contract_id,
            assessment=self.assessment,
            material_assertion_ids=tuple(a.id for a in self.material_assertions()),
            evidence_refs=tuple(e.ref for e in self.evidence_refs),
            coverage=self.coverage.to_dict(),
            created_at=self.created_at,
        )


@dataclass(frozen=True)
class UniversalDeltaRef:
    """The portable form. Section 1.3.

    What crosses a subsystem boundary is this, never the whole delta: Context
    Engine stores a ref and a summary and opens the detail on demand (§20), the
    council debates assertion ids, and Branching compares refs. The rule that
    makes it work is that everything here is derivable from the delta and
    nothing here is authoritative on its own.
    """

    delta_id: str
    domain: str
    source_revision: str
    target_revision: str
    assessment: str
    intent_contract_id: str = ""
    material_assertion_ids: Tuple[str, ...] = ()
    evidence_refs: Tuple[str, ...] = ()
    coverage: Mapping[str, Any] = None  # type: ignore[assignment]
    created_at: str = ""

    _KEYS = ("delta_id", "domain", "source_revision", "target_revision",
             "assessment", "intent_contract_id", "material_assertion_ids",
             "evidence_refs", "coverage", "created_at")

    def __post_init__(self) -> None:
        if self.coverage is None:
            object.__setattr__(self, "coverage", {})

    @classmethod
    def parse(cls, raw: Any, path: str = "delta_ref") -> "UniversalDeltaRef":
        data = as_mapping(raw, path)
        reject_unknown(data, cls._KEYS, path)
        return cls(
            delta_id=text(data, "delta_id", path, max_len=120),
            domain=one_of(data, "domain", path, choices=DOMAINS) or "",
            source_revision=text(data, "source_revision", path, max_len=200),
            target_revision=text(data, "target_revision", path, max_len=200),
            assessment=one_of(data, "assessment", path, choices=ASSESSMENTS) or "",
            intent_contract_id=text(data, "intent_contract_id", path, required=False,
                                    max_len=120),
            material_assertion_ids=text_list(data, "material_assertion_ids", path,
                                             default=(), max_len=120, max_items=1024),
            evidence_refs=text_list(data, "evidence_refs", path, default=(),
                                    max_len=1024, max_items=1024),
            coverage=_mapping(data, "coverage", path),
            created_at=timestamp(data, "created_at", path) or "",
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "delta_id": self.delta_id, "domain": self.domain,
            "source_revision": self.source_revision,
            "target_revision": self.target_revision,
            "assessment": self.assessment,
            "intent_contract_id": self.intent_contract_id,
            "material_assertion_ids": list(self.material_assertion_ids),
            "evidence_refs": list(self.evidence_refs),
            "coverage": dict(self.coverage),
            **({"created_at": self.created_at} if self.created_at else {}),
        }

    def source_ref(self) -> str:
        """How Context Engine addresses this. Matches `handles = ("delta:",)`."""
        return f"delta:{self.delta_id}"
