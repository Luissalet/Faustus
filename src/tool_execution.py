"""
tool_execution.py

Tool dispatcher and result formatter for the agent loop.
Routes tool blocks to MCP servers or native implementations.

Extracted from agent_tools.py.
"""

import asyncio
import collections
import contextvars
import hashlib
import json
import logging
import os
import pathlib
import re
import shlex
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Sequence, Tuple



from src.tool_security import (
    BUILTIN_EMAIL_TOOLS,
    email_tool_policy_names,
    is_public_blocked_tool,
    owner_is_admin_or_single_user,
)
from src.tool_capabilities import (
    ALWAYS_APPROVE_TOOLS,
    ToolRunSecurityContext,
    blocked_tool_result,
    command_guard_requires_approval,
)
from src.tool_approvals import ExactToolApproval
from src.tool_policy import ToolPolicy
from src.constants import MAX_OUTPUT_CHARS, MAX_READ_CHARS, MAX_DIFF_LINES, DATA_DIR
from src.tool_utils import _truncate, get_mcp_manager


class _MissingToolSecurityContext:
    pass


class _NoToolSecurityContext:
    """Explicit sentinel for non-agent callers that have no run provenance."""


_MISSING_TOOL_SECURITY_CONTEXT = _MissingToolSecurityContext()
NO_TOOL_SECURITY_CONTEXT = _NoToolSecurityContext()

# Git tools (Lote 87, OBJ-4, src/agent_tools/git_tools.py) — dispatched with
# owner + human_approved (agent git policy is owner+repo scoped, and a
# policy-denied write needs to know whether a human already approved this
# exact call), which the generic `dynamic_handlers` fallback below does not
# pass through.
_GIT_TOOL_NAMES = frozenset({
    "git_status", "git_log", "git_diff",
    "git_branch", "git_checkout", "git_commit",
    "git_push", "git_pull", "git_fetch",
})

# Persistent working directory for agent subprocesses.
# Resolves to <repo_root>/data, which is the bind-mounted volume in Docker
# (/app/data) and the local data directory for manual installs.
# Using this as cwd and HOME prevents the agent from silently creating files
# in ephemeral container layers that are lost on the next rebuild.
_AGENT_WORKDIR = DATA_DIR



# ---------------------------------------------------------------------------
# Path confinement for read_file / write_file
# ---------------------------------------------------------------------------
# read_file + write_file are admin-only tools, but the path the agent
# supplies is model-controlled. Prompt-injection in an admin's chat can
# weaponise "read /etc/shadow" or "write ~/.ssh/authorized_keys" without
# the admin noticing.
#
# Policy:
#   1. Sensitive-subpath deny list — checked FIRST. Blocks .ssh,
#      .gnupg, shell rc files, token/env files even if the root above
#      them is on the allowlist.
#   2. Allowlist — only the directories the agent legitimately needs
#      (project data/, system tmp). $HOME is NOT on the default list.
#   3. Opt-in extra roots — admin can add broader roots via the
#      "tool_path_extra_roots" setting (list of path strings).
# ---------------------------------------------------------------------------

_SENSITIVE_BASENAMES: set[str] = {
    ".ssh", ".gnupg", ".gitconfig",
    ".bashrc", ".bash_profile", ".bash_logout",
    ".zshrc", ".zprofile", ".zshenv",
    ".profile", ".tcshrc", ".cshrc",
    ".env", ".netrc",
}

_SENSITIVE_FILE_PATTERNS: tuple[str, ...] = (
    "authorized_keys", "id_rsa", "id_ed25519", "id_ecdsa",
    "known_hosts",
)

# SEC-03: Windows reserved device names. On Windows these are refused by the
# filesystem itself for *any* component of a path (not just the final
# segment) whether or not an extension follows — "CON", "con.txt" and
# "logs\\NUL\\out.log" all name a device, never a regular file or directory.
# A model-controlled path that names one is worth rejecting on every platform
# this process runs on: a write meant for a real file that silently lands on
# a device (or errors in a way the model then "fixes" by retrying with a
# slightly different name) is the kind of surprising, hard-to-audit behavior
# this resolver exists to prevent — and a workspace synced onto a Windows
# host later would hit the OS-level refusal anyway.
_WINDOWS_RESERVED_NAMES_CF: frozenset[str] = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{n}" for n in range(1, 10)}
    | {f"lpt{n}" for n in range(1, 10)}
)


def _is_windows_reserved_name(component: str) -> bool:
    """Whether *component* (one path segment, no separators) names a Windows
    reserved device — matched on the stem before the first ``.``, the same
    way Windows itself treats ``CON.txt`` as the ``CON`` device, and
    case-insensitively for the same reason ``_is_sensitive_path`` folds case.
    """
    stem = component.split(".", 1)[0]
    return stem.casefold() in _WINDOWS_RESERVED_NAMES_CF

# Case-folded views used for matching. On a case-insensitive filesystem
# (Windows, default macOS) ".SSH/AUTHORIZED_KEYS" and ".env" resolve to the
# same protected files as their lowercase forms, so the deny-list has to fold
# case before comparing — the sibling resolver already normcases paths for the
# same reason. casefold (not os.path.normcase) because normcase is a no-op on
# POSIX, which is exactly where the macOS read-exfil path lives.
_SENSITIVE_BASENAMES_CF: frozenset[str] = frozenset(b.casefold() for b in _SENSITIVE_BASENAMES)
_SENSITIVE_FILE_PATTERNS_CF: frozenset[str] = frozenset(p.casefold() for p in _SENSITIVE_FILE_PATTERNS)


def _is_sensitive_path(resolved: str) -> bool:
    """Return True if *resolved* falls under a sensitive directory, matches a
    sensitive filename, or (SEC-03) names a Windows reserved device in any
    component — regardless of what root it sits under.

    Matching is case-insensitive: on Windows / default macOS a case-variant
    name (``.SSH``, ``AUTHORIZED_KEYS``, ``Id_Rsa``) points at the same file as
    the lowercase form, so a case-sensitive check would let it slip past the
    deny-list in every file tool that relies on it.
    """
    parts = [p.casefold() for p in re.split(r"[\\/]", resolved)]
    filename = parts[-1] if parts else ""

    # Check if any path component is a sensitive directory.
    for part in parts:
        if part in _SENSITIVE_BASENAMES_CF:
            return True

    # SEC-03: a reserved device name in ANY component (not just the
    # filename) is rejected — "CON\\out.log" resolves through the device,
    # not a directory named CON, on the one platform where it matters.
    for part in parts:
        if part and _is_windows_reserved_name(part):
            return True

    # Check filename against known sensitive files.
    return filename in _SENSITIVE_FILE_PATTERNS_CF


def _tool_path_roots() -> list[str]:
    """Return the list of directory roots that read_file / write_file
    may touch. Default: project data/ + system temp dirs. Extra roots
    are loaded from the ``tool_path_extra_roots`` setting.
    """
    roots: list[str] = []

    # Project data directory — the agent's primary workspace.
    from src.constants import DATA_DIR
    roots.append(DATA_DIR)

    # /tmp (and its macOS realpath /private/tmp).
    roots.append("/tmp")
    try:
        private_tmp = os.path.realpath("/tmp")
        if private_tmp != "/tmp":
            roots.append(private_tmp)
    except OSError:
        pass

    # $TMPDIR — per-user temp root on macOS (e.g. /var/folders/.../T/).
    tmpdir = os.environ.get("TMPDIR")
    if tmpdir:
        roots.append(tmpdir)
    # The platform temp dir (Windows: %TEMP%, which no /tmp rule covers).
    try:
        import tempfile as _tempfile
        roots.append(_tempfile.gettempdir())
    except Exception:
        pass

    # Opt-in extra roots from settings.
    try:
        from src.settings import get_setting
        extra = get_setting("tool_path_extra_roots")
        if isinstance(extra, list):
            roots.extend(str(r) for r in extra if r)
    except Exception:
        pass

    # Deduplicate; resolve symlinks so containment is unambiguous.
    seen: set[str] = set()
    out: list[str] = []
    for r in roots:
        try:
            real = os.path.realpath(r)
        except OSError:
            continue
        if real in seen:
            continue
        seen.add(real)
        out.append(real)
    return out


def _resolve_tool_path(raw_path: str) -> str:
    """Resolve and confine a model-supplied path.

    Order of checks:
      1. Non-empty path.
      2. Sensitive-subpath deny list (blocks .ssh, .gnupg, etc.
         even when the root is on the allowlist).
      3. Allowlist containment (must land under one of the roots).

    Returns the realpath on success. Raises ValueError on rejection.
    Symlinks are resolved before comparison.

    When a workspace is active for this turn, paths are confined to it instead
    of the default allowlist (see _resolve_tool_path_in_workspace).
    """
    roots = get_active_workspace_roots()
    if roots:
        return _resolve_tool_path_in_roots(list(roots), raw_path, get_active_workspace())
    if raw_path is None or not str(raw_path).strip():
        raise ValueError("path is required")
    expanded = os.path.expanduser(str(raw_path).strip())
    resolved = os.path.realpath(expanded)

    if _is_sensitive_path(resolved):
        raise ValueError(
            f"path '{raw_path}' is inside a sensitive directory "
            f"(e.g. .ssh, .gnupg) or matches a sensitive filename"
        )

    for root in _tool_path_roots():
        if resolved == root:
            return resolved
        try:
            common = os.path.commonpath([resolved, root])
        except ValueError:
            continue
        if common == root:
            return resolved
    raise ValueError(
        f"path '{raw_path}' is outside the allowed roots"
    )


def _resolve_tool_path_in_workspace(workspace: str, raw_path: str) -> str:
    """Confine a model-supplied path to the active workspace.

    Layered on top of upstream's path policy: the workspace is the allowed
    root (relative paths resolve under it; paths that escape it are rejected),
    and the sensitive-file deny list (.ssh, .gnupg, id_rsa, …) still applies
    inside it. When no workspace is set, callers use _resolve_tool_path (the
    default data/tmp allowlist) instead.
    """
    if raw_path is None or not str(raw_path).strip():
        raise ValueError("path is required")
    base = os.path.realpath(workspace)
    expanded = os.path.expanduser(str(raw_path).strip())
    candidate = expanded if os.path.isabs(expanded) else os.path.join(base, expanded)
    resolved = os.path.realpath(candidate)
    if _is_sensitive_path(resolved):
        raise ValueError(
            f"path '{raw_path}' is inside a sensitive directory "
            f"(e.g. .ssh, .gnupg) or matches a sensitive filename"
        )
    if resolved != base:
        # normcase so containment holds on case-insensitive filesystems
        # (Windows, default macOS): it lowercases on Windows and is a no-op on
        # POSIX. commonpath raises ValueError across Windows drives (C: vs D:)
        # or mixed abs/rel — both mean "outside", so the except rejects them.
        nbase = os.path.normcase(base)
        try:
            if os.path.commonpath([os.path.normcase(resolved), nbase]) != nbase:
                raise ValueError
        except ValueError:
            raise ValueError(f"path '{raw_path}' is outside the workspace ({workspace})")
    return resolved



# ---------------------------------------------------------------------------
# Active workspace (per-turn, context-local)
# ---------------------------------------------------------------------------
# Set ONCE in execute_tool_block from the request's `workspace`. The path
# resolvers (_resolve_tool_path / _resolve_search_root) and the subprocess cwd
# helper (agent_cwd) read it from here, so confinement is enforced in a single
# place: any tool that resolves paths through these helpers is confined
# automatically and cannot accidentally bypass the workspace. contextvars are
# task-local, so concurrent turns don't leak into each other.
_active_workspace: contextvars.ContextVar = contextvars.ContextVar(
    "agent_active_workspace", default=None
)

_active_workspace_roots: contextvars.ContextVar = contextvars.ContextVar(
    "agent_active_workspace_roots", default=()
)

# Per-turn options the tool handlers may need (sampling overrides for
# delegated workers, the project's harness knobs). Same task-local scoping.
_active_turn_options: contextvars.ContextVar = contextvars.ContextVar(
    "agent_active_turn_options", default=None
)


def get_active_turn_options() -> dict:
    return dict(_active_turn_options.get() or {})


def get_active_workspace() -> Optional[str]:
    """The folder the agent is confined to this turn, or None."""
    return _active_workspace.get()


