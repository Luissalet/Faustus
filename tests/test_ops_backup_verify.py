"""OPS-03 — a backup that PROVES it restores: `backup_service.verify_backup`
actually extracts the archive's databases to a throwaway directory, opens
them, counts every table's rows, and compares that against the manifest
recorded at snapshot time — not just an integrity_check of a tarball nobody
has restored.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from src import backup_service as bs


@pytest.fixture()
def seeded_install(tmp_path, monkeypatch):
    """Patches `src.constants.DATA_DIR` directly, not the `ODYSSEUS_DATA_DIR`
    env var: that constant is read once, at `src.constants` import time —
    which has usually already happened by the time a test fixture runs — so
    setting the env var here would silently do nothing and `bs.snapshot()`
    would tar this machine's real data directory instead of the fixture's.
    """
    from src import constants
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(constants, "DATA_DIR", str(data_dir))
    monkeypatch.setenv("FAUSTUS_BACKUP_DIR", str(tmp_path / "backups"))
    conn = sqlite3.connect(data_dir / "app.db")
    conn.execute("CREATE TABLE widgets (id INTEGER PRIMARY KEY, name TEXT)")
    conn.executemany("INSERT INTO widgets (name) VALUES (?)", [("a",), ("b",), ("c",)])
    conn.commit()
    conn.close()
    return data_dir


def test_snapshot_records_row_counts_in_its_manifest(seeded_install):
    result = bs.snapshot(profile=bs.PROFILE_CONTENT, verify=False)
    assert result["ok"], result
    manifest = json.loads(Path(result["path"] + ".manifest.json").read_text())
    counts = manifest["db_row_counts"]["data/app.db"]
    assert counts["widgets"] == 3


def test_verify_backup_confirms_a_good_snapshot_restores_cleanly(seeded_install):
    result = bs.snapshot(profile=bs.PROFILE_CONTENT, verify=False)
    report = bs.verify_backup(result["path"])
    assert report["ok"], report
    assert report["restore_check"]["performed"] is True
    db_report = report["restore_check"]["databases"][0]
    assert db_report["ok"]
    assert db_report["tables"]["widgets"] == 3
    assert db_report["compared_to_manifest"] is True


def test_verify_backup_catches_a_row_count_mismatch_against_the_manifest(seeded_install):
    """The scenario `verify_archive` alone cannot catch: the archive is a
    perfectly valid tarball, opens fine, integrity_check passes — but what
    it restores to does not match what the manifest promised was backed up.
    """
    result = bs.snapshot(profile=bs.PROFILE_CONTENT, verify=False)
    manifest_path = Path(result["path"] + ".manifest.json")
    manifest = json.loads(manifest_path.read_text())
    manifest["db_row_counts"]["data/app.db"]["widgets"] = 999
    manifest_path.write_text(json.dumps(manifest))

    report = bs.verify_backup(result["path"])
    assert not report["ok"]
    mismatches = report["restore_check"]["databases"][0]["mismatches"]
    assert any(m["table"] == "widgets" and m["expected"] == 999 for m in mismatches)


def test_verify_backup_tolerates_a_manifest_with_no_row_counts(seeded_install):
    """A snapshot taken before this change has no `db_row_counts` at all —
    that must be reported as "not compared", never as a false failure."""
    result = bs.snapshot(profile=bs.PROFILE_CONTENT, verify=False)
    manifest_path = Path(result["path"] + ".manifest.json")
    manifest = json.loads(manifest_path.read_text())
    manifest.pop("db_row_counts", None)
    manifest_path.write_text(json.dumps(manifest))

    report = bs.verify_backup(result["path"])
    assert report["ok"], report
    db_report = report["restore_check"]["databases"][0]
    assert db_report["compared_to_manifest"] is False


# ── proof: integrity_check alone (the old behaviour) would miss the mismatch ─

def test_integrity_check_alone_would_not_have_caught_the_row_mismatch(seeded_install):
    """Same tampered manifest as above, but run through the OLD entry point
    (`verify_archive`) to show it reports the archive as fine — proving
    `verify_backup`'s restore-and-compare step is not redundant with it.
    """
    result = bs.snapshot(profile=bs.PROFILE_CONTENT, verify=False)
    manifest_path = Path(result["path"] + ".manifest.json")
    manifest = json.loads(manifest_path.read_text())
    manifest["db_row_counts"]["data/app.db"]["widgets"] = 999
    manifest_path.write_text(json.dumps(manifest))

    old_style_report = bs.verify_archive(result["path"])
    assert old_style_report["ok"], (
        "this demonstrates the gap verify_backup closes: verify_archive only "
        "PRAGMA integrity_checks the extracted db, it never compares row "
        "counts, so a tampered/short manifest still reports ok=True"
    )
