"""Lote 86 follow-up — SourceControlPanel's compact mode, checked by
behaviour instead of by slicing source text.

`l86-source-control-panel.check.mjs` used to assert on a text slice of
`SourceControlPanel.tsx` between `if (compact) {` and the full-mode
`return (` — a test on code text, not on what actually renders. That broke
the day compact mode legitimately grew a "Create repository" empty-state
action, which is correct behaviour: compact mode still must not surface
any of the full-mode-only dialogs (New repository, Create branch, Publish
to GitHub, repository policy) once a repo is selected, and the New
repository dialog it *does* offer from the empty state must stay closed
until it is clicked.

This wrapper runs the DOM-rendering replacement check
(`studio/checks/l86-source-control-compact.render.check.mjs`, esbuild +
happy-dom, same pattern as `test_creator_render_js.py`), which mounts the
real component against a fake fetch and asserts on the rendered DOM.

Run node studio/checks/l86-source-control-compact.render.check.mjs by hand
to see the same checks with their individual pass/fail lines.
"""
from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parent.parent
_HAS_NODE = shutil.which("node") is not None
_SKIP = pytest.mark.skipif(not _HAS_NODE, reason="node needed")


@_SKIP
def test_l86_source_control_compact_render():
    result = subprocess.run(
        ["node", "studio/checks/l86-source-control-compact.render.check.mjs"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok l86-source-control-compact" in result.stdout
