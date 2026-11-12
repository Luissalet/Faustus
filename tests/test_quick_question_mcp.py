from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from core.middleware import INTERNAL_TOOL_HEADER, INTERNAL_TOOL_TOKEN
from routes import auth_routes
from src import settings
from src.builtin_mcp import _BUILTIN_SERVERS


@pytest.fixture
def quick_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setattr(settings, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setattr(auth_routes, "migrate_from_settings", lambda: None)
    settings._invalidate_caches()
    auth = MagicMock(local_only=False)
    auth.get_username_for_token.return_value = None
    router = auth_routes.setup_auth_routes(auth)
    yield {
        method: route.endpoint
        for route in router.routes if route.path == "/api/auth/settings"
        for method in route.methods
    }
    settings._invalidate_caches()


def request_with(body, *, host="127.0.0.1", internal=False):
    async def read_json():
        return body

    return SimpleNamespace(
        cookies={}, headers={INTERNAL_TOOL_HEADER: INTERNAL_TOOL_TOKEN} if internal else {},
        state=SimpleNamespace(), client=SimpleNamespace(host=host), json=read_json,
    )


@pytest.mark.asyncio
async def test_builtin_can_write_its_keys_in_account_mode(quick_settings):
    assert _BUILTIN_SERVERS["quick_question"][0] == "mcp_servers/quick_question_server.py"
    open_request = {"id": "request", "session_id": "synthetic-session"}
    result = await quick_settings["POST"](request_with(
        {"quick_question_request": open_request, "quick_question_hotkey": "Ctrl+Alt+F10"}, internal=True,
    ))
    assert result["quick_question_request"] == open_request
    assert result["quick_question_hotkey"] == "Ctrl+Alt+F10"
    assert set(result) == {"quick_question_enabled", "quick_question_hotkey", "quick_question_request"}
    assert "quick_question_request" not in await quick_settings["GET"](request_with({}))


@pytest.mark.asyncio
@pytest.mark.parametrize(("body", "host", "internal", "code"), [
    ({"quick_question_enabled": False}, "127.0.0.1", False, 403),
    ({"quick_question_enabled": False}, "192.0.2.2", True, 403),
    ({"quick_question_enabled": False, "default_model": "other"}, "127.0.0.1", True, 403),
    ({"quick_question_enabled": "false"}, "127.0.0.1", True, 400),
])
async def test_quick_settings_write_preserves_existing_scope(quick_settings, body, host, internal, code):
    with pytest.raises(HTTPException) as error:
        await quick_settings["POST"](request_with(body, host=host, internal=internal))
    assert error.value.status_code == code
    assert settings.load_settings()["quick_question_enabled"] is True
