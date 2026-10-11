"""The MCP stdio bridge every app used to copy (``mcp_server.py``, ~14 near-identical files of 143-167 lines).

The bridge never opens the app's database: it lists the tools from ``GET /api/agent/tools`` and proxies every call to the
running app (``POST /api/agent/call``) with the token from ``<data>/mcp-token``. An app's whole ``mcp_server.py`` becomes::

    from kafka_hoard.hoard_link.bridge import CatalogBridge
    CatalogBridge(app="kafka", service="kafka-hoard", package="kafka_hoard", default_port=5200, data_dir_env="KAFKA_DATA_DIR",
                  title="Kafka's Hoard", root=__file__).run_bridge()

Behaviour (the union of what the copies did, plus what only the Node bridges had):

* The catalogue is **refreshed** when it is older than ``refresh_tools_s`` or when a call names an unknown tool (the old
  bridges listed once at start, so an app that started later or gained a tool stayed stale until a restart).
* **Per-tool timeout**: ``tool_timeouts`` (``{"pdf_*": 175}``) > the catalogue's ``x-timeout-s`` > ``default_timeout``; a
  ``wait_s`` argument stretches it to ``wait_s + 30`` s. Calls that outlast 10 s send ``report_progress`` **heartbeats** (when
  the client asked for progress) so clients that reset their timeout on progress keep waiting.
* A tool that is not read-only and **times out or loses the connection after sending** answers ``outcome_unknown`` (the write
  may have happened: read the state before retrying), like the Node ledger bridge. Before, a Python bridge gave an error
  indistinguishable from "nothing happened".
* Error envelopes of the app (``code``, ``hint``, ``issues``, ``details``, ``candidates``...) are forwarded; 401 says which token
  file was refused; a missing token file is not reported as a stopped app.
* ``httpx`` with ``trust_env=False`` (a ``HTTP_PROXY`` must never capture loopback), or ``urllib`` when httpx is missing.
* **Autostart** (:func:`ensure_running`): when nothing answers, a short-lived launcher starts ``python -m <package>`` and exits,
  so the app server is no longer a descendant of the MCP host. It has no console window, gets ``PORT_STRICT=1`` and no browser,
  logs to ``<data>/logs/<app>-app.log`` (rotated), and can be disabled with ``<APP>_BRIDGE_AUTOSTART=0``.
* ``mcp`` 1.x (``FastMCP``) and 2.x (``MCPServer``) are both supported; it is imported only by :meth:`CatalogBridge.build_server`.

Environment variables are derived from ``app`` (``"kafka"`` -> ``KAFKA_URL``, ``KAFKA_PORT``, ``KAFKA_TOKEN``,
``KAFKA_TOKEN_FILE``, ``KAFKA_BRIDGE_AUTOSTART``; ``env_prefix`` overrides) plus ``data_dir_env``.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Optional, Sequence, Union
from urllib.parse import urlparse

from . import launch, net, proc, service as service_mod, tokens
from .appconfig import env_flag
from .waiting import clamp_wait

__all__ = ["CatalogBridge", "BridgeResult", "ensure_running", "bridge_token", "tool_timeout", "FORWARDED_KEYS"]

log = logging.getLogger("hoard_link.bridge")

# What an app's error envelope may carry that the caller (the model) should see.
FORWARDED_KEYS = ("error", "code", "hint", "issues", "details", "candidates", "key", "params")
_ANNOTATION_KEYS = ("title", "readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")
LOCAL_BRIDGE_HOSTS = ("127.0.0.1", "localhost", "::1", "[::1]")

NOT_RUNNING = "Open {title} (python -m {package}) so the assistant can reach it."
NO_TOKEN = ("{title} is running, but this bridge has no access token at {path}. The app writes it as mcp-token in its data folder; "
            "point {prefix}_TOKEN_FILE at that file (the data folder is {data_dir_env} when the app was started with one).")
TOKEN_REFUSED = ("{title} is running but refused this bridge's token ({path}): the file belongs to another data folder. "
                 "Point {prefix}_TOKEN_FILE at the mcp-token of the running app's data folder.")
OUTCOME_UNKNOWN = ("No answer was received for {name}. The change may have been applied: read the current state before "
                   "repeating it.")


def _prefix_of(app: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", app.upper()).strip("_")


def tool_timeout(tool: Optional[Mapping[str, Any]], default: float, *, arguments: Optional[Mapping[str, Any]] = None,
                 overrides: Optional[Mapping[str, float]] = None) -> float:
    """Seconds the bridge waits for ``tool`` (a catalogue entry): ``overrides`` (exact names or ``fnmatch`` patterns) first, then
    the entry's ``x-timeout-s``, else ``default``; a numeric ``wait_s`` argument raises it to ``clamp_wait(wait_s) + 30``."""
    name = str((tool or {}).get("name") or "")
    value: Optional[float] = None
    for pattern, seconds in (overrides or {}).items():
        if pattern == name or fnmatch.fnmatchcase(name, pattern):
            value = float(seconds)
            break
    if value is None and tool:
        declared = tool.get("x-timeout-s")
        if declared is None and isinstance(tool.get("inputSchema"), Mapping):
            declared = tool["inputSchema"].get("x-timeout-s")
        try:
            value = float(declared) if declared is not None else None
        except (TypeError, ValueError):
            value = None
    base = value if value and value > 0 else float(default)
    wait = (arguments or {}).get("wait_s")
    if isinstance(wait, (int, float)) and not isinstance(wait, bool):
        base = max(base, clamp_wait(wait) + 30.0)
    return base


