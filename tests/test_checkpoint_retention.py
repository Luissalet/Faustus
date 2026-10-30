"""Shadow-repo checkpoints are pruned: a repo whose folder is gone goes at once,
one unused past the retention window goes, the oldest go while the total is
over the cap, and anything written in the last hour stays."""

import os
import shutil
import time

import pytest

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


@pytest.fixture
def env(tmp_path, monkeypatch):
    d = tmp_path / "data"
    monkeypatch.setenv("ODYSSEUS_DATA_DIR", str(d))
    import src.constants as consts
    monkeypatch.setattr(consts, "DATA_DIR", str(d), raising=False)
    values = {}
    import src.settings as st
    real = st.get_setting
    monkeypatch.setattr(st, "get_setting", lambda k, default=None: values[k] if k in values else real(k, default),
                        raising=False)
    from src import workspace_checkpoints as wc
    wc._EXCLUDE_CACHE.clear()
    wc._INCLUDED_BYTES.clear()
    wc._SKIPPED.clear()
    return wc, tmp_path, values


def _repo(wc, tmp_path, name, *, age_days=0.0, size=1000):
    ws = tmp_path / name
    ws.mkdir()
    (ws / "a.txt").write_text("x" * size)
    assert wc.checkpoint(str(ws))
    gd = wc.shadow_dir(str(ws))
    stamp = time.time() - age_days * 86400
    for root, dirs, files in os.walk(gd):
        for f in files + dirs:
            os.utime(os.path.join(root, f), (stamp, stamp))
    os.utime(gd, (stamp, stamp))
    return ws, gd


def test_a_repo_whose_folder_is_gone_is_removed(env):
    wc, tmp_path, _ = env
    ws, gd = _repo(wc, tmp_path, "gone", age_days=0.1)
    keep_ws, keep_gd = _repo(wc, tmp_path, "here", age_days=0.1)
    shutil.rmtree(ws)
    out = wc.prune(retention_days=14, total_mb=0)
    assert not os.path.isdir(gd)
    assert os.path.isdir(keep_gd)
    assert out["removed"] == 1 and out["reasons"] == {"workspace_gone": 1}


def test_a_repo_unused_past_the_window_expires(env):
    wc, tmp_path, _ = env
    _, old = _repo(wc, tmp_path, "old", age_days=20)
    _, fresh = _repo(wc, tmp_path, "fresh", age_days=2)
    out = wc.prune(retention_days=14, total_mb=0)
    assert not os.path.isdir(old) and os.path.isdir(fresh)
    assert out["reasons"] == {"expired": 1}
    # 0 switches the age rule off
    _, older = _repo(wc, tmp_path, "older", age_days=400)
    assert wc.prune(retention_days=0, total_mb=0)["removed"] == 0
    assert os.path.isdir(older)


def test_the_oldest_go_first_while_over_the_cap(env):
    wc, tmp_path, _ = env
    _, a = _repo(wc, tmp_path, "a", age_days=5)
    _, b = _repo(wc, tmp_path, "b", age_days=3)
    _, c = _repo(wc, tmp_path, "c", age_days=1)
    one = wc._dir_size_mb(c)
    out = wc.prune(retention_days=0, total_mb=one * 1.5)
    assert not os.path.isdir(a) and not os.path.isdir(b) and os.path.isdir(c)
    assert out["reasons"] == {"over_cap": 2} and out["kept"] == 1


def test_a_repo_written_in_the_last_hour_is_never_removed(env):
    wc, tmp_path, _ = env
    ws, gd = _repo(wc, tmp_path, "busy")
    shutil.rmtree(ws)
    out = wc.prune(retention_days=1, total_mb=0.0001)
    assert os.path.isdir(gd) and out["removed"] == 0


def test_a_workspace_busy_in_this_process_is_skipped(env):
    wc, tmp_path, _ = env
    ws, gd = _repo(wc, tmp_path, "locked", age_days=30)
    lock = wc._lock_for(wc._norm_root(str(ws)))
    with lock:
        assert wc.prune(retention_days=14, total_mb=0)["removed"] == 0
    assert os.path.isdir(gd)
    assert wc.prune(retention_days=14, total_mb=0)["removed"] == 1


def test_settings_drive_the_defaults(env):
    wc, tmp_path, values = env
    _, gd = _repo(wc, tmp_path, "s", age_days=10)
    values["agent_checkpoint_retention_days"] = 30
    values["agent_checkpoint_total_mb"] = 0
    assert wc.prune()["removed"] == 0
    values["agent_checkpoint_retention_days"] = 7
    assert wc.prune()["removed"] == 1 and not os.path.isdir(gd)
