"""HW-03 - residencia y concurrencia inteligentes (src/vram_admission.py).

Two protections layered on top of assess()'s existing eviction `suggestion`
(HW-01's biggest-first list), both advisory and both keyed the same way
reservations are ("root|model"):

  * pins (`pin_model`/`unpin_model`) keep a resident out of `suggestion`
    unless every unpinned resident combined still cannot cover the
    shortfall;
  * a resident observed via `assess()` within `RESIDENCY_GRACE_SECONDS` is
    treated the same way, so a quick chat on model B does not suggest
    evicting model A the instant A finished answering a turn.

Driven straight at `src/vram_admission.py` (same style as the existing
tests/test_vram_admission.py — a fake /api/tags + /api/ps + card via
monkeypatch, no network, no real load) plus one TestClient pass over the two
new routes/local_models_routes.py endpoints this lot adds (GET .../residency,
PUT .../{name}/pin), per COMUN rule 7.
"""
from __future__ import annotations

import time

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.testclient import TestClient

import routes.local_models_routes as lm
from src import vram_admission as va

GIB = 2**30
ROOT = "http://127.0.0.1:11434"

BIG = {"name": "qwen3.8:27b-q8_0", "size": 29 * GIB, "digest": "d-big"}
MED = {"name": "mixtral:8x7b", "size": 12 * GIB, "digest": "d-med"}
SMALL = {"name": "qwen3.5:9b", "size": 6 * GIB, "digest": "d-small"}
ASK = {"name": "qwen3.9:32b", "size": 20 * GIB, "digest": "d-ask"}
# A resident this big needs BOTH BIG and MED freed alongside it -- BIG's 29
# GB alone is not enough -- so the "only offered as a last resort" tests
# below actually exercise that fallback instead of stopping at BIG.
ASK_HUGE = {"name": "qwen3.9:huge", "size": 32 * GIB, "digest": "d-huge"}


@pytest.fixture(autouse=True)
def clean_state():
    va._PINS.clear()
    va._LAST_ACTIVE.clear()
    yield
    va._PINS.clear()
    va._LAST_ACTIVE.clear()


def _card(total_gb: float, used_gb: float):
    return {"supported": True, "name": "GeForce", "total": int(total_gb * GIB),
            "used": int(used_gb * GIB), "free": int((total_gb - used_gb) * GIB),
            "gpus": [{"index": 0, "name": "GeForce", "uuid": "u0", "total": int(total_gb * GIB),
                      "used": int(used_gb * GIB), "free": int((total_gb - used_gb) * GIB)}]}


def _ollama(monkeypatch, *, tags, ps, vram):
    def _get(root, path, timeout):
        return {"models": tags} if path == "/api/tags" else {"models": ps}
    monkeypatch.setattr(va, "_get", _get)
    monkeypatch.setattr("src.gpu_shared_memory.vram_snapshot", lambda: vram)
    monkeypatch.setattr("src.gpu_placement.placement", lambda root, loaded, gpus: {})


def _ps_two_residents():
    return [
        {"name": BIG["name"], "digest": "d-big", "size": 29 * GIB, "size_vram": 29 * GIB,
         "context_length": 8192},
        {"name": MED["name"], "digest": "d-med", "size": 12 * GIB, "size_vram": 12 * GIB,
         "context_length": 8192},
    ]


# ── pins ─────────────────────────────────────────────────────────────────


def test_pinned_resident_is_skipped_when_the_other_one_is_enough(monkeypatch):
    """Only BIG's bytes are needed to fit ASK; MED is pinned and must not be
    named even though it is a valid (smaller) candidate."""
    _ollama(monkeypatch, tags=[BIG, MED, ASK], ps=_ps_two_residents(), vram=_card(45, 41))
    va.pin_model(ROOT, MED["name"])
    a = va.assess(ROOT, ASK["name"])
    assert a["fits"] is False
    assert MED["name"] not in a["suggestion"]
    assert a["suggestion_protected_used"] == []


def test_a_pin_is_overridden_only_as_a_last_resort(monkeypatch):
    """ASK_HUGE needs BOTH residents freed: BIG's 29 GB alone falls short.
    The pinned MED must still be offered (with protected_used naming it)
    rather than silently reporting "no way to fit" for a load that *can*
    fit once the pin is set aside."""
    _ollama(monkeypatch, tags=[BIG, MED, ASK_HUGE], ps=_ps_two_residents(), vram=_card(45, 41))
    va.pin_model(ROOT, MED["name"])
    a = va.assess(ROOT, ASK_HUGE["name"])
    assert a["fits"] is False
    assert a["suggestion_enough"] is True
    assert MED["name"] in a["suggestion"]
    assert a["suggestion_protected_used"] == [MED["name"]]


