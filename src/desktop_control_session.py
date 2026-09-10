"""Desktop host indicator handshake and Escape cancellation for agent runs.

DESK-01 ("control visible del escritorio") adds one more thing to this
module: a per-session allowlist of windows/apps the automation is allowed to
be driving, and an audit trail of before/after captures for every control
action. Both are OPT-IN (empty by default for every session) so a caller
that never touches this stays byte-for-byte on today's behaviour -- the
existing indicator handshake and Escape kill-switch above are the
always-on half of DESK-01; the allowlist and audit trail below are the
half a caller turns on deliberately for a session that should be pinned to
one app and logged (rule 3: nothing existing is narrowed by adding this).
"""
from __future__ import annotations

import asyncio
import contextvars
import functools
import json
from pathlib import Path
import threading
import time
from typing import Any, Dict, List, Optional, Sequence
import uuid

RUNTIME = Path(__file__).resolve().parents[1] / "data" / "runtime"
_run = contextvars.ContextVar("desktop_control_run", default=None)


def _read(name):
    try:
        return json.loads((RUNTIME / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write(name, value):
    RUNTIME.mkdir(parents=True, exist_ok=True)
    path = RUNTIME / name
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value), encoding="utf-8")
    tmp.replace(path)


def cancelled(state):
    return _read("desktop-cancel.json").get("token") == state["token"]


def _heartbeat(state):
    if state["active"]:
        _write("desktop-control.json", {"token": state["token"], "updated": time.time()})


async def ensure_indicator():
    state = _run.get()
    if state is None:
        raise RuntimeError("Desktop control requires an active agent run in the Faustus desktop app.")
    if cancelled(state):
        raise asyncio.CancelledError("Desktop control stopped with Escape")
    other = _read("desktop-control.json")
    if other.get("token") not in (None, state["token"]) and time.time() - other.get("updated", 0) < 5:
        raise RuntimeError("Another task is controlling the desktop. Stop it first.")
    state["active"] = True
    _heartbeat(state)
    for _ in range(40):
        if cancelled(state):
            raise asyncio.CancelledError("Desktop control stopped with Escape")
        ack = _read("desktop-control-ack.json")
        if ack.get("token") == state["token"]:
            if ack.get("ready"):
                return
            raise RuntimeError(ack.get("error") or "Desktop indicator is unavailable")
        await asyncio.sleep(0.1)
    raise RuntimeError("Open the Faustus desktop app: the screen indicator and Escape stop must be ready before desktop control.")


def desktop_control_run(function):
    @functools.wraps(function)
    async def wrapped(*args, **kwargs):
        state = {"token": uuid.uuid4().hex, "active": False}
        context = _run.set(state)
        iterator = function(*args, **kwargs)
        pending = None
        beat = 0.0
        try:
            while True:
                pending = asyncio.create_task(anext(iterator))
                while not pending.done():
                    await asyncio.wait({pending}, timeout=0.1)
                    if state["active"] and cancelled(state):
                        pending.cancel()
                        await asyncio.gather(pending, return_exceptions=True)
                        yield 'data: {"delta":"Control de pantalla detenido con Esc."}\n\n'
                        yield "data: [DONE]\n\n"
                        return
                    if time.monotonic() - beat > 1:
                        _heartbeat(state)
                        beat = time.monotonic()
                try:
                    chunk = pending.result()
                except StopAsyncIteration:
                    return
                yield chunk
        finally:
            if pending and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            await iterator.aclose()
            if _read("desktop-control.json").get("token") == state["token"]:
                _write("desktop-control.json", {})
            _run.reset(context)
    return wrapped


# ---------------------------------------------------------------------------
# DESK-01: per-session app/window allowlist + control audit trail
# ---------------------------------------------------------------------------

_POLICY_LOCK = threading.Lock()
_ALLOWLISTS: Dict[str, List[str]] = {}
_AUDIT_ENABLED: set = set()
_AUDIT_LOG: List[Dict[str, Any]] = []
_AUDIT_LOG_MAX = 500  # ring buffer: a runaway session must not grow this without bound


def _key(session_id: Any) -> str:
    return str(session_id or "")


def set_allowlist(session_id: Any, titles: Sequence[str]) -> None:
    """Declare the only windows/apps `session_id` is authorised to drive.

    An empty (or never-set) allowlist means "no policy" -- every existing
    caller that never calls this keeps today's unrestricted behaviour.
    """
    cleaned = [str(t).strip() for t in (titles or []) if str(t).strip()]
    with _POLICY_LOCK:
        if cleaned:
            _ALLOWLISTS[_key(session_id)] = cleaned
        else:
            _ALLOWLISTS.pop(_key(session_id), None)


