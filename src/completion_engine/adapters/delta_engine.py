"""The Universal Delta Engine, read from the turn path. §19's scope-creep half.

What §19 asks the Delta Engine for is exactly three questions, and this module
exposes exactly those three: what changed that nobody asked for
(`incidental`), what broke (`regressions`), and what moved outside the intent
(`scope_creep`). Everything else the service offers -- reclassification,
evidence pages, invalidation -- belongs to the routes that own the delta, not
to a completion run reading one.

**Nothing here raises.** `agent_delta_engine` is off by default and
`DeltaEngineService` refuses every write path with `DeltaServiceError` while it
is, so the ordinary answer on a default build is `None` -- and a turn must not
end because the optional engine said no. `adapters.degraded()` is what turns
that `None` into a sentence in the closeout.

**The reading vocabulary is `discovery`'s.** `scope_creep` reads the
`out_of_scope` list and nothing else, because `discovery._delta_rows` reads the
same key for the same meaning. `CLASSIFICATIONS` has no `out_of_scope` member
-- it is `classification.Classified`'s third field, carried alongside the delta
-- so a reader that invented an assertion class for it would answer `()` on
every real delta and nobody would notice.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

__all__ = ["enabled", "delta_for_turn", "scope_creep", "regressions", "incidental"]

#: The domain a coding turn compares under. One of
#: `delta_engine.contracts.DOMAINS`, named here rather than passed by every
#: caller because a completion run over a workspace is a `code` comparison and
#: a caller that guessed `"source"` would get `unknown_domain` back.
DOMAIN = "code"


def enabled() -> bool:
    """`delta_engine.service.enabled()`, or False when it cannot be asked.

    Delegated rather than re-read from `settings`: the service's own switch is
    the one that decides whether `create()` will refuse, so any second reader
    could say yes to a call that is about to be refused.
    """
    try:
        from src.delta_engine.service import enabled as _enabled

        return bool(_enabled())
    except Exception as exc:  # noqa: BLE001 - an adapter never raises
        logger.debug("completion delta adapter: switch unreadable: %s", exc)
        return False


def delta_for_turn(*, owner: str, workspace: str,
                   source: Mapping[str, Any], target: Mapping[str, Any],
                   intent_text: str = "",
                   requested: Sequence[Mapping[str, Any]] = (),
                   project_id: str = "", run_id: str = "",
                   ) -> Optional[Mapping[str, Any]]:
    """One comparison for this turn, as `UniversalDelta.to_dict()`, or None.

    `source` and `target` are `RevisionRef` payloads -- `{"kind": "checkpoint",
    "ref": "<sha>"}` is the usual pair for a coding turn -- and they are passed
    through unshaped. `DeltaRequest.parse` is the validator and building a
    second one here would be a second opinion about what a revision is.

    `run=True` is left at its default so the contract is frozen and the
    comparison runs in one call. `create` freezes the intent BEFORE reading
    either revision (§1.9.1), and splitting that into `create(run=False)` plus
    `run()` from here would give a caller a way to insert work between the two.

    None means: the engine is off, the owner is missing, the request was
    refused, or the comparison failed. All four are `degraded`, and none of
    them is "there was no scope creep" -- which is why the return is `None` and
    not `{}`: an empty mapping reads like an answer.
    """
    if not enabled():
        return None
    try:
        from src.delta_engine.service import service

        payload: Dict[str, Any] = {
            "domain": DOMAIN,
            "source": dict(source or {}),
            "target": dict(target or {}),
            "intent_text": str(intent_text or ""),
            "requested": [dict(item) for item in (requested or ())],
            "project_id": str(project_id or ""),
            "run_id": str(run_id or ""),
        }
        result = service().create(payload, owner=str(owner or ""),
                                  workspace=str(workspace or ""))
    except Exception as exc:  # noqa: BLE001 - an adapter never raises
        logger.debug("completion delta adapter: comparison unavailable: %s", exc)
        return None
    delta = (result or {}).get("delta")
    return delta if isinstance(delta, Mapping) else None


def _assertions(delta: Optional[Mapping[str, Any]],
                classification: str) -> Tuple[Mapping[str, Any], ...]:
    """Every assertion of one classification, defensively.

    A row that is not a mapping is dropped rather than coerced: these come off
    a stored JSON blob, and turning a malformed row into an empty dict would
    put a nameless entry in a closeout's extras list.
    """
    rows = (delta or {}).get("assertions") if isinstance(delta, Mapping) else None
    if not rows or isinstance(rows, (str, bytes, Mapping)):
        return ()
    out: List[Mapping[str, Any]] = []
    try:
        for row in rows:
            if isinstance(row, Mapping) and str(row.get("classification") or "") == classification:
                out.append(row)
    except TypeError:  # noqa: BLE001 - not iterable is not an answer either
        return ()
    return tuple(out)


def regressions(delta: Optional[Mapping[str, Any]]) -> Tuple[Mapping[str, Any], ...]:
    """The assertions classified `regression`. §19: "recalcula riesgo".

    A regression is `core` work in `discovery._DELTA_KINDS` and it is reported
    in the closeout's `not done` when nothing repaired it, because a run that
    reports success with a regression in the optional half of its receipt has
    misled its reader about the thing that matters most.
    """
    return _assertions(delta, "regression")


def incidental(delta: Optional[Mapping[str, Any]]) -> Tuple[Mapping[str, Any], ...]:
    """The assertions classified `incidental`: changed, and nobody asked.

    §30's "no ocultar extras dentro del resumen del core" is why the closeout
    prints these on the extras line rather than folding them into the core
    summary. An incidental change is the purest form of an extra: it is real,
    it is on disk, and no line of the request accounts for it.
    """
    return _assertions(delta, "incidental")


def scope_creep(delta: Optional[Mapping[str, Any]]) -> Tuple[str, ...]:
    """What the delta placed outside the intent's scope, as plain strings.

    Read from the `out_of_scope` list that `classification.Classified` carries
    beside the delta, which is where `discovery._delta_rows` reads it too. One
    key, one meaning, in both readers.

    Strings and not mappings because that is what the list holds and what a
    receipt prints; a caller that needs the assertion behind one of these has
    the delta and can look it up.
    """
    rows = (delta or {}).get("out_of_scope") if isinstance(delta, Mapping) else None
    if not rows or isinstance(rows, (str, bytes, Mapping)):
        return ()
    out: List[str] = []
    try:
        for row in rows:
            text = str(row or "").strip()
            if text and text not in out:
                out.append(text[:512])
    except TypeError:  # noqa: BLE001
        return ()
    return tuple(out)
