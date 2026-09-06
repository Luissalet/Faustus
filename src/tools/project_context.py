"""Project-context mutations: the `manage_project_context` tool (plan §9, §21).

Why this module is deliberately thin
------------------------------------
`project_context` (dispatched in ``src/tool_execution.py``) READS what a
project has attached. This is the tool that CHANGES it, and it is a separate
tool on purpose: permissions, auditing and error messages all differ between
"show me the project's sources" and "make this document part of the project
from now on".

Everything that decides an outcome — ownership, deduplication, source
validation, revision, idempotency — lives in
``src.project_context.ProjectContextService`` (plan §23). This file translates
tool arguments into that service's vocabulary and its results back into the
`{...}` dict the dispatcher returns. It holds no policy of its own.

The two rules this module exists to enforce
-------------------------------------------
1. **The project is resolved on the server, from ``session_id``.** A
   ``project_id`` in the arguments is ignored, and the result says it was
   ignored. A model that can name the destination project can move one
   project's documents into another, and nothing downstream repairs that
   (plan §5.2).
2. **``source.kind="active_document"`` is resolved through the turn-reference
   registry, never guessed.** When two candidates are equally plausible this
   returns ``needs_clarification`` with both and mutates nothing: choosing the
   more recent of two documents created by the same operation attaches the
   wrong half of a pair, and the user has no way to notice (plan §10, §22).

Failures come back as ``{"error": ..., "exit_code": 1}`` like every other tool
in this package; nothing raises at the dispatcher.
"""

import logging
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from src.contracts.base import ContractError
from src.project_context.models import ActorRef, ProjectContextError, SourceRef
from src.project_context.references import registry
from src.project_context.service import service
from src.tools._common import _parse_tool_args

logger = logging.getLogger(__name__)

__all__ = ["do_manage_project_context"]

#: The actions the tool accepts. `list` is here as well as in the plan's
#: attach/detach/update/refresh/inspect set because reading a project's links
#: back is how an agent checks its own work before it claims it attached
#: something.
ACTIONS: Tuple[str, ...] = ("attach", "detach", "update", "refresh", "inspect", "list")

#: Verbs models actually emit, mapped to the action they mean. Translation,
#: not guessing: every one of these has a single unambiguous target.
_ACTION_ALIASES: Dict[str, str] = {
    "add": "attach", "link": "attach", "save": "attach", "create": "attach",
    "include": "attach", "remember": "attach",
    "remove": "detach", "unlink": "detach", "delete": "detach", "forget": "detach",
    "edit": "update", "set": "update", "patch": "update", "configure": "update",
    "reindex": "refresh", "reload": "refresh", "sync": "refresh",
    "status": "inspect", "show": "inspect", "get": "inspect", "read": "inspect",
    "ls": "list", "links": "list", "sources": "list",
}

#: Deictic source kinds — "this document" — and the canonical kind each one
#: resolves to. What gets STORED is always the canonical kind and the real id;
#: the shortcut only saves a small model from having to copy an id it can see
#: (plan §10). It never bypasses validation: the resolved id is checked against
#: the source itself by the service, exactly like a typed-in one.
_DEICTIC_KINDS: Dict[str, str] = {
    "active_document": "document",
    "active": "document",
    "this_document": "document",
    "current_document": "document",
}

#: Fields ``update`` may carry through to the service. It re-checks them; this
#: list only keeps a stray argument from being mistaken for a patch.
_PATCH_KEYS: Tuple[str, ...] = (
    "label", "role", "tags", "summary", "retrieval_policy", "version_policy",
    "pinned_version", "access_mode", "enabled",
)


def _error(message: str, **extra: Any) -> Dict[str, Any]:
    """The one failure shape. Structured, never an exception at the
    dispatcher, and never a bare string the agent has to parse."""
    out: Dict[str, Any] = {"ok": False, "error": message, "exit_code": 1}
    out.update({k: v for k, v in extra.items() if v not in ("", None)})
    return out


