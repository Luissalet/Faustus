"""src/model_lease.py — several instances of this app, one local Ollama:
each publishes its own small lease file to a machine-wide shared directory
and reads its siblings' files to agree on one resident model instead of
fighting over it (Lot L). See the module docstring for the full problem.
"""
from __future__ import annotations

import asyncio
import json
import os
import time

import pytest

from src import model_lease as ml
from src import vram_admission as va
from src import model_warmup as mw
from src import run_model_pin as pin

ROOT = "http://127.0.0.1:11434"
GIB = 2**30


@pytest.fixture(autouse=True)
def _lease_env(tmp_path, monkeypatch):
    monkeypatch.setenv("FAUSTUS_SHARED_DIR", str(tmp_path))
    monkeypatch.setenv("APP_PORT", "7001")
    ml.reset_for_tests()
    va._PINS.clear()
    va._LAST_ACTIVE.clear()
    yield
    ml.reset_for_tests()
    va._PINS.clear()
    va._LAST_ACTIVE.clear()


def _write_sibling(tmp_path, instance_id, **fields):
    rec = {
        "instance_id": instance_id, "pid": os.getpid(), "port": 7002,
        "data_dir": "/tmp/sibling", "started": time.time(), "heartbeat": time.time(),
        "default": {}, "pinned": [], "active": {}, "reservations": [],
        "adopted_model": "", "leader": False,
    }
    rec.update(fields)
    d = tmp_path / "leases"
    d.mkdir(parents=True, exist_ok=True)
    with open(d / f"{instance_id}.json", "w", encoding="utf-8") as f:
        json.dump(rec, f)
    return rec


# ── shared dir / identity ────────────────────────────────────────────────────

def test_shared_dir_honors_the_env_override(tmp_path):
    assert ml.shared_dir() == str(tmp_path)


def test_shared_dir_falls_back_to_a_platform_default(monkeypatch):
    monkeypatch.delenv("FAUSTUS_SHARED_DIR", raising=False)
    monkeypatch.delenv("ODYSSEUS_SHARED_DIR", raising=False)
    out = ml.shared_dir()
    assert out and "faustus" in out.lower()


# ── note_* are no-ops before start(); reads still work ──────────────────────

def test_note_star_are_no_ops_before_start():
    ml.note_default(ROOT, "qwen3.8:27b")
    ml.note_pin(ROOT, "qwen3.8:27b")
    ml.note_active(ROOT, "qwen3.8:27b")
    ml.note_reservation(ROOT, "qwen3.8:27b", 1000, 60)
    ml.note_adopted("qwen3.8:27b")
    assert ml.self_record() == {}
    assert ml.siblings() == []
    assert ml.sibling_pins(ROOT) == set()
    assert ml.sibling_defaults(ROOT) == set()
    assert ml.sibling_active(ROOT) == {}
    assert ml.sibling_reserved_bytes(ROOT) == 0
    assert not ml.enabled()


# ── lifecycle: start/stop, heartbeat, identity ───────────────────────────────

def test_start_registers_self_and_stop_removes_the_file(tmp_path):
    ml.start()
    try:
        rec = ml.self_record()
        assert rec["port"] == 7001
        assert rec["pid"] == os.getpid()
        assert (tmp_path / "leases" / f"{rec['instance_id']}.json").exists()
        assert ml.enabled() is True
    finally:
        asyncio.run(ml.stop())
    assert ml.self_record() == {}
    assert not any((tmp_path / "leases").glob("*.json"))


def test_identity_is_derived_from_data_dir_and_port(monkeypatch, tmp_path):
    import hashlib
    from src.constants import DATA_DIR
    ml.start()
    try:
        expected = hashlib.sha1(f"{DATA_DIR}|7001".encode("utf-8")).hexdigest()[:12]
        assert ml.self_record()["instance_id"] == expected
    finally:
        asyncio.run(ml.stop())


# ── siblings(): stale + dead-pid filtering ───────────────────────────────────

def test_siblings_excludes_stale_and_dead_but_keeps_fresh_alive_ones(tmp_path):
    ml.start()
    try:
        fresh = _write_sibling(tmp_path, "sib-fresh", heartbeat=time.time(), pid=os.getpid())
        _write_sibling(tmp_path, "sib-stale", heartbeat=time.time() - 999, pid=os.getpid())
        _write_sibling(tmp_path, "sib-dead", heartbeat=time.time(), pid=999_999_999)
        sibs = {s["instance_id"] for s in ml.siblings()}
        assert sibs == {"sib-fresh"}
    finally:
        asyncio.run(ml.stop())


