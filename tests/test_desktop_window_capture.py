"""Window capture by title and focus retries."""
from src.agent_tools import desktop_tools as dt
from tests.test_desktop_tools import _run, _settings, backend  # noqa: F401 - fixtures


def test_screenshot_of_a_window_by_title(backend):
    _, result = _run("desktop_screenshot", {"window": "firefox"})
    assert result["exit_code"] == 0, result
    assert ("grab", (100, 100, 1500, 800)) in backend.calls
    assert result["region"] == [100, 100, 1500, 800]


def test_screenshot_of_a_missing_window_says_so(backend):
    _, result = _run("desktop_screenshot", {"window": "nope"})
    assert result["exit_code"] != 0