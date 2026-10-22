"""The Tools screen can switch the integrated browser, and never drops what it does not list.

Seen live (22-10): ``disabled_tools`` held ``builtin_browser`` since at least
01-10, so every turn lost the 25 browser tools and an agent asked for a page
capture concluded "No browser tool available". The Tools screen only listed
TOOL_TAGS, so the browser could not be switched back on from the interface,
and saving that screen replaced the whole list (dropping disabled MCP servers).
"""
import sys
import types
from unittest.mock import MagicMock

from tests.helpers.import_state import clear_fake_endpoint_resolver_modules, preserve_import_state

with preserve_import_state("core.database", "src.database", "core.session_manager", "routes.model_routes"):
    clear_fake_endpoint_resolver_modules()
    if "core.database" not in sys.modules:
        _core_db = types.ModuleType("core.database")
        for _name in [
            "SessionLocal", "ModelEndpoint", "Session", "ChatMessage", "Document",
            "DocumentVersion", "GalleryImage", "GalleryAlbum", "Note",
            "CalendarCal", "CalendarEvent", "ScheduledTask", "TaskRun",
            "McpServer", "ProviderAuthSession", "Base",
        ]:
            setattr(_core_db, _name, MagicMock())
        _core_db.utcnow_naive = MagicMock()
        sys.modules["core.database"] = _core_db
    import routes.model_routes as model_routes


def _endpoints():
    router = model_routes.setup_model_routes(MagicMock())
    found = {}
    for route in router.routes:
        if getattr(route, "path", "") == "/api/tools":
            for method in route.methods:
                found[method] = route.endpoint
    return found


def _store(monkeypatch, disabled):
    state = {"disabled_tools": list(disabled)}
    monkeypatch.setattr(model_routes, "_load_settings", lambda: dict(state))

    def save(s):
        state.clear()
        state.update(s)

    monkeypatch.setattr(model_routes, "_save_settings", save)
    monkeypatch.setattr(model_routes, "require_admin", lambda request: None)
    return state


def test_the_browser_is_listed_as_a_switch_but_not_as_a_tool_tag(monkeypatch):
    _store(monkeypatch, ["builtin_browser"])
    out = _endpoints()["GET"]()
    assert {"id": "builtin_browser", "enabled": False} in out["switches"]
    assert all(t["id"] != "builtin_browser" for t in out["tools"])   # fences read `tools` as tool tags
    from src.agent_tools import TOOL_TAGS
    assert [t["id"] for t in out["tools"]] == sorted(TOOL_TAGS)


def test_switching_the_browser_on_keeps_disabled_mcp_servers(monkeypatch):
    state = _store(monkeypatch, ["41b3562a", "builtin_browser", "bash", "mcp__other__thing"])
    post = _endpoints()["POST"]
    import inspect
    # ToolsUpdate lives inside the route factory: take it from the signature.
    model = inspect.signature(post).parameters["body"].annotation
    out = post(model(disabled=["bash"]), request=None)
    assert "builtin_browser" not in out["disabled"]
    assert state["disabled_tools"] == ["41b3562a", "mcp__other__thing", "bash"]


def test_switching_the_browser_off_from_the_screen(monkeypatch):
    state = _store(monkeypatch, ["41b3562a"])
    post = _endpoints()["POST"]
    import inspect
    model = inspect.signature(post).parameters["body"].annotation
    post(model(disabled=["builtin_browser", "not_a_listed_id"]), request=None)
    assert state["disabled_tools"] == ["41b3562a", "builtin_browser"]
