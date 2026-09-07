from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(not shutil.which('node'), reason='Node.js required')
def test_saved_evidence_adapter_and_rendering():
    result = subprocess.run(['node', 'studio/checks/saved-evidence.check.mjs'], cwd=ROOT,
                            capture_output=True, text=True, encoding='utf-8', timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'ALL OK' in result.stdout


def test_saved_evidence_fetch_is_scoped_cancellable_and_retryable():
    source = (ROOT / 'studio/src/screens/studio/SavedEvidence.tsx').read_text(encoding='utf-8')
    assert 'encodeURIComponent(evidence.id)' in source
    assert 'controller.abort()' in source and '15_000' in source
    assert 'active = false' in source and 'if (active)' in source
    assert 'role="alert"' in source and "t('Retry')" in source
    assert 'dangerouslySetInnerHTML' not in source