def bridge_token(app: str, data_dir: Union[str, "os.PathLike[str]", None] = None, *, token_env: Optional[str] = None,
                 token_file: Union[str, "os.PathLike[str]", None] = None, env_prefix: Optional[str] = None) -> str:
    """The token to send: ``$<APP>_TOKEN`` (or ``token_env``), else the file ``$<APP>_TOKEN_FILE`` / ``token_file`` /
    ``<data_dir>/mcp-token``. Re-read on every call (the app may have created or replaced it since). Raises
    ``FileNotFoundError`` (naming the file) when there is none, so a caller can say which file is missing."""
    prefix = env_prefix or _prefix_of(app)
    given = os.environ.get(token_env or f"{prefix}_TOKEN")
    if given and given.strip():
        return given.strip()
    path = Path(os.environ.get(f"{prefix}_TOKEN_FILE") or token_file or (Path(data_dir) / "mcp-token" if data_dir else "mcp-token"))
    value = tokens.read_token(path, min_len=16)
    if value is None:
        raise FileNotFoundError(str(path))
    return value


# ------------------------------------------------------------------------------------------------ autostart

_launch_lock = threading.Lock()
_children: dict[str, tuple[int, Optional[float]]] = {}


def _healthy(port: int, service: str, host: str = "127.0.0.1") -> bool:
    """The app answers ``/api/health`` as ``service``. Two tries with growing timeouts (a long check or render in flight must
    not look like a dead app) - but a port nobody listens on answers at once."""
    if net.can_listen(port, host):
        return False
    url = f"http://{host}:{port}/api/health"
    for timeout in (4.0, 8.0):
        data = net.fetch_health(url, timeout=timeout)
        if data is not None and data.get("service") == service:
            return True
    return False


