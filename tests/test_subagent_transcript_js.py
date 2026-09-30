"""Studio side of the sub-agent transcript and the Android preset: the
`.check.mjs` scripts run under node, skipped when node/esbuild are missing."""
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_HAS_NODE = shutil.which("node") is not None
_HAS_ESBUILD = (_REPO / "node_modules" / "esbuild" / "lib" / "main.js").exists()

pytestmark = pytest.mark.skipif(
    not (_HAS_NODE and _HAS_ESBUILD), reason="node + node_modules/esbuild needed"
)


@pytest.mark.parametrize("name, marker", [
    ("subagent-transcript.check.mjs", "subagent-transcript: ALL OK"),
    ("mcp-android-preset.check.mjs", "mcp-android-preset: ALL OK"),
])
def test_check_script(name, marker):
    proc = subprocess.run(["node", str(_REPO / "studio" / "checks" / name)],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert marker in proc.stdout