def test_siblings_never_includes_myself(tmp_path):
    ml.start()
    try:
        me = ml.self_record()["instance_id"]
        _write_sibling(tmp_path, me, heartbeat=time.time())
        _write_sibling(tmp_path, "sib-other", heartbeat=time.time())
        assert {s["instance_id"] for s in ml.siblings()} == {"sib-other"}
    finally:
        asyncio.run(ml.stop())


# ── sibling pins / defaults / active / reservations ──────────────────────────

def test_sibling_pins_and_defaults_are_read_across_instances(tmp_path):
    ml.start()
    try:
        _write_sibling(tmp_path, "sib-a",
                       pinned=[{"root": ROOT, "model": "coder:30b", "since": time.time()}],
                       default={"root": ROOT, "model": "qwen3.8:27b"})
        assert ml.sibling_pins(ROOT) == {"coder:30b"}
        assert ml.sibling_defaults(ROOT) == {"qwen3.8:27b"}
        assert ml.sibling_pins("http://127.0.0.1:9999") == set()
    finally:
        asyncio.run(ml.stop())


def test_sibling_active_reports_the_newest_epoch(tmp_path):
    ml.start()
    try:
        _write_sibling(tmp_path, "sib-a", active={f"{ROOT}|coder:30b": 100.0})
        _write_sibling(tmp_path, "sib-b", active={f"{ROOT}|coder:30b": 200.0})
        assert ml.sibling_active(ROOT) == {"coder:30b": 200.0}
    finally:
        asyncio.run(ml.stop())


def test_sibling_reserved_bytes_counts_only_unexpired_reservations(tmp_path):
    ml.start()
    try:
        now = time.time()
        _write_sibling(tmp_path, "sib-a", reservations=[
            {"root": ROOT, "model": "big:27b", "bytes": 10 * GIB, "since": now, "until": now + 60},
            {"root": ROOT, "model": "gone:9b", "bytes": 4 * GIB, "since": now - 200, "until": now - 100},
        ])
        assert ml.sibling_reserved_bytes(ROOT) == 10 * GIB
    finally:
        asyncio.run(ml.stop())


def test_note_and_clear_reservation_round_trip(tmp_path):
    ml.start()
    try:
        ml.note_reservation(ROOT, "big:27b", 10 * GIB, 60)
        rec = ml.self_record()
        assert rec["reservations"][0]["bytes"] == 10 * GIB
        ml.clear_reservation(ROOT, "big:27b")
        assert ml.self_record()["reservations"] == []
    finally:
        asyncio.run(ml.stop())


# ── vram_admission integration: sibling pins/defaults protect a model ───────

def test_is_pinned_is_true_for_a_sibling_pin_and_a_sibling_default(tmp_path):
    ml.start()
    try:
        _write_sibling(tmp_path, "sib-a",
                       pinned=[{"root": ROOT, "model": "coder:30b", "since": time.time()}],
                       default={"root": ROOT, "model": "qwen3.8:27b"})
        assert va.is_pinned(ROOT, "coder:30b") is True
        assert va.is_pinned(ROOT, "qwen3.8:27b") is True
        assert va.is_pinned(ROOT, "unrelated:9b") is False
    finally:
        asyncio.run(ml.stop())


def test_is_pinned_ignores_siblings_when_the_lease_is_off(tmp_path, monkeypatch):
    ml.start()
    try:
        _write_sibling(tmp_path, "sib-a",
                       pinned=[{"root": ROOT, "model": "coder:30b", "since": time.time()}])
        monkeypatch.setattr("src.settings.get_setting",
                            lambda key, default=None: False if key == "model_lease_enabled" else default)
        assert ml.enabled() is False
        assert va.is_pinned(ROOT, "coder:30b") is False
    finally:
        asyncio.run(ml.stop())


def test_reserved_bytes_include_siblings(tmp_path):
    ml.start()
    try:
        va._RESERVATIONS.clear()
        now = time.time()
        _write_sibling(tmp_path, "sib-a", reservations=[
            {"root": ROOT, "model": "big:27b", "bytes": 6 * GIB, "since": now, "until": now + 60},
        ])
        assert va.reserved_bytes(ROOT) == 6 * GIB
    finally:
        va._RESERVATIONS.clear()
        asyncio.run(ml.stop())


