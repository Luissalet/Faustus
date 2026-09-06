"""Which source element is which target element. §8, deterministic first.

Before anything can be said about what CHANGED, something has to decide what
corresponds to what, and every wrong answer here is invisible downstream: an
element paired with the wrong partner produces a confident assertion about a
path that never held that value. So this module runs the ladder of §3.2 in
order and stops as soon as a step decides, and every step that could not decide
says `uncertain` rather than picking:

1. same `key` -> `same`. An identical address is identity, not a guess.
2. different key, same non-empty `hash` -> `moved`, `exact`, tier `hash`.
   This is the rename, and reporting it as `missing` + `added` is the §28 row
   "rename/move frente a delete+add" -- two assertions about a file that was
   never deleted, one of which reads as data loss.
3. only in source -> `missing`; only in target -> `added`.
4. name resemblance -> `uncertain`, `low`, and never anything better.

The one that costs the most to get right is a hash that appears more than once
on both sides. Three identical elements on the left and three on the right
admit nine pairings and the evidence prefers none of them, so this module emits
`uncertain` for each and pairs nothing. A detector that picked the first
candidate would report a `moved` with confidence `exact` about a correspondence
it invented, and `exact` is the one word no later layer is allowed to overturn.

`max_elements` cuts by the UNION of keys rather than by side, so a key present
on both sides is either kept or dropped as one decision and the cut can never
turn a matched pair into a `missing` plus an `added`. What it can still do is
separate a renamed pair, which is why crossing it sets `truncated` and lowers
`coverage_ratio`: a partial alignment that did not say so is a delta claiming
to have looked at a file it never opened.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

from src.delta_engine.adapters.base import Element, Snapshot
from src.delta_engine.contracts import Alignment, DeltaError

__all__ = ["AlignmentResult", "align", "by_key", "by_hash", "NAME_SIMILARITY_FLOOR"]

#: How alike two addresses have to look before this module will say they might
#: be the same element. High on purpose: the answer is capped at `low` anyway,
#: so a permissive threshold buys nothing except pairs a reader has to reject
#: one at a time. `src/auth.py` and `src/oauth.py` share most of their
#: characters and are not each other.
NAME_SIMILARITY_FLOOR: float = 0.82

#: `(source element, target element, alignment)`. Either end may be `None`, and
#: which one is missing is not the whole story: a pair with no target may be a
#: `missing` OR an `uncertain` this module refused to resolve, and only
#: `alignment.relation` distinguishes them.
Pair = Tuple[Optional[Element], Optional[Element], Alignment]


@dataclass(frozen=True)
class AlignmentResult:
    """What alignment decided, and how much of the two snapshots it saw.

    No `parse()`, unlike the wire contracts: this object is computed and never
    received. A `parse()` would let a caller hand-write an alignment result and
    skip the ladder that produced it, which is precisely the certainty §3.2
    says later layers may complement and never falsify.

    `coverage_ratio` is about ALIGNMENT completeness only -- what fraction of
    the elements in the two snapshots this pass considered. Whether either end
    was readable at all lives in `Coverage`, because they are rule 5's two
    different questions and a single number would hide both.
    """

    pairs: Tuple[Pair, ...] = ()
    truncated: bool = False
    coverage_ratio: float = 1.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pairs": [
                {
                    "source": source.to_dict() if source is not None else None,
                    "target": target.to_dict() if target is not None else None,
                    "alignment": alignment.to_dict(),
                }
                for source, target, alignment in self.pairs
            ],
            "truncated": self.truncated,
            "coverage_ratio": self.coverage_ratio,
        }

    def by_relation(self, relation: str) -> Tuple[Pair, ...]:
        """Every pair with one relation. Used by tests and by the adapters."""
        return tuple(p for p in self.pairs if p[2].relation == relation)


def by_key(source_index: Mapping[str, Element],
           target_index: Mapping[str, Element]) -> Tuple[Tuple[Pair, ...],
                                                         Dict[str, Element],
                                                         Dict[str, Element]]:
    """Step 1: the addresses that match. Returns `(pairs, rest, rest)`.

    Takes the two mappings `Snapshot.index()` produces, and hands back what it
    could not decide so the next step runs on the leftovers only. Returning the
    remainder rather than a set of consumed keys is what makes the ladder
    composable without any step having to know which step ran before it.

    Iteration is over sorted keys because §3.2's first word is "determinista":
    two runs over the same snapshots have to emit the same pairs in the same
    order, or the fingerprint in §22 stops identifying the same comparison.
    """
    pairs: List[Pair] = []
    # Keys and not the elements themselves: `Element` is a frozen dataclass
    # carrying a `detail` mapping, so it is not hashable and a set of elements
    # would raise here rather than anywhere near the field that caused it.
    matched: set = set()
    for key in sorted(source_index):
        target = target_index.get(key)
        if target is None:
            continue
        pairs.append((source_index[key], target,
                      _alignment("same", key, key, "exact", "element_key", "parser")))
        matched.add(key)
    rest_source = {k: v for k, v in source_index.items() if k not in matched}
    rest_target = {k: v for k, v in target_index.items() if k not in matched}
    return tuple(pairs), rest_source, rest_target


def by_hash(source: Mapping[str, Element],
            target: Mapping[str, Element]) -> Tuple[Tuple[Pair, ...],
                                                    Dict[str, Element],
                                                    Dict[str, Element]]:
    """Step 2: the renames. Same content, different address.

    Takes what `by_key` left over rather than a `Snapshot`, which is why it
    cannot use `Snapshot.by_hash()`: that method groups EVERY element, and
    running rename detection over elements whose keys already matched would
    re-pair them across addresses and manufacture moves out of files that never
    moved. The grouping here is the same rule -- empty hashes are skipped and
    never grouped together, because a bucket of hashless elements would look
    like a pile of copies of one another.

    A digest with exactly one element on each side is a rename. Anything else
    is `uncertain` for every element in the bucket, and the two ends are
    reported separately: choosing one of three identical candidates is a
    statement about which one moved, and nothing here measured that.
    """
    source_groups = _group_by_hash(source)
    target_groups = _group_by_hash(target)
    pairs: List[Pair] = []
    consumed_source: set = set()
    consumed_target: set = set()

    for digest in sorted(set(source_groups) & set(target_groups)):
        left = source_groups[digest]
        right = target_groups[digest]
        if len(left) == 1 and len(right) == 1:
            one, other = left[0], right[0]
            # Identical keys reaching here means this was called directly on a
            # full index rather than on `by_key`'s leftovers. Reporting `moved`
            # would invent a rename for an element that never changed address,
            # so the relation follows the evidence instead of the call order.
            relation = "same" if one.key == other.key else "moved"
            pairs.append((one, other,
                          _alignment(relation, one.key, other.key, "exact",
                                     "content_hash", "hash")))
            consumed_source.add(one.key)
            consumed_target.add(other.key)
            continue
        reason = f"content_hash({len(left)} source, {len(right)} target candidates)"
        for element in left:
            pairs.append((element, None,
                          _alignment("uncertain", element.key, "", "low", reason, "hash")))
            consumed_source.add(element.key)
        for element in right:
            pairs.append((None, element,
                          _alignment("uncertain", "", element.key, "low", reason, "hash")))
            consumed_target.add(element.key)

    rest_source = {k: v for k, v in source.items() if k not in consumed_source}
    rest_target = {k: v for k, v in target.items() if k not in consumed_target}
    return tuple(pairs), rest_source, rest_target


def align(source: Snapshot, target: Snapshot, *, max_elements: int = 5000) -> AlignmentResult:
    """Run the whole ladder over two snapshots. The only entry point adapters need.

    The order is the claim: key, then hash, then name, then the leftovers. Each
    step only sees what the one before it could not decide, so a later and
    weaker method can never overturn an earlier and stronger one -- §3.2's
    "las capas posteriores complementan, no falsifican certeza de las primeras",
    written as control flow rather than as advice.
    """
    if isinstance(max_elements, bool) or not isinstance(max_elements, int):
        raise DeltaError("max_elements", "expected a whole number", got=max_elements)
    if max_elements < 1:
        raise DeltaError("max_elements", "must be >= 1; an alignment budget of "
                                         "zero produces a delta that looked at "
                                         "nothing and cannot say so", got=max_elements)

    source_index = source.index()
    target_index = target.index()
    total = len(source.elements) + len(target.elements)

    keys = sorted(set(source_index) | set(target_index))
    truncated = len(keys) > max_elements
    kept = set(keys[:max_elements]) if truncated else set(keys)
    source_index = {k: v for k, v in source_index.items() if k in kept}
    target_index = {k: v for k, v in target_index.items() if k in kept}

    pairs: List[Pair] = []
    matched, rest_source, rest_target = by_key(source_index, target_index)
    pairs.extend(matched)
    moved, rest_source, rest_target = by_hash(rest_source, rest_target)
    pairs.extend(moved)
    guessed, rest_source, rest_target = _by_name(rest_source, rest_target)
    pairs.extend(guessed)

    for key in sorted(rest_source):
        pairs.append((rest_source[key], None,
                      _alignment("missing", key, "", "exact", "element_key", "parser")))
    for key in sorted(rest_target):
        pairs.append((None, rest_target[key],
                      _alignment("added", "", key, "exact", "element_key", "parser")))

    considered = len(source_index) + len(target_index)
    ratio = 1.0 if total == 0 else round(considered / total, 4)
    return AlignmentResult(pairs=tuple(sorted(pairs, key=_pair_order)),
                           truncated=truncated, coverage_ratio=ratio)


def _by_name(source: Mapping[str, Element],
             target: Mapping[str, Element]) -> Tuple[Tuple[Pair, ...],
                                                     Dict[str, Element],
                                                     Dict[str, Element]]:
    """Step 3, the last and weakest: two addresses that look alike.

    Applied to every leftover and not only to the hashless ones. An element
    that still has a hash reached this step precisely BECAUSE its digest
    matched nothing, so the hash has already said what it can and the name is
    the only remaining signal either way -- which is also the rename-plus-edit
    case, where the content genuinely changed and the address barely did.

    A resemblance only produces a pair when one candidate is clearly ahead of
    every other. Two candidates within a hair of each other are the ambiguity
    `by_hash` refuses for identical digests, and it is refused here for the
    same reason. The result is `uncertain` and `low` and can never be more,
    because a shared prefix is not evidence about content.
    """
    pairs: List[Pair] = []
    consumed_source: set = set()
    consumed_target: set = set()

    for key in sorted(source):
        candidates = sorted(
            ((difflib.SequenceMatcher(None, key, other).ratio(), other)
             for other in target if other not in consumed_target),
            key=lambda item: (-item[0], item[1]),
        )
        if not candidates:
            break
        score, best = candidates[0]
        if score < NAME_SIMILARITY_FLOOR:
            continue
        runner_up = candidates[1][0] if len(candidates) > 1 else 0.0
        if runner_up >= score:
            continue
        pairs.append((source[key], target[best],
                      _alignment("uncertain", key, best, "low", "name_similarity",
                                 "algorithm")))
        consumed_source.add(key)
        consumed_target.add(best)

    rest_source = {k: v for k, v in source.items() if k not in consumed_source}
    rest_target = {k: v for k, v in target.items() if k not in consumed_target}
    return tuple(pairs), rest_source, rest_target


def _group_by_hash(index: Mapping[str, Element]) -> Dict[str, Tuple[Element, ...]]:
    """`hash -> elements`, skipping the hashless. Sorted, for determinism.

    The same rule as `Snapshot.by_hash`, restated over a leftover mapping:
    grouping every hashless element under `""` would make them all look like
    copies of each other and a rename detector built on that would pair
    unrelated files and call it `exact`.
    """
    out: Dict[str, List[Element]] = {}
    for key in sorted(index):
        element = index[key]
        if not element.hash:
            continue
        out.setdefault(element.hash, []).append(element)
    return {digest: tuple(items) for digest, items in out.items()}


def _alignment(relation: str, source_element: str, target_element: str,
               confidence: str, method: str, tier: str) -> Alignment:
    """Build one `Alignment` THROUGH its own parser.

    Never the dataclass constructor. `Alignment.parse` is where an `uncertain`
    relation has its confidence pulled down to `low` and where a `same` or
    `moved` missing one of its two ends is refused, and a module that built the
    object directly would be a second door into the contract with none of the
    rules behind it.
    """
    payload: Dict[str, Any] = {
        "relation": relation, "confidence": confidence,
        "method": method, "tier": tier,
    }
    if source_element:
        payload["source_element"] = source_element
    if target_element:
        payload["target_element"] = target_element
    return Alignment.parse(payload, "alignment")


def _pair_order(pair: Pair) -> Tuple[str, str]:
    """Stable output order: by source address, then target address.

    Sorting the result rather than trusting the order the steps happened to
    produce, so that the pairs of two identical runs compare equal and a
    reviewer diffing two alignments sees only what actually differed.
    """
    source, target, _ = pair
    return (source.key if source is not None else "",
            target.key if target is not None else "")
