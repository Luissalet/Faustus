"""Two fresh processes must not race schema creation on an empty file-backed DB.

Historical failure: ``table sessions already exists`` / ``chat_messages already
exists`` when two interpreters import ``core.database`` together against a new
``DATABASE_URL``. ``create_all(checkfirst=True)`` is not enough across processes.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

_WORKER = r"""
import sys, time
from pathlib import Path
barrier = Path(sys.argv[1])
deadline = time.time() + 60
while not barrier.exists():
    if time.time() > deadline:
        raise SystemExit("barrier timeout")
    time.sleep(0.01)
import core.database  # noqa: F401
print("ok", flush=True)
"""


def test_two_processes_can_init_schema_on_empty_db(tmp_path):
    barrier = tmp_path / "go"
    data = tmp_path / "data"
    data.mkdir()
    db = data / "app.db"
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite:///{db.as_posix()}",
        "ODYSSEUS_DATA_DIR": str(data),
        "FAUSTUS_DATA_DIR": str(data),
        "PYTHONUTF8": "1",
    }
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _WORKER, str(barrier)],
            cwd=str(REPO),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    time.sleep(0.25)
    barrier.write_text("go", encoding="utf-8")
    results = []
    for p in procs:
        out, err = p.communicate(timeout=120)
        results.append((p.returncode, out, err))
    codes = [c for c, _, _ in results]
    assert codes == [0, 0], results
    assert db.exists()
    assert all("ok" in out for _, out, _ in results)
