"""Lote 69a — SEC-01 / SEC-04 / TOOL-04 Studio adapter checks, run the same
way `tests/test_l55_studio_checks_js.py` runs its `.check.mjs` files:

* `studio/checks/l69a-security-adapters.check.mjs` — the new
  `studio/src/adapters/approvals.ts` (GET /api/approvals/active,
  DELETE /api/approvals/{id}) and `studio/src/adapters/commandGuard.ts`
  (`routes/command_guard_routes.py`'s allowlist, previously reachable only
  by calling the route directly) against their real wire shapes, plus
  TOOL-04's `manifest_diff`/`declared_permissions`/`policy_decision`
  round-tripping through `studio/src/adapters/integrations.ts`'s new
  `setMcpEnvMode` and `approveMcpManifest`.

Needs node and the repo's node_modules (esbuild bundles the TS) — skipped
otherwise, same guard as the existing JS checks.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "studio" / "checks" / "l69a-security-adapters.check.mjs"
_HAS_NODE = shutil.which("node") is not None
_HAS_ESBUILD = (_REPO / "node_modules" / "esbuild" / "lib" / "main.js").exists()

pytestmark = pytest.mark.skipif(
    not (_HAS_NODE and _HAS_ESBUILD), reason="node + node_modules/esbuild needed"
)


def test_l69a_security_adapters_js_check_passes():
    proc = subprocess.run(
        ["node", str(_CHECK)], capture_output=True, text=True,
        encoding="utf-8", cwd=str(_REPO), timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ALL OK" in proc.stdout
    assert "FAIL" not in proc.stdout