def test_try_reserve_and_release_publish_to_the_lease(tmp_path):
    ml.start()
    try:
        va._RESERVATIONS.clear()
        rid = va.try_reserve(ROOT, "big:27b", 6 * GIB, 100 * GIB)
        assert rid is not None
        assert ml.self_record()["reservations"][0]["model"] == "big:27b"
        va.release_reservation(rid)
        assert ml.self_record()["reservations"] == []
    finally:
        va._RESERVATIONS.clear()
        asyncio.run(ml.stop())


def test_default_yield_plan_refuses_a_siblings_default(tmp_path, monkeypatch):
    """The X-D "default yields on its own" rule must never pick a SIBLING
    instance's own default for our own local default's yield plan — that
    memory is not ours to give away."""
    ml.start()
    try:
        _write_sibling(tmp_path, "sib-a", default={"root": ROOT, "model": "sibling-default:9b"})
        monkeypatch.setattr(va, "is_default_model", lambda root, name: False)
        residents = [
            {"name": "sibling-default:9b", "in_vram_bytes": 9 * GIB},
            {"name": "free:5b", "in_vram_bytes": 5 * GIB},
        ]
        picked, freed, uses_default = va._default_yield_plan(ROOT, residents, 8 * GIB)
        assert "sibling-default:9b" not in picked
        assert picked == ["free:5b"]
        assert freed == 5 * GIB
    finally:
        asyncio.run(ml.stop())


def test_default_yield_plan_refuses_a_siblings_recently_active_model(tmp_path, monkeypatch):
    ml.start()
    try:
        _write_sibling(tmp_path, "sib-a", active={f"{ROOT}|busy:9b": time.time()})
        monkeypatch.setattr(va, "is_default_model", lambda root, name: False)
        residents = [
            {"name": "busy:9b", "in_vram_bytes": 9 * GIB},
            {"name": "free:5b", "in_vram_bytes": 5 * GIB},
        ]
        picked, freed, _ = va._default_yield_plan(ROOT, residents, 4 * GIB)
        assert picked == ["free:5b"]
    finally:
        asyncio.run(ml.stop())


# ── residency leadership ─────────────────────────────────────────────────────

def test_leader_election_oldest_wins_ties_by_port(tmp_path):
    ml.start()
    try:
        model_lease_default = {"root": ROOT, "model": "qwen3.8:27b"}
        with ml._LOCK:
            ml._state["record"]["default"] = model_lease_default
            ml._state["record"]["started"] = 500.0
            ml._state["record"]["port"] = 7001
        _write_sibling(tmp_path, "sib-older", default=model_lease_default, started=100.0, port=7002)
        _write_sibling(tmp_path, "sib-tie-lower-port", default=model_lease_default, started=100.0, port=7000)
        leader = ml.residency_leader(ROOT, "qwen3.8:27b")
        assert leader == {"instance_id": "sib-tie-lower-port", "port": 7000}
        assert ml.is_residency_leader(ROOT, "qwen3.8:27b") is False
    finally:
        asyncio.run(ml.stop())


def test_is_residency_leader_true_when_alone(tmp_path):
    ml.start()
    try:
        ml.note_default(ROOT, "qwen3.8:27b")
        assert ml.is_residency_leader(ROOT, "qwen3.8:27b") is True
    finally:
        asyncio.run(ml.stop())


def test_is_residency_leader_true_before_start():
    assert ml.is_residency_leader(ROOT, "qwen3.8:27b") is True


# ── adoption ─────────────────────────────────────────────────────────────────

def test_adopt_resident_default_picks_the_oldest_qualifying_sibling(tmp_path):
    ml.start()
    try:
        _write_sibling(tmp_path, "sib-newer", default={"root": ROOT, "model": "sib-model:9b"}, started=200.0)
        _write_sibling(tmp_path, "sib-older", default={"root": ROOT, "model": "sib-model:9b"}, started=50.0)
        adopted = ml.adopt_resident_default(ROOT, "my-default:27b", {"sib-model:9b"})
        assert adopted == "sib-model:9b"
    finally:
        asyncio.run(ml.stop())


def test_adopt_resident_default_empty_when_my_own_default_is_resident(tmp_path):
    ml.start()
    try:
        _write_sibling(tmp_path, "sib-a", default={"root": ROOT, "model": "sib-model:9b"})
        assert ml.adopt_resident_default(ROOT, "my-default:27b", {"my-default:27b", "sib-model:9b"}) == ""
    finally:
        asyncio.run(ml.stop())