def get_active_workspace_roots() -> tuple[str, ...]:
    """All file/folder roots attached to the active project turn."""
    roots = tuple(_active_workspace_roots.get() or ())
    if roots:
        return roots
    workspace = get_active_workspace()
    return (workspace,) if workspace else ()


def vet_workspace(raw: str) -> Optional[str]:
    """Validate a requested workspace path at bind time.

    Returns the canonical path, or None when it is unusable: not a real
    directory, or itself a sensitive path (.ssh, .gnupg, ...). The in-workspace
    resolver deny-lists sensitive paths *inside* the workspace, but the
    empty-path search root is the workspace itself, so the root has to be
    vetted before it is ever bound.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    resolved = os.path.realpath(os.path.expanduser(raw))
    if not os.path.isdir(resolved) or _is_sensitive_path(resolved):
        return None
    # Reject filesystem roots: binding / (or a Windows drive/UNC root) as the
    # workspace would make every absolute path "inside" it, collapsing the
    # confinement into host-wide file access. A root is its own dirname, which
    # also covers C:\ and \\server\share without platform-specific lists.
    if os.path.dirname(resolved) == resolved:
        return None
    return resolved


def _path_is_within_root(resolved: str, root: str) -> bool:
    root = os.path.realpath(root)
    resolved = os.path.realpath(resolved)
    if os.path.isfile(root):
        return os.path.normcase(resolved) == os.path.normcase(root)
    try:
        return os.path.commonpath([
            os.path.normcase(resolved), os.path.normcase(root)
        ]) == os.path.normcase(root)
    except ValueError:
        return False


def _resolve_tool_path_in_roots(
    roots: list[str], raw_path: str, primary_workspace: Optional[str] = None
) -> str:
    """Resolve a path against all file/folder roots of the active project."""
    if raw_path is None or not str(raw_path).strip():
        raise ValueError("path is required")
    clean_roots = [os.path.realpath(root) for root in roots if root]
    expanded = os.path.expanduser(str(raw_path).strip())
    if os.path.isabs(expanded):
        resolved = os.path.realpath(expanded)
    else:
        base = primary_workspace or next(
            (root for root in clean_roots if os.path.isdir(root)), None
        )
        if not base:
            raise ValueError("relative paths require a project folder root")
        resolved = os.path.realpath(os.path.join(base, expanded))
    if _is_sensitive_path(resolved):
        raise ValueError(
            f"path '{raw_path}' is inside a sensitive directory or matches a sensitive filename"
        )
    if any(_path_is_within_root(resolved, root) for root in clean_roots):
        return resolved
    raise ValueError(f"path '{raw_path}' is outside the workspace / project work roots")


def vet_project_root(raw: str) -> Optional[str]:
    """Validate a file or directory before attaching it as a project root.

    Project roots may be individual files as well as directories. They reject
    missing targets, sensitive locations and filesystem roots. Tool calls
    re-check and confine concrete paths against the canonical roots.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    resolved = os.path.realpath(os.path.expanduser(raw))
    if not os.path.exists(resolved) or _is_sensitive_path(resolved):
        return None
    if os.path.isdir(resolved) and os.path.dirname(resolved) == resolved:
        return None
    return resolved


vet_readonly_context = vet_project_root


def agent_cwd() -> str:
    """Working directory for agent subprocesses (bash/python/background jobs):
    the active workspace when set, else the persistent data dir."""
    return get_active_workspace() or _AGENT_WORKDIR


def get_mcp_manager():
    from src import agent_tools
    return agent_tools.get_mcp_manager()




def _resolve_search_root(raw_path: str) -> str:
    """Resolve + confine a code-nav path (grep/glob/ls).

    With a workspace active, the workspace folder is the root and a supplied
    path is confined inside it. Otherwise an empty path defaults to the agent's
    primary root (project data dir) and a supplied path is confined by the
    global allowlist + sensitive-file policy.
    """
    raw = (raw_path or "").strip()
    roots = get_active_workspace_roots()
    ws = get_active_workspace()
    if roots:
        if not raw:
            return os.path.realpath(ws or next((r for r in roots if os.path.isdir(r)), roots[0]))
        return _resolve_tool_path_in_roots(list(roots), raw, ws)
    if not raw:
        roots = _tool_path_roots()
        return roots[0] if roots else os.path.realpath(".")
    return _resolve_tool_path(raw)

logger = logging.getLogger(__name__)


_ADMIN_TOOLS = {
    "app_api",
    "manage_endpoints",
    "manage_mcp",
    "manage_webhooks",
    "manage_tokens",
    "manage_settings",
    "manage_teach_mode",
    "capability_health",
    "branch_futures",
    "download_model",
    "serve_model",
    "serve_preset",
    "stop_served_model",
    "cancel_download",
}


def _owner_is_admin(owner: Optional[str]) -> bool:
    """Mirror route-level admin behavior for agent tool execution."""
    return owner_is_admin_or_single_user(owner)

# ---------------------------------------------------------------------------
# MCP-backed tool helpers
# ---------------------------------------------------------------------------

# Map legacy tool names -> (MCP server_id, MCP tool_name)
_MCP_TOOL_MAP = {
    "bash":           ("bash",       "bash"),
    "python":         ("python",     "python"),
    "read_file":      ("filesystem", "read_file"),
    "write_file":     ("filesystem", "write_file"),
    "web_search":     ("web_search", "web_search"),
    "web_fetch":      ("web_fetch",  "web_fetch"),
    "generate_image": ("image_gen",  "generate_image"),
}
_EMAIL_MCP_OWNER_ARG = "_odysseus_owner"


def _parse_qualified_mcp_args(tool: str, content: str) -> tuple[Dict, Optional[str]]:
    raw = (content or "").strip()
    if not raw:
        return {}, None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        if tool.startswith("mcp__email__"):
            return {}, "Email MCP tool arguments must be a JSON object."
        return {}, None
    if not isinstance(parsed, dict):
        if tool.startswith("mcp__email__"):
            return {}, "Email MCP tool arguments must be a JSON object."
        return {}, None
    return parsed, None


# ---------------------------------------------------------------------------
# WEB-04 (L24/L29): precondition + readback for builtin-browser MCP actions
# ---------------------------------------------------------------------------
# src/browser_actions.py was already built (an earlier lot) for exactly this
# wiring — its own docstring says as much and defers the wiring to whichever
# lot owns this file. `is_browser_action`/`BROWSER_VIEW_ACTIONS` (src/
# browser_view.py) is reused rather than a second, slightly-different
# click/type/navigate/select/press list (rule 4 of COMUN.md): it is already
# the authoritative "does this builtin-browser tool change what the page
# shows" answer in this codebase, one `after_browser_action` (agent_loop.py)
# already keys its own screenshot-after-an-action decision on.
from src.browser_view import BROWSER_MCP_PREFIX, is_browser_action, parse_page_info

_BROWSER_SNAPSHOT_TOOL = BROWSER_MCP_PREFIX + "browser_snapshot"


def _browser_result_text(result: Any) -> str:
    """The same three keys src/browser_view.py's own (private) `_result_text`
    reads — kept as a local copy rather than importing a name that file does
    not export, since browser_view.py is outside this lot's PROPIOS."""
    if not isinstance(result, dict):
        return ""
    for key in ("stdout", "output", "stderr"):
        value = result.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _browser_snapshot_tool_for(tool: str) -> str:
    """The `browser_snapshot` call that belongs to the SAME MCP connection as
    `tool` — `mcp__<server_id>__browser_click` -> `mcp__<server_id>__
    browser_snapshot` — so a session-scoped action (see
    `_ensure_session_browser` below) gets preconditioned against ITS OWN
    page, never the shared browser's. `server_id` never itself contains
    `"__"` (`session_browser_server_id` joins owner/task with `:`), so
    splitting on it is exact. Falls back to the constant shared-browser
    snapshot tool for anything that does not parse as `mcp__x__y`.
    """
    parts = tool.split("__", 2)
    if len(parts) == 3 and parts[0] == "mcp":
        return f"mcp__{parts[1]}__browser_snapshot"
    return _BROWSER_SNAPSHOT_TOOL


async def _ensure_session_browser(mcp: Any, owner: Any, session_id: Any) -> Optional[str]:
    """Ola A wiring: connect (or reuse) THIS task's own isolated browser MCP
    connection the first time it calls a `browser_*` tool, so two tasks'
    authenticated sessions never share the one subprocess `BROWSER_SERVER_ID`
    names (`src.builtin_mcp.connect_session_browser`/`session_browser_
    server_id`, WEB-03 — built earlier with no real caller; see that
    module's own docstring).

    Returns the session-scoped server_id to route this call through, or
    `None` when there is no owner+session identity to scope by (an
    unattended/headless caller with neither falls back to the shared
    browser exactly as before this wiring existed).

    Idempotent by asking the manager's OWN connection table
    (`get_all_statuses`) rather than a second, parallel "did we already
    connect this" cache — the one thing rule 4 (COMUN.md) asks for: no
    duplicate authority over the same fact. A task's matching disconnect
    lives in `agent_loop.py`'s `stream_agent_loop` wrapper, at run end.
    """
    owner_s = str(owner or "").strip()
    session_s = str(session_id or "").strip()
    if not owner_s or not session_s:
        return None
    try:
        from src.builtin_mcp import connect_session_browser, session_browser_server_id
        server_id = session_browser_server_id(owner_s, session_s)
        statuses = mcp.get_all_statuses()
        if isinstance(statuses, dict) and statuses.get(server_id, {}).get("status") == "connected":
            return server_id
        ok, connected_id = await connect_session_browser(mcp, owner_s, session_s)
        return connected_id if ok else None
    except Exception as e:  # noqa: BLE001 - a failed session connect falls back
        # to the shared browser rather than failing the tool call outright.
        logger.warning("tool_execution: session browser connect failed for owner=%r session=%r: %s",
                       owner_s, session_s, e)
        return None


