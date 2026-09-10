"""OPS-06 - extensible APIs and client contract.

Extends `src/api_version.py` (ARCH-01's negotiated-version module, reused
verbatim, not duplicated): a deprecation registry with RFC-8594-shaped
headers, a soft adaptation notice for a client that is old but still
supported, and an OpenAPI info-extension. Nothing here touches
`routes/chat_routes.py` or `app.py` (not owned by this lot) — see the lot's
final report for the exact wiring a future change would add there.
"""
from __future__ import annotations

from src import api_version


# ── deprecation registry ────────────────────────────────────────────────


def test_no_deprecations_are_active_yet():
    """COMUN rule 3 (no capability lost): this lot introduces the registry
    with nothing scheduled for removal."""
    assert api_version.active_deprecations() == []


def test_deprecation_headers_are_empty_for_an_unlisted_route():
    assert api_version.deprecation_headers("/api/whatever") == {}


def test_deprecation_headers_reflect_a_registered_entry(monkeypatch):
    fake = (
        {"route": "/api/old-thing", "since": "2.0", "sunset": "2027-01-01",
         "replacement": "/api/new-thing"},
    )
    monkeypatch.setattr(api_version, "DEPRECATIONS", fake)
    headers = api_version.deprecation_headers("/api/old-thing")
    assert headers["Deprecation"] == "true"
    assert headers["Sunset"] == "2027-01-01"
    assert "/api/new-thing" in headers["Link"]
    assert api_version.deprecation_headers("/api/other") == {}


def test_deprecation_headers_omit_sunset_and_link_when_unset(monkeypatch):
    fake = ({"route": "/api/old-thing", "since": "2.0"},)
    monkeypatch.setattr(api_version, "DEPRECATIONS", fake)
    headers = api_version.deprecation_headers("/api/old-thing")
    assert headers == {"Deprecation": "true"}


# ── adaptation notice: the "not a silent failure" half of OPS-06 ───────


def test_no_notice_for_a_client_with_no_declared_version():
    """Predates the scheme entirely -- nothing to compare against, and
    definitely not a notice implying something is wrong."""
    assert api_version.client_adaptation_notice(None) is None
    assert api_version.client_adaptation_notice("") is None


def test_no_notice_for_a_client_on_the_current_version():
    assert api_version.client_adaptation_notice(api_version.API_VERSION) is None


def test_no_notice_for_a_client_already_rejected_by_is_supported():
    """A client below MIN_CLIENT_VERSION gets the 426 path, not a soft
    notice pretending it will just work."""
    assert api_version.is_supported("0.1") is False
    assert api_version.client_adaptation_notice("0.1") is None


def test_a_supported_but_older_client_gets_an_actionable_notice(monkeypatch):
    """The core OPS-06 acceptance behaviour: a client between the floor and
    current gets told, in a message it can act on, instead of silence until
    something it cannot parse breaks it mid-request."""
    monkeypatch.setattr(api_version, "API_VERSION", "2.5")
    monkeypatch.setattr(api_version, "MIN_CLIENT_VERSION", "2.0")
    notice = api_version.client_adaptation_notice("2.1")
    assert notice is not None
    assert "2.1" in notice
    assert "2.5" in notice


def test_a_newer_client_than_the_server_gets_no_notice(monkeypatch):
    monkeypatch.setattr(api_version, "API_VERSION", "2.0")
    monkeypatch.setattr(api_version, "MIN_CLIENT_VERSION", "2.0")
    assert api_version.client_adaptation_notice("99.0") is None


# ── OpenAPI extension ────────────────────────────────────────────────────


def test_openapi_extension_carries_the_negotiated_contract():
    ext = api_version.openapi_version_extension()
    assert ext["x-api-version"] == api_version.API_VERSION
    assert ext["x-min-client-version"] == api_version.MIN_CLIENT_VERSION
    assert ext["x-api-version-header"] == api_version.API_VERSION_HEADER
    assert ext["x-client-version-header"] == api_version.CLIENT_VERSION_HEADER
    assert ext["x-deprecations"] == []


def test_openapi_extension_reflects_registered_deprecations(monkeypatch):
    fake = ({"route": "/api/old-thing", "since": "2.0"},)
    monkeypatch.setattr(api_version, "DEPRECATIONS", fake)
    ext = api_version.openapi_version_extension()
    assert ext["x-deprecations"] == [{"route": "/api/old-thing", "since": "2.0"}]
    # A defensive copy -- mutating the returned list must not corrupt the
    # module's own registry for the next caller.
    ext["x-deprecations"].append({"route": "injected"})
    assert api_version.active_deprecations() == [{"route": "/api/old-thing", "since": "2.0"}]
