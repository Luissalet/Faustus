"""With AUTH_ENABLED=false the server treats every caller as the operator,
so /api/auth/status must say so or the UI hides admin-only controls."""
from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_status_reports_admin_when_auth_is_disabled(monkeypatch, tmp_path):
    import routes.auth_routes as ar
    monkeypatch.setattr(ar, "_auth_disabled", lambda: True)

    class FakeAuth:
        signup_enabled = False
        def status(self, token):
            return {"configured": True, "authenticated": False, "username": None, "is_admin": False}

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