async def _run_browser_action_with_precondition(mcp: Any, tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Wrap one page-mutating builtin-browser MCP call in `src.browser_actions
    .run_with_precondition`: a FRESH `browser_snapshot` right before the call,
    checked against whatever the model told us to expect, and a fresh
    post-action readback attached either way (WEB-04's own acceptance
    criterion — "a shifted layout must not produce a blind click").

    `tool` may already be rewritten to a session-scoped server id (see
    `_ensure_session_browser`) — `_browser_snapshot_tool_for` derives the
    matching `browser_snapshot` call from the SAME connection rather than
    always the shared one.

    Two optional args the model may pass double as the precondition:
      * `ref` — Playwright's own element handle from a PRIOR snapshot. When
        present, it must still appear in the FRESH snapshot's text, or the
        action is refused instead of clicking/typing into whatever is now at
        that stale reference.
      * `expected_url` — additive: no real `@playwright/mcp` tool defines
        this field, so an ordinary call that never names it behaves exactly
        as before (never checked, always forwarded verbatim). Stripped
        before the real call either way, so an unrecognized extra key never
        reaches the actual MCP server.
    """
    from src import browser_actions

    precondition = browser_actions.ActionPrecondition(
        expected_url=str(args.get("expected_url") or ""),
        require_element=str(args.get("ref") or ""),
    )
    forward_args = {k: v for k, v in args.items() if k != "expected_url"}
    snapshot_tool = _browser_snapshot_tool_for(tool)

    async def _snapshot() -> Dict[str, Any]:
        snap = await mcp.call_tool(snapshot_tool, {})
        text = _browser_result_text(snap)
        url, title = parse_page_info(text)
        return {"url": url, "title": title, "text": text}

    async def _act() -> Any:
        return await mcp.call_tool(tool, forward_args)

    return await browser_actions.run_with_precondition(
        tool, precondition=precondition, snapshot_fn=_snapshot, act_fn=_act,
    )


def _parse_generate_image(content: str) -> Dict:
    lines = content.strip().split("\n")
    args = {"prompt": lines[0].strip() if lines else ""}
    for i, key in enumerate(["model", "size", "quality"], 1):
        if len(lines) > i and lines[i].strip():
            args[key] = lines[i].strip()
    return args


def _parse_manage_memory(content: str) -> Dict:
    lines = content.strip().split("\n")
    action = lines[0].strip().lower() if lines else ""
    args = {"action": action}
    if action == "add":
        args["text"] = lines[1].strip() if len(lines) > 1 else ""
        if len(lines) > 2 and lines[2].strip():
            args["category"] = lines[2].strip().lower()
    elif action == "edit":
        args["memory_id"] = lines[1].strip() if len(lines) > 1 else ""
        args["text"] = lines[2].strip() if len(lines) > 2 else ""
    elif action == "delete":
        args["memory_id"] = lines[1].strip() if len(lines) > 1 else ""
    elif action == "search":
        args["text"] = lines[1].strip() if len(lines) > 1 else ""
    elif action == "list":
        if len(lines) > 1 and lines[1].strip():
            args["category"] = lines[1].strip().lower()
    return args


def _parse_write_file(content: str) -> Dict:
    lines = content.split("\n", 1)
    return {"path": lines[0].strip(), "content": lines[1] if len(lines) > 1 else ""}


_MCP_ARG_PARSERS: Dict[str, Callable[[str], Dict[str, str]]] = {
    "bash":           lambda c: {"command": c},
    "python":         lambda c: {"code": c},
    "web_search":     lambda c: {"query": c.split("\n")[0].strip()},
    "web_fetch":      lambda c: {"url": c.split("\n")[0].strip()},
    "read_file":      lambda c: {"path": c.split("\n")[0].strip()},
    "write_file":     _parse_write_file,
    "generate_image": _parse_generate_image,
    "manage_memory":  _parse_manage_memory,
}


# Primary argument key(s) for the legacy line-parsed tools. When a fenced
# block's content is a JSON object carrying one of these keys, it's structured
# inline args (the relaxed parser's ```web_search {"query": "..."}``` shape) —
# use the object directly instead of letting the line-based parsers wrap the
# whole JSON string as the query/url/path/prompt. Keyed off membership only
# (the primary key never changes), so this can't drift; an unrecognized object
# safely falls through to the line-based parser, i.e. the previous behavior.
#
# IMPORTANT — this only covers the MCP path. _build_mcp_args is reached via
# _call_mcp_tool only for _MCP_TOOL_MAP tools (so an entry outside that map is
# dead, as manage_memory was). And of these, only generate_image has a live MCP
# server today; web_search/web_fetch/read_file/write_file have none, so they run
# via _direct_fallback -> TOOL_HANDLERS, whose handlers decode JSON themselves
# (see ReadFileTool/WriteFileTool/WebSearchTool/WebFetchTool). The entries here
# are kept as defense-in-depth for if/when those servers are added. The live
# fix for each server-less tool lives in its handler. test_write_file_inline_
# json_args and test_mcp_json_primary_keys_are_all_live pin both halves.
_MCP_JSON_PRIMARY_KEYS: Dict[str, tuple] = {
    "web_search":     ("query", "queries"),
    "web_fetch":      ("url",),
    "read_file":      ("path",),
    "write_file":     ("path",),
    "generate_image": ("prompt",),
}


def _build_mcp_args(tool: str, content: str) -> Dict:
    """Convert fenced-block text content to structured MCP arguments."""
    primaries = _MCP_JSON_PRIMARY_KEYS.get(tool)
    if primaries and content.strip().startswith("{"):
        try:
            decoded = json.loads(content.strip())
        except (json.JSONDecodeError, TypeError):
            decoded = None
        if isinstance(decoded, dict) and any(k in decoded for k in primaries):
            return decoded
    parser = _MCP_ARG_PARSERS.get(tool)
    return parser(content) if parser else {}


async def _call_mcp_tool(
    tool: str,
    content: str,
    progress_cb: Optional[Callable[[Dict], Awaitable[None]]] = None,
) -> Dict:
    """Route a legacy tool call through the MCP manager, with direct fallbacks."""
    mcp = get_mcp_manager()
    if not mcp:
        return await _direct_fallback(tool, content, progress_cb=progress_cb) or {"error": f"MCP manager not available for tool '{tool}'", "exit_code": 1}

    server_id, tool_name = _MCP_TOOL_MAP[tool]
    qualified = f"mcp__{server_id}__{tool_name}"
    args = _build_mcp_args(tool, content)
    result = await mcp.call_tool(qualified, args)

    # If MCP server not connected, try direct fallback
    if isinstance(result, dict) and result.get("exit_code") == 1 and "not connected" in result.get("error", ""):
        fallback = await _direct_fallback(tool, content, progress_cb=progress_cb)
        if fallback:
            return fallback

    # generate_image runs as a text-only MCP tool, so the saved image URL never
    # reaches the agent loop's structured forwarding (which renders the image via
    # buildImageBubble on result["image_url"]). Lift it out of the tool's stdout so
    # the image renders deterministically — no dependence on the model echoing the
    # URL into its prose (which it mangles/hallucinates).
    if tool == "generate_image":
        _promote_image_fields(result)

    return result


def _promote_image_fields(result: Dict) -> None:
    """Lift the image URL (+ prompt/model/size) from a successful generate_image MCP
    text result into structured fields the agent loop already forwards to
    buildImageBubble. Only acts on a dict result with exit_code 0; matches the
    generated-image URL by pattern (absolute or relative) so it's robust to the
    result's wording."""
    if not isinstance(result, dict) or result.get("exit_code") != 0:
        return
    out = result.get("stdout") or ""
    m = re.search(r'(?:https?://[^\s)\]]+)?/api/generated-image/[A-Za-z0-9._-]+', out)
    if not m:
        return
    result["image_url"] = m.group(0).strip()
    for field, pat in (
        ("image_prompt", r'^Generated image for:\s*(.+)$'),
        ("image_model", r'^model:\s*(.+)$'),
        ("image_size", r'^size:\s*(.+)$'),
    ):
        fm = re.search(pat, out, re.M)
        if fm:
            result[field] = fm.group(1).strip()


_BG_MARKERS = {"#!bg", "#bg", "# bg", "#background", "# background", "@background", "# @background"}


def _split_bg_marker(content: str):
    """If the bash content's first non-empty line is a background marker
    (e.g. `#!bg`), return (True, command_without_marker); else (False, content)."""
    lines = content.split("\n")
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i < len(lines) and lines[i].strip().lower() in _BG_MARKERS:
        del lines[i]
        return True, "\n".join(lines).strip()
    return False, content


async def _direct_fallback(
    tool: str,
    content: str,
    progress_cb: Optional[Callable[[Dict], Awaitable[None]]] = None,
    session_id: Optional[str] = None,
    owner: Optional[str] = None,
    human_approved: bool = False,
) -> Optional[Dict]:
    _subproc_env = {
        **os.environ,
        "TERM": "xterm-256color",
        "COLUMNS": "120",
        "LINES": "40",
        "HOME": _AGENT_WORKDIR,
    }

    try:
        _turn_opts = get_active_turn_options()
        _harness_options = _turn_opts.get("harness_options") or {}
        ctx = {
            "progress_cb": progress_cb,
            "subproc_env": _subproc_env,
            "session_id": session_id,
            "owner": owner,
            "gen_overrides": _turn_opts.get("gen_overrides"),
            "harness_options": _turn_opts.get("harness_options"),
            # Lote 87: whether THIS exact call was already claimed against a
            # sealed src.tool_approvals.PendingToolApproval — i.e. a human
            # answered a real approval card for this exact tool+content (see
            # execute_tool_block's `approval_claimed`). Additive and False by
            # default for every tool that doesn't check it; today only
            # src/agent_tools/git_tools.py reads it (see that module's
            # `_human_approved`) to unlock a policy-denied git_commit/
            # git_push/git_branch/git_checkout.
            "human_approved": human_approved,
            # The run's project identity, surfaced as its own ctx key so a tool
            # does not have to know that the route packs it into the harness
            # knobs (services/projects.py::agent_options puts it there). Read
            # from where it already travels rather than re-deriving it from a
            # folder name mid-run (plan §11: one context per run, resolved once).
            "project_id": str(_harness_options.get("project_id") or ""),
            # `run_id` is NOT carried in turn_options today — the agent loop
            # keeps it on ToolRunSecurityContext, which never reaches here. Left
            # empty on purpose: an invented run id in an audit trail is worse
            # than an absent one. Wiring it needs turn_options to carry it,
            # which is a change to src/agent_loop.py.
            "run_id": str(_turn_opts.get("run_id") or ""),
            "turn_id": str(_turn_opts.get("turn_id") or ""),
        }

        from src.agent_tools import TOOL_HANDLERS
        if tool in TOOL_HANDLERS:
            return await TOOL_HANDLERS[tool](content, ctx)

    except Exception as e:
        return {"error": f"{tool}: {e}", "exit_code": 1}

    return None


async def _document_tool_dispatch(
    tool: str,
    content: str,
    session_id: Optional[str] = None,
    owner: Optional[str] = None,
    document_id: Optional[str] = None,
    document_version: Optional[int] = None,
    document_digest: Optional[str] = None,
) -> Optional[Dict]:
    """Route a document tool through TOOL_HANDLERS with the right ctx shape."""
    from src.agent_tools import TOOL_HANDLERS
    ctx = {
        "session_id": session_id,
        "owner": owner,
        "doc_id": document_id,
        "expected_document_version": document_version,
        "expected_document_digest": document_digest,
    }
    if tool in TOOL_HANDLERS:
        return await TOOL_HANDLERS[tool](content, ctx)
    return None


# ---------------------------------------------------------------------------
# expert_review
# ---------------------------------------------------------------------------


def _expert_llm_call(expert: Dict, owner: Optional[str], session_id: Optional[str]):
    """The model function `src.expert_review.review` calls, bound to the
    expert's OWN model. Injected rather than imported by the review module so
    the whole pipeline stays testable without a model."""
    async def _call(messages):
        from src.ai_interaction import AI_CHAT_TIMEOUT, _resolve_model
        from src.llm_core import llm_call_async
        model_spec = str((expert or {}).get("model") or "").strip()
        url, model, headers = await asyncio.to_thread(
            _resolve_model, model_spec or "auto", owner=owner)
        kwargs = {}
        try:
            if (expert or {}).get("temperature") is not None:
                kwargs["temperature"] = float(expert["temperature"])
        except (TypeError, ValueError):
            pass
        return await llm_call_async(url, model, messages, headers=headers,
                                    timeout=AI_CHAT_TIMEOUT, session_id=session_id,
                                    **kwargs)
    return _call


async def _expert_review_action(action: str, args: Dict, session_id: Optional[str],
                                owner: Optional[str]) -> Dict:
    """One `expert_review` call. Returns a result dict — including the error
    shape — and never raises anything but ExpertReviewError/ValueError, which
    the dispatcher turns into a tool error the model can read."""
    from src import expert_review as review_mod
    from src import story_bible as bible_mod

    def _project():
        from services.projects import project_for_session
        return project_for_session(session_id or "", owner)

    if action == "experts":
        slug = str(args.get("slug") or "").strip()
        if slug:
            expert = review_mod.load_expert(slug)
            return {"expert": {k: expert.get(k) for k in
                               ("slug", "name", "model", "rubric", "instructions")
                               if k in expert}}
        listing = None
        for name in ("list_experts", "list_all", "all_experts", "experts"):
            fn = review_mod._experts_fn(name)
            if fn is None:
                continue
            try:
                listing = fn()
                break
            except Exception:  # noqa: BLE001 - try the next spelling instead
                continue
        if listing is None:
            raise review_mod.ExpertReviewError(
                "The expert store does not expose a listing here; call "
                "expert_review with action 'experts' and a slug to read one profile")
        rows = []
        for item in listing or []:
            if isinstance(item, dict):
                rows.append({k: item.get(k) for k in ("slug", "name", "model")
                             if k in item})
            else:
                rows.append({"slug": str(item)})
        return {"experts": rows}

    if action == "bible":
        project = _project()
        if not project:
            return {"error": "This chat is not attached to a project", "exit_code": 1}
        deltas = args.get("deltas")
        if isinstance(deltas, list) and deltas:
            return bible_mod.apply_deltas(project, deltas, "agent", session_id=session_id)
        text = str(args.get("text") or "")
        if text.strip():
            bible = bible_mod.load_bible(project)
            return {"findings": bible_mod.check_continuity(text, bible),
                    "candidates": bible_mod.extract_candidates(text, bible),
                    "counts": {s: len(bible.get(s) or []) for s in bible_mod.SECTIONS}}
        return bible_mod.payload(project)

    if action == "apply":
        text = str(args.get("text") or "")
        deltas = args.get("deltas")
        if not isinstance(deltas, list):
            raise review_mod.ExpertReviewError("'apply' needs the deltas from a review")
        accept = args.get("accept")
        accept = list(accept) if isinstance(accept, (list, tuple)) else None
        applied = review_mod.apply_deltas(text, deltas, accept)
        return {"text": applied, "applied": len(accept) if accept is not None else len(deltas)}

    if action == "feedback":
        return review_mod.record_feedback(str(args.get("slug") or ""),
                                          args.get("accepted") or 0,
                                          args.get("rejected") or 0)

    if action != "review":
        raise review_mod.ExpertReviewError(
            "Action must be review, experts, bible, apply or feedback")

    slug = str(args.get("slug") or "").strip()
    text = str(args.get("text") or "")
    expert = review_mod.load_expert(slug)
    story = None
    if args.get("use_bible", True):
        try:
            project = _project()
            if project:
                story = bible_mod.load_bible(project)
        except Exception as exc:  # noqa: BLE001 - the bible is optional context
            logger.debug("expert_review could not load the story bible: %s", exc)
    result = await review_mod.review(
        slug, text, llm_call=_expert_llm_call(expert, owner, session_id),
        story=story, max_chars=args.get("max_chars"))
    return review_mod.compact_result(result)


# ---------------------------------------------------------------------------
# verify_claim
# ---------------------------------------------------------------------------

#: Said only when nothing settled the claim — the one moment a model would
#: otherwise wait for a rung that is not there.
_NO_LAYER_5 = ("layer 5 (model judgement) is not available from this tool: it needs a judge "
               "model and the point of this ladder is that it is deterministic. `layer: null` "
               "means nothing here could show the claim, which is not the same as false")


def _verify_claim_action(content: str) -> Dict:
    """One `verify_claim` call: the deterministic ladder of src/claim_verify.py
    over the claim and the source the model already has.

    No judge is injected, on purpose — layers 1 to 4 need no model, and layer 5
    would put a model's opinion where a caller reads a deterministic score.
    The module itself never raises; what can go wrong here is the CALL: junk
    instead of JSON, or a call with nothing to check.
    """
    from src import claim_verify
    args = json.loads(content or "{}")
    if not isinstance(args, dict):
        raise ValueError("verify_claim arguments must be an object with 'claim' and 'source'")
    claim = args.get("claim")
    source = args.get("source")
    claim = "" if claim is None else str(claim)
    source = "" if source is None else str(source)
    if not claim.strip():
        raise ValueError("verify_claim: 'claim' is required — the sentence to check")
    if not source.strip():
        raise ValueError("verify_claim: 'source' is required — the text the claim must be "
                         "supported by. Nothing is fetched: pass the text you already have")
    verdict = claim_verify.verify(claim, source)
    out: Dict = {
        "supported": verdict["supported"],
        "layer": verdict["layer"],
        "confidence": verdict["confidence"],
        "why": verdict["why"],
        "unsupported_terms": verdict["unsupported_terms"],
        "label": verdict["label"],
        "claim": claim[:1000],
        "source_chars": len(source),
    }
    url = str(args.get("url") or "").strip()
    if url:
        # Where the text came from, recorded so the verdict can be cited. It
        # is never fetched: the source of truth is the text in the call.
        out["source_url"] = url[:2048]
    if verdict["layer"] is None:
        out["note"] = _NO_LAYER_5
    return out


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

async def execute_tool_block(
    block: Any,
    session_id: Optional[str] = None,
    disabled_tools: Optional[set] = None,
    owner: Optional[str] = None,
    progress_cb: Optional[Callable[[Dict], Awaitable[None]]] = None,
    workspace: Optional[str] = None,
    workspace_roots: Optional[list[str]] = None,
    tool_policy: Optional[Any] = None,
    security_context: (
        ToolRunSecurityContext
        | _NoToolSecurityContext
        | _MissingToolSecurityContext
    ) = _MISSING_TOOL_SECURITY_CONTEXT,
    exact_approval: Optional[ExactToolApproval] = None,
    turn_options: Optional[dict] = None,
) -> Tuple[str, Dict]:
    """Execute a single tool block. Returns (description, result_dict).

    Thin wrapper: bind the per-turn workspace (so the path resolvers + subprocess
    cwd confine to it) for the duration of this call, then delegate. Reset on the
    way out so the binding never leaks to the next tool call.
    """
    if security_context is _MISSING_TOOL_SECURITY_CONTEXT:
        raise TypeError(
            "execute_tool_block requires security_context; pass a "
            "ToolRunSecurityContext or NO_TOOL_SECURITY_CONTEXT explicitly"
        )
    if (
        not isinstance(security_context, ToolRunSecurityContext)
        and security_context is not NO_TOOL_SECURITY_CONTEXT
    ):
        raise TypeError(
            "security_context must be a ToolRunSecurityContext or "
            "NO_TOOL_SECURITY_CONTEXT"
        )

    approval_claimed = False
    if exact_approval is not None:
        # An always-approve tool (desktop input, FAUSTUS) is sealed and
        # approved per call whether or not the run ever saw external
        # context, so the "armed run" precondition below does not apply to
        # it; the digest still binds the exact action. A DANGEROUS/CRITICAL
        # shell command sealed by the destructive command guard is the same
        # kind of per-call approval — its card is created on clean runs too —
        # and the check is a deterministic recomputation from the SEALED
        # content, so it cannot widen to a different command (the claim below
        # still revalidates the digest byte for byte).
        _per_call_tool = (
            exact_approval.pending.tool_name in ALWAYS_APPROVE_TOOLS
            or command_guard_requires_approval(
                exact_approval.pending.tool_name,
                exact_approval.pending.content,
            )
        )
        if not isinstance(security_context, ToolRunSecurityContext) or (
            not _per_call_tool
            and (
                not security_context.external_untrusted_context_seen
                or not exact_approval.pending.external_untrusted_context_seen
            )
        ):
            return (
                f"{getattr(block, 'tool_type', None)}: BLOCKED",
                {
                    "error": "Exact-action approval requires an armed run security context.",
                    "exit_code": 1,
                    "blocked": True,
                    "policy": "exact_tool_approval",
                },
            )
        if (
            exact_approval.pending.tool_name
            in {"edit_document", "suggest_document", "update_document"}
            and (
                not exact_approval.pending.document_id
                or exact_approval.pending.document_version is None
                or not exact_approval.pending.document_digest
            )
        ):
            return (
                f"{getattr(block, 'tool_type', None)}: BLOCKED",
                {
                    "error": (
                        "The approved document action has no sealed target and "
                        "cannot be executed."
                    ),
                    "exit_code": 1,
                    "blocked": True,
                    "policy": "exact_tool_approval",
                },
            )
        sealed_workspace = exact_approval.pending.workspace
        if sealed_workspace and vet_workspace(sealed_workspace) != sealed_workspace:
            return (
                f"{getattr(block, 'tool_type', None)}: BLOCKED",
                {
                    "error": (
                        "The approved workspace is no longer a valid safe "
                        "directory. Review the action again."
                    ),
                    "exit_code": 1,
                    "blocked": True,
                    "policy": "exact_tool_approval",
                },
            )
        approval_claimed = exact_approval.claim(
            owner=owner,
            session_id=session_id,
            tool_name=getattr(block, "tool_type", None),
            content=getattr(block, "content", None),
            workspace=workspace,
        )
        if not approval_claimed:
            return (
                f"{getattr(block, 'tool_type', None)}: BLOCKED",
                {
                    "error": "The exact-action approval did not match this tool request.",
                    "exit_code": 1,
                    "blocked": True,
                    "policy": "exact_tool_approval",
                },
            )

    if isinstance(security_context, ToolRunSecurityContext) and not approval_claimed:
        decision = security_context.decision_for(
            getattr(block, "tool_type", None),
            getattr(block, "content", None),
        )
        if not decision.allowed:
            logger.warning(
                "External-context policy blocked tool=%r",
                getattr(block, "tool_type", None),
            )
            return blocked_tool_result(
                getattr(block, "tool_type", None),
                decision.reason or "Tool blocked by external-context policy.",
            )

    # Sub-agent file locks (src/agent_tools/subagent_tools.py): a worker must
    # not write a file another worker owns. Checked after the security gate,
    # before dispatch; no-op outside a delegation.
    try:
        from src.agent_tools.subagent_tools import write_block_reason as _lock_reason, note_write_result as _lock_note
        _locked = _lock_reason(getattr(block, "tool_type", None), getattr(block, "content", None))
    except Exception:
        _locked, _lock_note = None, None
    if _locked:
        return (
            f"{getattr(block, 'tool_type', None)}: BLOCKED",
            {"error": _locked, "exit_code": 1, "locked": True, "policy": "subagent_file_lock"},
        )

    roots = []
    for candidate in [workspace, *(workspace_roots or [])]:
        vetted = vet_project_root(candidate or "")
        if vetted and vetted not in roots:
            roots.append(vetted)
    token = _active_workspace.set(workspace or None)
    roots_token = _active_workspace_roots.set(tuple(roots))
    opts_token = _active_turn_options.set(turn_options or None)
    try:
        _tool_started_at = time.monotonic()
        output = await _execute_tool_block_impl(
            block,
            session_id=session_id,
            disabled_tools=disabled_tools,
            owner=owner,
            progress_cb=progress_cb,
            tool_policy=tool_policy,
            approved_document_id=(
                exact_approval.pending.document_id
                if approval_claimed
                else None
            ),
            approved_document_version=(
                exact_approval.pending.document_version
                if approval_claimed
                else None
            ),
            approved_document_digest=(
                exact_approval.pending.document_digest
                if approval_claimed
                else None
            ),
            human_approved=approval_claimed,
        )
        # CALL-05: normalize the tool's own ad hoc result dict into the
        # typed ToolResult contract (src/tool_result.py), at THIS single
        # execution point — every caller (chat, workflow, subagent, MCP)
        # already funnels through execute_tool_block, so this is the one
        # place the classification needs to happen for all of them, without
        # any of the ~40 files under src/agent_tools/ changing what they
        # return. Best-effort and read-only: never raises, never touches
        # `output` — a transport-successful call whose dict carries a
        # functional error (`error` set, or a non-zero `exit_code`) is
        # logged as `status="failed"` here, never as done, even though
        # nothing downstream of this function reads `_typed_result` yet (the
        # natural next consumer — folding this into a run's own done/error
        # state — lives in src/agent_runs.py, outside this lote's files).
        try:
            from src.tool_result import normalize_tool_result
            _typed_result = normalize_tool_result(output[1] if len(output) > 1 else None)
            if _typed_result.status != "succeeded":
                logger.debug(
                    "tool_result normalized: tool=%s status=%s%s",
                    getattr(block, "tool_type", None), _typed_result.status,
                    f" error={_typed_result.error.message}" if _typed_result.error else "",
                )
        except Exception:
            logger.debug(
                "normalize_tool_result failed for tool=%s",
                getattr(block, "tool_type", None), exc_info=True,
            )
        if isinstance(security_context, ToolRunSecurityContext):
            security_context.observe_tool_result(
                getattr(block, "tool_type", None),
                output[1],
                getattr(block, "content", None),
            )
        if _lock_note is not None:
            try:
                _lock_note(getattr(block, "tool_type", None), getattr(block, "content", None), output[1])
            except Exception:
                pass
        # Modo Enséñame records the semantic action/result while this turn's
        # session, owner and project bindings are still available. It is
        # deliberately best-effort: teaching must never turn a successful
        # tool into a failed tool. The control tool itself is excluded so
        # "start recording" does not become step one of the learned task.
        _captured_tool = str(getattr(block, "tool_type", None) or "")
        if _captured_tool != "manage_teach_mode" and owner and session_id:
            try:
                from src.settings import get_setting as _teach_setting
                if _teach_setting("agent_teach_mode", False):
                    from services.projects import project_for_session as _project_for_session
                    from src.teach_mode.service import capture_tool_observation as _capture_teach
                    _project = _project_for_session(session_id, owner) or {}
                    try:
                        _arguments = json.loads(str(getattr(block, "content", "") or "{}"))
                    except Exception:
                        _arguments = {"content": str(getattr(block, "content", "") or "")}
                    _result = output[1] if len(output) > 1 else {}
                    _failed = bool(isinstance(_result, dict) and (
                        _result.get("error") or int(_result.get("exit_code") or 0) != 0
                    ))
                    _capture_teach(
                        owner=str(owner), session_id=str(session_id),
                        project_id=str(_project.get("id") or ""), tool=_captured_tool,
                        arguments=_arguments, result=_result, success=not _failed,
                        duration_ms=max(0, int((time.monotonic() - _tool_started_at) * 1000)),
                    )
            except Exception:
                logger.exception("teach capture hook failed without affecting tool=%s", _captured_tool)
        return output
    finally:
        _active_turn_options.reset(opts_token)
        _active_workspace_roots.reset(roots_token)
        _active_workspace.reset(token)


async def _execute_tool_block_impl(
    block: Any,
    session_id: Optional[str] = None,
    disabled_tools: Optional[set] = None,
    owner: Optional[str] = None,
    progress_cb: Optional[Callable[[Dict], Awaitable[None]]] = None,
    tool_policy: Optional[Any] = None,
    approved_document_id: Optional[str] = None,
    approved_document_version: Optional[int] = None,
    approved_document_digest: Optional[str] = None,
    human_approved: bool = False,
) -> Tuple[str, Dict]:
    """Execute a single tool block. Returns (description, result_dict).

    `progress_cb` is forwarded to long-running subprocess tools
    (bash, python) so the agent loop can emit `tool_progress` SSE
    events while the command is in flight. Ignored by other tools.

    `human_approved` (Lote 87): whether the caller's `exact_approval` was
    claimed for THIS exact call — i.e. a human answered a real approval card
    sealing this tool+content. Forwarded into ctx["human_approved"] for the
    handful of tools (today: git_commit/git_push/git_branch/git_checkout)
    that need to tell "a human explicitly approved this" from "nothing
    stopped me".
    """
    from src.tool_implementations import (
        do_search_chats, do_manage_tasks,
        do_manage_skills, do_api_call, do_manage_notes,
        do_manage_calendar,
        do_download_model, do_serve_model, do_list_served_models, do_stop_served_model,
        do_tail_serve_output,
        do_list_downloads, do_cancel_download, do_search_hf_models, do_list_cached_models,
        do_list_serve_presets, do_serve_preset, do_adopt_served_model,
        do_list_cookbook_servers,
        do_edit_image, do_trigger_research, do_manage_research, do_resolve_contact,
        do_manage_contact,
        do_vault_search, do_vault_get, do_vault_unlock,
        do_app_api,
    )

    # HACK:
    # This is a temporary workaround for a circular dependency between
    # tool_execution.py and agent_tools.__init__.py.
    #
    # See issue #4277:
    # refactor(tools): Move the registry from __init__.py into a
    # dedicated registry.py module.
    #
    # Do not copy this pattern elsewhere. This import should be removed
    # once the registry refactor is completed.
    try:
        agent_tools_mod = __import__("src.agent_tools", fromlist=["TOOL_HANDLERS"])
        dynamic_handlers = getattr(agent_tools_mod, "TOOL_HANDLERS", {})
    except ImportError:
        dynamic_handlers = {}

    tool = block.tool_type
    content = block.content

    # The block/disable gates below must match every policy-equivalent
    # spelling of the tool name (bare email names alias their mcp__email__
    # form — see email_tool_policy_names), not just the spelling the model
    # happened to emit.
    policy_names = email_tool_policy_names(tool)

    # Misformatted tool call detection: model put JSON inside ```python``` (or
    # similar) without naming the tool. Common with MiniMax-style outputs.
    # Return a helpful error so the model retries with the correct format.
    if tool in ("python", "json", "xml") and content.strip().startswith("{") and content.strip().endswith("}"):
        try:
            parsed = json.loads(content.strip())
            if isinstance(parsed, dict):
                desc = f"{tool}: misformatted tool call"
                result = {
                    "error": (
                        f"You wrote a JSON object inside a ```{tool}``` block, but that's not a tool call.\n"
                        "To call a tool, use the tool name as the fence tag, e.g.\n"
                        "```resolve_contact\n"
                        "{\"name\": \"...\"}\n"
                        "```\n"
                        "or\n"
                        "```send_email\n"
                        "{\"to\": \"...\", \"subject\": \"...\", \"body\": \"...\"}\n"
                        "```"
                    ),
                    "exit_code": 1,
                }
                return desc, result
        except (ValueError, TypeError):
            pass

    # Reject tools that the user has disabled for this request
    if disabled_tools and not policy_names.isdisjoint(disabled_tools):
        desc = f"{tool}: BLOCKED"
        result = {"error": f"Tool '{tool}' is disabled by user.", "exit_code": 1}
        logger.info(f"Tool blocked by user: {tool}")
        return desc, result

    if tool_policy and any(tool_policy.blocks(name) for name in policy_names):
        desc = f"{tool}: BLOCKED"
        result = {
            "error": f"Execution of tool '{tool}' is forbade by the active guide-only policy.",
            "exit_code": 1,
        }
        logger.warning("Tool policy blocked tool=%s", tool)
        return desc, result

    if tool in _ADMIN_TOOLS and not _owner_is_admin(owner):
        desc = f"{tool}: BLOCKED"
        result = {"error": f"Tool '{tool}' requires an admin user.", "exit_code": 1}
        logger.warning("Admin tool blocked for non-admin owner=%r tool=%s", owner, tool)
        return desc, result

    if is_public_blocked_tool(tool) and not _owner_is_admin(owner):
        desc = f"{tool}: BLOCKED"
        result = {
            "error": (
                f"Tool '{tool}' is restricted to admin users on this deployment. "
                "Ask an admin to perform this action or grant the needed permission."
            ),
            "exit_code": 1,
        }
        logger.warning("Public tool policy blocked owner=%r tool=%s", owner, tool)
        return desc, result


    # Background execution: a `bash` block whose first line is the `#!bg`
    # marker runs DETACHED — returns a job id immediately so the chat stream
    # isn't held open for a multi-minute install/ffmpeg/download. The always-on
    # monitor re-invokes the agent with the full output when the job finishes.
    if tool == "bash" and session_id:
        _is_bg, _bg_cmd = _split_bg_marker(content)
        if _is_bg and _bg_cmd:
            from src import bg_jobs
            rec = bg_jobs.launch(_bg_cmd, session_id=session_id, cwd=agent_cwd())
            short = _bg_cmd.strip().split(chr(10))[0][:80]
            desc = f"bash (background): {short}"
            result = {
                "output": (
                    f"Started background job `{rec['id']}`. It is running detached; "
                    f"do NOT wait for it or poll it. You will be automatically re-invoked "
                    f"with its full output when it finishes. Continue with other work, or "
                    f"end your turn now and resume when the result arrives. If the user "
                    f"later asks to check progress or stop it, call the manage_bg_jobs "
                    f"tool yourself (output or kill); do not tell them to run a tool "
                    f"command, and do not surface raw tool syntax in your reply."
                ),
                "exit_code": 0,
                "bg_job_id": rec["id"],
            }
            logger.info(f"Tool executed: {desc} -> bg job {rec['id']}")
            return desc, result

    # Route MCP-extracted tools through the MCP manager. Forward
    # the progress callback so long-running subprocess tools
    # (bash, python) can stream `tool_progress` events to the UI.
    if tool in _MCP_TOOL_MAP:
        first_line = content.split(chr(10))[0][:80]
        desc = f"{tool}: {first_line}"
        result = await _call_mcp_tool(tool, content, progress_cb=progress_cb)
    elif tool in ("grep", "glob", "ls", "get_workspace", "inspect_media", "plan_media_transform", "transform_media"):
        # Code-navigation tools — no MCP server; run the direct implementation.
        first_line = content.split(chr(10))[0][:80]
        desc = f"{tool}: {first_line}"
        result = await _direct_fallback(tool, content, progress_cb=progress_cb) \
            or {"error": f"{tool}: execution failed", "exit_code": 1}
    elif tool in ("apply_patch", "todowrite"):
        first_line = content.split(chr(10))[0][:80]
        desc = f"{tool}: {first_line}" if first_line else tool
        result = await _direct_fallback(tool, content, session_id=session_id, owner=owner) \
            or {"error": f"{tool}: execution failed", "exit_code": 1}
    elif tool == "delegate_agents":
        # Multi-agent delegation: needs the parent session (model route, child
        # chats), the owner, and the progress callback for live worker cards.
        desc = "delegate_agents"
        result = await _direct_fallback(tool, content, progress_cb=progress_cb, session_id=session_id, owner=owner) \
            or {"error": "delegate_agents: execution failed", "exit_code": 1}
        try:
            _n = len((result or {}).get("subagents") or [])
            if _n:
                desc = f"delegate_agents: {_n} worker(s)"
        except Exception:
            pass
    elif tool == "manage_bg_jobs":
        # Inspect/kill detached `bash` jobs; needs session_id to scope to chat.
        desc = f"manage_bg_jobs: {content.split(chr(10))[0][:80]}"
        result = await _direct_fallback(tool, content, session_id=session_id, owner=owner) \
            or {"error": "manage_bg_jobs: execution failed", "exit_code": 1}
    elif tool in ("create_document", "update_document", "edit_document",
                  "suggest_document", "manage_documents"):
        desc = f"{tool}: {content.split(chr(10))[0][:80]}"
        result = await _document_tool_dispatch(
            tool,
            content,
            session_id,
            owner,
            document_id=approved_document_id,
            document_version=approved_document_version,
            document_digest=approved_document_digest,
        ) \
            or {"error": f"{tool}: execution failed", "exit_code": 1}
        if tool in ("edit_document", "suggest_document") and "title" in (result or {}):
            desc = f"{tool}: {result.get('title', '')}"
    elif tool == "search_chats":
        query = content.split("\n")[0].strip()
        desc = f"search_chats: {query[:80]}"
        result = await do_search_chats(query, owner=owner)
    elif tool == "search_project_chats":
        query = content.split("\n")[0].strip()
        desc = f"search_project_chats: {query[:80]}"
        from services.projects import project_for_session
        project = project_for_session(session_id or "", owner)
        if not project:
            result = {"error": "This chat is not attached to a project", "exit_code": 1}
        else:
            result = await do_search_chats(
                query, owner=owner, folder=project.get("folder") or ""
            )
    elif tool == "project_context":
        desc = "project_context"
        from services.projects import ProjectError, get_store, project_for_session
        project = project_for_session(session_id or "", owner)
        if not project:
            result = {"error": "This chat is not attached to a project", "exit_code": 1}
        else:
            try:
                args = json.loads(content or "{}")
                if not isinstance(args, dict):
                    raise ProjectError("Project context arguments must be an object")
                action = str(args.get("action") or "list").strip().lower()
                store = get_store()
                if action == "list":
                    result = store.list_context_path(
                        project,
                        str(args.get("item_id") or ""),
                        str(args.get("path") or ""),
                    )
                elif action == "read":
                    result = store.read_context_file(
                        project,
                        str(args.get("item_id") or ""),
                        str(args.get("path") or ""),
                        int(args.get("start_line") or 1),
                        int(args.get("line_count") or 200),
                    )
                elif action == "search":
                    result = store.search_context(
                        project,
                        str(args.get("query") or ""),
                        str(args.get("item_id") or ""),
                    )
                else:
                    raise ProjectError("Action must be list, read or search")
                desc = f"project_context: {action}"
            except (ProjectError, ValueError, TypeError, json.JSONDecodeError) as exc:
                result = {"error": str(exc), "exit_code": 1}
    elif tool == "manage_project_context":
        # The MUTATING half of the project's context links. Everything that
        # decides an outcome lives in ProjectContextService; this branch only
        # hands over the session (which is what resolves the project — never an
        # id from the model) and the run identity, and never raises.
        desc = "manage_project_context"
        from src.tools.project_context import do_manage_project_context
        _turn_opts = get_active_turn_options()
        result = await do_manage_project_context(
            content,
            session_id=session_id or "",
            owner=owner,
            run_id=str(_turn_opts.get("run_id") or ""),
            turn_id=str(_turn_opts.get("turn_id") or ""),
        )
        _action = (result or {}).get("action") or ""
        if _action:
            desc = f"manage_project_context: {_action}"
    elif tool == "project_objectives":
        desc = "project_objectives"
        from services.projects import project_for_session
        project = project_for_session(session_id or "", owner)
        if not project:
            result = {"error": "This chat is not attached to a project", "exit_code": 1}
        else:
            from services import objectives as objectives_svc
            try:
                args = json.loads(content or "{}")
                if not isinstance(args, dict):
                    raise objectives_svc.ObjectiveError(
                        "Project objectives arguments must be an object")
                action = str(args.get("action") or "list").strip().lower()
                if action == "list":
                    result = objectives_svc.list_payload(project)
                elif action == "apply":
                    result = objectives_svc.apply_deltas(
                        project, args.get("deltas") or [], "agent",
                        session_id=session_id,
                    )
                else:
                    raise objectives_svc.ObjectiveError("Action must be list or apply")
                desc = f"project_objectives: {action}"
            except (objectives_svc.ObjectiveError, ValueError, TypeError,
                    json.JSONDecodeError) as exc:
                result = {"error": str(exc), "exit_code": 1}
    elif tool in ("manage_teach_mode", "capability_health", "branch_futures"):
        from src.tools.capability_systems import (
            do_branch_futures, do_capability_health, do_manage_teach_mode,
        )
        handler = {"manage_teach_mode": do_manage_teach_mode,
                   "capability_health": do_capability_health,
                   "branch_futures": do_branch_futures}[tool]
        desc = tool
        result = await handler(content, session_id=session_id or "", owner=str(owner or ""))
        action = str((result or {}).get("action") or "")
        if action:
            desc = f"{tool}: {action}"
    elif tool == "memory_rules":
        desc = "memory_rules"
        from src import memory_engine as _engine
        # Scope: the session's project folder when it has one, "" otherwise.
        # A rule learned outside a project is a rule everywhere, which is
        # exactly how scoped_items() reads an empty project.
        _project = ""
        try:
            from services.projects import project_for_session
            _project = str((project_for_session(session_id or "", owner) or {}).get("workspace") or "")
        except Exception:  # noqa: BLE001 - no project is not an error here
            _project = ""

        def _row(item):
            pub = _engine.public_item(item)
            return {"id": pub["id8"], "full_id": pub["id"], "text": pub["text"],
                    "level": pub["level"], "status": pub["status"],
                    "maturity": pub["maturity"], "trust_class": pub["trust_class"],
                    "score": pub["effective_score"], "harmful_ratio": pub["harmful_ratio"]}

        try:
            args = json.loads(content or "{}")
            if not isinstance(args, dict):
                raise _engine.MemoryEngineError("memory_rules arguments must be an object")
            action = str(args.get("action") or "list").strip().lower()
            limit = max(1, min(50, int(args.get("limit") or 10)))
            if action == "add":
                item = _engine.add_item(
                    args.get("text"),
                    owner=str(owner or ""),
                    project=_project,
                    level=args.get("level") or "procedural",
                    category=args.get("category") or "",
                    trust_class="agent_assertion",
                    evidence=[{"kind": "chat", "session_id": session_id or "",
                               "excerpt": str(args.get("text") or "")[:200]}],
                )
                result = {"added": _row(item),
                          "note": "stored as agent_assertion (trust 0.50) — it earns trust "
                                  "from what happens on the turns it is used in"}
            elif action == "search":
                hits = _engine.search(args.get("query") or "", str(owner or ""),
                                      _project, k=limit)
                result = {"results": [_row(h) for h in hits],
                          "degraded": bool(hits and hits[0].get("degraded"))}
            elif action == "feedback":
                full_id = _engine.resolve_id(args.get("id"))
                if not full_id:
                    raise _engine.MemoryEngineError(
                        f"no memory item matches id '{args.get('id')}'")
                item = _engine.add_feedback(full_id, args.get("kind"),
                                            reason=str(args.get("reason") or ""),
                                            ref=str(session_id or "agent"))
                if not item:
                    raise _engine.MemoryEngineError(f"no memory item matches id '{full_id}'")
                result = {"updated": _row(item)}
            elif action == "list":
                items = _engine.scoped_items(str(owner or ""), _project,
                                             ("active", "anti_pattern"))
                if args.get("level"):
                    items = [i for i in items
                             if i.get("level") == str(args.get("level")).strip().lower()]
                rows = sorted((_row(i) for i in items),
                              key=lambda r: (-r["score"], r["full_id"]))
                result = {"items": rows[:limit], "total": len(rows)}
            else:
                raise _engine.MemoryEngineError(
                    "Action must be add, search, feedback or list")
            desc = f"memory_rules: {action}"
        except (_engine.MemoryEngineError, ValueError, TypeError,
                json.JSONDecodeError) as exc:
            result = {"error": str(exc), "exit_code": 1}
    elif tool == "expert_review":
        desc = "expert_review"
        from src import expert_review as _review
        try:
            args = json.loads(content or "{}")
            if not isinstance(args, dict):
                raise _review.ExpertReviewError(
                    "expert_review arguments must be an object")
            action = str(args.get("action") or "review").strip().lower()
            result = await _expert_review_action(action, args, session_id, owner)
            desc = f"expert_review: {action}"
        except (_review.ExpertReviewError, ValueError, TypeError,
                json.JSONDecodeError) as exc:
            result = {"error": str(exc), "exit_code": 1}
    elif tool == "verify_claim":
        desc = "verify_claim"
        try:
            result = _verify_claim_action(content)
            desc = f"verify_claim: layer {result.get('layer')}" if "layer" in result else desc
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            result = {"error": str(exc), "exit_code": 1}
    elif tool in ("chat_with_model", "ask_teacher", "list_models"):
        # Migrated to the agent_tools registry (#3629): dispatched through
        # TOOL_HANDLERS with the owner/session ctx these tools need, instead
        # of the legacy dispatch_ai_tool elif. The impls live in
        # src/agent_tools/model_interaction_tools.py.
        first_line = content.split(chr(10))[0].strip()[:60]
        desc = f"{tool}: {first_line}" if first_line else tool
        result = await _document_tool_dispatch(tool, content, session_id, owner) \
            or {"error": f"{tool}: execution failed", "exit_code": 1}
    elif tool in ("create_session", "list_sessions", "send_to_session", "manage_session"):
        # Migrated to the agent_tools registry (#3629): dispatched through
        # TOOL_HANDLERS with the owner/session ctx these tools need. The impls
        # live in src/agent_tools/session_tools.py.
        first_line = content.split(chr(10))[0].strip()[:60]
        desc = f"{tool}: {first_line}" if first_line else tool
        result = await _document_tool_dispatch(tool, content, session_id, owner) \
            or {"error": f"{tool}: execution failed", "exit_code": 1}
    elif tool in ("pipeline", "manage_memory", "ui_control"):
        from src.ai_interaction import dispatch_ai_tool
        desc, result = await dispatch_ai_tool(tool, content, session_id, owner=owner)
    elif tool == "manage_tasks":
        desc = "manage_tasks"
        result = await do_manage_tasks(content, owner=owner)
    elif tool == "manage_skills":
        desc = "manage_skills"
        result = await do_manage_skills(content, owner=owner)
    elif tool == "api_call":
        first_line = content.split("\n")[0].strip()[:60]
        desc = f"api_call: {first_line}"
        result = await do_api_call(content)
    elif tool in ("manage_endpoints", "manage_mcp", "manage_webhooks", "manage_tokens", "manage_settings"):
        # Registry-dispatched (agent_tools.admin_tools); owner threaded for ownership/admin checks.
        desc = tool
        result = await _direct_fallback(tool, content, owner=owner) \
            or {"error": f"{tool}: execution failed", "exit_code": 1}
    elif tool == "manage_notes":
        desc = "manage_notes"
        result = await do_manage_notes(content, owner=owner)
    elif tool == "manage_calendar":
        desc = "manage_calendar"
        result = await do_manage_calendar(content, owner=owner)
    elif tool == "download_model":
        desc = "download_model"
        result = await do_download_model(content, owner=owner)
    elif tool == "serve_model":
        desc = "serve_model"
        result = await do_serve_model(content, owner=owner)
    elif tool == "list_served_models":
        desc = "list_served_models"
        result = await do_list_served_models(content, owner=owner)
    elif tool == "stop_served_model":
        desc = "stop_served_model"
        result = await do_stop_served_model(content, owner=owner)
    elif tool == "tail_serve_output":
        desc = "tail_serve_output"
        result = await do_tail_serve_output(content, owner=owner)
    elif tool == "list_downloads":
        desc = "list_downloads"
        result = await do_list_downloads(content, owner=owner)
    elif tool == "cancel_download":
        desc = "cancel_download"
        result = await do_cancel_download(content, owner=owner)
    elif tool == "search_hf_models":
        desc = "search_hf_models"
        result = await do_search_hf_models(content, owner=owner)
    elif tool == "list_cached_models":
        desc = "list_cached_models"
        result = await do_list_cached_models(content, owner=owner)
    elif tool == "app_api":
        desc = "app_api"
        result = await do_app_api(content, owner=owner)
    elif tool == "list_serve_presets":
        desc = "list_serve_presets"
        result = await do_list_serve_presets(content, owner=owner)
    elif tool == "serve_preset":
        desc = "serve_preset"
        result = await do_serve_preset(content, owner=owner)
    elif tool == "adopt_served_model":
        desc = "adopt_served_model"
        result = await do_adopt_served_model(content, owner=owner)
    elif tool == "list_cookbook_servers":
        desc = "list_cookbook_servers"
        result = await do_list_cookbook_servers(content, owner=owner)
    elif tool == "edit_image":
        desc = "edit_image"
        result = await do_edit_image(content, owner=owner)
    elif tool == "edit_file":
        result = await _direct_fallback(tool, content) or {"error": "edit failed", "exit_code": 1}
        desc = result.get("output") or result.get("error") or "edit_file"
    elif tool == "trigger_research":
        desc = "trigger_research"
        result = await do_trigger_research(content, owner=owner)
    elif tool == "manage_research":
        desc = "manage_research"
        result = await do_manage_research(content, owner=owner)
    elif tool == "resolve_contact":
        desc = "resolve_contact"
        result = await do_resolve_contact(content, owner=owner)
    elif tool == "manage_contact":
        desc = "manage_contact"
        result = await do_manage_contact(content, owner=owner)
    elif tool == "vault_search":
        desc = "vault_search"
        result = await do_vault_search(content, owner=owner)
    elif tool == "vault_get":
        desc = "vault_get"
        result = await do_vault_get(content, owner=owner)
    elif tool == "vault_unlock":
        desc = "vault_unlock"
        result = await do_vault_unlock(content, owner=owner)
    elif tool in BUILTIN_EMAIL_TOOLS:
        # Bare email tool name from fenced-block models (e.g. Ollama) — route to MCP email server.
        # Non-admin owners never reach here: BUILTIN_EMAIL_TOOLS ⊆ NON_ADMIN_BLOCKED_TOOLS,
        # so is_public_blocked_tool() above already rejected them.
        mcp = get_mcp_manager()
        qualified = f"mcp__email__{tool}"
        desc = f"email: {tool}"
        if mcp:
            _raw = content.strip()
            args = {}
            _args_error = None
            if _raw:
                # A non-empty body is always meant to be the call's arguments,
                # and every email tool takes a JSON object. Anything that
                # isn't one is a correctable error — NOT a silent empty-args
                # call, which would read the DEFAULT mailbox/folder instead of
                # the one the model meant (#3966 class). Only an EMPTY body
                # keeps the no-arg path (e.g. ```list_email_accounts```).
                try:
                    parsed = json.loads(_raw)
                except (json.JSONDecodeError, TypeError) as _je:
                    # Covers both `{account: "work"}` (looks like JSON, bad)
                    # and `account: work` (not JSON at all).
                    _args_error = (
                        f"'{tool}' arguments are not valid JSON ({_je}). "
                        'Send a JSON object, e.g. {"account": "work"} — '
                        "keys and string values need double quotes."
                    )
                else:
                    if isinstance(parsed, dict):
                        args = parsed
                    else:
                        _args_error = (
                            f"'{tool}' arguments must be a JSON object, "
                            'e.g. {"uid": "..."} — got a JSON array/value instead.'
                        )
            if _args_error is not None:
                result = {"error": _args_error, "exit_code": 1}
            else:
                if owner:
                    args = dict(args)
                    args[_EMAIL_MCP_OWNER_ARG] = owner
                result = await mcp.call_tool(qualified, args)
        else:
            result = {"error": "MCP manager not available", "exit_code": 1}
    elif tool.startswith("mcp__"):
        # MCP tool dispatch
        mcp = get_mcp_manager()
        if mcp:
            desc = f"mcp: {tool}"
            args, parse_error = _parse_qualified_mcp_args(tool, content)
            if parse_error:
                result = {"error": parse_error, "exit_code": 1}
            else:
                if tool.startswith("mcp__email__") and owner:
                    args = dict(args)
                    args[_EMAIL_MCP_OWNER_ARG] = owner
                if is_browser_action(tool):
                    # Ola A wiring: route this task's browser_* calls through
                    # its own isolated MCP connection (WEB-03) instead of the
                    # one shared BROWSER_SERVER_ID — classification above
                    # (`is_browser_action`) is still keyed to the model-facing
                    # canonical tool name, never the rewritten one.
                    effective_tool = tool
                    session_server_id = await _ensure_session_browser(mcp, owner, session_id)
                    if session_server_id:
                        effective_tool = f"mcp__{session_server_id}__{tool[len(BROWSER_MCP_PREFIX):]}"
                    result = await _run_browser_action_with_precondition(mcp, effective_tool, args)
                else:
                    result = await mcp.call_tool(tool, args)
        else:
            desc = f"mcp: {tool}"
            result = {"error": "MCP manager not available", "exit_code": 1}


    elif tool in _GIT_TOOL_NAMES:
        # Lote 87: needs owner (agent git policy is owner+repo scoped) and
        # human_approved (whether this exact call was already sealed and
        # approved) — the generic dynamic_handlers branch below passes
        # neither, so these get their own branch, same shape as
        # manage_bg_jobs's above.
        first_line = content.split(chr(10))[0][:80]
        desc = f"{tool}: {first_line}" if first_line else tool
        result = await _direct_fallback(
            tool, content, session_id=session_id, owner=owner, human_approved=human_approved,
        ) or {"error": f"{tool}: execution failed", "exit_code": 1}
    elif tool in dynamic_handlers:
        first_line = content.split(chr(10))[0][:80]
        desc = f"registry: {tool} {first_line}".strip()
        res = await _direct_fallback(tool, content, progress_cb=progress_cb)

        if isinstance(res, tuple):
            desc, result = res
        else:
            result = res or {"error": f"{tool}: execution failed", "exit_code": 1}

    else:
        desc = f"unknown: {tool}"
        result = {
            "error": f"Unknown tool: {tool}",
            "exit_code": 1
        }

    logger.info(f"Tool executed: {desc} -> exit_code={result.get('exit_code', 'n/a')}")
    return desc, result


# ---------------------------------------------------------------------------
# Result formatting
# ---------------------------------------------------------------------------

# Keys handled by the dedicated branches below — never echo them as raw JSON.
_FORMATTER_HANDLED_KEYS = {
    "stdout", "stderr", "exit_code", "content", "size",
    "response", "results", "session_id", "name", "model", "session_name",
    "success", "path", "action", "title", "doc_id", "version", "applied",
    "error", "output",
    # Image payloads (MCP screenshots, desktop_screenshot) go to the model as
    # an image block from `_append_tool_results` and to the UI as a data URL;
    # echoing them here put ~8 KB of base64 per screenshot into the text the
    # model reads and told it nothing (FAUSTUS).
    "images", "screenshot",
}


def format_tool_result(description: str, result: Dict) -> str:
    """Format a tool result into text for feeding back to the LLM."""
    parts = [f"### {description}"]
    _image_count = 0
    try:
        from src.tool_images import normalize_result_images
        _image_count = len(normalize_result_images(result))
    except Exception:  # noqa: BLE001 - formatting must never fail on images
        _image_count = 0

    if "stdout" in result:
        if result["stdout"]:
            parts.append(f"**stdout:**\n```\n{result['stdout']}\n```")
        if result["stderr"]:
            parts.append(f"**stderr:**\n```\n{result['stderr']}\n```")
        parts.append(f"**exit_code:** {result.get('exit_code', 'unknown')}")
    elif "output" in result:
        # bash / python canonical result shape: {"output": ..., "exit_code": ...}
        parts.append(f"```\n{result['output']}\n```")
        if result.get("exit_code") not in (0, None):
            parts.append(f"**exit_code:** {result['exit_code']}")
    elif "content" in result:
        parts.append(f"**content ({result.get('size', '?')} chars):**\n```\n{result['content']}\n```")
    elif "response" in result:
        model = result.get("model", result.get("session_name", ""))
        if model:
            parts.append(f"**{model} responded:**\n{result['response']}")
        else:
            parts.append(result["response"])
    elif "results" in result:
        parts.append(result["results"])
    elif "session_id" in result and "name" in result:
        parts.append(f"Session created: **{result['name']}** (id: `{result['session_id']}`, model: {result.get('model', 'unknown')})")
    elif "success" in result:
        if result["success"]:
            parts.append(f"File written: {result['path']} ({result['size']} bytes)")
        else:
            parts.append(f"Error: {result.get('error', 'unknown')}")
    elif "action" in result:
        action = result["action"]
        if action == "create":
            parts.append(f"Document created: \"{result.get('title', '')}\" (id: {result['doc_id']}, v{result['version']})")
        elif action == "update":
            parts.append(f"Document updated: \"{result.get('title', '')}\" (v{result['version']})")
        elif action == "edit":
            parts.append(f'Document edited: "{result.get("title", "")}" (v{result.get("version", "?")}, {result.get("applied", 0)} edit(s) applied)')
    elif "error" in result:
        parts.append(f"**Error:** {result['error']}")

    if _image_count:
        parts.append(
            f"**images:** {_image_count} image(s) attached — shown to you as a "
            "separate image message right after this result."
        )

    # Surface any additional structured payload (events, tasks, notes, calendars,
    # documents, attachments, etc.) that the dedicated branches above don't show.
    # Without this, tools that return {"response": "...", "events": [...]} would
    # silently drop the events list and the model would only see the summary line.
    extra = {k: v for k, v in result.items() if k not in _FORMATTER_HANDLED_KEYS}
    if extra:
        try:
            extra_json = json.dumps(extra, indent=2, default=str, ensure_ascii=False)
            # Cap to avoid blowing the context window on huge payloads.
            if len(extra_json) > 8000:
                extra_json = extra_json[:8000] + f"\n... (truncated, {len(extra_json)} chars total)"
            parts.append(f"**data:**\n```json\n{extra_json}\n```")
        except (TypeError, ValueError):
            pass

    return "\n".join(parts)


# =============================================================================
# EXEC-05 — install dependencies with control
# =============================================================================
#
# ``docs/spec/v2/backlog.json`` (EXEC-05): a package manager/lockfile must be
# detected before anything runs; a plan (what would change, why, which files)
# needs explicit approval before it touches disk; the same UNCHANGED plan
# must not ask for approval twice; and installation is always scoped to the
# project -- never the system interpreter, never `npm install -g`. Nothing in
# the repo does any of this today (`docs/spec/v2/MAPA_REUTILIZACION.md` has no
# row for it because P1 requirements were out of that audit's scope; a repo
# grep for "pip install"/"npm install" inside `src/`/`services/` at this
# lote's start returned nothing).
#
# Every function below is plain data in, plain data out, with the actual
# subprocess call behind an INJECTABLE `runner` (default: a real
# `asyncio.create_subprocess_exec`) -- the same shape
# `src/browser_actions.run_with_precondition` uses for its snapshot/act
# split, so a test drives the whole plan -> approve -> execute flow with a
# fake runner and no real pip/npm/network.

_PACKAGE_MANAGER_LOCKFILES: Dict[str, str] = {
    "yarn": "yarn.lock",
    "pnpm": "pnpm-lock.yaml",
    "npm": "package-lock.json",
    "pip": "requirements.txt",
}

# Syntax checks only -- these exist to reject shell metacharacters, URLs and
# local paths a scraped/quoted "install this" suggestion might carry, not to
# vouch for a package's trustworthiness (that is what the approval step is
# for). A `==version`/`@version` pin is allowed since it is the normal way to
# ask for a specific release.
_PIP_PACKAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,213}(==[A-Za-z0-9][A-Za-z0-9._-]*)?$")
_NPM_PACKAGE_RE = re.compile(r"^(@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]{0,213}(@[A-Za-z0-9._^~<>=-]+)?$")
_DISALLOWED_SPEC_CHARS = frozenset(";|&$`\n\r<> \t")


