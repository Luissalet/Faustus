"""
mcp_manager.py

Manages connections to MCP (Model Context Protocol) tool servers.
Each server exposes tools that are made available to the agent loop.
"""

import json
import logging
import os
import re
import asyncio
from contextlib import contextmanager
from typing import Any, Dict, List, Optional, Set, Tuple
from src.database import McpServer, SessionLocal

from src.runtime_paths import get_app_root
from src.tool_capabilities import (
    BROWSER_CODE_EXECUTION_TOOLS,
    BROWSER_MCP_PREFIX,
    BROWSER_MCP_SERVER_ID,
)

logger = logging.getLogger(__name__)

# Upper bound for one MCP tool call. A server whose child process died mid-call
# never answers; without a bound the agent turn hangs forever instead of taking
# the reconnect path below. Generous: Playwright's own navigation timeout is
# 60 s and long pages / slow sites need headroom.
MCP_CALL_TIMEOUT_S = float(os.environ.get("ODYSSEUS_MCP_CALL_TIMEOUT", "180") or 180)

# How long disconnect waits for the owner task to tear a server down.
_MCP_CLOSE_TIMEOUT_S = 15.0

# Browser tool results that carry the page's accessibility snapshot. Their
# text is bounded by the `browser_snapshot_max_chars` setting (issue: a full
# tree is ~24k tokens on a big page, which drowns a 9B model).
_BROWSER_SNAPSHOT_TOOLS = frozenset({
    "browser_snapshot",
    "browser_navigate",
    "browser_navigate_back",
    "browser_click",
    "browser_type",
    "browser_fill_form",
    "browser_select_option",
    "browser_press_key",
    "browser_hover",
    "browser_drag",
    "browser_drop",
    "browser_wait_for",
})
_BROWSER_SNAPSHOT_DEFAULT_MAX = 12000
_BROWSER_SNAPSHOT_MIN_MAX = 500


def truncate_browser_snapshot(text: str, max_chars: int) -> str:
    """Cut a snapshot-bearing result to `max_chars` at a line boundary.

    Keeps the leading lines (the `### Page` URL/title header comes first in
    Playwright's output) and appends a note telling the model how to get the
    part it needs without re-requesting the whole tree.
    """
    if not isinstance(text, str):
        return text
    try:
        limit = int(max_chars)
    except (TypeError, ValueError):
        limit = _BROWSER_SNAPSHOT_DEFAULT_MAX
    limit = max(_BROWSER_SNAPSHOT_MIN_MAX, limit)
    if len(text) <= limit:
        return text
    head = text[:limit]
    cut = head.rfind("\n")
    if cut > limit // 2:
        head = head[:cut]
    head = head.rstrip()
    return (
        f"{head}\n\n(snapshot truncated to {limit} chars — use browser_find or "
        "browser_snapshot with a narrower scope)"
    )


def _browser_snapshot_budget() -> int:
    try:
        from src.settings import get_setting
        value = get_setting("browser_snapshot_max_chars", _BROWSER_SNAPSHOT_DEFAULT_MAX)
        return int(value) if value is not None else _BROWSER_SNAPSHOT_DEFAULT_MAX
    except Exception:
        return _BROWSER_SNAPSHOT_DEFAULT_MAX


def builtin_browser_policy_disabled() -> Set[str]:
    """Bare browser tool names withheld by the operator's settings.

    Two sources: the admin "browser off" toggle (server id `builtin_browser`
    or the `mcp__builtin_browser__*` wildcard in `disabled_tools`, which the
    qualified-name gates would otherwise never match) and the code-execution
    opt-in (`browser_allow_code_execution`, default off). The same set drives
    what is OFFERED (schemas, prompt text) and what is EXECUTABLE (call_tool),
    so a withheld tool is never advertised and then refused.
    """
    try:
        from src.settings import get_setting
    except Exception:  # pragma: no cover - settings backend unavailable
        return set()
    denied: Set[str] = set()
    try:
        disabled = get_setting("disabled_tools", []) or []
    except Exception:
        disabled = []
    names = {str(n) for n in disabled} if isinstance(disabled, (list, tuple, set, frozenset)) else set()
    if BROWSER_MCP_SERVER_ID in names or (BROWSER_MCP_PREFIX + "*") in names:
        denied.add("*")
    for name in names:
        if name.startswith(BROWSER_MCP_PREFIX):
            denied.add(name[len(BROWSER_MCP_PREFIX):])
    try:
        allow_code = bool(get_setting("browser_allow_code_execution", False))
    except Exception:
        allow_code = False
    if not allow_code:
        denied |= set(BROWSER_CODE_EXECUTION_TOOLS)
    return denied


def _browser_tool_denied(tool_name: str, denied: Set[str]) -> bool:
    return "*" in denied or tool_name in denied


