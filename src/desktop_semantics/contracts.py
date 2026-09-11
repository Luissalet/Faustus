"""ADP-08 — typed contract for the desktop SEMANTIC layer.

`src/agent_tools/desktop_tools.py::DesktopBackend` is 100% coordinate-based:
click(x, y), type_text, key_combo — the model has to look at a screenshot,
guess a pixel, and hope nothing moved before the click lands. This module
defines the alternative the ADP-08 ficha asks for: name a control by what it
IS (role/name/automation_id/structural path), not where it happened to be.

Vocabulary, all of it load-bearing:

  Snapshot   one bounded read of a window's control tree, scoped to a
             (session_id, generation). `generation` bumps whenever the
             (app, window) identity a session is looking at changes — see
             `session.take_snapshot` — so every `Ref` cut from an old
             generation is provably stale without re-walking the tree.
  Ref        f"{session_id}:{generation}:{snapshot_id}:{n}" — NEVER a bare
             index like "e7" (that was ADP-29's WEB-04 lesson applied here:
             an index is only meaningful against the exact snapshot/session
             that produced it). `n` is the element's position inside ITS
             OWN snapshot only; `resolve()` never trusts it against a
             different snapshot.
  resolve()  turns a `Ref` plus a FRESH snapshot into the same live control,
             matched by identity (role, name, automation_id, path) — never
             by replaying the index against a tree that has since changed
             shape. Fails with one of three typed errors below; never
             silently picks "the first one that looks right".
  act()      re-resolves, checks an optional precondition, executes, and
             reports an `ActionResult` that keeps three DIFFERENT facts
             separate: whether the call was actually delivered to the
             control, what was observed right after, and whether that
             observation was actively verified (re-read) rather than
             assumed. A timeout is `delivery="unknown"` — not "failed", not
             "succeeded" — because the platform genuinely does not know;
             per the repo-wide rule, `unknown` is a fact to reconcile, never
             a license to blindly retry an action that may already have run.

Errors are typed exceptions, not string-shaped, with `.code` matching the
repo's `{"error": ..., "error_class": "<area>.<reason>"}` convention (see
`routes/git_routes.py::_error`) — a caller can branch on `.code` without
parsing English.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Sequence, Tuple

DELIVERY_STATES: Tuple[str, str, str] = ("delivered", "not_delivered", "unknown")


class SemanticError(Exception):
    """Base for every desktop-semantics failure.

    `code` is this error's `error_class` — `desktop_semantics.<reason>` —
    for callers (routes, tools) that want to branch without string-matching
    `str(exc)`, which stays human-readable for the model/user.
    """

    code = "desktop_semantics.error"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class StaleRefError(SemanticError):
    """The ref's generation/snapshot is not the caller session's current
    one, or the element it named is simply gone from a fresh snapshot."""

    code = "desktop_semantics.stale_ref"


class WrongSessionError(SemanticError):
    """The ref (or the fresh snapshot used to resolve it) belongs to a
    different session than the caller's. Refs never cross sessions."""

    code = "desktop_semantics.wrong_session"


class AmbiguousTargetError(SemanticError):
    """Two or more controls in the fresh snapshot are equally plausible
    successors of the ref. `resolve` stops and explains rather than acting
    on the first match — the ADP-08 acceptance criterion in one exception."""

    code = "desktop_semantics.ambiguous_target"

    def __init__(self, message: str, candidates: Sequence[int]):
        super().__init__(message)
        self.candidates = tuple(candidates)


class PreconditionFailedError(SemanticError):
    """`act()`'s precondition did not hold on the re-resolved element right
    before executing — the caller's assumption about the control's state
    was wrong, so nothing was executed."""

    code = "desktop_semantics.precondition_failed"


class UnsupportedOperationError(SemanticError):
    """`op` is not one of the operations this backend/control supports."""

    code = "desktop_semantics.unsupported_operation"


class BackendUnavailableError(SemanticError):
    """No semantic backend for this platform/session (e.g. Windows UIA
    backend without `pywinauto` installed, or a non-Windows host)."""

    code = "desktop_semantics.backend_unavailable"


