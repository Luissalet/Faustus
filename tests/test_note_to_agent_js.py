"""Runs the note-to-agent prompt suite under pytest, and pins the wiring.

Behaviour lives in tests/note_to_agent.test.mjs (node:test, no DOM). The
wiring assertions are here because the button and the module live in two
different files and either one alone does nothing.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_HAS_NODE = shutil.which("node") is not None

NOTES = (_REPO / "static" / "js" / "notes.js").read_text(encoding="utf-8")
HTML = (_REPO / "static" / "index.html").read_text(encoding="utf-8")
MODULE = (_REPO / "static" / "js" / "noteToAgent.js").read_text(encoding="utf-8")


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_note_to_agent_prompt_behavior():
    result = subprocess.run(
        ["node", "--test", "tests/note_to_agent.test.mjs"],
        cwd=_REPO, capture_output=True, timeout=60, text=True, encoding="utf-8",
    )
    if result.returncode != 0:
        raise AssertionError(
            f"node --test failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")


def test_the_card_actually_has_the_button():
    assert "data-note-to-agent=" in NOTES


def test_the_module_is_loaded_by_the_page():
    assert '/static/js/noteToAgent.js' in HTML


def test_the_click_does_not_also_open_the_note_editor():
    """The card opens the editor on click; the button must swallow its own."""
    assert "stopPropagation()" in MODULE


def test_it_submits_through_the_normal_chat_path():
    """Reusing handleChatSubmit keeps agent mode, workspace and queue intact."""
    assert "handleChatSubmit" in MODULE
    assert "getElementById('message')" in MODULE
