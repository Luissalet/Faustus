"""DESK-02 · `scripts/ui_journeys.py` wired against a REAL Studio.

`src.browser_journey_verification.run_journey` was already proven correct
against fixture snapshots (`tests/test_p1_desk_02_journey.py`) — this test
is the other half: launching the script that drives it with an actual
Chromium against the actual built `static/studio/` bundle, the same way a
person would run it (`python scripts/ui_journeys.py`).

Marked `@pytest.mark.slow` on purpose (opens a real subprocess server and a
real browser, ~30s here) — the fast lane runs `-m "not slow"`
(tests/README.md), so this never slows the normal suite down; it is meant to
be run explicitly, the same way `scripts/ui_smoke.py` itself is not part of
the ordinary pytest run.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "ui_journeys.py"


def _playwright_chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            browser.close()
        return True
    except Exception:  # noqa: BLE001 — no browser binary installed, or a sandbox restriction
        return False


pytestmark = pytest.mark.slow


@pytest.mark.skipif(not _playwright_chromium_available(),
                    reason="Playwright Chromium is not available in this environment")
def test_ui_journeys_script_passes_against_a_real_studio(tmp_path):
    out_dir = tmp_path / "ui_journeys"
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--out-dir", str(out_dir)],
        cwd=str(REPO), capture_output=True, text=True, timeout=170,
    )
    assert proc.returncode == 0, (
        f"ui_journeys.py exited {proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )

    result = json.loads((out_dir / "result.json").read_text(encoding="utf-8"))
    assert result["mode"] == "live", result  # never silently fell back to --dry-run
    assert result["ok"] is True, result

    names = {j["name"] for j in result["journeys"]}
    assert names == {"object_leak_buttons", "main_screen"}
    for journey in result["journeys"]:
        assert journey["passed"] is True, journey


def test_dry_run_never_needs_a_browser_and_proves_the_selectors_are_real(tmp_path):
    """Companion to the live test above: proves the testids the live
    journeys drive actually exist in the shipped bundle, with no Chromium
    needed at all. Still under this module's `pytestmark = pytest.mark.slow`
    — the ID calls for a slow-marked test file wired to the live script, and
    keeping this alongside it (rather than in its own always-on file) means
    one place to find both; it finishes in well under a second regardless."""
    out_dir = tmp_path / "ui_journeys_dry"
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--dry-run", "--out-dir", str(out_dir)],
        cwd=str(REPO), capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    result = json.loads((out_dir / "result.json").read_text(encoding="utf-8"))
    assert result["mode"] == "dry-run"
    assert result["ok"] is True
    assert result["missing"] == []
