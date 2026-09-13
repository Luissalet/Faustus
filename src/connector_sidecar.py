"""src/connector_sidecar.py — F1.2: `data/connectors.json`.

`McpServer` (core/database.py) has no `preset_id`, `app_url`, `ui_url` or
`launch_profile_id` column — it is the generic MCP-server row every transport
already uses, and nothing here duplicates it: an entry in this sidecar always
points at a real `McpServer.id` and never re-stores `command`/`args`/`env`
itself. What the sidecar adds is exactly the fields `McpServer` does not
carry, and only for the servers the Connectors screen created from a preset —
an MCP server added the old way (`/api/mcp/servers`) simply has no sidecar
entry, and `routes/connector_routes.py` shows it alongside the ones that do
(`preset_id: null`).

Written atomically (`core.atomic_io.atomic_write_json`, tmp + os.replace) and
serialized through a module-level lock: two concurrent writers (two browser
tabs saving at once) must not interleave a read-modify-write and drop one
edit.

Ownership: F1.2 asks for the "misma convención que src/connector_registry.py"
— but `/api/mcp/servers` (routes/mcp/mcp_routes.py::list_servers) does not
filter its listing by owner at all (checked against the real route — it has
no owner concept whatsoever), so per the contract's own escape hatch ("si no
filtra por owner, tampoco lo hagas tú") `list_connectors()` here does not
filter either. `owner` is still recorded on write (who created this
connector), purely informational — the same "record it, do not gate on it"
posture `connector_registry.py` documents for the one column `src.integrations`
does not have yet.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from core.atomic_io import atomic_write_json
from core.platform_compat import safe_chmod
from src.constants import DATA_DIR as _DEFAULT_DATA_DIR

logger = logging.getLogger(__name__)

# Module-level, like src/mcp_manager.py's own `DATA_DIR` — tests point this at
# a disposable tmp_path via `monkeypatch.setattr(connector_sidecar, "DATA_DIR", ...)`
# rather than the real data dir. Read fresh on every call, never cached at
# import time.
DATA_DIR = _DEFAULT_DATA_DIR

_LOCK = threading.Lock()


def _connectors_file() -> str:
    return os.path.join(DATA_DIR, "connectors.json")

_REDACT_MARKERS = ("TOKEN", "KEY", "SECRET", "PASSWORD")
#: A key that names WHERE a secret lives (`TOKEN_FILE`, `KEY_PATH`) holds a
#: path, not the secret — the bridge reads the file itself. Redacting the
#: path made the edit form show "***redacted***" and write it back on Save.
_LOCATION_SUFFIXES = ("_FILE", "_PATH", "_DIR")
REDACTED = "***redacted***"


def is_secret_key(key: str) -> bool:
    upper = str(key).upper()
    if upper.endswith(_LOCATION_SUFFIXES):
        return False
    return any(marker in upper for marker in _REDACT_MARKERS)


def _redact_values(values: Dict[str, Any]) -> Dict[str, Any]:
    """Principle 2: never let a secret-shaped value leave this module."""
    out = {}
    for k, v in (values or {}).items():
        out[k] = REDACTED if is_secret_key(k) else v
    return out


def _load() -> Dict[str, Any]:
    path = _connectors_file()
    if not os.path.exists(path):
        return {"version": 1, "connectors": []}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Failed to load connector sidecar: %s", exc)
        return {"version": 1, "connectors": []}
    if not isinstance(raw, dict) or not isinstance(raw.get("connectors"), list):
        return {"version": 1, "connectors": []}
    return raw


def _save(data: Dict[str, Any]) -> None:
    path = _connectors_file()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    atomic_write_json(path, data, indent=2)
    safe_chmod(path, 0o600)


def list_connectors(*, redact: bool = True) -> List[Dict[str, Any]]:
    with _LOCK:
        data = _load()
        entries = [dict(e) for e in data["connectors"]]
    if redact:
        for e in entries:
            e["values"] = _redact_values(e.get("values") or {})
    return entries


def get_connector(connector_id: str, *, redact: bool = True) -> Optional[Dict[str, Any]]:
    with _LOCK:
        data = _load()
        for entry in data["connectors"]:
            if entry.get("id") == connector_id:
                out = dict(entry)
                break
        else:
            return None
    if redact:
        out["values"] = _redact_values(out.get("values") or {})
    return out


def get_connector_for_server(server_id: str, *, redact: bool = True) -> Optional[Dict[str, Any]]:
    with _LOCK:
        data = _load()
        for entry in data["connectors"]:
            if entry.get("server_id") == server_id:
                out = dict(entry)
                break
        else:
            return None
    if redact:
        out["values"] = _redact_values(out.get("values") or {})
    return out


def create_connector(
    *, preset_id: str, server_id: str, owner: Optional[str],
    values: Dict[str, Any], app_url: str, ui_url: Optional[str],
    launch_profile_id: Optional[str] = None,
) -> Dict[str, Any]:
    now = time.time()
    entry = {
        "id": str(uuid.uuid4()),
        "preset_id": preset_id,
        "server_id": server_id,
        "owner": owner or None,
        "values": dict(values or {}),
        "app_url": app_url,
        "ui_url": ui_url,
        "launch_profile_id": launch_profile_id,
        "created_at": now,
        "updated_at": now,
    }
    with _LOCK:
        data = _load()
        data["connectors"].append(entry)
        _save(data)
    return dict(entry)


def find_duplicate(preset_id: str, owner: Optional[str], app_url: str) -> Optional[Dict[str, Any]]:
    """A sidecar entry for the same preset+owner+app_url — F1.5's 409 case."""
    with _LOCK:
        data = _load()
        for entry in data["connectors"]:
            if (entry.get("preset_id") == preset_id
                    and (entry.get("owner") or None) == (owner or None)
                    and entry.get("app_url") == app_url):
                return dict(entry)
    return None


def update_connector(connector_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
    """Merge `fields` into the stored entry. Never raises on an unknown id —
    the caller (a route) turns a `None` return into its own 404."""
    with _LOCK:
        data = _load()
        for entry in data["connectors"]:
            if entry.get("id") == connector_id:
                entry.update(fields)
                entry["updated_at"] = time.time()
                _save(data)
                return dict(entry)
    return None


def delete_connector(connector_id: str) -> bool:
    with _LOCK:
        data = _load()
        before = len(data["connectors"])
        data["connectors"] = [e for e in data["connectors"] if e.get("id") != connector_id]
        if len(data["connectors"]) == before:
            return False
        _save(data)
    return True
