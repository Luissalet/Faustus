"""src/sparks.py — the Sparks as default backend through Prometheus's Hoard, against a fake Prometheus and an in-memory DB."""

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import core.database as cdb
from core.database import Base, ModelEndpoint
from src import sparks
from src.settings import load_settings, save_settings, update_settings


class FakePrometheus:
    def __init__(self):
        self.up = True
        self.running = []          # endpoints
        self.recipes = [{"name": "glm53-tp3", "title": "GLM TP3"}, {"name": "qwen38-27b-1m", "title": "Qwen 1M"}]
        self.calls = []

    def request(self, method, path, body=None, *, timeout=4.0, url=None):
        if not self.up:
            return None, None
        self.calls.append((method, path, body))
        if path == "/api/endpoints":
            return 200, {"endpoints": self.running}
        if path == "/api/overview":
            return 200, {"nodes": [{"id": "spark1", "name": "Spark1", "online": True, "memory": {"total": 128e9, "used": 100e9, "percent": 78},
                                    "gpu": {"util": 90}, "cpu": {"percent": 5}, "fabric": {"a": {"up": True, "rx_bps": 1, "tx_bps": 2}},
                                    "deployments": []}],
                         "cluster": {"online": 1, "total": 1}, "deployments": [], "jobs": []}
        if path == "/api/ui/call":
            name = body["name"]
            if name == "recipes_list":
                return 200, {"recipes": self.recipes}
            if name == "deploy_start":
                if body["arguments"]["recipe"] == "glm53-tp3" and self.running and not body["arguments"].get("stop_conflicts"):
                    return 409, {"error": "busy", "code": "conflict", "conflicts": [self.running[0]["recipe"]]}
                self.running = [self.endpoint(body["arguments"]["recipe"])]
                return 200, {"state": "starting"}
            if name == "deploy_stop":
                self.running = []
                return 200, {"state": "stopped"}
        return 404, {"error": "?"}

    @staticmethod
    def endpoint(recipe):
        model = {"glm53-tp3": "glm-5.3-flash", "qwen38-27b-1m": "qwen3.8-27b"}[recipe]
        return {"recipe": recipe, "title": recipe, "base_url": f"http://spark:{8000 if 'glm' in recipe else 8001}/v1", "models": [model],
                "max_model_len": 1048576, "default": False}


