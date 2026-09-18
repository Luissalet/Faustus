"""SEC-1 · B-010: a backup stops being a credential bundle.

The old snapshot put `.app_key` and everything that key protects into one
unencrypted tarball, together with `auth.json` (TOTP material) and
`sessions.json` (live sessions) — and that file is precisely the one people
copy to a NAS or a cloud folder. Reading it was enough to become the owner.

Two profiles now: `content`, which carries no credentials, and `full`, which
carries all of them and is therefore encrypted with a passphrase that is never
written into the archive. The tests plant sentinel credentials in a fake data
directory and then read the bytes back.
"""

import json
import os
import subprocess
import tarfile

import pytest

from core.platform_compat import IS_WINDOWS
from src import backup_crypto, backup_service

APP_KEY = "sentinel-app-key-do-not-back-up-in-the-clear"
TOTP = "sentinel-totp-seed"
PASSPHRASE = "correct horse battery staple"


@pytest.fixture
def install(tmp_path, monkeypatch):
    """A data/ directory with credentials in it, and a backups/ beside it."""
    data = tmp_path / "data"
    (data / "mcp_oauth" / "gmail").mkdir(parents=True)
    (data / "chats").mkdir()
    (data / ".app_key").write_text(APP_KEY, encoding="utf-8")
    (data / "auth.json").write_text(json.dumps({"users": {"admin": {"totp_secret": TOTP}}}),
                                    encoding="utf-8")
    (data / "sessions.json").write_text(json.dumps({"version": 2, "sessions": {}}), encoding="utf-8")
    (data / "vault.json").write_text('{"BW_SESSION": "sentinel-bw"}', encoding="utf-8")
    (data / "integrations.json").write_text('{"imap": "sentinel-imap"}', encoding="utf-8")
    (data / "mcp_oauth" / "gmail" / "credentials.json").write_text(
        '{"client_secret": "sentinel-oauth"}', encoding="utf-8")
    (data / "settings.json").write_text('{"theme": "dark"}', encoding="utf-8")
    (data / "chats" / "one.md").write_text("a conversation worth keeping", encoding="utf-8")

    backups = tmp_path / "backups"
    monkeypatch.setattr(backup_service, "data_dir", lambda: data)
    monkeypatch.setattr(backup_service, "backup_dir", lambda: backups)
    monkeypatch.delenv("FAUSTUS_BACKUP_PASSPHRASE", raising=False)
    monkeypatch.delenv("ODYSSEUS_BACKUP_PASSPHRASE", raising=False)
    return {"data": data, "backups": backups}


def _names(path, passphrase=None):
    if backup_crypto.is_encrypted(path):
        with open(path, "rb") as fh, backup_crypto.DecryptingReader(fh, passphrase) as reader:
            with tarfile.open(fileobj=reader, mode="r|gz") as tar:
                return [m.name for m in tar]
    with tarfile.open(path, "r:gz") as tar:
        return tar.getnames()


# ── the content profile ───────────────────────────────────────────────────

def test_the_default_snapshot_carries_no_credentials(install):
    out = backup_service.snapshot()
    assert out["ok"] is True, out
    assert out["profile"] == "content"
    assert out["encrypted"] is False

    path = install["backups"] / out["name"]
    names = _names(path)
    assert "data/chats/one.md" in names            # the content is there
    assert "data/settings.json" in names
    for secret in ("data/.app_key", "data/auth.json", "data/sessions.json",
                   "data/vault.json", "data/integrations.json",
                   "data/mcp_oauth/gmail/credentials.json"):
        assert secret not in names, f"{secret} is in a content snapshot"
    assert set(out["excluded"]) >= {".app_key", "auth.json", "sessions.json"}


def test_no_sentinel_survives_in_the_bytes_of_a_content_snapshot(install):
    out = backup_service.snapshot()
    blob = (install["backups"] / out["name"]).read_bytes()
    for sentinel in (APP_KEY, TOTP, "sentinel-bw", "sentinel-imap", "sentinel-oauth"):
        assert sentinel.encode() not in blob


# ── the full profile ──────────────────────────────────────────────────────

def test_a_full_snapshot_without_a_passphrase_is_refused_not_written(install):
    out = backup_service.snapshot(profile="full")
    assert out["ok"] is False
    assert "encrypted" in out["error"]
    assert list(install["backups"].glob("*.tar.gz*")) == []


def test_a_full_snapshot_is_encrypted_and_complete(install):
    out = backup_service.snapshot(profile="full", passphrase=PASSPHRASE)
    assert out["ok"] is True, out
    assert out["encrypted"] is True and out["name"].endswith(".tar.gz.enc")

    path = install["backups"] / out["name"]
    blob = path.read_bytes()
    assert APP_KEY.encode() not in blob          # not readable without the key
    assert TOTP.encode() not in blob
    names = _names(path, PASSPHRASE)
    assert "data/.app_key" in names               # but it IS in there
    assert "data/auth.json" in names


def test_the_passphrase_can_come_from_the_environment(install, monkeypatch):
    monkeypatch.setenv("FAUSTUS_BACKUP_PASSPHRASE", PASSPHRASE)
    out = backup_service.snapshot(profile="full")
    assert out["ok"] is True and out["encrypted"] is True


def test_a_wrong_passphrase_does_not_open_it(install):
    out = backup_service.snapshot(profile="full", passphrase=PASSPHRASE)
    path = install["backups"] / out["name"]
    with pytest.raises(backup_crypto.BackupCryptoError):
        _names(path, "not the passphrase")


