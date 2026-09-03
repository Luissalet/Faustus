"""Disk ballast: allocation that refuses to fill the disk it protects, the
EWMA+PID urgency, candidate scoring with its absolute .git veto, and the
quarantine that moves instead of deleting (src/disk_ballast.py)."""

import os
import time

import pytest

import src.settings as settings_mod
from src import disk_ballast


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


class FakeDisk:
    """A free-space source the tests drive by hand."""

    def __init__(self, total, free):
        self.total = int(total)
        self.free = int(free)
        self.calls = 0

    def __call__(self, path=None):
        self.calls += 1
        return self.total, self.total - self.free, self.free


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(disk_ballast, "DATA_DIR", str(tmp_path))
    disk_ballast.reset_estimator(floor=disk_ballast.FLOOR_MIN_BYTES)
    yield tmp_path
    disk_ballast.reset_estimator(floor=disk_ballast.FLOOR_MIN_BYTES)


def _fake_disk(monkeypatch, total, free):
    disk = FakeDisk(total, free)
    monkeypatch.setattr(disk_ballast, "disk_usage", disk)
    return disk


def _set_mode(monkeypatch, mode):
    monkeypatch.setattr(
        settings_mod, "get_setting",
        lambda key, default=None: mode if key == disk_ballast.MODE_SETTING else default,
    )


def _age(path, days):
    """Backdate a whole tree, directories included, so the scan sees its age."""
    stamp = time.time() - days * 86400.0
    if os.path.isdir(path):
        for root, dirs, files in os.walk(path, topdown=False):
            for name in files:
                os.utime(os.path.join(root, name), (stamp, stamp))
            for name in dirs:
                os.utime(os.path.join(root, name), (stamp, stamp))
    os.utime(path, (stamp, stamp))


def _write(path, size=64):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"x" * size)
    return path


# ---------------------------------------------------------------------------
# Ballast files
# ---------------------------------------------------------------------------


def test_ensure_creates_real_files_and_release_unlinks_them(env, monkeypatch):
    _fake_disk(monkeypatch, total=500 * disk_ballast.GIB, free=400 * disk_ballast.GIB)

    made = disk_ballast.ensure(3, 4096)
    assert made["ok"] is True and len(made["created"]) == 3
    assert made["created_bytes"] == 3 * 4096
    state = made["ballast"]
    assert state["count"] == 3 and state["bytes"] == 3 * 4096
    # Real blocks, not a sparse hole: the point is that unlinking them frees
    # space that the filesystem had actually handed out.
    for row in state["files"]:
        assert os.path.getsize(os.path.join(state["dir"], row["name"])) == 4096

    freed = disk_ballast.release(2)
    assert len(freed["released"]) == 2 and freed["freed_bytes"] == 2 * 4096
    assert freed["ballast"]["count"] == 1


def test_ensure_is_idempotent(env, monkeypatch):
    _fake_disk(monkeypatch, total=500 * disk_ballast.GIB, free=400 * disk_ballast.GIB)
    disk_ballast.ensure(2, 4096)
    again = disk_ballast.ensure(2, 4096)
    assert again["created"] == [] and again["ballast"]["count"] == 2


def test_ensure_refuses_to_allocate_below_the_floor(env, monkeypatch):
    """The ballast may never fill the disk it exists to protect."""
    total = 100 * disk_ballast.GIB
    floor = disk_ballast.floor_bytes(total)
    # Exactly one byte of slack above the floor: any allocation crosses it.
    _fake_disk(monkeypatch, total=total, free=floor + 1)

    result = disk_ballast.ensure(4, disk_ballast.GIB)
    assert result["created"] == []
    assert "floor" in result["reason"]
    assert result["ballast"]["count"] == 0
    assert not os.path.isdir(disk_ballast.ballast_dir()) or \
        os.listdir(disk_ballast.ballast_dir()) == []


def test_ensure_stops_at_its_share_of_the_volume(env, monkeypatch):
    total = 40960  # the 25% cap allows 10240 bytes of ballast
    monkeypatch.setattr(disk_ballast, "FLOOR_MIN_BYTES", 0)
    _fake_disk(monkeypatch, total=total, free=total)
    result = disk_ballast.ensure(8, 4096)
    assert len(result["created"]) == 2
    assert result["ballast"]["bytes"] <= int(total * disk_ballast.MAX_BALLAST_FRACTION)
    assert "of the volume" in result["reason"]