def get_allowlist(session_id: Any) -> List[str]:
    with _POLICY_LOCK:
        return list(_ALLOWLISTS.get(_key(session_id), []))


def clear_allowlist(session_id: Any) -> None:
    with _POLICY_LOCK:
        _ALLOWLISTS.pop(_key(session_id), None)


def is_window_allowed(title: Optional[str], allowlist: Sequence[str]) -> bool:
    """Case-insensitive substring match, either direction -- the same
    matching `WindowsBackend.focus_window` already uses for a title, so a
    window that would be a valid `desktop_focus_window` target is also a
    valid allowlist entry and vice versa. No allowlist configured allows
    everything (opt-in policy, see `set_allowlist`)."""
    if not allowlist:
        return True
    needle = (title or "").lower()
    if not needle:
        return False
    return any(
        (str(a or "").lower() in needle) or (needle in str(a or "").lower())
        for a in allowlist
    )


def check_focus_authorized(current_title: Optional[str], allowlist: Sequence[str]) -> Optional[str]:
    """``None`` when the foreground window named `current_title` is allowed
    under `allowlist`; otherwise the reason automation must pause.

    This IS the DESK-01 acceptance criterion made a function: "moving focus
    to an unauthorized app pauses the automation." `current_title=None`
    (the platform could not report a foreground window right now) fails
    OPEN rather than blocking on a transient platform quirk -- the same
    choice `browser_actions.check_precondition`'s callers make when a
    snapshot cannot be taken.
    """
    if not allowlist:
        return None
    if current_title is None:
        return None
    if not is_window_allowed(current_title, allowlist):
        allowed = ", ".join(allowlist)
        return (
            f"focus moved to {current_title!r}, which is not on the allowed list ({allowed}). "
            "Desktop automation is paused for safety -- call desktop_focus_window on an "
            "allowed window (or update the allowlist) before continuing."
        )
    return None


def enable_audit(session_id: Any) -> None:
    with _POLICY_LOCK:
        _AUDIT_ENABLED.add(_key(session_id))


def disable_audit(session_id: Any) -> None:
    with _POLICY_LOCK:
        _AUDIT_ENABLED.discard(_key(session_id))


def audit_enabled(session_id: Any) -> bool:
    with _POLICY_LOCK:
        return _key(session_id) in _AUDIT_ENABLED


def record_action(session_id: Any, tool: str, *, before_hash: str = "", after_hash: str = "",
                   note: str = "") -> Dict[str, Any]:
    """Append one audit-trail entry for a desktop CONTROL action: DESK-01
    requires "cada acción de escritorio deja captura antes/después y
    registro" -- `before_hash`/`after_hash` are the sha256 of the screen
    capture taken immediately before and after the action ran, so a change
    (or the lack of one) is provable without keeping every raw frame.

    Best-effort by design: a logging failure must never turn a desktop
    action that already ran into a reported failure, so disk I/O errors are
    swallowed after the in-memory entry is kept.
    """
    entry: Dict[str, Any] = {
        "session_id": _key(session_id),
        "tool": str(tool or ""),
        "before_hash": before_hash or "",
        "after_hash": after_hash or "",
        "note": note or "",
        "at": time.time(),
    }
    with _POLICY_LOCK:
        _AUDIT_LOG.append(entry)
        del _AUDIT_LOG[: max(0, len(_AUDIT_LOG) - _AUDIT_LOG_MAX)]
    try:
        RUNTIME.mkdir(parents=True, exist_ok=True)
        with (RUNTIME / "desktop-audit.log").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
    except OSError:
        pass
    return entry


def audit_log(session_id: Optional[Any] = None) -> List[Dict[str, Any]]:
    """The in-memory audit trail, optionally filtered to one session."""
    with _POLICY_LOCK:
        if session_id is None:
            return list(_AUDIT_LOG)
        key = _key(session_id)
        return [dict(e) for e in _AUDIT_LOG if e["session_id"] == key]


def reset_desk01_state() -> None:
    """Test hook: clear every per-session allowlist/audit flag and the
    in-memory audit trail, mirroring `desktop_tools.reset_capture_state`."""
    with _POLICY_LOCK:
        _ALLOWLISTS.clear()
        _AUDIT_ENABLED.clear()
        _AUDIT_LOG.clear()
