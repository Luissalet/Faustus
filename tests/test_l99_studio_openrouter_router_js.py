"""OBJ-8 / Lote B1 Studio check, run the same way
`tests/test_l69a_security_adapters_js.py` runs its `.check.mjs`:

* `studio/checks/openrouter_router.check.mjs` — `studio/src/adapters/
  openrouter.ts` and `studio/src/adapters/modelRouter.ts` against their real
  wire shapes (`docs/api/openrouter.md`, `docs/api/model_router.md`), plus
  source inspection confirming `studio/src/screens/settings/
  OpenRouterPrefs.tsx`, `studio/src/screens/settings/ModelRouter.tsx` and
  `studio/src/screens/Settings.tsx` actually wire to those routes (never a
  direct `fetch()`) and that every `t()` key literal those screens use has a
  row in `docs/ui/i18n/es.tsv`.

Needs node and the repo's node_modules (esbuild bundles the TS) — skipped
otherwise, same guard as the existing JS checks.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "studio" / "checks" / "openrouter_router.check.mjs"
_HAS_NODE = shutil.which("node") is not None
_HAS_ESBUILD = (_REPO / "node_modules" / "esbuild" / "lib" / "main.js").exists()

pytestmark = pytest.mark.skipif(
    not (_HAS_NODE and _HAS_ESBUILD), reason="node + node_modules/esbuild needed"
)


def test_l99_studio_openrouter_router_js_check_passes():
    proc = subprocess.run(
        ["node", str(_CHECK)], capture_output=True, text=True,
        encoding="utf-8", cwd=str(_REPO), timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ALL OK" in proc.stdout
    assert "FAIL" not in proc.stdout
