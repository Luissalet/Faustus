"""
tool_registry.py — a single catalogue over every tool the agent can offer
(TOOL-01, TOOL-03 partial, TOOL-04 partial).

Faustus already has three authorities that each know a slice of "what is a
tool": `src.agent_tools.TOOL_TAGS` (fence-format names, including the email
and desktop unions), `src.tool_schemas.FUNCTION_TOOL_SCHEMAS` (native
function-call schemas), and `src.tool_capabilities` (effects, result
integrity, and — via `KNOWN_CAPABILITY_TOOLS` — the union both of the above
are already asserted to be subsets of, in
`tests/test_external_context_tool_gate.py`). None of the three publishes a
single versioned descriptor, so a caller who wants "everything about
`bash`" has to know all three modules exist. This module reads all of them
(plus `src.tool_index.BUILTIN_TOOL_DESCRIPTIONS` for fence tools that have
no JSON schema, `src.settings` for the one timeout that is a real live
setting, and — when handed a live manager — `src.mcp_manager` for MCP
tools) and emits one `ToolDescriptor` per tool. It reads; it never edits any
of those modules, and it never executes a tool.

`ToolDescriptor` (`src.contracts.tool`, lote 2) is used purely as an OUTPUT
shape: `ToolRegistry.snapshot()` constructs instances directly and only
ever calls `.to_mapping()`/`.fingerprint()` on them, never
`.from_mapping()`. That matters because the spec's own `name` pattern
requires a dot (`fs.apply_patch`), and every tool name in this codebase is
flat (`bash`, `web_search`, `manage_mcp` — no dots). Round-tripping a
descriptor built here through `ToolDescriptor.from_mapping()` would fail on
that pattern; nothing here does that, and a caller who wants strict spec
validation of a THIRD PARTY payload should keep using `from_mapping`
directly, not this module's descriptors.

Fields with no existing source of truth are given a documented, narrow
default rather than a guess:
  - `version`: no per-tool semver exists in the codebase (TOOL-01's own
    acceptance criterion for a versioned registry is what this module is
    starting). Every descriptor reports `DEFAULT_TOOL_VERSION` ("1.0.0")
    until a tool declares its own.
  - `idempotency`: no tool declares an idempotency mode explicitly. Read-only
    tools (effect_class == "read") are `safe_read` — repeating a read cannot
    change the world — every other tool is `not_supported` until proven
    otherwise; nothing here claims a write is safe to retry blind.
  - `timeout_ms`: `bash`/`python` read the live
    `agent_subprocess_idle_timeout_seconds` setting (the one wall-clock bound
    that actually governs them — `agent_settings_schema.py`'s own help text
    names both tools). Everything else reports `DEFAULT_TIMEOUT_MS`, a
    documented floor, not a measurement.
  - `max_output_bytes`: `src.constants.MAX_OUTPUT_CHARS`, the shared
    truncation cap already applied to bash/python/web_search/web_fetch
    output; used uniformly as the catalogue's default because no other tool
    publishes a different one.
  - `retry_policy`: the contract's own default (`max_attempts=1`, no
    backoff) — no tool in this codebase declares a retry policy.
  - `required_scopes`: reuses `tool_capabilities.ToolEffect` values as-is
    (e.g. `["write_workspace"]`) rather than inventing a second scope
    vocabulary next to the one that already gates approvals.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from src.agent_tools import TOOL_TAGS
from src.constants import MAX_OUTPUT_CHARS
from src.contracts.base import fingerprint as _fingerprint_parts
from src.contracts.tool import RetryPolicy, ToolDescriptor
from src.settings import get_setting
from src.tool_capabilities import KNOWN_CAPABILITY_TOOLS, ToolEffect, capabilities_for_tool
from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS

logger = logging.getLogger(__name__)

#: Reported until a tool declares its own version (see module docstring).
DEFAULT_TOOL_VERSION = "1.0.0"

#: Floor used for every tool without a live setting behind its timeout.
DEFAULT_TIMEOUT_MS = 30_000

#: Shared truncation cap (src.constants), used uniformly as the catalogue
#: default — see module docstring.
DEFAULT_MAX_OUTPUT_BYTES = MAX_OUTPUT_CHARS

#: name -> the `agent_settings_schema.py` key that is genuinely this tool's
#: wall-clock bound today. Both entries point at the same idle-timeout
#: setting on purpose: that setting's own help text ("A bash / python
#: command that prints nothing for this long is killed...") names both.
_LIVE_TIMEOUT_SETTINGS: Mapping[str, str] = {
    "bash": "agent_subprocess_idle_timeout_seconds",
    "python": "agent_subprocess_idle_timeout_seconds",
}

#: Tools whose running call can be killed from outside (the idle-timeout
#: watchdog, or `manage_bg_jobs` itself) but with no guarantee the process
#: unwound cleanly — `best_effort`, not `cooperative`.
_BEST_EFFORT_CANCEL_TOOLS = frozenset({"bash", "python", "manage_bg_jobs"})

#: Tools whose entire purpose is handling a credential or secret store.
#: Classified `sensitive` regardless of what their read/write effects say,
#: because "which class of thing does this touch" (contract's own
#: `effect_class` question) is a different axis from "does it read or write"
#: for exactly these few.
_SENSITIVE_TOOL_NAMES = frozenset({"manage_tokens", "vault_unlock", "vault_get", "vault_search"})

#: Tool-name prefix an MCP server's tools resolve to (matches
#: `src.mcp_manager.get_all_tools()`'s own `qualified_name` shape:
#: `mcp__<server_id>__<tool>`).
MCP_EXECUTOR_PREFIX = "mcp:"


def _native_schema_index() -> Dict[str, Tuple[str, Mapping[str, Any]]]:
    """name -> (description, input_schema) for every FUNCTION_TOOL_SCHEMAS
    entry. Built fresh (not cached at import time) so a test that monkeypatches
    FUNCTION_TOOL_SCHEMAS sees its own fixture."""
    index: Dict[str, Tuple[str, Mapping[str, Any]]] = {}
    for entry in FUNCTION_TOOL_SCHEMAS:
        fn = entry.get("function") or {}
        name = fn.get("name")
        if not name:
            continue
        index[name] = (str(fn.get("description") or ""), dict(fn.get("parameters") or {}))
    return index


def _effect_class_for(name: str, effects: frozenset) -> str:
    """Map the fine-grained `ToolEffect` set (tool_capabilities.py) onto the
    spec's six coarse `EFFECT_CLASSES`. Order matters: the most consequential
    effect a tool carries decides its class, the same way the approval gate
    already treats one destructive effect as enough to gate the whole call."""
    if name in _SENSITIVE_TOOL_NAMES:
        return "sensitive"
    if ToolEffect.DESTRUCTIVE in effects:
        return "external"
    if ToolEffect.ADMIN_CHANGE in effects:
        return "control"
    if ToolEffect.EXECUTE_CODE in effects:
        return "execute"
    if ToolEffect.EXTERNAL_SIDE_EFFECT in effects:
        return "external"
    if effects & {ToolEffect.WRITE_WORKSPACE, ToolEffect.WRITE_PRIVATE}:
        return "write"
    if effects & {ToolEffect.NETWORK_EGRESS, ToolEffect.BROKERED_NETWORK_READ}:
        return "external"
    if ToolEffect.UI_SIDE_EFFECT in effects:
        return "control"
    if ToolEffect.USER_INTERACTION in effects:
        return "control"
    return "read"


def _idempotency_for(effect_class: str) -> str:
    return "safe_read" if effect_class == "read" else "not_supported"


def _cancellation_for(name: str) -> str:
    return "best_effort" if name in _BEST_EFFORT_CANCEL_TOOLS else "not_supported"


def _timeout_ms_for(name: str) -> int:
    setting_key = _LIVE_TIMEOUT_SETTINGS.get(name)
    if not setting_key:
        return DEFAULT_TIMEOUT_MS
    try:
        seconds = int(get_setting(setting_key, DEFAULT_TIMEOUT_MS // 1000))
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_MS
    return seconds * 1000 if seconds > 0 else DEFAULT_TIMEOUT_MS


def _builtin_names() -> List[str]:
    """Every name the agent can dispatch a built-in call to: the fence union
    (TOOL_TAGS, itself already `| BUILTIN_EMAIL_TOOLS | DESKTOP_TOOLS`) plus
    any native-only schema name, deduped, sorted for a stable snapshot."""
    names = set(TOOL_TAGS) | set(_native_schema_index())
    return sorted(names)


def _descriptor_for_builtin(name: str, native_index: Mapping[str, Tuple[str, Mapping[str, Any]]]) -> ToolDescriptor:
    caps = capabilities_for_tool(name)
    effect_class = _effect_class_for(name, caps.effects)
    native_desc, input_schema = native_index.get(name, ("", {}))
    description = native_desc or BUILTIN_TOOL_DESCRIPTIONS.get(name, "")
    executor = "native" if name in native_index else "fence"
    scopes = tuple(sorted(effect.value for effect in caps.effects))
    return ToolDescriptor(
        name=name,
        version=DEFAULT_TOOL_VERSION,
        description=description,
        input_schema=input_schema,
        output_schema={},
        effect_class=effect_class,
        required_scopes=scopes,
        timeout_ms=_timeout_ms_for(name),
        cancellation=_cancellation_for(name),
        idempotency=_idempotency_for(effect_class),
        retry_policy=RetryPolicy(),
        max_output_bytes=DEFAULT_MAX_OUTPUT_BYTES,
        executor=executor,
    )


def _descriptor_for_mcp_tool(tool: Mapping[str, Any]) -> ToolDescriptor:
    """One live MCP tool, as `McpManager.get_all_tools()` already reports it.
    Effect class comes from `mcp_tool_is_readonly` (mcp_manager's own
    fail-closed classifier: a missing hint reads as a write) — reused rather
    than re-guessed here."""
    from src.mcp_manager import mcp_tool_is_readonly  # local: mcp_manager is read-only for this module

    name = str(tool.get("qualified_name") or tool.get("name") or "")
    server_id = str(tool.get("server_id") or "")
    readonly = mcp_tool_is_readonly(dict(tool))
    effect_class = "read" if readonly else "write"
    scope = "brokered_network_read" if readonly else "external_side_effect"
    return ToolDescriptor(
        name=name,
        version=DEFAULT_TOOL_VERSION,
        description=str(tool.get("description") or ""),
        input_schema=dict(tool.get("input_schema") or {}),
        output_schema={},
        effect_class=effect_class,
        required_scopes=(scope,),
        timeout_ms=DEFAULT_TIMEOUT_MS,
        cancellation="not_supported",
        idempotency=_idempotency_for(effect_class),
        retry_policy=RetryPolicy(),
        max_output_bytes=DEFAULT_MAX_OUTPUT_BYTES,
        executor=f"{MCP_EXECUTOR_PREFIX}{server_id}",
    )


#: Raw `McpManager` connection statuses this module has actually observed
#: (`_honest_status`/the four literals set directly in `mcp_manager.py`),
#: translated to the vocabulary TOOL-03's frontend column names
#: (connected/error/needs_auth/degraded). `timeout` is the one status
#: `mcp_manager.py` sets that has no first-class frontend word yet (the
#: MAPA_REUTILIZACION note: "no hay estado 'Degradada' explicito") — mapped
#: to `degraded` here rather than invented in `mcp_manager.py` itself, which
#: this module does not edit. Every raw status not listed here (a future
#: addition to mcp_manager.py) passes through unchanged rather than being
#: silently dropped.
_MCP_STATUS_LABELS: Mapping[str, str] = {
    "connected": "connected",
    "error": "error",
    "needs_auth": "needs_auth",
    "timeout": "degraded",
    "connecting": "connecting",
    "disconnected": "disconnected",
}


def mcp_status_label(raw_status: str) -> str:
    """TOOL-03's frontend vocabulary for one raw `McpManager` status string."""
    return _MCP_STATUS_LABELS.get(raw_status, raw_status)


