"""The search backend is started, not assumed.

08-09-2026: a research planned, wrote seven queries and read nothing because
its own SearXNG container was down. Nothing was broken; nobody had started it.
These tests pin what the preflight may and may not do — above all that it only
ever starts a service this repo itself ships, and never touches a URL that is
not on this machine.
"""
import asyncio

import pytest

from services.search import appliance

LOCAL = "http://localhost:8080"
REMOTE = "https://searx.example.org"


@pytest.fixture
def no_settings(monkeypatch):
    monkeypatch.setattr(appliance, "_settings", lambda: {})


@pytest.fixture
def never_runs(monkeypatch):
    """Any docker call is a failure of the test's premise."""
    async def _boom(*argv, **kwargs):
        raise AssertionError(f"should not have run: {argv}")
    monkeypatch.setattr(appliance, "_run", _boom)
    return _boom


def _up(monkeypatch, reachable: bool):
    async def _probe(url, timeout=3.0):
        return reachable
    monkeypatch.setattr(appliance, "_reachable", _probe)


def test_a_backend_that_answers_is_left_alone(monkeypatch, no_settings, never_runs):
    monkeypatch.setattr(appliance, "provider_url", lambda p: LOCAL)
    _up(monkeypatch, True)

    assert asyncio.run(appliance.ensure_backend("searxng")) == (True, "")


def test_a_provider_with_no_url_is_not_ours_to_start(monkeypatch, no_settings, never_runs):
    monkeypatch.setattr(appliance, "provider_url", lambda p: "")

    assert asyncio.run(appliance.ensure_backend("tavily")) == (True, "")


def test_someone_else_s_server_is_never_started(monkeypatch, no_settings, never_runs):
    monkeypatch.setattr(appliance, "provider_url", lambda p: REMOTE)
    _up(monkeypatch, False)

    ready, reason = asyncio.run(appliance.ensure_backend("searxng"))
    assert ready is False
    assert REMOTE in reason


def test_autostart_off_reports_instead_of_starting(monkeypatch, never_runs):
    monkeypatch.setattr(appliance, "_settings", lambda: {"search_autostart": False})
    monkeypatch.setattr(appliance, "provider_url", lambda p: LOCAL)
    _up(monkeypatch, False)

    ready, reason = asyncio.run(appliance.ensure_backend("searxng"))
    assert ready is False
    assert "autostart is off" in reason


def test_a_service_this_repo_does_not_ship_is_not_invented(monkeypatch, no_settings, never_runs):
    # Firecrawl is not in the compose file: guessing an image would start
    # something the admin never chose.
    monkeypatch.setattr(appliance, "provider_url", lambda p: "http://localhost:3002")
    _up(monkeypatch, False)

    ready, reason = asyncio.run(appliance.ensure_backend("firecrawl"))
    assert ready is False
    assert "start on its own" in reason


def test_it_starts_the_container_and_waits_for_it(monkeypatch, no_settings):
    monkeypatch.setattr(appliance, "provider_url", lambda p: LOCAL)
    monkeypatch.setattr(appliance.shutil, "which", lambda name: r"C:\docker.exe")
    monkeypatch.setattr(appliance.os.path, "exists", lambda p: True)

    seen = []
    # Down, down, then up: the wait loop has to survive a backend that needs a
    # moment after the container is created.
    answers = iter([False, False, True])

    async def _probe(url, timeout=3.0):
        try:
            return next(answers)
        except StopIteration:
            return True

    async def _run(*argv, **kwargs):
        seen.append(argv)
        return 0, ""

    monkeypatch.setattr(appliance, "_reachable", _probe)
    monkeypatch.setattr(appliance, "_run", _run)
    monkeypatch.setattr(asyncio, "sleep", lambda *_a, **_k: asyncio.sleep(0))

    events = []
    ready, reason = asyncio.run(
        appliance.ensure_backend("searxng", on_progress=events.append)
    )

    assert (ready, reason) == (True, "")
    assert seen[0][:2] == ("docker", "version")          # is the engine up?
    assert seen[1][:2] == ("docker", "compose")          # then start the service
    assert seen[1][-3:] == ("up", "-d", "searxng")       # …and only that service
    assert [e["phase"] for e in events] == ["starting_search", "starting_search"]


def test_a_container_that_never_answers_is_reported_not_raised(monkeypatch, no_settings):
    monkeypatch.setattr(appliance, "provider_url", lambda p: LOCAL)
    monkeypatch.setattr(appliance.shutil, "which", lambda name: r"C:\docker.exe")
    monkeypatch.setattr(appliance.os.path, "exists", lambda p: True)
    monkeypatch.setattr(appliance, "_settings",
                        lambda: {"search_autostart_timeout_seconds": 30})
    _up(monkeypatch, False)

    async def _run(*argv, **kwargs):
        return 0, ""

    monkeypatch.setattr(appliance, "_run", _run)
    monkeypatch.setattr(asyncio, "sleep", lambda *_a, **_k: asyncio.sleep(0))

    ready, reason = asyncio.run(appliance.ensure_backend("searxng"))
    assert ready is False
    assert "did not answer" in reason
