"""routes/benchmark_routes.py — INF-04 A5.

Simple endpoints (no background task involved) go through a real
`TestClient`, following `tests/test_capability_system_routes.py`'s own
`_client()` pattern. `POST /runs/{id}/start` spawns a background
`asyncio.Task` that outlives the request, which a synchronous `TestClient`
cannot reliably observe finishing — so that one endpoint is called directly
(the `tests/test_inf02_serve_routes.py` pattern: fetch the route function
off the router and `await` it), staying in the SAME event loop as the rest
of the test so the spawned task can be awaited directly too.

No real model, ever: `src.llm_core.stream_llm` is a fake async generator,
`src.vram_admission.admit` a fake that only records `proceed`.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import routes.benchmark_routes as benchmark_routes
from src import launch_receipts, vram_admission
from src.bench import suites
from src.contracts.inference import BenchmarkCase


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    import src.constants as constants
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))


@pytest.fixture(autouse=True)
def _admin_and_owner(monkeypatch):
    monkeypatch.setattr(benchmark_routes, "require_admin", lambda request: None)
    monkeypatch.setattr(benchmark_routes, "get_current_user", lambda request: "alice")


@pytest.fixture(autouse=True)
def _no_real_receipts(monkeypatch):
    # profiles.current_profile() consults launch_receipts for a matching
    # receipt; keep that harmless so a route test never needs a real one.
    monkeypatch.setattr(launch_receipts, "identity_for_endpoint", lambda url, implementation_hint=None: None)
    monkeypatch.setattr(launch_receipts, "find_by_endpoint", lambda host, port: None)


@pytest.fixture
def fake_suite(monkeypatch):
    cases = tuple(
        BenchmarkCase.parse({"id": f"c{i}", "suite": "fake_suite", "prompt": "hi", "checks": []})
        for i in range(3)
    )
    suite = {"id": "fake_suite", "version": "1.0.0", "objective": "interactive", "cases": cases}
    monkeypatch.setattr(suites, "load_suite", lambda suite_id: suite if suite_id == "fake_suite" else (_ for _ in ()).throw(suites.SuiteNotFound(suite_id)))
    monkeypatch.setattr(suites, "list_suites", lambda: [suite])
    return suite


@pytest.fixture
def fake_model(monkeypatch):
    async def _fake_admit(endpoint_url, model, *, owner="", on_progress=None,
                          mode=None, timeout=None, waited_out=None):
        if waited_out is not None:
            waited_out["waited_s"] = 0.0
        return "proceed"

    monkeypatch.setattr(vram_admission, "admit", _fake_admit)

    async def _fake_stream(url, model, messages, **kwargs):
        yield "data: " + json.dumps({"delta": "hola"}) + "\n\n"
        usage = {
            "input_tokens": 5, "output_tokens": 5,
            "engine_timings": {"load_ms": 1, "prompt_ms": 2, "predicted_ms": 3,
                              "prompt_n": 5, "predicted_n": 5, "source": "ollama"},
        }
        yield "data: " + json.dumps({"type": "usage", "data": usage}) + "\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr("src.llm_core.stream_llm", _fake_stream)


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(benchmark_routes.setup_benchmark_routes())
    return TestClient(app)


def _plan_body(**overrides):
    body = {
        "endpoint_url": "http://127.0.0.1:11434", "model": "test-model",
        "suite_id": "fake_suite", "budget": {"repeats": 1}, "objective": "interactive",
    }
    body.update(overrides)
    return body


def _endpoint(router, path: str, method: str = "POST"):
    for route in router.routes:
        if route.path == path and method in route.methods:
            return route.endpoint
    raise AssertionError(f"{method} {path} route not found")


def _request(path: str, method: str = "POST") -> Request:
    return Request({"type": "http", "method": method, "path": path, "headers": [], "state": {}})


# ── simple endpoints, via TestClient ─────────────────────────────────────────

def test_list_suites_route(client, fake_suite):
    resp = client.get("/api/bench/suites")
    assert resp.status_code == 200
    body = resp.json()["suites"]
    assert body[0]["id"] == "fake_suite"
    assert len(body[0]["cases"]) == 3


def test_profiles_route_without_query_has_no_current(client):
    resp = client.get("/api/bench/profiles")
    assert resp.status_code == 200
    assert resp.json() == {"current": None, "saved": []}


def test_profiles_route_with_query_builds_current(client):
    resp = client.get("/api/bench/profiles", params={"endpoint": "http://127.0.0.1:11434", "model": "test-model"})
    assert resp.status_code == 200
    current = resp.json()["current"]
    assert current["model"]["artifact_id"] == "test-model"
    assert current["source"] == "current"


def test_plan_route_opening_the_screen_does_not_execute_anything(client, fake_suite):
    resp = client.post("/api/bench/plan", json=_plan_body())
    assert resp.status_code == 200
    run = resp.json()["run"]
    assert run["state"] == "planned"
    assert run["samples"] == []
    assert run["summary"]["cases_planned"] == 3


def test_plan_route_requires_a_budget(client, fake_suite):
    resp = client.post("/api/bench/plan", json=_plan_body(budget={}))
    assert resp.status_code == 400
    assert resp.json()["error_class"] == "bench.budget_required"


def test_plan_route_rejects_bad_objective(client, fake_suite):
    resp = client.post("/api/bench/plan", json=_plan_body(objective="fast"))
    assert resp.status_code == 400
    assert resp.json()["error_class"] == "bench.invalid_plan"


def test_plan_route_unknown_suite_is_not_found(client, fake_suite):
    resp = client.post("/api/bench/plan", json=_plan_body(suite_id="does-not-exist"))
    assert resp.status_code == 404
    assert resp.json()["error_class"] == "bench.not_found"


def test_plan_route_options_override_changes_fingerprint(client, fake_suite):
    base = client.post("/api/bench/plan", json=_plan_body()).json()["run"]
    overridden = client.post(
        "/api/bench/plan", json=_plan_body(options={"num_ctx": 32768}),
    ).json()["run"]
    assert base["profile"]["fingerprint"] != overridden["profile"]["fingerprint"]
    assert overridden["profile"]["options"]["num_ctx"] == 32768


def test_start_route_unknown_run_is_not_found(client):
    resp = client.post("/api/bench/runs/does-not-exist/start")
    assert resp.status_code == 404
    assert resp.json()["error_class"] == "bench.not_found"


def test_cancel_route_unknown_run_is_not_found(client):
    resp = client.post("/api/bench/runs/does-not-exist/cancel")
    assert resp.status_code == 404


def test_cancel_route_settles_a_planned_run(client, fake_suite):
    planned = client.post("/api/bench/plan", json=_plan_body()).json()["run"]
    resp = client.post(f"/api/bench/runs/{planned['id']}/cancel")
    assert resp.status_code == 200
    assert resp.json()["run"]["state"] == "cancelled"


def test_get_run_route(client, fake_suite):
    planned = client.post("/api/bench/plan", json=_plan_body()).json()["run"]
    resp = client.get(f"/api/bench/runs/{planned['id']}")
    assert resp.status_code == 200
    assert resp.json()["run"]["id"] == planned["id"]


def test_get_run_route_unknown_is_not_found(client):
    resp = client.get("/api/bench/runs/does-not-exist")
    assert resp.status_code == 404


def test_list_runs_route(client, fake_suite):
    client.post("/api/bench/plan", json=_plan_body())
    client.post("/api/bench/plan", json=_plan_body())
    resp = client.get("/api/bench/runs")
    assert resp.status_code == 200
    assert len(resp.json()["runs"]) == 2


def test_compare_route_unknown_run_is_not_found(client):
    resp = client.get("/api/bench/compare", params={"baseline": "a", "candidate": "b"})
    assert resp.status_code == 404
    assert resp.json()["error_class"] == "bench.not_found"


def test_promote_route_requires_both_ids(client):
    resp = client.post("/api/bench/profiles/p1/promote", json={})
    assert resp.status_code == 400
    assert resp.json()["error_class"] == "bench.not_comparable"


# ── start(): stays in one event loop so the background task can be awaited ──

@pytest.mark.asyncio
async def test_start_route_runs_in_the_background_and_the_run_completes(fake_suite, fake_model):
    router = benchmark_routes.setup_benchmark_routes()
    plan_ep = _endpoint(router, "/api/bench/plan")
    start_ep = _endpoint(router, "/api/bench/runs/{run_id}/start")
    get_ep = _endpoint(router, "/api/bench/runs/{run_id}", method="GET")

    planned = await plan_ep(_request("/api/bench/plan"), benchmark_routes.PlanRequest(**_plan_body()))
    run_id = planned["run"]["id"]

    started = await start_ep(_request(f"/api/bench/runs/{run_id}/start"), run_id)
    assert started.status_code == 202
    assert json.loads(started.body)["run"]["state"] == "planned"

    tasks = list(benchmark_routes._background_tasks)
    assert tasks, "start() must schedule the run as a background task"
    await asyncio.gather(*tasks)

    finished = await get_ep(_request(f"/api/bench/runs/{run_id}", method="GET"), run_id)
    assert finished["run"]["state"] == "completed"
    assert len(finished["run"]["samples"]) == 3


@pytest.mark.asyncio
async def test_start_route_refuses_a_run_not_in_planned_state(fake_suite, fake_model):
    router = benchmark_routes.setup_benchmark_routes()
    plan_ep = _endpoint(router, "/api/bench/plan")
    start_ep = _endpoint(router, "/api/bench/runs/{run_id}/start")

    planned = await plan_ep(_request("/api/bench/plan"), benchmark_routes.PlanRequest(**_plan_body()))
    run_id = planned["run"]["id"]

    first = await start_ep(_request(f"/api/bench/runs/{run_id}/start"), run_id)
    assert first.status_code == 202
    await asyncio.gather(*list(benchmark_routes._background_tasks))

    second = await start_ep(_request(f"/api/bench/runs/{run_id}/start"), run_id)
    assert second.status_code == 409
    assert json.loads(second.body)["error_class"] == "bench.bad_state"


@pytest.mark.asyncio
async def test_promote_route_via_two_completed_runs(fake_suite, fake_model):
    router = benchmark_routes.setup_benchmark_routes()
    plan_ep = _endpoint(router, "/api/bench/plan")
    start_ep = _endpoint(router, "/api/bench/runs/{run_id}/start")
    promote_ep = _endpoint(router, "/api/bench/profiles/{profile_id}/promote")

    async def _run_to_completion():
        planned = await plan_ep(
            _request("/api/bench/plan"), benchmark_routes.PlanRequest(**_plan_body(budget={"repeats": 3})),
        )
        run_id = planned["run"]["id"]
        await start_ep(_request(f"/api/bench/runs/{run_id}/start"), run_id)
        await asyncio.gather(*list(benchmark_routes._background_tasks))
        return run_id

    baseline_id = await _run_to_completion()
    candidate_id = await _run_to_completion()

    # The profile id itself was never saved -- promote() must still get far
    # enough to compute a fresh, comparable comparison before refusing on
    # "no such profile", proving compare() ran rather than short-circuiting.
    resp = await promote_ep(
        _request("/api/bench/profiles/does-not-exist/promote"), "does-not-exist",
        benchmark_routes.PromoteRequest(baseline_run_id=baseline_id, candidate_run_id=candidate_id),
    )
    assert resp.status_code == 404
    assert json.loads(resp.body)["error_class"] == "bench.not_found"


@pytest.mark.asyncio
async def test_plan_saves_its_profile_so_promote_can_find_it(fake_suite, fake_model):
    """The Studio promotes with the candidate run's OWN profile id: a
    profile a run was planned against has to be addressable later, or
    'Mark as recommended' can only ever answer bench.not_found."""
    from src.bench import profiles as bench_profiles

    router = benchmark_routes.setup_benchmark_routes()
    plan_ep = _endpoint(router, "/api/bench/plan")
    start_ep = _endpoint(router, "/api/bench/runs/{run_id}/start")
    promote_ep = _endpoint(router, "/api/bench/profiles/{profile_id}/promote")

    async def _run_to_completion():
        planned = await plan_ep(
            _request("/api/bench/plan"), benchmark_routes.PlanRequest(**_plan_body(budget={"repeats": 3})),
        )
        run_id = planned["run"]["id"]
        await start_ep(_request(f"/api/bench/runs/{run_id}/start"), run_id)
        await asyncio.gather(*list(benchmark_routes._background_tasks))
        return planned["run"]["profile"]["id"], run_id

    profile_id, baseline_id = await _run_to_completion()
    _, candidate_id = await _run_to_completion()
    assert bench_profiles.get_profile(profile_id) is not None

    resp = await promote_ep(
        _request(f"/api/bench/profiles/{profile_id}/promote"), profile_id,
        benchmark_routes.PromoteRequest(baseline_run_id=baseline_id, candidate_run_id=candidate_id),
    )
    body = resp if isinstance(resp, dict) else json.loads(resp.body)
    # identical runs: not an improvement, so the profile is NOT recommended --
    # but the route found it and answered with the profile, not a 404
    assert "profile" in body, body
    assert body["profile"]["evaluation"] != "recommended"
