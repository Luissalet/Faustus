"""src/extension_manifest.py — TOOL-04: a versioned manifest per installed
MCP server/plugin (docs/spec/v2/backlog.json TOOL-04).

Before this lote there was no concept of "an extension" beyond a row in the
``mcp_servers`` table: no version, no hash of what is actually configured to
run, and no record of what it is allowed to touch. This module is the new,
single authority for that — nothing else in the repo keeps a manifest, so
this is a genuinely new store (not a second one for something that already
existed elsewhere — rule 4 asks to reuse an authority when one exists; there
is not one for this).

What it holds per server, keyed by ``server_id``:

* ``name`` / ``version`` — whatever the caller declares (an MCP server has no
  package.json this codebase can trust blindly; the admin says what they
  installed).
* ``command_hash`` — sha256 of the command + args that will actually run,
  so "this MCP server's config changed under me" is a fact, not a guess.
* ``permissions`` — DECLARED, not inferred: ``{"network", "files",
  "secrets"}`` booleans the caller states. :func:`suggested_permissions`
  offers a heuristic starting point (transport/env/args), but the manifest
  always stores what was actually declared.
* ``dependencies`` — a sorted list of names the caller declares.

:func:`diff_permissions` compares two permission sets. :func:`record_install_or_update`
uses it on every call: when a manifest already exists and the new call asks
for a permission the stored one did not have, the server is quarantined —
added to the SAME ``disabled_tools`` setting ``src/safe_mode.py``'s own MCP
quarantine already reuses (rule 4: one gate, not a second one) — until
:func:`approve_new_permissions` is called explicitly. A first-ever install
records a baseline and is never quarantined by this module on its own (there
is nothing to have changed FROM yet); the UI is expected to show the
declared permissions before the admin accepts the install in the first
place, which is a client-side gate this module does not need to duplicate.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

SETTING_KEY = "extension_manifests"
PERMISSION_KEYS: tuple = ("network", "files", "secrets")

__all__ = [
    "SETTING_KEY", "PERMISSION_KEYS",
    "command_hash", "suggested_permissions", "diff_permissions",
    "get_manifest", "all_manifests", "record_install_or_update",
    "approve_new_permissions", "is_quarantined_for_permissions",
]


def command_hash(command: str, args: Optional[Sequence[str]] = None) -> str:
    """sha256 of the command + argv that will actually run — stable across
    process restarts, changes the instant an admin edits either."""
    payload = json.dumps([str(command or ""), [str(a) for a in (args or [])]], separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _clean_permissions(raw: Any) -> Dict[str, bool]:
    if not isinstance(raw, dict):
        return {k: False for k in PERMISSION_KEYS}
    return {k: bool(raw.get(k)) for k in PERMISSION_KEYS}


def suggested_permissions(*, transport: str = "", command: str = "", args: Optional[Sequence[str]] = None,
                          env: Optional[Dict[str, str]] = None,
                          dependencies: Optional[Sequence[str]] = None) -> Dict[str, bool]:
    """A heuristic STARTING POINT for the UI to pre-check before an admin
    confirms — never what gets stored: :func:`record_install_or_update`
    always stores the caller's DECLARED permissions, this is only offered so
    the admin is not starting from an all-unchecked form.

    network: an SSE/HTTP transport obviously talks to a network; a stdio
    command is guessed network-capable only when its own name/args mention
    a URL or a known network-facing runner (npx/uvx pulling a package).
    files: any argument that looks like a filesystem path.
    secrets: any declared env var name that looks like a credential.
    Never raises — the worst outcome is an under- or over-eager suggestion,
    the same "no code" ceiling as any advisory read function in this repo."""
    args = list(args or [])
    env = dict(env or {})
    network = transport in ("sse", "http") or any(
        ("http://" in a or "https://" in a or a in ("npx", "uvx", "npm", "pip")) for a in [command, *args]
    )
    files = any(("/" in a or "\\" in a) and not a.startswith(("http://", "https://")) for a in args)
    secret_markers = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
    secrets = any(marker in k.upper() for k in env for marker in secret_markers)
    return {"network": bool(network), "files": bool(files), "secrets": bool(secrets)}


def diff_permissions(old: Optional[Dict[str, Any]], new: Optional[Dict[str, Any]]) -> Dict[str, List[str]]:
    """``{"added": [...], "removed": [...]}`` — permission keys newly True in
    ``new`` that were not True in ``old`` (and the reverse). ``old=None``
    (no prior manifest) reports every True key in ``new`` as ``added``: a
    first install still has permissions to SHOW, even though
    :func:`record_install_or_update` does not quarantine over them."""
    old_p = _clean_permissions(old)
    new_p = _clean_permissions(new)
    return {
        "added": sorted(k for k in PERMISSION_KEYS if new_p.get(k) and not old_p.get(k)),
        "removed": sorted(k for k in PERMISSION_KEYS if old_p.get(k) and not new_p.get(k)),
    }


def _load_all() -> Dict[str, Dict[str, Any]]:
    try:
        from src.settings import get_setting
        raw = get_setting(SETTING_KEY, {})
    except Exception as e:  # noqa: BLE001
        logger.debug("extension_manifest: load failed: %s", e)
        return {}
    return dict(raw) if isinstance(raw, dict) else {}


def _save_one(server_id: str, manifest: Dict[str, Any]) -> None:
    from src.settings import load_settings, save_settings
    settings = dict(load_settings())
    table = settings.get(SETTING_KEY)
    table = dict(table) if isinstance(table, dict) else {}
    table[str(server_id)] = manifest
    settings[SETTING_KEY] = table
    save_settings(settings)


def get_manifest(server_id: str) -> Optional[Dict[str, Any]]:
    return _load_all().get(str(server_id))


def all_manifests() -> Dict[str, Dict[str, Any]]:
    return _load_all()


def _quarantine(server_id: str) -> None:
    """Add ``server_id`` to ``disabled_tools`` — the exact mechanism
    ``src/safe_mode.py::_add_to_disabled_tools`` already uses for MCP
    quarantine (that function is private to its module, so this calls the
    same public settings authority it calls rather than reaching across into
    another file's private helper — same gate, independent caller)."""
    try:
        from src.settings import get_setting, save_settings, load_settings
        current = get_setting("disabled_tools", []) or []
        names = list(current) if isinstance(current, list) else []
        if server_id not in names:
            names.append(server_id)
            settings = dict(load_settings())
            settings["disabled_tools"] = names
            save_settings(settings)
    except Exception as e:  # noqa: BLE001 — quarantine must not crash the install
        logger.warning("extension_manifest: could not quarantine %s: %s", server_id, e)


def _unquarantine(server_id: str) -> None:
    try:
        from src.settings import get_setting, save_settings, load_settings
        current = get_setting("disabled_tools", []) or []
        names = [n for n in current if n != server_id] if isinstance(current, list) else []
        settings = dict(load_settings())
        settings["disabled_tools"] = names
        save_settings(settings)
    except Exception as e:  # noqa: BLE001
        logger.warning("extension_manifest: could not un-quarantine %s: %s", server_id, e)


def record_install_or_update(
    server_id: str, *, name: str = "", version: str = "", command: str = "",
    args: Optional[Sequence[str]] = None, dependencies: Optional[Sequence[str]] = None,
    permissions: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Record (or update) one server's manifest. Returns ``{"manifest",
    "diff", "quarantined"}``. ``quarantined`` is only ever True here on an
    UPDATE that adds a permission the previous manifest did not have — a
    first install is recorded but never auto-quarantined by this function
    (see the module docstring for why)."""
    from src.contracts.base import now_iso

    prior = get_manifest(server_id)
    clean_perms = _clean_permissions(permissions)
    diff = diff_permissions(prior.get("permissions") if prior else None, clean_perms)
    quarantined = bool(prior) and bool(diff["added"])

    manifest = {
        "server_id": str(server_id),
        "name": str(name or (prior or {}).get("name") or server_id),
        "version": str(version or ""),
        "command_hash": command_hash(command, args),
        "permissions": clean_perms,
        "dependencies": sorted({str(d) for d in (dependencies or [])}),
        "installed_at": (prior or {}).get("installed_at") or now_iso(),
        "updated_at": now_iso(),
        "pending_approval": quarantined,
    }
    _save_one(server_id, manifest)
    if quarantined:
        _quarantine(server_id)
        logger.warning(
            "extension_manifest: %s requested new permissions (%s) on update — quarantined pending approval",
            server_id, ", ".join(diff["added"]),
        )
    return {"manifest": manifest, "diff": diff, "quarantined": quarantined}


def approve_new_permissions(server_id: str) -> Dict[str, Any]:
    """Explicit admin approval: clears ``pending_approval`` and lifts the
    quarantine this module added. Never touches a quarantine
    ``src/safe_mode.py`` itself placed for an unrelated reason (repeated
    connection failures) — that is a different quarantine reason living in
    the same list, and only that module's own MCP-failure bookkeeping should
    lift it."""
    manifest = get_manifest(server_id)
    if manifest is None:
        raise ValueError(f"no manifest recorded for {server_id!r}")
    manifest = dict(manifest)
    manifest["pending_approval"] = False
    _save_one(server_id, manifest)
    _unquarantine(server_id)
    return manifest


def is_quarantined_for_permissions(server_id: str) -> bool:
    manifest = get_manifest(server_id)
    return bool(manifest and manifest.get("pending_approval"))
