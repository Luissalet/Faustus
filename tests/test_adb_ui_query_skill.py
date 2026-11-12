import importlib
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
