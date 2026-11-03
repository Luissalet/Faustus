"""The Recipes screen's reasoning (studio/src/adapters/recipes.ts): a dry run
is structural and never verified, only a real `verified` execution may claim
it, unknown statuses read down, and the forms refuse bad JSON in words.
Needs node and the repo's node_modules (esbuild bundles the TS)."""
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "studio" / "checks" / "recipes.check.mjs"
pytestmark = pytest.mark.skipif(
    not (shutil.which("node") and (_REPO / "node_modules" / "esbuild" / "lib" / "main.js").exists()),
    reason="node + node_modules/esbuild needed",
)


def test_studio_recipes_checks_pass():
    proc = subprocess.run(["node", str(_CHECK)], capture_output=True, text=True, encoding="utf-8",
                          cwd=str(_REPO), timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ALL OK" in proc.stdout and "FAIL" not in proc.stdout
