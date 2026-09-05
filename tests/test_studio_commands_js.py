"""The slash-command registry (studio/src/screens/studio/commands.ts) and
the guided tours (studio/src/lib/tours.ts).

Every name, alias, flat alias and subcommand of the previous interface has
to keep resolving, the suggestions have to group by category, and `/help`
has to survive the pipes inside its own usage lines.
`studio/checks/commands.check.mjs` drives the registry, the tours and their
placement maths, and the pure half of the hidden commands
(studio/src/lib/fun.ts), and prints ok/FAIL lines;
this test runs it. Needs node and the repo's node_modules (esbuild bundles
the TS).
"""
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "studio" / "checks" / "commands.check.mjs"
_HAS_NODE = shutil.which("node") is not None
_HAS_ESBUILD = (_REPO / "node_modules" / "esbuild" / "lib" / "main.js").exists()

pytestmark = pytest.mark.skipif(
    not (_HAS_NODE and _HAS_ESBUILD), reason="node + node_modules/esbuild needed"
)


def test_studio_command_checks_pass():
    proc = subprocess.run(
        ["node", str(_CHECK)], capture_output=True, text=True,
        encoding="utf-8", cwd=str(_REPO), timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ALL OK" in proc.stdout
    assert "FAIL" not in proc.stdout
