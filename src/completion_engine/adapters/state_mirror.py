"""The State Mirror, read from the turn path. §19's "sigue siendo oportuna" half.

§19 asks the State Mirror three things: what the next step is, whether an
improvement is still timely, and whether the run is about to act on stale
state. All three are answered by the six deterministic situation queries in
`state_mirror.queries.SITUATIONS`, so this module exposes those and one
convenience over them, and adds no query of its own.

**Nothing here raises**, for the reasons `adapters/__init__.py` states:
`agent_state_mirror` is off by default, and `degraded()` is what turns the
resulting silence into a sentence in the closeout rather than an absence.

**A name outside `SITUATIONS` is dropped, never guessed at.**
`StateMirrorService.situation` already answers a refusal listing what exists,
with the comment that an empty list for a typo reads as "nothing is running" --
the most dangerous wrong answer this subsystem can give. This module honours
that by filtering names against `SITUATIONS` before asking and by keeping a
refused name OUT of the result mapping, so a caller that iterates the result
never sees a key it can mistake for an empty answer.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Sequence, Tuple

logger = logging.getLogger(__name__)

__all__ = ["enabled", "situations", "unfinished", "UNFINISHED_SITUATIONS"]

#: The situations that mean "something is not done yet", in the order a
#: closeout would read them: work that stopped, work still moving, and work
#: whose result nobody has verified. `pending_approvals`,
#: `available_capabilities` and `resource_pressure` are deliberately out --
#: they describe the environment, not unfinished work, and folding them in
#: would make `unfinished()` answer a different question from its name.
UNFINISHED_SITUATIONS: Tuple[str, ...] = (
    "blocked_work", "running_work", "unverified_changes",
)


def enabled() -> bool:
    """`state_mirror.service.enabled()`, or False when it cannot be asked."""
    try:
        from src.state_mirror.service import enabled as _enabled

        return bool(_enabled())
    except Exception as exc:  # noqa: BLE001 - an adapter never raises
        logger.debug("completion state adapter: switch unreadable: %s", exc)
        return False


def _known(names: Sequence[str]) -> Tuple[str, ...]:
    """The requested names that are real situations, in `SITUATIONS` order.

    `SITUATIONS`' order and not the caller's, so two callers asking for the
    same set in different orders get the same mapping back -- the same
    reproducibility rule `discovery.discover` follows for its generators.
    """
    try:
        from src.state_mirror.queries import SITUATIONS
    except Exception as exc:  # noqa: BLE001 - an adapter never raises
        logger.debug("completion state adapter: vocabulary unreadable: %s", exc)
        return ()
    wanted = {str(n or "").strip() for n in (names or ())}
    if not wanted:
        return tuple(SITUATIONS)
    return tuple(name for name in SITUATIONS if name in wanted)


def situations(*, owner: str, project_id: str = "",
               names: Sequence[str] = ()) -> Dict[str, List[Dict[str, Any]]]:
    """`{situation name: rows}` for the names asked for, or `{}`.

    The shape is `DiscoveryInput.situations`' shape exactly -- a name mapped to
    its rows -- so the result can be handed straight to `discover()` without a
    translation step that could drift.

    A situation the service refuses (`ok` is not true) is OMITTED rather than
    mapped to `[]`. `_from_state_mirror` produces nothing for a missing key and
    nothing for an empty list, so the two behave the same in discovery -- but
    they do not read the same to a person, and "we did not get an answer" must
    not be written down as "there is nothing there".
    """
    if not enabled():
        return {}
    wanted = _known(names)
    if not wanted:
        return {}
    out: Dict[str, List[Dict[str, Any]]] = {}
    try:
        from src.state_mirror.service import service

        mirror = service()
    except Exception as exc:  # noqa: BLE001 - an adapter never raises
        logger.debug("completion state adapter: service unavailable: %s", exc)
        return {}
    for name in wanted:
        try:
            answer = mirror.situation(name, owner=str(owner or ""),
                                      project_id=str(project_id or ""))
        except Exception as exc:  # noqa: BLE001 - an adapter never raises
            logger.debug("completion state adapter: %s failed: %s", name, exc)
            continue
        if not isinstance(answer, Mapping) or not answer.get("ok"):
            continue
        rows = answer.get("rows")
        if isinstance(rows, list):
            out[name] = [row for row in rows if isinstance(row, dict)]
    return out


def unfinished(*, owner: str, project_id: str = "") -> Tuple[Mapping[str, Any], ...]:
    """Every row of `UNFINISHED_SITUATIONS`, flattened, each tagged with its query.

    Flattened because the caller's question is "is anything still open", and
    tagged with `situation` because the answer to "which kind of open" is
    otherwise lost the moment the three lists are concatenated. The key added
    is `situation`, which none of the three queries emits, so nothing is
    overwritten.
    """
    found = situations(owner=owner, project_id=project_id,
                       names=UNFINISHED_SITUATIONS)
    out: List[Mapping[str, Any]] = []
    for name in UNFINISHED_SITUATIONS:
        for row in found.get(name) or ():
            tagged = dict(row)
            tagged["situation"] = name
            out.append(tagged)
    return tuple(out)
