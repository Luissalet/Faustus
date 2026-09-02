"""Ronda 6, seen live: "Haz una captura de pantalla de mi escritorio (la
pantalla del PC…)" classified as domain `desktop` AND as a local-computer
request. The local-computer branch REPLACED the selected set with the
Terminus (file/terminal) toolset, throwing away the deterministic domain
seed — the model got 0 desktop tools and answered that it cannot see the
screen. The domain seed must survive every branch that narrows the set."""
import pytest

from tests.test_agent_loop_workspace_tool_floor import tools_sent, workspace  # noqa: F401  (fixture re-export)


@pytest.fixture(autouse=True)
def _desktop_available(monkeypatch):
    from src.agent_tools import desktop_tools as dt

    class _Backend:
        name = "fake"

        def screen_size(self):
            return (1920, 1080)

    monkeypatch.setattr(dt, "desktop_availability", lambda: (True, "fake backend"))
    monkeypatch.setattr(dt, "get_backend", lambda: _Backend())
    yield


SPANISH = ("Haz una captura de pantalla de mi escritorio (la pantalla del PC, no el navegador), "
           "dime que ventanas hay abiertas y describe brevemente lo que ves en la captura")
ENGLISH = "Take a screenshot of my desktop (this PC's screen, not the browser) and tell me which windows are open"


@pytest.mark.parametrize("message", [SPANISH, ENGLISH])
def test_desktop_tools_survive_the_local_computer_toolset(message, workspace):
    names = tools_sent(message, workspace, settings={"desktop_control_mode": "ask_each"})
    assert "desktop_screenshot" in names, names
    assert "desktop_list_windows" in names, names


def test_desktop_tools_are_offered_without_a_workspace(tmp_path):
    names = tools_sent(SPANISH, None, settings={"desktop_control_mode": "ask_each"})
    assert "desktop_screenshot" in names, names


def test_a_plain_coding_request_does_not_drag_the_desktop_tools_in(workspace):
    names = tools_sent("Anade a cart.py una funcion apply_tax(total, rate) y su test", workspace)
    assert "desktop_screenshot" not in names and "desktop_click" not in names, names