def ensure_running(package: str, port: int, *, service: str, data_dir: Union[str, "os.PathLike[str]"], wait_s: float = 45.0,
                   env: Optional[Mapping[str, str]] = None, cwd: Union[str, "os.PathLike[str]", None] = None,
                   port_env: Optional[str] = None, log_name: Optional[str] = None, args: Sequence[str] = ()) -> bool:
    """True once ``service`` answers on ``port``; when it does not, start ``python -m <package>`` detached (no console window,
    ``PORT_STRICT=1``, no browser, ``<port_env>`` = ``port``) from ``cwd`` and wait up to ``wait_s`` seconds. Output goes to
    ``<data_dir>/logs/<log_name or service>-app.log``, rotated at 2 MB. Returns False when the child exits with an error or
    the wait runs out. One child per package per process; a second caller waits for the first."""
    if _healthy(port, service):
        return True
    logs = Path(data_dir) / "logs"
    key = f"{package}:{port}"
    with _launch_lock:
        child = _children.get(key)
        if child is None or not launch.process_alive(*child):
            logs.mkdir(parents=True, exist_ok=True)
            target = logs / f"{log_name or service.removesuffix('-hoard')}-app.log"
            service_mod.rotate_log(target)
            environment = {**os.environ, **{k: str(v) for k, v in (env or {}).items()}, port_env or "HOARD_PORT": str(port),
                           "PORT_STRICT": "1", "PYTHONUNBUFFERED": "1", "HOARD_NO_BROWSER": "1"}
            child_pid = launch.spawn_orphan([sys.executable, "-m", package, *args], str(cwd) if cwd else None,
                                            environment, target)
            created = launch.process_created(child_pid)
            if created is None:
                log.warning("%s exited before its process identity could be recorded; see %s (exit code unavailable)",
                            package, target)
                return False
            child = (child_pid, created)
            _children[key] = child
    deadline = time.monotonic() + max(0.0, wait_s)
    while True:
        if _healthy(port, service):
            return True
        if not launch.process_alive(*child):
            log.warning("%s exited while starting; see %s (exit code unavailable)", package, target)
            return False
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.4)


# ------------------------------------------------------------------------------------------------ transport

class _ConnectFailed(Exception):
    """Nothing was listening: the request never reached the app."""


class _NoAnswer(Exception):
    """The request may have reached the app but no (complete) answer came back."""

    def __init__(self, message: str, *, timed_out: bool = False) -> None:
        super().__init__(message)
        self.timed_out = timed_out


def _parse(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return None


async def _post_httpx(url: str, payload: Any, headers: Mapping[str, str], timeout: float) -> tuple[int, Any, str]:
    import httpx

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=min(10.0, timeout)), trust_env=False) as client:
            response = await client.post(url, json=payload, headers=dict(headers))
    except (httpx.ConnectError, httpx.ConnectTimeout) as error:
        raise _ConnectFailed(str(error)) from error
    except httpx.TimeoutException as error:
        raise _NoAnswer(f"timed out after {timeout:g} s", timed_out=True) from error
    except httpx.HTTPError as error:
        raise _NoAnswer(f"{type(error).__name__}: {error}") from error
    return response.status_code, _parse(response.content), response.text[:300]


def _post_urllib_blocking(url: str, payload: Any, headers: Mapping[str, str], timeout: float) -> tuple[int, Any, str]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json", **dict(headers)})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read()
            return response.status, _parse(raw), raw.decode("utf-8", "replace")[:300]
    except urllib.error.HTTPError as error:
        raw = error.read()
        return error.code, _parse(raw), raw.decode("utf-8", "replace")[:300]
    except urllib.error.URLError as error:
        reason = error.reason
        if isinstance(reason, (ConnectionRefusedError, ConnectionAbortedError)) or (isinstance(reason, OSError) and getattr(reason, "errno", None) in (111, 10061)):
            raise _ConnectFailed(str(reason)) from error
        if isinstance(reason, TimeoutError):
            raise _NoAnswer(f"timed out after {timeout:g} s", timed_out=True) from error
        raise _ConnectFailed(str(reason)) from error
    except TimeoutError as error:
        raise _NoAnswer(f"timed out after {timeout:g} s", timed_out=True) from error
    except OSError as error:
        raise _NoAnswer(f"{type(error).__name__}: {error}") from error


async def _post(url: str, payload: Any, headers: Mapping[str, str], timeout: float) -> tuple[int, Any, str]:
    try:
        import httpx  # noqa: F401
    except ImportError:
        return await asyncio.to_thread(_post_urllib_blocking, url, payload, headers, timeout)
    return await _post_httpx(url, payload, headers, timeout)


def _get_blocking(url: str, timeout: float) -> Any:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


# ------------------------------------------------------------------------------------------------ the bridge

