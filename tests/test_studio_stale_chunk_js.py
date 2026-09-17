"""17-09: the desktop window went blank on Connectors — yesterday's page asked
for a route chunk a rebuild had renamed, `import()` rejected, React unmounted
everything. `studio/checks/stale-chunk.check.mjs` asserts every route chunk
goes through `lazyChunk` (one reload on a stale chunk) and the routes sit
inside `RouteErrorBoundary` (the shell stays on screen with a Reload button).
Needs node — skipped otherwise."""
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "studio" / "checks" / "stale-chunk.check.mjs"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node needed")


def test_stale_chunk_check_passes():
    proc = subprocess.run(["node", str(_CHECK)], capture_output=True, text=True,
                          encoding="utf-8", cwd=str(_REPO), timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ALL OK" in proc.stdout
