"""A workspace whose snapshot alone would pass agent_checkpoint_max_repo_mb
(a media library of many small files) is not snapshotted: the checkpoint is
skipped with a reason instead of writing gigabytes into the data folder."""

import shutil

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
    ws = tmp_path / "ws"
    ws.mkdir()
    return wc, ws, values


def test_a_normal_workspace_is_measured_and_snapshotted(env):
    wc, ws, _ = env
    (ws / "a.txt").write_text("x" * 1000)
    (ws / "node_modules").mkdir()
    (ws / "node_modules" / "big.js").write_text("y" * 50_000)   # excluded folder
    (ws / "clip.mp4").write_bytes(b"z" * 40_000)                  # excluded pattern
    cp = wc.checkpoint(str(ws))
    assert cp and cp["created"]
    assert wc._INCLUDED_BYTES[wc.shadow_dir(str(ws))] == 1000
    assert wc.status(str(ws))["skipped"] is None


def test_a_workspace_over_the_cap_is_skipped_with_a_reason(env):
    wc, ws, values = env
    values["agent_checkpoint_max_repo_mb"] = 0.01          # ~10 KB
    for i in range(5):
        (ws / f"img{i}.png").write_bytes(b"p" * 4_000)      # 20 KB in small files
    assert wc.checkpoint(str(ws)) is None
    skipped = wc.status(str(ws))["skipped"]
    assert skipped["reason"] == "too_big" and skipped["cap_mb"] == 0.01
    # Nothing was written into the shadow repo's object store.
    assert wc._head(wc._norm_root(str(ws))) is None


def test_raising_the_cap_lets_it_through(env):
    wc, ws, values = env
    values["agent_checkpoint_max_repo_mb"] = 0.01
    (ws / "big.svg").write_bytes(b"s" * 30_000)
    assert wc.checkpoint(str(ws)) is None
    values["agent_checkpoint_max_repo_mb"] = 64
    wc._EXCLUDE_CACHE.clear()
    cp = wc.checkpoint(str(ws))
    assert cp and cp["created"]
    assert wc.status(str(ws))["skipped"] is None
