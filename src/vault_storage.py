"""Encrypted, expiring Bitwarden sessions shared by routes and agent tools.

The app encryption key must remain private. This protects the configuration
file, not a compromised host. Legacy plaintext sessions are discarded.
"""
from __future__ import annotations

import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path

from core.atomic_io import atomic_write_json
from core.kernel_file_lock import KernelFileLock
from src.secret_storage import decrypt, encrypt, is_encrypted

SESSION_TTL_SECONDS = 3600


class VaultConfig(dict):
    """Attach optimistic concurrency metadata without persisting or exposing it."""
    def __init__(self, values, revision):
        super().__init__(values)
        self.revision = revision


def _revision(path):
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


def _timestamp(value):
    try:
        dt = datetime.fromisoformat(value)
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except (TypeError, ValueError):
        return None


def _active(issued):
    dt = _timestamp(issued)
    return dt is not None and 0 <= (datetime.now(timezone.utc) - dt).total_seconds() < SESSION_TTL_SECONDS


def _identity(cfg):
    return [cfg.get("server_url", ""), cfg.get("email", "")]


def clear_session(cfg):
    for key in ("session", "session_encrypted", "unlocked_at", "expires_at"):
        cfg.pop(key, None)


def load_config(path) -> dict:
    path = Path(path)
    if not path.exists():
        return VaultConfig({}, None)
    with KernelFileLock(str(path) + ".lock"):
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}
        if not isinstance(cfg, dict):
            return {}
        token = cfg.get("session_encrypted")
        envelope = {}
        if isinstance(token, str) and is_encrypted(token):
            try:
                envelope = json.loads(decrypt(token))
            except (TypeError, ValueError):
                pass
        valid = (isinstance(envelope, dict)
                 and isinstance(envelope.get("session"), str)
                 and bool(envelope.get("session"))
                 and envelope.get("identity") == _identity(cfg)
                 and _active(envelope.get("unlocked_at")))
        dirty = any(key in cfg for key in ("session", "unlocked_at", "expires_at"))
        clear_session(cfg)
        if valid:
            cfg["session_encrypted"] = token
        if dirty or (token is not None and not valid):
            atomic_write_json(str(path), cfg, indent=2, private=True)
        cfg.pop("session_encrypted", None)
        if valid:
            cfg["session"] = envelope["session"]
            cfg["unlocked_at"] = envelope["unlocked_at"]
        return VaultConfig(cfg, _revision(path))


def save_config(path, cfg: dict) -> None:
    stored = dict(cfg)
    clear_session(stored)
    session = cfg.get("session")
    issued = cfg.get("unlocked_at")
    if isinstance(session, str) and session and _active(issued):
        # Encrypt the timestamp too: editing JSON cannot extend the lease.
        stored["session_encrypted"] = encrypt(json.dumps({
            "session": session, "unlocked_at": issued, "identity": _identity(cfg),
        }))
    path = Path(path)
    with KernelFileLock(str(path) + ".lock"):
        # A settings save/unlock started before another request locked the
        # vault must not resurrect the revoked session or overwrite its state.
        if isinstance(cfg, VaultConfig) and cfg.revision != _revision(path):
            raise OSError("Vault changed during this request; retry the operation")
        atomic_write_json(str(path), stored, indent=2, private=True)
        if isinstance(cfg, VaultConfig):
            cfg.revision = _revision(path)
