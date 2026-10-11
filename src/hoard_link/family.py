"""What an app needs to be a good citizen of the family — from inside the app.

Standard library only (no ``httpx``), so any app that vendors
``hoard_link/`` can import this even where the rest of the library's
optional bits are not installed. Three things:

* :func:`emit` — post an event to the hub's bus, fire-and-forget: a
  daemon thread does the HTTP, the caller never waits and never fails
  because the hub is down (the event is dropped; events are hints, the
  app's own database is the truth);
* :func:`call` — call a tool of another app *through the hub* with this
  app's own token: no sibling ports, no reading other apps' token files
  (``Funes`` used to do that; this replaces it);
* :func:`health_block` and :func:`record_call` — what the app adds to its
  ``/api/health`` answer and to its ``/api/agent/call`` handler so the
  hub's audit can see it is on the contract, and so every agent call
  lands in the bus as ``agent.call`` (the audit trail Cassandra keeps).

Configure once at startup with :func:`configure` (app id + data dir; the
token file is ``<data>/mcp-token`` by convention) — or set
``HOARD_APP_ID`` / ``HOARD_TOKEN_FILE``. For FastAPI apps
:func:`install_fastapi` does the whole thing: configuration, an ASGI
middleware that records the per-tool agent routes of the older apps, and
(``contract=True``) the shared ``/api/agent/tools`` + ``/api/agent/call``
routes on top of ``/api/agent/<tool>`` routes, so those apps answer the
same contract as the rest without touching their existing endpoints.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
from typing import Any, Callable, Optional

from ._hubclient import fetch, fetch_detailed, hub_url

try:  # only for install_fastapi; the rest of the module is standard library
    from fastapi import Request as _Request
    from fastapi.responses import JSONResponse as _JSONResponse
except Exception:  # noqa: BLE001
    _Request = Any  # type: ignore[misc,assignment]
    _JSONResponse = None  # type: ignore[assignment]

FAMILY_VERSION = "0.8.2"
_state: dict[str, Any] = {"app": "", "token_file": "", "hub_url": None, "enabled": True, "sent": 0, "dropped": 0,
                          "last_error": ""}
_lock = threading.Lock()


def configure(app: str, data_dir: Optional[str] = None, *, token_file: Optional[str] = None,
              hub: Optional[str] = None, enabled: Optional[bool] = None) -> dict[str, Any]:
    with _lock:
        _state["app"] = str(app or os.environ.get("HOARD_APP_ID") or "").strip()
        tf = token_file or os.environ.get("HOARD_TOKEN_FILE") or (os.path.join(str(data_dir), "mcp-token") if data_dir else "")
        _state["token_file"] = str(tf or "")
        _state["hub_url"] = hub or os.environ.get("HOARD_HUB_URL") or None
        if enabled is None:
            enabled = os.environ.get("HOARD_EVENTS", "1").strip().lower() not in ("0", "false", "no", "off")
        _state["enabled"] = bool(enabled)
    return status()


def status() -> dict[str, Any]:
    with _lock:
        return dict(_state)


def _token() -> str:
    path = _state.get("token_file") or ""
    if not path:
        return ""
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _hub() -> str:
    return hub_url(_state.get("hub_url"))


def _headers() -> dict[str, str]:
    tok = _token()
    return {"Authorization": "Bearer " + tok} if tok else {}


def _post(path: str, body: dict[str, Any], timeout: float) -> tuple[Optional[int], Any]:
    return fetch(_hub() + path, body, method="POST", timeout=timeout, headers=_headers())


def emit(type: str, data: Optional[dict[str, Any]] = None, *, block: bool = False, timeout: float = 3.0) -> bool:
    """Send an event. Returns True when it was *accepted* (only meaningful
    with ``block=True``); otherwise True means "queued"."""
    if not _state.get("enabled", True) or not _state.get("app"):
        return False
    body = {"type": str(type), "source": _state["app"], "data": data or {}}

    def send() -> bool:
        status_, resp = _post("/api/events", body, timeout)
        with _lock:
            if status_ == 200:
                _state["sent"] += 1
                return True
            _state["dropped"] += 1
            _state["last_error"] = (resp or {}).get("error", f"HTTP {status_}") if isinstance(resp, dict) else f"HTTP {status_}"
            return False

    if block:
        return send()
    threading.Thread(target=send, name="hoard-family-emit", daemon=True).start()
    return True


def call(app: str, tool: str, arguments: Optional[dict[str, Any]] = None, *, timeout: float = 120.0) -> dict[str, Any]:
    """Call ``tool`` of ``app`` through the hub. Same result shape as the
    hub's proxy: ``{ok, app, tool, status, result|error, ms}``."""
    status_, body = _post(f"/api/apps/{app}/call", {"tool": tool, "arguments": arguments or {}, "timeout_s": timeout},
                          timeout + 5.0)
    if status_ is None:
        return {"ok": False, "app": app, "tool": tool, "status": None, "error": f"hub not reachable at {_hub()}"}
    if isinstance(body, dict):
        body.setdefault("status", status_)
        if status_ == 401:
            body["error"] = "the hub refused this app's token (" + (_state.get("token_file") or "no token file") + ")"
        return body
    return {"ok": 200 <= status_ < 300, "app": app, "tool": tool, "status": status_, "result": body}