def snapshot(owner: Optional[str] = None, mcp_manager: Optional[Any] = None) -> List[ToolDescriptor]:
    """One `ToolDescriptor` per tool the agent can currently be offered:
    every native/fence built-in, plus every tool a connected MCP server
    reports when `mcp_manager` is passed. Deterministically ordered by name.

    `owner` narrows the built-in set to what `src.tool_security
    .blocked_tools_for_owner` (the existing non-admin denylist authority —
    the same one `agent_loop.py` consults) would actually let that caller
    reach; `None` (the default) returns the full system catalogue. MCP tools
    are never owner-filtered here — MCP servers are admin-configured and
    global (`core.database.McpServer` has no owner column); the route layer
    is what gates the catalogue endpoint to admins, the same way
    `routes/mcp/mcp_routes.py` gates every neighbouring MCP route.
    """
    blocked: frozenset = frozenset()
    if owner is not None:
        from src.tool_security import blocked_tools_for_owner
        blocked = frozenset(blocked_tools_for_owner(owner))

    native_index = _native_schema_index()
    rows: List[ToolDescriptor] = [
        _descriptor_for_builtin(name, native_index)
        for name in _builtin_names()
        if name not in blocked
    ]

    if mcp_manager is not None:
        seen_names = {d.name for d in rows}
        for tool in mcp_manager.get_all_tools():
            descriptor = _descriptor_for_mcp_tool(tool)
            if descriptor.name and descriptor.name not in seen_names:
                rows.append(descriptor)
                seen_names.add(descriptor.name)

    rows.sort(key=lambda d: d.name)
    return rows


