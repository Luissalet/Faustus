"""H22 refresh cannot overwrite a newer credential generation. Offline OAuth."""
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
import pytest
from sqlalchemy.orm.exc import StaleDataError
from core import database as d
from src import chatgpt_subscription as oauth
from routes import chatgpt_subscription_routes as routes
from tests.test_provider_auth_connection_revision import database, revisions


@pytest.mark.parametrize('same_tokens', [False, True])
def test_late_refresh_cannot_overwrite_real_reauthentication(database, monkeypatch, same_tokens):
    factory, auth_id, _ = database
    entered, release = threading.Event(), threading.Event()
    def refresh(*args):
        entered.set()
        assert release.wait(5)
        return {'access_token':'stale-refresh-access', 'refresh_token':'stale-refresh-token'}
    monkeypatch.setattr(oauth, 'refresh_oauth_tokens', refresh)
    expected_access = 'synthetic-access' if same_tokens else 'reauth-access'
    expected_refresh = 'synthetic-refresh' if same_tokens else 'reauth-refresh'
    with ThreadPoolExecutor() as pool:
        task = pool.submit(oauth.resolve_runtime_credentials, auth_id, 'alice', force_refresh=True)
        try:
            assert entered.wait(5)
            routes._provision_endpoint({'access_token':expected_access, 'refresh_token':expected_refresh}, 'alice')
            confirmed_endpoint_revisions = revisions(factory)
            with factory() as db:
                confirmed_version = db.get(d.ProviderAuthSession, auth_id).credential_revision
        finally:
            release.set()
        with pytest.raises(oauth.ChatGPTSubscriptionCredentialConflict) as error:
            task.result(timeout=5)
    assert oauth.to_http_exception(error.value).status_code == 409
    with factory() as db:
        auth = db.get(d.ProviderAuthSession, auth_id)
        assert auth.access_token == expected_access
        assert auth.refresh_token == expected_refresh
        assert auth.credential_revision == confirmed_version
    assert revisions(factory) == confirmed_endpoint_revisions


def test_normal_refresh_versions_auth_but_preserves_endpoint_identity(database, monkeypatch):
    factory, auth_id, _ = database
    before = revisions(factory)
    with factory() as db:
        version = db.get(d.ProviderAuthSession, auth_id).credential_revision
    monkeypatch.setattr(oauth, 'refresh_oauth_tokens', lambda *a: {'access_token':'renewed', 'refresh_token':'renewed-refresh'})
    assert oauth.resolve_runtime_credentials(auth_id, 'alice', force_refresh=True)['api_key'] == 'renewed'
    with factory() as db:
        assert db.get(d.ProviderAuthSession, auth_id).credential_revision != version
    assert revisions(factory) == before


def test_rollback_preserves_credential_version(database):
    factory, auth_id, _ = database
    before = revisions(factory)
    with factory() as db:
        auth = db.get(d.ProviderAuthSession, auth_id)
        version = auth.credential_revision
        auth.access_token = 'replacement'
        db.flush()
        assert auth.credential_revision != version
        db.rollback()
        assert auth.credential_revision == version
    assert revisions(factory) == before


@pytest.mark.parametrize('provider', ['chatgpt-subscription', 'synthetic-other-provider'])
def test_mapper_cas_applies_to_all_provider_rows(database, provider):
    factory, auth_id, _ = database
    with factory() as db:
        db.get(d.ProviderAuthSession, auth_id).provider = provider
        db.commit()
    with factory() as old, factory() as new:
        old_row = old.get(d.ProviderAuthSession, auth_id)
        new_row = new.get(d.ProviderAuthSession, auth_id)
        new_row.access_token = 'new-generation'
        new.commit()
        old_row.refresh_token = 'stale-generation'
        with pytest.raises(StaleDataError):
            old.commit()
        old.rollback()
    with factory() as db:
        assert db.get(d.ProviderAuthSession, auth_id).access_token == 'new-generation'
        assert db.get(d.ProviderAuthSession, auth_id).refresh_token == 'synthetic-refresh'


def test_reauthentication_force_version_with_identical_all_fields(database):
    factory, auth_id, _ = database
    with factory() as db:
        auth = db.get(d.ProviderAuthSession, auth_id)
        version = auth.credential_revision
        d._mark_provider_auth_reauthenticated(auth)
        db.commit()
        assert auth.credential_revision != version


def test_migration_idempotent_and_distinct_per_row(tmp_path, monkeypatch):
    path = tmp_path / 'legacy.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE provider_auth_sessions (id TEXT PRIMARY KEY, provider TEXT)')
        db.executemany('INSERT INTO provider_auth_sessions VALUES (?, ?)', [('a','one'),('b','two')])
    monkeypatch.setattr(d, 'DATABASE_URL', 'sqlite:///' + str(path))
    d._migrate_add_provider_credential_revision()
    with sqlite3.connect(path) as db:
        before = db.execute('SELECT * FROM provider_auth_sessions ORDER BY id').fetchall()
    d._migrate_add_provider_credential_revision()
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT * FROM provider_auth_sessions ORDER BY id').fetchall() == before
    assert before[0][2] != before[1][2]
    assert all(len(row[2]) == 32 for row in before)