@contextmanager
def _suppress_all():
    """Swallow teardown errors (never a cancellation) on best-effort paths."""
    try:
        yield
    except asyncio.CancelledError:
        raise
    except BaseException:  # noqa: BLE001 - teardown is best effort
        pass

def _format_mcp_connection_error(name: str, command: str = "", args: Optional[List[str]] = None, error: Exception = None) -> str:
    """Return a user-actionable MCP connection error message."""
    args = args or []
    raw_error = str(error) if error else "Unknown error"
    command_line = " ".join([command or "", *args]).strip()
    lower_command = command_line.lower()

    if "@playwright/mcp" in lower_command:
        return (
            f"{raw_error}\n\n"
            "Browser MCP could not start. On fresh installs, cache the Playwright MCP package once before connecting:\n\n"
            "npx -y @playwright/mcp@latest --version\n\n"
            "Then restart Faustus and reconnect the Browser MCP server."
        )

    return raw_error


# Caps for rendering untrusted MCP tool schemas into the agent prompt (issue #2660).
# MCP servers are third-party/user-added, so field names and parameter counts are
# untrusted input — bound them so an odd or hostile schema cannot distort the prompt.
_MCP_PARAM_MAX = 12   # max params rendered per tool
_MCP_TOKEN_MAX = 40   # max chars per rendered name / type token
_MCP_HINT_MAX = 300   # total-length backstop for the whole hint


def _sanitize_schema_token(value: Any, limit: int = _MCP_TOKEN_MAX) -> str:
    """Make an untrusted JSON-Schema token safe to splice into the prompt.

    Replaces control chars / newlines with a space, collapses whitespace, and
    length-caps the result, so a weird field name or type cannot inject newlines
    or run on. Normal short identifiers pass through unchanged.
    """
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def _format_mcp_params(input_schema: Any) -> str:
    """Render an MCP tool's JSON-Schema inputs as a compact prompt hint.

    Without this the agent only sees a tool's name + description and has to
    guess its arguments (issue #2509). Produces e.g.
    ` Args (JSON): {"path": string (required), "limit": integer}` — names,
    coarse types, and required-ness, kept short so it stays prompt-friendly.
    Returns "" when there are no parameters.

    MCP servers are third-party, so names/types are sanitized and the parameter
    count + total length are capped (issue #2660); normal schemas are unaffected.
    """
    if not isinstance(input_schema, dict):
        return ""
    props = input_schema.get("properties")
    if not isinstance(props, dict) or not props:
        return ""
    required = set(input_schema.get("required") or [])
    parts = []
    for pname, pinfo in list(props.items())[:_MCP_PARAM_MAX]:
        pinfo = pinfo if isinstance(pinfo, dict) else {}
        ptype = pinfo.get("type") or "any"
        if isinstance(ptype, list):
            ptype = "|".join(str(x) for x in ptype)
        tag = f'"{_sanitize_schema_token(pname)}": {_sanitize_schema_token(ptype)}'
        if pname in required:
            tag += " (required)"
        parts.append(tag)
    extra = len(props) - len(parts)
    if extra > 0:
        parts.append(f"…+{extra} more")
    hint = " Args (JSON): {" + ", ".join(parts) + "}"
    if len(hint) > _MCP_HINT_MAX:
        hint = hint[:_MCP_HINT_MAX - 1].rstrip() + "…"
    return hint


# Tool-name prefixes that denote a read-only/inspection operation. Used to
# classify MCP tools for plan mode when the server provides no readOnlyHint.
# These are PREFIXES, not whole words (matched via str.startswith below), so a
# stem like "summar" intentionally covers "summarise"/"summarize"/"summary".
_MCP_READONLY_VERBS = (
    "list", "get", "read", "search", "fetch", "query", "find", "describe",
    "show", "view", "lookup", "count", "status", "info", "inspect", "summar",
)


def mcp_tool_is_readonly(tool: Dict) -> bool:
    """Classify an MCP tool as safe (non-mutating) for plan mode.

    Prefer the server's own annotations (readOnlyHint / destructiveHint). When
    absent, fall back to a tool-name verb heuristic, and FAIL CLOSED (treat as
    write) for anything that doesn't clearly read — plan mode must not run a
    write tool just because its intent is ambiguous.
    """
    ann = tool.get("annotations")
    # annotations may be a dict or a pydantic model
    read_hint = None
    destructive = None
    if ann is not None:
        if isinstance(ann, dict):
            read_hint = ann.get("readOnlyHint")
            destructive = ann.get("destructiveHint")
        else:
            read_hint = getattr(ann, "readOnlyHint", None)
            destructive = getattr(ann, "destructiveHint", None)
    if read_hint is True:
        return True
    if read_hint is False or destructive is True:
        return False
    # No usable hint — heuristic on the tool name's leading verb.
    name = (tool.get("name") or "").lower()
    return name.startswith(_MCP_READONLY_VERBS)


