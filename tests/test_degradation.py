"""Unified degradation diagnostics (src/degradation.py), its route and MCP tool."""
import asyncio
import json
import time

import pytest

from src import degradation as dg


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(dg, "STATE_FILE", str(tmp_path / "degradation.json"))
    saved = dict(dg._REGISTRY)
    dg._REGISTRY.clear()
    dg.reset_for_tests()
    yield
    dg._REGISTRY.clear()
    dg._REGISTRY.update(saved)
    dg.reset_for_tests()


def _add(cid, fn, timeout=0.5, needs_app=False):
    dg.register(dg.Check(cid, cid.title(), fn, affects=[f"{cid} feature"],
                         hint={"es": f"arregla {cid}", "en": f"fix {cid}", "command": "do it"},
                         timeout=timeout, needs_app=needs_app))


def _states(report):
    return {i["id"]: i for i in report["items"]}


def test_each_state_reported_with_cause_and_hint():
    def boom(ctx):
        raise ConnectionRefusedError("connect to http://u:pw@host:1/x?api_key=abc failed")

    _add("a_ok", lambda ctx: dg._result(dg.OK))
    _add("b_deg", lambda ctx: dg._result(dg.DEGRADED, "partial"))
    _add("c_down", boom)
    _add("d_none", lambda ctx: dg._result(dg.UNCONFIGURED, "not set"))
    rep = dg.refresh({"inputs": {}})
    s = _states(rep)
    assert [s[k]["state"] for k in ("a_ok", "b_deg", "c_down", "d_none")] == ["ok", "degraded", "down", "unconfigured"]
    assert s["c_down"]["cause"].startswith("ConnectionRefusedError:")
    assert "pw" not in s["c_down"]["cause"] and "abc" not in s["c_down"]["cause"]
    assert s["b_deg"]["fix_hint"] == {"es": "arregla b_deg", "en": "fix b_deg", "command": "do it"}
    assert s["a_ok"]["fix_hint"]["en"] == "" and s["a_ok"]["cause"] == ""
    assert s["b_deg"]["affects"] == ["b_deg feature"]
    assert rep["overall"] == "down"
    assert [p["id"] for p in rep["problems"]] == ["b_deg", "c_down"]
    assert rep["counts"]["unconfigured"] == 1
    for item in rep["items"]:
        assert isinstance(item["latency_ms"], int) and item["last_checked"]


def test_timeout_is_degraded_and_does_not_block():
    _add("slow", lambda ctx: time.sleep(3) or dg._result(dg.OK), timeout=0.3)
    t0 = time.monotonic()
    rep = dg.refresh({"inputs": {}})
    assert time.monotonic() - t0 < 2
    item = _states(rep)["slow"]
    assert item["state"] == "degraded" and item["cause"].startswith("TimeoutError")


def test_invalid_result_state_becomes_down():
    _add("weird", lambda ctx: {"state": "banana"})
    assert _states(dg.refresh({"inputs": {}}))["weird"]["state"] == "down"


def test_since_tracks_first_time_in_state_and_survives_restart():
    box = {"state": dg.DOWN}
    _add("x", lambda ctx: dg._result(box["state"], "no"))
    first = _states(dg.refresh({"inputs": {}}))["x"]["since"]
    time.sleep(0.01)
    assert _states(dg.refresh({"inputs": {}}))["x"]["since"] == first
    box["state"] = dg.OK
    changed = _states(dg.refresh({"inputs": {}}))["x"]["since"]
    assert changed > first
    box["state"] = dg.OK
    # "restart": memory is wiped, the persisted file keeps the since
    dg.reset_for_tests()
    assert _states(dg.refresh({"inputs": {}}))["x"]["since"] == changed


def test_cache_serves_fresh_and_refreshes_stale_in_background():
    calls = []

    def fn(ctx):
        calls.append(1)
        return dg._result(dg.OK)

    _add("c", fn)
    first = dg.get_report({"inputs": {}})
    assert first["cached"] is False and len(calls) == 1
    second = dg.get_report({"inputs": {}})
    assert second["cached"] is True and len(calls) == 1
    dg._cache["at"] -= dg.CACHE_TTL_S + 1
    stale = dg.get_report({"inputs": {}})
    assert stale["cached"] is True  # answered from cache, never waits
    for _ in range(100):
        if len(calls) == 2 and not dg._refreshing.is_set():
            break
        time.sleep(0.02)
    assert len(calls) == 2
    forced = dg.get_report({"inputs": {}}, force=True)
    assert forced["cached"] is False and len(calls) == 3


def test_scrub_removes_secrets_and_truncates():
    s = dg.scrub("GET https://user:hunter2@h.example/p?token=zzz Authorization: Bearer abcdefghijk key sk-ABCDEFGHIJKLMNOPQRS " + "x" * 400)
    assert "hunter2" not in s and "zzz" not in s and "abcdefghijk" not in s and "ABCDEFGHIJKLMNOPQRS" not in s
    assert len(s) <= 180


def test_from_service_mapping():
    assert dg._from_service({"status": "ok"})["state"] == "ok"
    assert dg._from_service({"status": "disabled", "detail": "off"})["state"] == "unconfigured"
    r = dg._from_service({"status": "down", "detail": "connection refused", "meta": {"error": "connection_refused"}})
    assert r["state"] == "down" and r["cause"] == "ConnectionRefusedError: connection refused"
    assert dg._from_service({"status": "degraded", "detail": "d"})["state"] == "degraded"