def test_allocation_aborts_and_removes_the_partial_file(env, monkeypatch):
    """A write that would cross the floor stops and leaves nothing behind."""
    total = 100 * disk_ballast.GIB
    floor = disk_ballast.floor_bytes(total)
    state = {"free": floor + 10 * disk_ballast.GIB, "calls": 0}
    monkeypatch.setattr(disk_ballast, "WRITE_CHUNK_BYTES", 16)
    monkeypatch.setattr(disk_ballast, "FREE_CHECK_EVERY_CHUNKS", 1)

    def probe(path=None):
        state["calls"] += 1
        if state["calls"] > 1:
            # The volume fills under us after ensure()'s own budget check, so
            # the trip has to be caught mid-write.
            state["free"] = floor - 1
        return total, total - state["free"], state["free"]

    monkeypatch.setattr(disk_ballast, "disk_usage", probe)

    result = disk_ballast.ensure(1, 1024)
    assert result["created"] == []
    assert "floor" in result["reason"]
    assert os.listdir(disk_ballast.ballast_dir()) == []


def test_ensure_rejects_nonsense_arguments(env, monkeypatch):
    _fake_disk(monkeypatch, total=500 * disk_ballast.GIB, free=400 * disk_ballast.GIB)
    assert disk_ballast.ensure(1, 0)["ok"] is False
    assert disk_ballast.ensure(-1, 4096)["ok"] is False


# ---------------------------------------------------------------------------
# Urgency — hand-computed, no disk, injected clock
# ---------------------------------------------------------------------------


def test_urgency_matches_hand_computed_values_at_three_samples():
    """Three samples an hour apart on a 10 GB floor, worked out by hand.

    t=0    free 30 GB                      -> no rate yet, urgency 0
    t=3600 free 20 GB  rate -10GB/h        -> projects exactly onto the floor,
                                              error 0, urgency 0
    t=7200 free 12 GB  raw rate -8GB/h
        rate  = 0.5·(-8e9/3600) + 0.5·(-10e9/3600) = -2 500 000 B/s
        accel = 0.5·((-2.5e6 + 2.7777…e6)/3600)    =    38.580246… B/s²
        distance = -2.5e6·3600 + ½·38.580246…·3600² = -8.75e9
        projected = 12e9 - 8.75e9 = 3.25e9
        error = (10e9 - 3.25e9)/10e9 = 0.675
        integral = 0.675 (τ = 1 horizon), derivative = 0.675
        urgency = (0.25 + 0.08 + 0.02)·0.675 = 0.23625
    """
    est = disk_ballast.UrgencyEstimator(floor=10_000_000_000, horizon_s=3600.0,
                                        alpha=0.5)
    assert est.observe(30_000_000_000, 0.0) == 0.0
    assert est.observe(20_000_000_000, 3600.0) == 0.0
    assert est.observe(12_000_000_000, 7200.0) == pytest.approx(0.23625)

    state = est.state()
    assert state["rate_bytes_per_s"] == pytest.approx(-2_500_000.0)
    assert state["accel_bytes_per_s2"] == pytest.approx(38.580247, abs=1e-6)
    assert state["projected_free_bytes"] == 3_250_000_000
    assert state["error"] == pytest.approx(0.675)
    assert state["samples"] == 3


def test_urgency_is_zero_while_the_projection_stays_above_the_floor():
    est = disk_ballast.UrgencyEstimator(floor=10_000_000_000, horizon_s=3600.0)
    est.observe(500_000_000_000, 0.0)
    est.observe(499_000_000_000, 3600.0)
    est.observe(498_000_000_000, 7200.0)
    assert est.urgency() == 0.0


def test_sustained_pressure_saturates_at_one_and_calm_brings_it_back():
    """Windup is bounded on both sides.

    A one-sided error would leave a disk that recovered pinned at urgency 1 for
    ever, which is how this kind of signal usually goes wrong.
    """
    est = disk_ballast.UrgencyEstimator(floor=10_000_000_000, horizon_s=3600.0)
    est.observe(30_000_000_000, 0.0)
    for step in range(1, 25):
        est.observe(max(0, 30_000_000_000 - 8_000_000_000 * step), step * 3600.0)

    assert est.urgency() == 1.0
    # The I term alone can never exceed 1: the integral is capped at 1/Ki.
    assert est.state()["integral"] == pytest.approx(1.0 / disk_ballast.KI)

    for step in range(25, 45):
        est.observe(900_000_000_000, step * 3600.0)
    assert est.urgency() == 0.0
    assert est.state()["integral"] == 0.0


