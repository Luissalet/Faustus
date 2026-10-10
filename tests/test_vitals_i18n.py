"""UI localization contract against the backend's stable health registry."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

def test_every_registered_health_name_has_a_client_label():
    from src.health import DEFAULT_COMPONENTS
    source = (ROOT / 'studio/src/lib/vitals-i18n.ts').read_text(encoding='utf-8')
    block = source.split('export const HEALTH_LABEL', 1)[1].split('};', 1)[0]
    assert set(re.findall(r'^  (\w+):', block, flags=re.M)) == {name for name, _, _ in DEFAULT_COMPONENTS}

@pytest.mark.skipif(not shutil.which('node') or not (ROOT / 'node_modules/esbuild/lib/main.js').exists(), reason='node/esbuild required')
def test_client_translates_without_mutating_diagnostics_or_losing_numbers():
    result = subprocess.run(['node', str(ROOT / 'tests/vitals_i18n.check.mjs')], cwd=ROOT, capture_output=True, text=True, encoding='utf-8', timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'ALL OK' in result.stdout
