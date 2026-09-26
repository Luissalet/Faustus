"""scripts/ui_narrow_check.py: the route list parser (the browser part runs
against a live instance)."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("ui_narrow_check", ROOT / "scripts" / "ui_narrow_check.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def test_routes_are_normalised_and_deduplicated():
    assert mod.parse_routes(" /studio, settings,,/studio ") == ["/studio", "/settings"]
    assert mod.parse_routes("") == []


def test_the_default_list_covers_settings_and_the_main_screens():
    routes = mod.parse_routes(mod.DEFAULT_ROUTES)
    assert "/settings" in routes and "/studio" in routes and "/cookbook" in routes
    assert len(routes) == len(set(routes))


def test_the_check_is_read_only_by_construction():
    src = (ROOT / "scripts" / "ui_narrow_check.py").read_text(encoding="utf-8")
    assert 'r.request.method != "GET"' in src and "r.fulfill(" in src
