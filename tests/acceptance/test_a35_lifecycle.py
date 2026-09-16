"""A35 — clean install/upgrade/backup/restore/uninstall lifecycle
(docs/spec/distribution/LIFECYCLE.md, scripts/lifecycle_check.py,
scripts/uninstall.sh, scripts/uninstall.ps1).

Real code under test: ``scripts.lifecycle_check`` against a real, throwaway
``DATA_DIR`` under ``tmp_path`` — real files, a real zip archive, real
sha256 verification, real filesystem deletion for the "gone" half of the
round trip. No mocks. Clean install/upgrade on an actual machine is out of
reach of this test suite (no clean machine available) and is documented,
not simulated, in LIFECYCLE.md.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import lifecycle_check
from tests.acceptance.conftest import record_evidence

REPO_ROOT = Path(__file__).resolve().parents[2]


def _seed_data_dir(data_dir: Path) -> dict:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "settings.json").write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
    sub = data_dir / "sessions"
    sub.mkdir()
    (sub / "s1.json").write_text(json.dumps({"id": "s1", "history": ["hi"]}), encoding="utf-8")
    (data_dir / "note.txt").write_bytes(b"some binary-ish \x00\x01 content")
    return {
        "settings.json": (data_dir / "settings.json").read_bytes(),
        "sessions/s1.json": (sub / "s1.json").read_bytes(),
        "note.txt": (data_dir / "note.txt").read_bytes(),
    }


@pytest.mark.acceptance("A35")
def test_backup_then_restore_round_trip_preserves_user_data_bit_for_bit(request, tmp_path):
    data_dir = tmp_path / "DATA_DIR"
    original = _seed_data_dir(data_dir)

    backup_zip = tmp_path / "backup.zip"
    manifest = lifecycle_check.backup(data_dir, backup_zip)
    assert set(manifest["files"]) == set(original)
    assert backup_zip.is_file()

    # "delete" the install: the whole data dir is gone, exactly what an
    # uninstall --purge or a lost machine would leave behind.
    import shutil
    shutil.rmtree(data_dir)
    assert not data_dir.exists()

    restore_target = tmp_path / "DATA_DIR"  # same path, now empty
    result = lifecycle_check.restore(backup_zip, restore_target, overwrite=False)
    assert result["all_verified"] is True

    for rel, original_bytes in original.items():
        restored_bytes = (restore_target / rel).read_bytes()
        assert restored_bytes == original_bytes, f"{rel} did not restore bit-for-bit"

    record_evidence(
        request, data_dir=str(data_dir), backup_zip=str(backup_zip),
        files_restored=len(result["files"]), all_verified=result["all_verified"],
    )


@pytest.mark.acceptance("A35")
def test_restore_refuses_to_silently_overwrite_a_non_empty_data_dir(request, tmp_path):
    data_dir = tmp_path / "DATA_DIR"
    _seed_data_dir(data_dir)
    backup_zip = tmp_path / "backup.zip"
    lifecycle_check.backup(data_dir, backup_zip)

    # data_dir still has its original files (never deleted this time) —
    # restoring into it without --overwrite must refuse, not clobber.
    (data_dir / "settings.json").write_text(json.dumps({"theme": "light", "edited": True}), encoding="utf-8")
    with pytest.raises(RuntimeError):
        lifecycle_check.restore(backup_zip, data_dir, overwrite=False)
    # Confirm nothing was touched by the refused restore.
    assert json.loads((data_dir / "settings.json").read_text(encoding="utf-8"))["edited"] is True

    record_evidence(request, data_dir=str(data_dir))


@pytest.mark.acceptance("A35")
def test_uninstall_scripts_exist_and_never_delete_data_without_purge(request):
    results = lifecycle_check.check_uninstall_scripts(REPO_ROOT)
    assert set(results) == {"scripts/uninstall.sh", "scripts/uninstall.ps1"}
    for rel, entry in results.items():
        assert entry["exists"], f"{rel} is documented in LIFECYCLE.md but missing from the tree"
        assert entry["deletes_user_data"] is True, (
            f"{rel} never deletes data/.env/backups at all — sanity check on the test itself"
        )
        assert entry["guarded_by_purge_flag"] is True, (
            f"{rel} deletes user data without a --purge/-Purge guard"
        )
        assert entry["safe"] is True

    # Static source check: the default (no-flag) code path never reaches
    # the deletion — the deleting lines sit inside an if/guard block.
    sh_text = (REPO_ROOT / "scripts" / "uninstall.sh").read_text(encoding="utf-8")
    assert 'if [ "$PURGE" -eq 1 ]' in sh_text
    ps1_text = (REPO_ROOT / "scripts" / "uninstall.ps1").read_text(encoding="utf-8")
    assert "if ($Purge)" in ps1_text

    record_evidence(request, checked=list(results))
