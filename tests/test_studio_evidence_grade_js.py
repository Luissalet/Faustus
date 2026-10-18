import shutil
import subprocess
from pathlib import Path

import pytest


def test_evidence_grade_live_history_and_render():
    root = Path(__file__).resolve().parent.parent
    if not shutil.which('node') or any(not (root / 'node_modules' / package).exists()
                                     for package in ('esbuild', 'happy-dom')):
        pytest.skip('node, esbuild and happy-dom required for the real adapter/render check')
    result = subprocess.run(['node', 'studio/checks/evidence-grade.check.mjs'], cwd=root,
                            capture_output=True, text=True, encoding='utf-8', timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'ALL OK' in result.stdout
