import shutil,subprocess
from pathlib import Path
import pytest

def test_shared_global_store_and_cross_surface_sync():
    root=Path(__file__).resolve().parent.parent
    if not shutil.which('node') or not (root/'node_modules/esbuild').exists():pytest.skip('Node/esbuild required')
    result=subprocess.run(['node','studio/checks/global-approvals.check.mjs'],cwd=root,capture_output=True,text=True,encoding='utf8',timeout=60)
    assert result.returncode==0,result.stdout+result.stderr
