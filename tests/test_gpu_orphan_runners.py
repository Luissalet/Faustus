"""Orphaned model runners (src/gpu_placement.orphan_runners / release_orphan).

Seen live on the two-card box: restarting Ollama (Stop-Process on ollama*)
left two `llama-server.exe` children alive holding 13 GB on the 5060 Ti with
`ollama ps` empty — every gauge read "other 13 GB" and nothing said why.
"""
from __future__ import annotations

import asyncio
import json
import sys
import types

import pytest

from src import gpu_placement as gp
from src import gpu_shared_memory as gsm

GIB = 1024 ** 3
UUID1 = "GPU-15d17fee-8c0c-4be3-be46-35fb3e32f2aa"
RUNNER = r"C:\Users\luis\AppData\Local\Programs\Ollama\lib\ollama\llama-server.exe"
BLOB = r"D:\LocalAI\ollama-models\blobs\sha256-dec52a6a7a3fdd3e5e8b4c9f8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c3d2e1f0a9b"
GPUS = [
    {"index": 0, "name": "NVIDIA GeForce RTX 4070 Ti", "uuid": "GPU-5ab72dd9-1a45-c3af-5e12-ac7796b1def7",
     "bus_id": "00000000:01:00.0", "mem_used": 1386},
    {"index": 1, "name": "NVIDIA GeForce RTX 5060 Ti", "uuid": UUID1, "bus_id": "00000000:10:00.0", "mem_used": 13631},
]


# ── the classification rule ──────────────────────────────────────────────────

def test_a_runner_whose_parent_is_gone_or_not_ollama_is_an_orphan():
    assert gp.is_orphan_runner("llama-server.exe", None, False)
    assert gp.is_orphan_runner("llama-server.exe", "explorer.exe", True)      # recycled pid
    assert gp.is_orphan_runner("ollama_llama_server", None, False)
    assert not gp.is_orphan_runner("llama-server.exe", "ollama.exe", True)
    assert not gp.is_orphan_runner("llama-server.exe", "Ollama app.exe", True)
    # only runners: a browser with a dead parent is nobody's business
    assert not gp.is_orphan_runner("brave.exe", None, False)
    assert not gp.is_orphan_runner("", None, False)


# ── the scan, with a fake psutil ─────────────────────────────────────────────

class _Proc:
    def __init__(self, pid, name, parent=None, cmdline=(), alive=True):
        self.pid = pid
        self.info = {"pid": pid, "name": name}
        self._name = name
        self._parent = parent
        self._cmd = list(cmdline)
        self._alive = alive
        self.terminated = False
        self.killed = False

    def name(self):
        return self._name

    def parent(self):
        return self._parent

    def is_running(self):
        return self._alive

    def status(self):
        return "running"

    def create_time(self):
        return 1_756_000_000.0

    def cmdline(self):
        return self._cmd

    def terminate(self):
        self.terminated = True
        self._alive = False

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return 0


def _fake_psutil(monkeypatch, procs):
    mod = types.SimpleNamespace()
    mod.process_iter = lambda attrs=None: list(procs)
    mod.Process = lambda pid: next(p for p in procs if p.pid == pid)
    mod.STATUS_ZOMBIE = "zombie"

    class TimeoutExpired(Exception):
        pass

    mod.TimeoutExpired = TimeoutExpired
    monkeypatch.setitem(sys.modules, "psutil", mod)
    return mod


@pytest.fixture
def box(monkeypatch):
    ollama = _Proc(34012, "ollama.exe")
    owned = _Proc(15948, "llama-server.exe", parent=ollama, cmdline=[RUNNER, "--model", BLOB, "--port", "60644"])
    orphan_a = _Proc(49960, "llama-server.exe", parent=None, cmdline=[RUNNER, "--model", BLOB, "--port", "61001"])
    orphan_b = _Proc(46404, "llama-server.exe", parent=_Proc(7, "explorer.exe"), cmdline=[RUNNER, "--model", BLOB])
    browser = _Proc(3080, "brave.exe", parent=None)
    procs = [ollama, owned, orphan_a, orphan_b, browser]
    _fake_psutil(monkeypatch, procs)
    apps = (
        f"{UUID1}, 00000000:10:00.0, 15948, {RUNNER}, [N/A]\n"
        f"{UUID1}, 00000000:10:00.0, 49960, {RUNNER}, [N/A]\n"
        f"{UUID1}, 00000000:10:00.0, 46404, {RUNNER}, [N/A]\n"
        f"{UUID1}, 00000000:10:00.0, 3080, C:\\brave.exe, [N/A]\n"
    )
    monkeypatch.setattr(gp, "_compute_apps", lambda: gp.parse_compute_apps(apps))
    monkeypatch.setattr(gp, "_wddm", lambda: {
        "processes": [
            {"pid": 49960, "luid": "0x00000000_0x01b3ff4f", "dedicated": 6 * GIB, "shared": 0},
            {"pid": 46404, "luid": "0x00000000_0x01b3ff4f", "dedicated": 7 * GIB, "shared": 0},
            {"pid": 15948, "luid": "0x00000000_0x01b3ff4f", "dedicated": 5 * GIB, "shared": 0},
        ],
        "adapters": {"0x00000000_0x01aec8b1": 1386 * 1024 ** 2, "0x00000000_0x01b3ff4f": 13631 * 1024 ** 2},
    })
    monkeypatch.setattr(gsm, "reset_vram_cache", lambda: None)
    # release_orphan re-scans with the live snapshot (no nvidia-smi here)
    monkeypatch.setattr(gsm, "vram_snapshot", lambda: {"supported": True, "count": 2, "gpus": [
        dict(g, total=16 * GIB, used=int(g["mem_used"]) * 1024 ** 2) for g in GPUS]})
    gp.reset_cache()
    yield {"procs": procs, "owned": owned, "orphan_a": orphan_a, "orphan_b": orphan_b}
    gp.reset_cache()


