"""Control center (src/process_center.py, routes/process_center_routes.py):
what runs because of Faustus and a human-only Stop that needs the listing's
creation time as proof."""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time

import pytest

from src import process_center as pc

psutil = pytest.importorskip("psutil")


def _free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def server():
    port = _free_port()
    proc = subprocess.Popen([sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 10
    while time.time() < deadline:
        if any(port in r["ports"] for r in pc.snapshot(include_watched=False)["ports"]):
            break
        time.sleep(0.2)
    yield port, proc
    if proc.poll() is None:
        proc.kill()
        proc.wait(timeout=5)


def _family(proc) -> set:
    """The Popen pid and its children: on Windows a venv `python.exe` is a
    launcher whose child is the interpreter that really holds the port."""
    pids = {proc.pid}
    try:
        pids |= {c.pid for c in psutil.Process(proc.pid).children(recursive=True)}
    except Exception:  # noqa: BLE001
        pass
    return pids


def _row(port):
    rows = [r for r in pc.snapshot(include_watched=False)["ports"] if port in r["ports"]]
    assert rows, "the listener should be in the ports list"
    return rows[0]


def test_snapshot_lists_the_port_with_its_process_and_attributes_it_to_faustus(server):
    port, proc = server
    row = _row(port)
    assert row["pid"] in _family(proc)
    assert "http.server" in row["cmdline"]
    assert row["created_at"] and row["uptime_s"] is not None
    assert row["origin"] == "faustus", row      # a child of this interpreter
    assert row["protected"] is False


def test_snapshot_marks_self_protected_and_connector_ports(server):
    port, _ = server
    snap = pc.snapshot(connectors=[{"name": "Jobhunter's Hoard", "app_url": f"http://127.0.0.1:{port}"}],
                       include_watched=False)
    row = next(r for r in snap["ports"] if port in r["ports"])
    assert row["origin"] == "connector" and "Jobhunter" in row["label"]
    assert isinstance(snap["jobs"], list) and snap["available"] is True


def test_stop_needs_the_creation_time_and_refuses_recycled_self_and_os(server):
    port, proc = server
    row = _row(port)
    assert pc.stop(row["pid"], None)["code"] == "no_proof"
    assert pc.stop(row["pid"], row["created_at"] + 30)["code"] == "recycled"
    assert pc.stop(os.getpid(), time.time())["code"] == "self"
    assert pc.stop(0, None)["code"] == "bad_pid"
    assert proc.poll() is None, "nothing was signalled by the refusals"


def test_stop_kills_the_tree_and_a_second_stop_is_gone(server):
    port, proc = server
    row = _row(port)
    out = pc.stop(row["pid"], row["created_at"])
    assert out["ok"] and row["pid"] in out["signalled"], out
    proc.wait(timeout=10)
    assert pc.stop(row["pid"], row["created_at"])["code"] == "gone"
    assert pc.stop_port(port)["code"] == "gone"


def test_stop_port_finds_the_listener_by_port(server):
    port, proc = server
    out = pc.stop_port(port)
    assert out["ok"] and out["port"] == port and set(out["signalled"]) & _family(proc), out
    proc.wait(timeout=10)


def test_children_without_ports_are_listed_under_faustus_and_self_never_appears():
    # A child python that holds no port is still visible (it is what the
    # agent leaves behind); the interpreter itself is `self` and never a row.
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        time.sleep(0.5)
        snap = pc.snapshot(include_watched=True)
        row = next((r for r in snap["faustus"] if r["pid"] == child.pid), None)
        assert row and row["origin"] == "faustus" and row["ports"] == [], snap["faustus"][:3]
        everything = snap["ports"] + snap["faustus"] + snap["watched"]
        assert os.getpid() not in {r["pid"] for r in everything}
        assert len({r["pid"] for r in everything}) == len(everything), "a pid appears once"
    finally:
        child.kill()
        child.wait(timeout=5)
    assert "cursor" in pc.watchlist() and "chatgpt" in pc.watchlist()
    assert pc._base_name("Cursor.exe") == "cursor" and pc._base_name("python3.11") == "python"


def test_routes_stop_is_human_only_and_list_answers():
    from routes.process_center_routes import setup_process_center_routes
    from core.middleware import INTERNAL_TOOL_HEADER, INTERNAL_TOOL_TOKEN
    from fastapi import HTTPException
    router = setup_process_center_routes()
    routes = {(next(iter(r.methods)), r.path): r.endpoint for r in router.routes}
    assert ("GET", "/api/process-center") in routes and ("POST", "/api/process-center/stop") in routes

    class Req:
        def __init__(self, headers=None):
            self.headers = headers or {}
            self.state = type("S", (), {"current_user": "admin", "is_admin": True})()
            self.client = type("C", (), {"host": "127.0.0.1"})()

    from routes.process_center_routes import StopBody
    with pytest.raises(HTTPException) as exc:
        asyncio.run(routes[("POST", "/api/process-center/stop")](
            StopBody(pid=os.getpid(), created_at=time.time()), Req({INTERNAL_TOOL_HEADER: INTERNAL_TOOL_TOKEN})))
    assert exc.value.status_code == 403
