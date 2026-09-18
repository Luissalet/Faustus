"""BUG 7 — the audit and scorecard JSONL logs grew without bound.

`project_audit.record()` and `scorecard.record()` only ever appended, nothing
ever rotated, and `load()` walked the whole file on every request (the project
endpoint reads it twice: `load()` and then `files_index()`). On top of that
`project_audit.load()` trimmed with `rows = rows[-MAX_ENTRIES_READ:]` INSIDE
the per-line loop — a full copy of the retained window per line, i.e.
quadratic: the measured 60k-line log cost ~1 s per request.

Fixes: `collections.deque(maxlen=…)` for the trim, and size-based rotation in
`record()` that keeps the newest K lines and rewrites atomically (tmp +
os.replace).
"""

import json
import os
import time
import tracemalloc

import pytest

from src import project_audit as pa
from src import scorecard as sc


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    monkeypatch.setenv("ODYSSEUS_DATA_DIR", str(d))
    import src.constants as consts
    monkeypatch.setattr(consts, "DATA_DIR", str(d), raising=False)
    return d


@pytest.fixture
def scorecard_on(monkeypatch):
    monkeypatch.setattr(sc, "_setting", lambda key, default=None: True if key == "agent_scorecard" else default,
                        raising=False)


def _audit_entry(i):
    return {"ts": i, "kind": "turn", "session_id": "s", "message_id": i, "model": "qwen3",
            "workspace": "/tmp/ws", "project_id": None, "files": ["src/a.py", "src/b.py"],
            "stop_reason": "complete", "checkpoint": "a" * 40, "request": "fix the thing",
            "tests": "pass", "review": "ok"}


def _seed(path, n, make):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for i in range(n):
            f.write(json.dumps(make(i)) + "\n")
    return path


def _tmp_leftovers(directory):
    return [n for n in os.listdir(directory) if ".tmp" in n]


# ── rotation ───────────────────────────────────────────────────────────────

