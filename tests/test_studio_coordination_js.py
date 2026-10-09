"""The project coordination editor must not silently widen or lose saved grants."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT=Path(__file__).resolve().parents[1]
pytestmark=pytest.mark.skipif(not shutil.which('node') or not (ROOT/'node_modules/esbuild/lib/main.js').exists(),
                            reason='node and esbuild are required')

def test_coordination_adapter_permissions_and_transport():
    result=subprocess.run([shutil.which('node'),str(ROOT/'studio/checks/coordination.check.mjs')],cwd=ROOT,
                          capture_output=True,text=True,encoding='utf-8',timeout=60)
    assert result.returncode==0,result.stdout+result.stderr
