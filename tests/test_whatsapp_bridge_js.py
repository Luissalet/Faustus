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


def test_bridge_learns_lids_groups_media_and_history():
    src = (_BRIDGE / "server.mjs").read_text(encoding="utf-8")
    # LID ↔ phone mapping: chats and contacts are filed under the phone identity
    assert "function learnLid" in src and "lids.json" in src and "chats.phoneNumberShare" in src
    # group subjects from metadata, fetched for all groups on connect and lazily per group
    assert "groupFetchAllParticipating" in src and "groupMetadata(jid)" in src
    # voice notes / photos / documents pulled once under media/, served by /media/<id>.<ext>
    assert "downloadMediaMessage" in src and '"/media/"' in src and "MEDIA_MAX_BYTES" in src
    # profile pictures cached under avatars/ and older messages on demand
    assert "profilePictureUrl" in src and '"/avatar"' in src
    assert "fetchMessageHistory" in src and '"/history"' in src and "syncFullHistory: true" in src
    # the library's `error` key is serialised as a real error, not `{}`
    assert "error: pino.stdSerializers.err" in src
