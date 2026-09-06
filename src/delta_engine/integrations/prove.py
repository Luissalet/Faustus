"""The delta and the proof: two vocabularies, one direction, no promotion.

`prove` answers `proved | partial | unproved | contradicted` about a RUN, from
evidence on disk. A delta answers `matched | partial | mismatched | regressed |
inconclusive` about a CHANGE, from two revisions. They overlap on the word
`partial` and on nothing else, and the council already paid for the lesson that
two verdict vocabularies in one system need an explicit map rather than a
`str()` -- `src/council/adapters.py` keeps `_VERDICT_FROM_PROOF` for exactly
this reason.

The direction here is one-way and downhill:

* A delta never raises a proof. `unproved` with a beautiful `matched` beside it
  stays `unproved`; §20's rule is "no rebajar `unproved` porque exista
  descripción semántica", and a semantic description is precisely what a delta
  is.
* A proof CAN lower an assessment. A run whose evidence was contradicted did
  not produce the target the delta compared, so the comparison is at best
  `partial` however clean it looked.

`prove` itself is untouched: no new uncertainty token, no new verdict, no
argument added to its signature. What this module does is read a proof packet
and cap an assessment, which needs nothing from `prove` but its output.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Mapping, Optional, Tuple

from src.delta_engine.contracts import (
    ASSESSMENTS,
    UniversalDelta,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ASSESSMENT_CEILING",
    "PROOF_VERDICTS",
    "ceiling_for",
    "cap",
    "attach",
    "summarise",
]

#: `prove.VERDICTS`, repeated rather than imported at module scope on purpose:
#: importing `src.prove` here would pull settings and the whole proof stack into
#: every `registry.discover()`, and this module is reached from the service.
#: `_verdicts()` below checks the copy against the real tuple when `prove` is
#: already loaded, so a drift is noticed rather than assumed away.
PROOF_VERDICTS: Tuple[str, ...] = ("proved", "partial", "unproved", "contradicted")

#: The best a delta may claim once the run behind it has been judged.
#:
#: `proved` imposes no ceiling: the run did what it said, so the comparison
#: stands on its own. `partial` and `unproved` cap at `partial` -- the target
#: exists but the claim that this run produced it is not established, and a
#: comparison against a target of unknown provenance is at best a partial
#: answer. `contradicted` caps at `inconclusive`, not at `mismatched`: a
#: contradicted run means the evidence disagreed with the report, so we do not
#: know WHAT we compared, and `mismatched` would be a confident statement about
#: the change that nobody is entitled to.
ASSESSMENT_CEILING: Dict[str, str] = {
    "proved": "",
    "partial": "partial",
    "unproved": "partial",
    "contradicted": "inconclusive",
}

#: Only the assessments that are stronger than a ceiling get lowered to it.
#: `regressed` is NOT lowered by a weak proof, and that asymmetry is the point:
#: a regression is a finding about the target that stands whether or not the
#: run that produced it can be proved. Hiding it behind an unproved run would
#: be the one direction of this map that loses information.
_NEVER_LOWERED: Tuple[str, ...] = ("regressed", "inconclusive")

_STRENGTH: Dict[str, int] = {
    "matched": 4, "partial": 3, "mismatched": 2, "inconclusive": 1, "regressed": 5,
}


def _verdicts() -> Tuple[str, ...]:
    """`prove.VERDICTS` when it is already imported, our copy otherwise.

    Checked rather than trusted: if `prove` grows a fifth verdict, this module
    logs it once and treats the unknown word as the most cautious ceiling, so a
    new verdict cannot silently mean "no ceiling at all".
    """
    module = __import__("sys").modules.get("src.prove")
    real = getattr(module, "VERDICTS", None) if module is not None else None
    if isinstance(real, tuple) and set(real) != set(PROOF_VERDICTS):
        logger.warning("delta/prove: verdict vocabularies differ, ours=%s theirs=%s",
                       PROOF_VERDICTS, real)
        return real
    return PROOF_VERDICTS


def ceiling_for(proof: Any) -> str:
    """The strongest assessment this proof allows. `""` means no ceiling.

    An unreadable or absent proof imposes NO ceiling, which looks generous and
    is the correct reading: "nobody judged the run" is not evidence against the
    comparison. What lowers an assessment is a judgement that went badly, never
    the absence of one -- the opposite convention would make every delta
    computed outside a run permanently `partial`.
    """
    if not isinstance(proof, Mapping):
        return ""
    verdict = str(proof.get("verdict") or "")
    if not verdict:
        return ""
    if verdict not in _verdicts():
        logger.warning("delta/prove: unknown verdict %r, applying the safest ceiling",
                       verdict)
        return "partial"
    return ASSESSMENT_CEILING.get(verdict, "partial")


def cap(assessment: str, proof: Any) -> Tuple[str, str]:
    """`(assessment, reason)` after the proof's ceiling is applied.

    Returns the reason as a sentence and not a boolean, because a delta whose
    assessment was lowered by someone else's verdict has to be able to say so:
    a `partial` with no explanation reads as a weak comparison, and this one is
    a strong comparison against a target the run could not prove it produced.
    """
    name = str(assessment or "")
    if name not in ASSESSMENTS:
        return name, ""
    if name in _NEVER_LOWERED:
        return name, ""
    ceiling = ceiling_for(proof)
    if not ceiling or ceiling not in ASSESSMENTS:
        return name, ""
    if _STRENGTH.get(name, 0) <= _STRENGTH.get(ceiling, 0):
        return name, ""
    verdict = str((proof or {}).get("verdict") or "?")
    return ceiling, (
        f"lowered from `{name}` to `{ceiling}`: the run that produced the target "
        f"was judged `{verdict}` by prove, so what was compared is not "
        f"established"
    )


def attach(delta: UniversalDelta, proof: Any) -> UniversalDelta:
    """A copy of `delta` carrying the proof's identity and its ceiling.

    `proof_ref` holds `proof["identity"]` -- the length-prefixed digest `prove`
    already computes -- and not the packet. The packet belongs to the run; a
    copy of it here would be a second place for it to be true.
    """
    if not isinstance(proof, Mapping):
        return delta
    assessment, reason = cap(delta.assessment, proof)
    limitations = tuple(delta.limitations)
    if reason and reason not in limitations:
        limitations = limitations + (reason,)
    payload = delta.to_dict()
    payload["assessment"] = assessment
    payload["proof_ref"] = str(proof.get("identity") or "")
    payload["limitations"] = list(limitations)
    return UniversalDelta.parse(payload, "delta")


def summarise(proof: Any) -> Dict[str, Any]:
    """The three fields of a proof a delta reader needs, and nothing else."""
    if not isinstance(proof, Mapping):
        return {}
    return {
        "verdict": str(proof.get("verdict") or ""),
        "confidence": proof.get("confidence"),
        "identity": str(proof.get("identity") or ""),
        "uncertainty": list(proof.get("uncertainty") or ())[:5],
    }
