"""H22 fresh SQLite coherence checks; no provider/model requests."""
import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database as d
from src import endpoint_resolver as r, settings, chatgpt_subscription as auth


@pytest.fixture
def store(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///" + str(tmp_path / "endpoints.db"))
    d.ModelEndpoint.__table__.create(engine)
    sessions = sessionmaker(bind=engine)
    with sessions() as db:
        db.add(d.ModelEndpoint(id="a", name="fixture", owner="alice", is_enabled=True,
            base_url="https://old.invalid/v1", provider_auth_id="auth-a", cached_models='["model"]'))
        db.commit()
    monkeypatch.setattr(r, "SessionLocal", sessions)
    monkeypatch.setattr(settings, "load_settings", lambda: {})
    monkeypatch.setattr(settings, "get_user_setting", lambda key, owner, default=None:
        {"utility_endpoint_id": "a", "utility_model": "model"}.get(key, default))
    yield sessions
    engine.dispose()


def invoke(path):
    if path == "utility":
        return r.resolve_endpoint("utility", fallback_url="https://fallback.invalid", fallback_model="fallback", owner="alice")
    if path == "id":
        return r.resolve_endpoint_by_id("a", "model", owner="alice")
    if path == "descriptor":
        return r.resolve_route_descriptor("https://old.invalid/v1/chat/completions", "model", {}, owner="alice")
    if path == "chain":
        return r.resolve_fallback_entries([{"endpoint_id": "a", "model": "model"}], owner="alice")
    return r.resolve_fallback_entries_with_descriptors([{"endpoint_id": "a", "model": "model"}], owner="alice")


@pytest.mark.parametrize("path", ["utility", "id", "descriptor", "chain", "descriptor_chain"])
@pytest.mark.parametrize("change", ["connection_revision", "base_url", "api_key", "provider_auth_id", "endpoint_kind", "owner", "is_enabled", "delete"])
def test_mutation_never_returns_mixed_route_or_fallback(store, monkeypatch, path, change):
    def credentials(*args, **kw):
        with store() as db:
            ep = db.get(d.ModelEndpoint, "a")
            if change == "delete":
                db.delete(ep)
            else:
                setattr(ep, change, False if change == "is_enabled" else "replacement")
            db.commit()
        return {"base_url": "https://new.invalid/v1", "api_key": "synthetic-new-token"}
    monkeypatch.setattr(auth, "resolve_runtime_credentials", credentials)
    with pytest.raises(r.EndpointConfigurationChanged, match="configuration changed"):
        invoke(path)


@pytest.mark.parametrize("path", ["utility", "id"])
def test_explicit_new_attempt_captures_current_configuration(store, monkeypatch, path):
    calls = []
    def credentials(auth_id, **kw):
        calls.append(auth_id)
        if len(calls) == 1:
            with store() as db:
                ep = db.get(d.ModelEndpoint, "a")
                ep.provider_auth_id = "auth-b"
                ep.base_url = "https://new.invalid/v1"
                db.commit()
        return {"base_url": "https://new.invalid/v1", "api_key": "synthetic"}
    monkeypatch.setattr(auth, "resolve_runtime_credentials", credentials)
    with pytest.raises(r.EndpointConfigurationChanged):
        invoke(path)
    assert calls == ["auth-a"]  # No automatic credential retry.
    result = invoke(path)
    assert calls == ["auth-a", "auth-b"]
    assert result[0] == "https://new.invalid/v1/chat/completions"


@pytest.mark.parametrize("path", ["utility", "id"])
def test_unchanged_endpoint_accepts_rotated_access_token(store, monkeypatch, path):
    monkeypatch.setattr(auth, "resolve_runtime_credentials", lambda *a, **kw:
        {"base_url": "https://old.invalid/v1", "api_key": "synthetic-refreshed-access"})
    result = invoke(path)
    assert result[2]["Authorization"] == "Bearer synthetic-refreshed-access"


def fail(*a, **kw):
    raise r.EndpointConfigurationChanged("fixture configuration changed")


def test_worker_does_not_adopt_coordinator(monkeypatch):
    from src.agent_tools import subagent_tools as st
    monkeypatch.setattr(r, "resolve_endpoint_by_id", fail)
    run = SimpleNamespace(endpoint_id="a", model_override="model", team_bound=False, agent_def={})
    with pytest.raises(r.EndpointConfigurationChanged):
        st._route_for(run, "https://coordinator.invalid", "alice", {"Authorization": "coordinator"})
    assert run.agent_def == {}


def test_vision_does_not_adopt_other_endpoint(monkeypatch):
    from src import vision_routing as v
    monkeypatch.setattr(v, "list_vision_endpoints", lambda owner: [])
    monkeypatch.setattr(r, "resolve_endpoint_by_id", fail)
    with pytest.raises(r.EndpointConfigurationChanged):
        v._resolve_on_endpoint("a", "model", "alice")


def test_vision_listing_propagates_nested_runtime_change(store, monkeypatch):
    from src import vision_routing as v, database
    monkeypatch.setattr(database, "SessionLocal", store)
    monkeypatch.setattr(r, "resolve_endpoint_runtime", fail)
    with pytest.raises(r.EndpointConfigurationChanged):
        v.list_vision_endpoints("alice")


def test_model_search_does_not_probe_next_endpoint(store, monkeypatch):
    from src import ai_interaction as ai, database
    monkeypatch.setattr(database, "SessionLocal", store)
    monkeypatch.setattr(ai, "resolve_endpoint_runtime", fail)
    with pytest.raises(r.EndpointConfigurationChanged):
        ai._resolve_model("model", owner="alice")


def test_fanout_does_not_dispatch_coordinator(monkeypatch):
    from src.fanout import runner as f
    manifest = {"plan": {"prompt": "fixture", "workspace": "", "candidates": [{"label": "one", "model": "model", "endpoint_id": "a"}]},
                "exp_id": "fixture", "candidates": [{"label": "one", "model": "model", "endpoint_id": "a"}]}
    monkeypatch.setattr(f, "_manifest_or_error", lambda *a: manifest)
    monkeypatch.setattr(f, "_save_manifest", lambda *a: None)
    monkeypatch.setattr(f.budget_account, "open", lambda *a, **kw: None)
    monkeypatch.setattr(r, "resolve_endpoint_by_id", fail)
    async def no_dispatch(*a, **kw):
        pytest.fail("coordinator dispatch must not happen")
    monkeypatch.setattr(f, "_run_one_candidate", no_dispatch)
    with pytest.raises(r.EndpointConfigurationChanged):
        asyncio.run(f.run_all("fixture", "alice", coordinator_endpoint_url="https://coordinator.invalid"))


def test_verification_failure_cannot_fall_back(store, monkeypatch):
    with store() as db:
        ep = db.get(d.ModelEndpoint, "a")
        def unavailable():
            raise RuntimeError("synthetic-secret-not-for-errors")
        monkeypatch.setattr(r, "SessionLocal", unavailable)
        monkeypatch.setattr(auth, "resolve_runtime_credentials", lambda *a, **kw: {"api_key": "synthetic"})
        with pytest.raises(r.EndpointConfigurationChanged, match="could not be verified") as caught:
            r.resolve_endpoint_runtime(ep, owner="alice")
        assert "synthetic-secret" not in str(caught.value)


def test_static_key_resolution_retains_existing_contract(monkeypatch):
    ep = SimpleNamespace(base_url="https://static.invalid/v1", api_key="synthetic", provider_auth_id=None)
    monkeypatch.setattr(r, "SessionLocal", lambda: pytest.fail("static resolution must not query auth coherence"))
    assert r.resolve_endpoint_runtime(ep) == ("https://static.invalid/v1", "synthetic")


def test_original_race_is_reproduced_when_guard_is_removed(store, monkeypatch):
    monkeypatch.setattr(r, "_assert_runtime_config_current", lambda snapshot: None)
    def changed_credentials(*a, **kw):
        with store() as db:
            ep = db.get(d.ModelEndpoint, "a")
            ep.provider_auth_id = "auth-b"
            db.commit()
        return {"base_url": "https://new.invalid/v1", "api_key": "synthetic-b"}
    monkeypatch.setattr(auth, "resolve_runtime_credentials", changed_credentials)
    with store() as db:
        old_revision = db.get(d.ModelEndpoint, "a").connection_revision
    route, descriptor = r._resolve_endpoint_by_id_with_descriptor("a", "model", owner="alice")
    assert route[0] == "https://new.invalid/v1/chat/completions"
    assert descriptor["connection_revision"] == old_revision
    with store() as db:
        assert db.get(d.ModelEndpoint, "a").connection_revision != old_revision


@pytest.mark.parametrize("path", ["utility", "id"])
@pytest.mark.parametrize("change", ["provider_auth_id", "owner", "delete"])
def test_configuration_drift_wins_over_credentials_failure(store, monkeypatch, path, change):
    def failed_credentials(*a, **kw):
        with store() as db:
            ep = db.get(d.ModelEndpoint, "a")
            if change == "delete":
                db.delete(ep)
            else:
                setattr(ep, change, "replacement")
            db.commit()
        raise ValueError("synthetic credential unavailable")
    monkeypatch.setattr(auth, "resolve_runtime_credentials", failed_credentials)
    with pytest.raises(r.EndpointConfigurationChanged):
        invoke(path)


@pytest.mark.parametrize("path", ["utility", "id"])
def test_unchanged_credential_failure_preserves_legacy_fallback(store, monkeypatch, path):
    def failed_credentials(*a, **kw):
        raise ValueError("synthetic credential unavailable")
    monkeypatch.setattr(auth, "resolve_runtime_credentials", failed_credentials)
    result = invoke(path)
    assert result == (("https://fallback.invalid", "fallback", None) if path == "utility" else None)


@pytest.mark.parametrize("producer", ["worker", "vision", "search"])
def test_real_producer_drift_and_credential_error_do_not_dispatch(store, monkeypatch, producer):
    from src import database, ai_interaction as ai, vision_routing as v
    from src.agent_tools import subagent_tools as st
    monkeypatch.setattr(database, "SessionLocal", store)
    def failed_credentials(*a, **kw):
        with store() as db:
            db.get(d.ModelEndpoint, "a").provider_auth_id = "auth-b"
            db.commit()
        raise ValueError("synthetic old auth unavailable")
    monkeypatch.setattr(auth, "resolve_runtime_credentials", failed_credentials)
    with pytest.raises(r.EndpointConfigurationChanged):
        if producer == "worker":
            st._route_for(SimpleNamespace(endpoint_id="a", model_override="model", team_bound=False, agent_def={}),
                          "https://coordinator.invalid", "alice")
        elif producer == "vision":
            v.list_vision_endpoints("alice")
        else:
            ai._resolve_model("model", owner="alice")