def missing_capability_coverage() -> frozenset:
    """Builtin names `snapshot()` would try to describe that have no explicit
    entry in `tool_capabilities.TOOL_CAPABILITIES` (and so would fall back to
    the fail-high "unknown" capabilities instead of a real classification).
    Empty today — `tests/test_external_context_tool_gate.py` already asserts
    both halves of the union this checks — exposed here so this module's own
    test can assert it directly rather than trusting a sibling file to keep
    doing so forever."""
    return frozenset(_builtin_names()) - KNOWN_CAPABILITY_TOOLS


def by_name(rows: Sequence[ToolDescriptor], name: str) -> Optional[ToolDescriptor]:
    for row in rows:
        if row.name == name:
            return row
    return None


def catalog_fingerprint(rows: Sequence[ToolDescriptor]) -> str:
    """A single hash over the whole catalogue's content — changes whenever any
    descriptor's fields change, or a tool is added/removed. Reuses
    `contracts.base.fingerprint`, the same length-prefixed SHA-256 every
    other contract dataclass already hashes itself with."""
    ordered = sorted(rows, key=lambda d: d.name)
    return _fingerprint_parts([(d.name, d.fingerprint()) for d in ordered])


@dataclass(frozen=True)
class ToolRegistry:
    """Thin namespace so callers write `ToolRegistry.snapshot(...)` — the
    shape the lote's own spec names — while the real logic stays as plain,
    independently testable module functions above."""

    snapshot = staticmethod(snapshot)
    by_name = staticmethod(by_name)
    catalog_fingerprint = staticmethod(catalog_fingerprint)
    mcp_status_label = staticmethod(mcp_status_label)
    missing_capability_coverage = staticmethod(missing_capability_coverage)


__all__ = [
    "ToolRegistry",
    "snapshot",
    "by_name",
    "catalog_fingerprint",
    "mcp_status_label",
    "missing_capability_coverage",
    "DEFAULT_TOOL_VERSION",
    "DEFAULT_TIMEOUT_MS",
    "DEFAULT_MAX_OUTPUT_BYTES",
]
