"""effect_tools.py — which tool calls are non-repeatable external effects.

The effect outbox (``src/effect_outbox.py``) is the write-ahead record for
effects that leave the process and cannot be taken back. This module decides,
for one tool call, whether it is such an effect and describes it (kind,
destination, how the transport reports back). It never executes anything.

Transports:

* ``joined``: a transport that runs inside this process (WhatsApp bridge,
  the integration HTTP client) or inside the mail MCP server reads the
  admission from the effect context (``admission_id``) and settles the row
  itself with what it can actually tell (connection refused vs connection cut
  after the payload).
* ``opaque``: an MCP connector whose internals are not visible. The tool layer
  marks ``dispatching`` right before the handler and settles from the result;
  an error result without an explicit "not dispatched" marker is
  ``outcome_unknown``.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Optional

from src.effect_outbox import arguments_digest

#: Email tools whose effect leaves the process and cannot be repeated.
#: Mailbox flag/move/delete operations are repeatable and stay out.
EMAIL_EFFECT_TOOLS = frozenset({"send_email", "reply_to_email", "unsubscribe_email"})
#: A reaction replaces the previous one, so only the message itself is an effect.
WHATSAPP_EFFECT_TOOLS = frozenset({"whatsapp_send"})

_SAFE_HTTP_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

#: MCP servers that ship with the app. Their own tools are local state, not
#: third-party connector writes.
_BUILTIN_MCP_SERVERS = frozenset({
    "brain", "code_graph", "context_engine", "email", "image_gen", "memory", "prior_art", "rag",
    "research_prune", "workers", "workflows", "browser", "devtools", "chrome-devtools", "playwright",
})

_WRITE_VERBS = frozenset({
    "send", "post", "create", "update", "delete", "remove", "publish", "write", "upload", "add",
    "set", "submit", "reply", "forward", "invite", "cancel", "book", "pay", "transfer", "merge",
    "push", "comment", "assign", "move", "archive", "close", "reopen", "tweet", "share", "schedule",
    "approve", "reject", "trigger", "run", "execute", "commit", "edit", "patch", "put", "insert",
})


@dataclass(frozen=True)
class EffectDescriptor:
    kind: str
    tool: str
    destination: str
    transport: str  # "joined" | "opaque"
    arguments_sha256: str


def _args(content: Any) -> dict:
    if isinstance(content, dict):
        return content
    try:
        parsed = json.loads(str(content or "") or "{}")
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _bare_email_name(tool: str) -> str:
    return tool[len("mcp__email__"):] if tool.startswith("mcp__email__") else tool


def _destination(args: dict, *keys: str) -> str:
    for key in keys:
        value = args.get(key)
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(v) for v in value)
        if value:
            return str(value)[:300]
    return ""


def describe_call(tool_type: Any, content: Any) -> Optional[EffectDescriptor]:
    """Describe ``tool_type(content)`` when it is a non-repeatable external
    effect, else ``None`` (reads, local writes, internal tools)."""
    tool = str(tool_type or "")
    if not tool:
        return None
    digest = arguments_digest(str(content or ""))
    bare = _bare_email_name(tool)
    if bare in EMAIL_EFFECT_TOOLS and (tool == bare or tool.startswith("mcp__email__")):
        args = _args(content)
        return EffectDescriptor(f"email.{bare}", tool, _destination(args, "to", "uid", "message_id"),
                                "joined", digest)
    if tool in WHATSAPP_EFFECT_TOOLS:
        args = _args(content)
        return EffectDescriptor(f"whatsapp.{tool.split('_', 1)[1]}", tool,
                                _destination(args, "to", "chat", "message_id"), "joined", digest)
    if tool == "api_call":
        args = _args(content)
        method = str(args.get("method") or "GET").upper()
        if method in _SAFE_HTTP_METHODS:
            return None
        return EffectDescriptor(f"http.{method.lower()}", tool,
                                f"{args.get('integration', '')} {str(args.get('path') or '')}".strip()[:300],
                                "joined", digest)
    if tool.startswith("mcp__"):
        parts = tool.split("__", 2)
        if len(parts) == 3 and parts[1] not in _BUILTIN_MCP_SERVERS:
            verb = re.split(r"[_\-.]", parts[2].lower(), maxsplit=1)[0]
            if verb in _WRITE_VERBS:
                return EffectDescriptor(f"connector.{parts[1]}", tool, parts[2], "opaque", digest)
    return None


# ---------------------------------------------------------------------------
# Verdict -> result dict
# ---------------------------------------------------------------------------

_UNCERTAINTY_ACTIONS = {
    "email": "look for the message in the Sent folder by its identifier before sending again",
    "whatsapp": "check the chat for the message before sending again",
    "http": "read the remote state before repeating the request",
    "connector": "read the current state of the connector before repeating the action",
}


def reconcile_action_for(kind: str) -> str:
    return _UNCERTAINTY_ACTIONS.get(str(kind).split(".", 1)[0], "read the current state before retrying")


def apply_verdict(result: dict, effect: dict) -> dict:
    """Write what the outbox row knows into ``result`` (additive; called after
    the handler returns, before the result is normalized).

    A transport that settled the row precisely wins over the text the tool
    wrapper made of the exception: ``outcome_unknown`` and ``partial`` become
    the result status, ``failed_before_effect`` adds an explicit marker.
    """
    if not isinstance(result, dict) or not effect:
        return result
    state = effect.get("state")
    result["effect_id"] = effect.get("id", "")
    result["attempt_id"] = effect.get("attempt_id") or result.get("attempt_id", "")
    if state in ("succeeded",) or (state == "reconciled" and effect.get("effect_certainty") == "confirmed"):
        if not result.get("error") and result.get("status") in (None, "", "succeeded"):
            return result
    if state == "outcome_unknown" or (state == "cancelled" and effect.get("effect_certainty") == "unknown"):
        result["status"] = "outcome_unknown"
        result["outcome_unknown"] = True
        result.setdefault("uncertainty", {
            "reason": str(effect.get("reason") or "the destination may have acted; no confirmation arrived")[:500],
            "reconcile_action": reconcile_action_for(effect.get("kind", "")),
        })
    elif state == "partial":
        result["status"] = "partial"
        result.setdefault("uncertainty", {
            "reason": str(effect.get("reason") or "only part of the effect landed")[:500],
            "reconcile_action": reconcile_action_for(effect.get("kind", "")),
        })
    elif state == "failed_before_effect":
        result["effect_not_dispatched"] = True
    return result


# ---------------------------------------------------------------------------
# Admission of one tool call
# ---------------------------------------------------------------------------

@dataclass
class Admission:
    descriptor: EffectDescriptor
    row: dict
    attempt_id: str
    deduplicated: bool = False

    @property
    def id(self) -> str:
        return str(self.row.get("id") or "")


def admit_call(desc: EffectDescriptor, *, owner: str, session_id: str, run_id: str,
               call_id: str) -> Admission:
    """Commit and read back the intent of this call. Raises
    :class:`~src.effect_outbox.IntentNotPersisted` when that is impossible: the
    handler must not run then."""
    from src import effect_outbox
    row = effect_outbox.open_admission(
        kind=desc.kind, tool=desc.tool, owner=str(owner or ""), destination=desc.destination,
        arguments_sha256=desc.arguments_sha256, session_id=str(session_id or ""), run_id=str(run_id or ""),
        call_id=str(call_id or ""))
    return Admission(desc, row, str(row.get("attempt_id") or ""), bool(row.get("deduplicated")))


def context_for(adm: Admission, *, owner: str, session_id: str, run_id: str, call_id: str):
    from src import effect_outbox
    return effect_outbox.EffectContext(
        owner=str(owner or ""), session_id=str(session_id or ""), run_id=str(run_id or ""),
        call_id=str(call_id or ""), attempt_id=adm.attempt_id, tool=adm.descriptor.tool,
        admission_id=adm.id)


def hidden_argument(adm: Admission, ctx) -> dict:
    """The value the MCP mail server receives as ``_faustus_effect``."""
    return {"admission_id": adm.id, "attempt_id": adm.attempt_id, "run_id": ctx.run_id,
            "session_id": ctx.session_id, "call_id": ctx.call_id, "tool": adm.descriptor.tool}


def deduplicated_result(adm: Admission) -> dict:
    """What a repeated call of an already-dispatched effect returns: the state
    of the first attempt, never a second dispatch."""
    row = adm.row
    state = row.get("state")
    base = {"deduplicated": True, "effect_id": row.get("id", ""), "attempt_id": row.get("attempt_id", ""),
            "exit_code": 0}
    if state == "succeeded" or (state == "reconciled" and row.get("effect_certainty") == "confirmed"):
        return {**base, "output": "This exact call was already delivered by an earlier attempt; "
                                  "it was not sent again."}
    result = {**base, "exit_code": 1, "error": "An earlier attempt of this exact call may already have been "
              "delivered; it was not sent again. Reconcile it before repeating."}
    return apply_verdict(result, row)


def settle_admission(adm: Admission, result: dict, *, result_status: str,
                     exception: BaseException | None = None) -> dict:
    """Finalize the row after the handler returned (or raised) and write the
    verdict into ``result``. Returns the final row."""
    from src import effect_outbox
    error = str((result or {}).get("error") or "") if isinstance(result, dict) else ""
    try:
        if exception is not None and isinstance(exception, (KeyboardInterrupt, SystemExit)) or (
                exception is not None and type(exception).__name__ == "CancelledError"):
            row = effect_outbox.get(adm.id) or {}
            if row.get("state") == "dispatching":
                row = effect_outbox.cancel(adm.id, reason="cancelled while dispatching")
            elif row.get("state") in ("prepared", "admitted"):
                row = effect_outbox.cancel(adm.id, reason="cancelled before dispatch")
            return row
        row = effect_outbox.finalize_admission(
            adm.id, result_status=result_status if exception is None else "failed",
            error=error or (f"{type(exception).__name__}: {exception}" if exception else ""),
            explicit_not_dispatched=bool(isinstance(result, dict) and result.get("effect_not_dispatched")),
            external_ref=str((result or {}).get("message_id") or (result or {}).get("id") or "")
            if isinstance(result, dict) else "")
    except Exception:  # noqa: BLE001 - recording must not turn a delivered effect into a crash
        import logging
        logging.getLogger(__name__).warning("effect admission %s could not be finalized", adm.id, exc_info=True)
        return {}
    if isinstance(result, dict) and row:
        apply_verdict(result, row)
    return row
