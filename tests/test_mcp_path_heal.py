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
