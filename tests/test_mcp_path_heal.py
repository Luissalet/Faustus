import sys

from src.mcp_path_heal import heal


def _root(tmp_path):
    root = tmp_path / "app"
    (root / "bridges" / "rest_mcp").mkdir(parents=True)
    (root / "bridges" / "rest_mcp" / "server.py").write_text("# bridge", encoding="utf-8")
    return root


def test_a_bridge_saved_from_an_old_install_is_rerooted(tmp_path):
    root = _root(tmp_path)
    old_py = str(tmp_path / "old" / "venv" / "Scripts" / "python.exe")
    old_script = str(tmp_path / "old") + "/bridges/rest_mcp/server.py"
    cmd, args, notes = heal(old_py, [old_script, "--port", "1"], root=root)
    assert args[0] == str(root / "bridges" / "rest_mcp" / "server.py")
    assert args[1:] == ["--port", "1"]
    assert cmd == sys.executable
    assert len(notes) == 2


def test_windows_style_paths_are_rerooted_too(tmp_path):
    root = _root(tmp_path)
    cmd, args, notes = heal(r"D:\Old\venv\Scripts\python.exe", [r"D:\Old\bridges\rest_mcp\server.py"], root=root)
    assert args == [str(root / "bridges" / "rest_mcp" / "server.py")]
    assert cmd == sys.executable


def test_existing_paths_and_foreign_scripts_are_left_alone(tmp_path):
    root = _root(tmp_path)
    real = tmp_path / "hoard" / "mcp_server.py"
    real.parent.mkdir()
    real.write_text("x", encoding="utf-8")
    assert heal("python", [str(real)], root=root) == ("python", [str(real)], [])
    missing = str(tmp_path / "elsewhere" / "tool" / "run.py")
    cmd, args, notes = heal("/nope/python", [missing], root=root)
    assert (cmd, args, notes) == ("/nope/python", [missing], [])


def test_an_interpreter_that_exists_is_kept(tmp_path):
    root = _root(tmp_path)
    cmd, args, notes = heal(sys.executable, [str(tmp_path / "old") + "/bridges/rest_mcp/server.py"], root=root)
    assert cmd == sys.executable and len(notes) == 1


def test_env_paths_from_an_old_install_are_rerooted(tmp_path):
    """Live: the REST bridge's script was healed but REST_MANIFEST still
    pointed at the old install and the bridge exited on start."""
    from src.mcp_path_heal import heal_env
    root = tmp_path / "app"
    manifest = root / "bridges" / "rest_mcp" / "manifests" / "gepetto.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}", encoding="utf-8")
    env = {
        "REST_MANIFEST": "D:\\LocalAI\\oldname/bridges/rest_mcp/manifests/gepetto.json",
        "REST_BASE_URL": "http://127.0.0.1:5000",
        "OTHER": "C:\\somewhere\\that\\is\\gone.json",
    }
    out, notes = heal_env(env, root)
    assert out["REST_MANIFEST"] == str(manifest)
    assert out["REST_BASE_URL"] == "http://127.0.0.1:5000"
    assert out["OTHER"] == env["OTHER"]          # no matching tail here: left as it was
    assert len(notes) == 1 and "REST_MANIFEST" in notes[0]
