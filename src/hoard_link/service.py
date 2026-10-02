"""Running an app as a service: logging, ``python -m <package>`` (``run_main``), the single-page-app catch-all, one error
envelope, the PWA files and ``/api/health``.

Replaces the ``__main__.py`` of 17 apps (nine with an ``_already_running``, eight without), the logging of ``startup.py``
(Pygmalion, Galton) and the four ``RotatingFileHandler`` set-ups of the B family, the identical ``main.py`` catch-all
(17 copies) with its two exception handlers, ``api/pwa.py`` (15) and ``api/health.py`` (9). Importing needs the standard
library only (+ the commons ``net``, ``appconfig``, ``family``); ``fastapi`` / ``starlette`` / ``uvicorn`` are imported
inside the functions that use them.

Order matters in one place: ``install_spa`` adds a catch-all route, so call it **after** the routers are included
(``install_pwa`` keeps it last by itself).
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.util
import inspect
import json
import logging
import os
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import TracebackType
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence, Union

from . import family, net
from .appconfig import env_flag, env_int, env_str
from .errors import missing_dependency

__all__ = ["say", "setup_logging", "rotate_log", "install_excepthook", "install_spa", "install_error_handlers", "install_pwa",
           "health_router", "run_main", "LOG_FORMAT"]

LOG_FORMAT = "%(asctime)s %(name)s %(levelname)s %(message)s"
_OURS = "_hoard_link_log"

# ------------------------------------------------------------------------------------------------ logging


def say(message: str) -> None:
    """Print a start-up message when there is a console; never raises (under ``pythonw.exe`` ``sys.stdout`` is ``None``)."""
    stream = sys.stdout
    if stream is None:
        return
    try:
        stream.write(message + "\n")
        stream.flush()
    except Exception:  # noqa: BLE001 - a closed or broken console must not stop the app
        pass


def setup_logging(app_name: str, logs_dir: Union[str, "os.PathLike[str]"], *, level: int = logging.INFO, max_bytes: int = 2_000_000,
                  backups: int = 3, loggers: Iterable[str] = (), console: bool = True) -> Optional[Path]:
    """Log to ``<logs_dir>/<app_name>.log`` (rotating: ``max_bytes`` x ``backups``, UTF-8) and, when there is a console,
    to stderr. Returns the log path, or ``None`` when the folder cannot be written (the console handler still works).

    * The file is opened on the first record (``delay=True``): a second instance that exits early creates nothing.
    * **Idempotent**: handlers installed by an earlier call are replaced, never stacked; other handlers on the root
      logger are left alone.
    * Handlers go on the root logger (so uvicorn, httpx and the app's own loggers all land in the file); ``level`` is
      applied to the root and to each name in ``loggers`` (for a library logger that something lowered).
      ``httpx`` / ``httpcore`` are held at WARNING: one line per request would drown the log."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _OURS, False):
            root.removeHandler(handler)
            try:
                handler.close()
            except Exception:  # noqa: BLE001
                pass
    handlers: list[logging.Handler] = []
    if console and sys.stderr is not None:
        handlers.append(logging.StreamHandler(sys.stderr))
    path: Optional[Path] = None
    try:
        folder = Path(logs_dir)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{app_name}.log"
        handlers.append(RotatingFileHandler(path, maxBytes=int(max_bytes), backupCount=int(backups), encoding="utf-8", delay=True))
    except OSError:
        path = None
    if not handlers:
        handlers.append(logging.NullHandler())
    formatter = logging.Formatter(LOG_FORMAT)
    for handler in handlers:
        handler.setFormatter(formatter)
        setattr(handler, _OURS, True)
        root.addHandler(handler)
    root.setLevel(level)
    for name in loggers:
        logging.getLogger(name).setLevel(level)
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))
    return path


def rotate_log(path: Union[str, "os.PathLike[str]"], *, max_bytes: int = 2_000_000, backups: int = 3) -> bool:
    """Rotate ``path`` once (``x.log`` -> ``x.log.1`` -> ... -> ``x.log.<backups>``) when it is larger than ``max_bytes``.
    For a log a child process appends to without a ``logging`` handler (the MCP bridge's autostart). Returns whether it
    rotated; never raises (a file another process holds open on Windows is left to grow)."""
    target = Path(path)
    try:
        if not target.is_file() or target.stat().st_size <= max_bytes:
            return False
        for index in range(backups, 0, -1):
            source = target if index == 1 else target.with_name(f"{target.name}.{index - 1}")
            if not source.exists():
                continue
            destination = target.with_name(f"{target.name}.{index}")
            if destination.exists():
                destination.unlink()
            source.rename(destination)
        return True
    except OSError:
        return False


