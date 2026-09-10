"""SET-05 (lote 37): before saving a changed DEFAULT provider/model,
`studio/src/screens/Settings.tsx` previews what moves (privacy, cost) via
`GET /api/setup/provider-change-preview` and confirms with the user only
when there is something real to confirm.

Runs studio/checks/l37-provider-change-preview.check.mjs, mirroring
tests/test_l29_activity_questions_js.py's own wrapping pattern.
"""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parent.parent
_HAS_NODE = shutil.which("node") is not None
_HAS_ESBUILD = (ROOT / "node_modules" / "esbuild" / "lib" / "main.js").exists()


@pytest.mark.skipif(not (_HAS_NODE and _HAS_ESBUILD), reason="node + node_modules/esbuild needed")
def test_provider_change_preview_gates_the_confirm_dialog_correctly():
    result = subprocess.run(
        ["node", "studio/checks/l37-provider-change-preview.check.mjs"], cwd=ROOT,
        capture_output=True, text=True, encoding="utf-8", timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ALL OK" in result.stdout


def test_settings_screen_wires_the_preview_route_and_confirm():
    source = (ROOT / "studio" / "src" / "screens" / "Settings.tsx").read_text(encoding="utf-8")
    assert "/api/setup/provider-change-preview" in source
    assert "window.confirm" in source
    assert "providerChangeQuery" in source
    assert "shouldConfirmBeforeSaving" in source


def test_app_shell_routes_setup_to_the_onboarding_screen():
    """studio/src/screens/Onboarding.tsx's own docstring names the exact gap
    this lote closes: no route reached it at `/setup`."""
    source = (ROOT / "studio" / "src" / "shell" / "AppShell.tsx").read_text(encoding="utf-8")
    assert "'/setup'" in source or '"/setup"' in source
    assert "OnboardingScreen" in source
