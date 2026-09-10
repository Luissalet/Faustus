"""Lote 55's new Studio checks, run the same way
`tests/test_studio_commands_js.py` already runs `commands.check.mjs`:

* `studio/checks/l55-ux09-personal-actions.check.mjs` — UX-09's /nota,
  /recordatorio, /evento aliases resolve to note/reminder/event with no
  alias clash.
* `studio/checks/l55-act05-queue.check.mjs` — ACT-05's queue adapter
  (loadQueue/prioritizeQueueItem) against routes/queue_routes.py's wire
  shape.
* `studio/checks/l55-set02-model-profile.check.mjs` — SET-02's endpoint
  profile / capability-manifest adapter (studio/src/adapters/fit.ts) against
  routes/local_models_routes.py's wire shape.
* `studio/checks/l55-ux06-composer-perf.check.mjs` — UX-06's composer
  suggestion matching (composer-suggest.ts) coalesces a keystroke burst
  through frameBatcher into one match pass and caps a 500-candidate mention
  list, both measured under the 16ms-per-keystroke budget.

Needs node and the repo's node_modules (esbuild bundles the TS) — skipped
otherwise, same guard as the existing JS checks.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECKS = [
    _REPO / "studio" / "checks" / "l55-ux09-personal-actions.check.mjs",
    _REPO / "studio" / "checks" / "l55-act05-queue.check.mjs",
    _REPO / "studio" / "checks" / "l55-set02-model-profile.check.mjs",
    _REPO / "studio" / "checks" / "l55-ux06-composer-perf.check.mjs",
]
_HAS_NODE = shutil.which("node") is not None
_HAS_ESBUILD = (_REPO / "node_modules" / "esbuild" / "lib" / "main.js").exists()

pytestmark = pytest.mark.skipif(
    not (_HAS_NODE and _HAS_ESBUILD), reason="node + node_modules/esbuild needed"
)


@pytest.mark.parametrize("check", _CHECKS, ids=lambda p: p.name)
def test_l55_js_check_passes(check):
    proc = subprocess.run(
        ["node", str(check)], capture_output=True, text=True,
        encoding="utf-8", cwd=str(_REPO), timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ALL OK" in proc.stdout
    assert "FAIL" not in proc.stdout