class McpManager:
    """Manages MCP server connections and tool routing."""

    def __init__(self):
        # server_id -> connection state
        self._connections: Dict[str, Dict[str, Any]] = {}
        # server_id -> list of tool schemas
        self._tools: Dict[str, List[Dict]] = {}
        # server_id -> MCP ClientSession
        self._sessions: Dict[str, Any] = {}
        # server_id -> exit stack (for cleanup)
        self._stacks: Dict[str, Any] = {}
        # server_id -> background connect task (HTTP transport / OAuth)
        self._connect_tasks: Dict[str, Any] = {}
        # server_id -> (owner task, close event) for stdio servers. The task
        # that ENTERS the stdio/session contexts is the only one allowed to
        # EXIT them (anyio cancel scopes are task-bound), so each stdio server
        # lives in its own owner task and disconnect just asks it to leave.
        self._owner_tasks: Dict[str, Tuple[Any, Any]] = {}
        # Serialises crash-reconnects per server so two concurrent failing
        # calls do not both tear the same server down.
        self._reconnect_locks: Dict[str, Any] = {}
        # Tracking updates to tools/connections for RAG indexing / prompt cache
        self._generation = 0

    async def connect_server(
        self,
        server_id: str,
        name: str,
        transport: str,
        command: Optional[str] = None,
        args: Optional[List[str]] = None,
        env: Optional[Dict[str, str]] = None,
        url: Optional[str] = None,
    ) -> bool:
        """Connect to an MCP server via stdio, SSE, or Streamable HTTP transport."""
        try:
            if transport == "stdio":
                res = await self._connect_stdio(server_id, name, command, args or [], env or {})
            elif transport == "sse":
                res = await self._connect_sse(server_id, name, url)
            elif transport == "http":
                res = await self._start_http_connect(server_id, name, url)
            else:
                logger.error(f"Unknown MCP transport: {transport}")
                res = False
            if res:
                self._generation += 1
            return res
        except Exception as e:
            logger.error(f"Failed to connect MCP server {name} ({server_id}): {e}")
            error_message = _format_mcp_connection_error(name, command or "", args or [], e)
            self._connections[server_id] = {"status": "error", "error": error_message, "name": name}
            self._generation += 1
            return False

    async def _connect_stdio(self, server_id: str, name: str, command: str, args: List[str], env: Dict[str, str]) -> bool:
        """Connect to an MCP server via stdio transport.

        The transport and session contexts are entered — and later exited — by
        a dedicated owner task. Built-ins are connected from fire-and-forget
        startup tasks and closed from the app's shutdown hook (a different
        task); exiting the stdio task group from that other task raised
        "Attempted to exit cancel scope in a different task" for every server,
        the browser included. The owner task waits on a close event instead,
        so `disconnect_server` can be called from anywhere.
        """
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
            from contextlib import AsyncExitStack
        except ImportError:
            logger.warning("MCP package not installed. Install with: pip install mcp")
            self._connections[server_id] = {
                "status": "error",
                "error": "mcp package not installed",
                "name": name,
            }
            return False

        server_params = StdioServerParameters(
            command=command,
            args=args,
            env={**os.environ, **env} if env else None,
        )

        loop = asyncio.get_running_loop()
        ready: asyncio.Future = loop.create_future()
        close_event = asyncio.Event()

        async def _owner():
            stack = AsyncExitStack()
            try:
                transport = await stack.enter_async_context(stdio_client(server_params))
                read_stream, write_stream = transport
                session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
                await session.initialize()
                tools_result = await session.list_tools()
            except BaseException as exc:  # noqa: BLE001 - report to the waiter, then bail
                try:
                    await stack.aclose()
                except BaseException as close_exc:  # noqa: BLE001
                    logger.debug(f"MCP stdio teardown after failed connect ({server_id}): {close_exc}")
                if not ready.done():
                    ready.set_exception(exc if isinstance(exc, Exception) else RuntimeError(repr(exc)))
                if isinstance(exc, asyncio.CancelledError):
                    raise
                return
            if not ready.done():
                ready.set_result((session, tools_result))
            try:
                await close_event.wait()
            finally:
                try:
                    await stack.aclose()
                except BaseException as close_exc:  # noqa: BLE001
                    logger.debug(f"MCP stdio teardown ({server_id}): {close_exc}")

        task = asyncio.create_task(_owner(), name=f"mcp-stdio-{server_id}")
        try:
            session, tools_result = await ready
        except asyncio.CancelledError:
            # The caller gave up (timeout / shutdown): take the owner down with us.
            close_event.set()
            task.cancel()
            raise
        except BaseException:
            close_event.set()
            with _suppress_all():
                await asyncio.wait_for(task, timeout=_MCP_CLOSE_TIMEOUT_S)
            raise

        tools = []
        for tool in tools_result.tools:
            tools.append({
                "name": tool.name,
                "description": tool.description or "",
                "input_schema": tool.inputSchema if hasattr(tool, "inputSchema") else {},
                # MCP tool annotations (readOnlyHint / destructiveHint) drive
                # plan-mode read-only gating. Absent on many servers, so we
                # fall back to a name heuristic in mcp_tool_is_readonly().
                "annotations": getattr(tool, "annotations", None),
            })

        # Extract identity hints from env vars (e.g. email address, API name)
        # so tool descriptions can distinguish between multiple instances of
        # the same MCP server (e.g. two email accounts).
        identity_hints = []
        for k, v in (env or {}).items():
            k_lower = k.lower()
            if any(x in k_lower for x in ["email_address", "account", "user", "username"]):
                identity_hints.append(v)
        identity = ", ".join(identity_hints) if identity_hints else ""

        # A stale owner for the same id (reconnect race) is asked to leave.
        stale = self._owner_tasks.pop(server_id, None)
        if stale is not None:
            stale[1].set()

        self._sessions[server_id] = session
        self._owner_tasks[server_id] = (task, close_event)
        self._tools[server_id] = tools
        self._connections[server_id] = {
            "status": "connected",
            "name": name,
            "transport": "stdio",
            "tool_count": len(tools),
            "identity": identity,
        }

        logger.info(f"MCP server connected: {name} ({server_id}) - {len(tools)} tools via stdio")
        return True

    def _stdio_owner_alive(self, server_id: str) -> bool:
        """False once the owner task has finished, i.e. the server is gone."""
        entry = self._owner_tasks.get(server_id)
        if entry is None:
            return True  # not an owner-task connection (SSE/HTTP): nothing to say
        task = entry[0]
        return not task.done()

    async def _connect_sse(self, server_id: str, name: str, url: str) -> bool:
        """Connect to an MCP server via SSE transport."""
        try:
            from mcp import ClientSession
            from mcp.client.sse import sse_client
            from contextlib import AsyncExitStack

            stack = AsyncExitStack()
            registered = False

            try:
                transport = await stack.enter_async_context(sse_client(url))
                read_stream, write_stream = transport
                session = await stack.enter_async_context(ClientSession(read_stream, write_stream))

                await session.initialize()
                tools_result = await session.list_tools()

                tools = []
                for tool in tools_result.tools:
                    tools.append({
                        "name": tool.name,
                        "description": tool.description or "",
                        "input_schema": tool.inputSchema if hasattr(tool, 'inputSchema') else {},
                        # MCP tool annotations (readOnlyHint / destructiveHint) drive
                        # plan-mode read-only gating. Absent on many servers, so we
                        # fall back to a name heuristic in mcp_tool_is_readonly().
                        "annotations": getattr(tool, 'annotations', None),
                    })

                self._sessions[server_id] = session
                self._stacks[server_id] = stack
                self._tools[server_id] = tools
                self._connections[server_id] = {
                    "status": "connected",
                    "name": name,
                    "transport": "sse",
                    "tool_count": len(tools),
                }

                registered = True

                logger.info(f"MCP server connected: {name} ({server_id}) - {len(tools)} tools via SSE")
                return True

            finally:
                if not registered:
                    await stack.aclose()

        except ImportError:
            logger.warning("MCP package not installed. Install with: pip install mcp")
            self._connections[server_id] = {"status": "error", "error": "mcp package not installed", "name": name}
            return False

    async def _start_http_connect(self, server_id: str, name: str, url: str, wait: float = 8.0) -> bool:
        """Begin a Streamable HTTP connect in the background. Returns within
        `wait` seconds: True if it connected (cached-token path), otherwise the
        flow is awaiting browser authorization and status becomes 'needs_auth'."""
        import asyncio
        self._connections[server_id] = {"status": "connecting", "name": name, "transport": "http"}
        task = asyncio.create_task(self._connect_http(server_id, name, url))
        self._connect_tasks[server_id] = task
        done, _ = await asyncio.wait({task}, timeout=wait)
        if task in done:
            try:
                return task.result()
            except Exception as e:
                self._connections[server_id] = {"status": "error", "error": str(e), "name": name}
                return False
        # Still running → either awaiting authorization, or discovery/DCR is
        # still in flight. If _on_redirect already published needs_auth+auth_url,
        # leave it; otherwise mark needs_auth (auth_url filled in once it fires).
        from src.mcp_oauth import pop_auth_url
        cur = self._connections.get(server_id, {})
        if cur.get("status") != "needs_auth":
            self._connections[server_id] = {
                "status": "needs_auth", "name": name, "transport": "http",
                "auth_url": pop_auth_url(server_id),
            }
        return False

    async def _connect_http(self, server_id: str, name: str, url: str) -> bool:
        """Connect to a Streamable HTTP MCP server (with automatic OAuth)."""
        try:
            from mcp import ClientSession
            from mcp.client.streamable_http import streamablehttp_client
            from contextlib import AsyncExitStack
            from src.mcp_oauth import build_provider, clear_auth_url

            def _on_redirect(auth_url):
                # Publish needs_auth the moment the URL is known, independent of
                # how long discovery/DCR took (may exceed the bounded start wait).
                self._connections[server_id] = {
                    "status": "needs_auth", "name": name, "transport": "http",
                    "auth_url": auth_url,
                }

            provider = build_provider(server_id, url, on_redirect=_on_redirect)
            stack = AsyncExitStack()
            transport = await stack.enter_async_context(streamablehttp_client(url, auth=provider))
            read_stream, write_stream, _get_session_id = transport
            session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
            await session.initialize()

            tools_result = await session.list_tools()
            tools = []
            for tool in tools_result.tools:
                tools.append({
                    "name": tool.name,
                    "description": tool.description or "",
                    "input_schema": tool.inputSchema if hasattr(tool, "inputSchema") else {},
                })

            self._sessions[server_id] = session
            self._stacks[server_id] = stack
            self._tools[server_id] = tools
            self._connections[server_id] = {
                "status": "connected", "name": name, "transport": "http",
                "tool_count": len(tools),
            }
            clear_auth_url(server_id)
            # Tools changed (this can complete after connect_server already
            # returned, via the background OAuth flow), so bump the generation
            # to invalidate the tool-prompt cache.
            self._generation += 1
            logger.info(f"MCP server connected: {name} ({server_id}) - {len(tools)} tools via http")
            return True
        except ImportError:
            logger.warning("MCP package not installed. Install with: pip install mcp")
            self._connections[server_id] = {"status": "error", "error": "mcp package not installed", "name": name}
            return False
        except Exception as e:
            logger.error(f"Failed to connect HTTP MCP server {name} ({server_id}): {e}")
            self._connections[server_id] = {"status": "error", "error": str(e), "name": name}
            return False

    async def disconnect_server(self, server_id: str):
        """Disconnect from an MCP server."""
        # Cancel any in-flight HTTP/OAuth background connect so it stops
        # publishing status for a server that may be getting deleted.
        task = self._connect_tasks.pop(server_id, None)
        if task is not None and not task.done():
            task.cancel()
        try:
            from src.mcp_oauth import clear_auth_url
            clear_auth_url(server_id)
        except Exception:
            pass

        # stdio servers: ask the owner task to exit its contexts (same task
        # that entered them) and wait for it, from whichever task we are on.
        owner = self._owner_tasks.pop(server_id, None)
        if owner is not None:
            owner_task, close_event = owner
            close_event.set()
            if not owner_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(owner_task), timeout=_MCP_CLOSE_TIMEOUT_S)
                except asyncio.TimeoutError:
                    logger.warning(f"MCP server {server_id} did not shut down in time; cancelling")
                    owner_task.cancel()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(f"Error closing MCP server {server_id}: {e}")

        stack = self._stacks.pop(server_id, None)
        if stack:
            try:
                await stack.aclose()
            except Exception as e:
                logger.warning(f"Error closing MCP server {server_id}: {e}")

        self._sessions.pop(server_id, None)
        self._tools.pop(server_id, None)
        self._connections.pop(server_id, None)
        self._generation += 1
        logger.info(f"MCP server disconnected: {server_id}")

    async def disconnect_all(self):
        """Disconnect from all MCP servers."""
        ids = list(self._sessions.keys())
        for sid in ids:
            await self.disconnect_server(sid)


    async def connect_all_enabled(self):
        db = SessionLocal()
        try:
            servers = db.query(McpServer).filter(McpServer.is_enabled == True).all()

            tasks = [
                asyncio.create_task(self._connect_with_timeout(srv))
                for srv in servers
            ]

            await asyncio.gather(*tasks)
        finally:
            db.close()


    async def _connect_with_timeout(self, srv):
        args = json.loads(srv.args) if srv.args else []
        env = json.loads(srv.env) if srv.env else {}

        try:
            await asyncio.wait_for(
                self.connect_server(
                    server_id=srv.id,
                    name=srv.name,
                    transport=srv.transport,
                    command=srv.command,
                    args=args,
                    env=env,
                    url=srv.url,
                ),
                timeout=20,
            )
        except asyncio.TimeoutError:
            logger.warning("Timed out connecting to %s", srv.name)
            self._connections[srv.id] = {
                "status": "timeout",
                "error": f"Timed out after 20 seconds",
                "name": srv.name,
            }

    async def call_tool(self, qualified_name: str, arguments: Dict) -> Dict:
        """Call an MCP tool by its qualified name (mcp__{server_id}__{tool_name}).

        Returns a result dict compatible with agent_tools format.
        """
        parts = qualified_name.split("__", 2)
        if len(parts) != 3 or parts[0] != "mcp":
            return {"error": f"Invalid MCP tool name: {qualified_name}", "exit_code": 1}

        server_id = parts[1]
        tool_name = parts[2]

        if server_id == BROWSER_MCP_SERVER_ID:
            # Dispatch-side half of the offered-then-executable invariant: the
            # same predicate that hid the tool from the schemas refuses it here.
            denied = builtin_browser_policy_disabled()
            if _browser_tool_denied(tool_name, denied):
                if "*" in denied:
                    reason = "The built-in browser is switched off in Settings (disabled_tools)."
                elif tool_name in BROWSER_CODE_EXECUTION_TOOLS:
                    reason = (
                        f"{tool_name} runs model-written JavaScript in the page and is off "
                        "by default; enable the browser_allow_code_execution setting to use it."
                    )
                else:
                    reason = f"{tool_name} is disabled in Settings (disabled_tools)."
                return {
                    "error": reason,
                    "exit_code": 1,
                    "blocked": True,
                    "policy": "browser_policy",
                }
            # A settings change (profile / headless / caps / CDP) applies on
            # the next browser call instead of waiting for an app restart.
            await self.ensure_builtin_browser_current()

        session = self._sessions.get(server_id)
        if not session:
            detail = self._connections.get(server_id, {}).get("error")
            suffix = f" ({detail})" if detail else ""
            return {"error": f"MCP server not connected: {server_id}{suffix}", "exit_code": 1}

        # A built-in whose owner task already finished has no live process
        # behind the session: skip the doomed call and go straight to reconnect.
        dead = self.is_builtin(server_id) and not self._stdio_owner_alive(server_id)
        try:
            if dead:
                raise ConnectionError(f"MCP server process for {server_id} has exited")
            result = await self._do_call(session, tool_name, arguments)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # Auto-reconnect for builtin servers whose subprocess may have died
            if self.is_builtin(server_id):
                logger.warning(
                    f"MCP call failed for {qualified_name}, attempting reconnect: {type(e).__name__}: {e}"
                )
                reconnected = await self._reconnect_builtin(server_id)
                if reconnected:
                    session = self._sessions.get(server_id)
                    if session:
                        try:
                            result = await self._do_call(session, tool_name, arguments)
                        except asyncio.CancelledError:
                            raise
                        except Exception as e2:
                            logger.error(f"MCP tool call failed after reconnect: {qualified_name}: {e2}")
                            return {"error": str(e2), "exit_code": 1}
                    else:
                        return {"error": f"Reconnected but no session for {server_id}", "exit_code": 1}
                else:
                    logger.error(f"MCP reconnect failed for {server_id}")
                    return {"error": f"MCP server crashed and reconnect failed: {server_id}", "exit_code": 1}
            else:
                logger.error(f"MCP tool call failed: {qualified_name}: {e}")
                return {"error": str(e), "exit_code": 1}

        if server_id == BROWSER_MCP_SERVER_ID:
            result = self._postprocess_browser_result(tool_name, result)
        return result

    @staticmethod
    def _postprocess_browser_result(tool_name: str, result: Dict) -> Dict:
        """Apply the snapshot budget to successful snapshot-bearing results.

        Error text is never truncated: it is short and it is what the model
        needs verbatim to recover.
        """
        if not isinstance(result, dict) or tool_name not in _BROWSER_SNAPSHOT_TOOLS:
            return result
        if result.get("exit_code") not in (None, 0) or result.get("error"):
            return result
        text = result.get("stdout")
        if not isinstance(text, str):
            return result
        limit = _browser_snapshot_budget()
        truncated = truncate_browser_snapshot(text, limit)
        if truncated is not text:
            result = dict(result)
            result["stdout"] = truncated
        return result

    async def ensure_builtin_browser_current(self) -> bool:
        """Restart the built-in browser when its launch args went stale.

        Compares the argv the running server was started with against what
        the current settings produce; on a mismatch the server is restarted
        (profile, headless, vision caps and CDP endpoint all take effect).
        Returns True when a restart happened.
        """
        try:
            from src.builtin_mcp import browser_launch_is_stale, restart_builtin_browser
        except Exception:  # pragma: no cover - isolated loads
            return False
        try:
            if not browser_launch_is_stale(self):
                return False
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(f"browser launch staleness check failed: {exc}")
            return False
        lock = self._reconnect_locks.setdefault(BROWSER_MCP_SERVER_ID, asyncio.Lock())
        async with lock:
            if not browser_launch_is_stale(self):
                return False
            logger.info("Built-in browser settings changed; restarting the browser MCP server")
            await restart_builtin_browser(self)
            return True

    async def _do_call(self, session, tool_name: str, arguments: Dict) -> Dict:
        """Execute a single MCP tool call and return result dict."""
        result = await asyncio.wait_for(
            session.call_tool(tool_name, arguments), timeout=MCP_CALL_TIMEOUT_S
        )
        output_parts = []
        images = []
        for content in result.content:
            if hasattr(content, 'text'):
                output_parts.append(content.text)
            elif getattr(content, 'type', '') == 'image' and hasattr(content, 'data'):
                # Image content (e.g. Playwright screenshots)
                mime = getattr(content, 'mimeType', 'image/png')
                images.append({"data": content.data, "mimeType": mime})
                output_parts.append(f"[Screenshot captured ({mime})]")
            elif hasattr(content, 'data'):
                output_parts.append(str(content.data))

        output = "\n".join(output_parts)
        is_error = getattr(result, 'isError', False)

        result_dict = {
            "stdout": output if not is_error else "",
            "stderr": output if is_error else "",
            "exit_code": 1 if is_error else 0,
        }
        if is_error and output:
            result_dict["untrusted_content"] = True
        if images:
            result_dict["images"] = images
        return result_dict

    async def _reconnect_builtin(self, server_id: str) -> bool:
        """Tear down and reconnect a crashed builtin MCP server.

        Covers both the Python built-ins (`_BUILTIN_SERVERS`) and the NPX
        ones (`_BUILTIN_NPX_SERVERS`, i.e. the Playwright browser). Before
        this the browser had no reconnect path at all: one crash left it dead
        for the rest of the session while `get_server_status` still said
        "connected". On failure the status is now set to error so the UI and
        `manage_mcp` tell the truth.
        """
        import sys
        from src.builtin_mcp import (
            _BUILTIN_NPX_SERVERS,
            _BUILTIN_SERVERS,
            builtin_python_env,
            connect_builtin_npx_server,
        )

        if server_id not in _BUILTIN_SERVERS and server_id not in _BUILTIN_NPX_SERVERS:
            return False

        lock = self._reconnect_locks.setdefault(server_id, asyncio.Lock())
        async with lock:
            name = self._connections.get(server_id, {}).get("name", server_id)
            # Clean up old connection
            await self.disconnect_server(server_id)

            try:
                if server_id in _BUILTIN_NPX_SERVERS:
                    name = _BUILTIN_NPX_SERVERS[server_id]["name"]
                    ok = await connect_builtin_npx_server(self, server_id)
                else:
                    script_rel, name = _BUILTIN_SERVERS[server_id]
                    base_dir = get_app_root()
                    script_path = os.path.join(base_dir, script_rel)
                    ok = await self.connect_server(
                        server_id=server_id,
                        name=name,
                        transport="stdio",
                        command=sys.executable,
                        args=[script_path],
                        env=builtin_python_env(base_dir),
                    )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Failed to reconnect builtin MCP server {name}: {e}")
                ok = False

            if ok:
                logger.info(f"Reconnected builtin MCP server: {name}")
            else:
                cur = self._connections.get(server_id)
                if not isinstance(cur, dict) or cur.get("status") == "connected":
                    self._connections[server_id] = {
                        "status": "error",
                        "name": name,
                        "error": "server process exited and could not be restarted",
                    }
                    self._generation += 1
            return ok

    def get_all_openai_schemas(self, disabled_map: Optional[Dict[str, set]] = None) -> List[Dict]:
        """Return all MCP tools in OpenAI function-calling format.

        Tool names are namespaced as mcp__{server_id}__{tool_name}.
        disabled_map: optional {server_id: set_of_disabled_tool_names} to filter out.
        """
        schemas = []
        browser_denied = builtin_browser_policy_disabled()
        for server_id, tools in self._tools.items():
            # Skip builtin Python servers — they use the code-block tool format
            # But include NPX-based builtins (like browser) which need function calling
            if self.is_builtin(server_id) and server_id != "builtin_browser":
                continue
            conn = self._connections.get(server_id, {})
            server_name = conn.get("name", server_id)
            disabled = (disabled_map or {}).get(server_id, set())

            identity = conn.get("identity", "")
            label = f"{server_name} ({identity})" if identity else server_name

            for tool in tools:
                if tool["name"] in disabled:
                    continue
                if server_id == BROWSER_MCP_SERVER_ID and _browser_tool_denied(tool["name"], browser_denied):
                    continue
                qualified = f"mcp__{server_id}__{tool['name']}"
                schema = {
                    "type": "function",
                    "function": {
                        "name": qualified,
                        "description": f"[MCP:{label}] {tool['description']}",
                        "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
                    },
                }
                schemas.append(schema)

        return schemas

    def get_all_tools(self, disabled_map: Optional[Dict[str, set]] = None) -> List[Dict]:
        """Return a flat list of all discovered tools with server info."""
        result = []
        browser_denied = builtin_browser_policy_disabled()
        for server_id, tools in self._tools.items():
            conn = self._connections.get(server_id, {})
            disabled = (disabled_map or {}).get(server_id, set())
            for tool in tools:
                is_disabled = tool["name"] in disabled
                if server_id == BROWSER_MCP_SERVER_ID and _browser_tool_denied(tool["name"], browser_denied):
                    is_disabled = True
                result.append({
                    "server_id": server_id,
                    "server_name": conn.get("name", server_id),
                    "name": tool["name"],
                    "qualified_name": f"mcp__{server_id}__{tool['name']}",
                    "description": tool.get("description", ""),
                    "input_schema": tool.get("input_schema") or {},
                    "is_disabled": is_disabled,
                })
        return result

    def browser_tool_names(self) -> Set[str]:
        """Qualified names of every tool the connected browser server exposes
        (policy-disabled ones included — callers deny by prefix)."""
        return {
            f"{BROWSER_MCP_PREFIX}{tool['name']}"
            for tool in self._tools.get(BROWSER_MCP_SERVER_ID, [])
            if tool.get("name")
        }

    def set_connection_meta(self, server_id: str, **meta: Any) -> None:
        """Attach bookkeeping (e.g. launch argv) to a server's status entry."""
        conn = self._connections.get(server_id)
        if isinstance(conn, dict):
            conn.update(meta)

    def plan_mode_blocked_mcp(self) -> Tuple[Dict[str, Set[str]], Set[str]]:
        """Plan mode: block every MCP tool that isn't clearly read-only.

        Returns (disabled_map, qualified_names):
          - disabled_map: {server_id: {tool_name, ...}} to hide write tools from
            the prompt/schemas (merged into the existing mcp_disabled_map).
          - qualified_names: {"mcp__<server>__<tool>", ...} for runtime rejection
            in execute_tool_block (which matches the qualified name).
        """
        disabled_map: Dict[str, Set[str]] = {}
        qualified: Set[str] = set()
        for server_id, tools in self._tools.items():
            for tool in tools:
                if not mcp_tool_is_readonly(tool):
                    disabled_map.setdefault(server_id, set()).add(tool["name"])
                    qualified.add(f"mcp__{server_id}__{tool['name']}")
        return disabled_map, qualified

    def is_builtin(self, server_id: str) -> bool:
        """Check if a server is a built-in (auto-registered) server."""
        return server_id.startswith("builtin_") or server_id in {
            "image_gen",
            "memory",
            "rag",
            "email",
        }

    def _honest_status(self, server_id: str, conn: Dict) -> Dict:
        """A stdio server whose owner task has finished has no process behind
        it any more; report that instead of the stale "connected"."""
        if conn.get("status") == "connected" and not self._stdio_owner_alive(server_id):
            return {**conn, "status": "error", "error": "server process exited (will reconnect on next call)"}
        return conn

    def get_server_status(self, server_id: str) -> Dict:
        """Get connection status for a server."""
        conn = self._connections.get(server_id)
        if conn is None:
            return {"status": "disconnected"}
        return self._honest_status(server_id, conn)

    def get_all_statuses(self) -> Dict[str, Dict]:
        """Get connection statuses for all servers."""
        return {sid: self._honest_status(sid, conn) for sid, conn in self._connections.items()}

    _cached_prompt_desc = None
    _cached_prompt_desc_key = None

    def get_tool_descriptions_for_prompt(self, disabled_map: Optional[Dict[str, set]] = None) -> str:
        """Generate text describing MCP tools for the agent system prompt. Cached."""
        cache_key = (
            frozenset((k, frozenset(v)) for k, v in (disabled_map or {}).items()),
            len(self._tools),
            self._generation,
            # The browser policy (toggle / code-execution opt-in) is read
            # from settings; the prompt must follow it without a restart.
            frozenset(builtin_browser_policy_disabled()) if BROWSER_MCP_SERVER_ID in self._tools else None,
        )
        if self._cached_prompt_desc is not None and self._cached_prompt_desc_key == cache_key:
            return self._cached_prompt_desc
        tools = self.get_all_tools(disabled_map)
        if not tools:
            return ""

        lines = ["\n\nYou also have access to external MCP tool servers. These tools are called via native function calling:"]
        by_server = {}
        for t in tools:
            # Skip builtin Python servers — they're already in the agent prompt
            # But include NPX-based builtins (like browser) which aren't hardcoded
            if self.is_builtin(t["server_id"]) and t["server_id"] != "builtin_browser":
                continue
            if t.get("is_disabled"):
                continue
            sn = t["server_name"]
            if sn not in by_server:
                by_server[sn] = []
            by_server[sn].append(t)

        if not by_server:
            return ""

        for server_name, server_tools in by_server.items():
            # Include identity (e.g. email address) if available
            sid = server_tools[0]["server_id"] if server_tools else ""
            identity = self._connections.get(sid, {}).get("identity", "")
            label = f"{server_name} ({identity})" if identity else server_name
            lines.append(f"\n**{label}:**")
            for t in server_tools:
                # Truncate long descriptions
                desc = t['description'][:120] + '...' if len(t['description']) > 120 else t['description']
                # Include the tool's declared inputs so the model calls it with
                # real argument names instead of guessing from the description
                # alone (issue #2509).
                args_hint = _format_mcp_params(t.get("input_schema"))
                lines.append(f"  - {t['qualified_name']}: {desc}{args_hint}")

        result = "\n".join(lines)
        self._cached_prompt_desc = result
        self._cached_prompt_desc_key = cache_key
        return result