def chat(messages: list[dict[str, Any]], *, capability: str = "llm", images: Optional[list[Any]] = None,
         json: Any = None, effort: Optional[str] = None, max_tokens: Optional[int] = None,
         temperature: Optional[float] = None, timeout: float = 300.0) -> dict[str, Any]:
    """Ask the local model through the hub (``POST /api/link/chat``), for apps that cannot load models
    themselves (standard library only; apps that have :class:`hoard_link.Link` should use it directly).

    ``messages`` is ``[{"role": "system"|"user"|"assistant", "content": str}]``; ``capability`` is ``"llm"`` or
    ``"vision"`` (vision needs ``images``: bytes or base64 strings, attached to the last user message);
    ``json`` is True, or a JSON Schema dict, to also get the parsed answer; ``effort`` is
    off|low|medium|high|max. Never raises. Returns ``{ok, text, json, model, provider, ms, error, detail}``;
    when ``ok`` is False, ``error`` is ``no_model`` (nothing can serve it), ``timeout``, ``hub_down`` or
    ``http_<code>`` and ``code`` carries the hub's own word (``gpu_busy``, ``bad_request``...). Same semantics
    as the Node helper ``chat()`` in ``hoard-link.js``."""
    body: dict[str, Any] = {"capability": capability, "messages": list(messages or []), "timeout_s": timeout}
    if images:
        body["images"] = [i if isinstance(i, str) else base64.b64encode(bytes(i)).decode("ascii") for i in images]
    if json is not None and json is not False:
        body["json"] = json
    if effort:
        body["effort"] = effort
    if max_tokens:
        body["max_tokens"] = max_tokens
    if temperature is not None:
        body["temperature"] = temperature
    blank = {"text": "", "json": None, "model": None, "provider": None}
    status_, resp, why = fetch_detailed(_hub() + "/api/link/chat", body, method="POST", timeout=timeout + 15.0,
                                        headers=_headers())
    if status_ is None:
        if why == "timeout":
            return {"ok": False, "error": "timeout", "detail": f"no answer from the hub within {timeout:.0f}s", **blank}
        return {"ok": False, "error": "hub_down", "detail": f"hub not reachable at {_hub()}", **blank}
    d = resp if isinstance(resp, dict) else {}
    if status_ == 200 and d.get("ok"):
        out = {"ok": True, "text": d.get("text") or "", "json": d.get("json"), "model": d.get("model"),
               "provider": d.get("provider"), "ms": d.get("ms"), "usage": d.get("usage"), "error": None, "detail": None}
        if d.get("json_error"):
            out["json_error"] = d["json_error"]
        return out
    code = str(d.get("error") or "")
    detail = ("the hub refused this app's token (" + (_state.get("token_file") or "no token file") + ")"
              if status_ == 401 else str(d.get("detail") or code))
    return {"ok": False, "error": code if code in ("no_model", "timeout") else f"http_{status_}", "code": code,
            "detail": detail, **blank}


def link_status(*, force: bool = False, timeout: float = 30.0) -> dict[str, Any]:
    """Which model serves llm / vision / embed / tts now (``GET /api/link/status``): ``{ok, llm: {available,
    model, provider, reason}, ...}``, or ``{ok: False, error: "hub_down" | "timeout" | "http_<code>"}``."""
    status_, resp, why = fetch_detailed(_hub() + "/api/link/status" + ("?force=1" if force else ""), method="GET",
                                        timeout=timeout, headers=_headers())
    if status_ is None:
        return {"ok": False, "error": "timeout" if why == "timeout" else "hub_down", "detail": f"hub at {_hub()}"}
    if status_ == 200 and isinstance(resp, dict):
        return resp
    return {"ok": False, "error": f"http_{status_}", "detail": str((resp or {}).get("error") if isinstance(resp, dict) else "")}