@pytest.fixture
def fake(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(cdb, "SessionLocal", sessionmaker(bind=engine, autoflush=False))
    prom = FakePrometheus()
    monkeypatch.setattr(sparks, "_request", prom.request)
    import routes.prefs_routes as pr
    monkeypatch.setattr(pr, "_load", lambda: {})
    # a local default the person had before the Sparks
    db = cdb.SessionLocal()
    db.add(ModelEndpoint(id="local1", name="Ollama", base_url="http://127.0.0.1:11434/v1", is_enabled=True))
    db.commit()
    db.close()
    s = load_settings()
    s["default_endpoint_id"], s["default_model"] = "local1", "qwen3.8:27b-q4_K_M"
    save_settings(s)
    return prom


def _endpoint(ep_id):
    db = cdb.SessionLocal()
    try:
        return db.query(ModelEndpoint).filter(ModelEndpoint.id == ep_id).first()
    finally:
        db.close()


def test_status_unreachable_is_not_an_error(fake):
    fake.up = False
    out = sparks.status()
    assert out["ok"] is False and out["error"] == "prometheus unreachable"
    assert sparks.sync() == {"ok": False, "error": "prometheus unreachable", "action": "none"}


def test_disabled(fake):
    update_settings({"sparks_enabled": False})
    assert sparks.status()["error"] == "disabled"
    assert fake.calls == []


def test_status_trims_nodes(fake):
    out = sparks.status()
    assert out["ok"] and out["nodes"][0]["fabric"] == {"up": 1, "total": 1, "rx_bps": 1, "tx_bps": 2}
    assert [r["name"] for r in out["recipes"]] == ["glm53-tp3", "qwen38-27b-1m"]


def test_default_moves_to_the_sparks_and_back(fake):
    assert sparks.sync()["action"] == "none"       # nothing serving: the local default stays
    assert load_settings()["default_endpoint_id"] == "local1"
    fake.running = [fake.endpoint("glm53-tp3")]
    res = sparks.sync()
    assert res["action"] == "to_sparks"
    s = load_settings()
    ep_id = s["sparks_endpoints"]["glm53-tp3"]
    assert s["default_endpoint_id"] == ep_id and s["default_model"] == "glm-5.3-flash"
    assert s["sparks_local_default"] == {"endpoint_id": "local1", "model": "qwen3.8:27b-q4_K_M"}
    ep = _endpoint(ep_id)
    assert ep.is_enabled and ep.base_url == "http://spark:8000/v1" and json.loads(ep.cached_models) == ["glm-5.3-flash"]
    assert ep.name == "Sparks · glm53-tp3"
    fake.running = []
    assert sparks.sync()["action"] == "to_local"
    s = load_settings()
    assert s["default_endpoint_id"] == "local1" and s["default_model"] == "qwen3.8:27b-q4_K_M"
    assert _endpoint(ep_id).is_enabled is False


def test_preferred_recipe(fake):
    fake.running = [fake.endpoint("glm53-tp3"), fake.endpoint("qwen38-27b-1m")]
    update_settings({"sparks_recipe": "qwen38-27b-1m"})
    sparks.sync()
    assert load_settings()["default_model"] == "qwen3.8-27b"


def test_default_backend_off_leaves_the_default_alone(fake):
    update_settings({"sparks_default_backend": False})
    fake.running = [fake.endpoint("glm53-tp3")]
    res = sparks.sync()
    assert res["action"] == "none" and load_settings()["default_endpoint_id"] == "local1"
    assert _endpoint(res["endpoints"]["glm53-tp3"]).is_enabled  # the endpoint is still offered in the picker


def test_a_manual_default_turns_the_switch_off(fake):
    fake.running = [fake.endpoint("glm53-tp3")]
    sparks.sync()
    s = load_settings()
    s["default_endpoint_id"], s["default_model"] = "local1", "otro"
    save_settings(s)
    res = sparks.sync()
    assert res["action"] == "user_override"
    s = load_settings()
    assert s["sparks_default_backend"] is False and s["default_model"] == "otro"
    assert sparks.sync()["action"] == "none"


def test_deploy_proxies_and_syncs(fake):
    out = sparks.deploy("qwen38-27b-1m", "start")
    assert out["ok"] and load_settings()["default_model"] == "qwen3.8-27b"
    conflict = sparks.deploy("glm53-tp3", "start")
    assert conflict["ok"] is False and conflict["status"] == 409 and conflict["conflicts"] == ["qwen38-27b-1m"]
    assert sparks.deploy("glm53-tp3", "start", stop_conflicts=True)["ok"]
    assert load_settings()["default_model"] == "glm-5.3-flash"
    sparks.deploy("glm53-tp3", "stop")
    assert load_settings()["default_endpoint_id"] == "local1"
    assert sparks.deploy("x", "restart")["ok"] is False


def test_preferences(fake):
    assert sparks.set_preferences(url="ftp://x")["ok"] is False
    out = sparks.set_preferences(url="http://127.0.0.1:5999/", default_backend=False, recipe="glm53-tp3")
    assert out["config"]["url"] == "http://127.0.0.1:5999" and out["config"]["default_backend"] is False and out["config"]["recipe"] == "glm53-tp3"


def test_routes_registered():
    from routes.sparks_routes import setup_sparks_routes

    paths = {getattr(r, "path", "") for r in setup_sparks_routes().routes}
    assert {"/api/sparks/status", "/api/sparks/sync", "/api/sparks/deploy", "/api/sparks/settings"} <= paths
    app_src = open("app.py", encoding="utf-8").read()
    assert "setup_sparks_routes()" in app_src and "_sparks_sync_loop()" in app_src


def test_detected_servers_show_as_loaded(fake, monkeypatch):
    orig = fake.request

    def request(method, path, body=None, **kw):
        code, data = orig(method, path, body, **kw)
        if path == "/api/overview":
            data["detected"] = [{"recipe": "spark3-8003", "title": "qwen (Spark3:8003)", "node": "spark3", "up": True,
                                 "base_url": "http://spark3:8003/v1", "models": ["qwen3.8-27b-nvfp4"], "max_model_len": 1048576}]
        return code, data

    monkeypatch.setattr(sparks, "_request", request)
    dep = sparks.status()["deployments"][-1]
    assert dep["detected"] and dep["state"] == "running" and dep["served"] == ["qwen3.8-27b-nvfp4"]


def test_disabling_gives_the_default_back(fake):
    fake.running = [fake.endpoint("glm53-tp3")]
    sparks.sync()
    ep_id = load_settings()["default_endpoint_id"]
    update_settings({"sparks_enabled": False})
    out = sparks.sync()
    assert out["action"] == "to_local" and load_settings()["default_endpoint_id"] == "local1"
    assert _endpoint(ep_id).is_enabled is False


def test_prometheus_closed_keeps_a_server_that_still_answers(fake, monkeypatch):
    fake.running = [fake.endpoint("glm53-tp3")]
    sparks.sync()
    ep_id = load_settings()["default_endpoint_id"]
    answers = {"v": {"data": [{"id": "glm-5.3-flash"}]}}
    orig = fake.request

    def request(method, path, body=None, *, timeout=4.0, url=None):
        if url and url.startswith("http://spark:"):
            return (200, answers["v"]) if answers["v"] is not None else (None, None)
        fake.up = False
        return orig(method, path, body, timeout=timeout, url=url)

    monkeypatch.setattr(sparks, "_request", request)
    assert sparks.sync()["action"] == "none" and load_settings()["default_endpoint_id"] == ep_id
    answers["v"] = None
    assert sparks.sync()["action"] == "to_local"
    assert load_settings()["default_endpoint_id"] == "local1" and _endpoint(ep_id).is_enabled is False


@pytest.mark.parametrize("served", [{"data": []}, {"data": [{"id": "otro"}]}, {"error": "x"}])
def test_prometheus_closed_drops_a_port_that_serves_something_else(fake, monkeypatch, served):
    fake.running = [fake.endpoint("glm53-tp3")]
    sparks.sync()
    orig = fake.request

    def request(method, path, body=None, *, timeout=4.0, url=None):
        if url and url.startswith("http://spark:"):
            return 200, served
        fake.up = False
        return orig(method, path, body, timeout=timeout, url=url)

    monkeypatch.setattr(sparks, "_request", request)
    assert sparks.sync()["action"] == "to_local" and load_settings()["default_endpoint_id"] == "local1"


def test_a_sparks_model_picked_by_hand_stays_with_the_switch_off(fake):
    update_settings({"sparks_default_backend": False})
    fake.running = [fake.endpoint("glm53-tp3")]
    ep = sparks.sync()["endpoints"]["glm53-tp3"]
    s = load_settings()
    s["default_endpoint_id"], s["default_model"] = ep, "glm-5.3-flash"
    save_settings(s)
    assert sparks.sync()["action"] == "none" and load_settings()["default_endpoint_id"] == ep
    fake.running = []                                   # it stops serving: back to the local default
    assert sparks.sync()["action"] == "to_local" and load_settings()["default_endpoint_id"] == "local1"


def test_turning_the_switch_off_undoes_the_automatic_default(fake):
    fake.running = [fake.endpoint("glm53-tp3")]
    assert sparks.sync()["action"] == "to_sparks"
    update_settings({"sparks_default_backend": False})
    assert sparks.sync()["action"] == "to_local" and load_settings()["default_endpoint_id"] == "local1"


def test_another_sparks_model_picked_by_hand_is_an_override(fake):
    fake.running = [fake.endpoint("glm53-tp3"), fake.endpoint("qwen38-27b-1m")]
    ids = sparks.sync()["endpoints"]
    s = load_settings()
    s["default_endpoint_id"], s["default_model"] = ids["qwen38-27b-1m"], "qwen3.8-27b"
    save_settings(s)
    assert sparks.sync()["action"] == "user_override"
    s = load_settings()
    assert s["sparks_default_backend"] is False and s["default_model"] == "qwen3.8-27b"
    assert sparks.sync()["action"] == "none" and load_settings()["default_model"] == "qwen3.8-27b"
