"""L20 (integrates L13, item 2): the Composer autonomy-preset picker must
actually reach the server. `studio/checks/studio-autonomy-wiring.check.mjs`
checks the `sendTurn({...})` call site in `studio/src/screens/Studio.tsx`
uses the shared pick/project/global resolver and blocks unresolved projects;
this runs it. Runtime precedence and request bodies have a separate adapter check.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "studio" / "checks" / "studio-autonomy-wiring.check.mjs"
_HAS_NODE = shutil.which("node") is not None

pytestmark = pytest.mark.skipif(not _HAS_NODE, reason="node needed")


def test_studio_forwards_autonomy_preset_to_send_turn():
    proc = subprocess.run(
        ["node", str(_CHECK)], capture_output=True, text=True,
        encoding="utf-8", cwd=str(_REPO), timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "forwards knobs.autonomyPreset" in proc.stdout