def test_unpin_restores_the_resident_as_an_ordinary_candidate(monkeypatch):
    _ollama(monkeypatch, tags=[BIG, MED, ASK], ps=_ps_two_residents(), vram=_card(45, 41))
    va.pin_model(ROOT, MED["name"])
    assert va.is_pinned(ROOT, MED["name"]) is True
    va.unpin_model(ROOT, MED["name"])
    assert va.is_pinned(ROOT, MED["name"]) is False
    assert va.pinned_models(ROOT) == []


# ── residency grace period (no flapping on an indecisive router) ──────────


def test_a_model_that_just_answered_a_turn_is_not_the_first_suggestion(monkeypatch):
    """MED answers a turn (assess() called for MED while MED is resident:
    the "already_resident" path), then, a moment later, loading ASK needs to
    evict someone. BIG alone is not enough, so both would ordinarily be
    picked biggest-first (BIG then MED) â€” with MED "in grace", it must be
    named only as the extra, last-resort pick, after BIG."""
    ps = _ps_two_residents()
    _ollama(monkeypatch, tags=[BIG, MED, ASK_HUGE], ps=ps, vram=_card(45, 41))
    # MED "answers a turn": assess() called for MED while it is resident.
    resident_answer = va.assess(ROOT, MED["name"])
    assert resident_answer["already_resident"] is True

    a = va.assess(ROOT, ASK_HUGE["name"])
    assert a["fits"] is False
    assert a["suggestion"][0] == BIG["name"]           # BIG first: not in grace
    assert MED["name"] in a["suggestion"]              # still offered: BIG alone is not enough
    assert MED["name"] in a["suggestion_protected_used"]


def test_grace_period_expires(monkeypatch):
    _ollama(monkeypatch, tags=[BIG, MED, ASK], ps=_ps_two_residents(), vram=_card(30, 29))
    va.assess(ROOT, MED["name"])
    # Pretend the grace window has already elapsed.
    key = va._active_key(ROOT, MED["name"])
    va._LAST_ACTIVE[key] = time.time() - va.RESIDENCY_GRACE_SECONDS - 1
    a = va.assess(ROOT, ASK["name"])
    assert a["suggestion_protected_used"] == []        # MED no longer protected


def test_residency_status_reports_pin_and_grace(monkeypatch):
    _ollama(monkeypatch, tags=[BIG, MED], ps=_ps_two_residents(), vram=_card(45, 41))
    va.pin_model(ROOT, BIG["name"])
    va.assess(ROOT, MED["name"])  # marks MED active
    a = va.assess(ROOT, "some-other-model:latest")
    status = va.residency_status(ROOT, a["residents"])
    by_name = {r["name"]: r for r in status}
    assert by_name[BIG["name"]]["pinned"] is True
    assert by_name[MED["name"]]["in_grace"] is True
    assert by_name[BIG["name"]]["in_grace"] is False


# ── revert-proof: without the protections, the pin/grace tests above fail ──
# (see report for the temporary-revert check; kept lightweight here so this
# file only asserts the current, protected behaviour.)


# ── HTTP surface (routes/local_models_routes.py) ───────────────────────────


def _client(monkeypatch):
    """Same fixture shape as tests/test_local_models_routes.py's `env`."""
    monkeypatch.setattr(
        lm, "list_ollama_endpoints",
        lambda include_default=True, **kw: [
            {"id": "local-ollama", "name": "Ollama", "base_url": ROOT + "/v1",
             "root": ROOT, "same_machine": True},
        ],
    )

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": _ps_two_residents()})
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [BIG, MED]})
        return httpx.Response(404)

    monkeypatch.setattr(lm, "_client_factory",
                        lambda timeout=10.0: httpx.Client(transport=httpx.MockTransport(_handler), timeout=timeout))

    app = FastAPI()
    app.include_router(lm.setup_local_models_routes())
    app.state.auth_manager = SimpleNamespace(is_configured=True, is_admin=lambda u: u == "root")

    class _Stamp(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            user = request.headers.get("x-user")
            if user:
                request.state.current_user = user
            return await call_next(request)

    app.add_middleware(_Stamp)
    return TestClient(app, raise_server_exceptions=False)


ADMIN = {"x-user": "root"}


def test_residency_and_pin_routes(monkeypatch):
    client = _client(monkeypatch)
    r = client.get("/api/local-models/residency", headers=ADMIN)
    assert r.status_code == 200
    body = r.json()
    names = {row["name"] for row in body["residents"]}
    assert names == {BIG["name"], MED["name"]}

    r = client.put(f"/api/local-models/{BIG['name']}/pin", json={"pinned": True}, headers=ADMIN)
    assert r.status_code == 200
    assert r.json()["pinned"] is True
    assert va.is_pinned(ROOT, BIG["name"]) is True

    r = client.put(f"/api/local-models/{BIG['name']}/pin", json={"pinned": False}, headers=ADMIN)
    assert r.status_code == 200
    assert r.json()["pinned"] is False
