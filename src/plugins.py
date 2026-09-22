"""plugins.py — one file per plugin, and that file is the whole plugin.

A *plugin* is a capability pack Faustus can be given: a domain app it knows
how to reach, the tools that come with it, and (schema 1 onwards) the skills,
recipes and knowledge sources it contributes. "Jobhunter's Hoard" is tools
about a job search; "Dorian's Hoard" is closer to a DLC for what Faustus
knows about its user.

Before this module a plugin was not one thing. The template for its MCP
server lived in `src/connectors.py`, the fingerprint that recognises the app
when it is already running lived in a second dict in
`src/connector_discovery.py`, and whether the app had a UI at all was decided
by an `if preset.id == "jobhunter"` in the middle of generic code. Adding a
plugin meant remembering all three. The first two were remembered; by the
third plugin the fingerprint was not, so three of the five shipped plugins
were invisible to "nearby apps" — Faustus could not recognise its own
applications running in front of it.

So: everything about a plugin is declared in one manifest, and the old
structures are derived from it. `PRESETS` and `FINGERPRINTS` become views,
not sources. A new plugin is a folder with a JSON file in it and nothing
else, which is also what makes a plugin something a user — or Faustus
itself — can write without editing Faustus.

Where they live
---------------
* ``<repo>/plugins/<id>/plugin.json`` — shipped with Faustus.
* ``<DATA_DIR>/plugins/<id>/plugin.json`` — installed by this user.

A user manifest with the id of a built-in one replaces it (that is how you
patch a shipped plugin without forking), and the replacement is reported in
`load_all().shadowed` rather than happening silently.

Failure policy
--------------
A malformed manifest never raises out of this module and never takes the
others down with it: it is skipped, and the reason is kept in
`load_all().errors` so a screen can say which plugin did not load and why.
The alternative — one bad third-party file and Faustus has no connectors at
all — is not a trade this module is willing to make.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.constants import BASE_DIR, DATA_DIR

logger = logging.getLogger(__name__)

#: Manifest schema version this module understands. A manifest declaring a
#: higher one is refused rather than read with today's rules — a plugin
#: written for a Faustus that knows more than this one is not something to
#: guess at.
SCHEMA = 1

MANIFEST_NAME = "plugin.json"

#: Every key a manifest may carry at the top level. An unknown key is a
#: typo or a newer schema, and both deserve to be said out loud rather than
#: ignored into silence.
TOP_LEVEL_KEYS = {
    "schema", "id", "name", "purpose", "capabilities",
    "app", "mcp", "placeholders", "defaults", "provides", "notes",
}
APP_KEYS = {"url_default", "ui_url", "health", "identify", "launch_hint"}
MCP_KEYS = {"transport", "command", "args", "env", "optional_env"}
HEALTH_KEYS = {"path", "expect"}
PROVIDES_KEYS = {"skills", "recipes", "knowledge"}


class ManifestError(ValueError):
    """A manifest could not be read as a plugin. Carries the reason."""


@dataclass(frozen=True)
class Plugin:
    """One loaded, validated manifest plus where it came from."""

    id: str
    name: str
    purpose: str
    capabilities: List[str]
    placeholders: List[str]
    defaults: Dict[str, str]
    # -- the app this plugin speaks to -------------------------------------
    app_url_default: str
    ui_url: Optional[str]
    health_path: str
    health_expect: Dict[str, Any]
    #: Health-body fields that identify this app when it is found listening
    #: on a port: ``{"service": [...], "title": [...]}``. Empty means the
    #: plugin cannot be recognised by a scan — which is a fact worth being
    #: able to see, not a reason to fail.
    identify: Dict[str, List[str]]
    launch_hint: Dict[str, Any]
    # -- the tools it brings ------------------------------------------------
    transport: str
    command: str
    args: List[str]
    env: Dict[str, str]
    optional_env: List[str]
    # -- everything else it contributes -------------------------------------
    provides: Dict[str, Any] = field(default_factory=dict)
    notes: str = ""
    # -- provenance ---------------------------------------------------------
    source: str = "builtin"          # "builtin" | "user"
    path: str = ""                   # the manifest file this came from

    def to_preset(self):
        """The `ConnectorPreset` this plugin is, for the code that still
        speaks that language. Imported here, not at module scope, because
        `src.connectors` imports THIS module to build its presets."""
        from src.connectors import ConnectorPreset

        return ConnectorPreset(
            id=self.id,
            name=self.name,
            purpose=self.purpose,
            capabilities=list(self.capabilities),
            transport=self.transport,
            command=self.command,
            args=list(self.args),
            env=dict(self.env),
            app_url_default=self.app_url_default,
            health_path=self.health_path,
            health_expect=dict(self.health_expect),
            ui_url_default=self.ui_url,
            placeholders=list(self.placeholders),
            launch_profile_hint=dict(self.launch_hint),
            defaults=dict(self.defaults),
            optional_extra_env=list(self.optional_env),
        )


@dataclass
class Loaded:
    """The result of a scan: what loaded, what did not, and what replaced what."""

    plugins: Dict[str, Plugin] = field(default_factory=dict)
    #: ``[{"path": ..., "reason": ...}]`` — one entry per manifest refused.
    errors: List[Dict[str, str]] = field(default_factory=list)
    #: ``[{"id": ..., "kept": <path>, "shadowed": <path>}]``
    shadowed: List[Dict[str, str]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def builtin_dir() -> str:
    return os.path.join(BASE_DIR, "plugins")


def user_dir() -> str:
    return os.path.join(DATA_DIR, "plugins")


def _as_str_list(value: Any, where: str) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ManifestError(f"{where} must be a list of strings")
    return list(value)


def _as_str_map(value: Any, where: str) -> Dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    ):
        raise ManifestError(f"{where} must be an object of string to string")
    return dict(value)


def _expand_default(value: str) -> str:
    """A default value with ``%VAR%``/``$VAR``/``~`` resolved, once.

    Only a value that actually expanded is normalised, and never one that
    looks like a URL: ``os.path.normpath`` turns ``http://host`` into
    ``http:/host``. The normalisation exists so a path written with forward
    slashes in a cross-platform manifest reaches the child process in this
    platform's own spelling, the way `os.path.join` used to write it.
    """
    expanded = os.path.expandvars(os.path.expanduser(value))
    if expanded == value or "://" in expanded:
        return expanded
    return os.path.normpath(expanded)


def _unknown(keys: Any, allowed: set, where: str) -> None:
    if not isinstance(keys, dict):
        raise ManifestError(f"{where} must be an object")
    extra = sorted(set(keys) - allowed)
    if extra:
        raise ManifestError(f"{where} has unknown key(s): {', '.join(extra)}")


def parse_manifest(data: Any, *, source: str = "builtin", path: str = "") -> Plugin:
    """Validate one manifest object and return the Plugin it describes.

    Raises `ManifestError` with a reason a person can act on. Deliberately
    strict about unknown keys: a manifest is the whole plugin, so a key that
    goes nowhere is a promise that will not be kept.
    """
    _unknown(data, TOP_LEVEL_KEYS, "manifest")

    schema = data.get("schema")
    if schema is None:
        raise ManifestError("manifest has no 'schema'")
    if not isinstance(schema, int) or isinstance(schema, bool) or schema < 1:
        raise ManifestError("'schema' must be a positive integer")
    if schema > SCHEMA:
        raise ManifestError(
            f"manifest declares schema {schema}; this Faustus understands up to {SCHEMA}"
        )

    plugin_id = data.get("id")
    if not isinstance(plugin_id, str) or not plugin_id.strip():
        raise ManifestError("manifest has no 'id'")
    plugin_id = plugin_id.strip()
    if not all(c.isalnum() or c in "-_" for c in plugin_id):
        raise ManifestError(f"'id' must be alphanumeric with - or _: {plugin_id!r}")

    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ManifestError("manifest has no 'name'")

    purpose = data.get("purpose") or ""
    if not isinstance(purpose, str):
        raise ManifestError("'purpose' must be a string")

    capabilities = _as_str_list(data.get("capabilities"), "'capabilities'")
    placeholders = _as_str_list(data.get("placeholders"), "'placeholders'")
    # A default may name an environment variable or a home directory
    # (``%APPDATA%/writers-hoard/aibridge/token``, ``~/.config/...``) and is
    # expanded here, once, so what reaches the child process is a real path.
    # This is what keeps a shipped manifest machine-independent: the
    # alternative is a default holding one particular person's home
    # directory, which is what happens the moment a value like this is
    # written out already expanded.
    defaults = {
        key: _expand_default(value)
        for key, value in _as_str_map(data.get("defaults"), "'defaults'").items()
    }

    app = data.get("app") or {}
    _unknown(app, APP_KEYS, "'app'")
    app_url_default = app.get("url_default") or ""
    if not isinstance(app_url_default, str):
        raise ManifestError("'app.url_default' must be a string")
    ui_url = app.get("ui_url")
    if ui_url is not None and not isinstance(ui_url, str):
        raise ManifestError("'app.ui_url' must be a string or null")

    health = app.get("health") or {}
    _unknown(health, HEALTH_KEYS, "'app.health'")
    health_path = health.get("path") or ""
    if not isinstance(health_path, str):
        raise ManifestError("'app.health.path' must be a string")
    health_expect = health.get("expect") or {}
    if not isinstance(health_expect, dict):
        raise ManifestError("'app.health.expect' must be an object")

    identify_raw = app.get("identify") or {}
    if not isinstance(identify_raw, dict):
        raise ManifestError("'app.identify' must be an object")
    identify: Dict[str, List[str]] = {}
    for key, value in identify_raw.items():
        if not isinstance(key, str):
            raise ManifestError("'app.identify' keys must be strings")
        identify[key] = _as_str_list(value, f"'app.identify.{key}'")

    launch_hint = app.get("launch_hint") or {}
    if not isinstance(launch_hint, dict):
        raise ManifestError("'app.launch_hint' must be an object")

    mcp = data.get("mcp") or {}
    _unknown(mcp, MCP_KEYS, "'mcp'")
    transport = mcp.get("transport") or "stdio"
    if transport != "stdio":
        raise ManifestError(f"'mcp.transport' must be 'stdio' (got {transport!r})")
    command = mcp.get("command") or ""
    if not isinstance(command, str) or not command.strip():
        raise ManifestError("'mcp.command' is required")
    args = _as_str_list(mcp.get("args"), "'mcp.args'")
    env = _as_str_map(mcp.get("env"), "'mcp.env'")
    optional_env = _as_str_list(mcp.get("optional_env"), "'mcp.optional_env'")

    provides = data.get("provides") or {}
    _unknown(provides, PROVIDES_KEYS, "'provides'")

    notes = data.get("notes") or ""
    if not isinstance(notes, str):
        raise ManifestError("'notes' must be a string")

    return Plugin(
        id=plugin_id,
        name=name.strip(),
        purpose=purpose,
        capabilities=capabilities,
        placeholders=placeholders,
        defaults=defaults,
        app_url_default=app_url_default,
        ui_url=ui_url,
        health_path=health_path,
        health_expect=dict(health_expect),
        identify=identify,
        launch_hint=dict(launch_hint),
        transport=transport,
        command=command,
        args=args,
        env=env,
        optional_env=optional_env,
        provides=dict(provides),
        notes=notes,
        source=source,
        path=path,
    )


def _scan(root: str, source: str, into: Loaded) -> None:
    if not root or not os.path.isdir(root):
        return
    try:
        entries = sorted(os.listdir(root))
    except OSError as exc:  # noqa: BLE001 - an unreadable folder is not fatal
        into.errors.append({"path": root, "reason": f"cannot list directory: {exc}"})
        return
    for entry in entries:
        manifest_path = os.path.join(root, entry, MANIFEST_NAME)
        if not os.path.isfile(manifest_path):
            continue
        try:
            with open(manifest_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:
            into.errors.append({"path": manifest_path, "reason": f"unreadable: {exc}"})
            continue
        try:
            plugin = parse_manifest(data, source=source, path=manifest_path)
        except ManifestError as exc:
            into.errors.append({"path": manifest_path, "reason": str(exc)})
            continue
        if plugin.id != entry:
            into.errors.append({
                "path": manifest_path,
                "reason": f"id {plugin.id!r} does not match its folder name {entry!r}",
            })
            continue
        previous = into.plugins.get(plugin.id)
        if previous is not None:
            into.shadowed.append({
                "id": plugin.id, "kept": plugin.path, "shadowed": previous.path,
            })
        into.plugins[plugin.id] = plugin


def load_all() -> Loaded:
    """Every plugin on this installation, read fresh from disk. Built-ins
    first, then the user's, so a user manifest with the same id replaces the
    shipped one."""
    out = Loaded()
    _scan(builtin_dir(), "builtin", out)
    _scan(user_dir(), "user", out)
    for entry in out.errors:
        logger.warning("plugin manifest refused (%s): %s", entry["path"], entry["reason"])
    return out


#: Read once per process. Manifests are static files, and `PRESETS` is built
#: from them on import, so re-reading five JSON files on every request buys
#: nothing. Anything that adds, edits or removes a manifest calls
#: `reset_cache()` — which is also what a test does after writing one into a
#: temporary directory.
_cache: Optional[Loaded] = None


def cached() -> Loaded:
    global _cache
    if _cache is None:
        _cache = load_all()
    return _cache


def reset_cache() -> None:
    """Forget the loaded manifests. Call after installing or editing one."""
    global _cache
    _cache = None


def load_plugins() -> Dict[str, "Plugin"]:
    """Just the plugins, for callers that have nothing to do about a bad one."""
    return dict(cached().plugins)


def get(plugin_id: str) -> Optional["Plugin"]:
    return cached().plugins.get(str(plugin_id or ""))
