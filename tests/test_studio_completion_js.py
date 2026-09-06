"""The Completion screen's reasoning.

A completion decision says what a turn did beyond the literal ask, what it
refused, and why it stopped. Everything the screen claims about that is derived
in `studio/src/adapters/completion.ts` rather than inside a component: that
`converged`, `budget` and `unfinished` are three endings with three tones and
three sentences and are never each other, that an honest stop beside an
exhausted budget line is drawn as contested rather than believed, that a stop
reason this build has never heard of reads as an interruption and never as
finished, that shadow decisions are never mixed silently with real ones, that
the five budget lines meter three pots whose ceilings partition the total
exactly, that a line which was never opened is not reported as spent, and that
every refusal is drawn under a reason -- including the sentinel for one nobody
recorded.

A panel whose reasoning cannot be checked is decoration, so
`studio/checks/completion.check.mjs` drives those functions and prints ok/FAIL
lines; this test runs it. Needs node and the repo's node_modules (esbuild
bundles the TS).
"""
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "studio" / "checks" / "completion.check.mjs"
_HAS_NODE = shutil.which("node") is not None
_HAS_ESBUILD = (_REPO / "node_modules" / "esbuild" / "lib" / "main.js").exists()

pytestmark = pytest.mark.skipif(
    not (_HAS_NODE and _HAS_ESBUILD), reason="node + node_modules/esbuild needed"
)


def test_studio_completion_checks_pass():
    proc = subprocess.run(
        ["node", str(_CHECK)], capture_output=True, text=True,
        encoding="utf-8", cwd=str(_REPO), timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ALL OK" in proc.stdout
    assert "FAIL" not in proc.stdout