def test_urgency_ignores_a_clock_that_does_not_advance():
    est = disk_ballast.UrgencyEstimator(floor=10_000_000_000, horizon_s=3600.0)
    est.observe(30_000_000_000, 100.0)
    before = est.state()
    est.observe(1.0, 100.0)      # same instant
    est.observe(1.0, 50.0)       # and a clock that went backwards
    after = est.state()
    assert after["rate_bytes_per_s"] == before["rate_bytes_per_s"] is None
    assert est.urgency() == 0.0


def test_urgency_survives_junk_samples():
    est = disk_ballast.UrgencyEstimator(floor=10_000_000_000)
    assert est.observe("banana", None) == 0.0
    assert est.observe(None, "later") == 0.0
    assert est.urgency() == 0.0


# ---------------------------------------------------------------------------
# Candidate scoring and the .git veto
# ---------------------------------------------------------------------------


def test_scoring_weights_age_size_and_rederivability():
    # 90 days and 1 GiB are the full-score points of each axis.
    assert disk_ballast.score_candidate(
        size_bytes=disk_ballast.GIB, age_days=90, rederivable=1.0) == pytest.approx(1.0)
    assert disk_ballast.score_candidate(
        size_bytes=0, age_days=0, rederivable=0.0) == pytest.approx(0.0)
    assert disk_ballast.score_candidate(
        size_bytes=0, age_days=90, rederivable=0.0) == pytest.approx(disk_ballast.W_AGE)


def test_candidates_are_ordered_worst_first_with_their_reasons(env):
    _write(str(env / "tts_cache" / "a.wav"), size=4096)
    _age(str(env / "tts_cache"), days=120)
    _write(str(env / "generated_images" / "fresh.png"), size=16)

    rows = {row["name"]: row for row in disk_ballast.scan()}
    assert rows["tts_cache"]["score"] > rows["fresh.png"]["score"]
    assert rows["tts_cache"]["rederivable"] == 1.0
    assert rows["fresh.png"]["rederivable"] == 0.15
    assert any("days ago" in reason for reason in rows["tts_cache"]["reasons"])
    assert disk_ballast.scan()[0]["name"] == "tts_cache"


def test_a_git_directory_anywhere_inside_vetoes_the_highest_scoring_candidate(env):
    """The absolute rule: version control is never a cache.

    Two checkpoints identical in age and size — one with a .git directory
    inside it. Without the veto the git one would tie for the top of the list;
    with it, it scores 0, is not deletable, and says why.
    """
    _write(str(env / "checkpoints" / "with_git" / "payload.bin"), size=8192)
    os.makedirs(str(env / "checkpoints" / "with_git" / ".git" / "objects"))
    _write(str(env / "checkpoints" / "with_git" / ".git" / "HEAD"), size=8)
    _write(str(env / "checkpoints" / "plain" / "payload.bin"), size=8192)
    _age(str(env / "checkpoints"), days=365)

    rows = {row["name"]: row for row in disk_ballast.scan()}
    plain, with_git = rows["plain"], rows["with_git"]

    assert plain["deletable"] is True and plain["score"] > 0
    assert with_git["deletable"] is False and with_git["score"] == 0.0
    assert any(".git" in veto for veto in with_git["vetoes"])
    # And the vetoed one is not offered ahead of anything.
    assert disk_ballast.scan()[0]["name"] == "plain"


def test_an_unscannable_candidate_is_vetoed_rather_than_assumed_clean(env, monkeypatch):
    _write(str(env / "checkpoints" / "big" / "payload.bin"), size=64)
    _age(str(env / "checkpoints"), days=365)
    monkeypatch.setattr(disk_ballast, "MAX_WALK_ENTRIES", 0)

    row = {r["name"]: r for r in disk_ballast.scan()}["big"]
    assert row["deletable"] is False and row["scan_complete"] is False
    assert any("could not be fully scanned" in veto for veto in row["vetoes"])


# ---------------------------------------------------------------------------
# Quarantine, undo, sweep — and the modes
# ---------------------------------------------------------------------------


def test_observe_mode_is_the_default_and_moves_nothing(env, monkeypatch):
    monkeypatch.setattr(settings_mod, "get_setting",
                        lambda key, default=None: default)
    assert disk_ballast.mode() == "observe"

    target = _write(str(env / "tts_cache" / "a.wav"))
    result = disk_ballast.quarantine(target)

    assert result["ok"] is False and result["mode"] == "observe"
    assert "nothing was moved" in result["reason"]
    assert os.path.exists(target)
    assert disk_ballast.list_quarantine() == []


