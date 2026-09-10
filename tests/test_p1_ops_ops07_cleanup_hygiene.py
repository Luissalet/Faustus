"""OPS-07 - temp/cache/blob hygiene, trash (not hard-delete), quotas,
orphan detection, and an honest space/remote-cost report.

Extends src/cleanup_service.py (the one place session archive/delete
already lived) rather than building a second cleanup module. Reuses:
UploadHandler.cleanup_expired_chunked_sessions (PERF-05) for orphaned
upload sessions, src.constants.ARTIFACT_STORE_DIR for blob usage,
src.scorecard._path() for the scorecard log location, and DATA_DIR/runs
(the same directory OBS-04's support bundle reads) for run-log retention
and the remote-cost report.
"""
from __future__ import annotations

import json
import os
import time

import pytest

from src import cleanup_service as cs


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    """Every module this file touches resolves DATA_DIR the same way
    (`from src.constants import DATA_DIR`) -- patch the constant itself so
    all of them agree, mirroring how existing tests (e.g. test_scorecard*)
    isolate this module's on-disk state."""
    import src.constants as constants
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(constants, "DATA_DIR", str(data_dir))
    return data_dir


# ── trash: move, not delete ─────────────────────────────────────────────


def test_move_to_trash_relocates_a_file_and_returns_its_new_path(tmp_path, _data_dir):
    src_file = tmp_path / "orphan.bin"
    src_file.write_bytes(b"x" * 100)
    dest = cs.move_to_trash(str(src_file), category="uploads")
    assert dest is not None
    assert not src_file.exists()
    assert os.path.isfile(dest)
    assert ".trash" in dest and "uploads" in dest


def test_move_to_trash_relocates_a_directory(tmp_path, _data_dir):
    src_dir = tmp_path / "session123"
    src_dir.mkdir()
    (src_dir / "part.0").write_bytes(b"y" * 10)
    dest = cs.move_to_trash(str(src_dir), category="uploads")
    assert not src_dir.exists()
    assert os.path.isdir(dest)
    assert os.path.isfile(os.path.join(dest, "part.0"))


def test_move_to_trash_on_a_missing_path_returns_none(tmp_path, _data_dir):
    assert cs.move_to_trash(str(tmp_path / "nope"), category="uploads") is None


def test_purge_trash_only_removes_entries_past_retention(tmp_path, _data_dir):
    old_file = tmp_path / "old.bin"
    old_file.write_bytes(b"a" * 50)
    new_file = tmp_path / "new.bin"
    new_file.write_bytes(b"b" * 50)
    old_dest = cs.move_to_trash(str(old_file), category="misc")
    new_dest = cs.move_to_trash(str(new_file), category="misc")

    # Backdate only the "old" entry's mtime past the retention window.
    old_time = time.time() - (40 * 86400)
    os.utime(old_dest, (old_time, old_time))

    result = cs.purge_trash(older_than_days=30)
    assert any("old.bin" in r for r in result["removed"])
    assert not os.path.exists(old_dest)
    assert os.path.exists(new_dest)   # too recent -- survives
    assert result["freed_bytes"] == 50


# ── orphaned chunked uploads: reuses PERF-05's own sweep ────────────────


def test_sweep_upload_orphans_delegates_to_the_upload_handler(tmp_path, _data_dir, monkeypatch):
    calls = []

    class _FakeHandler:
        def cleanup_expired_chunked_sessions(self, *, max_age_seconds):
            calls.append(max_age_seconds)
            return 3

    removed = cs.sweep_upload_orphans(_FakeHandler(), max_age_seconds=999)
    assert removed == 3
    assert calls == [999]


# ── blob quota: never touches anything the caller didn't nominate ──────


def test_enforce_blob_quota_is_a_noop_under_quota(tmp_path, _data_dir):
    result = cs.enforce_blob_quota([], quota_bytes=1000, current_total_bytes=500)
    assert result == {"evicted": [], "freed_bytes": 0, "remaining_bytes": 500}


