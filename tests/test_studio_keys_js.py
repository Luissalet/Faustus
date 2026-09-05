"""Keyboard shortcuts, and the AltGr trap.

On AZERTY, QWERTZ and most non-US layouts, AltGr is how you type @ # { } [ ]
| \\ and €. Browsers report it as ctrlKey AND altKey, so without a guard a
French user typing "@" into an email address silently fires a Ctrl+Alt
shortcut — in this app: new chat, delete chat, incognito.

`studio/checks/keys.check.mjs` drives `matchesCombo` with the events a
browser really sends and prints ok/FAIL lines; this test runs it. Needs node
and the repo's node_modules (esbuild bundles the TS).
"""
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "studio" / "checks" / "keys.check.mjs"
_HAS_NODE = shutil.which("node") is not None
_HAS_ESBUILD = (_REPO / "node_modules" / "esbuild" / "lib" / "main.js").exists()

pytestmark = pytest.mark.skipif(
    not (_HAS_NODE and _HAS_ESBUILD), reason="node + node_modules/esbuild needed"
)


def test_studio_keys_checks_pass():
    proc = subprocess.run(
        ["node", str(_CHECK)], capture_output=True, text=True,
        encoding="utf-8", cwd=str(_REPO), timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ALL OK" in proc.stdout
    assert "FAIL" not in proc.stdout


def test_the_guard_prefers_the_real_signal():
    """`getModifierState('AltGraph')` is the browser saying so. The character
    heuristic is the fallback for browsers that do not implement it — not the
    other way round, which would misjudge a genuine Ctrl+Alt+ç."""
    src = (_REPO / "studio" / "src" / "adapters" / "settings.ts").read_text(encoding="utf-8")
    body = src[src.index("function isAltGr"):src.index("export function matchesCombo")]
    assert "getModifierState" in body
    assert body.index("getModifierState") < body.index("/^[a-z0-9]$/i")
