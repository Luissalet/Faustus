"""src/connectors.py — F1.1: the Faustus connector catalogue (Jobhunter's Hoard,
Writer's Hoard).

A *preset* is a template for an MCP stdio server plus the extra, non-MCP
metadata the Connectors screen needs to show something more useful than "MCP
server, transport stdio": the domain app's own URL, its health endpoint, and
a hint for how a human would start that app themselves.

Nothing here talks to a database or the filesystem beyond the two validation
checks the contract calls for (`{X_DIR}` is a real directory, the bridge
script file exists) — resolving a preset against a user's `values` is a pure
function, `resolve_preset_values`. `src/connector_sidecar.py` is what
persists the result next to the `McpServer` row it was substituted into.

No secrets live in this module or its output: `resolve_preset_values` returns
plain strings the caller already had (paths, URLs) — nothing here reads a
credential store.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class ConnectorPreset:
    id: str                        # "jobhunter" | "writer"
    name: str                      # "Jobhunter's Hoard" | "Writer's Hoard"
    purpose: str                   # one sentence
    capabilities: List[str]        # e.g. ["contexts", "jobs", "applications", "answers"]
    transport: str                 # "stdio"
    command: str                   # "node"
    args: List[str]                # ["{JOBHUNT_DIR}/server/mcp.js"]
    env: Dict[str, str]             # {"JOBHUNT_URL": "{APP_URL}", ...}
    app_url_default: str            # "http://127.0.0.1:5178" | "http://127.0.0.1:8766"
    health_path: str                # "/api/health"
    health_expect: Dict[str, str]   # {} | {"service": "writers-hoard-ai-bridge"}
    ui_url_default: Optional[str]   # Jobhunter: same as app url; Writer: None (8766 is an API, not a UI)
    placeholders: List[str]         # ["JOBHUNT_DIR", "APP_URL"] — every substitution variable
    launch_profile_hint: Dict[str, Any]  # documented example argv (never executed as-is)
    # Not in the contract's literal field list (F1.1): a placeholder's default
    # value/expression, when it has one — e.g. Writer's APP_URL and
    # WH_BRIDGE_TOKEN_FILE. A placeholder present here is NOT required: an
    # empty `values[placeholder]` falls back to this instead of being
    # reported as missing. `{X_DIR}` placeholders deliberately have no
    # default — there is no safe guess for where the user installed the app
    # (rule 7: no personal paths as universal defaults).
    defaults: Dict[str, str] = field(default_factory=dict)
    # Placeholders that may be declared in `values` but never make a preset
    # "unconfigured" when absent (e.g. Writer's optional WH_BRIDGE_GROUPS).
    optional_extra_env: List[str] = field(default_factory=list)


def _writer_token_file_default() -> str:
    """Windows default: ``%APPDATA%/writers-hoard/aibridge/token``.

    Expanded eagerly (`expandvars`/`expanduser`) so the value stored in the
    sidecar and handed to the child is already a real path — a raw
    ``%APPDATA%`` would mean nothing on POSIX, and `os.path.isfile` below
    needs a concrete path either way.
    """
    raw = os.path.join("%APPDATA%", "writers-hoard", "aibridge", "token")
    return os.path.expandvars(os.path.expanduser(raw))


PRESETS: Dict[str, ConnectorPreset] = {
    "jobhunter": ConnectorPreset(
        id="jobhunter",
        name="Jobhunter's Hoard",
        purpose="Track job applications, answers and context through the Jobhunter MCP bridge.",
        capabilities=["contexts", "jobs", "applications", "answers"],
        transport="stdio",
        command="node",
        args=["{JOBHUNT_DIR}/server/mcp.js"],
        env={
            "JOBHUNT_URL": "{APP_URL}",
            "JOBHUNT_TOKEN_FILE": "{JOBHUNT_DIR}/data/mcp-token",
        },
        app_url_default="http://127.0.0.1:5178",
        health_path="/api/health",
        # Empty on purpose: old Jobhunter builds have no /api/health at all,
        # and the contract says any HTTP answer (404 included) still counts
        # as the app being reachable — an empty expectation is trivially
        # satisfied by any JSON (or lack of it), which is exactly that rule.
        health_expect={},
        ui_url_default=None,  # resolved as "= app_url" (see resolve_preset_values)
        placeholders=["JOBHUNT_DIR", "APP_URL"],
        launch_profile_hint={
            "kind": "process",
            "executable": "node",
            "argv": ["server/index.js"],
            "cwd": "{JOBHUNT_DIR}",
            "readiness": {"url": "{APP_URL}/api/health", "timeout_s": 20},
        },
        defaults={"APP_URL": "http://127.0.0.1:5178"},
    ),
    "writer": ConnectorPreset(
        id="writer",
        name="Writer's Hoard",
        purpose="Draft, edit and query manuscripts through the Writer's Hoard AI bridge.",
        capabilities=["documents", "notes", "outline", "search"],
        transport="stdio",
        command="node",
        args=["{WRITER_DIR}/dist-electron/aibridge/mcpStdio.cjs"],
        env={
            "WH_BRIDGE_URL": "{APP_URL}",
            "WH_BRIDGE_TOKEN_FILE": "{TOKEN_FILE}",
        },
        app_url_default="http://127.0.0.1:8766",
        health_path="/api/health",
        health_expect={"service": "writers-hoard-ai-bridge"},
        ui_url_default=None,  # 8766 is an API, not a UI — no ui_url
        placeholders=["WRITER_DIR", "APP_URL", "TOKEN_FILE"],
        launch_profile_hint={
            "kind": "open_exe",
            "executable": "{WRITER_DIR}/release/win-unpacked/Writers Hoard.exe",
            "argv": [],
        },
        defaults={
            "APP_URL": "http://127.0.0.1:8766",
            "TOKEN_FILE": _writer_token_file_default(),
        },
        # Forwarded verbatim into `env` when the user supplies it; never
        # required, and its absence never makes the preset "unconfigured".
        optional_extra_env=["WH_BRIDGE_GROUPS"],
    ),
}


def list_presets() -> List[Dict[str, Any]]:
    """Presets as plain dicts, safe to return from a route."""
    out = []
    for preset in PRESETS.values():
        out.append({
            "id": preset.id,
            "name": preset.name,
            "purpose": preset.purpose,
            "capabilities": list(preset.capabilities),
            "transport": preset.transport,
            "command": preset.command,
            "args": list(preset.args),
            "env": dict(preset.env),
            "app_url_default": preset.app_url_default,
            "health_path": preset.health_path,
            "health_expect": dict(preset.health_expect),
            "ui_url_default": preset.ui_url_default,
            "placeholders": list(preset.placeholders),
            "defaults": dict(preset.defaults),
            "launch_profile_hint": dict(preset.launch_profile_hint),
            "optional_extra_env": list(preset.optional_extra_env),
        })
    return out


def get_preset(preset_id: str) -> Optional[ConnectorPreset]:
    return PRESETS.get(preset_id)


def _dir_placeholder(preset: ConnectorPreset) -> Optional[str]:
    for name in preset.placeholders:
        if name.endswith("_DIR"):
            return name
    return None


def _substitute(template: str, merged: Dict[str, str]) -> str:
    out = template
    for key, value in merged.items():
        out = out.replace("{" + key + "}", value)
    return out


def resolve_preset_values(preset: ConnectorPreset, values: Optional[Dict[str, str]]) -> Dict[str, Any]:
    """Resolve `preset` against user-supplied `values`.

    Returns a dict that is always one of two shapes:

    * ``{"ok": False, "missing": [...], "reasons": [...]}`` — a declared
      placeholder has neither a value nor a default, or a directory/file
      check failed (F1.1: "sólo los declarados"; unknown keys in `values`
      are ignored rather than erroring, so a stale form field never blocks
      an otherwise-valid save).
    * ``{"ok": True, "command": ..., "args": [...], "env": {...},
      "app_url": ..., "ui_url": ...|None}``.

    Never touches the network; the two filesystem checks (`os.path.isdir`,
    `os.path.isfile`) are the ones the contract calls out explicitly.
    """
    values = {k: str(v) for k, v in (values or {}).items() if v is not None and str(v).strip()}
    merged = dict(preset.defaults)
    merged.update(values)

    missing = [p for p in preset.placeholders if not merged.get(p)]
    if missing:
        return {
            "ok": False,
            "missing": missing,
            "reasons": [f"missing value for {{{name}}}" for name in missing],
        }

    args = [_substitute(a, merged) for a in preset.args]
    env = {k: _substitute(v, merged) for k, v in preset.env.items()}
    for extra in preset.optional_extra_env:
        if values.get(extra):
            env[extra] = values[extra]

    app_url = merged.get("APP_URL") or preset.app_url_default
    if preset.ui_url_default is None:
        ui_url = app_url if preset.id == "jobhunter" else None
    else:
        ui_url = _substitute(preset.ui_url_default, merged)

    reasons: List[str] = []
    dir_placeholder = _dir_placeholder(preset)
    if dir_placeholder:
        dir_value = merged.get(dir_placeholder, "")
        if not os.path.isdir(dir_value):
            reasons.append(f"{dir_placeholder} is not an existing directory: {dir_value}")
    bridge_path = args[0] if args else ""
    if bridge_path and not os.path.isfile(bridge_path):
        reasons.append(f"bridge script not found: {bridge_path}")

    if reasons:
        return {"ok": False, "missing": [], "reasons": reasons}

    return {
        "ok": True,
        "command": preset.command,
        "args": args,
        "env": env,
        "app_url": app_url,
        "ui_url": ui_url,
    }
