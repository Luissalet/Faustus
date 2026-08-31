"""The research preset is reachable from the app (FAUSTUS)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROUTES = (ROOT / "routes" / "research" / "research_routes.py").read_text(encoding="utf-8")
SLASH = (ROOT / "static" / "js" / "slashCommands.js").read_text(encoding="utf-8")


def test_endpoints_exist_and_are_admin_only():
    for route in ("/api/research/preset", "/api/research/preset/apply"):
        assert f'"{route}"' in ROUTES
    idx = ROUTES.index("/api/research/preset")
    assert "require_admin(request)" in ROUTES[idx:idx + 600]


def test_the_search_probe_never_invents_a_blocker():
    """An unavailable probe must read as 'unknown', not as 'unreachable'."""
    idx = ROUTES.index("searxng_ok")
    assert "searxng_ok = None" in ROUTES[idx:idx + 900]


def test_slash_command_registered_without_stealing_slash_research():
    assert "handler: _cmdResearchFit" in SLASH
    assert "researchfit:" in SLASH
    idx = SLASH.index("handler: _cmdResearchFit")
    assert "'research'" not in SLASH[max(0, idx - 400):idx]


def test_fixes_are_opt_in():
    """Switching someone's search provider is not a side effect of a preset."""
    assert "include_fixes" in ROUTES and "include_fixes" in SLASH