@dataclass(frozen=True)
class Element:
    """One control inside ONE `Snapshot`. `n` is only meaningful paired with
    that snapshot's `snapshot_id` — see the module docstring on `Ref`."""

    n: int
    role: str
    name: str
    automation_id: str = ""
    path: Tuple[int, ...] = ()
    enabled: bool = True
    value: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def identity(self) -> Tuple[str, str, str, Tuple[int, ...]]:
        """What `resolve()` matches on across snapshots — deliberately NOT
        `n` (an index), which shifts as the tree changes shape."""
        return (self.role, self.name, self.automation_id, self.path)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "n": self.n,
            "role": self.role,
            "name": self.name,
            "automation_id": self.automation_id,
            "path": list(self.path),
            "enabled": self.enabled,
            "value": self.value,
        }


@dataclass(frozen=True)
class Snapshot:
    """One bounded read of a window's control tree. `truncated` is honest
    about depth/size limits ADP-08 requires — never a silently cut tree."""

    snapshot_id: str
    session_id: str
    generation: int
    app: str
    window: str
    elements: Tuple[Element, ...]
    truncated: bool
    taken_at: float

    def ref(self, n: int) -> str:
        return f"{self.session_id}:{self.generation}:{self.snapshot_id}:{n}"

    def element(self, n: int) -> Optional[Element]:
        for el in self.elements:
            if el.n == n:
                return el
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "session_id": self.session_id,
            "generation": self.generation,
            "app": self.app,
            "window": self.window,
            "truncated": self.truncated,
            "taken_at": self.taken_at,
            "elements": [
                {**el.to_dict(), "ref": self.ref(el.n)} for el in self.elements
            ],
        }


def parse_ref(ref: Any) -> Tuple[str, int, str, int]:
    """`f"{session_id}:{generation}:{snapshot_id}:{n}"` -> its four parts.

    A malformed ref (wrong shape, non-integer generation/n) is a CALLER bug
    — raised as the generic `SemanticError`, never `StaleRefError`: staleness
    is a fact about a well-formed ref that no longer resolves, not about a
    ref that was never valid to begin with.
    """
    raw = str(ref or "")
    parts = raw.split(":")
    if len(parts) != 4:
        raise SemanticError(
            f"malformed desktop ref {raw!r}; expected \"session:generation:snapshot:n\""
        )
    session_id, generation_s, snapshot_id, n_s = parts
    if not session_id or not snapshot_id:
        raise SemanticError(
            f"malformed desktop ref {raw!r}; expected \"session:generation:snapshot:n\""
        )
    try:
        generation = int(generation_s)
        n = int(n_s)
    except ValueError:
        raise SemanticError(
            f"malformed desktop ref {raw!r}; expected \"session:generation:snapshot:n\""
        ) from None
    return session_id, generation, snapshot_id, n


@dataclass(frozen=True)
class ActionResult:
    """Three separate facts about one `act()` call — never collapsed into a
    single boolean:

      delivery        'delivered' (the backend confirmed the call reached
                       the control), 'not_delivered' (the backend
                       affirmatively says it did NOT apply — e.g. the
                       control vanished or was disabled), or 'unknown' (the
                       backend could not tell within the timeout — a fact,
                       not a failure code; see module docstring).
      observed_after   whatever the backend read right after acting (e.g.
                       the control's new value), or None when nothing was
                       observed (delivery='unknown').
      verified         True only when `observed_after` was actively
                       re-read and matches an expected post-state — a
                       'delivered' call with verified=False just means the
                       backend confirmed the call was sent, not that anyone
                       checked the control changed.
    """

    delivery: str
    observed_after: Optional[Dict[str, Any]]
    verified: bool

    def __post_init__(self):
        if self.delivery not in DELIVERY_STATES:
            raise SemanticError(f"invalid ActionResult.delivery {self.delivery!r}")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "delivery": self.delivery,
            "observed_after": self.observed_after,
            "verified": self.verified,
        }
