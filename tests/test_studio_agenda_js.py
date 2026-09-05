"""The week as a timetable (studio/src/lib/agenda.ts).

Minutes to pixels and back, the slice of an event that falls on one day,
overlapping events packed into columns, drag-to-move and drag-to-resize
with their snapping and limits, and what the calendar search box matches.
`studio/checks/agenda.check.mjs` drives them and prints ok/FAIL lines; this
test runs it. Needs node and the repo's node_modules (esbuild bundles the
TS).
"""
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "studio" / "checks" / "agenda.check.mjs"
_HAS_NODE = shutil.which("node") is not None
_HAS_ESBUILD = (_REPO / "node_modules" / "esbuild" / "lib" / "main.js").exists()

pytestmark = pytest.mark.skipif(
    not (_HAS_NODE and _HAS_ESBUILD), reason="node + node_modules/esbuild needed"
)


def test_studio_agenda_checks_pass():
    proc = subprocess.run(
        ["node", str(_CHECK)], capture_output=True, text=True,
        encoding="utf-8", cwd=str(_REPO), timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ALL OK" in proc.stdout
    assert "FAIL" not in proc.stdout
