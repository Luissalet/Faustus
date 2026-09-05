"""B-021: the uploads.json index must survive more than one process.

``threading.Lock`` only orders writers inside one interpreter, so two
processes sharing an upload directory could each read the index, add a
different row and replace the file in turn - the later write dropping the
earlier row and orphaning its bytes. These tests drive the real
``UploadHandler`` from separate OS processes and pin the three properties the
fix rests on: the advisory file lock excludes other processes, entering the
guard reloads the index from disk instead of trusting the per-process cache,
and a concurrent upload/dedupe/reserve/cleanup mix loses nothing.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.upload_handler import UploadHandler  # noqa: E402


def _make_dirs(tmp_path: Path) -> tuple[Path, Path]:
    base = tmp_path / "base"
    upload = tmp_path / "base" / "uploads"
    upload.mkdir(parents=True)
    return base, upload


def _db_path(upload: Path) -> Path:
    return upload / "uploads.json"


# --- the audit's two-process test -------------------------------------------

# Each worker uploads a unique file, re-uploads it to hit the dedupe branch,
# reserves it against cleanup, and periodically runs cleanup - the four index
# mutators the audit names - while the other worker does the same. The
# _atomic_write_json stall widens the read-modify-write window so a lost
# update is a certainty rather than a coin flip on a fast disk.
_WORKER = r"""
import io, os, sys, time
sys.path.insert(0, sys.argv[1])
from types import SimpleNamespace
from src.upload_handler import UploadHandler

base, upload, tag, count = sys.argv[2], sys.argv[3], sys.argv[4], int(sys.argv[5])
handler = UploadHandler(base, upload)

_real_write = handler._atomic_write_json
def _slow_write(path, data, **kw):
    time.sleep(0.004)
    return _real_write(path, data, **kw)
handler._atomic_write_json = _slow_write

owner = "owner-" + tag
for i in range(count):
    payload = ("%s-%d" % (tag, i)).encode("utf-8") * 8
    name = "%s_%d.txt" % (tag, i)
    # Fresh client IP per iteration: the per-process rate limiter is not what
    # this test is about.
    ip = "10.%s.0.%d" % (tag, i)
    first = handler.save_upload(
        SimpleNamespace(filename=name, file=io.BytesIO(payload)), ip, owner=owner
    )
    dup = handler.save_upload(
        SimpleNamespace(filename=name, file=io.BytesIO(payload)), ip, owner=owner
    )
    assert dup["id"] == first["id"], "dedupe returned a different id"
    reserved = handler.reserve_upload(first["id"], owner=owner)
    assert reserved is not None, "reserve_upload refused a row it just wrote"
    if i % 7 == 0:
        handler.cleanup_old_uploads(set(), set())
print("OK")
"""

WORKER_UPLOADS = 40


def test_two_processes_uploading_deduping_reserving_and_cleaning_keep_every_row(tmp_path):
    base, upload = _make_dirs(tmp_path)
    _db_path(upload).write_text("{}", encoding="utf-8")

    children = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                _WORKER,
                str(PROJECT_ROOT),
                str(base),
                str(upload),
                tag,
                str(WORKER_UPLOADS),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(PROJECT_ROOT),
        )
        for tag in ("1", "2")
    ]
    outputs = [child.communicate(timeout=300) for child in children]
    for child, (out, err) in zip(children, outputs):
        assert child.returncode == 0, f"worker failed:\n{out}\n{err}"

    index = json.loads(_db_path(upload).read_text(encoding="utf-8"))
    rows_per_owner: dict[str, int] = {}
    for info in index.values():
        rows_per_owner[info["owner"]] = rows_per_owner.get(info["owner"], 0) + 1

    assert rows_per_owner == {
        "owner-1": WORKER_UPLOADS,
        "owner-2": WORKER_UPLOADS,
    }, f"lost index rows across processes: {rows_per_owner}"

    # Every surviving row must still point at bytes on disk, and every byte on
    # disk must still have a row: a lost update orphans one or the other.
    indexed_paths = {os.path.realpath(info["path"]) for info in index.values()}
    for path in indexed_paths:
        assert os.path.isfile(path), f"index row without bytes: {path}"

    on_disk = set()
    for root, _dirs, files in os.walk(upload):
        if os.path.realpath(root) == os.path.realpath(str(upload)):
            continue
        for name in files:
            on_disk.add(os.path.realpath(os.path.join(root, name)))
    assert on_disk == indexed_paths, (
        f"orphaned bytes without index rows: {sorted(on_disk - indexed_paths)}"
    )


# --- cross-process exclusion -------------------------------------------------

_HOLDER = r"""
import sys, time
sys.path.insert(0, sys.argv[1])
from src.upload_handler import UploadHandler

handler = UploadHandler(sys.argv[2], sys.argv[3])
with handler._index_lock:
    open(sys.argv[4], "w").write("held")
    time.sleep(float(sys.argv[5]))
"""

HOLD_SECONDS = 2.0


def test_index_lock_blocks_a_second_process(tmp_path):
    base, upload = _make_dirs(tmp_path)
    _db_path(upload).write_text("{}", encoding="utf-8")
    marker = tmp_path / "held.marker"

    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _HOLDER,
            str(PROJECT_ROOT),
            str(base),
            str(upload),
            str(marker),
            str(HOLD_SECONDS),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(PROJECT_ROOT),
    )
    try:
        deadline = time.monotonic() + 60
        while not marker.exists():
            assert holder.poll() is None, holder.communicate()
            assert time.monotonic() < deadline, "holder never took the lock"
            time.sleep(0.02)

        handler = UploadHandler(str(base), str(upload))
        started = time.monotonic()
        with handler._index_lock:
            waited = time.monotonic() - started
    finally:
        holder.wait(timeout=60)

    assert waited > HOLD_SECONDS / 4, (
        f"acquired the index lock after {waited:.3f}s while another process held it - "
        "the guard is not excluding other processes"
    )


# --- reload from disk inside the lock ---------------------------------------

def test_index_lock_drops_the_process_local_cache_on_entry(tmp_path):
    """The stat signature that validates the cache can miss a competitor's
    rewrite (same size, coarse timestamp), so the guard must not trust it."""
    base, upload = _make_dirs(tmp_path)
    handler = UploadHandler(str(base), str(upload))
    db = _db_path(upload)
    on_disk = {"owner:real": {"id": "a" * 32, "owner": "owner", "hash": "h"}}
    handler._atomic_write_json(str(db), on_disk)

    # Poison the cache while leaving the signature agreeing with the files on
    # disk, i.e. exactly what a same-size same-timestamp rewrite produces.
    handler._index_cache = {"owner:phantom": {"id": "b" * 32, "owner": "owner"}}
    handler._index_signature = handler._upload_index_signature(
        (str(db), str(db) + ".bak")
    )
    assert handler._load_upload_index() == handler._index_cache

    with handler._index_lock:
        assert handler._load_upload_index() == on_disk
