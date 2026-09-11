"""ADP-08 — per-session snapshot/generation registry, and the `resolve`/
`act` verbs built on top of it.

State here is process-local and in-memory, on purpose: a `Ref` is only ever
meant to survive one chat session's lifetime (the same scope
`src/agent_tools/desktop_tools.py::_focused_targets` already uses for
"the window desktop_focus_window last selected") — nothing here is a second
persistent store for anything `agent_runs`/`tool_approvals` already own.
"""
from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Dict, Optional, Tuple

from .contracts import (
    ActionResult,
    AmbiguousTargetError,
    BackendUnavailableError,
    Element,
    PreconditionFailedError,
    SemanticError,
    Snapshot,
    StaleRefError,
    WrongSessionError,
    parse_ref,
)

_LOCK = threading.Lock()
# session_id -> {"generation": int, "window_key": (app, window) | None,
#                "snapshots": {snapshot_id: Snapshot}, "latest": snapshot_id | None}
_STATE: Dict[str, Dict[str, Any]] = {}

# A session keeps only its most recent snapshots; a ref pointing past this
# ring is legitimately gone (StaleRefError), the same as one from an old
# generation — bounding memory, not correctness.
_SNAPSHOT_RING = 20


def reset_state() -> None:
    """Test-only: forget every session's generation/snapshot memory."""
    with _LOCK:
        _STATE.clear()


def _window_key(app: str, window: str) -> Tuple[str, str]:
    return (str(app or ""), str(window or ""))


def current_generation(session_id: str) -> int:
    with _LOCK:
        return _STATE.get(str(session_id or ""), {}).get("generation", 0)


def get_snapshot(session_id: str, snapshot_id: str) -> Optional[Snapshot]:
    with _LOCK:
        return _STATE.get(str(session_id or ""), {}).get("snapshots", {}).get(snapshot_id)


def latest_snapshot(session_id: str) -> Optional[Snapshot]:
    with _LOCK:
        state = _STATE.get(str(session_id or ""))
        if not state or not state.get("latest"):
            return None
        return state["snapshots"].get(state["latest"])


def take_snapshot(session_id: str, raw: Dict[str, Any]) -> Snapshot:
    """Wrap a raw semantic-backend read (`{app, window, elements, truncated}`)
    into a session-scoped `Snapshot`.

    `generation` bumps the moment the (app, window) identity differs from
    the session's last snapshot — a window swap or app switch — so every
    `Ref` cut from the OLD generation fails `StaleRefError` on its next
    `resolve()`, even if the platform happens to reuse the same underlying
    window handle. This is the ADP-08 "ref de otra generación es rechazada"
    criterion, made structural rather than a per-call check someone could
    forget to write.
    """
    session_id = str(session_id or "")
    if not session_id:
        raise SemanticError("a desktop snapshot requires a session")
    app = str(raw.get("app") or "")
    window = str(raw.get("window") or "")
    raw_elements = raw.get("elements") or []
    elements = tuple(
        Element(
            n=i,
            role=str(e.get("role") or ""),
            name=str(e.get("name") or ""),
            automation_id=str(e.get("automation_id") or ""),
            path=tuple(int(p) for p in (e.get("path") or ())),
            enabled=bool(e.get("enabled", True)),
            value=e.get("value"),
            extra={
                k: v for k, v in e.items()
                if k not in {"role", "name", "automation_id", "path", "enabled", "value"}
            },
        )
        for i, e in enumerate(raw_elements)
    )
    truncated = bool(raw.get("truncated", False))

    with _LOCK:
        state = _STATE.setdefault(
            session_id, {"generation": 0, "window_key": None, "snapshots": {}, "latest": None}
        )
        key = _window_key(app, window)
        if state["window_key"] != key:
            state["generation"] += 1
            state["window_key"] = key
            # Old snapshot ids are dropped too: even a direct snapshot_id
            # lookup for the old window must not resolve once the identity
            # it described is gone.
            state["snapshots"] = {}
        snapshot_id = uuid.uuid4().hex[:12]
        snap = Snapshot(
            snapshot_id=snapshot_id,
            session_id=session_id,
            generation=state["generation"],
            app=app,
            window=window,
            elements=elements,
            truncated=truncated,
            taken_at=time.time(),
        )
        state["snapshots"][snapshot_id] = snap
        state["latest"] = snapshot_id
        if len(state["snapshots"]) > _SNAPSHOT_RING:
            oldest_id = min(state["snapshots"].values(), key=lambda s: s.taken_at).snapshot_id
            if oldest_id != snapshot_id:
                state["snapshots"].pop(oldest_id, None)
        return snap


