"""H22 linked credentials invalidate every route in the same ORM transaction."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from core import database as d
from routes import chatgpt_subscription_routes as routes
from src import chatgpt_subscription as oauth, model_calibration as c


@pytest.fixture
def database(tmp_path, monkeypatch):
    engine = create_engine('sqlite:///' + str(tmp_path / 'auth.db'))
    d.ModelEndpoint.__table__.create(engine)
    d.ProviderAuthSession.__table__.create(engine)
    factory = sessionmaker(bind=engine, autoflush=False)
    monkeypatch.setattr(routes, 'SessionLocal', factory)
    monkeypatch.setattr(oauth, '_database_handles', lambda: (d.ProviderAuthSession, factory, d.utcnow_naive))
    monkeypatch.setattr(oauth, 'fetch_available_models', lambda token: ['fixture'])
    monkeypatch.setattr(c, '_default_data_dir', lambda: str(tmp_path))
    monkeypatch.setattr(c, '_record_deployment_evidence', lambda *args: None)
    result = routes._provision_endpoint({'access_token': 'synthetic-access', 'refresh_token': 'synthetic-refresh'}, 'alice')
    with factory() as db:
        primary = db.get(d.ModelEndpoint, result['id'])
        auth_id = primary.provider_auth_id
        db.add(d.ModelEndpoint(id='second', name='shared credential', owner='other',
            base_url=primary.base_url, provider_auth_id=auth_id))
        db.add(d.ModelEndpoint(id='unrelated', name='unrelated', base_url='http://fixture/api'))
        db.commit()
    yield factory, auth_id, result['id']
    engine.dispose()


def revisions(factory):
    with factory() as db:
        return {row.id: row.connection_revision for row in db.query(d.ModelEndpoint).all()}


def assert_linked_changed(before, after, primary):
    assert before[primary] != after[primary]
    assert before['second'] != after['second']
    assert before['unrelated'] == after['unrelated']
    assert after[primary] != after['second']


@pytest.mark.parametrize('field,value', [('provider','different-provider'), ('owner','bob'),
    ('base_url','https://different.example/v1'), ('auth_mode','different-mode'),
    ('access_token','different-access'), ('refresh_token','different-refresh')])
def test_semantic_orm_change_invalidates_all_refs(database, field, value):
    factory, auth_id, primary = database
    before = revisions(factory)
    with factory() as db:
        setattr(db.get(d.ProviderAuthSession, auth_id), field, value)
        db.commit()
    assert_linked_changed(before, revisions(factory), primary)


def test_reauthentication_same_tokens_invalidates_all_refs(database):
    factory, _, primary = database
    before = revisions(factory)
    result = routes._provision_endpoint({'access_token':'synthetic-access', 'refresh_token':'synthetic-refresh'}, 'alice')
    assert result['id'] == primary
    assert_linked_changed(before, revisions(factory), primary)


def test_label_timestamp_and_same_values_preserve_identity(database):
    factory, auth_id, _ = database
    before = revisions(factory)
    with factory() as db:
        auth = db.get(d.ProviderAuthSession, auth_id)
        auth.label = 'renamed'
        auth.last_refresh = d.utcnow_naive()
        auth.access_token = 'synthetic-access'
        db.commit()
    assert revisions(factory) == before


def test_real_oauth_refresh_preserves_all_revisions(database, monkeypatch):
    factory, auth_id, _ = database
    before = revisions(factory)
    monkeypatch.setattr(oauth, 'refresh_oauth_tokens', lambda *args: {'access_token':'renewed-access', 'refresh_token':'renewed-refresh'})
    result = oauth.resolve_runtime_credentials(auth_id, owner='alice', force_refresh=True)
    assert result['api_key'] == 'renewed-access'
    assert revisions(factory) == before
    with factory() as db:
        assert db.get(d.ProviderAuthSession, auth_id).refresh_token == 'renewed-refresh'


def test_refresh_marker_never_exempts_semantic_changes(database):
    factory, auth_id, primary = database
    before = revisions(factory)
    with factory() as db:
        auth = db.get(d.ProviderAuthSession, auth_id)
        d._mark_provider_auth_oauth_refresh(auth)
        auth.access_token = 'renewed'
        auth.base_url = 'https://different.example/v1'
        db.commit()
    assert_linked_changed(before, revisions(factory), primary)


def test_refresh_marker_consumed_after_one_flush(database):
    factory, auth_id, primary = database
    before = revisions(factory)
    with factory() as db:
        auth = db.get(d.ProviderAuthSession, auth_id)
        d._mark_provider_auth_oauth_refresh(auth)
        auth.access_token = 'renewed'
        db.flush()
        assert revisions(factory) == before
        auth.access_token = 'manual-after-refresh'
        db.commit()
    assert_linked_changed(before, revisions(factory), primary)


def test_rollback_restores_credentials_and_endpoint_revisions(database):
    factory, auth_id, _ = database
    before = revisions(factory)
    with factory() as db:
        auth = db.get(d.ProviderAuthSession, auth_id)
        auth.access_token = 'manual-replacement'
        db.flush()
        assert db.get(d.ModelEndpoint, 'second').connection_revision != before['second']
        db.rollback()
        assert auth.access_token == 'synthetic-access'
    assert revisions(factory) == before


def test_rollback_clears_unused_refresh_intent(database):
    factory, auth_id, primary = database
    before = revisions(factory)
    with factory() as db:
        auth = db.get(d.ProviderAuthSession, auth_id)
        d._mark_provider_auth_oauth_refresh(auth)
        db.rollback()
        auth.access_token = 'manual-replacement'
        db.commit()
    assert_linked_changed(before, revisions(factory), primary)


def test_old_calibration_completion_remains_historical_after_reauth(database):
    factory, _, primary = database
    captured = revisions(factory)[primary]
    identity = dict(vendor='ollama', model_id='fixture', endpoint_id=primary,
        protocol=c.NATIVE_OLLAMA_PROTOCOL, endpoint_revision=captured)
    routes._provision_endpoint({'access_token':'synthetic-access', 'refresh_token':'synthetic-refresh'}, 'alice')
    c.save_scoped_tested(**identity, tested={c.TEST_TOOL_CALLING:{'ok':True}}, announced={})
    assert c.get_effective_manifest(**identity)['tested']
    assert c.get_effective_manifest(**{**identity, 'endpoint_revision':revisions(factory)[primary]})['tested'] == {}
