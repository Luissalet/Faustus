"""src.desktop_semantics — ADP-08 (semantic desktop contract) + ADP-09
(optional Windows UIA backend).

See `contracts.py` for the full vocabulary (`Snapshot`, `Ref`, `resolve`,
`act`, `ActionResult`) and `session.py` for the per-session generation
registry those verbs run on. `fake_backend.py` is the deterministic
in-memory app tests drive; `windows_uia.py` is the real, optional
`pywinauto`-based backend; `evidence.py` records `desktop_act` outcomes into
the same audit trail the coordinate-based desktop_* tools already use.

`src/agent_tools/desktop_semantic_tools.py` wraps this package as three
agent tools (`desktop_snapshot`, `desktop_find`, `desktop_act`); this
package itself has no dependency on the tool-calling layer and can be used
directly (routes, scripts, tests).
"""
from __future__ import annotations

from .contracts import (
    DELIVERY_STATES,
    ActionResult,
    AmbiguousTargetError,
    BackendUnavailableError,
    Element,
    PreconditionFailedError,
    SemanticError,
    Snapshot,
    StaleRefError,
    UnsupportedOperationError,
    WrongSessionError,
    parse_ref,
)
from .session import (
    act,
    check_precondition,
    current_generation,
    get_snapshot,
    latest_snapshot,
    reset_state,
    resolve,
    take_snapshot,
)

__all__ = [
    "DELIVERY_STATES",
    "ActionResult",
    "AmbiguousTargetError",
    "BackendUnavailableError",
    "Element",
    "PreconditionFailedError",
    "SemanticError",
    "Snapshot",
    "StaleRefError",
    "UnsupportedOperationError",
    "WrongSessionError",
    "parse_ref",
    "act",
    "check_precondition",
    "current_generation",
    "get_snapshot",
    "latest_snapshot",
    "reset_state",
    "resolve",
    "take_snapshot",
]
