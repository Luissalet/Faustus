"""Detected distributed engines preserve the controller's residency in public status."""
import pytest
from src import sparks


def detected_status(monkeypatch, detected):
    monkeypatch.setattr(sparks, "config", lambda: {
        "enabled": True, "url": "http://controller.test", "default_backend": True,
        "recipe": "", "local_default": {}, "endpoints": {}})
    monkeypatch.setattr(sparks, "effective_default", lambda: {})
    monkeypatch.setattr(sparks, "_request", lambda *args, **kwargs: (200, {
        "nodes": [{"id": node, "online": True} for node in ("spark1", "spark2", "spark3")],
        "detected": detected}))
    return sparks.status(with_recipes=False)


def test_distributed_residency_is_preserved_without_certifying_context(monkeypatch):
    out = detected_status(monkeypatch, [{
        "recipe": "spark1-8002", "node": "spark1", "nodes": ["spark1", "spark2", "spark3"],
        "models": ["distributed-model"], "up": True, "max_model_len": 1048576}])
    dep = out["deployments"][0]
    assert dep["nodes"] == ["spark1", "spark2", "spark3"]
    assert dep["head"] == "spark1" and dep["state"] == "running"
    assert dep["served"] == ["distributed-model"]
    assert not dep.get("context_verified")


@pytest.mark.parametrize("nodes", [None, [], "spark1,spark2", {"spark1": True}])
def test_legacy_or_malformed_residency_falls_back_to_reported_head(monkeypatch, nodes):
    dep = detected_status(monkeypatch, [{"node": "spark3", "nodes": nodes, "up": False}])["deployments"][0]
    assert dep["nodes"] == ["spark3"] and dep["state"] == "starting"


def test_invalid_members_do_not_create_phantom_workers(monkeypatch):
    dep = detected_status(monkeypatch, [{"node": "spark1", "nodes": ["spark1", "spark2", "spark2", None, 3, ""], "up": True}])["deployments"][0]
    assert dep["nodes"] == ["spark1", "spark2"]
    unknown = detected_status(monkeypatch, [{"nodes": [None], "up": False}])["deployments"][0]
    assert unknown["nodes"] == []