def detect_package_manager(project_root: str) -> Optional[str]:
    """Which manager owns `project_root`, told apart by its LOCKFILE (npm,
    yarn and pnpm all share ``package.json``, so the manifest alone cannot
    distinguish them). Falls back to the bare manifest (no lockfile
    committed yet) before giving up."""
    for manager in ("yarn", "pnpm", "npm"):
        if os.path.isfile(os.path.join(project_root, _PACKAGE_MANAGER_LOCKFILES[manager])):
            return manager
    if os.path.isfile(os.path.join(project_root, "package.json")):
        return "npm"
    if os.path.isfile(os.path.join(project_root, "requirements.txt")) or \
            os.path.isfile(os.path.join(project_root, "pyproject.toml")):
        return "pip"
    return None


def validate_package_name(manager: str, name: str) -> Optional[str]:
    """``None`` when `name` is a well-formed package spec for `manager`;
    otherwise the reason it is refused. EXEC-05's acceptance criterion:
    "a package suggested by untrusted text is not installed without
    validating its origin and permission" -- this is the origin-validation
    half (well-formed registry name, not a URL/path/shell fragment); the
    permission half is `plan_already_approved`/`execute_dependency_install`
    below."""
    spec = (name or "").strip()
    if not spec:
        return "empty package name"
    if any(ch in spec for ch in _DISALLOWED_SPEC_CHARS):
        return f"{name!r} contains characters not allowed in a package spec"
    if spec.startswith(("http://", "https://", "git+", "git@", "file:", "/", "./", "../")):
        return f"{name!r} looks like a URL or local path, not a registry package name"
    if manager == "pip":
        if not _PIP_PACKAGE_RE.match(spec):
            return f"{name!r} is not a well-formed pip package spec"
    elif manager in ("npm", "yarn", "pnpm"):
        if not _NPM_PACKAGE_RE.match(spec):
            return f"{name!r} is not a well-formed npm package spec"
    else:
        return f"unknown package manager {manager!r}"
    return None


