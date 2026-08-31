"""Verified snapshots of data/ (FAUSTUS).

The whole point of this module is that a backup which would not restore is
worse than no backup, because it buys false confidence. So the tests care most
about the failure paths: a corrupt archive, a corrupt database inside a
perfectly readable archive, and an admin endpoint being talked into reading
something outside the backup directory.
"""

import os
import sqlite3
import tarfile
import time
from pathlib import Path

import pytest

from src import backup_service as bs


@pytest.fixture
def install(tmp_path, monkeypatch):
    """A fake install: data/ with a real SQLite DB, plus an empty backups/."""
    data = tmp_path / "data"
    (data / "logs").mkdir(parents=True)
    (data / "deep_research").mkdir()
    (data / "mail-attachments").mkdir()
    (data / "settings.json").write_text('{"a": 1}', encoding="utf-8")
    (data / "logs" / "app.log").write_text("hello", encoding="utf-8")
    (data / "deep_research" / "run1.json").write_text("[]", encoding="utf-8")
    (data / "mail-attachments" / "x.bin").write_bytes(b"\x00" * 10)
    conn = sqlite3.connect(str(data / "app.db"))
    conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
    conn.execute("INSERT INTO notes (body) VALUES ('keep me')")
    conn.commit()
    conn.close()

    backups = tmp_path / "backups"
    monkeypatch.setattr(bs, "data_dir", lambda: data)
    monkeypatch.setattr(bs, "backup_dir", lambda: backups)
    return {"root": tmp_path, "data": data, "backups": backups}


def members(path):
    with tarfile.open(path, "r:gz") as tar:
        return [m.name for m in tar.getmembers()]


class TestSnapshot:
    def test_writes_a_verified_archive_under_data_prefix(self, install):
        out = bs.snapshot()
        assert out["ok"] is True
        assert out["verified"]["ok"] is True
        names = members(out["path"])
        assert "data/settings.json" in names
        assert "data/logs/app.log" in names
        assert all(n.startswith("data/") for n in names)

    def test_bulk_directories_are_skipped_unless_asked_for(self, install):
        default = members(bs.snapshot()["path"])
        assert not any("deep_research" in n for n in default)
        assert not any("mail-attachments" in n for n in default)
        everything = members(bs.snapshot(include_research=True,
                                         include_attachments=True)["path"])
        assert any("deep_research" in n for n in everything)
        assert any("mail-attachments" in n for n in everything)

    def test_the_database_survives_readable(self, install, tmp_path):
        """The reason snapshots use sqlite .backup() instead of copying bytes."""
        out = bs.snapshot()
        with tarfile.open(out["path"], "r:gz") as tar:
            extracted = tar.extractfile("data/app.db").read()
        restored = tmp_path / "restored.db"
        restored.write_bytes(extracted)
        conn = sqlite3.connect(str(restored))
        assert conn.execute("SELECT body FROM notes").fetchone()[0] == "keep me"
        conn.close()

    def test_refuses_to_write_inside_data(self, install):
        out = bs.snapshot(out_path=str(install["data"] / "self.tar.gz"))
        assert out["ok"] is False and "outside data/" in out["error"]

    def test_missing_data_dir_is_reported_not_raised(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bs, "data_dir", lambda: tmp_path / "nope")
        monkeypatch.setattr(bs, "backup_dir", lambda: tmp_path / "backups")
        assert bs.snapshot()["ok"] is False