def test_builtin_checks_use_fakes(monkeypatch):
    import src.service_health as sh

    monkeypatch.setattr(sh, "searxng_health", lambda settings: {"name": "searxng", "status": "down", "detail": "connection refused", "meta": {"error": "connection_refused"}})
    monkeypatch.setattr(sh, "ntfy_health", lambda i, s: {"name": "ntfy", "status": "disabled", "detail": "none", "meta": {}})
    monkeypatch.setattr(sh, "chromadb_health", lambda r, m: {"name": "chromadb", "status": "degraded", "detail": "one store lost", "meta": {"error": "partial"}})
    ctx = {"inputs": {"settings": {}, "integrations": [], "accounts": [], "endpoints": []}}
    assert dg._searxng(ctx)["state"] == "down"
    assert dg._ntfy(ctx)["state"] == "unconfigured"
    assert dg._chromadb(ctx)["state"] == "degraded"


def test_mcp_check_with_fake_manager():
    class M:
        def __init__(self, st):
            self.st = st

        def get_all_statuses(self):
            return self.st

    assert dg._mcp({})["state"] == "unconfigured"
    assert dg._mcp({"mcp_manager": M({})})["state"] == "unconfigured"
    assert dg._mcp({"mcp_manager": M({"a": {"status": "connected"}})})["state"] == "ok"
    mixed = dg._mcp({"mcp_manager": M({"a": {"status": "connected"}, "b": {"status": "error", "error": "exited\nmore"}})})
    assert mixed["state"] == "degraded" and "b: exited" in mixed["cause"]
    alldown = dg._mcp({"mcp_manager": M({"b": {"status": "error", "error": "x"}})})
    assert alldown["state"] == "down"


def test_sparks_check_with_fakes(monkeypatch):
    from src import sparks

    monkeypatch.setattr(sparks, "config", lambda: {"enabled": False})
    assert dg._sparks({})["state"] == "unconfigured"
    monkeypatch.setattr(sparks, "config", lambda: {"enabled": True})
    monkeypatch.setattr(sparks, "status", lambda with_recipes=True: {"ok": True})
    assert dg._sparks({})["state"] == "ok"
    monkeypatch.setattr(sparks, "status", lambda with_recipes=True: {"ok": False, "error": "unreachable"})
    r = dg._sparks({})
    assert r["state"] == "down" and "unreachable" in r["cause"]


def test_snapshot_or_check_prefers_recent_app_snapshot():
    _add("only", lambda ctx: dg._result(dg.DEGRADED, "bad"))
    dg.refresh({"inputs": {}})
    snap = dg.snapshot_or_check()
    assert snap["source"] == "app_snapshot" and snap["problems"][0]["id"] == "only"
    # app-bound checks are skipped in the standalone path
    _add("needs", lambda ctx: dg._result(dg.OK), needs_app=True)
    fresh = dg.snapshot_or_check(force=True)
    assert fresh["source"] == "standalone_probe"
    assert "needs" not in _states(fresh)


def test_snapshot_ignores_stale_file(tmp_path):
    _add("only", lambda ctx: dg._result(dg.OK))
    rep = dg.refresh({"inputs": {}})
    rep["generated_at"] = "2020-01-01T00:00:00+00:00"
    json.dump(rep, open(dg._path(), "w"))
    assert dg.snapshot_or_check()["source"] == "standalone_probe"


def test_route_admin_only_and_report(monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("starlette.testclient")
    from fastapi import FastAPI, HTTPException
    from starlette.testclient import TestClient
    diag = pytest.importorskip("routes.diagnostics_routes")

    _add("r", lambda ctx: dg._result(dg.DOWN, "RuntimeError: no"))

    def build(gate):
        monkeypatch.setattr(diag, "require_admin", gate)
        app = FastAPI()
        app.include_router(diag.setup_diagnostics_routes(rag_manager=None, rag_available=False,
                                                         research_handler=None, memory_vector=None))
        return TestClient(app, raise_server_exceptions=False)

    def deny(_r):
        raise HTTPException(403, "Admin only")

    assert build(deny).get("/api/diagnostics/degradation").status_code == 403
    client = build(lambda _r: None)
    # keep the route off the network: inputs are injected through the loader
    monkeypatch.setattr(dg, "_gather_inputs", lambda: {"settings": {}, "integrations": [], "accounts": [], "endpoints": []})
    body = client.get("/api/diagnostics/degradation?refresh=1").json()
    assert body["overall"] == "down" and body["problems"][0]["cause"] == "RuntimeError: no"
    again = client.get("/api/diagnostics/degradation").json()
    assert again["cached"] is True


def test_mcp_tool_registered_and_callable():
    pytest.importorskip("mcp")
    import mcp_servers.harness_server as srv

    names = [t.name for t in asyncio.run(srv.list_tools())]
    assert "degradation_status" in names
    _add("only", lambda ctx: dg._result(dg.DEGRADED, "bad"))
    dg.refresh({"inputs": {}})
    out = json.loads(asyncio.run(srv.call_tool("degradation_status", {"only_problems": True}))[0].text)
    assert out["items"] == out["problems"] and out["items"][0]["id"] == "only"