def test_unknown_or_unreadable_modes_fall_back_to_observe(monkeypatch):
    monkeypatch.setattr(settings_mod, "get_setting",
                        lambda key, default=None: "delete_everything")
    assert disk_ballast.mode() == "observe"

    def boom(key, default=None):
        raise RuntimeError("settings on fire")

    monkeypatch.setattr(settings_mod, "get_setting", boom)
    assert disk_ballast.mode() == "observe"


def test_enforce_quarantines_and_undo_puts_it_back(env, monkeypatch):
    _set_mode(monkeypatch, "enforce")
    target = str(env / "tts_cache")
    _write(os.path.join(target, "a.wav"), size=32)

    moved = disk_ballast.quarantine(target, reason="oldest cache")
    assert moved["ok"] is True and moved["id"]
    assert not os.path.exists(target)
    entries = disk_ballast.list_quarantine()
    assert len(entries) == 1 and entries[0]["reason"] == "oldest cache"
    assert os.path.exists(entries[0]["payload"])

    restored = disk_ballast.undo(moved["id"])
    assert restored["ok"] is True
    assert os.path.exists(os.path.join(target, "a.wav"))
    with open(os.path.join(target, "a.wav"), "rb") as fh:
        assert fh.read() == b"x" * 32
    assert disk_ballast.list_quarantine() == []


def test_undo_refuses_to_overwrite_what_came_back(env, monkeypatch):
    _set_mode(monkeypatch, "enforce")
    target = _write(str(env / "tts_cache" / "a.wav"))
    moved = disk_ballast.quarantine(target)
    _write(target, size=1)                      # something new is there now

    result = disk_ballast.undo(moved["id"])
    assert result["ok"] is False
    assert "refusing to overwrite" in result["reason"]
    assert os.path.getsize(target) == 1
    assert len(disk_ballast.list_quarantine()) == 1


def test_the_git_veto_cannot_be_overridden_by_a_mode(env, monkeypatch):
    _set_mode(monkeypatch, "enforce")
    target = str(env / "checkpoints" / "repo")
    _write(os.path.join(target, "file.txt"))
    os.makedirs(os.path.join(target, ".git"))

    result = disk_ballast.quarantine(target)
    assert result["ok"] is False and ".git" in result["reason"]
    assert os.path.isdir(target)


def test_a_path_outside_data_dir_is_refused(env, monkeypatch, tmp_path_factory):
    _set_mode(monkeypatch, "enforce")
    outside = tmp_path_factory.mktemp("elsewhere") / "precious.txt"
    outside.write_text("mine", encoding="utf-8")

    result = disk_ballast.quarantine(str(outside))
    assert result["ok"] is False and "outside DATA_DIR" in result["reason"]
    assert outside.exists()


def test_canary_mode_stops_at_ten_quarantines_an_hour(env, monkeypatch):
    _set_mode(monkeypatch, "canary")
    for index in range(disk_ballast.CANARY_PER_HOUR):
        target = _write(str(env / "tts_cache" / f"f{index}.wav"))
        assert disk_ballast.quarantine(target)["ok"] is True

    eleventh = _write(str(env / "tts_cache" / "f99.wav"))
    refused = disk_ballast.quarantine(eleventh)
    assert refused["ok"] is False
    assert "canary" in refused["reason"] and "budget is 10" in refused["reason"]
    assert os.path.exists(eleventh)
    assert len(disk_ballast.list_quarantine()) == disk_ballast.CANARY_PER_HOUR


def test_sweep_keeps_entries_inside_the_undo_window_and_destroys_older_ones(env,
                                                                           monkeypatch):
    from datetime import timedelta

    _set_mode(monkeypatch, "enforce")
    target = _write(str(env / "tts_cache" / "a.wav"))
    moved = disk_ballast.quarantine(target)
    stamp = disk_ballast._utcnow()

    kept = disk_ballast.sweep(now=stamp + timedelta(hours=23))
    assert kept["swept"] == [] and kept["kept"] == 1
    assert disk_ballast.undo(moved["id"])["ok"] is True

    again = disk_ballast.quarantine(_write(target))
    gone = disk_ballast.sweep(now=disk_ballast._utcnow() + timedelta(hours=25))
    assert gone["swept"] == [again["id"]]
    assert disk_ballast.list_quarantine() == []
    assert not os.path.exists(target)


def test_quarantine_reports_instead_of_raising_on_a_missing_path(env, monkeypatch):
    _set_mode(monkeypatch, "enforce")
    result = disk_ballast.quarantine(str(env / "nope"))
    assert result["ok"] is False and result["reason"] == "no such path"
    assert disk_ballast.undo("")["ok"] is False
    assert disk_ballast.undo("not-an-id")["reason"] == "no such quarantine entry"