def record_call(tool: str, ok: bool, ms: Optional[int] = None, *, caller: str = "", error: str = "", session: str = "") -> None:
    """One ``agent.call`` event per agent tool call — the audit trail."""
    data: dict[str, Any] = {"tool": str(tool), "ok": bool(ok)}
    if ms is not None:
        data["ms"] = int(ms)
    if caller:
        data["caller"] = str(caller)[:80]
    if session:
        data["session"] = str(session)[:120]
    if error:
        data["error"] = str(error)[:200]
    emit("agent.call", data)


def record_write(entry: dict[str, Any]) -> None:
    """One ``agent.write`` event per journalled agent write (``agentkit.make_agent_router`` calls it): ids and short texts
    only, never the arguments. The full line is in the app's ``agent_journal.jsonl``."""
    data: dict[str, Any] = {"journal_id": entry.get("id"), "tool": entry.get("tool"), "ok": bool(entry.get("ok")),
                            "agent": str(entry.get("agent") or "")[:80], "session": str(entry.get("session") or "")[:120],
                            "reason": str(entry.get("reason") or "")[:200], "undoable": bool(entry.get("undoable")),
                            "objects": list(entry.get("objects") or [])[:8]}
    if entry.get("error"):
        data["error"] = str(entry["error"])[:200]
    emit("agent.write", data)


def record_undo(app: str, result: dict[str, Any], *, reason: str = "") -> None:
    """One ``agent.undo`` event after a session was undone for real (not after a dry run)."""
    counts = result.get("counts") or {}
    emit("agent.undo", {"session": str(result.get("session") or "")[:120], "agent": str(result.get("agent") or "")[:80],
                        "undone": len(result.get("undone") or []), "conflicts": len(result.get("conflicts") or []),
                        "not_undoable": len(result.get("not_undoable") or []), "complete": bool(result.get("complete")),
                        "reason": str(reason)[:200], "writes": counts.get("writes", 0)})


def health_block() -> dict[str, Any]:
    """Add as ``"hoard_link": family.health_block()`` in ``/api/health``."""
    return {"version": _library_version(), "family": FAMILY_VERSION, "events": bool(_state.get("enabled") and _state.get("app")),
            "app": _state.get("app") or None, "hub": _hub()}


def _library_version() -> str:
    try:
        from . import __version__
        return str(__version__)
    except Exception:  # noqa: BLE001
        return FAMILY_VERSION


# ---------------------------------------------------------------------------
# FastAPI helpers
# ---------------------------------------------------------------------------

class _ToolTokenMiddleware:
    """Protect direct tool routes without consuming the request body."""

    def __init__(self, app: Any, *, token_file: str) -> None:
        self.app, self.token_file = app, token_file

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        path = scope.get("path", "").rstrip("/")
        prefix = "/api/agent/"
        if scope.get("type") == "http" and scope.get("method") == "POST" and path.startswith(prefix) and path not in (prefix + "tools", prefix + "call"):
            from .tokens import check_bearer
            try:
                with open(self.token_file, encoding="utf-8") as handle:
                    expected = handle.read().strip()
            except OSError:
                expected = ""
            header = next((value.decode("latin-1") for key, value in scope.get("headers", ()) if key.lower() == b"authorization"), "")
            if not check_bearer(header, expected):
                body = b'{"error":"Invalid MCP token.","code":"unauthorized"}'
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


