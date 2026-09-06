"""The honest floor: what can be said about two byte streams and nothing more.

`DOMAINS` in `contracts.py` carries `binary` so that "all we could compare was
the hash" is a domain with a name rather than a real adapter quietly degrading
into one. This module is that domain, and its value is entirely in what it
refuses to claim.

What it compares: size, media type, the sha256 of the whole stream and -- when
the bytes decode as text and fit the budget -- the line count and a digest per
block of lines. Nothing else. It does not parse, it does not sniff, it does not
guess a format from a magic number.

Three rules, each with the failure it prevents:

* **`preserved` requires an exact hash equality and nothing weaker.** Identical
  bytes preserve every property of those bytes, which is the one place this
  adapter is authoritative. A differing hash proves that something changed and
  proves NOTHING about which property survived, so every invariant comes back
  `unknown` -- never `violated`, which would be a claim about a structure this
  adapter never read, and never `preserved`, which is rule 1 of `contracts.py`.

* **A `modified` says out loud that it does not know what changed inside.**
  The confidence is `exact` and the tier is `hash`, because two digests either
  agree or they do not; the LIMITATION is that "the bytes differ" is the entire
  content of the finding. Without that sentence a reader takes an `exact`
  finding for a complete one.

* **Semantic coverage is 0.0, declared, on every comparison.** Not omitted --
  `Coverage.ratio` distinguishes "not measured" from "measured and covered
  none", and this adapter DID decide not to read meaning. That zero is what
  makes `coverage.sufficient(..., dimensions=("semantic",))` refuse to let a
  conclusion about content rest on a byte comparison, which is the whole reason
  the binary floor is a named domain instead of a fallback.

`registry.py` finds this module by the module-level `ADAPTER_FACTORY` at the
bottom, and there is no list anywhere to add it to -- which is why it cannot be
simultaneously finished and invisible.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional, Tuple

from src.delta_engine import confidence as confidence_mod
from src.delta_engine import coverage as coverage_mod
from src.delta_engine import sources
from src.delta_engine.adapters.base import (
    Element,
    Extraction,
    Finding,
    Scope,
    Snapshot,
    unreadable,
)
from src.delta_engine.contracts import (
    Coverage,
    IntentContract,
    InvariantResult,
    RevisionRef,
)

__all__ = ["GenericAdapter", "ADAPTER_FACTORY"]

#: How many lines go into one block digest. Blocks exist so that "the file
#: changed" can become "the file changed around here" without parsing anything;
#: 64 is small enough to localise an edit and large enough that a thousand-line
#: file is sixteen elements rather than a thousand.
BLOCK_LINES = 64

#: The keys this adapter always emits, in the order a reader wants them. Named
#: so `compare` and `check_invariants` agree on the one that carries identity:
#: a second spelling of `"content"` in either of them would compare the digest
#: against nothing and report `added`.
CONTENT_KEY = "content"
SIZE_KEY = "size"
MEDIA_TYPE_KEY = "media_type"
LINES_KEY = "lines"

#: How many element keys a coverage may list. `Coverage.regions_analyzed`
#: accepts 1024; the cut is made below it and REPORTED, because a list silently
#: clipped at the contract's ceiling would read as the complete set of regions
#: that were examined.
MAX_LISTED_REGIONS = 512


def _digest(text: str) -> str:
    """sha256 of a block of text, as UTF-8. One spelling, used by both sides."""
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _as_text(resolved: sources.Resolved) -> Optional[str]:
    """The payload as text, or `None` when it is not text.

    Strict UTF-8 and a NUL check, and no third heuristic. `errors="replace"`
    would turn any binary file into text and produce a line count and block
    digests for a JPEG -- numbers that look like structure and describe none.
    A NUL byte is legal UTF-8 and is what separates a file that happens to
    decode from one anybody would call text.
    """
    try:
        raw = resolved.data()
    except sources.SourceError:
        return None
    if b"\x00" in raw:
        return None
    try:
        return raw.decode("utf-8", "strict")
    except UnicodeDecodeError:
        return None


def _unique(values: List[str]) -> Tuple[str, ...]:
    """Order-preserving de-duplication of a list assembled HERE.

    Same rule as `coverage._unique`: the repeat is an artifact of joining two
    snapshots' notes, and both ends cut at the same `max_bytes` produce the same
    sentence twice. It is removed here, where it was created; a duplicate inside
    one snapshot's own list is still that adapter's contradiction and is still
    refused by `text_list` on the way into `Coverage`.
    """
    seen = set()
    out: List[str] = []
    for value in values:
        text = str(value)
        if text in seen:
            continue
        seen.add(text)
        out.append(text)
    return tuple(out)


def _signature(element: Element) -> str:
    """What makes two elements the same element. Hash first, then value.

    Hash first because a content digest is identity and a rendered value is a
    description; an element carrying both must be compared on the one that
    cannot collide.
    """
    return element.hash or element.value


def _content_hash(snapshot: Snapshot) -> str:
    """The whole-stream digest this snapshot recorded, or `""`.

    `""` for an unreadable end, and `check_invariants` treats that as "not
    identical" rather than as an empty file matching another empty file -- two
    ends nobody could read are not two ends that agree.
    """
    element = snapshot.index().get(CONTENT_KEY)
    return element.hash if element is not None else ""


def _read_ratio(snapshot: Snapshot) -> float:
    """How much of this end was actually read, 0..1.

    Read from the snapshot's own detail rather than recomputed, so that the
    number in the coverage is the number the resolver reported. A snapshot with
    no size (an empty file) counts as fully read: nothing was left out of it.
    """
    detail = dict(snapshot.detail or {})
    size = int(detail.get("size") or 0)
    read = int(detail.get("read_bytes") or 0)
    if size <= 0:
        return 1.0
    return max(0.0, min(1.0, round(read / size, 4)))


class GenericAdapter:
    """The `binary` domain. Implements the `DeltaAdapter` Protocol.

    Deliberately NOT a fallback. `registry.adapter_for` refuses a domain whose
    own adapter is missing rather than handing the request here, because this
    adapter would answer `modified` about a rewritten Python file with `exact`
    confidence and the answer would look exactly like a real one. What it is
    for is the floor everything else is measured against: whatever a real
    adapter claims, it should at least be able to claim this.
    """

    domain = "binary"
    version = "1"

    def available(self) -> bool:
        """Always true. This adapter needs nothing that could be missing.

        `hashlib` and a byte string. There is no parser to install and no
        server to reach, so a `False` here could only ever be a lie -- and an
        adapter that probes for something it does not use is an adapter that
        goes unavailable for a reason nobody can act on.
        """
        return True

    def snapshot(self, revision: RevisionRef, *, scope: Scope) -> Snapshot:
        """One end, as bytes plus whatever a byte reader can address in them.

        An unreadable revision returns `unreadable(...)` with the resolver's own
        reason rather than raising: "the checkpoint is gone" is an ANSWER, it
        makes the delta `inconclusive`, and raising would lose the other end's
        work and say nothing about which end failed.

        The element budget (`budget.max_elements`) cuts the block list and sets
        `truncated` with a note. The cut lands on blocks and never on `content`,
        `size` or `media_type`: those three are what every later step reads, and
        a budget that removed the content digest would make an over-large file
        indistinguishable from an unreadable one.
        """
        resolved = sources.resolve(revision, scope=scope)
        if not resolved.readable:
            return unreadable(revision, resolved.reason, tier="hash")

        detail = resolved.to_dict()
        notes: List[str] = []
        excluded: List[str] = []
        if resolved.truncated:
            excluded.append(resolved.reason)

        elements: List[Element] = [
            Element(key=SIZE_KEY, kind="metadata", value=str(resolved.size)),
            Element(key=MEDIA_TYPE_KEY, kind="metadata", value=resolved.media_type),
            Element(key=CONTENT_KEY, kind="bytes", hash=resolved.sha256()),
        ]

        text = _as_text(resolved)
        if text is None:
            notes.append("the payload does not decode as UTF-8 text, so no "
                         "line count and no per-block digests were taken")
        else:
            lines = text.splitlines()
            elements.append(Element(key=LINES_KEY, kind="metadata",
                                    value=str(len(lines))))
            blocks, cut = self._blocks(lines, scope=scope,
                                       reserved=len(elements))
            elements.extend(blocks)
            if cut:
                excluded.append(cut)

        truncated = bool(resolved.truncated or excluded)
        return Snapshot(
            revision=revision,
            elements=tuple(elements),
            readable=True,
            tier="hash",
            excluded=tuple(excluded),
            notes=tuple(notes),
            truncated=truncated,
            detail=detail,
        )

    def _blocks(self, lines: List[str], *, scope: Scope,
                reserved: int) -> Tuple[Tuple[Element, ...], str]:
        """Per-block digests, and the sentence describing the cut, if any.

        `reserved` is the number of elements already spent on metadata, so the
        budget bounds the SNAPSHOT and not just this list -- a budget counted
        only over blocks would be exceeded by exactly the elements everything
        else depends on.
        """
        limit = scope.budget.max_elements
        total = (len(lines) + BLOCK_LINES - 1) // BLOCK_LINES
        allowed = total
        if limit is not None:
            allowed = max(0, min(total, limit - reserved))
        out: List[Element] = []
        for index in range(allowed):
            start = index * BLOCK_LINES
            chunk = lines[start:start + BLOCK_LINES]
            out.append(Element(
                key=f"block:{index:05d}",
                kind="text_block",
                hash=_digest("\n".join(chunk)),
                value=f"lines {start + 1}-{start + len(chunk)}",
            ))
        if allowed >= total:
            return tuple(out), ""
        first_missing = allowed * BLOCK_LINES + 1
        return tuple(out), (
            f"blocks from line {first_missing} onwards were not hashed; "
            f"`budget.max_elements` ({limit}) stopped the snapshot and nothing "
            f"past that line was compared")


    def compare(self, source: Snapshot, target: Snapshot, *,
                scope: Scope) -> Extraction:
        """What differs between two byte streams, and what that does not say.

        Every finding is `exact` at tier `hash`, because a digest comparison
        has no margin -- and every finding that reports a difference carries the
        limitation that this adapter cannot say what changed inside. Those two
        sentences are not in tension: the OBSERVATION is certain and its CONTENT
        is thin, which is exactly the distinction rule 5 of `contracts.py` draws
        between confidence and coverage.

        An unreadable end produces no findings at all rather than a `missing`
        for every element: "we could not open it" and "it was emptied" are
        opposite facts, and the second is what a list of removals would say.
        `verdict.assess` reads `coverage.both_readable` first and answers
        `inconclusive`, which is why the coverage is built either way.
        """
        readable = source.readable and target.readable
        limitations = [
            "this adapter compares bytes: size, media type, whole-stream "
            "digest and per-block digests for text. It reads no structure, so "
            "nothing here is evidence about meaning",
        ]
        if not readable:
            which = "source" if not source.readable else "target"
            limitations.append(
                f"the {which} revision could not be read, so nothing was "
                f"compared; this is not a report that nothing changed")
            return Extraction(
                findings=(),
                coverage=self._coverage(source, target, regions=(),
                                        readable=False),
                invariants=(),
                extractor_versions={self.domain: self.version},
                limitations=tuple(limitations),
            )

        left, right = source.index(), target.index()
        findings: List[Finding] = []
        for key in sorted(set(left) | set(right)):
            before, after = left.get(key), right.get(key)
            finding = self._finding(key, before, after)
            if finding is not None:
                findings.append(finding)

        return Extraction(
            findings=tuple(findings),
            coverage=self._coverage(source, target,
                                    regions=tuple(sorted(set(left) | set(right))),
                                    readable=True),
            invariants=(),
            extractor_versions={self.domain: self.version},
            limitations=tuple(limitations),
        )

    def _finding(self, key: str, before: Optional[Element],
                 after: Optional[Element]) -> Optional[Finding]:
        """One observation about one element key. Never a classification.

        `unchanged` is emitted rather than skipped: it is a real observation and
        says we looked, which is a different statement from the absence of a
        row. `classification.classify_all` is what decides whether any of this
        was requested; this method has never seen the intent and cannot.
        """
        sample = before if before is not None else after
        if sample is None:  # pragma: no cover - the keys come from a union
            return None
        # A hash is identity and a rendered field is a declaration, and the two
        # sit on different rungs of the determinism ladder. Reading the tier out
        # of `confidence.METHOD_TIERS` rather than writing `"hash"` here keeps
        # one table authoritative: an adapter that names its own tier is an
        # adapter that can promote itself.
        method = "content_hash" if sample.hash else "manifest_field"
        tier = confidence_mod.tier_of(method)
        common: Dict[str, Any] = {
            "path": key, "method": method, "tier": tier, "confidence": "exact",
        }
        if before is None and after is not None:
            return Finding(operation="added", after=_signature(after),
                           element_kind=after.kind, **common)
        if after is None and before is not None:
            return Finding(operation="missing", before=_signature(before),
                           element_kind=before.kind, **common)
        if before is None or after is None:  # pragma: no cover - see above
            return None
        if _signature(before) == _signature(after):
            return Finding(operation="unchanged", before=_signature(before),
                           after=_signature(after), element_kind=before.kind,
                           **common)
        return Finding(
            operation="modified",
            before=_signature(before),
            after=_signature(after),
            element_kind=before.kind,
            limitations=(
                "the two digests differ, which is certain; WHAT changed inside "
                "them is not something a byte comparison can say",
            ),
            **common,
        )


    def _coverage(self, source: Snapshot, target: Snapshot, *,
                  regions: Tuple[str, ...], readable: bool) -> Coverage:
        """How much was compared, on the two axes this adapter can speak to.

        `structural` is high because every addressable thing this adapter has
        -- the stream, its size, its media type, each block -- was compared;
        when a budget cut one end it is the fraction of bytes actually read, so
        the number describes the comparison and not the intention.

        `semantic` is 0.0 and is DECLARED rather than omitted. `Coverage.ratio`
        returns `None` for a dimension nobody measured and `0.0` for one
        measured as empty, and the difference matters here: this adapter did not
        fail to read meaning, it does not read meaning, and the zero is what
        makes `coverage.sufficient(..., dimensions=("semantic",))` refuse to
        rest a conclusion about content on a hash.
        """
        structural = 0.0
        if readable:
            structural = min(_read_ratio(source), _read_ratio(target))
        listed = regions[:MAX_LISTED_REGIONS]
        notes = [
            "no semantic reading was performed: this adapter compares digests, "
            "sizes and media types, and cannot say what any difference means",
        ]
        if len(regions) > len(listed):
            notes.append(f"{len(regions) - len(listed)} further element keys "
                         f"were compared and are not listed here")
        return coverage_mod.build(
            source_readable=source.readable,
            target_readable=target.readable,
            dimensions={"structural": structural, "semantic": 0.0},
            regions=listed,
            excluded=_unique(list(source.excluded) + list(target.excluded)),
            notes=_unique(notes + list(source.notes) + list(target.notes)),
        )

    def check_invariants(self, intent: IntentContract, source: Snapshot,
                         target: Snapshot, *,
                         scope: Scope) -> Tuple[InvariantResult, ...]:
        """One result per declared invariant, and only one way to say `preserved`.

        Identical, untruncated digests on both ends is the single case where
        this adapter is authoritative about everything: bytes that are the same
        bytes preserve every property those bytes have, and that claim is
        `exact` at tier `hash` because it is an identity.

        Every other case is `unknown`. Not `violated` -- a differing digest
        proves something changed and nothing about WHICH property broke, and a
        `violated` here would put a blocking row on a security invariant this
        adapter never looked at. Not `preserved` -- that is rule 1, and the
        whole subsystem exists to refuse it.

        Truncation is checked as carefully as readability, and for the same
        reason: two digests over the first eight megabytes of two files that
        agree there are not two identical files, and treating them as such would
        be the most expensive `preserved` this module could emit.
        """
        left, right = _content_hash(source), _content_hash(target)
        identical = bool(
            source.readable and target.readable
            and not source.truncated and not target.truncated
            and left and left == right
        )
        observations: List[str] = []
        if left:
            observations.append(f"source sha256 {left}")
        if right:
            observations.append(f"target sha256 {right}")

        results: List[InvariantResult] = []
        for invariant in intent.invariants:
            payload: Dict[str, Any] = {
                "invariant_id": invariant.id,
                "method": "content_hash",
                "tier": "hash",
                "severity": invariant.severity,
            }
            if identical:
                payload.update({
                    "status": "preserved",
                    "confidence": "exact",
                    "observations": [
                        f"both revisions are sha256 {left}, byte for byte",
                        "identical bytes preserve every property of those bytes",
                    ],
                })
            else:
                payload.update({
                    "status": "unknown",
                    "confidence": "unknown",
                    "observations": observations,
                    "limitations": [self._why_unknown(source, target, invariant.klass)],
                })
            results.append(InvariantResult.parse(
                payload, f"invariant_result[{invariant.id}]"))
        return tuple(results)

    def _why_unknown(self, source: Snapshot, target: Snapshot, klass: str) -> str:
        """The concrete reason this adapter cannot answer about one invariant.

        Three different reasons, kept apart because they call for three
        different actions: read the missing end, raise the budget, or route the
        comparison to an adapter that understands the domain.
        """
        if not source.readable or not target.readable:
            which = "source" if not source.readable else "target"
            return (f"the {which} revision could not be read, so no property "
                    f"of it was observed")
        if source.truncated or target.truncated:
            return ("only part of at least one revision was read, so a digest "
                    "that agrees says nothing about the part nobody compared")
        return (f"the two revisions differ by hash and this adapter reads no "
                f"structure, so it cannot say whether a `{klass}` property "
                f"survived; not detected is not preserved")


#: How `registry.py` finds this adapter. A module-level factory and NOT an
#: entry in a tuple somewhere else: `state_mirror/adapters/__init__.py` records
#: what the tuple cost -- five correct adapters that did not exist as far as the
#: running system was concerned, with green tests and no warning, because nobody
#: added them to it. Writing this line IS the registration.
ADAPTER_FACTORY = GenericAdapter
