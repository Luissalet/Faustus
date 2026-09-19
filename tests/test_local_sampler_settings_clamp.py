"""POST /api/auth/settings must clamp the local sampler floors the same way
it already clamps agent_max_rounds/agent_max_tool_calls: `local_temperature_default`
and `local_top_p_default` (floats), `local_top_k_default` (int) — see
routes/auth_routes.py's `_FLOAT_RANGES`/`_INT_RANGES`. 0/empty is valid for
all three (means "do not send") and must survive unclamped.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


def _settings_endpoints():
    from routes.auth_routes import setup_auth_routes

    auth_manager = MagicMock()
    router = setup_auth_routes(auth_manager)
    found = {}
    for route in router.routes:
        if getattr(route, "path", "") == "/api/auth/settings":
            for method in getattr(route, "methods", set()):
                found[method] = route.endpoint
    assert {"GET", "POST"} <= set(found), "settings routes not registered"
    return auth_manager, found["GET"], found["POST"]


@pytest.fixture
def settings_api(monkeypatch):
    import routes.auth_routes as auth_routes
    from src.settings import DEFAULT_SETTINGS

    stored = dict(DEFAULT_SETTINGS)
    monkeypatch.setattr(auth_routes, "_load_settings", lambda: dict(stored))
    monkeypatch.setattr(auth_routes, "_save_settings", lambda new: stored.clear() or stored.update(new))

    auth_manager, get_settings, set_settings = _settings_endpoints()
    auth_manager.get_username_for_token.return_value = "luis"
    auth_manager.is_admin.return_value = True

    request = SimpleNamespace(cookies={}, client=SimpleNamespace(host="127.0.0.1"))
    return SimpleNamespace(stored=stored, request=request, post=set_settings)


async def _post(settings_api, body):
    settings_api.request.json = lambda: _async(body)
    return await settings_api.post(settings_api.request)


async def _async(value):
    return value


@pytest.mark.asyncio
async def test_temperature_default_is_clamped_to_its_range(settings_api):
    await _post(settings_api, {"local_temperature_default": 5.0})
    assert settings_api.stored["local_temperature_default"] == 2.0

    await _post(settings_api, {"local_temperature_default": -1.0})
    assert settings_api.stored["local_temperature_default"] == 0.0


@pytest.mark.asyncio
async def test_top_p_default_is_clamped_to_its_range(settings_api):
    await _post(settings_api, {"local_top_p_default": 3.0})
    assert settings_api.stored["local_top_p_default"] == 1.0


@pytest.mark.asyncio
async def test_top_k_default_is_clamped_and_coerced_to_int(settings_api):
    await _post(settings_api, {"local_top_k_default": 999})
    assert settings_api.stored["local_top_k_default"] == 200
    assert isinstance(settings_api.stored["local_top_k_default"], int)


@pytest.mark.asyncio
async def test_zero_is_accepted_unclamped_for_all_three(settings_api):
    """0 means 'do not send' (src.llm_core._local_sampler_default_optional) —
    it must round-trip as 0, not get pushed up to a minimum."""
    await _post(settings_api, {
        "local_temperature_default": 0,
        "local_top_p_default": 0,
        "local_top_k_default": 0,
    })
    assert settings_api.stored["local_temperature_default"] == 0.0
    assert settings_api.stored["local_top_p_default"] == 0.0
    assert settings_api.stored["local_top_k_default"] == 0


@pytest.mark.asyncio
async def test_a_non_numeric_value_is_rejected(settings_api):
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        await _post(settings_api, {"local_temperature_default": "warm"})