# ── the file on disk ──────────────────────────────────────────────────────

def test_the_snapshot_is_owner_only_and_leaves_no_part_file(install):
    out = backup_service.snapshot(profile="full", passphrase=PASSPHRASE)
    path = install["backups"] / out["name"]
    leftovers = [p.name for p in install["backups"].iterdir()
                 if ".part-" in p.name or ".plain-" in p.name]
    assert leftovers == []
    if IS_WINDOWS:
        acl = subprocess.run(["icacls", str(path)], capture_output=True, text=True).stdout
        assert "(I)" not in acl, acl
    else:
        assert os.stat(path).st_mode & 0o077 == 0


def test_two_snapshots_in_the_same_second_do_not_collide(install):
    first = backup_service.snapshot()
    second = backup_service.snapshot()
    assert first["ok"] and second["ok"]
    assert first["name"] != second["name"]


def test_a_held_lease_refuses_a_second_backup(install):
    install["backups"].mkdir(parents=True, exist_ok=True)
    lease = backup_service._Lease(install["backups"])
    lease.__enter__()
    try:
        out = backup_service.snapshot()
        assert out["ok"] is False and out.get("busy") is True
    finally:
        lease.__exit__(None, None, None)
    # released: the next one works
    assert backup_service.snapshot()["ok"] is True


# ── the manifest ──────────────────────────────────────────────────────────

def test_the_manifest_is_written_and_authenticates(install, monkeypatch):
    monkeypatch.setattr(backup_service, "_manifest_key", lambda: b"a key that is not in the archive")
    out = backup_service.snapshot()
    path = install["backups"] / out["name"]
    checked = backup_service.read_manifest(path)
    assert checked["present"] is True
    assert checked["authenticated"] is True, checked
    assert checked["manifest"]["profile"] == "content"
    assert checked["manifest"]["sha256"]


def test_an_edited_manifest_stops_authenticating(install, monkeypatch):
    monkeypatch.setattr(backup_service, "_manifest_key", lambda: b"a key that is not in the archive")
    out = backup_service.snapshot()
    path = install["backups"] / out["name"]
    side = path.with_name(path.name + ".manifest.json")
    data = json.loads(side.read_text(encoding="utf-8"))
    data["files"] = 99999
    side.write_text(json.dumps(data), encoding="utf-8")

    checked = backup_service.read_manifest(path)
    assert checked["authenticated"] is False
    assert "HMAC" in checked["problem"]


# ── verification, including through the encryption ────────────────────────

def test_verify_reads_a_content_snapshot(install):
    out = backup_service.snapshot(verify=True)
    assert out["verified"]["ok"] is True, out["verified"]
    assert out["verified"]["members"] > 0


def test_verify_reads_an_encrypted_snapshot(install):
    out = backup_service.snapshot(profile="full", passphrase=PASSPHRASE, verify=True)
    assert out["ok"] is True, out
    report = backup_service.verify_archive(install["backups"] / out["name"], passphrase=PASSPHRASE)
    assert report["ok"] is True, report
    assert report["encrypted"] is True


def test_verify_says_so_when_it_has_no_passphrase(install):
    out = backup_service.snapshot(profile="full", passphrase=PASSPHRASE)
    report = backup_service.verify_archive(install["backups"] / out["name"])
    assert report["ok"] is False
    assert any("passphrase" in p for p in report["problems"])


def test_a_tampered_encrypted_snapshot_does_not_verify(install):
    out = backup_service.snapshot(profile="full", passphrase=PASSPHRASE)
    path = install["backups"] / out["name"]
    blob = bytearray(path.read_bytes())
    blob[-1] ^= 0x01                       # one bit, in the last chunk's tag
    path.write_bytes(bytes(blob))

    report = backup_service.verify_archive(path, passphrase=PASSPHRASE, check_hash=False)
    assert report["ok"] is False
    assert report["problems"]


def test_a_truncated_encrypted_snapshot_does_not_verify(install):
    out = backup_service.snapshot(profile="full", passphrase=PASSPHRASE)
    path = install["backups"] / out["name"]
    blob = path.read_bytes()
    path.write_bytes(blob[: len(blob) // 2])

    report = backup_service.verify_archive(path, passphrase=PASSPHRASE, check_hash=False)
    assert report["ok"] is False


def test_verify_notices_the_archive_does_not_match_its_manifest(install):
    out = backup_service.snapshot()
    path = install["backups"] / out["name"]
    path.write_bytes(path.read_bytes() + b"appended junk")

    report = backup_service.verify_archive(path)
    assert report["ok"] is False
    assert any("hash" in p for p in report["problems"])


# ── the round trip ────────────────────────────────────────────────────────

def test_an_encrypted_snapshot_decrypts_back_to_the_same_bytes(install, tmp_path):
    out = backup_service.snapshot(profile="full", passphrase=PASSPHRASE)
    path = install["backups"] / out["name"]
    plain = tmp_path / "restored.tar.gz"
    backup_crypto.decrypt_file(path, plain, PASSPHRASE)

    with tarfile.open(plain, "r:gz") as tar:
        names = tar.getnames()
        member = tar.extractfile("data/.app_key")
        assert member is not None and member.read().decode() == APP_KEY
    assert "data/chats/one.md" in names
