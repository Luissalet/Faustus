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
import re
import unicodedata
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

#: The same manifest, living in the application's own repository instead of
#: in Faustus. Namespaced, because it sits in someone else's project root
#: next to their package.json and their README.
#:
#: This is the one that matters for an app nobody has taught Faustus about:
#: the author of an application knows what it exposes, and should be able to
#: say so in their own repo without sending a patch to Faustus. An app found
#: running with one of these is connectable on sight.
APP_MANIFEST_NAME = "faustus-plugin.json"

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


# ---------------------------------------------------------------------------
# How a person names a plugin in a sentence
# ---------------------------------------------------------------------------
#
# One place, because two readers need the same answer: tool selection (a
# request that names an installed app should bring the plugin tools along)
# and the approval gate (src/user_request_gate.py: an act on an app the user
# named passes). If they disagreed, the gate could let through a call the
# selection never offered, or the reverse.

def fold(text: Any) -> str:
    """Lower case, accents off, apostrophes gone, everything else a space."""
    raw = unicodedata.normalize("NFKD", str(text or ""))
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch)).casefold()
    raw = re.sub(r"['’`]", "", raw)
    return " ".join(re.sub(r"[^\w]+", " ", raw).split())


def names_for(plugin: "Plugin") -> List[str]:
    """How a person refers to this plugin: its id, its full name, and its
    first word when the name has more than one ("Jobhunter's Hoard" ->
    "jobhunters", "jobhunter"). Anything under four letters is dropped: too
    likely to be an ordinary word."""
    full = fold(getattr(plugin, "name", ""))
    names = {fold(getattr(plugin, "id", "")), full}
    parts = full.split()
    if len(parts) > 1:
        head = parts[0]
        names.add(head)
        if head.endswith("s") and len(head) > 5:
            names.add(head[:-1])
    return sorted(n for n in names if len(n) >= 4)


def named_in(text: Any) -> List["Plugin"]:
    """The installed plugins a sentence names, by id or name, as whole words."""
    folded = fold(text)
    if not folded:
        return []
    return [
        plugin for plugin in load_plugins().values()
        if any(re.search(rf"\b{re.escape(name)}\b", folded) for name in names_for(plugin))
    ]


# ---------------------------------------------------------------------------
# Manifests that travel inside the application
# ---------------------------------------------------------------------------

def read_app_manifest(app_dir: str) -> Optional["Plugin"]:
    """The plugin an application declares about itself, or None.

    Looks for `faustus-plugin.json` in the directory an app is installed in.
    Nothing is written and nothing is registered: this only answers "does
    this application say what it offers?", which is the question the
    nearby-apps scan needs in order to offer an app Faustus ships no
    knowledge of.

    A malformed one is a None with a logged reason, never an exception — it
    is a file in somebody else's repository, and Faustus is reading it
    uninvited.
    """
    path = os.path.join(str(app_dir or ""), APP_MANIFEST_NAME)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return parse_manifest(data, source="app", path=path)
    except (OSError, ValueError, ManifestError) as exc:
        logger.info("plugin manifest in %s refused: %s", app_dir, exc)
        return None


def install_from_dir(app_dir: str) -> Dict[str, Any]:
    """Adopt the manifest an application ships, as an installed plugin.

    Copies it under `<DATA_DIR>/plugins/<id>/plugin.json` — the same place a
    hand-installed plugin lives — so nothing downstream needs to know where
    it came from, and so removing the application does not silently take a
    configured connection with it.

    The application's own copy stays the original. This is a snapshot taken
    on adoption, not a link: an app that changes what it offers in a later
    version is a change the user should see and accept, not one that
    rewrites a live connection underneath them.
    """
    plugin = read_app_manifest(app_dir)
    if plugin is None:
        return {"ok": False,
                "reason": f"no readable {APP_MANIFEST_NAME} in {app_dir}"}
    target_dir = os.path.join(user_dir(), plugin.id)
    target = os.path.join(target_dir, MANIFEST_NAME)
    try:
        os.makedirs(target_dir, exist_ok=True)
        with open(plugin.path, "r", encoding="utf-8") as src:
            raw = src.read()
        with open(target, "w", encoding="utf-8", newline="\n") as dst:
            dst.write(raw)
    except OSError as exc:
        return {"ok": False, "reason": f"could not install {plugin.id}: {exc}"}
    reset_cache()
    return {"ok": True, "id": plugin.id, "name": plugin.name,
            "path": target, "from": plugin.path}
