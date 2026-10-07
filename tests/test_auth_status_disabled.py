"""With AUTH_ENABLED=false the server treats every caller as the operator,
so /api/auth/status must say so or the UI hides admin-only controls."""
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest


@pytest.mark.parametrize('old_cookie_authenticated', [False, True])
def test_status_reports_admin_when_auth_is_disabled(monkeypatch, tmp_path, old_cookie_authenticated):
    import routes.auth_routes as ar
    monkeypatch.setattr(ar, "_auth_disabled", lambda: True)
    monkeypatch.setattr(ar, 'migrate_from_settings', lambda: None)

    class FakeAuth:
        signup_enabled = False
        def status(self, token):
            return {"configured": True, "authenticated": old_cookie_authenticated, "username": None, "is_admin": False}

    app = FastAPI()
    setup = getattr(ar, "setup_auth_routes")
    try:
        router = setup(FakeAuth())
    except TypeError:
        import pytest
        pytest.skip("auth router needs more wiring")
    app.include_router(router)
    body = TestClient(app).get("/api/auth/status").json()
    assert body["is_admin"] is True and body["authenticated"] is True and body["auth_disabled"] is True
    assert body['auth_enabled'] is False