# ---------------------------------------------------------------------------
# status(): the one read
# ---------------------------------------------------------------------------


def test_status_measures_without_touching_anything(env, monkeypatch):
    _set_mode(monkeypatch, "observe")
    _fake_disk(monkeypatch, total=100 * disk_ballast.GIB, free=50 * disk_ballast.GIB)
    _write(str(env / "tts_cache" / "a.wav"), size=128)
    _age(str(env / "tts_cache"), days=200)

    before = sorted(os.listdir(env))
    report = disk_ballast.status()

    assert report["mode"] == "observe"
    assert report["disk"]["free_bytes"] == 50 * disk_ballast.GIB
    assert report["disk"]["below_floor"] is False
    assert report["ballast"]["count"] == 0
    assert report["urgency"]["samples"] == 1
    assert [c["name"] for c in report["candidates"]] == ["tts_cache"]
    assert report["quarantine"]["sweep_after_hours"] == 24.0
    assert sorted(os.listdir(env)) == before


def test_status_reports_a_probe_failure_instead_of_raising(env, monkeypatch):
    def boom(path=None):
        raise OSError("no such device")

    monkeypatch.setattr(disk_ballast, "disk_usage", boom)
    report = disk_ballast.status()
    assert report["disk"]["total_bytes"] == 0
    assert report["problems"] and "free space unreadable" in report["problems"][0]


# ---------------------------------------------------------------------------
# The HTTP API — /api/storage/*
# ---------------------------------------------------------------------------


@pytest.fixture
def client(env, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core.middleware import require_admin
    from routes import storage_routes

    _fake_disk(monkeypatch, total=100 * disk_ballast.GIB, free=60 * disk_ballast.GIB)
    app = FastAPI()
    app.include_router(storage_routes.setup_storage_routes())
    app.dependency_overrides[require_admin] = lambda: None
    return TestClient(app)


def test_status_route_reports_disk_urgency_ballast_and_candidates(client, env):
    _write(str(env / "tts_cache" / "a.wav"), size=64)
    _age(str(env / "tts_cache"), days=400)

    body = client.get("/api/storage/status").json()
    assert body["status"] == "success"
    assert body["mode"] == "observe" and body["modes"] == ["observe", "canary", "enforce"]
    assert body["disk"]["free_bytes"] == 60 * disk_ballast.GIB
    assert [c["name"] for c in body["candidates"]] == ["tts_cache"]
    assert "urgency" in body and "value" in body["urgency"]


def test_status_route_answers_in_robot_mode(client):
    plain = client.get("/api/storage/status")
    robot = client.get("/api/storage/status?robot=1")
    assert plain.json()["status"] == "success"
    envelope = robot.json()
    assert envelope["ok"] is True and envelope["error_code"] is None
    assert envelope["data"]["mode"] == "observe"
    assert "schema_version" in envelope


def test_ballast_and_release_routes_round_trip(client):
    made = client.post("/api/storage/ballast", json={"count": 2, "size_bytes": 4096}).json()
    assert made["ballast"]["count"] == 2
    freed = client.post("/api/storage/release", json={"n": 2}).json()
    assert freed["freed_bytes"] == 2 * 4096 and freed["ballast"]["count"] == 0


def test_ballast_route_rejects_nonsense(client):
    assert client.post("/api/storage/ballast", json={"count": -1}).status_code == 400
    assert client.post("/api/storage/ballast", json={"size_bytes": 0}).status_code == 400
    assert client.post("/api/storage/release", json={"n": -2}).status_code == 400


def test_quarantine_route_is_a_refusal_not_an_error_in_observe_mode(client, env,
                                                                   monkeypatch):
    monkeypatch.setattr(settings_mod, "get_setting", lambda key, default=None: default)
    target = _write(str(env / "tts_cache" / "a.wav"))

    response = client.post("/api/storage/quarantine", json={"path": target})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False and "nothing was moved" in body["reason"]
    assert os.path.exists(target)


def test_quarantine_and_undo_routes_round_trip(client, env, monkeypatch):
    _set_mode(monkeypatch, "enforce")
    target = _write(str(env / "tts_cache" / "a.wav"))

    moved = client.post("/api/storage/quarantine",
                        json={"path": target, "reason": "test"}).json()
    assert moved["ok"] is True and not os.path.exists(target)

    restored = client.post("/api/storage/undo", json={"id": moved["id"]}).json()
    assert restored["ok"] is True and os.path.exists(target)
    assert client.post("/api/storage/undo", json={"id": "nope"}).status_code == 404
    assert client.post("/api/storage/quarantine", json={"path": " "}).status_code == 400
