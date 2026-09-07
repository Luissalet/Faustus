"""Explicit owner-scoped script credentials, encrypted outside workflow JSON.

Workflows store names only. Approvals bind opaque revisions; plaintext is
resolved only after permission checks and passed to the container's explicit
environment injection. This store never imports ambient environment credentials.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from pathlib import Path

from core.atomic_io import atomic_write_json
from core.kernel_file_lock import KernelFileLock
from src.constants import DATA_DIR
from src.secret_storage import encrypt, decrypt

STORE_PATH = Path(DATA_DIR) / "workflow_credentials.json"


def _key(owner, name):
    if not isinstance(owner, str) or not owner or len(owner) > 256:
        raise ValueError("workflow credential requires an owner")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name):
        raise ValueError("credential name must contain 1-64 letters, digits, dots, dashes or underscores")
    return hashlib.sha256(json.dumps([owner, name]).encode()).hexdigest()


def _load():
    try:
        data = json.loads(Path(STORE_PATH).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (ValueError, OSError):
        raise ValueError("workflow credential store is unreadable") from None
    if not isinstance(data, dict) or any(not isinstance(row, dict) for row in data.values()):
        raise ValueError("workflow credential store is malformed")
    return data


def put(owner, name, value, *, expected_revision=None):
    key = _key(owner, name)
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 65536 or any(c in value for c in "\x00\r\n"):
        raise ValueError("credential value must be a nonempty single line of at most 64 KiB")
    with KernelFileLock(str(STORE_PATH) + ".lock"):
        data = _load()
        previous = data.get(key)
        if previous and expected_revision != previous.get("revision"):
            raise ValueError("credential changed; supply its current revision to replace it")
        if not previous and expected_revision:
            raise ValueError("credential no longer exists")
        if not previous and sum(1 for row in data.values() if row.get("owner") == owner) >= 256:
            raise ValueError("owner credential limit reached")
        revision = uuid.uuid4().hex
        token = encrypt(json.dumps({"owner": owner, "name": name, "revision": revision, "value": value}))
        data[key] = {"owner": owner, "name": name, "revision": revision, "encrypted": token}
        atomic_write_json(str(STORE_PATH), data, indent=2, private=True)
        return {"name": name, "revision": revision}


def list_metadata(owner):
    _key(owner, "validate")
    with KernelFileLock(str(STORE_PATH) + ".lock"):
        return [{"name": row["name"], "revision": row["revision"]}
                for row in _load().values() if row.get("owner") == owner]


def remove(owner, name, *, expected_revision):
    key = _key(owner, name)
    with KernelFileLock(str(STORE_PATH) + ".lock"):
        data = _load()
        row = data.get(key)
        if not row:
            return False
        if row.get("revision") != expected_revision:
            raise ValueError("credential changed; reload before deleting")
        del data[key]
        atomic_write_json(str(STORE_PATH), data, indent=2, private=True)
        return True


def bind(owner, declared, bindings, *, reveal=False, expected=None):
    """Snapshot name/revision bindings, or resolve that exact approved snapshot."""
    if not isinstance(bindings, dict) or set(bindings) != set(declared):
        raise ValueError("secret_bindings must match exactly the skill's declared secret names")
    if any(not isinstance(slot, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", slot) for slot in bindings):
        raise ValueError("declared secrets must be environment variable names")
    if not bindings:
        return {}
    result = {}
    with KernelFileLock(str(STORE_PATH) + ".lock"):
        data = _load()
        for slot, name in sorted(bindings.items()):
            row = data.get(_key(owner, name))
            if not isinstance(row, dict) or row.get("owner") != owner or row.get("name") != name:
                raise ValueError("a bound workflow credential is unavailable for this owner")
            metadata = {"name": name, "revision": row.get("revision")}
            if reveal:
                if not expected or expected.get(slot) != metadata:
                    raise ValueError("bound credential changed since approval")
                try:
                    token = row.get("encrypted", "")
                    if not isinstance(token, str) or not token.startswith("enc:"):
                        raise ValueError()
                    envelope = json.loads(decrypt(token))
                    if (envelope.get("owner"), envelope.get("name"), envelope.get("revision")) != (owner, name, metadata["revision"]):
                        raise ValueError()
                    value = envelope["value"]
                    if not isinstance(value, str) or not value:
                        raise ValueError()
                except (ValueError, TypeError, KeyError, AttributeError):
                    raise ValueError("bound workflow credential cannot be decrypted") from None
                result[slot] = value
            else:
                result[slot] = metadata
    return result
