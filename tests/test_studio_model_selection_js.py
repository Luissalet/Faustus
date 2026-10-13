from pathlib import Path
import shutil,subprocess
import pytest

def test_studio_model_selection_contract():
    if not shutil.which('node'):pytest.skip('Node unavailable')
    root=Path(__file__).resolve().parents[1]
    if not (root/'node_modules/esbuild/lib/main.js').exists():pytest.skip('Frontend dependencies unavailable')
    result=subprocess.run(['node',str(root/'studio/checks/model-selection.check.mjs')],cwd=root,capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr
