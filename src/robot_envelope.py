"""Robot mode — the uniform envelope every machine-facing read answers with.

A coordinating model (Fable/Claude through the MCP server, a script holding an
API token) reads Faustus's own output. Every read endpoint historically shaped
its answer for the browser page that calls it: sometimes ``{"status":
"success", ...}``, sometimes a bare object, sometimes a list. A machine reader
then needs one branch per endpoint to find out whether the call worked.

Robot mode gives all of them the SAME outer shape::

    {"ok": bool, "data": ..., "error_code": str|None, "error": str|None,
     "elapsed_ms": int, "schema_version": int}

``ok`` is exactly ``error_code is None`` — one field to test, on success and on
failure, including the failures FastAPI would otherwise answer with a bare
``{"detail": ...}``. ``elapsed_ms`` is measured from a ``time.monotonic()``
stamp taken when the route started, so it cannot go backwards across a clock
change. ``schema_version`` lets a coordinator notice the day the shape moves.

The body is rendered as JSON (``format=json``, the default) or as TOON
(``format=toon``, ``src/toon.py`` — the compact form, ~40-60 % fewer characters
on tabular payloads).

Turning it on is per request and never changes a default answer:

* ``?robot=1`` — envelope, in JSON;
* ``?format=toon`` — envelope, as ``text/plain`` TOON (implies robot mode);
* no query parameter at all — the endpoint answers exactly as it always did,
  byte for byte. The browser pages keep working with no change.

Routes use it in three lines — `reply` in an ``async def`` route,
`reply_sync` in a plain ``def`` one (which FastAPI keeps running in its
threadpool, so a blocking payload builder stays off the event loop)::

    if robot_envelope.wants(request):
        return await robot_envelope.reply(request, lambda: _payload())
    return _payload()

Everything here is defensive: both replies turn an ``HTTPException`` into an
envelope carrying the same HTTP status, any other exception into a 500
envelope, and never let a rendering problem escape into the response path.
Stdlib only (FastAPI's response classes are imported lazily, inside the
reply helpers).
"""

from __future__ import annotations

import inspect
import json
import time
from typing import Any, Awaitable, Callable, Dict, Optional, Union

from src import toon

__all__ = ["envelope", "render", "wants", "fmt_of", "reply", "reply_sync",
           "SCHEMA_VERSION"]

SCHEMA_VERSION = 1
_TRUE = ("1", "true", "yes", "on")
_TOON_MEDIA = "text/plain; charset=utf-8"


def envelope(data: Any = None, *, error_code: Optional[str] = None,
             error: Optional[str] = None, started_at: Optional[float] = None,
             schema_version: int = SCHEMA_VERSION) -> Dict[str, Any]:
    """The standard payload. `started_at` is a `time.monotonic()` value."""
    elapsed = 0
    if started_at is not None:
        try:
            elapsed = int(max(0.0, time.monotonic() - float(started_at)) * 1000)
        except (TypeError, ValueError):
            elapsed = 0
    try:
        version = int(schema_version)
    except (TypeError, ValueError):
        version = SCHEMA_VERSION
    return {
        "ok": error_code is None,
        "data": data,
        "error_code": error_code,
        "error": error,
        "elapsed_ms": elapsed,
        "schema_version": version,
    }


def render(payload: Any, fmt: str) -> str:
    """The envelope as text: TOON for "toon", JSON for anything else. This is
    the standalone form (a file, a log line, a test); over HTTP the JSON side
    goes out through Starlette's JSONResponse, which writes the same object
    with compact separators."""
    if str(fmt or "").strip().lower() == "toon":
        return toon.encode(payload)
    try:
        return json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        return json.dumps(payload, ensure_ascii=False, default=str)


# ── the request side ────────────────────────────────────────────────────────

def _query(request: Any, name: str) -> str:
    try:
        return str(request.query_params.get(name) or "").strip().lower()
    except Exception:  # noqa: BLE001 - a stub request must not break a route
        return ""


def fmt_of(request: Any) -> str:
    """"toon" when the caller asked for it, else "json"."""
    return "toon" if _query(request, "format") == "toon" else "json"


def wants(request: Any) -> bool:
    """True when this request asked for robot mode (`robot=1` or `format=toon`).
    False for every browser call, which is what keeps the default answers
    byte-identical."""
    return _query(request, "robot") in _TRUE or _query(request, "format") == "toon"


def _response(request: Any, payload: Dict[str, Any], status: int):
    from fastapi.responses import JSONResponse, PlainTextResponse
    fmt = fmt_of(request)
    if fmt == "toon":
        return PlainTextResponse(render(payload, "toon"), status_code=status,
                                 media_type=_TOON_MEDIA)
    return JSONResponse(payload, status_code=status)


def _failure(exc: BaseException):
    """(status, error_code, error) for an exception a payload builder raised."""
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):          # fastapi.HTTPException and friends
        detail = getattr(exc, "detail", "")
        return status, f"http_{status}", str(detail if detail is not None else "")[:800]
    return 500, "internal_error", str(exc)[:800]


def _finish(request: Any, data: Any, status: int, code: Optional[str],
            message: Optional[str], started_at: float, schema_version: int):
    payload = envelope(data, error_code=code, error=message, started_at=started_at,
                       schema_version=schema_version)
    try:
        return _response(request, payload, status)
    except Exception:  # noqa: BLE001 - last resort: plain JSON, never a crash
        from fastapi.responses import JSONResponse
        return JSONResponse(envelope(None, error_code="render_failed",
                                     error="could not render the answer",
                                     started_at=started_at), status_code=500)


def reply_sync(request: Any, factory: Callable[[], Any], *,
               started_at: Optional[float] = None,
               schema_version: int = SCHEMA_VERSION):
    """`reply` for a plain ``def`` route: run `factory` and answer with the
    envelope, including when it raises."""
    t0 = time.monotonic() if started_at is None else started_at
    try:
        data = factory()
    except Exception as exc:  # noqa: BLE001 - the envelope is the error channel
        status, code, message = _failure(exc)
        return _finish(request, None, status, code, message, t0, schema_version)
    return _finish(request, data, 200, None, None, t0, schema_version)


async def reply(request: Any, factory: Union[Callable[[], Any], Callable[[], Awaitable[Any]]],
                *, started_at: Optional[float] = None, schema_version: int = SCHEMA_VERSION):
    """Run `factory` (sync or async) and answer with the envelope — including
    when it raises: an HTTPException keeps its status and becomes
    ``http_<status>``, anything else becomes a 500 ``internal_error``. The
    coordinator never sees a bare FastAPI error body."""
    t0 = time.monotonic() if started_at is None else started_at
    try:
        data = factory()
        if inspect.isawaitable(data):
            data = await data
    except Exception as exc:  # noqa: BLE001 - the envelope is the error channel
        status, code, message = _failure(exc)
        return _finish(request, None, status, code, message, t0, schema_version)
    return _finish(request, data, 200, None, None, t0, schema_version)
