"""Reading the neighbouring subsystems without depending on them being on.

`agent_delta_engine` and `agent_state_mirror` are both OFF by default, and this
engine runs on the turn path. Those two facts together fix the shape of
everything in this package:

1.  **An adapter never raises into its caller.** A completion engine that broke
    a turn because an optional subsystem was misconfigured would be worse than
    not existing. Every function here answers `None`, `()` or `{}` on any
    failure, and the failure is logged at debug, never re-raised.

2.  **But the silence is NAMED.** `degraded()` is what
    `CompletionDecision.degraded_integrations` is filled from and what the
    closeout prints. A frontier computed without the Delta Engine is a frontier
    that could not see scope creep, and a stop reported as clean while a
    subsystem was dark is a claim to know more than was known. Silent is not
    the same as invisible, and this module is the difference.

3.  **The switch is read live, every time.** Each `enabled()` here delegates to
    the subsystem's OWN `enabled()` -- `delta_engine.service.enabled`,
    `state_mirror.service.enabled`, `prove.enabled` -- rather than reading the
    setting a second time. A second reader of one setting is a second answer on
    the day either starts caching, and the subsystem's own reader is the one
    that decides whether a call would actually do anything.

`prove` sits in this list beside the other two even though it defaults ON. Its
switch (`agent_dispatch_prove`) can be turned off, and a closeout whose proof
line came back empty because proving was disabled must say so rather than print
"not established" as though nothing could have been shown.
"""

from __future__ import annotations

import logging
from typing import Callable, Dict, Tuple

logger = logging.getLogger(__name__)

__all__ = ["INTEGRATIONS", "available", "degraded"]

#: The integrations this engine consults, in the order a closeout names them.
#: Names, not modules: this tuple is also the vocabulary that ends up in
#: `CompletionDecision.degraded_integrations`, so it has to be stable text.
INTEGRATIONS: Tuple[str, ...] = ("delta_engine", "state_mirror", "proof")


def _switches() -> Dict[str, Callable[[], bool]]:
    """Name -> the subsystem's own `enabled`, imported at call time.

    Imported inside the function for the reason `delta_engine.service.enabled`
    gives about settings: a module-level import would make availability a
    property of when this module first loaded. It also keeps this package
    importable on a build where one of the three is absent, which is what lets
    `available()` report `False` instead of failing to answer at all.
    """
    from src.completion_engine.adapters import delta_engine, proof, state_mirror

    return {
        "delta_engine": delta_engine.enabled,
        "state_mirror": state_mirror.enabled,
        "proof": proof.enabled,
    }


def available() -> Dict[str, bool]:
    """Which integrations would answer if asked, right now.

    An integration whose `enabled()` itself fails is reported `False`. That is
    the safe direction: the alternative is claiming a subsystem is on, asking
    it, getting nothing back, and reporting a clean run.
    """
    out: Dict[str, bool] = {name: False for name in INTEGRATIONS}
    try:
        switches = _switches()
    except Exception as exc:  # noqa: BLE001 - an adapter never raises
        logger.debug("completion adapters: no switch is readable: %s", exc)
        return out
    for name in INTEGRATIONS:
        check = switches.get(name)
        if check is None:
            continue
        try:
            out[name] = bool(check())
        except Exception as exc:  # noqa: BLE001 - an adapter never raises
            logger.debug("completion adapters: %s.enabled failed: %s", name, exc)
            out[name] = False
    return out


def degraded() -> Tuple[str, ...]:
    """The integrations that are NOT available, in `INTEGRATIONS` order.

    This is the value that belongs in `CompletionDecision.degraded_integrations`
    and the one `closeout.render` prints. The order is fixed rather than sorted
    so two runs of one configuration produce the same receipt text.
    """
    ready = available()
    return tuple(name for name in INTEGRATIONS if not ready.get(name))
