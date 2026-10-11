"""The agent contract every Hoard app implements: a catalogue of tools, one ``call`` entry point, a capped result, a
shared error type and the two routes (``GET /api/agent/tools``, ``POST /api/agent/call``) the MCP bridge talks to.

Replaces ``agent_tools.py``'s plumbing (``Tool``, ``_ann``, ``Empty``, ``tool_catalog``, ``call_tool``, ``cap_result``,
``uncapped``, ``_confirm`` - 18 copies, 6 of them with the result cap), ``api/agent.py`` (18), ``api/deps.py``'s
``tool()`` helper (6 groups) and the nine small ``errors.py`` files. An app keeps only its own tool list.

Importing needs the standard library only: ``pydantic`` is imported by the functions that need it and ``fastapi``
inside :func:`make_agent_router`. ``Empty`` is resolved on first access (module ``__getattr__``).

    TOOLS = [Tool("note_add", "Add a note.", NoteArgs, ann(), lambda svc, a: svc.add(a.text))]
    router = make_agent_router(tools_fn=lambda: tool_catalog(TOOLS), instructions=INSTRUCTIONS, app_name="notes",
                               call_fn=lambda name, args, request: call_tool(TOOLS, request.app.state.services, name, args),
                               token_fn=lambda request: request.app.state.services.token)

Since 0.8.2 the same router can also hold the agents accountable (all of it opt-in, see :func:`make_agent_router`): who
called (``X-Agent-Id`` / ``X-Agent-Session``), a mandatory ``reason`` on writes, a write journal, undo of a whole agent
session through per-tool ``undo`` handlers, and extra tokens with a profile (``read_only``, ``drafts``, ``all``).
"""

# No ``from __future__ import annotations``: the route handlers built in make_agent_router annotate their parameters with
# classes imported there (fastapi resolves the annotations from the function, not from this module).
import contextlib
import contextvars
import hashlib
import inspect
import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional, Sequence, Union

from . import agent_journal, family
from .tokens import bearer_of, check_bearer, list_agent_tokens, lookup_agent_token, mint_agent_token, revoke_agent_tokens, PROFILES

__all__ = ["Tool", "ann", "Empty", "tool_catalog", "call_tool", "cap_result", "uncapped", "is_uncapped", "confirm", "AppError",
           "UnknownTool", "validate_arguments", "format_issues", "issues_of", "make_agent_router", "MAX_RESULT_BYTES", "REASON_MIN", "REASON_MAX"]

MAX_RESULT_BYTES = 20_000
REASON_MIN, REASON_MAX = 3, 300

log = logging.getLogger("hoard_link.agentkit")


# ------------------------------------------------------------------------------------------------ errors