def test_adopt_resident_default_off_by_setting(tmp_path, monkeypatch):
    ml.start()
    try:
        _write_sibling(tmp_path, "sib-a", default={"root": ROOT, "model": "sib-model:9b"})
        monkeypatch.setattr("src.settings.get_setting",
                            lambda key, default=None: False if key == "model_lease_adopt_resident" else default)
        assert ml.adopt_resident_default(ROOT, "my-default:27b", {"sib-model:9b"}) == ""
    finally:
        asyncio.run(ml.stop())


def test_adopted_model_for_reads_own_record_scoped_to_the_root(tmp_path):
    ml.start()
    try:
        ml.note_default(ROOT, "my-default:27b")
        ml.note_adopted("sib-model:9b")
        assert ml.adopted_model_for(ROOT) == "sib-model:9b"
        assert ml.adopted_model_for("http://127.0.0.1:9999") == ""
    finally:
        asyncio.run(ml.stop())


# ── endpoint_resolver: adopted model only for the IMPLICIT default ──────────

def _install_default_resolver_fakes(monkeypatch, ep_id="ep-1", base_url="http://127.0.0.1:11434/v1",
                                    model="my-default:27b"):
    from types import SimpleNamespace
    import src.endpoint_resolver as er

    settings = {"default_endpoint_id": ep_id, "default_model": model}

    class _Settings:
        @staticmethod
        def get_user_setting(key, owner, default):
            return settings.get(key, default)

        @staticmethod
        def load_settings():
            return dict(settings)

    monkeypatch.setattr(
        "src.settings.get_user_setting",
        lambda key, owner, default: settings.get(key, default),
    )
    monkeypatch.setattr("src.settings.load_settings", lambda: dict(settings))

    ep = SimpleNamespace(id=ep_id, base_url=base_url, api_key=None, is_enabled=True,
                        cached_models=None, models=None, pinned_models=None, hidden_models=None,
                        provider_auth_id=None)

    class _Query:
        def filter(self, *a, **k):
            return self

        def first(self):
            return ep

    class _DB:
        def query(self, *a, **k):
            return _Query()

        def close(self):
            pass

    monkeypatch.setattr(er, "SessionLocal", lambda: _DB())
    return ep


def test_resolve_endpoint_default_uses_the_adopted_model(monkeypatch, tmp_path):
    _install_default_resolver_fakes(monkeypatch)
    from src import endpoint_resolver as er

    ml.start()
    try:
        ml.note_default("http://127.0.0.1:11434", "my-default:27b")
        ml.note_adopted("sib-model:9b")
        url, model, _headers = er.resolve_endpoint("default")
        assert model == "sib-model:9b"
        assert url.startswith("http://127.0.0.1:11434")
    finally:
        asyncio.run(ml.stop())


def test_resolve_endpoint_explicit_session_pick_is_never_adopted(monkeypatch, tmp_path):
    """A caller passing its own `fallback_url`/`fallback_model` (an explicit
    session pick) returns before the adoption hook ever runs, regardless of
    what the lease says."""
    import src.endpoint_resolver as er
    monkeypatch.setattr("src.settings.get_user_setting", lambda key, owner, default: default)
    monkeypatch.setattr("src.settings.load_settings", lambda: {})

    ml.start()
    try:
        ml.note_default("http://127.0.0.1:11434", "my-default:27b")
        ml.note_adopted("sib-model:9b")
        url, model, headers = er.resolve_endpoint(
            "task", fallback_url="http://session.example/chat",
            fallback_model="session-picked:1b", fallback_headers={},
        )
        assert model == "session-picked:1b"
        assert url == "http://session.example/chat"
    finally:
        asyncio.run(ml.stop())


def test_resolve_endpoint_non_default_prefix_is_never_adopted(monkeypatch, tmp_path):
    """Only the plain "default" prefix is eligible — a prefix that merely
    FALLS BACK to the default settings (e.g. "research" with nothing of its
    own configured) must not pick up the adoption either."""
    _install_default_resolver_fakes(monkeypatch)
    import src.endpoint_resolver as er
    monkeypatch.setattr(
        "src.settings.get_user_setting",
        lambda key, owner, default: {"default_endpoint_id": "ep-1", "default_model": "my-default:27b"}.get(key, default),
    )
    monkeypatch.setattr("src.settings.load_settings", lambda: {})

    ml.start()
    try:
        ml.note_default("http://127.0.0.1:11434", "my-default:27b")
        ml.note_adopted("sib-model:9b")
        _url, model, _headers = er.resolve_endpoint("research")
        assert model == "my-default:27b"
    finally:
        asyncio.run(ml.stop())


