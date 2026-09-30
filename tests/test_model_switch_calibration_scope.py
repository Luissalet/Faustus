"""H22 model switches read scoped evidence, with SQLite PATCH fixtures."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import core.database as db
from src import agent_loop, model_calibration as c, llm_core

NATIVE = "http://127.0.0.1:11434/api/chat"


@pytest.fixture(autouse=True)
def temporary_store(tmp_path, monkeypatch):
    monkeypatch.setattr(c, "_default_data_dir", lambda: str(tmp_path / "calibration"))
    monkeypatch.setattr(c, "_record_deployment_evidence", lambda *a, **kw: None)


def write(*, model="new", ep="a", digest="", ok=True, announced=None):
    return c.save_scoped_tested(vendor="ollama", model_id=model, endpoint_id=ep,
        protocol=c.NATIVE_OLLAMA_PROTOCOL, digest=digest,
        tested={c.TEST_TOOL_CALLING: {"ok": ok}},
        announced={"capabilities": {"tools": True}} if announced is None else announced)


def switch(**changes):
    args = dict(previous_model="old", previous_endpoint_url=NATIVE,
        new_model="new", new_endpoint_url=NATIVE, previous_endpoint_id="before", new_endpoint_id="a")
    return agent_loop.recompute_capabilities_on_model_switch(**{**args, **changes})


@pytest.mark.parametrize("ok", [True, False])
def test_helper_legacy_observations_are_removed_but_announcements_and_shape_stay(ok):
    key = c.manifest_key(vendor="ollama", model_id="new", endpoint_id="a")
    c.save_tested(key, {c.TEST_TOOL_CALLING: {"ok": ok}}, announced={"capabilities": {"tools": True}})
    result = switch()
    assert result["capabilities"]["tested"] == {}
    assert result["capabilities"]["announced"]["capabilities"]["tools"] is True
    assert set(result["capabilities"]) == {"announced", "tested", "degraded", "updated_at"}
    assert c.get_manifest(key)["tested"][c.TEST_TOOL_CALLING]["ok"] is ok


@pytest.mark.parametrize("ok", [True, False])
def test_helper_without_revision_keeps_historical_probe_out(ok):
    write(ok=ok)
    assert switch()["capabilities"]["tested"] == {}  # Revision propagation pending.


@pytest.mark.parametrize("changes", [{"new_endpoint_id": "b"}, {"new_endpoint_id": ""},
    {"new_endpoint_url": "http://127.0.0.1:11434/v1"},
    {"new_endpoint_url": "http://127.0.0.1:11434"}])
def test_helper_unknown_or_mismatched_context_never_selects_probe(changes):
    write()
    assert switch(**changes)["capabilities"]["tested"] == {}


@pytest.mark.parametrize("digest", ["", "different", None, {"value": "blob"}])
def test_helper_does_not_adopt_digest_observation_without_explicit_matching_string(digest):
    write(digest="blob")
    assert switch(new_digest=digest)["capabilities"]["tested"] == {}


def test_helper_without_revision_cannot_adopt_digest_scoped_artifact():
    write(model="old-alias", ep="before", digest="oldblob", ok=False,
        announced={"capabilities": {"tools": True, "vision": True}})
    write(model="new-alias", digest="newblob", ok=True, announced={})
    result = switch(previous_digest="oldblob", new_digest="newblob")
    assert result["lost"] == []  # Old scoped declarations are not current authority.
    assert result["capabilities"]["tested"] == {}


def test_probe_difference_alone_never_creates_lost_capabilities():
    write(model="old", ep="before", ok=True)
    write(ok=False)
    assert switch()["lost"] == []


@pytest.fixture
def patch_session(tmp_path, monkeypatch):
    from routes import session_routes as routes
    engine = create_engine("sqlite:///" + str(tmp_path / "sessions.sqlite"))
    db.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(routes, "SessionLocal", factory)
    monkeypatch.setattr(routes, "effective_user", lambda request: "alice")
    monkeypatch.setattr(routes, "_reject_raw_endpoint_url_for_non_admin", lambda *a, **kw: None)
    # Headers/auth are outside this read-only calibration change.
    session = SimpleNamespace(model="old", endpoint_url=NATIVE, headers={}, folder=None)
    manager = MagicMock()
    manager.get_session.return_value = session
    with factory() as connection:
        connection.add(db.Session(id="s", owner="alice", name="chat", model="old", endpoint_url=NATIVE, headers={}))
        connection.add(db.ModelEndpoint(id="a", owner="alice", name="native", base_url=NATIVE,
            api_key=None, is_enabled=True))
        connection.commit()
    router = routes.setup_session_routes(manager, {})
    handler = next(route.endpoint for route in router.routes
        if route.path == "/api/session/{sid}" and "PATCH" in route.methods)

    def call(*, endpoint_id="a", url=NATIVE):
        return handler(request=None, sid="s", name=None, folder=None, model="new",
            endpoint_url=url, endpoint_id=endpoint_id)

    yield call, session, factory
    engine.dispose()


@pytest.mark.parametrize("ok", [True, False])
def test_patch_without_revision_excludes_probe_and_persists_existing_fields(patch_session, ok):
    call, session, factory = patch_session
    write(ok=ok)
    result = call()
    assert result["capabilities"]["tested"] == {}  # Session revision propagation pending.
    assert set(result["capabilities"]) == {"announced", "tested", "degraded", "updated_at"}
    assert result["model"] == session.model == "new"
    assert result["endpoint_url"] == session.endpoint_url == NATIVE
    with factory() as connection:
        stored = connection.get(db.Session, "s")
        assert stored.model == "new" and stored.endpoint_url == NATIVE and stored.headers == {}


def test_patch_previous_endpoint_unknown_never_adopts_old_scoped_origin(patch_session):
    call, _, _ = patch_session
    write(model="old", ep="before", announced={"capabilities": {"tools": True, "vision": True}})
    write(announced={})
    assert call()["lost"] == []  # No previous endpoint identity retained by session.


def test_patch_legacy_prior_announcements_explain_loss_without_prior_probe(patch_session):
    call, _, _ = patch_session
    c.save_tested(c.manifest_key(vendor="ollama", model_id="old"),
        {c.TEST_TOOL_CALLING: {"ok": False}}, announced={"capabilities": {"tools": True, "vision": True}})
    write(ok=True, announced={})
    assert call()["lost"] == ["vision", "native tool calling"]


def test_patch_no_endpoint_or_digest_does_not_fabricate_probe_identity(patch_session):
    call, _, _ = patch_session
    write(digest="blob")
    assert call()["capabilities"]["tested"] == {}
    assert call(endpoint_id=None)["capabilities"]["tested"] == {}


def test_patch_unknown_protocol_preserves_only_legacy_declarations(patch_session):
    call, _, _ = patch_session
    url = "http://127.0.0.1:11434/v1"
    c.save_tested(c.manifest_key(vendor=llm_core._detect_provider(url), model_id="new"),
        {c.TEST_TOOL_CALLING: {"ok": True}}, announced={"capabilities": {"tools": True}})
    result = call(endpoint_id=None, url=url)
    assert result["capabilities"]["tested"] == {}
    assert result["capabilities"]["announced"]["capabilities"]["tools"] is True
