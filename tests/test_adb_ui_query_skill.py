import importlib
import json
import subprocess
import sys
from pathlib import Path

PACKAGE = "com.example.dex"
FIELD = "android.widget.EditText"
RECT = (0, 190, 720, 1460)


def query(monkeypatch):
    directory = Path(__file__).resolve().parents[1] / "skills/library/android-emulator-qa/scripts"
    monkeypatch.syspath_prepend(str(directory))
    return importlib.import_module("adb_ui_query").find_nodes


def element(label="Mote", *, text="", bounds="[20,410][510,490]", package=PACKAGE, kind=FIELD, enabled=True):
    return (
        f'<node package="{package}" class="{kind}" enabled="{str(enabled).lower()}" '
        f'content-desc="{label}" text="{text}" bounds="{bounds}" focused="true"/>'
    )


def test_readback_uses_exact_package_label_and_editable_class(monkeypatch):
    find_nodes = query(monkeypatch)
    tree = "<hierarchy>" + "".join([
        element(kind="android.widget.TextView"), element(package="com.android.settings"),
        element(label="Mote de captura"), element(text="Regalos verificado"),
    ]) + "</hierarchy>"
    assert find_nodes(tree, PACKAGE, "Mote", RECT, FIELD, 48) == [{
        "label": "Mote", "text": "Regalos verificado", "class": FIELD,
        "bounds": [20, 410, 510, 490], "center": [265, 450], "focused": True,
    }]


def test_rejects_controls_under_fixed_edges_or_clipped_below_minimum(monkeypatch):
    find_nodes = query(monkeypatch)
    tree = "<hierarchy>" + "".join([
        element(bounds="[20,140][510,205]"), element(bounds="[20,1430][510,1510]"),
        element(bounds="[20,1428][510,1456]"), element(bounds="[20,410][510,490]", enabled=False),
        element(bounds="[20,410][20,490]"),
    ]) + "</hierarchy>"
    assert find_nodes(tree, PACKAGE, "Mote", RECT, FIELD, 48) == []


def test_returns_both_observed_matches_without_choosing_an_ambiguous_field(monkeypatch):
    find_nodes = query(monkeypatch)
    tree = "<hierarchy>" + element(text="Uno") + element(text="Dos", bounds="[20,600][510,680]") + "</hierarchy>"
    assert [item["text"] for item in find_nodes(tree, PACKAGE, "Mote", RECT, FIELD, 48)] == ["Uno", "Dos"]


def run_cli(monkeypatch, tmp_path, capsys, readbacks, foregrounds, wait=6000):
    query(monkeypatch)
    module = importlib.import_module("adb_ui_query")
    adb = tmp_path / "adb"
    adb.touch()
    monkeypatch.setattr(sys, "argv", [
        "adb_ui_query.py", "--adb", str(adb), "--serial", "emulator-5554", "--package", PACKAGE,
        "--label", "Mote", "--class", FIELD, "--rect", *map(str, RECT), "--wait-tree-ms", str(wait),
    ])
    commands = []

    def observe(args, *, timeout):
        command = args[3:]
        commands.append(command)
        if command[:2] == ["exec-out", "cat"]:
            value = next(readbacks)
            if isinstance(value, Exception):
                raise value
            return value.encode()
        return b""

    monkeypatch.setattr(module.subprocess, "check_output", observe)
    monkeypatch.setattr(module, "check_foreground", lambda *_: next(foregrounds))
    clock = iter([0, 0.2, 0.4, 0.6, 0.8, 1.0, 7.0])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    status = module.main()
    return status, json.loads(capsys.readouterr().out), commands


def test_tree_wait_recovers_missing_snapshot_without_touching_device(monkeypatch, tmp_path, capsys):
    error = subprocess.CalledProcessError(1, ["adb", "cat"])
    good_tree = "<hierarchy>" + element(text="Readback") + "</hierarchy>"
    status, result, commands = run_cli(monkeypatch, tmp_path, capsys,
        iter([error, good_tree]), iter([{"matched": True}]*3))
    assert status == 0 and result["node"]["text"] == "Readback"
    snapshots = [cmd[-1] for cmd in commands if cmd[:3] == ["shell", "uiautomator", "dump"]]
    assert len(snapshots) == len(set(snapshots)) == 2
    assert all("input" not in cmd for cmd in commands)


def test_tree_wait_stops_if_the_app_loses_foreground(monkeypatch, tmp_path, capsys):
    status, result, commands = run_cli(monkeypatch, tmp_path, capsys,
        iter(["<hierarchy/>"]), iter([{"matched": True},{"matched": True},{"matched": False}]))
    assert status == 1 and result["foreground"] == {"matched": False}
    assert sum(cmd[:3] == ["shell", "uiautomator", "dump"] for cmd in commands) == 1


def test_tree_wait_preserves_transport_timeout(monkeypatch, tmp_path, capsys):
    error = subprocess.TimeoutExpired(["adb", "cat"], 15)
    status, result, commands = run_cli(monkeypatch, tmp_path, capsys,
        iter([error]), iter([{"matched": True}]))
    assert status == 2 and "timed out" in result["error"]
    assert sum(cmd[:3] == ["shell", "uiautomator", "dump"] for cmd in commands) == 1
