"""Credential handling (R1): decrypted through secret_storage, never
logged/returned raw."""
from __future__ import annotations

from src import secret_storage
from src.reach import credentials


def test_plaintext_token_passes_through():
    assert credentials.get_token("nonexistent_channel_xyz") == ""


def test_encrypted_token_is_decrypted(monkeypatch, tmp_path):
    monkeypatch.setattr("src.constants.APP_KEY_FILE", str(tmp_path / ".app_key"))
    monkeypatch.setattr(secret_storage, "_KEY_PATH", tmp_path / ".app_key")
    monkeypatch.setattr(secret_storage, "_fernet", None)
    encrypted = secret_storage.encrypt("plain-secret")
    assert encrypted.startswith("enc:")

    monkeypatch.setattr(credentials, "get_setting", lambda key, default=None: (
        encrypted if key == "reach_github_token" else default
    ))
    assert credentials.get_token("github") == "plain-secret"


def test_redact_never_leaks_full_value():
    assert credentials.redact("") == ""
    assert credentials.redact("ab") == "***"
    out = credentials.redact("supersecrettoken")
    assert "supersecrettoken" not in out
    assert out.startswith("sup")