def resolve(ref: str, snapshot_fresh: Snapshot, *, caller_session_id: str) -> Element:
    """Turn `ref` into the live `Element` inside `snapshot_fresh` — a
    snapshot the caller took JUST NOW, not the one `ref` was cut from.

    Three typed failures (never a silent "closest match"):

      WrongSessionError    `ref`, or `snapshot_fresh`, names a different
                            session than `caller_session_id`.
      StaleRefError         `ref`'s generation is not the session's CURRENT
                            one, the origin snapshot/element is gone, or no
                            element in `snapshot_fresh` shares its identity.
      AmbiguousTargetError  more than one element in `snapshot_fresh` is an
                            equally plausible successor — resolve refuses to
                            guess which one.
    """
    session_id, generation, snapshot_id, n = parse_ref(ref)
    caller_session_id = str(caller_session_id or "")
    if session_id != caller_session_id:
        raise WrongSessionError(
            f"ref {ref!r} belongs to session {session_id!r}, not the caller's {caller_session_id!r}"
        )
    if snapshot_fresh.session_id != caller_session_id:
        raise WrongSessionError(
            f"the fresh snapshot belongs to session {snapshot_fresh.session_id!r}, "
            f"not {caller_session_id!r} — resolve needs a same-session snapshot"
        )
    current_gen = current_generation(caller_session_id)
    if generation != current_gen:
        raise StaleRefError(
            f"ref {ref!r} is from generation {generation}, the session is now at "
            f"{current_gen} (the window/app changed since this ref was taken) — "
            "take a new desktop_snapshot"
        )
    origin = get_snapshot(caller_session_id, snapshot_id)
    if origin is None:
        raise StaleRefError(
            f"ref {ref!r} points to a snapshot Faustus no longer holds — take a new desktop_snapshot"
        )
    target = origin.element(n)
    if target is None:
        raise StaleRefError(f"ref {ref!r} has no element #{n} in its own snapshot")

    identity = target.identity()
    exact = [el for el in snapshot_fresh.elements if el.identity() == identity]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise AmbiguousTargetError(
            f"{len(exact)} controls in the fresh snapshot are identical to {ref!r} "
            f"({target.role} {target.name!r}); cannot pick one without more information",
            candidates=[el.n for el in exact],
        )
    # No exact structural match: the tree may have reflowed without the
    # control being deleted. Fall back to role+name+automation_id, but this
    # relaxation can ALSO turn up more than one plausible match — still
    # AmbiguousTargetError, never "pick the first".
    relaxed = [
        el for el in snapshot_fresh.elements
        if (el.role, el.name, el.automation_id) == (target.role, target.name, target.automation_id)
    ]
    if not relaxed:
        raise StaleRefError(
            f"the control behind {ref!r} ({target.role} {target.name!r}) is no longer present"
        )
    if len(relaxed) > 1:
        raise AmbiguousTargetError(
            f"{len(relaxed)} controls now match {target.role} {target.name!r} "
            f"(structural path changed); none is a unique successor of {ref!r} — "
            "take a new desktop_snapshot and pick explicitly",
            candidates=[el.n for el in relaxed],
        )
    return relaxed[0]


def check_precondition(element: Element, precondition: Optional[Dict[str, Any]]) -> None:
    """`act()`'s guard: an optional `{attr: expected_value}` that must hold
    on the RE-RESOLVED element right before the action runs (e.g.
    `{"enabled": True}`, `{"value": "0"}`). Any mismatch is a typed failure
    — nothing is ever executed against a control that moved from under a
    caller's assumption."""
    if not precondition:
        return
    for key, expected in precondition.items():
        if hasattr(element, key):
            actual = getattr(element, key)
        else:
            actual = element.extra.get(key)
        if actual != expected:
            raise PreconditionFailedError(
                f"precondition failed: expected {key}={expected!r}, found {actual!r} on "
                f"{element.role} {element.name!r}"
            )


def act(
    *,
    session_id: str,
    ref: str,
    op: str,
    backend: Any,
    params: Optional[Dict[str, Any]] = None,
    precondition: Optional[Dict[str, Any]] = None,
    timeout: float = 5.0,
    snapshot_kwargs: Optional[Dict[str, Any]] = None,
) -> Tuple[Element, ActionResult]:
    """Re-resolve `ref` against a FRESH snapshot from `backend.semantic()`,
    check `precondition`, execute `op`, and return `(element, ActionResult)`.

    `unknown` delivery (the backend's `invoke()` timed out) is returned as
    data, exactly once — this function itself never retries the call. A
    caller that resubmits an `unknown` action on its own is doing exactly
    the thing the repo-wide rule forbids ("nunca se reintenta solo"); that
    is a decision for a human/reconciliation step, not this function.
    """
    semantic = backend.semantic() if hasattr(backend, "semantic") else None
    if semantic is None:
        raise BackendUnavailableError(
            "this platform/backend has no semantic desktop support "
            "(desktop_click/desktop_type/desktop_key still work by coordinates)"
        )
    fresh_raw = semantic.snapshot(session_id=session_id, **(snapshot_kwargs or {}))
    fresh = take_snapshot(session_id, fresh_raw)
    element = resolve(ref, fresh, caller_session_id=session_id)
    check_precondition(element, precondition)
    try:
        outcome = semantic.invoke(element, op, params or {}, timeout=timeout)
    except TimeoutError:
        return element, ActionResult(delivery="unknown", observed_after=None, verified=False)
    delivery = str((outcome or {}).get("delivery") or "unknown")
    if delivery not in ("delivered", "not_delivered", "unknown"):
        delivery = "unknown"
    observed_after = (outcome or {}).get("observed_after")
    verified = bool((outcome or {}).get("verified", False)) and delivery == "delivered"
    return element, ActionResult(delivery=delivery, observed_after=observed_after, verified=verified)