def install_excepthook(logger: logging.Logger) -> None:
    """Without a console an uncaught exception vanishes: send it to the log first."""
    previous = sys.excepthook

    def hook(kind: type[BaseException], value: BaseException, tb: Optional[TracebackType]) -> None:
        logger.critical("Uncaught exception", exc_info=(kind, value, tb))
        try:
            previous(kind, value, tb)
        except Exception:  # noqa: BLE001
            pass

    sys.excepthook = hook


# ------------------------------------------------------------------------------------------------ small helpers

def _declares(fn: Callable[..., Any], name: str) -> bool:
    try:
        return name in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


_MEDIA_TYPES = {
    ".js": "text/javascript", ".mjs": "text/javascript", ".css": "text/css", ".html": "text/html; charset=utf-8",
    ".json": "application/json", ".map": "application/json", ".svg": "image/svg+xml", ".webmanifest": "application/manifest+json",
    ".wasm": "application/wasm", ".woff": "font/woff", ".woff2": "font/woff2", ".ttf": "font/ttf", ".png": "image/png",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp", ".ico": "image/x-icon", ".txt": "text/plain",
}
_STATIC_EXTENSIONS = frozenset(_MEDIA_TYPES) | {".mp3", ".mp4", ".webm", ".pdf", ".xml", ".avif"}


def _media_type(path: Path) -> Optional[str]:
    # Windows maps .js to text/plain when the registry says so; module scripts then fail to load.
    return _MEDIA_TYPES.get(path.suffix.lower())


def _safe_file(root: Path, relative: str) -> Optional[Path]:
    """``root / relative`` when that is an existing file strictly inside ``root`` (symlinks and ``..`` resolved), else None.
    Dotfiles, ``:`` (drive letters, NTFS streams), NUL and ``..`` segments are refused outright."""
    if not relative or "\x00" in relative:
        return None
    parts = [p for p in relative.replace("\\", "/").split("/") if p]
    if not parts or any(p == ".." or p.startswith(".") or ":" in p for p in parts):
        return None
    try:
        base = root.resolve()
        candidate = (base / Path(*parts)).resolve()
    except (OSError, ValueError, RuntimeError):
        return None
    if base not in candidate.parents:
        return None
    try:
        return candidate if candidate.is_file() else None
    except OSError:
        return None


def _is_hashed(path: Path) -> bool:
    stem = path.stem
    tail = stem.rsplit("-", 1)[-1] if "-" in stem else stem.rsplit(".", 1)[-1] if "." in stem else ""
    return len(tail) >= 8 and tail.replace("_", "").isalnum()


# ------------------------------------------------------------------------------------------------ SPA

