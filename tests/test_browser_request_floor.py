"""A request about a web page must be offered the integrated browser.

Seen live (22-10, DeepSeek V4.1 Flash): "Haz una captura de pantalla de la
página http://127.0.0.1:5205/#/modelos con tu herramienta de navegador" got
desktop_* and page_* tools only. The browser is an MCP server with English
descriptions, so the Spanish examples of the desktop tools out-ranked it in
retrieval; the model said "No browser tool available" and spent 15 rounds
failing with desktop tools.
"""
import inspect

import pytest

from src import agent_loop as al


@pytest.mark.parametrize("text", [
    "Haz una captura de pantalla de la página http://127.0.0.1:5205/#/modelos con tu herramienta de navegador",
    "abre http://localhost:3000 y dime qué sale",
    "Entra en https://example.com y rellena el formulario de contacto",
    "mira la página http://127.0.0.1:8000/admin",
    "Open https://example.org and take a screenshot",
    "go to www.example.com and click on Pricing",
    "usa el navegador para ver si la web carga",
    "check it in the browser",
    "haz clic en el botón de login de http://localhost:5173",
])
def test_requests_about_a_page_want_the_browser(text):
    assert al._browser_request(text)


@pytest.mark.parametrize("text", [
    "the API listens on http://localhost:8000, add an endpoint that returns the version",
    "Añade a server.py una función health() que devuelva {\"status\": \"ok\"}",
    "el servidor web en http://localhost:8000 devuelve 500, arréglalo en el código",
    "haz una captura de pantalla del escritorio",
    "pon delante la ventana de Blender",
    "",
])
def test_other_requests_do_not(text):
    assert not al._browser_request(text)


def test_the_floor_adds_route_level_browser_intent_before_expansion():
    src = inspect.getsource(al)
    floor = src.index("browser offered for a request about a web page")
    expansion = src.index("_relevant_tools = _expand_browser_mcp_tools(_relevant_tools, mcp_mgr)\n")
    assert floor < expansion
    block = src[src.rindex("if (not guide_only", 0, floor):floor]
    assert "not relevant_tools" in block            # a caller-pinned scope is a ceiling
    assert "builtin_browser\" not in disabled_tools" in block


class _Mgr:
    def get_all_tools(self):
        return [{"server_id": "builtin_browser", "qualified_name": f"mcp__builtin_browser__{n}", "is_disabled": False}
                for n in ("browser_navigate", "browser_take_screenshot", "browser_snapshot", "browser_click")]


def test_route_level_intent_expands_to_the_browser_tools():
    got = al._expand_browser_mcp_tools({"builtin_browser", "desktop_screenshot"}, _Mgr())
    assert "mcp__builtin_browser__browser_navigate" in got
    assert "mcp__builtin_browser__browser_take_screenshot" in got
