"""Web-source handling in Studio (freshness/sources lote): dedupe-by-URL in
the transcript reducer (studio/src/screens/studio/model.ts) and the favicon
helpers in studio/src/adapters/chat.ts (domain extraction, /api/favicon
path, dedupe-by-site + cap-at-N).

`studio/checks/sources.check.mjs` drives both modules directly and prints
ok/FAIL lines; this test runs it. Needs node and the repo's node_modules
(esbuild bundles the TS).
"""
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "studio" / "checks" / "sources.check.mjs"
_HAS_NODE = shutil.which("node") is not None
_HAS_ESBUILD = (_REPO / "node_modules" / "esbuild" / "lib" / "main.js").exists()

pytestmark = pytest.mark.skipif(
    not (_HAS_NODE and _HAS_ESBUILD), reason="node + node_modules/esbuild needed"
)


def test_studio_sources_checks_pass():
    proc = subprocess.run(
        ["node", str(_CHECK)], capture_output=True, text=True,
        encoding="utf-8", cwd=str(_REPO), timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
