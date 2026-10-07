"""OBJ-25: the Research report view shows the citation verdicts and the blind
review honestly. studio/checks/research-verification.check.mjs bundles
studio/src/lib/researchVerdicts.ts and drives it ("not checked" is never
"could not be checked" nor verified, a source that also fails a sentence is
partial, a failed or missing review is never a grade), then checks the
adapter, the view and its styles keep those promises.
"""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(not shutil.which("node") or not (ROOT / "node_modules" / "esbuild" / "lib" / "main.js").exists(),
                    reason="node + node_modules/esbuild needed")
def test_the_report_reads_its_own_checks_honestly():
    result = subprocess.run(["node", "studio/checks/research-verification.check.mjs"], cwd=ROOT,
                            capture_output=True, text=True, encoding="utf-8", timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok research-verification" in result.stdout