def test_scan_lists_only_the_orphans_with_their_card_and_bytes(box):
    out = gp.orphan_runners(GPUS)
    assert [o["pid"] for o in out] == [49960, 46404]
    a = out[0]
    assert a["name"] == "llama-server.exe" and a["gpus"] == [1] and a["bytes"] == 6 * GIB
    assert a["blob"] == gp.blob_key(BLOB) and a["started"] == 1_756_000_000.0
    assert out[1]["bytes"] == 7 * GIB
    # the runner Ollama still owns and the browser are not in the list
    assert all(o["pid"] not in (15948, 3080) for o in out)


def test_scan_runs_with_nothing_loaded_and_is_cached_briefly(box, monkeypatch):
    calls = []
    real = gp._orphans_uncached
    monkeypatch.setattr(gp, "_orphans_uncached", lambda gpus: calls.append(1) or real(gpus))
    gp.orphan_runners(GPUS)
    gp.orphan_runners(GPUS)
    assert len(calls) == 1
    gp.reset_cache()
    gp.orphan_runners(GPUS)
    assert len(calls) == 2


def test_scan_never_raises(box, monkeypatch):
    monkeypatch.setattr(gp, "_orphan_processes", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    gp.reset_cache()
    assert gp.orphan_runners(GPUS) == []


def test_release_kills_an_orphan_and_refuses_anything_else(box):
    # the runner Ollama owns: refused, untouched
    r = gp.release_orphan(15948)
    assert r["ok"] is False and not box["owned"].terminated
    # a browser pid: refused
    assert gp.release_orphan(3080)["ok"] is False
    # an unknown pid: refused
    assert gp.release_orphan(424242)["ok"] is False
    # the orphan: terminated, and the answer says what it held
    r = gp.release_orphan(49960)
    assert r["ok"] is True and r["killed"] is True and box["orphan_a"].terminated
    assert r["bytes"] == 6 * GIB and r["gpus"] == [1]


# ── the route and the app_api blocklist ──────────────────────────────────────

def test_usage_carries_the_orphans(monkeypatch):
    import routes.system_usage_routes as sur

    async def _ollama(client):
        return {"reachable": True, "base": "http://127.0.0.1:11434", "models": []}

    monkeypatch.setattr(sur, "_collect_ollama", _ollama)
    monkeypatch.setattr(sur, "_collect_gpu", lambda: ([dict(g, util=1.0, mem_total=16311.0, temp=40.0, power=8.0,
                                                             power_limit=180.0, mem_free=2680.0) for g in GPUS], None))
    monkeypatch.setattr(sur, "_collect_host", lambda: {"cpu": {}, "ram": {}})
    monkeypatch.setattr(sur.gpu_shared_memory, "collect", lambda: {"supported": False, "reason": "test"})
    monkeypatch.setattr(sur, "_collect_policy", lambda: {"exposed": False})
    seen = []
    monkeypatch.setattr(gp, "orphan_runners", lambda gpus: seen.append([g["index"] for g in gpus]) or
                        [{"pid": 49960, "name": "llama-server.exe", "started": 1.0, "gpus": [1], "bytes": 6 * GIB, "blob": "x"}])
    sur._cache["ts"] = 0.0
    sur._cache["data"] = None
    try:
        data = asyncio.run(sur.collect_usage())
    finally:
        sur._cache["ts"] = 0.0
        sur._cache["data"] = None
    assert data["orphans"][0]["pid"] == 49960 and seen == [[0, 1]]


def test_release_route_is_admin_only_and_answers_409_when_not_an_orphan(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import routes.system_usage_routes as sur

    monkeypatch.setattr(sur, "require_admin", lambda request: "admin")
    monkeypatch.setattr(gp, "release_orphan",
                        lambda pid: {"ok": pid == 49960, "pid": pid, "killed": pid == 49960,
                                     "reason": "" if pid == 49960 else "not an orphaned runner right now"})
    app = FastAPI()
    app.include_router(sur.setup_system_usage_routes())
    client = TestClient(app)
    assert client.post("/api/system/gpu/orphans/release", json={}).status_code == 422
    r = client.post("/api/system/gpu/orphans/release", json={"pid": 15948})
    assert r.status_code == 409 and "orphan" in r.json()["detail"]
    r = client.post("/api/system/gpu/orphans/release", json={"pid": 49960})
    assert r.status_code == 200 and r.json()["killed"] is True

    def _deny(request):
        from fastapi import HTTPException
        raise HTTPException(403, "admin only")

    monkeypatch.setattr(sur, "require_admin", _deny)
    assert client.post("/api/system/gpu/orphans/release", json={"pid": 49960}).status_code == 403


@pytest.mark.asyncio
async def test_app_api_cannot_kill_runners(monkeypatch):
    import httpx
    from src.tool_implementations import do_app_api

    class Unexpected:
        def __init__(self, *a, **k):
            raise AssertionError("the kill must be refused before any loopback call")

    monkeypatch.setattr(httpx, "AsyncClient", Unexpected)
    result = await do_app_api(json.dumps({"action": "call", "method": "POST",
                                          "path": "/api/system/gpu/orphans/release", "body": {"pid": 49960}}),
                              owner="admin")
    assert result["exit_code"] == 1 and "blocked" in result["error"].lower()
