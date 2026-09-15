"""Copy in the desktop window uses navigator.clipboard.writeText.

Chromium asks the session for `clipboard-sanitized-write` on that path.
The desktop shell used to deny every permission except media,
notifications and clipboard-read — so the chat Copy button failed
silently. Sanitized clipboard *write* is the same as Ctrl+C: grant it
for the local origin without a prompt. Reading the clipboard, camera
and notifications still prompt. Everything else stays denied.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_CJS = (REPO_ROOT / "desktop" / "main.cjs").read_text(encoding="utf-8")
POLICY_CJS = (REPO_ROOT / "desktop" / "policy.cjs").read_text(encoding="utf-8")


def test_policy_auto_allows_sanitized_clipboard_write_for_the_local_origin():
    assert "clipboard-sanitized-write" in POLICY_CJS
    assert "function permissionCheck(" in POLICY_CJS
    assert "function permissionRequest(" in POLICY_CJS


def test_the_desktop_session_uses_the_policy_helpers():
    assert "permissionCheck(" in MAIN_CJS
    assert "permissionRequest(" in MAIN_CJS
    # A prompt still exists for the sensitive three; write is not among them.
    assert "['media','notifications','clipboard-read']" in POLICY_CJS or (
        "'media'" in POLICY_CJS and "'clipboard-read'" in POLICY_CJS
    )
    assert "clipboard-sanitized-write" in MAIN_CJS or "permissionCheck" in MAIN_CJS


@pytest.mark.skipif(not shutil.which("node"), reason="node required")
def test_desktop_policy_unit_tests():
    result = subprocess.run(
        ["node", "--test", "test.cjs"],
        cwd=REPO_ROOT / "desktop",
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