class AppError(Exception):
    """An expected, explainable failure: a stable ``code``, a human ``message`` and an actionable ``hint``.

    ``status`` is the HTTP status; left out, it comes from :attr:`STATUS` by code (``not_found`` 404,
    ``confirm_required`` 400, ``forbidden`` 403, ``conflict`` 409, ``too_large`` 413, ``unsupported`` 415,
    ``offline`` / ``unavailable`` 503 ...) and is 400 for any other code. ``details`` travels with the error.
    Apps subclass it (``class KafkaError(AppError)``) to keep their own constructor and codes."""

    STATUS: Mapping[str, int] = {
        "invalid": 400, "invalid_arguments": 400, "not_configured": 400, "confirm_required": 400,
        "forbidden": 403, "not_found": 404, "conflict": 409, "too_large": 413, "unsupported": 415,
        "failed": 500, "offline": 503, "unavailable": 503,
    }

    def __init__(self, code: str, message: str, *, hint: str = "", status: Optional[int] = None,
                 details: Optional[Mapping[str, Any]] = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.status = int(status) if status else int(self.STATUS.get(code, 400))
        self.details: dict[str, Any] = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        """The JSON body: ``{**details, "error": message, "code": code, "hint"?: hint}``."""
        body: dict[str, Any] = {**self.details, "error": self.message, "code": self.code}
        if self.hint:
            body["hint"] = self.hint
        return body


class UnknownTool(KeyError):
    """:func:`call_tool` was asked for a tool the app does not have."""


def confirm(flag: Any, what: str) -> None:
    """Guard for destructive tools: raise ``AppError("confirm_required")`` unless ``flag`` is truthy.
    ``confirm(args.confirm, "this document")``."""
    if not flag:
        raise AppError("confirm_required", f"Deleting {what} is permanent.",
                       hint="Repeat the call with confirm=true if the user asked for it.")


# ------------------------------------------------------------------------------------------------ tools

@dataclass(frozen=True)
class Tool:
    """One agent tool. ``run(ctx, args)`` gets the app's context object (its ``Services``) and the validated
    ``input_model`` instance. ``timeout_s`` (optional) is published as ``x-timeout-s`` in the catalogue so the MCP
    bridge waits that long for it.

    Accountability hooks (0.8.2; used by the router when it has a ``data_dir``; all optional, all for writes):

    * ``capture(ctx, args) -> dict | None`` runs just *before* the write and returns whatever is needed to take it back
      (the previous state of the object); it is kept in the journal line as ``before`` (JSON, at most 256 KB).
    * ``track(args, result[, ctx=]) -> dict`` (``ctx`` only when it declares that parameter; the result it sees is already
      trimmed, so read the live state from ``ctx`` for anything big) runs just *after* it and says what the write touched: ``objects`` (slash-separated
      paths such as ``["deck:ID/slide:ID"]``; two writes conflict when their paths overlap), ``ids`` (extra identifiers)
      and ``etag`` (a string that identifies the state of the object right after the write). Without it the objects are
      guessed from ``*_id`` arguments and the result's ids.
    * ``undo(ctx, record) -> dict`` takes the write back. ``record`` is the journal line (``before``, ``ids``, ``objects``,
      ``etag``, ``tool``...). Raise an :class:`AppError` with code ``conflict`` when the object no longer matches ``etag``.
      A handler that declares ``dry_run`` is also called with ``dry_run=True`` and must then only check and describe.

    ``annotations`` may carry ``draftSafeHint`` (``ann(draft_safe=True)``): the tool writes drafts or new objects but never
    deletes or publishes; tokens with the ``drafts`` profile may call only those and the read-only tools."""

    name: str
    description: str
    input_model: Any
    annotations: Mapping[str, bool]
    run: Callable[[Any, Any], Any]
    timeout_s: Optional[float] = None
    capped: bool = True
    undo: Optional[Callable[..., Any]] = None
    capture: Optional[Callable[[Any, Any], Any]] = None
    track: Optional[Callable[[Any, Any], Any]] = None


def ann(read_only: bool = False, destructive: bool = False, idempotent: Optional[bool] = None,
        open_world: bool = False, draft_safe: bool = False) -> dict[str, bool]:
    """MCP tool annotations. ``idempotent`` defaults to ``read_only`` (a read is repeatable; a write is not unless you say so).
    ``draft_safe`` adds ``draftSafeHint``: the write creates or edits drafts and never deletes or publishes (the ``drafts``
    token profile may call it). The key is only present when true."""
    out = {"readOnlyHint": bool(read_only), "destructiveHint": bool(destructive),
           "idempotentHint": bool(read_only if idempotent is None else idempotent), "openWorldHint": bool(open_world)}
    if draft_safe:
        out["draftSafeHint"] = True
    return out


_EMPTY: Any = None


def __getattr__(name: str) -> Any:
    """``Empty`` (a pydantic model with no fields) is built on first use so importing this module needs no pydantic."""
    global _EMPTY
    if name == "Empty":
        if _EMPTY is None:
            from pydantic import BaseModel

            class Empty(BaseModel):  # type: ignore[misc]
                """A tool without arguments."""

            Empty.__qualname__ = "Empty"
            _EMPTY = Empty
            globals()["Empty"] = Empty
        return _EMPTY
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _schema_of(model: Any) -> dict[str, Any]:
    if model is None:
        return {"type": "object", "properties": {}}
    if isinstance(model, Mapping):
        return dict(model)
    if hasattr(model, "model_json_schema"):
        return model.model_json_schema(by_alias=True)
    raise TypeError(f"cannot derive a JSON schema from {model!r}")


def tool_catalog(tools: Sequence[Tool]) -> list[dict[str, Any]]:
    """The ``GET /api/agent/tools`` entries: ``{name, description, annotations, inputSchema[, "x-timeout-s"]}``.
    ``input_model`` is a pydantic model (its JSON schema, by alias), a ready JSON schema dict, or ``None``."""
    out = []
    for tool in tools:
        entry: dict[str, Any] = {"name": tool.name, "description": tool.description, "annotations": dict(tool.annotations),
                                 "inputSchema": _schema_of(tool.input_model)}
        if tool.timeout_s:
            entry["x-timeout-s"] = float(tool.timeout_s)
        out.append(entry)
    return out


def _index(tools: Union[Sequence[Tool], Mapping[str, Tool]]) -> Mapping[str, Tool]:
    return tools if isinstance(tools, Mapping) else {t.name: t for t in tools}


def validate_arguments(tool: Tool, arguments: Optional[Mapping[str, Any]]) -> Any:
    """The tool's ``input_model`` built from ``arguments`` (pydantic's ``ValidationError`` when they do not fit; a plain dict
    for a tool without a model)."""
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, Mapping):
        raise ValueError("arguments must be an object")
    model = tool.input_model
    if model is None or isinstance(model, Mapping):
        return dict(arguments)
    if hasattr(model, "model_validate"):
        return model.model_validate(dict(arguments))
    return model(**arguments)