@dataclass
class BridgeResult:
    """What one proxied call produced: MCP content parts (``{"type": "text", "text": ...}`` or an image part), whether it is an
    error, and the decoded body (None when there was none)."""

    content: list[dict[str, Any]]
    is_error: bool = False
    body: Any = None


def agent_headers(env: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    """``X-Agent-Id`` / ``X-Agent-Session`` from ``HOARD_AGENT_ID`` / ``HOARD_AGENT_SESSION``: whoever launches the MCP
    server (an editor, a coding agent, the Hub) sets them so the app can tell which agent and which session a call came from
    (journal, reasons, undo of a whole session). Printable ASCII only, 80 / 120 characters at most; empty values are left out."""
    env = os.environ if env is None else env
    out: dict[str, str] = {}
    for name, header, limit in (("HOARD_AGENT_ID", "X-Agent-Id", 80), ("HOARD_AGENT_SESSION", "X-Agent-Session", 120)):
        value = "".join(c for c in str(env.get(name) or "") if " " <= c <= "~").strip()[:limit]
        if value:
            out[header] = value
    return out


def _text(payload: Any) -> dict[str, Any]:
    return {"type": "text", "text": payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)}


Progress = Callable[[float, str], Awaitable[None]]


class CatalogBridge:
    """A stdio MCP server whose tools are the running app's catalogue. See the module docstring; ``run_bridge()`` is the entry point.

    ``app`` short name (``"kafka"``); ``service`` the ``/api/health`` name (``"kafka-hoard"``); ``package`` what ``python -m`` starts;
    ``default_port``; ``data_dir_env`` the variable holding the data folder (default ``<data>`` is ``<root>/data``, ``root`` being the
    folder of ``mcp_server.py``: pass ``root=__file__``). ``token_env`` / ``token_file`` / ``url_file`` override where the token and
    the app's URL come from (``url_file`` is the ``data/url`` the app records; ``<APP>_URL`` wins over it). ``tool_timeouts``,
    ``default_timeout``, ``refresh_tools_s``, ``autostart``, ``image_content`` (a ``_image: {data, mime}`` in a result becomes an MCP
    image part, Lumiere's way), ``title`` (for messages), ``heartbeat_s``."""

    def __init__(self, *, app: str, service: str, package: str, default_port: int, data_dir_env: Optional[str] = None,
                 token_env: Optional[str] = None, token_file: Union[str, "os.PathLike[str]", None] = None,
                 url_file: Union[str, "os.PathLike[str]", None] = None, default_timeout: float = 90.0, autostart: bool = True,
                 refresh_tools_s: float = 60.0, tool_timeouts: Optional[Mapping[str, float]] = None, image_content: bool = False,
                 title: Optional[str] = None, env_prefix: Optional[str] = None, root: Union[str, "os.PathLike[str]", None] = None,
                 heartbeat_s: float = 10.0, base_url: Optional[str] = None, instructions: str = "") -> None:
        self.app = app
        self.service = service
        self.package = package
        self.default_port = int(default_port)
        self.data_dir_env = data_dir_env
        self.token_env = token_env
        self.token_file = token_file
        self.url_file = url_file
        self.default_timeout = float(default_timeout)
        self.autostart = autostart
        self.refresh_tools_s = float(refresh_tools_s)
        self.tool_timeouts = dict(tool_timeouts or {})
        self.image_content = image_content
        self.title = title or service
        self.prefix = env_prefix or _prefix_of(app)
        self.heartbeat_s = float(heartbeat_s)
        self._base_url = base_url
        self.instructions = instructions
        if root is None:
            root = sys.argv[0] if sys.argv and sys.argv[0] else os.getcwd()
        root_path = Path(root).resolve()
        self.root = root_path.parent if root_path.is_file() or root_path.suffix else root_path
        self._catalog: list[dict[str, Any]] = []
        self._fetched_at: Optional[float] = None
        self._refresh_lock: Optional[asyncio.Lock] = None

    # ----- where things are
    @property
    def data_dir(self) -> Path:
        configured = os.environ.get(self.data_dir_env) if self.data_dir_env else None
        return Path(configured) if configured else self.root / "data"

    @property
    def base_url(self) -> str:
        if self._base_url:
            return self._base_url.rstrip("/")
        env = os.environ.get(f"{self.prefix}_URL")
        if env and env.strip():
            return env.strip().rstrip("/")
        recorded = tokens.read_url(self.url_file) if self.url_file else None
        if recorded:
            return recorded.rstrip("/")
        port = os.environ.get(f"{self.prefix}_PORT")
        return f"http://127.0.0.1:{int(port) if port and port.isdigit() else self.default_port}"

    @property
    def port(self) -> int:
        return urlparse(self.base_url).port or self.default_port

    def check_local(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme != "http" or parsed.hostname not in LOCAL_BRIDGE_HOSTS:
            raise SystemExit("The MCP bridge only connects to the local server.")

    def token(self) -> str:
        return bridge_token(self.app, self.data_dir, token_env=self.token_env, token_file=self.token_file, env_prefix=self.prefix)

    def autostart_enabled(self) -> bool:
        return bool(self.autostart) and env_flag(f"{self.prefix}_BRIDGE_AUTOSTART", True)

    def message(self, template: str, **extra: Any) -> str:
        return template.format(title=self.title, package=self.package, prefix=self.prefix, data_dir_env=self.data_dir_env or "the data folder variable",
                               path=self._token_path(), **extra)

    def _token_path(self) -> str:
        return os.environ.get(f"{self.prefix}_TOKEN_FILE") or str(self.token_file or self.data_dir / "mcp-token")

    # ----- the app side (blocking helpers run in a thread)
    def healthy(self) -> bool:
        return _healthy(self.port, self.service)

    def start_app(self, wait_s: float = 45.0) -> bool:
        """Start the app unless it is up (or autostart is off); True once it answers."""
        if self.healthy():
            return True
        if not self.autostart_enabled():
            return False
        return ensure_running(self.package, self.port, service=self.service, data_dir=self.data_dir, wait_s=wait_s,
                              cwd=self.root, port_env=f"{self.prefix}_PORT", log_name=self.app)

    # ----- the catalogue
    async def fetch_catalog(self) -> list[dict[str, Any]]:
        """Fetch ``/api/agent/tools`` now (raises on failure)."""
        data = await asyncio.to_thread(_get_blocking, f"{self.base_url}/api/agent/tools", 10.0)
        tools = data.get("tools") if isinstance(data, dict) else None
        if not isinstance(tools, list):
            raise ValueError("the app's /api/agent/tools did not return a tool list")
        self._catalog = [t for t in tools if isinstance(t, dict) and t.get("name")]
        if isinstance(data, dict) and isinstance(data.get("instructions"), str):
            self.instructions = data["instructions"]
        self._fetched_at = time.monotonic()
        return self._catalog

    async def tools(self, *, force: bool = False) -> list[dict[str, Any]]:
        """The catalogue, refreshed when ``force`` or older than ``refresh_tools_s``. A failed refresh keeps the last good one;
        with none yet it raises (after trying to start the app)."""
        if self._refresh_lock is None:
            self._refresh_lock = asyncio.Lock()
        stale = self._fetched_at is None or force or (time.monotonic() - self._fetched_at) > self.refresh_tools_s
        if not stale:
            return self._catalog
        async with self._refresh_lock:
            fresh = self._fetched_at is not None and not force and (time.monotonic() - self._fetched_at) <= self.refresh_tools_s
            if fresh:
                return self._catalog
            try:
                return await self.fetch_catalog()
            except Exception as first:  # noqa: BLE001
                if self._catalog:
                    log.warning("could not refresh the tool list (%s); using the last one", first)
                    self._fetched_at = time.monotonic() - self.refresh_tools_s + 5.0       # try again soon, not on every call
                    return self._catalog
                if await asyncio.to_thread(self.start_app):
                    return await self.fetch_catalog()
                raise

    async def list_tools(self) -> list[dict[str, Any]]:
        """The catalogue entries (``{name, description, inputSchema, annotations}``), refreshed when stale."""
        return await self.tools()

    async def call_tool(self, name: str, arguments: Optional[Mapping[str, Any]] = None, progress: Optional[Progress] = None) -> BridgeResult:
        """Alias of :meth:`call`."""
        return await self.call(name, arguments, progress)

    def tool_meta(self, name: str) -> Optional[dict[str, Any]]:
        return next((t for t in self._catalog if t.get("name") == name), None)

    # ----- one call
    async def call(self, name: str, arguments: Optional[Mapping[str, Any]] = None, progress: Optional[Progress] = None) -> BridgeResult:
        """Proxy ``name(arguments)`` to the app and shape the answer as MCP content. Never raises."""
        arguments = dict(arguments or {})
        try:
            if self.tool_meta(name) is None:
                try:
                    await self.tools(force=True)
                except Exception:  # noqa: BLE001 - the app may be down; the call below starts it
                    pass
                if self._catalog and self.tool_meta(name) is None:
                    return self._error({"error": f"Unknown tool: {name}", "code": "unknown_tool"})
            else:
                try:
                    await self.tools()
                except Exception:  # noqa: BLE001
                    pass
            return await self._call(name, arguments, progress, retry=True)
        except Exception as error:  # noqa: BLE001 - keep the bridge alive on any failure
            log.exception("bridge call %s failed", name)
            return self._error({"error": f"{type(error).__name__}: {error}"})

    def _error(self, body: Mapping[str, Any]) -> BridgeResult:
        return BridgeResult([_text(dict(body))], True, dict(body))

    async def _call(self, name: str, arguments: dict[str, Any], progress: Optional[Progress], retry: bool) -> BridgeResult:
        meta = self.tool_meta(name)
        read_only = bool(((meta or {}).get("annotations") or {}).get("readOnlyHint"))
        timeout = tool_timeout(meta or {"name": name}, self.default_timeout, arguments=arguments, overrides=self.tool_timeouts)
        try:
            token = self.token()
        except FileNotFoundError:
            # A missing token is not a stopped app: say which file is missing instead of offering to start it.
            if await asyncio.to_thread(self.healthy):
                return self._error({"error": self.message(NO_TOKEN), "code": "no_token"})
            if retry and await asyncio.to_thread(self.start_app):
                return await self._call(name, arguments, progress, retry=False)
            return self._error({"error": self.message(NOT_RUNNING), "code": "not_running"})
        beat = asyncio.create_task(self._heartbeat(name, progress)) if progress is not None else None
        try:
            who = agent_headers()
            payload: dict[str, Any] = {"name": name, "arguments": arguments}
            if who.get("X-Agent-Id"):
                payload["caller"] = who["X-Agent-Id"]            # apps that predate the headers still log who called
            status, body, raw = await _post(f"{self.base_url}/api/agent/call", payload,
                                            {"Authorization": f"Bearer {token}", **who}, timeout)
        except _ConnectFailed:
            if retry and await asyncio.to_thread(self.start_app):
                return await self._call(name, arguments, progress, retry=False)
            return self._error({"error": self.message(NOT_RUNNING), "code": "not_running"})
        except _NoAnswer as error:
            if not read_only:
                return self._error({"error": self.message(OUTCOME_UNKNOWN, name=name), "code": "outcome_unknown", "status": "outcome_unknown",
                                    "outcome_unknown": True, "reconcile_action": "read_current_state_before_retry"})
            return self._error({"error": f"The app did not answer {name}: {error}", "code": "timeout" if error.timed_out else "no_answer"})
        finally:
            if beat is not None:
                beat.cancel()
        return self._shape(status, body, raw)

    def _shape(self, status: int, body: Any, raw: str) -> BridgeResult:
        if status == 401:
            return self._error({"error": self.message(TOKEN_REFUSED), "code": "token_refused"})
        if status >= 400:
            detail = {k: v for k, v in body.items() if k in FORWARDED_KEYS} if isinstance(body, dict) else {}
            detail.setdefault("error", (raw.strip()[:200] if body is None and raw.strip() else f"Error {status}"))
            return self._error(detail)
        if body is None:
            return self._error({"error": f"The app answered with something that is not JSON (HTTP {status}): {raw.strip()[:200]}",
                                "code": "bad_response"})
        content: list[dict[str, Any]] = []
        image = body.pop("_image", None) if (self.image_content and isinstance(body, dict)) else None
        content.append(_text(body))
        if isinstance(image, dict) and image.get("data"):
            # a frame the assistant asked to look at travels as a picture, not as a path it may not be allowed to open
            content.append({"type": "image", "data": image["data"], "mimeType": image.get("mime") or image.get("mimeType") or "image/jpeg"})
        return BridgeResult(content, False, body)

    async def _heartbeat(self, name: str, progress: Optional[Progress]) -> None:
        beats = 0
        try:
            while progress is not None:
                await asyncio.sleep(self.heartbeat_s)
                beats += 1
                try:
                    await progress(float(beats), f"{name} is still running...")
                except Exception:  # noqa: BLE001 - a client that cannot take progress must not break the call
                    return
        except asyncio.CancelledError:
            pass

    # ----- the MCP server
    def build_server(self) -> Any:
        """The ``mcp`` server object (``FastMCP`` of mcp 1.x or ``MCPServer`` of 2.x) wired to this bridge. ``mcp`` is imported here."""
        core = self
        try:
            from mcp.server.fastmcp import FastMCP as Base  # mcp 1.x
            version = 1
        except ImportError:
            from mcp.server.mcpserver import MCPServer as Base  # type: ignore[no-redef]  # mcp 2.x
            version = 2
        from mcp.types import CallToolResult, ImageContent, TextContent, Tool as MCPTool, ToolAnnotations

        def to_tool(entry: Mapping[str, Any]) -> Any:
            annotations = {k: v for k, v in (entry.get("annotations") or {}).items() if k in _ANNOTATION_KEYS}
            return MCPTool(name=entry["name"], description=entry.get("description") or "", inputSchema=entry.get("inputSchema") or {"type": "object"},
                           annotations=ToolAnnotations(**annotations) if annotations else None)

        def to_result(result: BridgeResult) -> Any:
            parts = [TextContent(type="text", text=p["text"]) if p["type"] == "text"
                     else ImageContent(type="image", data=p["data"], mimeType=p["mimeType"]) for p in result.content]
            return CallToolResult(content=parts, isError=result.is_error)

        if version == 1:
            class Server(Base):  # type: ignore[misc, valid-type]
                async def list_tools(self) -> list[Any]:
                    return [to_tool(t) for t in await core.tools()]

                async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
                    try:
                        context = self.get_context()
                    except Exception:  # noqa: BLE001
                        context = None
                    return to_result(await core.call(name, arguments, _progress_of(context)))
        else:
            class Server(Base):  # type: ignore[no-redef, misc, valid-type]
                async def list_tools(self) -> list[Any]:
                    return [to_tool(t) for t in await core.tools()]

                async def call_tool(self, name: str, arguments: dict[str, Any], context: Any = None) -> Any:
                    return to_result(await core.call(name, arguments, _progress_of(context)))

        return Server(name=self.service, instructions=self.instructions or None)

    def run_bridge(self) -> None:
        """Entry point: check the URL is local, make sure the app is up (autostart), read the catalogue, serve over stdio.
        Exits with a message naming what to open when the app cannot be reached."""
        logging.basicConfig(level=logging.WARNING, stream=sys.stderr)  # stdout belongs to the protocol
        logging.getLogger("httpx").setLevel(logging.WARNING)
        self.check_local()
        try:
            asyncio.run(self._prefetch())
        except Exception as error:  # noqa: BLE001
            raise SystemExit(f"{self.message(NOT_RUNNING)} ({error})") from error
        self.build_server().run(transport="stdio")

    async def _prefetch(self) -> None:
        await asyncio.to_thread(self.start_app)
        await self.fetch_catalog()


def _progress_of(context: Any) -> Optional[Progress]:
    """``context.report_progress`` as a ``(beat, message)`` coroutine function, or None without a context."""
    if context is None or not hasattr(context, "report_progress"):
        return None

    async def progress(beat: float, message: str) -> None:
        await context.report_progress(beat, None, message)

    return progress
