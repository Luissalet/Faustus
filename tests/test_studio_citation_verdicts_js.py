"""Research report citation verdicts (§144 follow-up):
`studio/checks/citation-verdicts.check.mjs` asserts the per-source verdict
field reaches the adapter's ResearchSource type and is rendered as a badge
in the report view, against the real source. Needs node — skipped
otherwise."""
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "studio" / "checks" / "citation-verdicts.check.mjs"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node needed")


def test_citation_verdicts_check_passes():
    proc = subprocess.run(["node", str(_CHECK)], capture_output=True, text=True,
                          encoding="utf-8", cwd=str(_REPO), timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ALL OK" in proc.stdout
