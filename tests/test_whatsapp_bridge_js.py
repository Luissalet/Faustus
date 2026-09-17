"""The Node bridge parses (node --check) and declares its dependencies;
its behaviour needs a WhatsApp account, so the Python side is tested with a
fake bridge (tests/test_whatsapp_bridge.py)."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_BRIDGE = _REPO / "bridges" / "whatsapp"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node needed")


def test_bridge_script_parses_and_package_pins_baileys():
    proc = subprocess.run(["node", "--check", str(_BRIDGE / "server.mjs")], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    pkg = json.loads((_BRIDGE / "package.json").read_text(encoding="utf-8"))
    assert "@whiskeysockets/baileys" in pkg["dependencies"] and pkg["type"] == "module"
    assert (_BRIDGE / ".gitignore").read_text().strip().startswith("node_modules")
