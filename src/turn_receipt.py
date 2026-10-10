"""turn_receipt.py - the end-of-turn receipt Faustus leaves on the family Hub's bus (#268).

When a turn's assistant reply is persisted, Faustus posts ONE event,
``faustus.turn.completed``, to the Hoard Hub's event bus (the audit trail and
the rules engine of the family read it). It is a receipt, not a transcript:

* payload keys, and only these: ``id`` (``<session_id>:<message_id>``),
  ``session_id``, ``ts`` (epoch seconds), ``status`` (``ok`` | ``error`` |
  ``cancelled``), the counters the turn's metadata really carries (``rounds``,
  ``tool_calls``, ``input_tokens``, ``output_tokens``, ``duration_s``; an
  unknown counter is left out, never invented), ``model`` and ``project_id``
  when there are ones;
* never the prompt, the reply, a title, file contents or credentials;
* never for an incognito turn;
* best effort and off the turn's path: the caller only builds a small dict and
  starts a daemon thread; the thread reuses Faustus's existing Hub client
  (``src.family_services.configure`` + ``hoard_link.family.emit``: same URL,
  same token discovery, ``HOARD_EVENTS=0`` switches it off), waits at most
  ``SEND_TIMEOUT_S``, and swallows every failure. A Hub that is down is simply
  not told (``family.emit`` never auto-starts it).
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

EVENT_TYPE = "faustus.turn.completed"
STATUSES = ("ok", "error", "cancelled")
SEND_TIMEOUT_S = 2.0

#: The whole contract: nothing outside this tuple ever leaves Faustus.
ALLOWED_KEYS = (
    "id", "session_id", "ts", "status",
    "rounds", "tool_calls", "input_tokens", "output_tokens", "duration_s",
    "model", "project_id",
)

_MAX_STR = 120


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _as_seconds(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, 2) if number >= 0 else None


def _as_label(value: Any) -> str:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return ""
    return str(value).strip()[:_MAX_STR]


def status_of(metadata: Optional[Dict[str, Any]]) -> str:
    """``cancelled`` for a stopped partial, ``error`` for a failed turn, else ``ok``."""
    md = metadata if isinstance(metadata, dict) else {}
    if md.get("stopped"):
        return "cancelled"
    if md.get("failed") or md.get("failure"):
        return "error"
    return "ok"


def build_payload(
    session_id: str,
    message_id: Any,
    metadata: Optional[Dict[str, Any]],
    *,
    status: Optional[str] = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """The receipt for one saved assistant message. Pure: reads only counters."""
    md = metadata if isinstance(metadata, dict) else {}
    harness = md.get("harness") if isinstance(md.get("harness"), dict) else {}
    sid = _as_label(session_id)
    ts = float(now if now is not None else time.time())
    mid = _as_label(message_id) or _as_label(md.get("run_id")) or str(int(ts * 1000))
    state = status if status in STATUSES else status_of(md)

    payload: Dict[str, Any] = {
        "id": f"{sid}:{mid}",
        "session_id": sid,
        "ts": round(ts, 3),
        "status": state,
    }

    rounds = _as_int(md.get("agent_rounds"))
    if not rounds:
        rounds = _as_int(harness.get("round_count")) or rounds
    if rounds is not None:
        payload["rounds"] = rounds

    tool_calls = _as_int(md.get("tool_calls"))
    if tool_calls is None:
        tool_calls = _as_int(harness.get("tool_calls"))
    if tool_calls is None and isinstance(md.get("tool_events"), list):
        tool_calls = len(md["tool_events"])
    if tool_calls is not None:
        payload["tool_calls"] = tool_calls

    for key in ("input_tokens", "output_tokens"):
        number = _as_int(md.get(key))
        if number is not None:
            payload[key] = number

    seconds = _as_seconds(md.get("response_time"))
    if seconds is None:
        seconds = _as_seconds(md.get("total_time"))
    if seconds is not None:
        payload["duration_s"] = seconds

    model = _as_label(md.get("model") or md.get("requested_model"))
    if model:
        payload["model"] = model
    project_id = _as_label(harness.get("project_id") or md.get("project_id"))
    if project_id:
        payload["project_id"] = project_id
    return payload


def _send_to_hub(payload: Dict[str, Any], owner: Optional[str] = None) -> bool:
    """Blocking, runs on the daemon thread only. Never raises."""
    try:
        from src import family_services
        from src.hoard_link import family

        family_services.configure()
        if not family.status().get("enabled", True):
            return False
        body = dict(payload)
        if "project_id" not in body:
            try:
                from services.projects import project_context_for_session

                project_id = _as_label(project_context_for_session(body["session_id"], owner).project_id)
                if project_id:
                    body["project_id"] = project_id
            except Exception:  # noqa: BLE001 - no project is the common case
                pass
        body = {key: body[key] for key in ALLOWED_KEYS if key in body}
        return bool(family.emit(EVENT_TYPE, body, block=True, timeout=SEND_TIMEOUT_S))
    except Exception:  # noqa: BLE001 - a receipt must never hurt anything
        logger.debug("turn receipt not delivered", exc_info=True)
        return False


#: Tests swap this one to keep a unit test off the user's running Hub.
_send = _send_to_hub


def _spawn(fn, *args) -> None:
    threading.Thread(target=fn, args=args, name="faustus-turn-receipt", daemon=True).start()


def emit_turn_receipt(
    session_id: str,
    message_id: Any,
    metadata: Optional[Dict[str, Any]],
    *,
    status: Optional[str] = None,
    incognito: bool = False,
    owner: Optional[str] = None,
) -> bool:
    """Queue the receipt of a persisted turn and return at once.

    True when it was handed to the background sender; False when there was
    nothing to send (incognito, no session) or anything went wrong while
    preparing it. Never raises and never waits on the network.
    """
    if incognito or not str(session_id or "").strip():
        return False
    try:
        payload = build_payload(session_id, message_id, metadata, status=status)
        _spawn(_dispatch, payload, owner)
        return True
    except Exception:  # noqa: BLE001
        logger.debug("turn receipt skipped", exc_info=True)
        return False


def _dispatch(payload: Dict[str, Any], owner: Optional[str]) -> None:
    try:
        _send(payload, owner)
    except Exception:  # noqa: BLE001
        logger.debug("turn receipt dispatch failed", exc_info=True)
