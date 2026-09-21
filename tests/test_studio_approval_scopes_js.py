"""`studio/checks/approval-scopes.check.mjs` — the permission card's answers.

Guards the 20-09-2026 fix: the card renders the scopes the server sends
(narrowest first) instead of its own four buttons, whose wording said the
opposite of what the decisions did. Needs node + the repo's node_modules
(esbuild bundles the TS), same guard as the other JS checks.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "studio" / "checks" / "approval-scopes.check.mjs"
_HAS_NODE = shutil.which("node") is not None
_HAS_ESBUILD = (_REPO / "node_modules" / "esbuild" / "lib" / "main.js").exists()

pytestmark = pytest.mark.skipif(
    not (_HAS_NODE and _HAS_ESBUILD), reason="node + node_modules/esbuild needed"
)


def test_approval_scopes_check():
    proc = subprocess.run(
        [shutil.which("node"), str(_CHECK)],
        cwd=str(_REPO), capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