def call_tool(tools: Union[Sequence[Tool], Mapping[str, Tool]], ctx: Any, name: str, arguments: Optional[Mapping[str, Any]],
              *, cap: bool = True, post: Optional[Callable[[Any, Any], Any]] = None) -> Any:
    """Validate ``arguments`` against the tool's model, run it with ``ctx``, make the result a dict and cap it.

    Raises :class:`UnknownTool` (a ``KeyError``) for an unknown name and pydantic's ``ValidationError`` for bad
    arguments; whatever the tool raises propagates. A non-dict result is wrapped as ``{"result": ...}``; a pydantic
    model is dumped. ``post(result, args)`` runs before the cap (Kafka masks personal identifiers there). With
    ``cap=False``, or inside ``with uncapped():`` (the web UI sharing the handlers), the result is not capped."""
    tool = _index(tools).get(name)
    if tool is None:
        raise UnknownTool(f"Unknown tool: {name}")
    args = validate_arguments(tool, arguments)
    result = tool.run(ctx, args)
    if hasattr(result, "model_dump"):
        result = result.model_dump(mode="json")
    if not isinstance(result, dict):
        result = {"result": result}
    if post is not None:
        result = post(result, args)
    if cap and tool.capped:
        result = cap_result(result)
    return result


# ------------------------------------------------------------------------------------------------ result cap

_UNCAPPED: contextvars.ContextVar[bool] = contextvars.ContextVar("hoard_link_uncapped", default=False)


def is_uncapped() -> bool:
    """Whether this call belongs to the web UI's uncapped result context."""
    return _UNCAPPED.get()


@contextlib.contextmanager
def uncapped() -> Iterator[None]:
    """The web UI shares the tool handlers but is not bound by the assistant's context budget:
    ``with uncapped(): call_tool(...)`` returns the whole result."""
    token = _UNCAPPED.set(True)
    try:
        yield
    finally:
        _UNCAPPED.reset(token)


