"""WhatsApp screen: `studio/checks/whatsapp.check.mjs` asserts the adapter
(`waStatus`/`waStart`/`waStop`/`waLogout`/`waChats`/`waMessages`/`waContacts`/
`waSend`/`waMarkRead`), the screen's setup panel per status, the QR image,
the two-pane chat layout with inline (never native) confirms for Stop/Unlink,
and the route wiring in AppShell/routes.ts, against the real source. Needs
node — skipped otherwise."""
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "studio" / "checks" / "whatsapp.check.mjs"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node needed")


def test_whatsapp_check_passes():
    proc = subprocess.run(["node", str(_CHECK)], capture_output=True, text=True,
                          encoding="utf-8", cwd=str(_REPO), timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ALL OK" in proc.stdout