def install_spa(app: Any, static_dir: Union[str, "os.PathLike[str]"], *, api_prefix: str = "/api") -> None:
    """Serve a built single-page app (``static_dir``, the Vite ``dist``) from ``app``.

    * ``<api_prefix>/*`` that nothing matched: ``404 {"error": "Not found.", "code": "not_found"}`` (any method).
    * ``GET`` / ``HEAD`` of a path that is a file **inside** ``static_dir``: that file (``..``, encoded traversal, absolute
      and drive-letter paths, dotfiles and symlinks out of the folder are refused). Hashed ``assets/`` files are
      ``immutable``; everything else is ``no-cache`` (the stale ``index.html`` that pointed at deleted hashed assets).
    * A missing file under ``assets/`` or with a static extension: 404 (an HTML page served as a script is worse).
    * Anything else: ``index.html`` with ``Cache-Control: no-cache`` (client-side routes).
    * ``static_dir`` not built (no ``index.html``): ``503 {"error": ..., "code": "not_built"}``."""
    from starlette.requests import Request
    from starlette.responses import FileResponse, JSONResponse, Response

    root = Path(static_dir)
    prefix = "/" + api_prefix.strip("/")

    async def api_not_found(request: Request) -> Response:
        return JSONResponse({"error": "Not found.", "code": "not_found"}, status_code=404)

    async def spa(request: Request) -> Response:
        relative = request.path_params.get("path", "")
        found = _safe_file(root, relative)
        if found is not None:
            headers = {"Cache-Control": "public, max-age=31536000, immutable"
                       if relative.replace("\\", "/").startswith("assets/") and _is_hashed(found) else "no-cache"}
            return FileResponse(found, media_type=_media_type(found), headers=headers)
        last = Path(relative.replace("\\", "/")).suffix.lower() if relative else ""
        if relative.replace("\\", "/").startswith("assets/") or last in _STATIC_EXTENSIONS:
            return JSONResponse({"error": "Not found.", "code": "not_found"}, status_code=404)
        index = root / "index.html"
        if index.is_file():
            return FileResponse(index, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-cache"})
        return JSONResponse({"error": "The client is not built yet: run `npm install && npm run build`.", "code": "not_built"},
                            status_code=503)

    app.add_route(prefix, api_not_found, methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"], include_in_schema=False)
    app.add_route(prefix + "/{rest:path}", api_not_found, methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
                  include_in_schema=False)
    app.add_route("/{path:path}", spa, methods=["GET", "HEAD"], include_in_schema=False)
    app.state.hoard_spa = (prefix, str(root))


def _keep_spa_last(app: Any) -> None:
    """Move the catch-all routes ``install_spa`` added to the end (routes added after it would never match)."""
    routes = getattr(getattr(app, "router", None), "routes", None)
    if not isinstance(routes, list) or not getattr(getattr(app, "state", None), "hoard_spa", None):
        return
    prefix = app.state.hoard_spa[0]
    tail = [r for r in routes if getattr(r, "path", "") in ("/{path:path}", prefix, prefix + "/{rest:path}")]
    if tail:
        routes[:] = [r for r in routes if r not in tail] + tail


# ------------------------------------------------------------------------------------------------ errors

def install_error_handlers(app: Any) -> None:
    """One error envelope for the whole app: ``{"error": "<text>", "code"?, "hint"?, "details"?, "issues"?}``.

    * ``HTTPException`` (any detail; a dict detail with ``error`` is passed through) -> its status.
    * Request validation -> 400 ``{"error": "loc: msg; loc: msg", "code": "invalid_arguments", "issues": [...]}``.
    * :class:`hoard_link.agentkit.AppError` (and subclasses) -> its status and ``to_dict()``.
    * Anything else -> 500 ``{"error": "Internal error: <Type>: <msg>", "code": "internal"}``, logged with the traceback."""
    from starlette.exceptions import HTTPException as StarletteHTTPException
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    from .agentkit import AppError, issues_of, format_issues

    log = logging.getLogger("hoard_link.service")

    async def http_error(request: Request, exc: Any) -> Any:
        detail = exc.detail
        body = dict(detail) if isinstance(detail, dict) and "error" in detail else {"error": str(detail)}
        return JSONResponse(body, status_code=exc.status_code, headers=getattr(exc, "headers", None))

    async def validation_error(request: Request, exc: Any) -> Any:
        return JSONResponse({"error": format_issues(exc), "code": "invalid_arguments", "issues": issues_of(exc)}, status_code=400)

    async def app_error(request: Request, exc: Any) -> Any:
        return JSONResponse(exc.to_dict(), status_code=exc.status)

    async def invalid_value(request: Request, exc: Any) -> Any:
        return JSONResponse({"error": str(exc), "code": "invalid"}, status_code=400)

    async def internal(request: Request, exc: Exception) -> Any:
        log.error("Unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
        return JSONResponse({"error": f"Internal error: {type(exc).__name__}: {str(exc)[:200]}", "code": "internal"}, status_code=500)

    app.add_exception_handler(StarletteHTTPException, http_error)
    try:
        from fastapi.exceptions import RequestValidationError
    except ImportError:  # plain Starlette: no request validation
        RequestValidationError = None  # type: ignore[assignment]
    if RequestValidationError is not None:
        app.add_exception_handler(RequestValidationError, validation_error)
    try:
        from pydantic import ValidationError
    except ImportError:
        ValidationError = None
    if ValidationError is not None:
        app.add_exception_handler(ValidationError, validation_error)
    app.add_exception_handler(ValueError, invalid_value)
    app.add_exception_handler(AppError, app_error)
    app.add_exception_handler(Exception, internal)


# ------------------------------------------------------------------------------------------------ PWA

_DEFAULT_ICONS = (
    {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
    {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
)

_SW_TEMPLATE = """// Service worker: installability plus a cache for the built, content-hashed /assets/ files.
//  * navigations (the page): network first, so a new build is never hidden behind an old index.html; the cached copy only
//    answers when the network fails;
//  * /assets/: cache first (their names carry a hash);
//  * /api/, other origins, everything else: normal browser handling (network).
// The cache is named after the build (the hash of index.html): activating a new worker deletes the older caches.
const CACHE_NAME = __CACHE__;

self.addEventListener("install", () => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const names = await caches.keys();
      await Promise.all(names.filter((name) => name !== CACHE_NAME).map((name) => caches.delete(name)));
      await self.clients.claim();
    })(),
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/")) return; // network only, never cached

  if (request.mode === "navigate") {
    event.respondWith(
      (async () => {
        try {
          const response = await fetch(request);
          if (response.ok) {
            const cache = await caches.open(CACHE_NAME);
            cache.put(request, response.clone());
          }
          return response;
        } catch (error) {
          const cached = await caches.match(request);
          if (cached) return cached;
          throw error;
        }
      })(),
    );
    return;
  }

  if (!url.pathname.startsWith("/assets/")) return;
  event.respondWith(
    (async () => {
      const cache = await caches.open(CACHE_NAME);
      const cached = await cache.match(request);
      if (cached) return cached;
      const response = await fetch(request);
      if (response.ok) cache.put(request, response.clone());
      return response;
    })(),
  );
});
"""


def _build_id(static_dir: Optional[Path], version: str) -> str:
    if static_dir is not None:
        try:
            return hashlib.sha256((static_dir / "index.html").read_bytes()).hexdigest()[:12]
        except OSError:
            pass
    return version


def install_pwa(app: Any, *, name: str, short_name: str, theme: str, background: str, cache: str, icons: Sequence[Mapping[str, Any]] = (),
                start_url: str = "/", lang: str = "en", static_dir: Union[str, "os.PathLike[str]", None] = None,
                version: str = "0") -> None:
    """``/manifest.webmanifest`` and ``/sw.js`` (the worker described in the module's template: network-first navigations,
    cache-first ``/assets/``, never ``/api/``). ``cache`` is the cache-name prefix (``"kafka-hoard"``); the build id after it is
    the hash of ``<static_dir>/index.html`` (so a new build starts a new cache) or ``version`` when there is no built client.
    ``icons`` empty means the two standard ``/icon-192.png`` / ``/icon-512.png``."""
    from starlette.requests import Request
    from starlette.responses import Response

    manifest = {"name": name, "short_name": short_name, "start_url": start_url, "display": "standalone", "background_color": background,
                "theme_color": theme, "lang": lang, "icons": [dict(i) for i in (icons or _DEFAULT_ICONS)]}
    folder = Path(static_dir) if static_dir else None

    async def manifest_route(request: Request) -> Response:
        return Response(json.dumps(manifest), media_type="application/manifest+json", headers={"Cache-Control": "no-cache"})

    async def sw_route(request: Request) -> Response:
        script = _SW_TEMPLATE.replace("__CACHE__", json.dumps(f"{cache}-{_build_id(folder, version)}"))
        return Response(script, media_type="application/javascript", headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})

    app.add_route("/manifest.webmanifest", manifest_route, methods=["GET", "HEAD"], include_in_schema=False)
    app.add_route("/sw.js", sw_route, methods=["GET", "HEAD"], include_in_schema=False)
    _keep_spa_last(app)


# ------------------------------------------------------------------------------------------------ health

def health_router(service: str, version: str, *, extra: Union[Mapping[str, Any], Callable[..., Mapping[str, Any]], None] = None) -> Any:
    """``GET /api/health`` -> ``{"service", "version", **extra, "hoard_link": family.health_block()}``.

    ``service`` (``"kafka-hoard"``) is what the launcher, the MCP bridge, :func:`hoard_link.net.already_running` and the hub
    match on. ``extra`` is a dict or a callable returning one (it may declare a ``request`` parameter and gets the Starlette
    request); keep it cheap, the launcher polls this. A failing ``extra`` never fails the probe: the answer stays 200 with
    ``"health_error"``. ``service`` and ``hoard_link`` cannot be overridden by ``extra``."""
    from fastapi import APIRouter, Request

    router = APIRouter()
    log = logging.getLogger("hoard_link.service")

    def health(request: Request) -> dict[str, Any]:
        body: dict[str, Any] = {"service": service, "version": version}
        if extra is not None:
            try:
                data = extra(request) if callable(extra) and _declares(extra, "request") else extra() if callable(extra) else extra
                body.update(dict(data or {}))
            except Exception as error:  # noqa: BLE001 - the probe must answer
                log.warning("health extra failed: %s", error)
                body["health_error"] = f"{type(error).__name__}: {str(error)[:200]}"
        body["service"] = service
        body["hoard_link"] = family.health_block()
        return body

    # This module uses ``from __future__ import annotations`` and ``Request`` is imported here, so tell fastapi what the parameter is.
    health.__annotations__ = {"request": Request, "return": dict}
    router.add_api_route("/api/health", health, methods=["GET"])
    return router


# ------------------------------------------------------------------------------------------------ python -m <package>

def _load_factory(spec: Union[str, Callable[..., Any]]) -> Callable[[Optional[int]], Any]:
    """``"pkg.main:create_app"`` (a zero-argument factory, optionally taking ``port``) or ``"pkg.main:app"`` (the ASGI application
    itself) or a callable, as a function ``(port) -> asgi_app``."""
    if isinstance(spec, str):
        module_name, _, attr = spec.partition(":")
        if not module_name or not attr:
            raise ValueError(f"app_factory must look like 'package.module:name', got {spec!r}")
        target: Any = getattr(importlib.import_module(module_name), attr)
    else:
        target = spec
    is_factory = inspect.isfunction(target) or inspect.ismethod(target) or inspect.isclass(target) or hasattr(target, "func")

    def build(port: Optional[int]) -> Any:
        if not is_factory:
            return target
        return target(port=port) if _declares(target, "port") else target()

    return build


def _default_data_dir(package: str) -> Path:
    try:
        spec = importlib.util.find_spec(package)
        if spec and spec.origin:
            return Path(spec.origin).resolve().parent.parent / "data"
    except (ImportError, ValueError):
        pass
    return Path.cwd() / "data"


def _open_when_up(url: str, service: str) -> None:
    def work() -> None:
        if net.wait_healthy(url, service, timeout=30.0):
            net.open_in_browser(url)

    threading.Thread(target=work, name="hoard-open-browser", daemon=True).start()


def run_main(*, service: str, package: str, default_port: int, app_factory: Union[str, Callable[..., Any]], data_dir_env: Optional[str] = None,
             port_env: Optional[str] = None, argv: Optional[Sequence[str]] = None, open_browser_default: bool = True,
             default_data_dir: Union[str, "os.PathLike[str]", Callable[[], Any], None] = None,
             title: Optional[str] = None, serve: Optional[Callable[..., Any]] = None) -> int:
    """The whole ``python -m <package>``: ``raise SystemExit(run_main(service="kafka-hoard", package="kafka_hoard", default_port=5200,
    app_factory="kafka_hoard.main:create_app", data_dir_env="KAFKA_DATA_DIR", port_env="KAFKA_PORT"))``.

    1. ``argparse``: ``--port``, ``--data-dir``, ``--host`` (default 127.0.0.1), ``--no-browser`` / ``--browser``.
    2. **Before touching the data folder**: if this very app already answers ``/api/health`` on the wanted port
       (:func:`hoard_link.net.already_running`) print that, optionally open the browser at it, and return **0**. (Eight
       apps used to rotate the token and migrate the database first.)
    3. Port: the wanted one is ``--port``, else ``port_env``, else ``default_port``. With ``PORT_STRICT=1`` a taken port is an
       error (return 1: another program has it); otherwise the next free one (``net.find_available_port``) is used.
    4. ``--data-dir`` / ``data_dir_env`` and the chosen port are exported to the environment (``data_dir_env``, ``port_env`` or
       ``HOARD_DATA_DIR`` / ``HOARD_PORT``) so the app's ``Config.from_env()`` sees them, then logging goes to
       ``<data>/logs/<service>.log`` (:func:`setup_logging`) and the uncaught-exception hook is installed.
    5. ``app_factory`` (``"pkg.main:create_app"``, ``"pkg.main:app"`` or a callable; a factory may take a ``port`` argument) builds
       the ASGI app and ``uvicorn.run`` serves it (``serve(app, host=, port=)`` replaces uvicorn, for tests and other servers).

    The browser opens only when ``open_browser_default`` is true, ``--no-browser`` is absent and ``HOARD_NO_BROWSER`` is unset
    (the MCP bridge's autostart sets it). Returns the exit code: 0 after a clean stop or an already-running app, 1 otherwise."""
    parser = argparse.ArgumentParser(prog=f"python -m {package}", description=title or service)
    parser.add_argument("--port", type=int, default=None, help=f"preferred port (default {default_port})")
    parser.add_argument("--data-dir", default=None, help="data folder")
    parser.add_argument("--host", default="127.0.0.1", help="address to listen on (default 127.0.0.1)")
    parser.add_argument("--no-browser", action="store_true", help="do not open the browser")
    parser.add_argument("--browser", action="store_true", help="open the browser even if this app does not by default")
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exit_:
        return int(exit_.code or 0) if isinstance(exit_.code, int) else 2

    shown = title or service
    host = args.host
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    data_key = data_dir_env or "HOARD_DATA_DIR"
    port_key = port_env or "HOARD_PORT"
    preferred = args.port if args.port is not None else env_int(port_env, default=default_port) if port_env else default_port
    if not preferred or not 1 <= int(preferred) <= 65535:
        say(f"Invalid port: {preferred!r}")
        return 2
    preferred = int(preferred)
    strict = env_flag("PORT_STRICT", False)
    want_browser = (open_browser_default or args.browser) and not args.no_browser and not env_flag("HOARD_NO_BROWSER", False)

    if net.already_running(service, preferred, host=probe_host):
        url = f"http://{probe_host}:{preferred}"
        say(f"{shown} is already running on {url}")
        if want_browser:
            net.open_in_browser(url)
        return 0
    if strict and not net.can_listen(preferred, host):
        say(f"Port {preferred} is taken by another program (PORT_STRICT=1).")
        return 1
    try:
        port = preferred if strict else net.find_available_port(preferred, host=host)
    except (RuntimeError, ValueError) as error:
        say(str(error))
        return 1

    if args.data_dir:
        os.environ[data_key] = str(Path(args.data_dir))
    data_dir = Path(env_str(data_key) or (default_data_dir() if callable(default_data_dir) else default_data_dir) or _default_data_dir(package))
    os.environ[port_key] = str(port)
    log = logging.getLogger(package.split(".")[0])
    setup_logging(service, data_dir / "logs", loggers=(package.split(".")[0],))
    install_excepthook(log)

    url = f"http://{probe_host}:{port}"
    try:
        app = _load_factory(app_factory)(port)
    except Exception:  # noqa: BLE001
        log.critical("%s could not start: the app failed to build", shown, exc_info=True)
        say(f"{shown} could not start (see the log in {data_dir / 'logs'}).")
        return 1
    note = f"{shown} listening on {url}"
    log.info("%s - data in %s", note, data_dir)
    say(note)
    if want_browser:
        _open_when_up(url, service)
    try:
        if serve is not None:
            serve(app, host=host, port=port)
        else:
            try:
                import uvicorn
            except ImportError:
                error = missing_dependency("uvicorn", "serving the app", pip_name="uvicorn")
                log.critical("%s", error)
                say(str(error))
                return 1
            # log_config=None: uvicorn's default formatter asks the console whether it is a terminal, which fails without one.
            uvicorn.run(app, host=host, port=port, log_level="warning", log_config=None)
    except KeyboardInterrupt:
        return 0
    except SystemExit as exit_:
        return int(exit_.code or 0) if isinstance(exit_.code, int) else 1
    except Exception:  # noqa: BLE001
        log.critical("%s stopped with an error", shown, exc_info=True)
        return 1
    return 0
