"""Exporting a chat, and what happens when the server says no.

`window.open` on an export URL cannot see the response, so a 400 (a format
the server does not know) or a 503 (the PDF dependency is not installed)
arrived as a blank tab or a page of raw JSON. It reads as a broken button,
and the server's sentence — the one that names the package to install —
reached nobody.

The download goes through fetch and a Blob, so the status can be read and
the message shown. `studio/checks/export.check.mjs` drives it against a
stubbed server; this test runs it and pins that no caller has gone back to
opening a tab.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "studio" / "checks" / "export.check.mjs"
_SRC = _REPO / "studio" / "src"
_HAS_NODE = shutil.which("node") is not None
_HAS_ESBUILD = (_REPO / "node_modules" / "esbuild" / "lib" / "main.js").exists()


@pytest.mark.skipif(not (_HAS_NODE and _HAS_ESBUILD), reason="node + node_modules/esbuild needed")
def test_the_download_behaves():
    proc = subprocess.run(
        ["node", str(_CHECK)], capture_output=True, text=True,
        encoding="utf-8", cwd=str(_REPO), timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ALL OK" in proc.stdout
    assert "FAIL" not in proc.stdout


def test_no_export_is_opened_in_a_tab_it_cannot_read():
    """The root-cause guard: `window.open` on an export URL is the bug."""
    for rel in ("screens/Studio.tsx", "screens/studio/SessionDialog.tsx"):
        src = (_SRC / rel).read_text(encoding="utf-8")
        for line in src.splitlines():
            if "window.open" in line and "export" in line.lower():
                raise AssertionError(f"{rel}: an export opened in a tab:\n  {line.strip()}")


def test_both_callers_report_the_failure():
    """A refused export that says nothing is the same bug wearing a different
    hat: the user still just sees nothing happen."""
    studio = (_SRC / "screens" / "Studio.tsx").read_text(encoding="utf-8")
    dialog = (_SRC / "screens" / "studio" / "SessionDialog.tsx").read_text(encoding="utf-8")
    for name, src in (("Studio.tsx", studio), ("SessionDialog.tsx", dialog)):
        assert "downloadExport(" in src, f"{name} does not use the checked download"
        block = src[src.index("downloadExport("):][:600]
        assert "catch" in block, f"{name}: the failure is not caught"
        assert "Could not export" in block, f"{name}: the failure is not said out loud"
