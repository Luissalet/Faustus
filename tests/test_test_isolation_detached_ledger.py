"""Tests never write the live detached-process ledger (#1113).

`process_launch.detached_ledger_path()` lives under BASE_DIR/data/runtime, outside the
ODYSSEUS_DATA_DIR redirection, and `server_runtime` reads it on shutdown to spare the
processes listed there. A test that faked a spawn (pid 4242, `node server.js`) wrote
that entry into the running checkout's ledger.
"""
import os
from pathlib import Path

from src import process_launch


def test_ledger_path_is_redirected_for_every_test(tmp_path):
    path = Path(process_launch.detached_ledger_path()).resolve()
    live = (Path(__file__).resolve().parents[1] / "data" / "runtime" / "detached.json").resolve()
    assert path != live
    assert tmp_path.resolve() in path.parents


def test_a_faked_spawn_writes_only_the_redirected_ledger(monkeypatch, tmp_path):
    class FakeProc:
        pid = 4242

    monkeypatch.setattr(process_launch.subprocess, "Popen", lambda argv, **kw: FakeProc())
    monkeypatch.setattr(process_launch.process_ownership, "note_started", lambda *a, **k: None)
    monkeypatch.setattr(process_launch.process_ownership, "creation_time", lambda pid: None)
    process_launch.spawn_detached(["node", "server.js"], cwd=str(tmp_path), env=None,
                                  log_path=str(tmp_path / "out.log"))
    assert os.path.exists(process_launch.detached_ledger_path())
    assert tmp_path.resolve() in Path(process_launch.detached_ledger_path()).resolve().parents