@dataclass(frozen=True)
class DependencyInstallPlan:
    """What EXEC-05's frontend must show before anything runs: package,
    origin (manager), and which file changes (the lockfile) -- never
    executed directly, only through `execute_dependency_install`."""

    manager: str
    project_root: str
    packages: Tuple[str, ...]
    lockfile: str
    plan_hash: str

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "manager": self.manager,
            "project_root": self.project_root,
            "packages": list(self.packages),
            "lockfile": self.lockfile,
            "plan_hash": self.plan_hash,
            "scope": "project (never installed globally)",
        }


def plan_dependency_install(project_root: str, packages: Sequence[str]) -> DependencyInstallPlan:
    """Detect the manager, validate every package name, and hash the result
    -- raises :class:`ValueError` (never runs anything) when the project has
    no recognizable manager or a package name fails validation."""
    manager = detect_package_manager(project_root)
    if not manager:
        raise ValueError(f"no recognizable package manager under {project_root!r} (no lockfile or manifest found)")
    cleaned: List[str] = []
    for package in packages:
        error = validate_package_name(manager, package)
        if error:
            raise ValueError(f"refusing to plan install: {error}")
        cleaned.append(str(package).strip())
    if not cleaned:
        raise ValueError("no packages given")
    # Order-independent: the same set of packages is the same plan regardless
    # of the order they were requested in, so re-requesting it in a different
    # order still hits the "already approved" cache below.
    plan_hash = hashlib.sha256(f"{manager}:{sorted(set(cleaned))}".encode("utf-8")).hexdigest()[:16]
    return DependencyInstallPlan(
        manager=manager,
        project_root=project_root,
        packages=tuple(cleaned),
        lockfile=_PACKAGE_MANAGER_LOCKFILES[manager],
        plan_hash=plan_hash,
    )