# ── run_model_pin: restore_keep_alive never shortens a sibling default ─────

def test_restore_keep_alive_never_shortens_a_sibling_pin_or_default(monkeypatch, tmp_path):
    pin.reset_for_tests()
    posted = []

    class _Resp:
        def json(self):
            return {"models": [{"name": "sibling-default:9b"}]}

    class _Http:
        @staticmethod
        def post(url, **kw):
            posted.append((url, kw.get("json")))

        @staticmethod
        def get(url, **kw):
            return _Resp()

    import sys
    monkeypatch.setitem(sys.modules, "httpx", _Http)

    ml.start()
    try:
        _write_sibling(tmp_path, "sib-a", default={"root": ROOT, "model": "sibling-default:9b"})
        assert pin.restore_keep_alive("http://127.0.0.1:11434/v1", "sibling-default:9b", "5m") is True
        assert len(posted) == 1
        assert posted[0][1]["keep_alive"] == -1
    finally:
        asyncio.run(ml.stop())
        pin.reset_for_tests()


# ── model_warmup: follower keeper never reloads/re-pins/unloads ────────────

def _fake_ps(models):
    async def _ps(root):
        return {"models": models}
    return _ps


@pytest.fixture(autouse=True)
def _reset_keeper_state():
    mw._keeper.update({"last_check": None, "resident": None, "expires_at": "", "reloads": 0,
                       "repins": 0, "yielding_to": None, "waiting_for_room": False,
                       "backend": None, "resident_since": None,
                       "leader": None, "follower_of": None, "adopted_model": "", "siblings": 0})
    mw._yield_logged = False
    mw._fit_wait_logged = False
    yield


_TARGET = {"url": "http://127.0.0.1:11434/v1", "model": "qwen3.8:27b", "root": "http://127.0.0.1:11434"}


def test_follower_keeper_observes_only_never_reloads(tmp_path, monkeypatch):
    warmed = []
    pinned = []
    monkeypatch.setattr(mw, "resolve_default", lambda: dict(_TARGET))
    monkeypatch.setattr(mw, "_pin", lambda root, model: pinned.append((root, model)))
    monkeypatch.setattr(mw, "_api_ps", _fake_ps([]))  # absent — a leader would reload

    async def fake_warm_once():
        warmed.append(True)
        return {"ok": True}
    monkeypatch.setattr(mw, "warm_once", fake_warm_once)

    ml.start()
    try:
        _write_sibling(tmp_path, "sib-leader", default=dict(_TARGET.__class__() if False else
                                                            {"root": _TARGET["root"], "model": _TARGET["model"]}),
                       started=1.0, port=7002)
        out = asyncio.run(mw.check_once())
        assert warmed == []              # never reloaded
        assert pinned == []              # never re-pinned
        assert out["leader"] is False
        assert out["follower_of"] == 7002
        assert out["resident"] is False  # observed from /api/ps, not acted on
    finally:
        asyncio.run(ml.stop())


def test_leader_keeper_behaves_exactly_as_before(monkeypatch):
    """Alone (no siblings), leadership is trivially ours — the lease must
    not change the existing, already-tested behaviour at all."""
    warmed = []
    monkeypatch.setattr(mw, "resolve_default", lambda: dict(_TARGET))
    monkeypatch.setattr(mw, "_pin", lambda root, model: None)
    monkeypatch.setattr(mw, "_load_in_flight", lambda model: False)
    monkeypatch.setattr(mw, "_api_ps", _fake_ps([]))
    monkeypatch.setattr("src.vram_admission.assess", lambda root, model: {"fits": True})

    async def fake_warm_once():
        warmed.append(True)
        return {"ok": True}
    monkeypatch.setattr(mw, "warm_once", fake_warm_once)

    ml.start()
    try:
        out = asyncio.run(mw.check_once())
        assert warmed == [True]
        assert out["reloads"] == 1
        assert out["leader"] is True
        assert out["follower_of"] is None
    finally:
        asyncio.run(ml.stop())