class _AgentCallsMiddleware:
    """Pure ASGI: records ``POST /api/agent/<tool>`` (the per-tool shape)
    as ``agent.call`` events from the response status, without touching
    the body. The shared ``/api/agent/call`` route records itself."""

    def __init__(self, app: Any, prefix: str = "/api/agent/") -> None:
        self.app = app
        self.prefix = prefix

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or scope.get("method") != "POST":
            return await self.app(scope, receive, send)
        path = str(scope.get("path") or "")
        if not path.startswith(self.prefix) or path in (self.prefix + "call", self.prefix + "tools"):
            return await self.app(scope, receive, send)
        tool = path[len(self.prefix):].strip("/")
        t0 = time.monotonic()
        status_holder = {"status": 0}

        async def send_wrapper(message: dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                status_holder["status"] = int(message.get("status") or 0)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            st = status_holder["status"]
            if tool and st:
                record_call(tool, 200 <= st < 300, int((time.monotonic() - t0) * 1000),
                            error="" if 200 <= st < 300 else f"HTTP {st}")


def _write_token_if_missing(path: str) -> str:
    """The app's token at ``path``, created when missing (kept stable across restarts). Since 0.8 this is
    :func:`hoard_link.tokens.read_or_create_token` (atomic, 0600, two starting processes agree on one token);
    any existing token of 16+ characters is kept as it is."""
    from .tokens import read_or_create_token
    return read_or_create_token(path, min_len=16)


def install_fastapi(app: Any, app_id: str, data_dir: str, *, contract: bool = True, instructions: str = "",
                    descriptions: Optional[dict[str, str]] = None, token: bool = True,
                    mcp_source: Optional[str] = None, protect_tool_routes: bool = False) -> dict[str, Any]:
    """Wire a FastAPI app into the family. With ``contract=True`` also adds
    ``GET /api/agent/tools`` and ``POST /api/agent/call`` built from the
    app's existing ``POST /api/agent/<tool>`` routes (their pydantic body
    model gives the schema; ``descriptions`` or the endpoint docstring the
    text), guarded by a bearer token written to ``<data>/mcp-token``. The
    ``protect_tool_routes=True`` also requires the token on the existing
    per-tool routes. The catalogue remains readable without a token."""
    from pathlib import Path
    data_dir = str(data_dir)
    token_file = str(Path(data_dir) / "mcp-token")
    if mcp_source:
        parsed = descriptions_from_fastmcp_source(mcp_source)
        instructions = instructions or parsed.pop("__instructions__", "")
        descriptions = {**parsed, **(descriptions or {})}
    elif descriptions and "__instructions__" in descriptions:
        descriptions = dict(descriptions)
        instructions = instructions or descriptions.pop("__instructions__")
    configure(app_id, data_dir, token_file=token_file)
    if token:
        _write_token_if_missing(token_file)
    app.add_middleware(_AgentCallsMiddleware)
    if protect_tool_routes:
        if not token:
            raise ValueError("protect_tool_routes requires token=True")
        app.add_middleware(_ToolTokenMiddleware, token_file=token_file)
    installed: dict[str, Any] = {"app": app_id, "token_file": token_file, "contract": False, "tools": []}
    if not contract:
        return installed
    if _JSONResponse is None:
        return installed
    JSONResponse = _JSONResponse

    prefix = "/api/agent/"

    def _routes() -> dict[str, Any]:
        found: dict[str, Any] = {}
        for r in getattr(app, "routes", []):
            path = getattr(r, "path", "")
            methods = getattr(r, "methods", None) or set()
            if path.startswith(prefix) and "POST" in methods and path not in (prefix + "call", prefix + "tools"):
                name = path[len(prefix):]
                if "/" in name or "{" in name:
                    continue
                found[name] = r
        return found

    def _body_model(route: Any) -> Any:
        fn = getattr(route, "endpoint", None)
        if fn is None:
            return None
        import inspect
        import typing
        # FastAPI already resolved the body model when it built the route.
        dep = getattr(route, "dependant", None)
        for field in (getattr(dep, "body_params", None) or []):
            ann = getattr(getattr(field, "field_info", None), "annotation", None) or getattr(field, "type_", None)
            if hasattr(ann, "model_json_schema") and hasattr(ann, "model_validate"):
                return ann
        try:
            hints = typing.get_type_hints(fn)
        except Exception:  # noqa: BLE001
            hints = {}
        try:
            for p in inspect.signature(fn).parameters.values():
                ann = hints.get(p.name, p.annotation)
                if hasattr(ann, "model_json_schema") and hasattr(ann, "model_validate"):
                    return ann
        except (TypeError, ValueError):
            return None
        return None

    def catalogue() -> list[dict[str, Any]]:
        out = []
        for name, route in sorted(_routes().items()):
            model = _body_model(route)
            schema = model.model_json_schema() if model is not None else {"type": "object", "properties": {}}
            schema.pop("title", None)
            doc = (descriptions or {}).get(name) or (getattr(route, "endpoint", None).__doc__ or "").strip() \
                or (getattr(model, "__doc__", "") or "").strip() or name.replace("_", " ")
            out.append({"name": name, "description": doc, "inputSchema": schema,
                        "annotations": {"readOnlyHint": _looks_read_only(name)}})
        return out

    async def tools_route(_request: _Request) -> Any:
        cat = catalogue()
        installed["tools"] = [t["name"] for t in cat]
        return {"instructions": instructions, "tools": cat, "contract": "shared", "app": app_id}

    async def call_route(request: _Request) -> Any:
        import secrets as _secrets
        header = request.headers.get("authorization", "")
        given = header[7:].strip() if header.lower().startswith("bearer ") else ""
        expected = _token()
        if token and (not given or not expected or not _secrets.compare_digest(given, expected)):
            return JSONResponse({"ok": False, "error": "Invalid MCP token."}, status_code=401)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        if not isinstance(body, dict):
            body = {}
        name = str(body.get("name") or body.get("tool") or "")
        args = body.get("arguments") or body.get("args") or {}
        routes = _routes()
        route = routes.get(name)
        if route is None:
            return JSONResponse({"ok": False, "error": f"unknown tool: {name}", "tools": sorted(routes)}, status_code=404)
        model = _body_model(route)
        t0 = time.monotonic()
        try:
            if model is not None:
                parsed = model.model_validate(args if isinstance(args, dict) else {})
                result = route.endpoint(parsed)
            else:
                result = route.endpoint()
            if hasattr(result, "__await__"):
                result = await result
        except Exception as exc:  # noqa: BLE001
            ms = int((time.monotonic() - t0) * 1000)
            status_ = getattr(exc, "status", None) or getattr(exc, "status_code", None) or 400
            msg = getattr(exc, "message", None) or str(exc)
            if type(exc).__name__ == "ValidationError":
                status_ = 400
            record_call(name, False, ms, caller=str(body.get("caller") or ""), error=msg)
            return JSONResponse({"ok": False, "error": msg, "code": getattr(exc, "code", None)}, status_code=int(status_))
        ms = int((time.monotonic() - t0) * 1000)
        record_call(name, True, ms, caller=str(body.get("caller") or ""))
        if hasattr(result, "body") and hasattr(result, "status_code"):
            return result  # already a Response
        return result

    app.add_api_route(prefix + "tools", tools_route, methods=["GET"], include_in_schema=False)
    app.add_api_route(prefix + "call", call_route, methods=["POST"], include_in_schema=False)
    # Routes match in order, and an app that serves a built frontend usually
    # ends with a catch-all (GET /{path:path} → index.html, or /api/{rest} →
    # 404). Installed last, ours would never be reached: move them first.
    routes = getattr(getattr(app, "router", None), "routes", None)
    if isinstance(routes, list) and len(routes) >= 2:
        ours = routes[-2:]
        del routes[-2:]
        routes[0:0] = ours
    installed["contract"] = True
    return installed


def descriptions_from_fastmcp_source(path: str) -> dict[str, str]:
    """Tool descriptions from a FastMCP adapter's source (``@mcp.tool`` or
    ``@tool`` functions and their docstrings), read with ``ast`` — no import of
    ``mcp`` needed. Used to give ``install_fastapi`` the same texts the
    stdio bridge shows, so the shared catalogue and the MCP one agree."""
    import ast
    try:
        tree = ast.parse(open(path, "r", encoding="utf-8").read())
    except (OSError, SyntaxError):
        return {}
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        # FastMCP("name", instructions="...") → the catalogue's instructions
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "FastMCP":
            for kw in node.keywords:
                if kw.arg == "instructions":
                    try:
                        val = ast.literal_eval(kw.value)
                    except (ValueError, SyntaxError):
                        val = None
                    if isinstance(val, str):
                        out["__instructions__"] = val
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        is_tool = False
        name = node.name
        for dec in node.decorator_list:
            target = dec.func if isinstance(dec, ast.Call) else dec
            # ``@mcp.tool`` / ``@server.tool(...)`` and a plain ``@tool`` (an adapter's own decorator)
            if (isinstance(target, ast.Attribute) and target.attr == "tool") or (isinstance(target, ast.Name) and target.id == "tool"):
                is_tool = True
                if isinstance(dec, ast.Call):
                    for kw in dec.keywords:
                        if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                            name = str(kw.value.value)
        if is_tool:
            doc = ast.get_docstring(node) or ""
            if doc:
                out[name] = doc
    return out


_READ_VERBS = ("search", "list", "get", "read", "lookup", "status", "stats", "info", "find", "recent", "check",
               "libraries", "catalog", "history", "recall", "timeline", "sources", "due", "export", "why")


def _looks_read_only(name: str) -> bool:
    parts = name.lower().split("_")
    return any(p in _READ_VERBS for p in parts) and not any(p in ("add", "set", "delete", "remove", "create", "install",
                                                                  "index", "run", "start", "stop", "write", "save",
                                                                  "update", "review", "import") for p in parts)


__all__ = ["configure", "status", "emit", "call", "chat", "link_status", "record_call", "health_block", "install_fastapi",
           "descriptions_from_fastmcp_source", "FAMILY_VERSION"]
