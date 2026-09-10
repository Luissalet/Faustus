"""
connector_registry.py — CONN-01: a connection center with declared scopes.

`src/integrations.py` is the existing, reused authority for a connector's
CREDENTIAL (its API key, base URL, auth headers) — this module does not
duplicate that store (rule 4); every call here reads or writes it through
`get_integration` / `delete_integration`. What it adds is the thing that
store has no column for: a per-connector SCOPE declaration (a token never
grants more than what is declared here, whatever the credential itself is
technically capable of), an owner, and the revocation event a connection
center needs to show.

Persisted separately, in its own JSON file next to `integrations.json` (same
`atomic_write_json` + `safe_chmod` pattern `src.integrations` itself uses).
`src.integrations`'s own schema has no `owner` column yet — that is a change
to a file this batch does not own (see the batch report's "ficheros
ajenos"); until it lands, ownership is enforced HERE, on top of a store that
is itself still global. `register()` refuses to attach a second owner to an
id another owner already registered, which is the practical version of
isolation available without that column.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

from core.atomic_io import atomic_write_json
from core.platform_compat import safe_chmod
from src.constants import DATA_DIR
from src.contracts.base import now_iso

log = logging.getLogger(__name__)

REGISTRY_FILE = os.path.join(DATA_DIR, "connector_registry.json")

#: The closed vocabulary a scope must come from. `read`/`write` map to HTTP
#: method families; `delete` is its own scope because an irreversible call
#: deserves a separate yes even when a connector's credential could do both
#: — the acceptance line this module exists for ("un token nunca da mas de
#: lo declarado") is only real if the vocabulary a scope can promise is
#: closed, not whatever string a caller happened to send.
SCOPES = ("read", "write", "delete")

_METHOD_SCOPE = {
    "GET": "read", "HEAD": "read", "OPTIONS": "read",
    "POST": "write", "PUT": "write", "PATCH": "write",
    "DELETE": "delete",
}

STATUSES = ("connected", "revoked")


class ScopeError(PermissionError):
    """A call asked for more than its connector's declared scopes allow."""


def _load() -> Dict[str, Dict[str, Any]]:
    if not os.path.exists(REGISTRY_FILE):
        return {}
    try:
        with open(REGISTRY_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return raw if isinstance(raw, dict) else {}
    except (json.JSONDecodeError, IOError) as exc:
        log.error("Failed to load connector registry: %s", exc)
        return {}


def _save(data: Dict[str, Dict[str, Any]]) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    atomic_write_json(REGISTRY_FILE, data, indent=2)
    safe_chmod(REGISTRY_FILE, 0o600)


def register(integration_id: str, *, owner: str, scopes: List[str]) -> Dict[str, Any]:
    """Declare (or replace) one connector's scopes and owner.

    Called once when a connection is made, and again any time the scopes
    are deliberately widened or narrowed — never silently: this is the one
    function that decides what `enforce_scope` will later allow, so every
    value in `scopes` is checked against the closed vocabulary first.
    """
    from src.integrations import get_integration
    if get_integration(integration_id) is None:
        raise ValueError(f"no integration {integration_id!r} to register")
    bad = [s for s in scopes if s not in SCOPES]
    if bad:
        raise ValueError(f"unknown scope(s) {bad!r}; must be one of {SCOPES}")
    data = _load()
    current = data.get(integration_id) or {}
    if current.get("owner") and owner and current["owner"] != owner:
        raise PermissionError(
            f"connector {integration_id!r} already belongs to another owner")
    entry = {
        "integration_id": integration_id,
        "owner": owner or current.get("owner", ""),
        "scopes": sorted(set(scopes)),
        "status": "connected",
        "registered_at": current.get("registered_at") or now_iso(),
        "last_synced_at": current.get("last_synced_at", ""),
        "revoked_at": "",
    }
    data[integration_id] = entry
    _save(data)
    return dict(entry)


def get(integration_id: str) -> Optional[Dict[str, Any]]:
    entry = _load().get(integration_id)
    return dict(entry) if entry is not None else None


def list_for_owner(owner: str) -> List[Dict[str, Any]]:
    return [dict(v) for v in _load().values() if v.get("owner") == owner]


def mark_synced(integration_id: str, *, when: str = "") -> Optional[Dict[str, Any]]:
    data = _load()
    entry = data.get(integration_id)
    if entry is None:
        return None
    entry["last_synced_at"] = when or now_iso()
    _save(data)
    return dict(entry)


def revoke(integration_id: str) -> Dict[str, Any]:
    """Revoke a connector: strip its credential from `src.integrations`
    (the store a caller actually authenticates against) AND record the
    revocation here, so the connection center can still show that it once
    existed and when it stopped.

    Deleting the credential — rather than only flagging it in this
    module's own file — is what makes the acceptance line true.
    `src.integrations.get_integration` reads its file fresh on every call
    (no in-process cache to go stale there), so a worker mid-run that asks
    for this connector again gets nothing the instant this function
    returns, never a cached answer from before the click.
    """
    from src.integrations import delete_integration
    data = _load()
    entry = data.get(integration_id) or {
        "integration_id": integration_id, "owner": "", "scopes": [],
        "status": "connected", "registered_at": "", "last_synced_at": "",
    }
    entry["status"] = "revoked"
    entry["revoked_at"] = now_iso()
    data[integration_id] = entry
    _save(data)
    delete_integration(integration_id)
    return dict(entry)


def effective_scopes(integration_id: str) -> Dict[str, Any]:
    """What this connector can actually do right now — the "ver permisos
    efectivos" a connection center needs, computed fresh rather than
    trusting whatever was merely requested at connect time."""
    entry = get(integration_id)
    from src.integrations import get_integration
    credential_present = get_integration(integration_id) is not None
    if entry is None:
        # Never registered through this module: back-compat with every
        # integration `src.integrations` already had before this batch —
        # nothing here restricts it, exactly as before (rule 3: no
        # capability lost). `unscoped=True` says so explicitly rather than
        # pretending an empty scope list was ever declared.
        return {"integration_id": integration_id, "owner": "", "scopes": [],
                "unscoped": True,
                "status": "connected" if credential_present else "revoked",
                "last_synced_at": "", "revoked_at": ""}
    status = entry["status"] if credential_present else "revoked"
    return {"integration_id": integration_id, "owner": entry.get("owner", ""),
            "scopes": list(entry.get("scopes", [])), "unscoped": False,
            "status": status, "last_synced_at": entry.get("last_synced_at", ""),
            "revoked_at": entry.get("revoked_at", "")}


def enforce_scope(integration_id: str, method: str) -> None:
    """Raise `ScopeError` when `method` needs a scope this connector never
    declared, or when it was revoked. A connector never registered here
    (`unscoped`) is left exactly as capable as it always was — this module
    only NARROWS, on request; it never widens what
    `src.integrations.execute_api_call` would already allow.
    """
    needed = _METHOD_SCOPE.get(method.upper(), "write")
    info = effective_scopes(integration_id)
    if info["status"] == "revoked":
        raise ScopeError(f"connector {integration_id!r} is revoked")
    if info["unscoped"]:
        return
    if needed not in info["scopes"]:
        raise ScopeError(
            f"connector {integration_id!r} is not scoped for {needed!r} "
            f"(declared: {info['scopes']})")
