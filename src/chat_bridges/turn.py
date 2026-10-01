"""src/chat_bridges/turn.py — one Faustus turn on behalf of a chat bridge.

A turn from a chat app is an ordinary turn: the message is saved in a real
session (visible in Studio's conversation list), the agent loop runs against
that session's history, and the answer and its tool cards are saved back the
same way a background follow-up does it (`bg_monitor._run_followup`). This
module is deliberately the only place that knows how; the bridge itself only
sees :func:`run_turn`, which makes it easy to replace in tests.

Safety, in order of importance:

* the incoming text is saved with `trusted: False` / `tool_gate_untrusted`, the
  marker the tool gate already reads: a state-changing tool then stops at an
  exact-approval card instead of running, exactly as after any other outside
  text. Nothing here approves anything;
* a card that stops the turn comes back in `TurnResult.approvals` so the bridge
  can say what is waiting and where to answer (Studio);
* only files in the generated-images folder are returned as images.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: A turn from a chat app is bounded like any headless turn.
TURN_TIMEOUT_S = 1800.0
#: Rounds when the `agent_max_rounds` setting cannot be read.
DEFAULT_MAX_ROUNDS = 25
MAX_IMAGES = 4
_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
_GENERATED_URL = re.compile(r"/api/generated-image/([^?#/\s]+)")


class TurnUnavailable(RuntimeError):
    """The turn could not start (no owner, no model, no session manager)."""


@dataclass
class TurnRequest:
    owner: str
    session_id: str
    text: str
    mode: str = "agent"            # "agent" (tools) | "chat" (the model alone)
    model: str = ""                # "" = the session's own model
    source: str = "telegram"
    timeout_s: float = TURN_TIMEOUT_S


@dataclass
class TurnResult:
    text: str = ""
    images: List[str] = field(default_factory=list)       # local file paths
    approvals: List[Dict[str, Any]] = field(default_factory=list)
    error: str = ""
    stop_reason: str = ""
    model: str = ""
    tool_calls: int = 0


# ── owner, model, session ────────────────────────────────────────────────

def resolve_owner(configured: str = "") -> str:
    """The Faustus account that owns bridge conversations.

    The configured name when it is a real account; otherwise the first
    administrator (the account ownerless events and legacy data already go to,
    `event_bus._resolve_event_owner`); with login switched off, the local owner.
    Empty when no account exists to own anything.
    """
    from src.owner_identity import auth_disabled, effective_storage_owner
    wanted = (configured or "").strip()
    users: Dict[str, Any] = {}
    try:
        from src.constants import AUTH_FILE
        with open(AUTH_FILE, "r", encoding="utf-8") as stream:
            users = json.load(stream).get("users") or {}
    except Exception:  # noqa: BLE001 - no auth file: single-user mode or first run
        users = {}
    if wanted:
        for name in users:
            if str(name).lower() == wanted.lower():
                return str(name)
        if not users and auth_disabled():
            return wanted
    try:
        from src.event_bus import _resolve_event_owner
        first = _resolve_event_owner(None)
        if first:
            return str(first)
    except Exception:  # noqa: BLE001
        pass
    return str(effective_storage_owner(None) or "")


def _default_route(owner: str, model: str = ""):
    from src.endpoint_resolver import resolve_endpoint
    url, default_model, headers = resolve_endpoint("default", owner=owner or None)
    if not url:
        raise TurnUnavailable("no default chat model is configured (Settings → Models)")
    chosen = (model or "").strip() or str(default_model or "")
    if not chosen:
        raise TurnUnavailable("no model is configured for the default endpoint")
    return url, chosen, headers


def default_model_name(owner: str, model: str = "") -> str:
    try:
        return _default_route(owner, model)[1]
    except Exception:  # noqa: BLE001 - /status must answer even with no model
        return (model or "").strip()


def _manager():
    from src.ai_interaction import get_session_manager
    sm = get_session_manager()
    if sm is None:
        raise TurnUnavailable("Faustus is still starting (no session manager yet)")
    return sm


def get_session(session_id: str):
    try:
        return _manager().get_session(session_id)
    except TurnUnavailable:
        raise
    except Exception:  # noqa: BLE001 - deleted or never existed
        return None


def session_exists(session_id: str, owner: str = "") -> bool:
    sess = get_session(session_id)
    if sess is None:
        return False
    return not owner or (getattr(sess, "owner", None) or "") == owner


def create_session(owner: str, name: str, model: str = "") -> str:
    """A new conversation named `name`, on the default endpoint."""
    url, chosen, headers = _default_route(owner, model)
    sm = _manager()
    sid = str(uuid.uuid4())
    session = sm.create_session(session_id=sid, name=name[:120], endpoint_url=url, model=chosen,
                                rag=False, owner=owner or None)
    if headers:
        try:
            session.headers = dict(headers)
            from routes.session_routes import _persist_session_headers
            _persist_session_headers(sid, session.headers)
        except Exception:  # noqa: BLE001 - the header is re-resolved per turn below
            logger.debug("telegram bridge: could not persist endpoint headers", exc_info=True)
    return sid


def local_image_path(url_or_path: str) -> Optional[str]:
    """A local file for a generated-image URL, or None.

    Only the generated-images folder qualifies: a tool result must not be able
    to make the bridge upload some other file from the machine."""
    value = str(url_or_path or "").strip()
    match = _GENERATED_URL.search(value)
    name = match.group(1) if match else ""
    if not name:
        return None
    from src.generated_images import resolve_generated_image_path
    try:
        path = resolve_generated_image_path(name)
    except Exception:  # noqa: BLE001 - HTTPException: invalid name or missing file
        return None
    if Path(path).suffix.lower() not in _IMAGE_EXT:
        return None
    return str(path)


# ── the turn ─────────────────────────────────────────────────────────────

def _public_card(payload: Dict[str, Any]) -> Dict[str, Any]:
    options = [str(o.get("label") or "") for o in (payload.get("options") or []) if isinstance(o, dict)]
    return {
        "kind": str(payload.get("kind") or "question"),
        "question": str(payload.get("question") or payload.get("description") or "")[:600],
        "options": [o for o in options if o][:6],
    }


def _note_card(acc: Dict[str, Any], payload: Dict[str, Any]) -> None:
    card = _public_card(payload)
    if card not in acc["approvals"]:
        acc["approvals"].append(card)


async def _agent_turn(sess, messages, req: TurnRequest, acc: Dict[str, Any]) -> None:
    from src.agent_loop import stream_agent_loop
    try:
        from src.settings import get_setting
        max_rounds = int(get_setting("agent_max_rounds", DEFAULT_MAX_ROUNDS) or DEFAULT_MAX_ROUNDS)
    except Exception:  # noqa: BLE001
        max_rounds = DEFAULT_MAX_ROUNDS
    url, model, headers = sess.endpoint_url, sess.model, getattr(sess, "headers", None)
    if req.model:
        url, model, headers = _default_route(req.owner, req.model)
    acc["model"] = model
    round_num = 1
    stream = stream_agent_loop(
        url, model, messages, headers=headers,
        context_length=getattr(sess, "context_length", 0) or 0,
        session_id=sess.id, max_rounds=max(1, max_rounds), owner=req.owner or None,
    )
    try:
        async for chunk in stream:
            if not isinstance(chunk, str):
                continue
            if chunk.startswith("event: error"):
                acc["error"] = chunk[:300]
                continue
            if not chunk.startswith("data: "):
                continue
            body = chunk[6:].strip()
            if not body or body == "[DONE]":
                continue
            try:
                event = json.loads(body)
            except (ValueError, TypeError):
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            if "delta" in event:
                delta = event.get("delta")
                if isinstance(delta, str) and not event.get("thinking"):
                    acc["text"] += delta
            elif kind == "agent_step":
                round_num = event.get("round", round_num)
            elif kind == "tool_output":
                acc["tool_calls"] += 1
                tool_event = {"round": round_num, "tool": event.get("tool"), "command": event.get("command"),
                              "output": event.get("output"), "exit_code": event.get("exit_code")}
                card = event.get("ask_user")
                if isinstance(card, dict):
                    tool_event["ask_user"] = card
                    _note_card(acc, card)
                acc["tool_events"].append(tool_event)
            elif kind == "ask_user" and isinstance(event.get("data"), dict):
                _note_card(acc, event["data"])
            elif kind == "generated_image":
                path = local_image_path(str(event.get("url") or event.get("image_url") or ""))
                if path and path not in acc["images"]:
                    acc["images"].append(path)
            elif kind == "error" and event.get("message"):
                acc["error"] = str(event.get("message"))[:300]
            elif kind == "metrics" and isinstance(event.get("data"), dict):
                acc["metrics"] = event["data"]
    finally:
        try:
            await stream.aclose()
        except Exception:  # noqa: BLE001 - closing a finished generator is best effort
            pass


async def _chat_turn(sess, messages, req: TurnRequest, acc: Dict[str, Any]) -> None:
    from src.llm_core import llm_call_async
    url, model, headers = sess.endpoint_url, sess.model, getattr(sess, "headers", None)
    if req.model:
        url, model, headers = _default_route(req.owner, req.model)
    acc["model"] = model
    acc["text"] = str(await llm_call_async(url, model, messages, headers=headers, session_id=sess.id) or "")


async def run_turn(req: TurnRequest) -> TurnResult:
    """Save the message, run the turn on the mapped session, save the answer."""
    from core.models import ChatMessage
    sm = _manager()
    sess = get_session(req.session_id)
    if sess is None:
        raise TurnUnavailable("the conversation no longer exists")
    from src import agent_runs
    if req.session_id in agent_runs.active_session_ids():
        return TurnResult(error="That conversation has a turn running in Studio right now. Try again in a moment.")
    agent_runs.mark_busy(req.session_id)
    acc: Dict[str, Any] = {"text": "", "images": [], "approvals": [], "tool_events": [],
                           "tool_calls": 0, "error": "", "model": sess.model, "metrics": {}}
    stop_reason = ""
    try:
        sm.add_message(sess.id, ChatMessage("user", req.text, metadata={
            "source": req.source,
            # Text from outside the app: it arms the tool gate like any other
            # outside text, so a state-changing tool waits for a person.
            "trusted": False, "provenance_origin": "external", "tool_gate_untrusted": True,
        }))
        messages = sess.get_context_messages()
        runner = _chat_turn if req.mode == "chat" else _agent_turn
        try:
            await asyncio.wait_for(runner(sess, messages, req, acc), timeout=max(1.0, req.timeout_s))
        except asyncio.TimeoutError:
            stop_reason = "timeout"
            acc["error"] = acc["error"] or "The turn took too long and was stopped."
        except TurnUnavailable:
            raise
        except asyncio.CancelledError:
            stop_reason = "cancelled"
            raise
        except Exception as exc:  # noqa: BLE001 - a failed turn is a message, not a crash
            logger.warning("telegram bridge: turn failed: %s", exc)
            acc["error"] = f"{type(exc).__name__}: {exc}"[:300]
        metadata: Dict[str, Any] = {"model": acc["model"], "source": req.source}
        if acc["tool_events"]:
            metadata["tool_events"] = acc["tool_events"]
        text = acc["text"]
        if text.strip() or acc["tool_events"]:
            sm.add_message(sess.id, ChatMessage("assistant", text, metadata=metadata))
    finally:
        agent_runs.clear_busy(req.session_id)
    return TurnResult(text=acc["text"], images=acc["images"][:MAX_IMAGES], approvals=acc["approvals"],
                      error=acc["error"], stop_reason=stop_reason or ("approval" if acc["approvals"] else ""),
                      model=acc["model"], tool_calls=acc["tool_calls"])
