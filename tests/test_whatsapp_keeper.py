"""The WhatsApp bridge is kept running where Faustus runs, and only there."""

import json

import pytest

from src import whatsapp_bridge as wa
from src import whatsapp_keeper as keeper


@pytest.fixture()
def env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    (data / "whatsapp" / "auth").mkdir(parents=True)
    (data / "runtime").mkdir()
    monkeypatch.setattr("src.constants.DATA_DIR", str(data), raising=False)
    monkeypatch.setattr(wa, "data_dir", lambda: str(data / "whatsapp"))
    monkeypatch.setattr(keeper, "enabled", lambda: True)
    calls = {"start": 0, "stop": 0, "running": None}
    monkeypatch.setattr(wa, "probe", lambda timeout=3.0: calls["running"])

    def start(owner=""):
        calls["start"] += 1
        calls["running"] = {"status": "connected"}
        return {"started": True, "pid": 123}

    def stop():
        calls["stop"] += 1
        calls["running"] = None
        return {"stopped": True}

    monkeypatch.setattr(wa, "start", start)
    monkeypatch.setattr(wa, "stop", stop)
    return data, calls


def _pair(data):
    (data / "whatsapp" / "auth" / "creds.json").write_text("{}", encoding="utf-8")


def test_starts_a_paired_bridge_that_is_down(env):
    data, calls = env
    _pair(data)
    assert keeper.tick()["action"] == "started"
    assert keeper.tick()["action"] == "running"
    assert calls["start"] == 1


def test_never_starts_without_a_session(env):
    _data, calls = env
    assert keeper.tick()["action"] == "not_paired"
    assert calls["start"] == 0


def test_respects_a_deliberate_stop(env):
    data, calls = env
    _pair(data)
    wa.set_wanted(False)
    assert keeper.tick()["action"] == "stopped_by_person"
    wa.set_wanted(True)
    assert keeper.tick()["action"] == "started"


def test_a_copy_moved_elsewhere_never_runs_the_session(env):
    data, calls = env
    _pair(data)
    (data / "runtime" / "placement.json").write_text(json.dumps({"location": "sparks", "node": "Spark3"}), encoding="utf-8")
    assert keeper.tick()["action"] == "elsewhere"
    calls["running"] = {"status": "connected"}
    assert keeper.tick()["action"] == "stopped_here"
    assert calls["stop"] == 1 and calls["start"] == 0


def test_health_flags_broken_keys_only_when_nothing_readable_arrives():
    assert wa.health(None)["state"] == "stopped"
    assert wa.health({"status": "qr"})["needs_pairing"] is True
    broken = {"status": "connected", "undecryptable": {"count": 40, "lastAt": 2000}, "lastInboundAt": 1000}
    assert wa.health(broken)["state"] == "broken_keys"
    fine = {"status": "connected", "undecryptable": {"count": 40, "lastAt": 2000}, "lastInboundAt": 3000}
    assert wa.health(fine) == {"ok": True, "state": "connected", "needs_pairing": False}


def test_installed_follows_the_pinned_library(tmp_path, monkeypatch):
    bridge = tmp_path / "bridge"
    (bridge / "node_modules" / "@whiskeysockets" / "baileys").mkdir(parents=True)
    monkeypatch.setattr(wa, "bridge_dir", lambda: str(bridge))
    (bridge / "package.json").write_text(json.dumps({"dependencies": {"@whiskeysockets/baileys": "7.0.0-rc14"}}), encoding="utf-8")
    lib = bridge / "node_modules" / "@whiskeysockets" / "baileys" / "package.json"
    lib.write_text(json.dumps({"version": "6.7.24"}), encoding="utf-8")
    assert wa.installed() is False
    lib.write_text(json.dumps({"version": "7.0.0-rc14"}), encoding="utf-8")
    assert wa.installed() is True
