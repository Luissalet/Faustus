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


def element(label="Mote", *, text="", bounds="[20,410][510,490]", package=PACKAGE, kind=FIELD, enabled=True, focused=True):
    return (
        f'<node package="{package}" class="{kind}" enabled="{str(enabled).lower()}" '
        f'content-desc="{label}" text="{text}" bounds="{bounds}" focused="{str(focused).lower()}"/>'
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


def run_cli(monkeypatch, tmp_path, capsys, readbacks, foregrounds, wait=6000, selection=("--label", "Mote", "--class", FIELD), dump_error=None):
    query(monkeypatch)
    module = importlib.import_module("adb_ui_query")
    adb = tmp_path / "adb"
    adb.touch()
    monkeypatch.setattr(sys, "argv", [
        "adb_ui_query.py", "--adb", str(adb), "--serial", "emulator-5554", "--package", PACKAGE,
        *selection, "--rect", *map(str, RECT), "--wait-tree-ms", str(wait),
    ])
    commands = []

    def observe(args, *, timeout):
        command = args[3:]
        commands.append(command)
        if command[:3] == ["shell", "uiautomator", "dump"] and dump_error is not None:
            if sum(item[:3] == command[:3] for item in commands) == 1:
                raise dump_error
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


def test_list_recovers_after_android_dump_process_crashes(monkeypatch, tmp_path, capsys):
    failure = subprocess.CalledProcessError(139, ["adb", "uiautomator"], stderr=b"invalid pthread_t")
    tree = "<hierarchy>" + element(label="Ruta 13") + element(label="Ruta 13") + element(label="", text="Tabla") + "</hierarchy>"
    status, result, commands = run_cli(monkeypatch, tmp_path, capsys,
        iter([tree]), iter([{"matched": True}]*3), selection=("--list",), dump_error=failure)
    assert status == 0 and result["matchCount"] == 3
    assert [node["label"] for node in result["nodes"]] == ["Ruta 13", "Ruta 13", "Tabla"]
    assert sum(cmd[:3] == ["shell", "uiautomator", "dump"] for cmd in commands) == 2
    assert all("input" not in cmd for cmd in commands)


def test_list_reports_full_count_and_bounds_output(monkeypatch, tmp_path, capsys):
    tree = "<hierarchy>" + element(label="Control") * 81 + element(label="Blocked", enabled=False) + "</hierarchy>"
    status, result, _ = run_cli(monkeypatch, tmp_path, capsys,
        iter([tree]), iter([{"matched": True}]*2), selection=("--list",))
    assert status == 0 and result["matchCount"] == 81 and len(result["nodes"]) == 80


def test_list_waits_for_required_labels_in_one_snapshot(monkeypatch, tmp_path, capsys):
    first = "<hierarchy>" + element(label="Pokédex") + "</hierarchy>"
    second = "<hierarchy>" + element(label="Pokédex") + element(label="Colección", text="1/1025") + "</hierarchy>"
    status, result, commands = run_cli(monkeypatch, tmp_path, capsys,
        iter([first, second]), iter([{"matched": True}]*4),
        selection=("--list", "--require-label", "Pokédex", "--require-label", "1/1025"))
    assert status == 0 and result["matched"] and result["missingLabels"] == []
    assert result["requiredNodes"]["1/1025"]["label"] == "Colección"
    assert sum(cmd[:3] == ["shell", "uiautomator", "dump"] for cmd in commands) == 2
    assert all("input" not in cmd for cmd in commands)


def test_list_reports_the_missing_context_without_approving_a_partial_tree(monkeypatch, tmp_path, capsys):
    tree = "<hierarchy>" + element(label="Pokédex") + "</hierarchy>"
    status, result, _ = run_cli(monkeypatch, tmp_path, capsys,
        iter([tree]), iter([{"matched": True}]*2), wait=0,
        selection=("--list", "--require-label", "Pokédex", "--require-label", "1/1025"))
    assert status == 1 and not result["matched"]
    assert result["missingLabels"] == ["1/1025"]
    assert list(result["requiredNodes"]) == ["Pokédex"]


def test_exact_field_waits_for_focus_in_a_fresh_tree(monkeypatch, tmp_path, capsys):
    unfocused = "<hierarchy>" + element(text="Before", focused=False) + "</hierarchy>"
    focused = "<hierarchy>" + element(text="Before", focused=False) + element(text="Ready") + "</hierarchy>"
    status, result, commands = run_cli(
        monkeypatch, tmp_path, capsys, iter([unfocused, focused]), iter([{"matched": True}]*4),
        selection=("--label", "Mote", "--class", FIELD, "--require-focus"),
    )
    assert status == 0 and result["node"]["focused"] and result["node"]["text"] == "Ready"
    assert sum(command[:3] == ["shell", "uiautomator", "dump"] for command in commands) == 2
    assert all("input" not in command for command in commands)


def test_visible_unfocused_field_is_not_ready_for_typing(monkeypatch, tmp_path, capsys):
    tree = "<hierarchy>" + element(text="Visible", focused=False) + "</hierarchy>"
    status, result, _ = run_cli(
        monkeypatch, tmp_path, capsys, iter([tree]), iter([{"matched": True}]*2), wait=0,
        selection=("--label", "Mote", "--class", FIELD, "--require-focus"),
    )
    assert status == 1 and not result["matched"] and result["matchCount"] == 0


def test_readback_uses_the_observed_keyboard_edge(monkeypatch, tmp_path, capsys):
    tree = "<hierarchy>" + element(bounds="[20,978][510,1052]") + element(bounds="[20,1010][510,1090]") + "</hierarchy>"
    foreground = {"matched": True, "imeMatched": True, "imeBounds": [0, 1069, 720, 1600]}
    status, result, commands = run_cli(
        monkeypatch, tmp_path, capsys, iter([tree]), iter([foreground] * 2), wait=0,
        selection=("--label", "Mote", "--require-focus", "--above-ime", "--expect-ime", "visible"),
    )
    assert status == 0 and result["node"]["bounds"] == [20, 978, 510, 1052]
    assert all("input" not in command for command in commands)


def test_readback_rejects_bounds_when_the_keyboard_moves(monkeypatch, tmp_path, capsys):
    tree = "<hierarchy>" + element(bounds="[20,978][510,1052]") + "</hierarchy>"
    bounds = [{"matched": True, "imeMatched": True, "imeBounds": [0, top, 720, 1600]} for top in (1069, 1050)]
    status, result, _ = run_cli(
        monkeypatch, tmp_path, capsys, iter([tree]), iter(bounds), wait=0,
        selection=("--label", "Mote", "--require-focus", "--above-ime", "--expect-ime", "visible"),
    )
    assert status == 1 and result["reason"] == "ime-moved-during-observation"
    assert "node" not in result
