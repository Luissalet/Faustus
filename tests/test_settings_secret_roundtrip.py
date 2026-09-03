"""Saving the settings form must never store a mask over a real credential.

`src/settings_scrub.py` blanks secret-shaped values so `GET /api/auth/settings`
— which is auth-exempt, because the login page reads keybinds from it — cannot
leak an API key. The danger on the way back is silent: if the form that
received `brave_api_key: ""` posted the whole object again, the stored key
would be destroyed and nobody would find out until a search failed.

Faustus is safe today because of two facts that belong together, and neither is
obvious from the code that implements it:

  * the caller who receives a mask is exactly the caller who cannot write —
    admins read the settings unscrubbed, and POST is admin-only;
  * POST is a patch, not a replace: a key absent from the body is untouched.

These pin both. If the GET is ever hardened to scrub for admins too — a very
reasonable-looking change — `test_an_admin_round_trip_preserves_the_secret`
fails, which is the point: the destructive version of that change must not be
able to land quietly.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from src.settings_scrub import scrub_settings

SECRET = "brave-real-key-do-not-lose"


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
    """The two endpoints over an in-memory settings store."""
    import routes.auth_routes as auth_routes

    stored = {"brave_api_key": SECRET, "search_provider": "brave", "tts_enabled": True}
    monkeypatch.setattr(auth_routes, "_load_settings", lambda: dict(stored))
    monkeypatch.setattr(auth_routes, "_save_settings", lambda new: stored.clear() or stored.update(new))

    auth_manager, get_settings, set_settings = _settings_endpoints()
    auth_manager.get_username_for_token.return_value = "luis"

    def as_admin(is_admin: bool):
        auth_manager.is_admin.return_value = is_admin

    request = SimpleNamespace(cookies={}, client=SimpleNamespace(host="127.0.0.1"))
    return SimpleNamespace(
        stored=stored, request=request, as_admin=as_admin,
        get=get_settings, post=set_settings,
    )


async def _post(settings_api, body):
    settings_api.request.json = lambda: _async(body)
    return await settings_api.post(settings_api.request)


async def _async(value):
    return value


@pytest.mark.asyncio
async def test_an_admin_round_trip_preserves_the_secret(settings_api):
    """Read the settings, save them straight back: the key must survive."""
    settings_api.as_admin(True)
    read_back = await settings_api.get(settings_api.request)

    assert read_back["brave_api_key"] == SECRET, "an admin form must not be handed a mask"

    await _post(settings_api, dict(read_back))
    assert settings_api.stored["brave_api_key"] == SECRET


@pytest.mark.asyncio
async def test_the_caller_who_receives_a_mask_cannot_write_it_back(settings_api):
    """The whole guard in one test: masked read, refused write, key intact."""
    settings_api.as_admin(False)
    read_back = await settings_api.get(settings_api.request)

    assert read_back["brave_api_key"] == ""
    assert read_back["search_provider"] == "brave"      # non-secrets survive the scrub

    with pytest.raises(HTTPException) as refused:
        await _post(settings_api, dict(read_back))

    assert refused.value.status_code == 403
    assert settings_api.stored["brave_api_key"] == SECRET


@pytest.mark.asyncio
async def test_a_key_absent_from_the_body_is_left_alone(settings_api):
    """POST is a patch: saving one panel cannot blank another panel's secret."""
    settings_api.as_admin(True)
    await _post(settings_api, {"search_provider": "tavily"})

    assert settings_api.stored["search_provider"] == "tavily"
    assert settings_api.stored["brave_api_key"] == SECRET


def test_the_scrub_blanks_the_secret_it_is_asked_about():
    """The premise the tests above rest on, stated once."""
    assert scrub_settings({"brave_api_key": SECRET})["brave_api_key"] == ""