def _tags(value: Any) -> Optional[List[str]]:
    """Tags as a list, from a list or from the comma string models send."""
    if value is None:
        return None
    if isinstance(value, str):
        return [t.strip() for t in value.split(",") if t.strip()]
    if isinstance(value, Sequence):
        return [str(t).strip() for t in value if str(t).strip()]
    return None


def _pinned(value: Any) -> Optional[int]:
    if value in (None, "", "null"):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _project_block(project: Mapping[str, Any]) -> Dict[str, str]:
    return {"id": str(project.get("id") or ""), "name": str(project.get("name") or "")}


def _candidate(ref) -> Dict[str, str]:
    """One clarification candidate: enough for the user to tell them apart,
    and nothing that came from inside the source."""
    return {"kind": ref.kind, "id": ref.ref_id, "label": ref.label or "(untitled)",
            "created_by": ref.source_tool, "relation": ref.relation}


def _raw_source(args: Mapping[str, Any]) -> Dict[str, Any]:
    """The source block, from ``source`` or from flat top-level keys.

    Small models routinely emit ``{"action":"attach","kind":"document",
    "id":"..."}`` instead of nesting. Accepting both is translation; it does
    not decide anything the nested form would not.
    """
    source = args.get("source")
    if isinstance(source, Mapping):
        return dict(source)
    if isinstance(source, str) and source.strip():
        # A bare string is only usable when the kind is stated elsewhere.
        return {"kind": args.get("kind") or "", "id": source.strip()}
    return {"kind": args.get("kind") or "", "id": args.get("id") or args.get("ref_id") or "",
            "path": args.get("path") or ""}


def _resolve_source(
    raw: Mapping[str, Any], args: Mapping[str, Any], *, session_id: str, owner: str,
    turn_id: str,
) -> "Tuple[Optional[SourceRef], Optional[Dict[str, Any]]]":
    """``(source, early_result)``. Exactly one of the two is set.

    A deictic kind is resolved here, against the turn-reference registry, and
    what comes out is the canonical kind with a real id. An ambiguous answer
    ends the call with ``needs_clarification`` **before** anything is written.
    """
    kind = str(raw.get("kind") or "").strip().lower()
    if kind not in _DEICTIC_KINDS:
        try:
            return SourceRef.parse({"kind": kind, "id": str(raw.get("id") or "").strip(),
                                    "path": str(raw.get("path") or "").strip()}), None
        except ContractError as exc:
            return None, _error(str(exc), action="attach")

    canonical = _DEICTIC_KINDS[kind]
    hint: Dict[str, Any] = {"kind": canonical, "turn_id": turn_id}
    explicit = str(raw.get("id") or "").strip()
    if explicit:
        hint["id"] = explicit
    label = str(raw.get("label") or args.get("label") or "").strip()
    if label:
        hint["label"] = label

    chosen, candidates = registry().resolve(hint, session_id=session_id, owner=owner)
    if chosen is not None:
        return SourceRef(kind=canonical, id=chosen.ref_id), None
    if candidates:
        # Two documents created by the same operation are two candidates, not
        # a race won by the later one (plan §10). Nothing is mutated.
        return None, {
            "ok": False,
            "needs_clarification": True,
            "action": "attach",
            "candidates": [_candidate(c) for c in candidates],
            "message": ("Several sources match 'this document' in this chat. Ask the user "
                        "which one, then call again with source.id set."),
            "exit_code": 0,
        }
    return None, _error(
        "There is no active document in this chat to attach. Ask the user which source "
        "they mean and pass source.id (or source.path for a file or folder).",
        action="attach",
    )


