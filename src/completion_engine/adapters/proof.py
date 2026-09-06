"""`prove`, reached the way the rest of Faustus reaches it: through a ChangeSet.

§19's last block: "Core siempre se verifica según riesgo... Cierre distingue
core probado de bonus parcial." The verdict this engine prints in its closeout
has to be the SAME verdict the rest of the product prints, so this module calls
`changesets.from_turn` and `changesets.judge` and does not call
`prove.prove` directly.

That indirection is the point rather than a detour. `changesets.judge` folds in
`ChangeSet.evidence_gaps()` -- an `implement` that verified nothing is a gap an
`explore` does not have -- before handing the packet to `prove`, and a caller
that went straight to `prove.prove(evidence, verification, claims)` would get a
verdict that sounds surer than the one on the change card for the same turn.
Two confidence numbers for one turn is the failure this whole subsystem exists
to avoid.

**Nothing here raises**, and the empty answer is `{}`. `verdict_of({})` is
`""`, which `closeout` prints as "not established" -- distinct from `unproved`,
which is a real verdict somebody computed.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Mapping, Sequence

logger = logging.getLogger(__name__)

__all__ = ["enabled", "judge_turn", "verdict_of", "is_proved", "uncertainty_line"]


def enabled() -> bool:
    """`prove.enabled()` -- setting `agent_dispatch_prove`, which defaults ON.

    Present so `adapters.available()` can report all three integrations from
    one place. `judge_turn` does NOT gate on it: `prove` itself never raises
    and answers a verdict regardless, and refusing to compute one here would
    turn a setting about dispatch payloads into a silent hole in the closeout.
    """
    try:
        from src.prove import enabled as _enabled

        return bool(_enabled())
    except Exception as exc:  # noqa: BLE001 - an adapter never raises
        logger.debug("completion proof adapter: switch unreadable: %s", exc)
        return False


def judge_turn(harness_summary: Mapping[str, Any], *, workspace: str = "",
               claims: Sequence[Mapping[str, Any]] = ()) -> Dict[str, Any]:
    """A `TurnLedger.summary()` -> a `prove` packet. `{}` when it cannot be built.

    `intent="implement"` is `from_turn`'s own default and is left there
    deliberately: a completion run is by definition doing work, and an
    `explore` intent would excuse the missing verification that
    `evidence_gaps()` is there to notice.

    `claims` is passed as `None` when empty rather than as `[]`, matching
    `from_turn`'s signature -- the two are equivalent to `build`, and using the
    documented default keeps this call readable next to the function it calls.
    """
    try:
        from src import changesets

        changeset = changesets.from_turn(
            dict(harness_summary or {}),
            workspace=str(workspace or ""),
            claims=[dict(c) for c in (claims or ())] or None,
        )
        proof = changesets.judge(changeset)
    except Exception as exc:  # noqa: BLE001 - an adapter never raises
        logger.debug("completion proof adapter: no verdict could be built: %s", exc)
        return {}
    return dict(proof) if isinstance(proof, Mapping) else {}


def verdict_of(proof: Any) -> str:
    """The verdict word, or `""` when there is no proof or it is not a verdict.

    Checked against `prove.VERDICTS` rather than returned raw: the closeout
    prints this, and a typo or a half-built packet reaching a receipt as though
    it were a verdict is exactly the kind of thing a reader cannot detect for
    themselves. An unrecognised word answers `""`, which reads as "not
    established" -- the honest direction.
    """
    try:
        from src.prove import VERDICTS

        name = str((proof or {}).get("verdict") or "")
        return name if name in VERDICTS else ""
    except Exception as exc:  # noqa: BLE001 - an adapter never raises
        logger.debug("completion proof adapter: verdict unreadable: %s", exc)
        return ""


def is_proved(proof: Any) -> bool:
    """Whether the verdict is `proved`. Nothing weaker counts.

    `partial` is not a pass here. `prove.VERDICTS` uses the word for a run with
    real evidence and real doubts, and §19's "cierre distingue core probado de
    bonus parcial" is the rule that the two must not collapse into one word on
    a receipt.
    """
    return verdict_of(proof) == "proved"


def uncertainty_line(proof: Any) -> str:
    """`"<kind>: <detail>"` for the heaviest doubt, or `""` when there is none.

    `prove.top_uncertainty` is the ranking, reused rather than re-sorted:
    `prove` orders the list by penalty and a second sort here would agree with
    it until somebody changed a penalty, at which point the receipt and the
    change card would name different reasons for the same verdict.
    """
    try:
        from src.prove import top_uncertainty

        top = top_uncertainty(proof)
        if not top:
            return ""
        kind = str(top.get("kind") or "").strip()
        detail = str(top.get("detail") or "").strip()
        if kind and detail:
            return f"{kind}: {detail}"
        return kind or detail
    except Exception as exc:  # noqa: BLE001 - an adapter never raises
        logger.debug("completion proof adapter: uncertainty unreadable: %s", exc)
        return ""
