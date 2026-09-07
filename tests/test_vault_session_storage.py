"""Synthetic sessions only; never access the user's vault or app key."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet

from src import secret_storage, vault_storage as store


@pytest.fixture
def vault(tmp_path, monkeypatch):
    monkeypatch.setattr(secret_storage, "_fernet", Fernet(Fernet.generate_key()))
    return tmp_path / "vault.json"


def config(age=0):
    return {"server_url": "https://vault.example", "email": "test@example.com",
            "session": "synthetic-sensitive-session",
            "unlocked_at": (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat()}


def test_encrypted_round_trip_and_shared_route_tool_store(vault, monkeypatch):
    import routes.vault_routes as routes
    import src.tools.vault as tool
    monkeypatch.setattr(routes, "VAULT_FILE", vault)
    monkeypatch.setattr(tool, "VAULT_FILE", str(vault))
    cfg = config()
    routes._save_config(cfg)
    text = vault.read_text()
    assert cfg["session"] not in text
    assert cfg["unlocked_at"] not in text
    assert json.loads(text)["session_encrypted"].startswith("enc:")
    assert tool._load_vault_config() == cfg
    assert routes._load_config() == cfg


@pytest.mark.parametrize("age", [3601, -60])
def test_invalid_age_never_saved(vault, age):
    store.save_config(vault, config(age))
    assert "session" not in store.load_config(vault)
    assert "session_encrypted" not in json.loads(vault.read_text())


def test_expired_session_is_removed_without_losing_settings(vault, monkeypatch):
    store.save_config(vault, config())
    monkeypatch.setattr(store, "SESSION_TTL_SECONDS", 0)
    assert store.load_config(vault) == {"server_url": "https://vault.example", "email": "test@example.com"}
    assert "session_encrypted" not in json.loads(vault.read_text())


def test_legacy_plaintext_is_removed_and_requires_new_unlock(vault):
    vault.write_text(json.dumps(config()))
    assert "session" not in store.load_config(vault)
    assert "synthetic-sensitive-session" not in vault.read_text()
    assert json.loads(vault.read_text())["email"] == "test@example.com"


def test_wrong_key_locks_and_removes_token(vault, monkeypatch):
    store.save_config(vault, config())
    monkeypatch.setattr(secret_storage, "_fernet", Fernet(Fernet.generate_key()))
    assert "session" not in store.load_config(vault)
    assert "session_encrypted" not in json.loads(vault.read_text())


def test_account_change_invalidates_token(vault):
    store.save_config(vault, config())
    disk = json.loads(vault.read_text())
    disk["email"] = "different@example.com"
    vault.write_text(json.dumps(disk))
    assert "session" not in store.load_config(vault)


def test_settings_save_does_not_extend_expiry(vault):
    cfg = config(100)
    store.save_config(vault, cfg)
    loaded = store.load_config(vault)
    store.save_config(vault, loaded)
    assert store.load_config(vault)["unlocked_at"] == cfg["unlocked_at"]


def test_stale_settings_cannot_resurrect_locked_session(vault):
    store.save_config(vault, config())
    stale = store.load_config(vault)
    locked = store.load_config(vault)
    store.clear_session(locked)
    store.save_config(vault, locked)
    with pytest.raises(OSError, match="changed"):
        store.save_config(vault, stale)
    assert "session" not in store.load_config(vault)


def test_same_request_can_save_again_without_conflicting_with_itself(vault):
    cfg = store.load_config(vault)
    cfg.update(config())
    store.save_config(vault, cfg)
    store.save_config(vault, cfg)
    assert store.load_config(vault)["session"] == cfg["session"]


def test_failed_atomic_replace_keeps_old_file(vault, monkeypatch):
    store.save_config(vault, config())
    previous = vault.read_bytes()
    def fail(*args, **kwargs):
        raise OSError("synthetic failure")
    monkeypatch.setattr(store, "atomic_write_json", fail)
    with pytest.raises(OSError):
        store.save_config(vault, {})
    assert vault.read_bytes() == previous


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["login", "unlock"])
async def test_route_unlock_and_login_persist_encrypted_session(vault, monkeypatch, action):
    import routes.vault_routes as routes
    monkeypatch.setattr(routes, "VAULT_FILE", vault)
    monkeypatch.setattr(routes, "require_admin", lambda request: None)
    async def run(*args, **kwargs):
        return "synthetic-sensitive-session", "", 0
    monkeypatch.setattr(routes, "_run_bw", run)
    router = routes.setup_vault_routes()
    endpoint = next(route.endpoint for route in router.routes if route.path == "/api/vault/" + action)
    request_type = routes.VaultLoginRequest if action == "login" else routes.VaultUnlockRequest
    result = await endpoint(request_type(master_password="synthetic-password", email="test@example.com"), None)
    assert result["ok"] is True
    assert store.load_config(vault)["session"] == "synthetic-sensitive-session"
    assert "synthetic-sensitive-session" not in vault.read_text()


@pytest.mark.asyncio
@pytest.mark.parametrize("module", ["routes.vault_routes", "src.tools.vault"])
async def test_child_does_not_inherit_an_unapproved_session(monkeypatch, module):
    import importlib
    import asyncio
    implementation = importlib.import_module(module)
    monkeypatch.setenv("BW_SESSION", "ambient-session")
    monkeypatch.setenv("BW_PASSWORD", "ambient-password")
    captured = {}
    class Process:
        returncode = 0
        async def communicate(self, input=None):
            return b"", b""
    async def spawn(*args, **kwargs):
        captured.update(kwargs["env"])
        return Process()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await implementation._run_bw(["lock"])
    assert "BW_SESSION" not in captured
    assert "BW_PASSWORD" not in captured
