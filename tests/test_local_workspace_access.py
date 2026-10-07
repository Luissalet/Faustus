from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_local_access_preserves_credential_bytes_and_owner_attribution(tmp_path):
    from core.local_auth import LocalWorkspaceAccess
    from src.owner_identity import DEFAULT_LOCAL_OWNER, effective_storage_owner

    credential_file = tmp_path / 'auth.json'
    original = b'{"version":1,"users":{"existing-user":{"marker":"keep"}}}\n'
    credential_file.write_bytes(original)
    access = LocalWorkspaceAccess()
    assert access.status('stale-browser-cookie')['username'] is None
    assert credential_file.read_bytes() == original
    assert effective_storage_owner(None, auth_is_disabled=True) == DEFAULT_LOCAL_OWNER
    assert effective_storage_owner('existing-user', auth_is_disabled=True) == 'existing-user'


def test_local_workspace_does_not_read_or_create_credentials(monkeypatch):
    from core.local_auth import LocalWorkspaceAccess
    def forbidden(*args, **kwargs):
        raise AssertionError('Local workspace attempted credential IO')
    monkeypatch.setattr('builtins.open', forbidden)
    access = LocalWorkspaceAccess()
    assert access.status('stale-browser-cookie')['auth_disabled']
    assert access.list_users() == [] and access.users == {}
    assert not access.validate_token('stale-browser-cookie')


def test_local_routes_have_settings_access_without_account_operations(monkeypatch):
    import routes.auth_routes as routes
    from core.local_auth import LocalWorkspaceAccess
    monkeypatch.setenv('AUTH_ENABLED','false')
    monkeypatch.setattr(routes, 'migrate_from_settings', lambda: None)
    monkeypatch.setattr(routes, '_load_settings', lambda: {})
    app=FastAPI();app.include_router(routes.setup_auth_routes(LocalWorkspaceAccess()))
    client=TestClient(app)
    client.cookies.set(routes.SESSION_COOKIE, 'stale-browser-cookie')
    status_response = client.get('/api/auth/status')
    status=status_response.json()
    assert status['auth_disabled'] and status['authenticated']
    assert status['username'] is None and status_response.headers.get('set-cookie') is None
    assert client.get('/api/auth/users').status_code==404
    assert client.post('/api/auth/setup',json={'username':'unnecessary','password':'12345678'}).status_code==404
    assert client.post('/api/auth/signup-toggle').status_code == 404
    assert client.put('/api/auth/open-signup',json={'enabled':True}).status_code == 404
    assert client.get('/api/auth/settings').status_code==200
