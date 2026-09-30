"""Acquire catalogued source trees without installing or executing their code."""
from __future__ import annotations

import json
from functools import wraps
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
from urllib.parse import urlsplit

_LOCK = threading.RLock()
_ID = re.compile(r"^[a-z][a-z0-9_-]*$")
GIT_TIMEOUT_S = 120


class MarketplaceError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def _io_errors(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except OSError:
            raise MarketplaceError("filesystem_error", "Plugin source or registry operation failed") from None
    return wrapped


def _paths(repo_root=None, data_dir=None):
    root = Path(repo_root).resolve() if repo_root is not None else Path(__file__).resolve().parents[1]
    if data_dir is None:
        from src.constants import DATA_DIR
        data_dir = DATA_DIR
    return root, Path(data_dir).resolve() / "plugin-marketplace-links.json"


def _canonical_url(url, *, allow_ssh=False):
    try:
        if allow_ssh and isinstance(url, str):
            match = re.fullmatch(r"git@github\.com:([^?#\s]+)", url, re.IGNORECASE)
            if match:
                url = "https://github.com/" + match.group(1)
            elif url.startswith("ssh://git@github.com/"):
                url = "https://github.com/" + url[len("ssh://git@github.com/"):]
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment or parsed.port):
            raise ValueError()
        path = parsed.path.rstrip("/")
        if path.endswith(".git"):
            path = path[:-4]
        if not path or path == "/":
            raise ValueError()
        if parsed.hostname.lower() == "github.com":
            path = path.lower()
        return "https://" + parsed.hostname.lower() + path
    except (ValueError, TypeError):
        raise MarketplaceError("invalid_repository_url", "Repository must be a credential-free HTTPS URL") from None


def _catalog(root):
    try:
        doc = json.loads((root / "plugins" / "marketplace.json").read_text(encoding="utf-8"))
        if doc.get("schema") != 1 or not isinstance(doc.get("plugins"), list):
            raise ValueError()
        rows, seen = [], set()
        for raw in doc["plugins"]:
            plugin_id = raw["id"]
            if not isinstance(plugin_id, str) or not _ID.fullmatch(plugin_id) or plugin_id in seen:
                raise ValueError()
            if "required" in raw and type(raw["required"]) is not bool:
                raise ValueError()
            _canonical_url(raw["repository_url"])
            manifest_path = root / "plugins" / plugin_id / "plugin.json"
            from src.plugins import parse_manifest
            manifest = parse_manifest(json.loads(manifest_path.read_text(encoding="utf-8")), path=str(manifest_path))
            if manifest.id != plugin_id:
                raise ValueError()
            rows.append({"id": plugin_id, "name": manifest.name, "purpose": manifest.purpose,
                         "repository_url": raw["repository_url"], "required": raw.get("required", False)})
            seen.add(plugin_id)
        return rows
    except MarketplaceError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise MarketplaceError("invalid_catalog", "Marketplace catalog or builtin manifest is invalid") from None


