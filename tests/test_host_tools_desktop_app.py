"""On Windows the desktop tools are offered only while the desktop app runs.

Seen live (22-10, session c9bb4580): Faustus run from the web launcher, the
desktop app closed; every desktop tool waited 4 s and failed with "Open the
Faustus desktop app", and the model, offered only those, spent 15 rounds.
"""
import json
import time

import pytest

from src import host_tools as ht


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    ht._app_cache[0] = 0.0
    ht._app_cache[1] = False
    monkeypatch.setattr(ht, "_runtime_dir", lambda: str(tmp_path))
    monkeypatch.setattr(ht, "_app_process_running", lambda: False)
    monkeypatch.setattr(ht.os, "name", "nt")
    monkeypatch.delenv("FAUSTUS_HOST_TOOLS_ON", raising=False)
    ht.unavailable_tools.cache_clear()
    yield
    ht.unavailable_tools.cache_clear()
    ht._app_cache[0] = 0.0


def _beat(tmp_path, updated):
    (tmp_path / "desktop-app.json").write_text(json.dumps({"pid": 1, "updated": updated}), encoding="utf-8")


def test_closed_app_withdraws_every_desktop_tool():
    off = ht.turn_unavailable_tools()
    for name in ("desktop_screenshot", "desktop_list_windows", "desktop_click", "desktop_snapshot", "desktop_find", "desktop_act"):
        assert name in off


def test_a_fresh_heartbeat_offers_them(tmp_path):
    _beat(tmp_path, time.time())
    assert not ({"desktop_screenshot", "desktop_snapshot"} & ht.turn_unavailable_tools())


def test_a_stale_or_closing_heartbeat_withdraws_them(tmp_path):
    _beat(tmp_path, time.time() - 60)
    assert "desktop_screenshot" in ht.turn_unavailable_tools()
    ht._app_cache[0] = 0.0
    _beat(tmp_path, 0)
    assert "desktop_screenshot" in ht.turn_unavailable_tools()


def test_an_older_app_build_is_recognised_by_its_process(monkeypatch):
    monkeypatch.setattr(ht, "_app_process_running", lambda: True)
    assert "desktop_screenshot" not in ht.turn_unavailable_tools()


def test_the_operator_override_still_forces_them_on(monkeypatch):
    monkeypatch.setenv("FAUSTUS_HOST_TOOLS_ON", "desktop_screenshot")
    off = ht.turn_unavailable_tools()
    assert "desktop_screenshot" not in off and "desktop_click" in off


def test_other_hosts_need_no_app(monkeypatch):
    monkeypatch.setattr(ht.os, "name", "posix")
    assert ht.desktop_app_running()


def test_the_answer_is_cached_briefly(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(ht, "_app_process_running", lambda: calls.append(1) or False)
    ht.turn_unavailable_tools()
    ht.turn_unavailable_tools()
    assert len(calls) == 1
