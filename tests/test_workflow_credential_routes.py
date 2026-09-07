import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from cryptography.fernet import Fernet

from src import secret_storage
from src.workflows import credentials
from routes.workflow_credentials_routes import setup_workflow_credentials_routes


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setattr(credentials, "STORE_PATH", tmp_path / "credentials.json")
    monkeypatch.setattr(secret_storage, "_fernet", Fernet(Fernet.generate_key()))
    app = FastAPI()
    @app.middleware("http")
    async def identity(request, call_next):
        request.state.current_user = "alice"
        return await call_next(request)
    app.include_router(setup_workflow_credentials_routes(), prefix="/api/workflows")
    with TestClient(app) as result:
        yield result


def test_human_can_manage_metadata_but_no_read_route_returns_secret(client):
    response = client.put("/api/workflows/credentials/provider", json={"value": "synthetic-only-secret"})
    assert response.status_code == 200
    revision = response.json()["revision"]
    assert "synthetic-only-secret" not in response.text
    listed = client.get("/api/workflows/credentials")
    assert listed.json()["credentials"] == [{"name": "provider", "revision": revision}]
    assert client.get("/api/workflows/credentials/provider").status_code == 405
    assert client.delete("/api/workflows/credentials/provider").status_code == 400
    result = client.request("DELETE", "/api/workflows/credentials/provider", json={"expected_revision": revision})
    assert result.json() == {"removed": True}


def test_tool_token_cannot_create_rotate_delete_or_enumerate_credentials(client):
    from core.middleware import INTERNAL_TOOL_HEADER, INTERNAL_TOOL_TOKEN
    headers = {INTERNAL_TOOL_HEADER: INTERNAL_TOOL_TOKEN}
    for method, path in [("GET", ""), ("PUT", "/provider"), ("DELETE", "/provider")]:
        assert client.request(method, "/api/workflows/credentials" + path, headers=headers,
                              json={"value": "synthetic-only"}).status_code == 403
    assert not credentials.STORE_PATH.exists()


def test_owner_cannot_be_spoofed_in_body(client):
    result = client.put("/api/workflows/credentials/provider", json={"value": "secret", "owner": "bob"})
    assert result.status_code == 400
    assert not credentials.STORE_PATH.exists()


def test_oversized_input_and_invalid_secret_do_not_echo_value(client):
    response = client.put("/api/workflows/credentials/provider", json={"value": "x" * 140000})
    assert response.status_code == 413
    result = client.put("/api/workflows/credentials/provider", json={"value": "secret\nmultiline"})
    assert result.status_code == 400
    assert "secret\nmultiline" not in result.text
