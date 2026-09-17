"""Nearby apps (Connectors screen): scan local ports, one-click add for a
recognised preset with no missing placeholder, and the "followed the app"
note for a connector whose `check` reported `relocated_from`.
`studio/checks/nearby-apps.check.mjs` asserts the adapter functions
(`discoverApps`/`adoptApp`), the panel mount in Connectors.tsx, and both
render paths against the real source. Needs node — skipped otherwise."""
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "studio" / "checks" / "nearby-apps.check.mjs"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node needed")


def test_nearby_apps_check_passes():
    proc = subprocess.run(["node", str(_CHECK)], capture_output=True, text=True,
                          encoding="utf-8", cwd=str(_REPO), timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ALL OK" in proc.stdout
