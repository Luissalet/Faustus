"""ARCH-01 — negotiated wire version for the chat SSE protocol.

`src/api_version.py` is the whole negotiation: a client that never heard of
the scheme (no header — every request before this lot, and every existing
test in this repo) must keep working exactly as before; a client that
IDENTIFIES as older than MIN_CLIENT_VERSION must be told so in a message it
can act on, instead of being handed a stream it cannot parse.
"""
from src import api_version


def test_no_header_is_treated_as_compatible():
    """Every existing test and every request sent before this lot carries no
    X-Faustus-Client-Version header at all. FAILS if a future change makes
    absence of the header mean "reject" instead of "predates the scheme" —
    that would 426 every client shipped before this lot landed."""
    assert api_version.is_supported(None) is True
    assert api_version.is_supported("") is True
    assert api_version.is_supported("   ") is True


def test_current_version_is_supported():
    assert api_version.is_supported(api_version.API_VERSION) is True


def test_a_version_at_the_floor_is_supported():
    assert api_version.is_supported(api_version.MIN_CLIENT_VERSION) is True


def test_a_version_below_the_floor_is_rejected():
    assert api_version.is_supported("1.9") is False
    assert api_version.is_supported("0.1") is False


def test_a_higher_client_version_is_supported():
    """Negotiation is a floor, not an exact match — a newer client than the
    server is not the incompatibility case this module exists to catch."""
    assert api_version.is_supported("99.0") is True


def test_garbage_is_rejected_rather_than_silently_let_through():
    for bogus in ("nonsense", "2.0-beta", "2..0", "..", "v2"):
        assert api_version.is_supported(bogus) is False, bogus


def test_upgrade_required_detail_names_the_versions_and_is_actionable():
    detail = api_version.upgrade_required_detail("1.0")
    assert "1.0" in detail
    assert api_version.MIN_CLIENT_VERSION in detail
    assert "reload" in detail.lower()


def test_upgrade_required_detail_handles_a_missing_version_gracefully():
    detail = api_version.upgrade_required_detail(None)
    assert "unknown" in detail.lower()


def test_version_constants_are_dotted_numeric():
    """Sanity: both constants must themselves parse — a MIN_CLIENT_VERSION
    that does not parse would make is_supported() reject every client,
    compatible or not."""
    assert api_version._parse(api_version.API_VERSION) is not None
    assert api_version._parse(api_version.MIN_CLIENT_VERSION) is not None