def _size(value: Any) -> int:
    return len(json.dumps(value, default=str, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def cap_result(data: Any, limit: int = MAX_RESULT_BYTES) -> Any:
    """Keep a tool result under ~``limit`` bytes of JSON so a listing cannot flood the model's context.

    Over the limit, the largest top-level list is halved (at least one item stays) until it fits, and a
    ``"truncated": {"reason", "original_lengths", "hint"}`` block says what was cut. A result that is still too big
    because of one huge string has that string cut (and ``original_lengths`` records its length). A list result is
    returned as ``{"result": [...], "truncated": ...}``. Anything else is returned as it is. Inside
    :func:`uncapped` nothing is cut."""
    if _UNCAPPED.get():
        return data
    if isinstance(data, list):
        wrapped: Any = {"result": data}
        capped = cap_result(wrapped, limit)
        return data if capped is wrapped else capped
    if not isinstance(data, dict) or _size(data) <= limit:
        return data
    data = dict(data)
    truncated: dict[str, int] = {}
    for _ in range(40):
        if _size(data) <= limit - 300:
            break
        lists = [(k, v) for k, v in data.items() if isinstance(v, list) and len(v) > 1]
        if not lists:
            break
        key, value = max(lists, key=lambda kv: _size(kv[1]))
        truncated.setdefault(key, len(value))
        data[key] = value[: max(1, len(value) // 2)]
    if _size(data) > limit - 300:
        strings = [(k, v) for k, v in data.items() if isinstance(v, str) and len(v) > 200]
        if strings:
            key, value = max(strings, key=lambda kv: len(kv[1]))
            truncated.setdefault(key, len(value))
            data[key] = value[: max(200, limit // 2)] + "…"
    data["truncated"] = {"reason": f"result capped at ~{limit // 1000} KB", "original_lengths": truncated,
                         "hint": "Use limit or narrower filters to see the rest."}
    return data


# ------------------------------------------------------------------------------------------------ validation issues

def issues_of(error: Any) -> list[dict[str, str]]:
    """``[{"loc": "a.b", "msg": "..."}]`` for a pydantic ``ValidationError`` or fastapi ``RequestValidationError``
    (``loc`` without the leading ``body``; ``"input"`` when empty)."""
    items = error.errors() if callable(getattr(error, "errors", None)) else []
    out = []
    for item in items:
        loc = ".".join(str(part) for part in item.get("loc", ()) if part != "body") or "input"
        out.append({"loc": loc, "msg": str(item.get("msg", "invalid"))})
    return out


def format_issues(error: Any) -> str:
    """``"a.b: msg; c: msg"`` - what the family always put in ``error`` for bad arguments."""
    return "; ".join(f"{i['loc']}: {i['msg']}" for i in issues_of(error)) or str(error)


# ------------------------------------------------------------------------------------------------ the router

def _declares(fn: Callable[..., Any], name: str) -> bool:
    try:
        return name in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


def _call_with_request(fn: Callable[..., Any], request: Any, *args: Any) -> Any:
    """Call ``fn(*args)``, adding ``request=request`` when ``fn`` declares a parameter named ``request``."""
    if _declares(fn, "request"):
        return fn(*args, request=request)
    return fn(*args)


def _is_write(annotations: Optional[Mapping[str, Any]]) -> bool:
    return not bool((annotations or {}).get("readOnlyHint"))


def _with_reason(entry: Mapping[str, Any]) -> dict[str, Any]:
    """A catalogue entry of a write tool with ``reason`` added (and required) in its input schema, so the model sees it."""
    import copy

    if not _is_write(entry.get("annotations")):
        return dict(entry)
    schema = copy.deepcopy(dict(entry.get("inputSchema") or {"type": "object", "properties": {}}))
    props = schema.setdefault("properties", {})
    if "reason" not in props:
        props["reason"] = {"type": "string", "minLength": REASON_MIN, "maxLength": REASON_MAX,
                           "description": "Required. Why you are making this change, in one sentence the person can read in the "
                                          f"history ({REASON_MIN}-{REASON_MAX} characters)."}
    required = schema.setdefault("required", [])
    if "reason" not in required:
        required.append("reason")
    return {**entry, "inputSchema": schema}


def make_agent_router(*, tools_fn: Callable[..., Sequence[Mapping[str, Any]]], call_fn: Callable[..., Any],
                      token_fn: Callable[..., str], instructions: Union[str, Callable[..., str]], app_name: str,
                      error_types: Sequence[type] = (), reasons: bool = False, data_dir: Any = None,
                      tools: Union[Sequence[Tool], Mapping[str, Tool], None] = None, ctx_fn: Optional[Callable[..., Any]] = None,
                      journal_max_bytes: int = agent_journal.MAX_JOURNAL_BYTES) -> Any:
    """The ``APIRouter`` (prefix ``/api/agent``) for the MCP bridge.

    * ``GET /tools`` -> ``{"instructions", "tools", "app"}`` (no token: the catalogue is not a secret).
    * ``POST /call`` ``{name, arguments, caller?}`` with ``Authorization: Bearer <token>`` (scheme case-insensitive,
      constant-time compare; the token comes from ``token_fn()``, never empty) -> the tool result.

    ``tools_fn()``, ``call_fn(name, arguments)`` and ``token_fn()`` are plain callables; any of them that declares a
    parameter named ``request`` also receives the Starlette request (so a ``call_fn`` can reach
    ``request.app.state.services``). ``call_fn`` may be ``async``; a sync one runs in the thread pool. Errors, all as
    ``{"error", "code", ...}`` JSON:

    ====================================== ======= ===========================================
    :class:`AppError` / ``error_types``     its own ``status`` and ``to_dict()``
    :class:`UnknownTool`                     404     ``unknown_tool``
    other ``KeyError``                       404     ``not_found``
    pydantic ``ValidationError``             400     ``invalid_arguments`` + ``issues``
    ``ValueError``                           400     ``invalid``
    anything else                            500     ``internal`` (logged)
    ====================================== ======= ===========================================

    Every call - success or failure - ends in ``family.record_call`` (the ``agent.call`` audit event).

    **Agent identity** (0.8.2). A call may say who makes it: ``X-Agent-Id`` / ``X-Agent-Session`` headers (the MCP bridge
    sends them from ``HOARD_AGENT_ID`` / ``HOARD_AGENT_SESSION``), or ``caller`` / ``agent`` and ``_session`` / ``session``
    in the body. A token from ``agent_tokens.json`` fixes the agent id (the header cannot override it).

    The rest is opt-in so that an app adopts it when it is ready:

    * ``reasons=True``: a tool that is not read-only needs a ``reason`` (3-300 characters; in the body or among the
      arguments) or the call is refused with 400 ``reason_required``. The catalogue then lists ``reason`` as a required
      property of those tools. Only ``/api/agent/call`` asks for it: an app's own UI calls the tools directly.
    * ``data_dir`` (a path, or a callable taking the request when it needs it): turns on
      the write **journal** (``<data_dir>/agent_journal.jsonl``, ``GET /journal?session=&agent=&limit=&kind=&since=&full=``),
      the ``agent.write`` event, **undo** (``POST /undo {session, agent?, confirm, dry_run?, reason?}``), and the extra tokens of
      ``agent_tokens.json`` with their **profiles** (``read_only`` blocks writes; ``drafts`` allows only tools annotated
      ``draftSafeHint``; ``all``) plus their admin routes ``GET|POST /tokens`` and ``DELETE /tokens/{id}`` (main token only).
    * ``tools``: the app's :class:`Tool` list, which carries the ``undo`` / ``capture`` / ``track`` hooks (the router needs the
      objects, ``tools_fn`` only returns the catalogue). ``ctx_fn(request)`` gives the context those hooks and ``undo`` get
      (default: ``request.app.state.services``).

    Journal lines, the undo answer and the profile rules are described in ``docs/COMMONS.md`` (section *Accountable agents*)."""
    from fastapi import APIRouter, Request
    from fastapi.concurrency import run_in_threadpool
    from fastapi.responses import JSONResponse
    from pydantic import BaseModel, Field, ValidationError

    class CallBody(BaseModel):
        name: str = Field(..., min_length=1, max_length=100)
        arguments: Optional[dict[str, Any]] = None
        caller: Optional[str] = Field(default=None, max_length=80)
        agent: Optional[str] = Field(default=None, max_length=80)
        session: Optional[str] = Field(default=None, max_length=120)
        session_alias: Optional[str] = Field(default=None, alias="_session", max_length=120)
        reason: Optional[str] = Field(default=None, max_length=5000)

    class UndoBody(BaseModel):
        session: str = Field(..., min_length=1, max_length=120)
        agent: Optional[str] = Field(default=None, max_length=80)
        confirm: bool = False
        dry_run: bool = False
        reason: Optional[str] = Field(default=None, max_length=5000)

    class TokenBody(BaseModel):
        agent: str = Field(..., min_length=1, max_length=80)
        profile: str = "drafts"
        label: str = Field(default="", max_length=120)

    router = APIRouter(prefix="/api/agent")
    handled = (AppError, *tuple(error_types))
    agent_log = logging.getLogger(f"hoard_link.agent.{app_name}")
    tool_index: dict[str, Tool] = dict(_index(tools)) if tools is not None else {}
    meta_cache: dict[str, Mapping[str, Any]] = {}
    undo_lock = threading.Lock()

    # ------------------------------------------------------------------ small helpers
    def folder(request: Any) -> Optional[Path]:
        if data_dir is None:
            return None
        value = _call_with_request(data_dir, request) if callable(data_dir) else data_dir
        return Path(value) if value else None

    def journal_of(request: Any) -> Optional[agent_journal.Journal]:
        path = folder(request)
        return agent_journal.Journal(path, max_bytes=journal_max_bytes, app=app_name) if path else None

    def context(request: Any) -> Any:
        if ctx_fn is not None:
            return _call_with_request(ctx_fn, request)
        return getattr(request.app.state, "services", None)

    def meta_of(request: Any, name: str) -> Optional[Mapping[str, Any]]:
        """The catalogue entry of ``name`` (its annotations and schema), or None for an unknown tool."""
        tool = tool_index.get(name)
        if tool is not None:
            return {"name": name, "annotations": dict(tool.annotations), "_tool": tool}
        if name not in meta_cache:
            try:
                for entry in _call_with_request(tools_fn, request):
                    if isinstance(entry, Mapping) and entry.get("name"):
                        meta_cache[str(entry["name"])] = entry
            except Exception:  # noqa: BLE001 - a broken catalogue must not break the call
                agent_log.exception("tool catalogue failed")
        return meta_cache.get(name)

    def declares_reason(meta: Mapping[str, Any]) -> bool:
        tool = meta.get("_tool")
        try:
            schema = _schema_of(tool.input_model) if tool is not None else (meta.get("inputSchema") or {})
        except Exception:  # noqa: BLE001
            return False
        return "reason" in (schema.get("properties") or {})

    def authenticate(request: Any) -> Optional[_Access]:
        header = request.headers.get("authorization")
        if check_bearer(header, _call_with_request(token_fn, request)):
            return _Access(None, "all", "", True)
        path = folder(request)
        if path is not None:
            found = lookup_agent_token(path, bearer_of(header))
            if found:
                return _Access(found["agent"], found["profile"], found["id"], False)
        return None

    def denied(error: str, code: str, status: int, hint: str = "") -> Any:
        body = {"error": error, "code": code}
        if hint:
            body["hint"] = hint
        return JSONResponse(body, status_code=status)

    def reason_error(text: str) -> Any:
        return denied(text, "reason_required", 400,
                      f"Add a `reason` ({REASON_MIN}-{REASON_MAX} characters): one sentence on why you are making this change. "
                      "It is kept in the history so the person can review and undo what you did.")

    # ------------------------------------------------------------------ catalogue
    @router.get("/tools")
    def tools_route(request: Request) -> dict[str, Any]:
        text = _call_with_request(instructions, request) if callable(instructions) else instructions
        listing = list(_call_with_request(tools_fn, request))
        if reasons:
            listing = [_with_reason(entry) for entry in listing]
            text = (text or "") + ("\n\n" if text else "") + (
                f"Every tool that changes something needs a `reason` ({REASON_MIN}-{REASON_MAX} characters, one sentence for the "
                "history); a call without it is refused with reason_required. Tell the session apart with X-Agent-Id and "
                "X-Agent-Session so the person can undo everything one session did.")
        out: dict[str, Any] = {"instructions": text, "tools": listing, "app": app_name}
        if reasons:
            out["reasons_required"] = True
        return out

    # ------------------------------------------------------------------ the call
    @router.post("/call")
    async def call(request: Request, body: CallBody) -> Any:
        access = authenticate(request)
        if access is None:
            return JSONResponse({"error": "Invalid MCP token.", "code": "unauthorized"}, status_code=401)
        agent = access.agent or agent_journal.clean_identity(request.headers.get("x-agent-id")) \
            or agent_journal.clean_identity(body.agent) or agent_journal.clean_identity(body.caller)
        session = agent_journal.clean_identity(request.headers.get("x-agent-session"), 120) \
            or agent_journal.clean_identity(body.session_alias, 120) or agent_journal.clean_identity(body.session, 120)
        arguments = dict(body.arguments or {})
        meta = meta_of(request, body.name)
        write = meta is not None and _is_write(meta.get("annotations"))
        t0 = time.monotonic()
        outcome: dict[str, Any] = {"ok": False, "error": "", "result": None, "ran": False}
        reason = ""
        validated: Any = None
        before: Any = None
        capture_failed = False
        try:
            if meta is not None and access.profile != "all":
                annotations = meta.get("annotations") or {}
                if write and (access.profile == "read_only" or not annotations.get("draftSafeHint")):
                    outcome["error"] = f"profile_forbidden: {access.profile}"
                    hint = ("This token is read-only: only tools that do not change anything." if access.profile == "read_only" else
                            "This token may only read and write drafts: use a tool that creates or edits drafts, or ask the person for a full token.")
                    return denied(f"The {access.profile} profile of this token does not allow {body.name}.", "profile_forbidden", 403, hint)
            if write:
                given = body.reason if body.reason is not None else arguments.get("reason")
                text = given.strip() if isinstance(given, str) else ""
                if "reason" in arguments and not declares_reason(meta):
                    arguments.pop("reason")
                if reasons and not (REASON_MIN <= len(text) <= REASON_MAX):
                    outcome["error"] = "reason_required"
                    return reason_error("This tool changes data, so the call needs a reason." if not text else
                                        f"The reason must have {REASON_MIN}-{REASON_MAX} characters (yours has {len(text)}).")
                reason = text[:REASON_MAX]
            journal = journal_of(request) if write else None
            tool = tool_index.get(body.name) if journal is not None else None
            if tool is not None and tool.capture is not None:
                try:
                    validated = validate_arguments(tool, arguments)
                    before = await run_in_threadpool(tool.capture, context(request), validated)
                except Exception:  # noqa: BLE001 - the call itself reports a bad argument; no snapshot means no undo
                    capture_failed = True
            outcome["ran"] = True
            if inspect.iscoroutinefunction(call_fn):
                result = await _call_with_request(call_fn, request, body.name, arguments)
            else:
                result = await run_in_threadpool(_call_with_request, call_fn, request, body.name, arguments)
            outcome["ok"] = True
            outcome["result"] = result
            return result
        except handled as error:
            outcome["error"] = f"{getattr(error, 'code', 'error')}: {getattr(error, 'message', str(error))}"
            return JSONResponse(error.to_dict(), status_code=int(getattr(error, "status", 400)))  # type: ignore[attr-defined]
        except UnknownTool as error:
            message = str(error.args[0]) if error.args else f"Unknown tool: {body.name}"
            outcome["error"] = message
            return JSONResponse({"error": message, "code": "unknown_tool"}, status_code=404)
        except KeyError as error:
            message = str(error.args[0]) if error.args else "Not found."
            outcome["error"] = message
            return JSONResponse({"error": message, "code": "not_found"}, status_code=404)
        except LookupError as error:
            outcome["error"] = str(error)
            return JSONResponse({"error": str(error), "code": "not_found"}, status_code=404)
        except PermissionError as error:
            outcome["error"] = str(error)
            return JSONResponse({"error": str(error), "code": "forbidden"}, status_code=403)
        except ValidationError as error:
            message = format_issues(error)
            outcome["error"] = message
            return JSONResponse({"error": message, "code": "invalid_arguments", "issues": issues_of(error)}, status_code=400)
        except ValueError as error:
            outcome["error"] = str(error)
            return JSONResponse({"error": str(error), "code": "invalid"}, status_code=400)
        except Exception as error:  # noqa: BLE001 - the bridge must always get JSON, never an HTML 500
            outcome["error"] = f"{type(error).__name__}: {error}"
            agent_log.exception("agent tool %s failed", body.name)
            return JSONResponse({"error": f"Internal error: {type(error).__name__}: {str(error)[:200]}", "code": "internal"},
                                status_code=500)
        finally:
            ms = int((time.monotonic() - t0) * 1000)
            extra = {"session": session} if session else {}      # older stubs of record_call do not take it
            family.record_call(body.name, outcome["ok"], ms, caller=agent or body.caller or "", error=outcome["error"], **extra)
            if write and outcome["ran"]:
                try:
                    await run_in_threadpool(_journal_write, request, body.name, arguments, agent, session, reason, access, outcome,
                                            ms, tool_index.get(body.name), validated, before, capture_failed)
                except Exception:  # noqa: BLE001 - the journal must never break a write that already happened
                    agent_log.exception("could not journal %s", body.name)

    def _journal_write(request: Any, name: str, arguments: dict[str, Any], agent: str, session: str, reason: str, access: _Access,
                       outcome: dict[str, Any], ms: int, tool: Optional[Tool], validated: Any, before: Any, capture_failed: bool) -> None:
        journal = journal_of(request)
        if journal is None:
            return
        result = outcome["result"] if outcome["ok"] else None
        tracked: dict[str, Any] = {}
        if outcome["ok"] and tool is not None and tool.track is not None:
            try:
                if validated is None:
                    validated = validate_arguments(tool, arguments)
                if _declares(tool.track, "ctx"):
                    tracked = dict(tool.track(validated, result, ctx=context(request)) or {})
                else:
                    tracked = dict(tool.track(validated, result) or {})
            except Exception:  # noqa: BLE001
                agent_log.exception("track hook of %s failed", name)
        ids = agent_journal.result_ids(result) + [str(i)[:100] for i in (tracked.get("ids") or [])]
        undoable = bool(outcome["ok"] and tool is not None and tool.undo is not None and not capture_failed
                        and (tool.capture is None or before is not None))
        entry: dict[str, Any] = {
            "kind": "write", "tool": name, "agent": agent, "session": session, "reason": agent_journal.mask_text(reason),
            "args_digest": agent_journal.digest_args(arguments), "args_summary": agent_journal.summarize_args(arguments),
            "ids": list(dict.fromkeys(ids))[:30], "objects": list(tracked["objects"] if "objects" in tracked and tracked["objects"] is not None
                         else agent_journal.default_objects(arguments, result))[:30],
            "etag": str(tracked.get("etag") or ""), "ok": bool(outcome["ok"]), "error": agent_journal.mask_text(str(outcome["error"]))[:300],
            "ms": ms, "profile": access.profile, "token": access.token_id, "undoable": undoable}
        if before is not None:
            entry["before"] = before
        if tool is not None and tool.undo is not None and outcome["ok"] and not undoable:
            entry["not_undoable_reason"] = "capture_failed"
        stored = journal.append(entry)
        family.record_write(stored)

    # ------------------------------------------------------------------ journal and undo
    @router.get("/journal")
    def journal_route(request: Request, session: str = "", agent: str = "", limit: int = 100, kind: str = "", since: float = 0.0,
                      full: bool = False) -> Any:
        access = authenticate(request)
        if access is None:
            return JSONResponse({"error": "Invalid MCP token.", "code": "unauthorized"}, status_code=401)
        journal = journal_of(request)
        if journal is None:
            return denied("This app does not keep an agent journal.", "journal_unavailable", 404)
        if not access.main:
            agent = access.agent or agent          # a scoped token only sees what its agent did
            full = False
        undoable = (lambda tool: tool in handlers_now()) if tool_index else None
        rows = journal.query(session=agent_journal.clean_identity(session, 120), agent=agent_journal.clean_identity(agent), kind=kind,
                             limit=max(1, min(int(limit), 1000)), since=since, full=full, undoable=undoable)
        return {"ok": True, "app": app_name, "entries": rows, "count": len(rows), "reasons_required": bool(reasons),
                "undo_tools": sorted(handlers_now())}

    def handlers_now() -> dict[str, Callable[..., Any]]:
        return {n: t.undo for n, t in tool_index.items() if t.undo is not None}

    @router.post("/undo")
    async def undo(request: Request, body: UndoBody) -> Any:
        access = authenticate(request)
        if access is None:
            return JSONResponse({"error": "Invalid MCP token.", "code": "unauthorized"}, status_code=401)
        if access.profile != "all":
            return denied(f"The {access.profile} profile of this token cannot undo sessions.", "profile_forbidden", 403,
                          "Ask the person to undo it, or use a token with the all profile.")
        journal = journal_of(request)
        if journal is None:
            return denied("This app does not keep an agent journal.", "journal_unavailable", 404)
        session = agent_journal.clean_identity(body.session, 120)
        agent = access.agent or agent_journal.clean_identity(body.agent)
        if not body.dry_run and not body.confirm:
            return denied("Undoing a session changes data.", "confirm_required", 400,
                          "Repeat with dry_run=true to see what would be undone, then with confirm=true to do it.")
        reason = (body.reason or "").strip()
        if reasons and not body.dry_run and not (REASON_MIN <= len(reason) <= REASON_MAX):
            return reason_error("Undoing a session needs a reason.")
        if not any(r.get("session") == session and r.get("kind", "write") == "write" and (not agent or r.get("agent") == agent)
                   for r in journal.entries()):
            return denied(f"No writes of session {session!r} in the journal.", "session_not_found", 404,
                          "List the sessions with GET /api/agent/journal.")
        ctx = context(request)
        actor = access.agent or "main-token"

        def work() -> dict[str, Any]:
            with undo_lock:
                return agent_journal.undo_session(journal, handlers_now(), ctx, session=session, agent=agent, dry_run=body.dry_run,
                                                  reason=agent_journal.mask_text(reason)[:REASON_MAX], actor=actor)

        result = await run_in_threadpool(work)
        if not body.dry_run:
            family.record_undo(app_name, result, reason=reason)
        return {"ok": True, "app": app_name, **result}

    # ------------------------------------------------------------------ extra tokens (main token only)
    def main_only(request: Any) -> Any:
        access = authenticate(request)
        if access is None:
            return JSONResponse({"error": "Invalid MCP token.", "code": "unauthorized"}, status_code=401)
        if not access.main:
            return denied("Only the app's main token manages agent tokens.", "profile_forbidden", 403)
        if folder(request) is None:
            return denied("This app does not keep agent tokens.", "journal_unavailable", 404)
        return None

    @router.get("/tokens")
    def tokens_list(request: Request) -> Any:
        refused = main_only(request)
        if refused is not None:
            return refused
        return {"ok": True, "app": app_name, "profiles": list(PROFILES), "tokens": list_agent_tokens(folder(request))}

    @router.post("/tokens")
    def tokens_mint(request: Request, body: TokenBody) -> Any:
        refused = main_only(request)
        if refused is not None:
            return refused
        try:
            minted = mint_agent_token(folder(request), body.agent, body.profile, label=body.label)
        except ValueError as error:
            return denied(str(error), "invalid", 400)
        return {"ok": True, "app": app_name, **minted, "hint": "Copy the token now: only its hash is kept."}

    @router.delete("/tokens/{token_id}")
    def tokens_revoke(request: Request, token_id: str) -> Any:
        refused = main_only(request)
        if refused is not None:
            return refused
        try:
            count = revoke_agent_tokens(folder(request), token_id=token_id)
        except ValueError as error:
            return denied(str(error), "invalid", 400)
        if not count:
            return denied("No token with that id.", "not_found", 404)
        return {"ok": True, "app": app_name, "revoked": count}

    return router


@dataclass(frozen=True)
class _Access:
    """Who is calling: the app's main token (``main``) or a scoped token (its agent, its profile and its short id)."""

    agent: Optional[str]
    profile: str
    token_id: str
    main: bool
