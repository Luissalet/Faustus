"""Clicking by on-screen text (local OCR) and OCR text for models that cannot see."""
import pytest

from src import screen_ocr
from src.agent_tools import desktop_tools as dt
from tests.test_desktop_tools import _run, _settings, backend  # noqa: F401 - fixtures

LINES = [
    {"text": "Auto Process Figure", "score": 0.9, "box": [0, 0, 10, 10], "center": [1400, 160]},
    {"text": "Assign Upscaled Materials", "score": 0.9, "box": [0, 0, 10, 10], "center": [1450, 281]},
    {"text": "Load Sculptors Textures", "score": 0.9, "box": [0, 0, 10, 10], "center": [1455, 314]},
    {"text": "Load", "score": 0.7, "box": [0, 0, 10, 10], "center": [1490, 795]},
]


@pytest.fixture
def ocr(monkeypatch):
    monkeypatch.setattr(screen_ocr, "available", lambda: True)
    monkeypatch.setattr(screen_ocr, "read_text", lambda img: [dict(line) for line in LINES])


def test_match_score_tolerates_apostrophes_and_case():
    assert screen_ocr.match_score("Load Sculptor's Textures", "Load Sculptors Textures") == 1.0
    assert screen_ocr.match_score("load", "Load") == 1.0
    assert screen_ocr.match_score("Sculptors Textures", "Load Sculptors Textures") > 0.9
    assert screen_ocr.match_score("Render", "Load Sculptors Textures") < screen_ocr.MATCH_MIN


def test_click_by_text_clicks_the_label_centre(backend, ocr):
    _, result = _run("desktop_click", {"text": "Load Sculptors Textures"})
    assert result["exit_code"] == 0, result
    assert backend.calls[-1] == ("click", 1455, 314, "left")
    assert result["matched_text"] == "Load Sculptors Textures"


def test_click_by_text_prefers_the_exact_label(backend, ocr):
    _, result = _run("desktop_click", {"text": "Load"})
    assert backend.calls[-1] == ("click", 1490, 795, "left")


def test_click_by_text_inside_a_window_offsets_to_screen(backend, ocr):
    _, result = _run("desktop_click", {"text": "Load Sculptors Textures", "window": "firefox"})
    assert result["exit_code"] == 0, result
    assert ("grab", (100, 100, 1500, 800)) in backend.calls
    assert backend.calls[-1] == ("click", 1555, 414, "left")


def test_click_by_text_reports_missing_label(backend, ocr):
    _, result = _run("desktop_click", {"text": "Render Animation"})
    assert result["exit_code"] != 0
    assert not any(c[0] == "click" for c in backend.calls)


def test_ambiguous_label_needs_an_index(backend, monkeypatch):
    twice = [dict(LINES[0]), dict(LINES[0], center=[1450, 220])]
    monkeypatch.setattr(screen_ocr, "available", lambda: True)
    monkeypatch.setattr(screen_ocr, "read_text", lambda img: [dict(x) for x in twice])
    _, result = _run("desktop_click", {"text": "Auto Process Figure"})
    assert result["exit_code"] != 0 and "index" in str(result)
    _, result = _run("desktop_click", {"text": "Auto Process Figure", "index": 2})
    assert backend.calls[-1] == ("click", 1450, 220, "left")


def test_text_only_model_gets_the_ocr_listing():
    from src.agent_loop import _tool_image_messages
    record = {"tool_name": "desktop_screenshot", "vision_capable": False,
              "image_text": screen_ocr.format_lines(LINES),
              "result": {"images": [{"data": "iVBORw0KGgo=", "mimeType": "image/png"}]}}
    messages = _tool_image_messages(record, vision_capable=None)
    text = messages[0]["content"]
    assert '"Load Sculptors Textures" at (1455, 314)' in text
    assert "desktop_click" in text