async def do_manage_project_context(
    content: str,
    *,
    session_id: str = "",
    owner: Optional[str] = None,
    run_id: str = "",
    turn_id: str = "",
) -> Dict[str, Any]:
    """Attach, detach, update, refresh, inspect or list this project's sources.

    ``session_id`` is what decides the project; a ``project_id`` in ``content``
    is read only so the result can report that it was ignored.
    """
    try:
        args = _parse_tool_args(content)
    except ValueError as exc:
        return _error(f"Invalid JSON arguments: {exc}")

    raw_action = str(args.get("action") or "").replace("-", "_").strip().lower()
    action = _ACTION_ALIASES.get(raw_action, raw_action)
    if action not in ACTIONS:
        return _error(
            f"Action must be one of: {', '.join(ACTIONS)}.",
            action=(raw_action or "(missing)"),
        )

    session_id = str(session_id or "")
    from services.projects import project_for_session
    project = project_for_session(session_id, owner)
    if not project:
        return _error(
            "This chat is not attached to a project, so it has no durable project "
            "context to change. Ask the user to move this chat into a project first.",
            action=action,
        )

    # Rule 1 (plan §5.2). Read, reported, and never used: the destination is
    # the session's project, whatever the arguments say.
    claimed_project = str(args.get("project_id") or "").strip()
    ignored: Dict[str, Any] = {}
    if claimed_project:
        logger.info(
            "manage_project_context: ignoring project_id %r from tool arguments; "
            "session %s resolves to project %s",
            claimed_project, session_id, project.get("id"),
        )
        ignored = {
            "ignored_project_id": claimed_project,
            "ignored_reason": ("the project is resolved from this chat on the server; "
                               "a project id in the arguments is never used"),
        }

    actor = ActorRef(kind="agent", session_id=session_id, run_id=str(run_id or ""))
    effective_owner = str(owner or "")
    try:
        result = _dispatch(action, args, project=project, owner=effective_owner,
                           actor=actor, session_id=session_id, turn_id=str(turn_id or ""))
    except (ProjectContextError, ContractError) as exc:
        return {**_error(str(exc), action=action), **ignored}
    except Exception as exc:  # noqa: BLE001 - a tool never raises at the dispatcher
        logger.exception("manage_project_context failed")
        return {**_error(f"manage_project_context failed: {exc}", action=action), **ignored}
    return {**result, **ignored}


def _flag(value: Any, default: bool = True) -> bool:
    """`bool("false")` is True, and models send booleans as strings."""
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on", "si", "sí"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return default


def _patch_from(args: Mapping[str, Any]) -> Dict[str, Any]:
    """The patch an ``update`` carries. Only keys the caller actually sent —
    an absent field must stay as it is, not be reset to a default."""
    patch: Dict[str, Any] = {}
    for key in _PATCH_KEYS:
        if key not in args:
            continue
        value = args[key]
        if key == "tags":
            tags = _tags(value)
            if tags is not None:
                patch[key] = tags
        elif key == "pinned_version":
            patch[key] = _pinned(value)
        elif key == "enabled":
            patch[key] = _flag(value)
        else:
            patch[key] = str(value or "")
    return patch


def _envelope(payload: Dict[str, Any], *, project: Dict[str, str], action: str,
              ok: bool, message: str) -> Dict[str, Any]:
    """A service result, dressed as a tool result.

    ``exit_code`` and ``error`` are what the agent reads; the untouched service
    fields ride along so a confirmation can be built from what actually
    happened rather than from the fact that the call returned (plan §9).
    """
    payload = dict(payload)
    # RefreshResult carries no action of its own; the others name what they did
    # ("attached" vs "deduplicated"), which is the distinction the agent must
    # repeat rather than flatten.
    payload.setdefault("action", action)
    payload["project"] = project
    payload["ok"] = ok
    payload["exit_code"] = 0 if ok else 1
    if ok:
        payload.pop("error", None)
    else:
        # `inspect` already carries a state; the mutation results carry an
        # error code instead. Keep whichever one the service actually set.
        payload["state"] = payload.get("state") or payload.get("error") or ""
        payload["error"] = message
    return payload