class TestVerify:
    def test_a_truncated_archive_is_caught(self, install):
        path = Path(bs.snapshot()["path"])
        data = path.read_bytes()
        path.write_bytes(data[:len(data) // 2])
        report = bs.verify_archive(path)
        assert report["ok"] is False and report["problems"]

    def test_a_corrupt_database_inside_a_valid_archive_is_caught(self, install):
        """This is the one a tar listing would never notice."""
        (install["data"] / "broken.db").write_bytes(b"SQLite format 3\x00" + b"\xff" * 500)
        report = bs.snapshot(verify=False)
        check = bs.verify_archive(report["path"])
        broken = [db for db in check["databases"] if db["name"].endswith("broken.db")]
        assert broken and broken[0]["ok"] is False
        assert check["ok"] is False

    def test_an_archive_escaping_data_is_refused(self, install, tmp_path):
        evil = install["backups"] / "faustus-backup-evil.tar.gz"
        evil.parent.mkdir(parents=True, exist_ok=True)
        victim = tmp_path / "outside.txt"
        victim.write_text("x", encoding="utf-8")
        with tarfile.open(evil, "w:gz") as tar:
            tar.add(victim, arcname="../../outside.txt")
        report = bs.verify_archive(evil)
        assert report["ok"] is False
        assert any("escapes" in p or "outside data/" in p for p in report["problems"])

    def test_missing_file_is_a_problem_not_a_crash(self, install):
        assert bs.verify_archive(install["backups"] / "nope.tar.gz")["ok"] is False


class TestListingAndPruning:
    def _make(self, install, n):
        made = []
        for i in range(n):
            p = install["backups"] / f"faustus-backup-2026083{i}-000000.tar.gz"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x" * (i + 1))
            os.utime(p, (time.time() - (n - i) * 3600,) * 2)
            made.append(p)
        return made

    def test_newest_first_and_foreign_files_ignored(self, install):
        self._make(install, 3)
        (install["backups"] / "notes.txt").write_text("not a backup", encoding="utf-8")
        listed = bs.list_backups()
        assert len(listed) == 3
        assert listed[0]["age_hours"] < listed[-1]["age_hours"]

    def test_prune_keeps_the_newest_and_reports_what_went(self, install):
        self._make(install, 5)
        removed = bs.prune(2)
        assert len(removed) == 3
        assert len(bs.list_backups()) == 2

    def test_prune_never_touches_anything_else(self, install):
        self._make(install, 3)
        stray = install["backups"] / "important.zip"
        stray.write_text("keep", encoding="utf-8")
        bs.prune(1)
        assert stray.exists()

    def test_prune_with_no_limit_is_a_no_op(self, install):
        self._make(install, 3)
        assert bs.prune(0) == [] and len(bs.list_backups()) == 3


class TestPathSafety:
    def test_only_real_snapshots_in_the_backup_dir_resolve(self, install):
        real = bs.snapshot()["name"]
        assert bs.resolve_in_backup_dir(real) is not None
        for bad in ("../settings.json", "..", "", "sub/dir.tar.gz",
                    "C:\\Windows\\win.ini", "/etc/passwd", "notes.txt"):
            assert bs.resolve_in_backup_dir(bad) is None, bad


class TestSchedule:
    def test_due_when_there_is_nothing_or_the_last_one_is_old(self, install):
        assert bs.due(24) is True
        bs.snapshot()
        assert bs.due(24) is False
        # A zero/garbage interval is floored at 15 minutes so a misconfigured
        # setting cannot turn the loop into a snapshot storm.
        assert bs.due(0) is False
        for entry in bs.list_backups():
            os.utime(entry["path"], (time.time() - 20 * 60,) * 2)
        assert bs.due(0) is True

    def test_scheduled_run_respects_the_off_switch(self, install):
        settings = {"backup_auto_enabled": False}
        assert bs.run_scheduled_snapshot(lambda k, d=None: settings.get(k, d)) is None
        assert bs.list_backups() == []

    def test_scheduled_run_snapshots_and_prunes(self, install):
        settings = {"backup_auto_enabled": True, "backup_interval_hours": 24,
                    "backup_keep": 1}
        for _ in range(2):
            result = bs.run_scheduled_snapshot(lambda k, d=None: settings.get(k, d))
            if result is None:  # second call is not due yet — force it
                for entry in bs.list_backups():
                    os.utime(entry["path"], (time.time() - 48 * 3600,) * 2)
                result = bs.run_scheduled_snapshot(lambda k, d=None: settings.get(k, d))
            assert result["ok"] is True
        assert len(bs.list_backups()) == 1

    def test_broken_settings_fall_back_to_defaults(self, install):
        settings = {"backup_auto_enabled": True, "backup_interval_hours": "soon",
                    "backup_keep": None}
        assert bs.run_scheduled_snapshot(lambda k, d=None: settings.get(k, d))["ok"]
