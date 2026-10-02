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
"""

# No ``from __future__ import annotations``: the route handlers built in make_agent_router annotate their parameters with
# classes imported there (fastapi resolves the annotations from the function, not from this module).
import contextlib
import contextvars
import inspect
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Mapping, Optional, Sequence, Union

from . import family
from .tokens import check_bearer

__all__ = ["Tool", "ann", "Empty", "tool_catalog", "call_tool", "cap_result", "uncapped", "is_uncapped", "confirm", "AppError",
           "UnknownTool", "format_issues", "issues_of", "make_agent_router", "MAX_RESULT_BYTES"]

MAX_RESULT_BYTES = 20_000

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
    bridge waits that long for it."""

    name: str
    description: str
    input_model: Any
    annotations: Mapping[str, bool]
    run: Callable[[Any, Any], Any]
    timeout_s: Optional[float] = None
    capped: bool = True


def ann(read_only: bool = False, destructive: bool = False, idempotent: Optional[bool] = None,
        open_world: bool = False) -> dict[str, bool]:
    """MCP tool annotations. ``idempotent`` defaults to ``read_only`` (a read is repeatable; a write is not unless you say so)."""
    return {"readOnlyHint": bool(read_only), "destructiveHint": bool(destructive),
            "idempotentHint": bool(read_only if idempotent is None else idempotent), "openWorldHint": bool(open_world)}


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
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, Mapping):
        raise ValueError("arguments must be an object")
    model = tool.input_model
    if model is None or isinstance(model, Mapping):
        args: Any = dict(arguments)
    elif hasattr(model, "model_validate"):
        args = model.model_validate(dict(arguments))
    else:
        args = model(**arguments)
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


def make_agent_router(*, tools_fn: Callable[..., Sequence[Mapping[str, Any]]], call_fn: Callable[..., Any],
                      token_fn: Callable[..., str], instructions: Union[str, Callable[..., str]], app_name: str,
                      error_types: Sequence[type] = ()) -> Any:
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

    Every call - success or failure - ends in ``family.record_call`` (the ``agent.call`` audit event)."""
    from fastapi import APIRouter, Request
    from fastapi.concurrency import run_in_threadpool
    from fastapi.responses import JSONResponse
    from pydantic import BaseModel, Field, ValidationError

    class CallBody(BaseModel):
        name: str = Field(..., min_length=1, max_length=100)
        arguments: Optional[dict[str, Any]] = None
        caller: Optional[str] = Field(default=None, max_length=80)

    router = APIRouter(prefix="/api/agent")
    handled = (AppError, *tuple(error_types))
    agent_log = logging.getLogger(f"hoard_link.agent.{app_name}")

    @router.get("/tools")
    def tools(request: Request) -> dict[str, Any]:
        text = _call_with_request(instructions, request) if callable(instructions) else instructions
        return {"instructions": text, "tools": list(_call_with_request(tools_fn, request)), "app": app_name}

    @router.post("/call")
    async def call(request: Request, body: CallBody) -> Any:
        token = _call_with_request(token_fn, request)
        if not check_bearer(request.headers.get("authorization"), token):
            return JSONResponse({"error": "Invalid MCP token.", "code": "unauthorized"}, status_code=401)
        t0 = time.monotonic()
        outcome = {"ok": False, "error": ""}
        try:
            if inspect.iscoroutinefunction(call_fn):
                result = await _call_with_request(call_fn, request, body.name, body.arguments or {})
            else:
                result = await run_in_threadpool(_call_with_request, call_fn, request, body.name, body.arguments or {})
            outcome["ok"] = True
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
            family.record_call(body.name, outcome["ok"], int((time.monotonic() - t0) * 1000), caller=body.caller or "",
                               error=outcome["error"])

    return router