def _dispatch(action: str, args: Mapping[str, Any], *, project: Mapping[str, Any],
              owner: str, actor: ActorRef, session_id: str,
              turn_id: str) -> Dict[str, Any]:
    """One action, translated both ways. No decision of its own lives here."""
    svc = service()
    block = _project_block(project)
    link_id = str(args.get("link_id") or "").strip()
    if not link_id and action != "attach":
        # Models put the link id in `id` about as often as in `link_id`. For
        # `attach`, `id` is the SOURCE, so the fallback is deliberately absent.
        link_id = str(args.get("id") or "").strip()

    if action == "attach":
        source, early = _resolve_source(_raw_source(args), args, session_id=session_id,
                                        owner=owner, turn_id=turn_id)
        if early is not None:
            return {**early, "project": block}
        result = svc.attach(
            project=project, owner=owner, source=source, actor=actor,
            retrieval_policy=str(args.get("retrieval_policy") or "auto").strip().lower(),
            version_policy=str(args.get("version_policy") or "latest").strip().lower(),
            pinned_version=_pinned(args.get("pinned_version")),
            role=str(args.get("role") or "reference").strip().lower(),
            label=str(args.get("label") or "").strip(),
            tags=_tags(args.get("tags")),
            access_mode=str(args.get("access_mode") or "read_only").strip().lower(),
        )
        # The locator in the failure message is the caller's own argument, so
        # it discloses nothing; the service's message never names a title or a
        # path belonging to somebody else (plan §17).
        return _envelope(
            result.to_dict(), project=block, action=action, ok=result.ok,
            message=f"Cannot attach {source.kind} {source.locator!r}: {result.message}",
        )

    if action == "detach":
        if not link_id:
            return _error("detach needs the link_id of the link to remove; "
                          "call action='list' to see them.", action=action)
        result = svc.detach(project=project, owner=owner, link_id=link_id, actor=actor)
        return _envelope(result.to_dict(), project=block, action=action, ok=result.ok,
                         message=f"Cannot detach {link_id!r}: {result.message}")

    if action == "update":
        if not link_id:
            return _error("update needs the link_id of the link to change.", action=action)
        patch = _patch_from(args)
        if not patch:
            return _error(
                "update needs at least one field to change: " + ", ".join(_PATCH_KEYS),
                action=action, link_id=link_id,
            )
        link = svc.update(project=project, owner=owner, link_id=link_id, patch=patch,
                          actor=actor)
        return {"ok": True, "action": "updated", "project": block,
                "link": link.to_dict(), "updated_fields": sorted(patch),
                "message": f"{link.label!r} updated: {', '.join(sorted(patch))}.",
                "exit_code": 0}

    if action == "refresh":
        if not link_id:
            return _error("refresh needs the link_id of the link to re-check.",
                          action=action)
        result = svc.refresh(project=project, owner=owner, link_id=link_id, actor=actor)
        return _envelope(result.to_dict(), project=block, action=action, ok=result.ok,
                         message=f"Cannot refresh {link_id!r}: {result.message}")

    if action == "inspect":
        if not link_id:
            return _error("inspect needs the link_id of the link to look at.",
                          action=action)
        status = svc.inspect(project=project, owner=owner, link_id=link_id)
        ok = status.state == "ok"
        payload = status.to_dict()
        payload["action"] = "inspect"
        return _envelope(payload, project=block, action=action, ok=ok,
                         message=f"Cannot inspect {link_id!r}: {status.message}")

    links = svc.list(project=project, owner=owner,
                     status=str(args.get("index_status") or "").strip() or None,
                     kind=str(args.get("kind") or "").strip() or None)
    return {"ok": True, "action": "list", "project": block, "count": len(links),
            "links": [link.to_dict() for link in links],
            "message": (f"{len(links)} source(s) linked to {block['name'] or 'this project'}."
                        if links else "This project has no linked sources yet."),
            "exit_code": 0}