def test_project_audit_record_rotates_and_keeps_the_newest(data_dir, monkeypatch):
    monkeypatch.setattr(pa, "ROTATE_MAX_BYTES", 4096)
    monkeypatch.setattr(pa, "ROTATE_KEEP_LINES", 10)
    key = "proj1"
    for i in range(300):
        pa.record(key, session_id="s", message_id=i, model="m", files=["a.py"],
                  workspace="/tmp/ws", stop_reason="complete", user_text="x" * 100)
    p = pa.path_for(key)
    size = os.path.getsize(p)
    assert size <= 4096 * 2, f"the audit log never rotated ({size} bytes)"
    rows = pa.load(key, limit=50)
    assert rows and rows[0]["message_id"] == 299, "rotation dropped the NEWEST entries"
    # How many lines survive depends on how many fit under ROTATE_MAX_BYTES,
    # and a line is one byte wider on Windows (CRLF), so the cycle lands on a
    # different phase there. Assert the invariant, not the phase.
    assert 10 <= len(rows) <= 40, len(rows)
    assert _tmp_leftovers(pa._dir()) == [], "rotation left a temp file behind"
    # Every surviving line is still valid JSON (atomic rewrite, no torn tail).
    with open(p, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                json.loads(line)


def test_scorecard_record_rotates_and_keeps_the_newest(data_dir, scorecard_on, monkeypatch):
    monkeypatch.setattr(sc, "ROTATE_MAX_BYTES", 4096)
    monkeypatch.setattr(sc, "ROTATE_KEEP_LINES", 10)
    for i in range(300):
        assert sc.record({"ts": i, "model": "m", "seq": i, "pad": "x" * 100}) is True
    p = sc._path()
    assert os.path.getsize(p) <= 4096 * 2, "scorecard.jsonl never rotated"
    rows = sc.load()
    assert rows and rows[-1]["seq"] == 299, "rotation dropped the NEWEST turns"
    # Same as above: the exact survivor count is platform-dependent (CRLF).
    assert 10 <= len(rows) <= 40, len(rows)
    assert _tmp_leftovers(os.path.dirname(p)) == [], "rotation left a temp file behind"


def test_rotation_is_a_no_op_below_the_threshold(data_dir, monkeypatch):
    monkeypatch.setattr(pa, "ROTATE_MAX_BYTES", 8 * 1024 * 1024)
    key = "small"
    for i in range(20):
        pa.record(key, session_id="s", message_id=i, model="m", files=["a.py"],
                  workspace="/tmp/ws", stop_reason="complete")
    assert len(pa.load(key, limit=100)) == 20
    assert _tmp_leftovers(pa._dir()) == []


def test_rotation_survives_an_unwritable_directory(data_dir, monkeypatch):
    """A rotation failure must never lose the append that just happened."""
    monkeypatch.setattr(pa, "ROTATE_MAX_BYTES", 1)

    def _boom(*a, **kw):
        raise OSError("nope")
    monkeypatch.setattr(pa.os, "replace", _boom)
    key = "brittle"
    assert pa.record(key, session_id="s", message_id=1, model="m", files=["a.py"],
                     workspace="/tmp/ws", stop_reason="complete") is not None
    assert [r["message_id"] for r in pa.load(key)] == [1]
    assert _tmp_leftovers(pa._dir()) == []


# ── bounded trimming in load() ─────────────────────────────────────────────

def test_project_audit_load_scales_linearly(data_dir, monkeypatch):
    """The per-line `rows = rows[-MAX:]` copy made load() quadratic. Compare
    the cost of a small file with the cost of one 12x bigger, in the same
    process: a linear reader stays near 12x, the quadratic one blows past it."""
    monkeypatch.setattr(pa, "MAX_ENTRIES_READ", 20000)
    n_small, n_big = 5_000, 60_000
    _seed(pa.path_for("small"), n_small, _audit_entry)
    _seed(pa.path_for("big"), n_big, _audit_entry)

    def _timed(key):
        best = float("inf")
        for _ in range(2):
            t0 = time.perf_counter()
            pa.load(key, limit=200)
            best = min(best, time.perf_counter() - t0)
        return best

    small, big = _timed("small"), _timed("big")
    ratio = big / max(small, 1e-6)
    # Linear ≈ 12x. Measured quadratic on this file: ~100x. 30x is a wide gap
    # from both sides and both timings come from the same machine and process.
    assert ratio < 30, (
        f"load() is not linear in the file size: {n_small} lines took {small:.3f}s, "
        f"{n_big} lines took {big:.3f}s ({ratio:.0f}x)"
    )


def test_project_audit_load_keeps_the_newest_within_the_cap(data_dir, monkeypatch):
    monkeypatch.setattr(pa, "MAX_ENTRIES_READ", 50)
    _seed(pa.path_for("k"), 500, _audit_entry)
    rows = pa.load("k", limit=1000)
    assert len(rows) == 50
    assert [r["message_id"] for r in rows[:3]] == [499, 498, 497]
    # files_index() walks the same capped window.
    idx = {r["path"]: r for r in pa.files_index("k")}
    assert idx["src/a.py"]["turns"] == 50


def test_project_audit_load_still_skips_broken_lines(data_dir):
    p = pa.path_for("mixed")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(json.dumps(_audit_entry(1)) + "\n")
        f.write("{not json\n\n")
        f.write(json.dumps(_audit_entry(2)) + "\n")
    assert [r["message_id"] for r in pa.load("mixed")] == [2, 1]


def test_scorecard_load_does_not_hold_the_whole_file(data_dir, scorecard_on):
    """`out[-limit:]` parsed EVERY row into memory first. With deque(maxlen)
    only `limit` rows are ever alive."""
    path = sc._path()
    _seed(path, 30_000, lambda i: {"ts": i, "model": "m", "seq": i, "pad": "x" * 400})
    tracemalloc.start()
    try:
        rows = sc.load(limit=100)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert [r["seq"] for r in rows] == list(range(29_900, 30_000))
    assert peak < 8 * 1024 * 1024, f"load() held the whole log in memory ({peak / 1e6:.1f} MB peak)"


def test_scorecard_load_still_filters_by_days(data_dir, scorecard_on):
    now = time.time()
    _seed(sc._path(), 10, lambda i: {"ts": now - i * 86400, "model": "m", "seq": i})
    assert [r["seq"] for r in sc.load(days=3.5)] == [0, 1, 2, 3]
    assert len(sc.load()) == 10