_INSTALL_APPROVAL_LOCK = threading.Lock()
_APPROVED_INSTALL_PLANS: Dict[str, set] = {}


def plan_already_approved(owner: str, plan_hash: str) -> bool:
    with _INSTALL_APPROVAL_LOCK:
        return plan_hash in _APPROVED_INSTALL_PLANS.get(owner or "", set())


def record_plan_approval(owner: str, plan_hash: str) -> None:
    """EXEC-05 frontend requirement: "no pedir aprobación repetida para el
    mismo plan inalterado" -- an owner who approved THIS exact plan_hash
    once does not have to approve it again; a plan that changes (a package
    added/removed) hashes differently and asks again."""
    with _INSTALL_APPROVAL_LOCK:
        _APPROVED_INSTALL_PLANS.setdefault(owner or "", set()).add(plan_hash)


def reset_install_approvals() -> None:
    """Test hook."""
    with _INSTALL_APPROVAL_LOCK:
        _APPROVED_INSTALL_PLANS.clear()


@dataclass(frozen=True)
class CommandOutcome:
    """The result of running one argv (a dependency install, or an
    EXEC-06 saved-script/remote run) -- shared shape so a caller formats
    either the same way."""

    ok: bool
    command: Tuple[str, ...]
    stdout: str
    stderr: str
    returncode: int

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "command": list(self.command),
            "stdout": self.stdout,
            "stderr": self.stderr,
            "returncode": self.returncode,
        }