def test_enforce_blob_quota_evicts_oldest_first_until_under_quota(tmp_path, _data_dir):
    blobs = []
    for i, size in enumerate((300, 300, 300)):
        p = tmp_path / f"blob{i}.bin"
        p.write_bytes(b"z" * size)
        mtime = 1000.0 + i  # blob0 oldest, blob2 newest
        os.utime(p, (mtime, mtime))
        blobs.append((str(p), "artifacts", mtime))

    result = cs.enforce_blob_quota(blobs, quota_bytes=500, current_total_bytes=900, dry_run=False)
    # 900 -> needs to drop below 500: evicting blob0 (300) leaves 600, still
    # over; evicting blob1 (300) leaves 300, under quota. blob2 untouched.
    evicted_paths = [e["path"] for e in result["evicted"]]
    assert evicted_paths == [blobs[0][0], blobs[1][0]]
    assert result["remaining_bytes"] == 300
    assert not os.path.exists(blobs[0][0])   # trashed
    assert not os.path.exists(blobs[1][0])
    assert os.path.exists(blobs[2][0])       # never nominated for the shortfall -- untouched


def test_enforce_blob_quota_never_touches_a_path_outside_evictable(tmp_path, _data_dir):
    """The structural guarantee behind 'no borrar lo referenciado': a file
    NOT passed in `evictable` is never at risk, no matter how large or old
    it is, because this function has no way to discover it on its own."""
    referenced = tmp_path / "still_used.bin"
    referenced.write_bytes(b"r" * 5000)
    os.utime(referenced, (1.0, 1.0))  # ancient, but never nominated

    cs.enforce_blob_quota([], quota_bytes=100, current_total_bytes=5000, dry_run=False)
    assert referenced.exists()


def test_enforce_blob_quota_dry_run_reports_without_moving_anything(tmp_path, _data_dir):
    p = tmp_path / "blob.bin"
    p.write_bytes(b"q" * 300)
    os.utime(p, (1.0, 1.0))
    result = cs.enforce_blob_quota([(str(p), "artifacts", 1.0)], quota_bytes=100,
                                    current_total_bytes=300, dry_run=True)
    assert result["freed_bytes"] == 300
    assert p.exists()   # dry_run: preview only, never moved


# ── run-log retention ────────────────────────────────────────────────────


def test_runs_log_retention_trashes_only_old_jsonl_files(tmp_path, _data_dir):
    runs_dir = _data_dir / "runs"
    runs_dir.mkdir()
    old = runs_dir / "old-session.jsonl"
    old.write_text('{"event": "start"}\n')
    new = runs_dir / "new-session.jsonl"
    new.write_text('{"event": "start"}\n')
    old_time = time.time() - (90 * 86400)
    os.utime(old, (old_time, old_time))

    result = cs.runs_log_retention(older_than_days=60, dry_run=False)
    assert result["trashed"] == ["old-session.jsonl"]
    assert not old.exists()
    assert new.exists()


def test_runs_log_retention_dry_run_changes_nothing(tmp_path, _data_dir):
    runs_dir = _data_dir / "runs"
    runs_dir.mkdir()
    old = runs_dir / "old-session.jsonl"
    old.write_text('{"event": "start"}\n')
    old_time = time.time() - (90 * 86400)
    os.utime(old, (old_time, old_time))

    result = cs.runs_log_retention(older_than_days=60, dry_run=True)
    assert result["trashed"] == ["old-session.jsonl"]
    assert old.exists()   # dry_run: nothing actually moved


# ── usage reporting: real numbers, no placeholders ──────────────────────


def test_artifact_store_usage_measures_real_bytes(tmp_path, _data_dir, monkeypatch):
    store = tmp_path / "artifact_store"
    store.mkdir()
    (store / "abc123.png").write_bytes(b"i" * 1234)
    import src.constants as constants
    monkeypatch.setattr(constants, "ARTIFACT_STORE_DIR", str(store))
    usage = cs.artifact_store_usage()
    assert usage["exists"] is True
    assert usage["bytes"] == 1234
    assert usage["file_count"] == 1