def test_adoption_when_own_default_absent_and_sibling_default_resident(tmp_path, monkeypatch):
    monkeypatch.setattr(mw, "resolve_default", lambda: dict(_TARGET))
    monkeypatch.setattr(mw, "_pin", lambda root, model: None)
    monkeypatch.setattr(mw, "_load_in_flight", lambda model: False)
    # Our own default is absent; a sibling's OWN default ("sib-model:9b") is
    # what /api/ps actually shows resident.
    monkeypatch.setattr(mw, "_api_ps", _fake_ps([{"name": "sib-model:9b"}]))
    warmed = []

    async def fake_warm_once():
        warmed.append(True)
        return {"ok": True}
    monkeypatch.setattr(mw, "warm_once", fake_warm_once)

    ml.start()
    try:
        _write_sibling(tmp_path, "sib-a", default={"root": _TARGET["root"], "model": "sib-model:9b"},
                       started=1.0)
        out = asyncio.run(mw.check_once())
        assert warmed == []                       # never loaded our own default
        assert out["adopted_model"] == "sib-model:9b"
        assert ml.self_record()["adopted_model"] == "sib-model:9b"

        # The sibling goes away (its file is simply gone/stale) → adoption clears.
        for f in (tmp_path / "leases").glob("sib-a.json"):
            f.unlink()
        out2 = asyncio.run(mw.check_once())
        assert out2["adopted_model"] == ""
        assert ml.self_record()["adopted_model"] == ""
    finally:
        asyncio.run(ml.stop())


def test_adoption_clears_once_our_own_default_becomes_resident(tmp_path, monkeypatch):
    monkeypatch.setattr(mw, "resolve_default", lambda: dict(_TARGET))
    monkeypatch.setattr(mw, "_pin", lambda root, model: None)

    ml.start()
    try:
        mw._keeper["adopted_model"] = "sib-model:9b"
        ml.note_adopted("sib-model:9b")
        monkeypatch.setattr(mw, "_api_ps", _fake_ps(
            [{"name": "qwen3.8:27b", "expires_at": "0001-01-01T00:00:00Z"}]))
        out = asyncio.run(mw.check_once())
        assert out["adopted_model"] == ""
        assert ml.self_record()["adopted_model"] == ""
    finally:
        asyncio.run(ml.stop())


# ── snapshot / holders / the /instances route ────────────────────────────────

def test_holders_reports_every_matching_kind_across_instances(tmp_path):
    ml.start()
    try:
        ml.note_default(ROOT, "my-default:27b")
        ml.note_pin(ROOT, "coder:30b")
        _write_sibling(tmp_path, "sib-a", active={f"{ROOT}|coder:30b": time.time()}, port=7002)
        rows = ml.holders(ROOT, "coder:30b")
        kinds = {(r["port"], r["kind"]) for r in rows}
        assert (7001, "pinned") in kinds
        assert (7002, "active") in kinds
    finally:
        asyncio.run(ml.stop())


def test_snapshot_lists_self_and_siblings_with_resident_holders(tmp_path, monkeypatch):
    monkeypatch.setattr(va, "_get", lambda root, path, timeout: {"models": [{"name": "my-default:27b"}]})
    ml.start()
    try:
        ml.note_default(ROOT, "my-default:27b")
        _write_sibling(tmp_path, "sib-a", default={"root": ROOT, "model": "sib-model:9b"}, port=7002)
        snap = ml.snapshot(ROOT)
        ids = {i["instance_id"] for i in snap["instances"]}
        assert ids == {ml.self_record()["instance_id"], "sib-a"}
        resident_models = {r["model"] for r in snap["resident"]}
        assert resident_models == {"my-default:27b"}
        my_holders = next(r for r in snap["resident"] if r["model"] == "my-default:27b")["holders"]
        assert any(h["kind"] == "default" and h["port"] == 7001 for h in my_holders)
    finally:
        asyncio.run(ml.stop())


def test_instances_route_returns_the_snapshot(monkeypatch, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import routes.local_models_routes as lmr

    app = FastAPI()
    app.include_router(lmr.setup_local_models_routes())

    ep = {"id": "ep-1", "name": "Ollama", "base_url": ROOT + "/v1",
         "root": ROOT, "same_machine": True}
    monkeypatch.setattr(lmr, "list_ollama_endpoints", lambda **kw: [ep])
    monkeypatch.setattr(lmr, "require_user", lambda request: "tester")
    monkeypatch.setattr(va, "_get", lambda root, path, timeout: {"models": []})

    ml.start()
    try:
        ml.note_default(ROOT, "my-default:27b")
        client = TestClient(app)
        resp = client.get("/api/local-models/instances")
        assert resp.status_code == 200
        body = resp.json()
        assert body["self_instance_id"] == ml.self_record()["instance_id"]
        assert any(i["instance_id"] == body["self_instance_id"] for i in body["instances"])
    finally:
        asyncio.run(ml.stop())
