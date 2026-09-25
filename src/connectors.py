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
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.constants import BASE_DIR


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


def _implicit_defaults() -> Dict[str, str]:
    """Placeholders every preset may use without a user ever setting them:
    Faustus's own install directory and its own interpreter. Filled in here
    (not in any single preset's ``defaults``) so `resolve_preset_values`
    never reports them as missing, no matter which preset declares them."""
    return {
        "FAUSTUS_DIR": BASE_DIR.rstrip("/\\"),
        "FAUSTUS_PYTHON": sys.executable or "python3",
    }


def _writer_token_file_default() -> str:
    """Windows default: ``%APPDATA%/writers-hoard/aibridge/token``.

    Expanded eagerly (`expandvars`/`expanduser`) so the value stored in the
    sidecar and handed to the child is already a real path — a raw
    ``%APPDATA%`` would mean nothing on POSIX, and `os.path.isfile` below
    needs a concrete path either way.
    """
    raw = os.path.join("%APPDATA%", "writers-hoard", "aibridge", "token")
    return os.path.expandvars(os.path.expanduser(raw))


#: Every plugin this installation has, as the `ConnectorPreset` shape the
#: rest of the code already speaks. Derived, not written: the source of
#: truth for a plugin is its own manifest (`plugins/<id>/plugin.json`, or
#: `<DATA_DIR>/plugins/<id>/plugin.json` for one the user installed).
#:
#: This used to be five hand-written entries here, with the fingerprint that
#: recognises each app kept in a SECOND dict in `src/connector_discovery.py`.
#: Two files to remember meant the second one was forgotten: three of the
#: five plugins had no fingerprint at all and were invisible to the nearby-
#: apps scan. One manifest per plugin makes that particular mistake
#: unavailable.
PRESETS: Dict[str, ConnectorPreset] = {}


def reload_presets() -> Dict[str, ConnectorPreset]:
    """Re-read the manifests and rebuild `PRESETS` in place.

    In place, because callers hold a reference to the dict itself (and tests
    patch entries into it); rebinding the name would leave them looking at
    the old one.
    """
    from src import plugins as plugins_mod

    plugins_mod.reset_cache()
    fresh = {pid: p.to_preset() for pid, p in plugins_mod.load_plugins().items()}
    PRESETS.clear()
    PRESETS.update(fresh)
    return PRESETS


def _load_presets_once() -> None:
    from src import plugins as plugins_mod

    PRESETS.update({pid: p.to_preset() for pid, p in plugins_mod.load_plugins().items()})


_load_presets_once()


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
            # Only what the form has a field for: a manifest may carry
            # defaults for the Hub (DATA_DIR, where the app keeps backups)
            # that Faustus neither asks for nor uses.
            "defaults": {k: v for k, v in preset.defaults.items() if k in preset.placeholders},
            "launch_profile_hint": dict(preset.launch_profile_hint),
            "optional_extra_env": list(preset.optional_extra_env),
        })
    return out


def get_preset(preset_id: str) -> Optional[ConnectorPreset]:
    return PRESETS.get(preset_id)


def adopt_declared_app(app_dir: str, expected_id: str) -> Dict[str, Any]:
    """Turn an application's own `faustus-plugin.json` into an installed plugin.

    Discovery can recognise an app Faustus ships nothing for because the app
    declares itself, but a connector is built from a preset, and a preset
    only exists for manifests Faustus has loaded. Adopting such an app
    therefore installs its manifest first — the snapshot under
    ``<DATA_DIR>/plugins/<id>/`` that `plugins.install_from_dir` writes —
    and reloads the presets, which is what docs/api/plugins.md promises.

    Only for an id Faustus does not already know: a declaration can never
    replace a shipped or installed plugin this way. Never raises.
    """
    if get_preset(expected_id) is not None:
        return {"ok": True, "id": expected_id, "installed": False}
    from src import plugins as plugins_mod

    declared = plugins_mod.read_app_manifest(app_dir)
    if declared is None:
        return {"ok": False, "reason": f"no readable {plugins_mod.APP_MANIFEST_NAME} in {app_dir}"}
    if declared.id != expected_id:
        return {"ok": False,
                "reason": f"the app now declares {declared.id!r}, not {expected_id!r}; scan again"}
    out = plugins_mod.install_from_dir(app_dir)
    if not out.get("ok"):
        return out
    reload_presets()
    if get_preset(expected_id) is None:
        problems = [e["reason"] for e in plugins_mod.cached().errors if expected_id in e.get("path", "")]
        return {"ok": False,
                "reason": "installed but not loaded: " + ("; ".join(problems) or "unknown reason")}
    return {"ok": True, "id": expected_id, "installed": True, "path": out.get("path")}


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
    merged.update(_implicit_defaults())
    merged.update(values)
    # A default may itself name another placeholder ("{JOBHUNT_DIR}/data/
    # mcp-token"); resolve those against the user's values first.
    merged = {k: _substitute(v, merged) for k, v in merged.items()}

    missing = [p for p in preset.placeholders if not merged.get(p)]
    if missing:
        return {
            "ok": False,
            "missing": missing,
            "reasons": [f"missing value for {{{name}}}" for name in missing],
        }

    command = _substitute(preset.command, merged)
    args = [_substitute(a, merged) for a in preset.args]
    env = {k: _substitute(v, merged) for k, v in preset.env.items()}
    for extra in preset.optional_extra_env:
        if values.get(extra):
            env[extra] = values[extra]

    app_url = merged.get("APP_URL") or preset.app_url_default
    # Whether an app has a UI worth opening is a fact about that app, so it
    # is declared in its manifest (`app.ui_url`, usually "{APP_URL}", null
    # when the port is an API and not a page). It used to be decided here by
    # an `if preset.id == "jobhunter"` — one plugin's detail sitting in the
    # code path every plugin goes through.
    ui_url = _substitute(preset.ui_url_default, merged) if preset.ui_url_default else None

    reasons: List[str] = []
    dir_placeholder = _dir_placeholder(preset)
    if dir_placeholder:
        dir_value = merged.get(dir_placeholder, "")
        if not os.path.isdir(dir_value):
            reasons.append(f"{dir_placeholder} is not an existing directory: {dir_value}")
    bridge_path = args[0] if args else ""
    # `python -m package.module` runs a module, not a file: the first
    # argument is the flag itself. Seen live with an app whose bridge is a
    # module: adopting it failed with "bridge script not found: -m".
    if bridge_path.startswith("-"):
        bridge_path = ""
    if bridge_path and not os.path.isfile(bridge_path):
        reasons.append(f"bridge script not found: {bridge_path}")

    if reasons:
        return {"ok": False, "missing": [], "reasons": reasons}

    return {
        "ok": True,
        "command": command,
        "args": args,
        "env": env,
        "app_url": app_url,
        "ui_url": ui_url,
        "token_file": merged.get("TOKEN_FILE") or "",
    }


def read_token_file(path: str) -> Optional[str]:
    """The bearer token an app's bridge writes for its own clients, or None.
    Read only — never logged, never stored anywhere else."""
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            token = f.read().strip()
        return token or None
    except OSError:
        return None


def default_token_files() -> Dict[str, str]:
    """Preset id -> default TOKEN_FILE that needs no user value (Writer's
    Hoard writes it under %APPDATA%). Used by discovery to identify an app
    that answers 401 without a token."""
    out: Dict[str, str] = {}
    for preset in PRESETS.values():
        path = preset.defaults.get("TOKEN_FILE") or ""
        if path and "{" not in path and os.path.isfile(path):
            out[preset.id] = path
    return out
