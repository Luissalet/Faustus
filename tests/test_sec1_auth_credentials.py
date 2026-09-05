"""SEC-1 · B-020: what auth persists must not be usable as a credential.

`sessions.json` used the bearer token itself as the dictionary key, and
`auth.json` kept the TOTP seed and all eight recovery codes in the clear and
compared the codes the same way. Reading either file was enough to walk in.
These tests plant a real token/secret and then read the file back: the check
is always "the value is not in there", never "the code looks careful".
"""

import json
import os
import subprocess

import pyotp
import pytest

from core.auth import SESSIONS_FORMAT_VERSION, AuthManager, _digest
from core.platform_compat import IS_WINDOWS


def assert_owner_only(path):
    if IS_WINDOWS:
        acl = subprocess.run(["icacls", str(path)], capture_output=True, text=True).stdout
        assert "(I)" not in acl, acl
        assert os.environ.get("USERNAME", "?").lower() in acl.lower(), acl
    else:
        assert os.stat(path).st_mode & 0o077 == 0


@pytest.fixture
def manager(tmp_path):
    mgr = AuthManager(str(tmp_path / "auth.json"))
    mgr.setup("admin", "correct horse battery staple")
    return mgr


def _sessions_raw(manager):
    return open(manager._sessions_path, encoding="utf-8").read()


# ── sessions ──────────────────────────────────────────────────────────────

def test_the_session_token_is_never_written_to_disk(manager):
    token = manager.create_session("admin", "correct horse battery staple")
    assert token
    raw = _sessions_raw(manager)
    assert token not in raw
    assert _digest(token) in raw
    assert manager.validate_token(token) is True
    assert manager.get_username_for_token(token) == "admin"


def test_sessions_file_is_versioned_and_owner_only(manager):
    manager.create_session("admin", "correct horse battery staple")
    data = json.loads(_sessions_raw(manager))
    assert data["version"] == SESSIONS_FORMAT_VERSION
    assert list(data["sessions"])  # one digest key
    assert_owner_only(manager._sessions_path)


def test_auth_file_is_owner_only(manager):
    assert_owner_only(manager.auth_path)


def test_a_pre_sec1_sessions_file_is_invalidated(tmp_path):
    """The old format is a ring of live tokens. It cannot be converted."""
    auth_path = tmp_path / "auth.json"
    AuthManager(str(auth_path)).setup("admin", "correct horse battery staple")
    legacy_token = "b" * 64
    (tmp_path / "sessions.json").write_text(
        json.dumps({legacy_token: {"username": "admin", "expiry": 9e12}}),
        encoding="utf-8",
    )

    mgr = AuthManager(str(auth_path))

    assert mgr.validate_token(legacy_token) is False
    rewritten = json.loads((tmp_path / "sessions.json").read_text(encoding="utf-8"))
    assert rewritten == {"version": SESSIONS_FORMAT_VERSION, "sessions": {}}


def test_revoking_and_selective_revoking_still_work(manager):
    keep = manager.create_session("admin", "correct horse battery staple")
    drop = manager.create_session("admin", "correct horse battery staple")

    manager.revoke_token(drop)
    assert manager.validate_token(drop) is False
    assert manager.validate_token(keep) is True

    again = manager.create_session("admin", "correct horse battery staple")
    revoked = manager.revoke_user_sessions("admin", except_token=again)
    assert revoked == 1  # `keep`; `drop` was already gone
    assert manager.validate_token(again) is True
    assert manager.validate_token(keep) is False


# ── second factor ─────────────────────────────────────────────────────────

def _enable_2fa(manager, username="admin"):
    secret = manager.totp_generate_secret(username)
    codes = manager.totp_confirm_enable(username, pyotp.TOTP(secret).now())
    return secret, codes


def _auth_raw(manager):
    return open(manager.auth_path, encoding="utf-8").read()


def test_the_totp_seed_is_not_stored_in_the_clear(manager):
    secret, _ = _enable_2fa(manager)
    raw = _auth_raw(manager)
    assert secret not in raw
    assert '"totp_secret": "enc:' in raw
    # and it still verifies
    assert manager.totp_verify("admin", pyotp.TOTP(secret).now()) is True


def test_recovery_codes_are_returned_once_and_stored_hashed(manager):
    _, codes = _enable_2fa(manager)
    assert len(codes) == 8
    raw = _auth_raw(manager)
    for code in codes:
        assert code not in raw
    assert all(_digest(c) in raw for c in codes)
    assert "totp_backup_codes" not in json.loads(raw)["users"]["admin"]


def test_a_recovery_code_works_once_and_only_once(manager):
    _, codes = _enable_2fa(manager)
    assert manager.totp_verify("admin", codes[0]) is True
    assert manager.totp_verify("admin", codes[0]) is False
    assert manager.totp_verify("admin", codes[1]) is True
    remaining = json.loads(_auth_raw(manager))["users"]["admin"]["totp_backup_code_hashes"]
    assert len(remaining) == 6


def test_a_wrong_second_factor_is_refused(manager):
    _enable_2fa(manager)
    assert manager.totp_verify("admin", "000000") is False
    assert manager.totp_verify("admin", "not-a-code") is False


def test_confirm_returns_none_for_a_bad_code(manager):
    manager.totp_generate_secret("admin")
    assert manager.totp_confirm_enable("admin", "000000") is None
    assert manager.totp_enabled("admin") is False


# ── upgrading an install that predates SEC-1 ──────────────────────────────

def _legacy_auth_file(tmp_path, secret, codes):
    import bcrypt

    path = tmp_path / "auth.json"
    path.write_text(json.dumps({"users": {"admin": {
        "password_hash": bcrypt.hashpw(b"pw", bcrypt.gensalt()).decode(),
        "is_admin": True,
        "totp_enabled": True,
        "totp_secret": secret,
        "totp_backup_codes": codes,
    }}}), encoding="utf-8")
    return path


def test_a_legacy_auth_file_is_migrated_on_load(tmp_path):
    secret = pyotp.random_base32()
    path = _legacy_auth_file(tmp_path, secret, ["aaaa1111", "bbbb2222"])

    mgr = AuthManager(str(path))

    raw = path.read_text(encoding="utf-8")
    assert secret not in raw
    assert "aaaa1111" not in raw
    assert "totp_backup_codes" not in json.loads(raw)["users"]["admin"]
    # The second factor keeps working across the migration, with both the
    # authenticator and the codes the user already wrote down.
    assert mgr.totp_verify("admin", pyotp.TOTP(secret).now()) is True
    assert mgr.totp_verify("admin", "aaaa1111") is True
    assert mgr.totp_verify("admin", "aaaa1111") is False


def test_the_migration_is_idempotent(tmp_path):
    secret = pyotp.random_base32()
    path = _legacy_auth_file(tmp_path, secret, ["cccc3333"])
    AuthManager(str(path))
    first = path.read_text(encoding="utf-8")

    AuthManager(str(path))

    assert path.read_text(encoding="utf-8") == first
