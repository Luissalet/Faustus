"""ACT-02: studio/checks/p1-act02-notifications.check.mjs — the client-side
half of the "a reconnect doesn't re-notify twenty times" guarantee
(shell/notifications.ts::emitForNewRuns only ever emits a run once per
poll-tick diff; the server-side half is tests/test_p1_act_02_notifications.py)."""
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "studio" / "checks" / "p1-act02-notifications.check.mjs"
_HAS_NODE = shutil.which("node") is not None
_HAS_ESBUILD = (_REPO / "node_modules" / "esbuild" / "lib" / "main.js").exists()

pytestmark = pytest.mark.skipif(
    not (_HAS_NODE and _HAS_ESBUILD), reason="node + node_modules/esbuild needed"
)


def test_notification_dedupe_check_passes():
    proc = subprocess.run(
        ["node", str(_CHECK)], capture_output=True, text=True,
        encoding="utf-8", cwd=str(_REPO), timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "OK" in proc.stdout