def _registry(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        raise MarketplaceError("invalid_registry", "Local plugin link registry cannot be read") from None
    if not isinstance(value, dict) or value.get("schema") != 1 or not isinstance(value.get("links"), dict):
        raise MarketplaceError("invalid_registry", "Local plugin link registry has an invalid format")
    links = value["links"]
    if any(not isinstance(k, str) or not _ID.fullmatch(k) or not isinstance(v, str) for k, v in links.items()):
        raise MarketplaceError("invalid_registry", "Local plugin link registry has invalid entries")
    return links


def _save_registry(path, links):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".plugin-links-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"schema": 1, "links": links}, stream, indent=2)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _git(args):
    env = dict(os.environ)
    env.update(GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    # Ignore inherited command-level config (which can install filters/hooks).
    for key in list(env):
        if key in {"GIT_CONFIG_COUNT", "GIT_CONFIG", "GIT_CONFIG_PARAMETERS", "GIT_TEMPLATE_DIR",
                   "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR",
                   "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES"} or key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            env.pop(key)
    try:
        result = subprocess.run(["git", "-c", "core.hooksPath=" + os.devnull,
                                 "-c", "protocol.file.allow=never", "-c", "protocol.ext.allow=never", *args],
                                shell=False, timeout=GIT_TIMEOUT_S, capture_output=True, text=True, env=env,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        raise MarketplaceError("git_timeout", "Git operation timed out") from None
    except OSError:
        raise MarketplaceError("git_unavailable", "Git could not be started") from None
    if result.returncode:
        # Do not echo Git stderr: remotes/config can contain credentials.
        raise MarketplaceError("git_failed", "Git operation failed")
    return result.stdout.strip()


def _validate_source(path, row):
    if not path.is_dir():
        raise MarketplaceError("invalid_source", "Plugin source must be an existing directory")
    manifest_path = path / "faustus-plugin.json"
    if os.path.lexists(manifest_path):
        from src.plugins import read_app_manifest
        manifest = read_app_manifest(str(path))
        if manifest is None or manifest.id != row["id"]:
            raise MarketplaceError("identity_mismatch", "Application manifest does not match the catalog plugin")
        return
    # An old source tree without a manifest is accepted only by known origin.
    if not (path / ".git").exists():
        raise MarketplaceError("identity_mismatch", "Source has neither a matching manifest nor a Git checkout")
    remote = _git(["-C", str(path), "remote", "get-url", "origin"])
    if _canonical_url(remote, allow_ssh=True) != _canonical_url(row["repository_url"]):
        raise MarketplaceError("identity_mismatch", "Git origin does not match the catalog repository")


def _row(root, plugin_id):
    for row in _catalog(root):
        if row["id"] == plugin_id:
            return row
    raise MarketplaceError("unknown_plugin", "Plugin is not in the marketplace catalog")


@_io_errors
def list_marketplace(*, repo_root=None, data_dir=None):
    with _LOCK:
        root, registry = _paths(repo_root, data_dir)
        links = _registry(registry)
        rows = []
        for row in _catalog(root):
            clone = root / "plugins" / row["id"] / "repository"
            local = links.get(row["id"])
            state, source_kind = "not_installed", "none"
            if local:
                source_kind = "linked"
                try:
                    _validate_source(Path(local), row)
                    state = "linked"
                except MarketplaceError:
                    state = "invalid_link"
            elif os.path.lexists(clone):
                source_kind = "cloned"
                local = str(clone)
                try:
                    _validate_source(clone, row)
                    state = "cloned"
                except MarketplaceError:
                    state = "conflict"
            rows.append({**row, "source_kind": source_kind, "local_path": local,
                         "clone_path": str(clone), "state": state,
                         "can_install": not local and not os.path.lexists(clone)})
        return {"root": str(root), "plugins": rows}


@_io_errors
def link(plugin_id, path, *, repo_root=None, data_dir=None):
    with _LOCK:
        root, registry = _paths(repo_root, data_dir)
        row = _row(root, plugin_id)
        if not isinstance(path, (str, os.PathLike)) or not str(path).strip():
            raise MarketplaceError("invalid_source", "A local source path is required")
        source = Path(path).expanduser().resolve()
        _validate_source(source, row)
        links = _registry(registry)
        links[plugin_id] = str(source)
        _save_registry(registry, links)
        return next(r for r in list_marketplace(repo_root=root, data_dir=registry.parent)["plugins"] if r["id"] == plugin_id)


@_io_errors
def unlink(plugin_id, *, repo_root=None, data_dir=None):
    with _LOCK:
        root, registry = _paths(repo_root, data_dir)
        _row(root, plugin_id)
        links = _registry(registry)
        links.pop(plugin_id, None)
        _save_registry(registry, links)
        return {"id": plugin_id, "unlinked": True}


def _install_one(plugin_id, *, repo_root=None, data_dir=None):
    with _LOCK:
        root, registry = _paths(repo_root, data_dir)
        row = _row(root, plugin_id)
        target = root / "plugins" / plugin_id / "repository"
        if target.parent.resolve() != target.parent:
            raise MarketplaceError("unsafe_target", "Clone parent must be inside the checkout without path aliases")
        if plugin_id in _registry(registry) or os.path.lexists(target):
            raise MarketplaceError("already_present", "Existing sources are preserved; unlink or choose a local link")
        stage = Path(tempfile.mkdtemp(prefix=".repository-stage-", dir=target.parent))
        try:
            _git(["clone", "--", row["repository_url"], str(stage)])
            _validate_source(stage, row)
            if os.path.lexists(target):
                raise MarketplaceError("already_present", "Clone target became occupied")
            os.rename(stage, target)
        finally:
            if stage.exists():
                # Check the actual Windows deletion target, including aliases.
                if stage.resolve().parent != target.parent.resolve() or stage.is_symlink():
                    raise MarketplaceError("unsafe_target", "Staging directory moved outside its plugin folder")
                shutil.rmtree(stage)
        return next(r for r in list_marketplace(repo_root=root, data_dir=registry.parent)["plugins"] if r["id"] == plugin_id)


@_io_errors
def install(plugin_id, *, repo_root=None, data_dir=None):
    """Acquire required sources first; a required failure prevents this clone."""
    with _LOCK:
        options = {"repo_root": repo_root, "data_dir": data_dir}
        rows = list_marketplace(**options)["plugins"]
        target = next((row for row in rows if row["id"] == plugin_id), None)
        if target is None:
            raise MarketplaceError("unknown_plugin", "Plugin is not in the marketplace catalog")
        if not target["can_install"]:
            raise MarketplaceError("already_present", "Existing sources are preserved; unlink or choose a local link")
        for required in rows:
            if not required["required"] or required["id"] == plugin_id:
                continue
            if required["state"] in {"linked", "cloned"}:
                continue
            if not required["can_install"]:
                raise MarketplaceError("required_unavailable", "A required plugin source needs attention before installation")
            _install_one(required["id"], **options)
        return _install_one(plugin_id, **options)