RunnerFn = Callable[[Tuple[str, ...], str], Awaitable[Tuple[int, str, str]]]


async def _default_runner(command: Tuple[str, ...], cwd: str) -> Tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *command, cwd=cwd or None,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout_b, stderr_b = await proc.communicate()
    return proc.returncode or 0, stdout_b.decode("utf-8", "replace"), stderr_b.decode("utf-8", "replace")


def _install_command(plan: DependencyInstallPlan) -> Tuple[str, ...]:
    """Always scoped to `plan.project_root` -- EXEC-05: "preferir entorno
    aislado" / "no modificar globalmente el sistema por defecto". `pip`
    prefers an active virtualenv (`VIRTUAL_ENV`) and otherwise installs into
    a project-local directory rather than ever falling back to a bare
    `pip install` against the system interpreter."""
    if plan.manager == "pip":
        venv = os.environ.get("VIRTUAL_ENV")
        if venv:
            pip_bin = os.path.join(venv, "Scripts", "pip.exe") if os.name == "nt" else os.path.join(venv, "bin", "pip")
            return (pip_bin, "install", *plan.packages)
        target = os.path.join(plan.project_root, ".faustus-deps")
        return (sys.executable, "-m", "pip", "install", "--target", target, *plan.packages)
    if plan.manager == "npm":
        return ("npm", "install", "--prefix", plan.project_root, *plan.packages)
    if plan.manager == "yarn":
        return ("yarn", "--cwd", plan.project_root, "add", *plan.packages)
    if plan.manager == "pnpm":
        return ("pnpm", "--dir", plan.project_root, "add", *plan.packages)
    raise ValueError(f"unknown package manager {plan.manager!r}")