def test_artifact_store_usage_handles_a_missing_directory(tmp_path, _data_dir, monkeypatch):
    import src.constants as constants
    monkeypatch.setattr(constants, "ARTIFACT_STORE_DIR", str(tmp_path / "does-not-exist"))
    usage = cs.artifact_store_usage()
    assert usage == {"path": str(tmp_path / "does-not-exist"), "exists": False, "bytes": 0, "file_count": 0}


def test_scorecard_log_usage_reuses_the_scorecard_modules_own_path(tmp_path, _data_dir):
    from src.scorecard import _path as scorecard_path
    path = scorecard_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write('{"model": "m"}\n')
    usage = cs.scorecard_log_usage()
    assert usage["path"] == path
    assert usage["bytes"] > 0


@pytest.mark.asyncio
async def test_storage_report_combines_every_category(tmp_path, _data_dir, monkeypatch):
    import src.constants as constants
    monkeypatch.setattr(constants, "ARTIFACT_STORE_DIR", str(tmp_path / "no-store"))

    async def _fake_preview(owner=None):
        return {
            "sessions_to_archive": [{"id": "s1"}],
            "sessions_to_delete": [],
            "preserved_sessions": [],
            "estimated_space_freed_mb": 0.0,
        }
    monkeypatch.setattr(cs, "get_cleanup_preview", _fake_preview)

    report = await cs.storage_report()
    assert report["sessions"]["archivable"] == 1
    assert "artifact_store" in report
    assert "scorecard_log" in report
    assert "trash" in report


# ── remote cost: never a silent 0 when data is unknown ──────────────────


def test_remote_cost_report_with_no_run_logs_reports_unknown_not_zero(tmp_path, _data_dir):
    """The core OPS-07 acceptance behaviour: no data means 'unknown', never
    a total of 0.0 implying nothing was spent."""
    report = cs.remote_cost_report()
    assert report["known_total_usd"] is None
    assert report["unknown_period"] is True


def test_remote_cost_report_sums_measured_costs_when_all_events_carry_one(tmp_path, _data_dir):
    runs_dir = _data_dir / "runs"
    runs_dir.mkdir()
    with open(runs_dir / "s1.jsonl", "w") as f:
        f.write(json.dumps({"event": "run_cost", "total_cost_usd": 0.42, "ts": time.time()}) + "\n")
        f.write(json.dumps({"event": "run_cost", "total_cost_usd": 0.08, "ts": time.time()}) + "\n")

    report = cs.remote_cost_report()
    assert report["known_total_usd"] == pytest.approx(0.50)
    assert report["unknown_period"] is False
    assert report["unknown_cost_events"] == 0


def test_remote_cost_report_flags_unknown_rather_than_treating_a_missing_cost_as_zero(tmp_path, _data_dir):
    """A run that reached the cost-event type but carries no cost field
    (e.g. the CLI never reported one) must not silently contribute 0 to the
    total -- it has to show up as 'unknown', per OPS-07's acceptance
    criterion ('factura remota desconocida no se estima como cero')."""
    runs_dir = _data_dir / "runs"
    runs_dir.mkdir()
    with open(runs_dir / "s1.jsonl", "w") as f:
        f.write(json.dumps({"event": "run_cost", "total_cost_usd": 1.00, "ts": time.time()}) + "\n")
        f.write(json.dumps({"event": "run_cost", "ts": time.time()}) + "\n")  # cost unknown

    report = cs.remote_cost_report()
    assert report["unknown_period"] is True
    assert report["unknown_cost_events"] == 1
    # The known partial sum is still surfaced, distinctly from "the total".
    assert report["known_total_usd"] == pytest.approx(1.00)


def test_remote_cost_report_ignores_events_outside_the_since_window(tmp_path, _data_dir):
    runs_dir = _data_dir / "runs"
    runs_dir.mkdir()
    now = time.time()
    with open(runs_dir / "s1.jsonl", "w") as f:
        f.write(json.dumps({"event": "run_cost", "total_cost_usd": 5.0, "ts": now - 1_000_000}) + "\n")
        f.write(json.dumps({"event": "run_cost", "total_cost_usd": 1.0, "ts": now}) + "\n")

    report = cs.remote_cost_report(since=now - 10)
    assert report["known_total_usd"] == pytest.approx(1.0)
