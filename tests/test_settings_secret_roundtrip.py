"""Saving the settings form must never store a mask over a real credential.

`src/settings_scrub.py` blanks secret-shaped values so `GET /api/auth/settings`
— which is auth-exempt, because the login page reads keybinds from it — cannot
leak an API key. The danger on the way back is silent: if the form that
received `brave_api_key: ""` posted the whole object again, the stored key
would be destroyed and nobody would find out until a search failed.

Credentials are write-only: admins read a mask too (the last four characters,
so two keys can be told apart), and POST swaps any mask sent back for the
stored value. That makes the naive "read, then save the whole object" safe for
every caller, and the key never travels back to a browser after it was typed.

The guards pinned here:

  * an admin read is masked, and saving it straight back keeps the key;
  * the caller who receives a blank (non-admin) still cannot write at all;
  * POST is a patch: a key absent from the body is untouched;
  * a new key replaces the old one, and an empty string clears it;
  * the swap works inside nested values too.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from src.settings_scrub import MASK, mask_secrets, restore_masked, scrub_settings

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

    assert read_back["brave_api_key"] != SECRET, "the key must not travel back to the browser"
    assert read_back["brave_api_key"] == MASK + SECRET[-4:]
    assert read_back["search_provider"] == "brave"

    answer = await _post(settings_api, dict(read_back))
    assert settings_api.stored["brave_api_key"] == SECRET
    assert answer["brave_api_key"] == MASK + SECRET[-4:], "the POST answer is masked too"


@pytest.mark.asyncio
async def test_a_new_key_replaces_and_an_empty_one_clears(settings_api):
    settings_api.as_admin(True)
    await _post(settings_api, {"brave_api_key": "another-real-key-1234"})
    assert settings_api.stored["brave_api_key"] == "another-real-key-1234"

    await _post(settings_api, {"brave_api_key": ""})
    assert settings_api.stored["brave_api_key"] == ""


@pytest.mark.asyncio
async def test_an_edit_around_the_mask_never_stores_the_mask(settings_api):
    settings_api.as_admin(True)
    await _post(settings_api, {"brave_api_key": MASK + "lose" + "x"})
    assert settings_api.stored["brave_api_key"] == SECRET


def test_nested_credentials_are_masked_and_restored():
    stored = {"external_runtimes_herdr": {"url": "http://h", "token": "tok-abcdefghijkl"}}
    shown = mask_secrets(stored)
    assert shown["external_runtimes_herdr"] == {"url": "http://h", "token": MASK + "ijkl"}
    back = restore_masked("external_runtimes_herdr", {"url": "http://h2", "token": shown["external_runtimes_herdr"]["token"]},
                          stored["external_runtimes_herdr"])
    assert back == {"url": "http://h2", "token": "tok-abcdefghijkl"}


def test_short_keys_show_no_tail_and_ids_stay_readable():
    shown = mask_secrets({"serper_api_key": "short", "reminder_webhook_integration_id": "int-42"})
    assert shown["serper_api_key"] == MASK
    assert shown["reminder_webhook_integration_id"] == "int-42", "admins pick this id from a list"


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
