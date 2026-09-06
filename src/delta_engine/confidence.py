"""How much to believe one comparison, and what weakens it on the way out.

Rule 5 of `contracts.py` says coverage is not confidence. This module owns the
other half of that sentence: the arithmetic of confidence alone. It reads a
coverage ratio only in the direction that can LOWER an answer, never raise one,
because the failure §3.5 names is a pipeline that folds a hash comparison and a
model's opinion into one percentage and reports a number nobody measured.

Three rules, each with the failure it prevents:

* **Combining is `min` over the ladder, never an average.**
  `contracts.weakest_confidence` already implements it. `combine` exists so the
  operation has a name a caller reaches for, instead of an adapter writing
  `sum(...)/len(...)` and turning `exact` and `unknown` into `medium`.

* **A tier is a ceiling, and `tier_of` may only answer with a real tier.**
  `Finding.tier` and `DeltaAssertion.parse` validate against `EXTRACTION_TIERS`
  with `one_of`, so a helpful-looking answer such as `"heuristic"` would be a
  hard rejection two frames later and would read as a bug in the contract. An
  unregistered method therefore gets `model` -- the weakest CEILING among the
  real tiers (`medium`), and the one that cannot launder a guess into identity.

* **Everything that could weaken an answer is applied in one place.**
  `propagate` is that place. §8: "una alineación dudosa propaga incertidumbre a
  las afirmaciones dependientes" -- so an assertion built on an uncertain
  alignment cannot claim `exact` however good its own extractor was.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from src.delta_engine.contracts import (
    EXTRACTION_TIERS,
    DeltaError,
    weakest_confidence,
)

__all__ = ["METHOD_TIERS", "tier_of", "combine", "propagate", "describe",
           "alignment_signal"]


#: Method name -> tier, for the methods this repo's adapters name out loud.
#: The table is a claim about DETERMINISM (§3.2), not about quality: `delta_e`
#: is an excellent measurement of colour distance and still sits at
#: `perceptual`, because a colour metric answers a question about appearance
#: and the contract's `exact` means identity. The dictionary is open to
#: extension and deliberately NOT exhaustive -- `tier_of` has a defined answer
#: for everything absent from it, so a new extractor is a weaker delta rather
#: than a crash.
METHOD_TIERS: Dict[str, str] = {
    # 1. Identity. Two digests agree or they do not; there is no margin.
    "content_hash": "hash",
    "sha256": "hash",
    "byte_compare": "hash",
    # 2. Decoders reading the format's own structure. `element_key` is the
    #    adapter's own addressing, which is why `alignment.by_key` claims
    #    `exact` with it: an identical address is identity, not a guess.
    "element_key": "parser",
    "ast_diff": "parser",
    "symbol_table": "parser",
    "schema_diff": "parser",
    "json_pointer": "parser",
    "text_extract": "parser",
    "manifest_field": "parser",
    "exif": "parser",
    # 3. Specialised algorithms: right about their own question, and their
    #    question is a proxy for the one that was asked.
    "line_diff": "algorithm",
    "sequence_match": "algorithm",
    "name_similarity": "algorithm",
    "table_compare": "algorithm",
    "timing_align": "algorithm",
    "transcript_align": "algorithm",
    # 4. Perceptual metrics. §31: a perceptual hash is not identity.
    "perceptual_hash": "perceptual",
    "delta_e": "perceptual",
    "ssim": "perceptual",
    "mask_iou": "perceptual",
    "embedding_distance": "perceptual",
    "face_match": "perceptual",
    "waveform_compare": "perceptual",
    # 5. A model reading the artifact. §3.3: never the model that made it.
    "model_compare": "model",
    "model_review": "model",
    # 6. A person. Capped at `high` by `TIER_CEILING`, not `exact`: a strong
    #    witness who cannot be re-run is still not a checksum.
    "human_review": "human",
}

#: What `describe` says. The `unknown` line is the one this module exists for:
#: every other entry is a measurement, and that one is the absence of a
#: measurement wearing a word that a reader must not mistake for reassurance.
_DESCRIPTIONS: Dict[str, str] = {
    "exact": "Decided by identity; two runs of this check cannot disagree.",
    "high": "Measured directly, by a method that can be wrong at the margin.",
    "medium": "Measured through a proxy for the property, not the property.",
    "low": "One weak signal, and the correspondence behind it may be wrong.",
    "unknown": "Not measured. This is not a report that nothing changed.",
}


def tier_of(method: str) -> str:
    """The tier a method belongs to. An unregistered method gets `model`.

    `model` and not a new word: the answer is written into `Finding.tier` and
    `Alignment.tier`, both validated against `EXTRACTION_TIERS`, so returning
    the honest string `"unregistered"` would raise inside a contract and be
    read as the contract's fault. Among the tiers that exist, `model` carries
    the weakest ceiling (`medium`), which is the direction that cannot promote
    an unknown method into a certainty.

    A caller that already knows its tier may pass the tier name itself; it is
    returned unchanged, because an adapter naming `"parser"` as its method is
    describing the same thing this table describes.
    """
    name = str(method or "").strip()
    if name in METHOD_TIERS:
        return METHOD_TIERS[name]
    if name in EXTRACTION_TIERS:
        return name
    return "model"


def combine(*values: Any) -> str:
    """The weakest of several confidences. §4: never a uniform percentage.

    A thin wrapper over `contracts.weakest_confidence` on purpose -- one
    implementation of the fold, in the contract, where it cannot drift -- and a
    named call site so that the alternative anyone reaches for by reflex, an
    average, has to be written out deliberately to be written at all.
    """
    return weakest_confidence(values)


def propagate(base: Any, *, alignment: Any = None,
              coverage_ratio: Optional[float] = None,
              limitations: Sequence[str] = ()) -> str:
    """`base`, weakened by everything the caller knows could be wrong with it.

    The four inputs are four different ways to be wrong and they are folded,
    never averaged:

    * `alignment` -- if we may be describing the WRONG element, nothing about
      that element can be `exact`. An `uncertain` relation contributes `low`
      even when the alignment object itself claims better, because `Alignment`
      built by keyword arguments never passed through `Alignment.parse` and its
      confidence was therefore never capped.
    * `coverage_ratio` -- a comparison that examined half the elements is a
      weaker witness about the whole. It can only lower: a ratio of 1.0
      contributes `exact`, which changes nothing. Coverage never RAISES
      confidence (rule 5), which is why full coverage by a perceptual method
      still ends at `medium`.
    * `limitations` -- `exact` is a claim that nothing was left unmeasured, and
      a named limitation is exactly something that was. It caps at `high`
      rather than lower: a limitation removes identity, it does not turn a
      direct measurement into a guess.

    Out-of-range ratios raise rather than clamp, for `contracts._ratio`'s
    reason: clamping 1.4 to 1.0 turns an extractor's arithmetic bug into a
    claim of complete coverage, and that is the one direction that must never
    happen quietly.
    """
    parts = [base]
    relation, alignment_confidence = alignment_signal(alignment)
    if alignment_confidence:
        parts.append(alignment_confidence)
    if relation == "uncertain":
        parts.append("low")
    if coverage_ratio is not None:
        parts.append(_ceiling_for_ratio(coverage_ratio))
    if limitations:
        parts.append("high")
    return weakest_confidence(parts)


def describe(level: str) -> str:
    """One short sentence for an interface. Never raises mid-render.

    An unrecognised word is described as `unknown` rather than rejected: this
    is called while a card is being drawn, and the same reasoning as
    `contracts.confidence_rank` applies -- the failure mode of raising here is
    a view that dies on a typo, and the failure mode of answering `unknown` is
    a view that under-claims.
    """
    name = str(level or "").strip()
    if name in _DESCRIPTIONS:
        return _DESCRIPTIONS[name]
    return _DESCRIPTIONS["unknown"]


def alignment_signal(alignment: Any) -> Tuple[str, str]:
    """`(relation, confidence)` out of whatever `Finding.alignment` holds.

    Public because `classification` needs the relation to decide whether an
    unrequested change is `incidental` or `unknown`, and a second reader for a
    field with three shapes would drift from this one -- the copy that drifted
    being the one that decides whether an uncertain correspondence stays
    uncertain.

    `Finding.alignment` is typed `Optional[Any]` and its `to_dict` already
    handles both an object with `to_dict` and a raw value, so the three shapes
    below are the ones that genuinely arrive: an `Alignment`, its mapping form
    after a round trip through storage, and a bare relation name from an
    adapter that had nothing else to say. Anything else contributes `unknown`,
    which is weakest and therefore safe -- guessing at an unrecognised object
    is how an uncertain correspondence would end up propagating as fact.
    """
    if alignment is None:
        return "", ""
    relation = getattr(alignment, "relation", None)
    confidence = getattr(alignment, "confidence", None)
    if isinstance(relation, str):
        return relation, confidence if isinstance(confidence, str) else "unknown"
    if isinstance(alignment, Mapping):
        return (str(alignment.get("relation") or ""),
                str(alignment.get("confidence") or "unknown"))
    if isinstance(alignment, str):
        # A relation with no confidence attached: the relation still decides
        # whether this is a correspondence anyone should build on.
        return alignment.strip(), ""
    return "", "unknown"


def _ceiling_for_ratio(ratio: Any) -> str:
    """The best confidence a partial comparison may claim. Bands, not a curve.

    Bands because the input is not that precise: a coverage ratio is "how much
    of it did we look at", and mapping it through a continuous function would
    dress four honest buckets up as a measurement. 0.0 is `unknown` and not
    `low`, because a comparison that examined nothing observed nothing.
    """
    if isinstance(ratio, bool) or not isinstance(ratio, (int, float)):
        raise DeltaError("coverage_ratio", "expected a number between 0 and 1", got=ratio)
    value = float(ratio)
    if value < 0.0 or value > 1.0:
        raise DeltaError("coverage_ratio", "is outside 0..1", got=ratio)
    if value >= 1.0:
        return "exact"
    if value >= 0.8:
        return "high"
    if value >= 0.5:
        return "medium"
    if value > 0.0:
        return "low"
    return "unknown"
