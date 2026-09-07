import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize('check', ['clipboard-attachments', 'provider-connect', 'runner-connections', 'workbench'])
def test_clipboard_and_upload_lifecycle(check):
    root = Path(__file__).resolve().parent.parent
    if not shutil.which('node') or not (root / 'node_modules/esbuild').exists():
        pytest.skip('node and esbuild required')
    result = subprocess.run(['node', f'studio/checks/{check}.check.mjs'],
                            cwd=root, capture_output=True, text=True, encoding='utf-8', timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'ALL OK' in result.stdout