async def execute_dependency_install(
    plan: DependencyInstallPlan,
    *,
    approved: bool = False,
    owner: str = "",
    runner: RunnerFn = _default_runner,
) -> CommandOutcome:
    """Run `plan` -- refuses with :class:`PermissionError` (never runs
    anything) unless `approved` is True or an identical plan was already
    approved for `owner`."""
    if not approved and not plan_already_approved(owner, plan.plan_hash):
        raise PermissionError(f"dependency install plan {plan.plan_hash} was not approved")
    record_plan_approval(owner, plan.plan_hash)
    command = _install_command(plan)
    returncode, stdout, stderr = await runner(command, plan.project_root)
    return CommandOutcome(ok=(returncode == 0), command=command, stdout=stdout, stderr=stderr, returncode=returncode)


# =============================================================================
# EXEC-06 — reusable scripts and remote (SSH) execution
# =============================================================================
#
# ``docs/spec/v2/backlog.json`` (EXEC-06): a command saved as a script is
# VERSIONED (saving under an existing name bumps the version, never silently
# overwrites it) and its inputs are TYPED (declared parameter names, missing
# or undeclared ones refused before anything runs); SSH execution requires a
# fingerprint that was already paired for that exact alias -- a similar
# hostname must not inherit another target's trust, and a recipe must not
# silently change which machine it runs on.

_SCRIPTS_DIRNAME = "exec06-scripts"
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_-]+")


def _scripts_dir() -> str:
    return os.path.join(DATA_DIR, _SCRIPTS_DIRNAME)


def _script_path(name: str) -> str:
    safe = _SAFE_NAME_RE.sub("_", (name or "").strip()) or "script"
    return os.path.join(_scripts_dir(), f"{safe}.json")


@dataclass(frozen=True)
class SavedScript:
    """A versioned, reusable recipe: `command_template` names its
    placeholders (``{path}``) and `params` is the closed list of names it is
    allowed to reference -- a template cannot use a parameter it did not
    declare, and a caller cannot skip one it did."""

    name: str
    command_template: str
    params: Tuple[str, ...]
    version: int

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "command_template": self.command_template,
            "params": list(self.params),
            "version": self.version,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "SavedScript":
        return cls(
            name=str(data.get("name") or ""),
            command_template=str(data.get("command_template") or ""),
            params=tuple(data.get("params") or ()),
            version=int(data.get("version") or 1),
        )


def save_script(name: str, command_template: str, params: Sequence[str]) -> SavedScript:
    """Save (or version-bump) a script under `name`. A second save under the
    same name never overwrites the first version in place -- `version`
    increments, so a recipe's history is never lost (rule 3: no capability
    lost)."""
    if not (name or "").strip():
        raise ValueError("script name is required")
    if not (command_template or "").strip():
        raise ValueError("command_template is required")
    path = _script_path(name)
    version = 1
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as handle:
                version = int(json.load(handle).get("version", 0)) + 1
        except (OSError, ValueError, TypeError):
            version = 1
    script = SavedScript(name=name.strip(), command_template=command_template, params=tuple(params or ()), version=version)
    os.makedirs(_scripts_dir(), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(script.to_mapping(), handle)
    os.replace(tmp, path)
    return script


def load_script(name: str) -> Optional[SavedScript]:
    path = _script_path(name)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            return SavedScript.from_mapping(json.load(handle))
    except (OSError, ValueError, TypeError):
        return None


_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def render_script_command(script: SavedScript, values: Mapping[str, str]) -> List[str]:
    """Render `script.command_template` into an ARGV LIST (never a shell
    string) with `values` substituted per placeholder.

    Rendering into a list rather than a shell string is what makes this safe
    regardless of what characters a value contains -- there is no shell to
    inject into. Refuses (rather than silently ignoring) a missing
    declared parameter or a template placeholder that was never declared in
    `script.params`, so a script's typed-input contract cannot be bypassed
    by a caller or drift silently when the template is edited.
    """
    missing = [p for p in script.params if p not in values]
    if missing:
        raise ValueError(f"missing required parameter(s): {', '.join(missing)}")
    used_in_template = set(_PLACEHOLDER_RE.findall(script.command_template))
    undeclared = used_in_template - set(script.params)
    if undeclared:
        raise ValueError(f"template references undeclared parameter(s): {', '.join(sorted(undeclared))}")
    tokens = shlex.split(script.command_template)
    rendered: List[str] = []
    for token in tokens:
        full_match = _PLACEHOLDER_RE.fullmatch(token)
        if full_match:
            rendered.append(str(values[full_match.group(1)]))
        else:
            rendered.append(_PLACEHOLDER_RE.sub(lambda m: str(values[m.group(1)]), token))
    return rendered


async def run_saved_script(
    script: SavedScript,
    values: Mapping[str, str],
    *,
    cwd: str = ".",
    runner: RunnerFn = _default_runner,
) -> CommandOutcome:
    """Render and run `script` locally -- output is a plain
    :class:`CommandOutcome` (returncode/stdout/stderr), EXEC-06's own
    "salida verificable"."""
    argv = tuple(render_script_command(script, values))
    returncode, stdout, stderr = await runner(argv, cwd)
    return CommandOutcome(ok=(returncode == 0), command=argv, stdout=stdout, stderr=stderr, returncode=returncode)


# -- SSH pairing --------------------------------------------------------


@dataclass(frozen=True)
class SSHTarget:
    alias: str
    host: str
    port: int
    fingerprint: str

    def to_mapping(self) -> Dict[str, Any]:
        return {"alias": self.alias, "host": self.host, "port": self.port, "fingerprint": self.fingerprint}


_SSH_REGISTRY_LOCK = threading.Lock()


def _ssh_registry_path() -> str:
    return os.path.join(DATA_DIR, "exec06-ssh-targets.json")


def _load_ssh_registry() -> Dict[str, Dict[str, Any]]:
    path = _ssh_registry_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _save_ssh_registry(registry: Dict[str, Dict[str, Any]]) -> None:
    path = _ssh_registry_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(registry, handle)
    os.replace(tmp, path)


def pair_ssh_target(alias: str, host: str, fingerprint: str, *, port: int = 22) -> SSHTarget:
    """Register `alias` -> (`host`, `fingerprint`) -- "SSH ya emparejado"
    per this lote's own scope: this function records a pairing an operator
    already vetted out-of-band, it does not perform key discovery/TOFU
    itself."""
    if not (alias or "").strip():
        raise ValueError("alias is required")
    if not (host or "").strip():
        raise ValueError("host is required")
    if not (fingerprint or "").strip():
        raise ValueError("fingerprint is required")
    with _SSH_REGISTRY_LOCK:
        registry = _load_ssh_registry()
        registry[alias.strip()] = {"host": host.strip(), "port": int(port), "fingerprint": fingerprint.strip()}
        _save_ssh_registry(registry)
    return SSHTarget(alias=alias.strip(), host=host.strip(), port=int(port), fingerprint=fingerprint.strip())


def resolve_ssh_target(alias: str) -> Optional[SSHTarget]:
    with _SSH_REGISTRY_LOCK:
        entry = _load_ssh_registry().get((alias or "").strip())
    if not entry:
        return None
    return SSHTarget(alias=alias.strip(), host=entry["host"], port=int(entry.get("port", 22)), fingerprint=entry["fingerprint"])


def reset_ssh_registry() -> None:
    """Test hook."""
    with _SSH_REGISTRY_LOCK:
        _save_ssh_registry({})


def verify_ssh_target(alias: str, *, host: str, fingerprint: str) -> Optional[str]:
    """``None`` when (`host`, `fingerprint`) matches the PAIRED target for
    `alias` exactly; otherwise the reason the run is refused.

    Both comparisons are exact equality, never a prefix/substring match --
    EXEC-06's acceptance criterion is precisely that a similar hostname
    (``prod-db`` vs ``prod-db.evil.example``) must not inherit another
    alias's trust, and that a recipe cannot silently start running against a
    different machine just because someone repointed the alias without
    re-pairing.
    """
    target = resolve_ssh_target(alias)
    if target is None:
        return f"no SSH target is paired under alias {alias!r} -- pair it before running against it"
    if target.host != host:
        return (
            f"alias {alias!r} is paired to host {target.host!r}, not {host!r} -- "
            "a similar hostname does not inherit that pairing's trust"
        )
    if target.fingerprint != fingerprint:
        return (
            f"host key fingerprint for {alias!r} does not match the paired one "
            f"(expected {target.fingerprint[:16]}..., got {fingerprint[:16]}...) -- refusing: "
            "a recipe cannot silently change which machine it runs on"
        )
    return None


async def run_remote_script(
    script: SavedScript,
    values: Mapping[str, str],
    *,
    alias: str,
    presented_host: str,
    presented_fingerprint: str,
    runner: RunnerFn = _default_runner,
) -> CommandOutcome:
    """Verify the SSH pairing, then run `script` on the paired host.

    Secrets stay out of the prompt: this builds an `ssh` argv relying on the
    caller's existing key-based auth (an already-paired target, per this
    lote's scope) -- no password is ever accepted as a parameter here.
    """
    reason = verify_ssh_target(alias, host=presented_host, fingerprint=presented_fingerprint)
    if reason:
        raise PermissionError(reason)
    target = resolve_ssh_target(alias)
    assert target is not None  # verify_ssh_target already confirmed this
    remote_argv = render_script_command(script, values)
    remote_command = " ".join(shlex.quote(part) for part in remote_argv)
    ssh_argv: Tuple[str, ...] = (
        "ssh", "-p", str(target.port),
        "-o", "BatchMode=yes",  # never prompt for (or accept) a password interactively
        target.host, remote_command,
    )
    returncode, stdout, stderr = await runner(ssh_argv, ".")
    return CommandOutcome(ok=(returncode == 0), command=ssh_argv, stdout=stdout, stderr=stderr, returncode=returncode)
